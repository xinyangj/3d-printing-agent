from __future__ import annotations

import asyncio
import contextlib
import hashlib
import ipaddress
import json
import re
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

import uvicorn
from fastapi import FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, SecretStr, model_validator

from printing_agent.bambu_studio_session import BambuStudioSessionError
from printing_agent.bootstrap import Container, build_container
from printing_agent.cloud_credentials import (
    CloudRegion,
    CredentialStoreError,
)
from printing_agent.cloud_inventory import CloudInventoryError, mask_identifier
from printing_agent.config import get_settings
from printing_agent.domain import RevisionMode, WorkflowState
from printing_agent.errors import (
    ConflictError,
    NotFoundError,
    PrintingAgentError,
    ValidationError,
)
from printing_agent.fabrication import (
    JobOverrides,
    MaterialDefinitionRevision,
    MaterialDefinitionSpec,
    ProfileOrigin,
    SlicingProfileRevision,
    SlicingProfileSpec,
)
from printing_agent.fabrication_profiles import (
    resolve_profile_dependency_digests,
)
from printing_agent.printers import ProfilePrinterAdapter


class CreateWorkflowRequest(BaseModel):
    requirement: str = Field(min_length=1, max_length=20_000)
    printer_name: str = Field(default="simulator", min_length=1, max_length=100)
    overrides: JobOverrides = Field(default_factory=JobOverrides)


class CreateSlicingCopyRequest(BaseModel):
    profile_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    overrides: JobOverrides = Field(default_factory=JobOverrides)


class ConfirmMaterialAssignmentRequest(BaseModel):
    assignment_id: str
    confirmed_by: str = Field(default="local-web", min_length=1, max_length=200)
    spool_overrides: dict[str, str] = Field(default_factory=dict)


class SaveCloudCredentialRequest(BaseModel):
    access_token: SecretStr
    region: CloudRegion
    experimental_acknowledged: bool

    @model_validator(mode="after")
    def require_acknowledgement(self) -> SaveCloudCredentialRequest:
        if not self.experimental_acknowledged:
            raise ValueError("Experimental private cloud API risk must be acknowledged")
        if not self.access_token.get_secret_value().strip():
            raise ValueError("Bambu Cloud access token cannot be empty")
        return self


class ImportBambuStudioCredentialRequest(BaseModel):
    experimental_acknowledged: bool
    expected_session_ref: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def require_acknowledgement(self) -> ImportBambuStudioCredentialRequest:
        if not self.experimental_acknowledged:
            raise ValueError("Experimental private cloud API risk must be acknowledged")
        return self


class RefreshCloudSnapshotRequest(BaseModel):
    device_id: str | None = Field(default=None, min_length=1, max_length=128)


class BindCloudDeviceRequest(BaseModel):
    device_ref: str = Field(pattern=r"^[a-f0-9]{64}$")


class ApproveArtifactRequest(BaseModel):
    artifact_version: int = Field(ge=1)
    manifest_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    approved_by: str = Field(default="local-web", min_length=1, max_length=200)


class RevisionRequestBody(BaseModel):
    mode: RevisionMode
    feedback: str = Field(min_length=1, max_length=10_000)
    allowed_part_ids: list[str] | None = Field(default=None, min_length=1, max_length=100)
    part_id: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")

    @model_validator(mode="after")
    def normalize_legacy_part_id(self) -> RevisionRequestBody:
        if self.part_id is not None:
            if self.allowed_part_ids is not None:
                raise ValueError("Use either allowed_part_ids or the legacy part_id, not both")
            self.allowed_part_ids = [self.part_id]
        if self.allowed_part_ids is not None:
            if len(self.allowed_part_ids) != len(set(self.allowed_part_ids)):
                raise ValueError("Allowed part IDs must be unique")
            if any(
                not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", item)
                for item in self.allowed_part_ids
            ):
                raise ValueError("Allowed part IDs are invalid")
        return self


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    if not hasattr(app.state, "container"):
        app.state.container = await build_container()
    container: Container = app.state.container
    worker_task = asyncio.create_task(container.worker.run())
    try:
        yield
    finally:
        container.worker.stop()
        with contextlib.suppress(asyncio.CancelledError):
            await worker_task
        await container.catalog.close()


def create_app(container: Container | None = None) -> FastAPI:
    app = FastAPI(
        title="3D Printing Agent",
        version="0.1.0",
        lifespan=lifespan,
    )
    if container is not None:
        app.state.container = container
    origins = container.settings.cors_origins if container else get_settings().cors_origins
    credential_mutation_lock = asyncio.Lock()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def cache_static_content(
        request: Request,
        call_next,
    ):
        response = await call_next(request)
        if request.method in {"GET", "HEAD"}:
            content_type = response.headers.get("content-type", "")
            if content_type.startswith("text/html"):
                response.headers["Cache-Control"] = (
                    "no-store, no-cache, must-revalidate"
                )
            elif request.url.path.startswith("/assets/"):
                response.headers["Cache-Control"] = (
                    "public, max-age=31536000, immutable"
                )
        return response

    @app.exception_handler(PrintingAgentError)
    async def printing_error_handler(
        request: Request,
        error: PrintingAgentError,
    ) -> JSONResponse:
        status = (
            404
            if isinstance(error, NotFoundError)
            else 409
            if isinstance(error, ConflictError)
            else 422
        )
        return JSONResponse(
            status_code=status,
            content={"error": {"code": error.code, "message": error.message}},
        )

    @app.exception_handler(RequestValidationError)
    async def request_validation_handler(
        request: Request,
        error: RequestValidationError,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "invalid_request",
                    "details": [
                        {
                            "loc": item["loc"],
                            "type": item["type"],
                            "msg": item["msg"],
                        }
                        for item in error.errors()
                    ],
                }
            },
        )

    def get_container(request: Request) -> Container:
        return request.app.state.container

    def is_loopback_hostname(value: str | None) -> bool:
        if not value:
            return False
        if value.casefold() == "localhost":
            return True
        try:
            return ipaddress.ip_address(value).is_loopback
        except ValueError:
            return False

    def request_hostname(value: str | None, *, origin: bool = False) -> str | None:
        if not value:
            return None
        try:
            parsed = urlsplit(value if origin else f"//{value}")
            return parsed.hostname
        except ValueError:
            return None

    def require_local_credential_request(request: Request) -> None:
        forwarded_headers = {
            "forwarded",
            "x-forwarded-for",
            "x-real-ip",
        }
        client_host = request.client.host if request.client else None
        host = request_hostname(request.headers.get("host"))
        origin_header = request.headers.get("origin")
        origin = request_hostname(origin_header, origin=True)
        test_request = client_host == "testclient" and host == "testserver"
        if (
            request.client is None
            or not (is_loopback_hostname(client_host) or test_request)
            or not (is_loopback_hostname(host) or test_request)
            or (
                origin_header is not None
                and not (
                    is_loopback_hostname(origin)
                    or (test_request and origin == "testserver")
                )
            )
            or any(request.headers.get(name) for name in forwarded_headers)
        ):
            raise NotFoundError("Endpoint was not found")

    async def bambu_studio_connection_status(
        container: Container,
    ) -> dict[str, object]:
        studio_status, studio_session = await asyncio.to_thread(
            container.bambu_studio_session.inspect
        )
        credential_status = await asyncio.to_thread(
            container.cloud_credentials.status
        )
        relation = "not_configured"
        import_required = studio_session is not None
        message = studio_status.message

        if credential_status.configured:
            relation = "comparison_unavailable"
            import_required = False
            if studio_session is not None:
                try:
                    credentials = await asyncio.to_thread(
                        container.cloud_credentials.load
                    )
                except CredentialStoreError:
                    credentials = None
                if credentials is not None and credentials.region != studio_session.region:
                    relation = "different_account"
                    import_required = True
                    message = (
                        "Bambu Studio uses a different cloud region; import the "
                        "new account to replace this app's connection"
                    )
                elif (
                    credentials is not None
                    and credentials.source == "bambu_studio"
                    and credentials.account_fingerprint
                    and studio_session.account_fingerprint
                ):
                    if not secrets.compare_digest(
                        credentials.account_fingerprint,
                        studio_session.account_fingerprint,
                    ):
                        relation = "different_account"
                        import_required = True
                        message = (
                            "Bambu Studio switched accounts; import the new account "
                            "to replace this app's connection"
                        )
                    elif secrets.compare_digest(
                        credentials.access_token.get_secret_value(),
                        studio_session.access_token.get_secret_value(),
                    ):
                        relation = "same_account_current_session"
                        message = "Bambu Studio and this app use the same account"
                    else:
                        relation = "same_account_new_session"
                        import_required = True
                        message = (
                            "Bambu Studio refreshed this account session; import it "
                            "to update this app"
                        )
                elif credentials is not None:
                    message = (
                        "This credential has no Studio account metadata; automatic "
                        "same-region account comparison is unavailable"
                    )

        payload = studio_status.model_dump(mode="json")
        payload.update(
            {
                "connection_relation": relation,
                "import_required": import_required,
                "connected_account_hint": credential_status.account_hint,
                "session_ref": (
                    studio_session.session_ref if studio_session is not None else None
                ),
                "message": message,
            }
        )
        return payload

    def _masked_slicing_snapshot(snapshot) -> dict[str, object]:
        payload = snapshot.model_dump(mode="json")
        serial = payload["profile"].get("cloud_device_serial")
        if isinstance(serial, str) and serial:
            payload["profile"]["cloud_device_serial"] = mask_identifier(serial)
        return payload

    def _masked_profile(profile) -> dict[str, object]:
        payload = profile.model_dump(mode="json")
        serial = payload["spec"].get("cloud_device_serial")
        if isinstance(serial, str) and serial:
            payload["cloud_device_ref"] = hashlib.sha256(
                serial.encode()
            ).hexdigest()
            payload["spec"]["cloud_device_serial"] = mask_identifier(serial)
        else:
            payload["cloud_device_ref"] = None
        return payload

    async def serialize_workflow(
        container: Container,
        workflow,
    ) -> dict[str, object]:
        artifact = (
            await container.repository.get_artifact(
                workflow.id,
                workflow.active_artifact_version,
            )
            if workflow.active_artifact_version is not None
            else None
        )
        job = await container.repository.get_latest_job(workflow.id)
        revision_failure = await container.repository.get_active_revision_failure(
            workflow.id
        )
        revision_verification = (
            await container.repository.get_latest_revision_verification(workflow.id)
        )
        try:
            printer_snapshot = (
                await container.repository.get_workflow_printer_snapshot(workflow.id)
            )
        except NotFoundError:
            printer_snapshot = None
        try:
            material_assignment = (
                await container.repository.get_latest_material_assignment(workflow.id)
            )
        except NotFoundError:
            material_assignment = None
        try:
            slice_job = await container.repository.get_latest_slice_job(workflow.id)
        except NotFoundError:
            slice_job = None
        sliced_artifact = None
        try:
            cloud_snapshot = (
        await container.repository.get_latest_cloud_device_snapshot(
            workflow.id
        )
            )
        except NotFoundError:
            cloud_snapshot = None
        if (
            material_assignment is not None
            and cloud_snapshot is not None
            and material_assignment.cloud_snapshot_digest
            != cloud_snapshot.digest
        ):
            material_assignment = None
        if slice_job is not None and slice_job.status.value == "ready":
            try:
                sliced_artifact = await container.repository.get_sliced_artifact(
                    slice_job.id
                )
            except NotFoundError:
                sliced_artifact = None
        artifact_payload = None
        if artifact is not None:
            artifact_payload = artifact.model_dump(
                mode="json",
                exclude={
                    "source_path",
                    "model_path",
                    "project_path",
                    "three_mf_path",
                },
            )
            artifact_payload["source_available"] = artifact.source_path is not None
            artifact_payload["downloads"] = [
                {
                    "role": item.role,
                    "path": item.path,
                    "media_type": item.media_type,
                    "part_id": item.part_id,
                }
                for item in artifact.files
                if item.role
                in {
                    "openscad_source",
                    "part_project",
                    "multipart_3mf",
                    "combined_stl",
                    "publisher_combined_stl",
                    "part_stl",
                }
            ]
            bundle = container.artifacts.build_artifact_bundle(
                artifact.workflow_id,
                artifact.version,
            )
            artifact_payload["package"] = {
                "url": (
                    f"/api/v1/workflows/{artifact.workflow_id}/artifacts/"
                    f"{artifact.version}/package"
                ),
                "size_bytes": bundle.stat().st_size,
                "filename": f"artifact-{artifact.workflow_id[:8]}-v{artifact.version}.zip",
            }
        return {
            "workflow": workflow.model_dump(mode="json"),
            "artifact": artifact_payload,
            "job": job.model_dump(mode="json") if job else None,
            "revision_failure": (
                revision_failure.model_dump(mode="json")
                if revision_failure is not None
                else None
            ),
            "revision_verification": (
                revision_verification.model_dump(mode="json")
                if revision_verification is not None
                else None
            ),
            "printer_snapshot": (
                _masked_slicing_snapshot(printer_snapshot)
                if printer_snapshot is not None
                else None
            ),
            "material_assignment": (
                material_assignment.model_dump(mode="json")
                if material_assignment is not None
                else None
            ),
            "slice_job": (
                slice_job.model_dump(mode="json")
                if slice_job is not None
                else None
            ),
            "sliced_artifact": (
                {
                    **sliced_artifact.model_dump(
                        mode="json",
                        exclude={"path", "thumbnail_path"},
                    ),
                    "thumbnail_available": (
                        sliced_artifact.thumbnail_path is not None
                    ),
                }
                if sliced_artifact is not None
                else None
            ),
            "cloud_snapshot": (
                cloud_snapshot.masked_dump()
                if cloud_snapshot is not None
                else None
            ),
        }

    @app.get("/api/v1/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/api/v1/workflows", status_code=202)
    async def create_workflow(
        body: CreateWorkflowRequest,
        request: Request,
    ) -> dict[str, object]:
        workflow = await get_container(request).application.create_workflow(
            body.requirement,
            body.printer_name,
            body.overrides,
        )
        return workflow.model_dump(mode="json")

    @app.get("/api/v1/workflows")
    async def list_workflows(request: Request) -> list[dict[str, object]]:
        container = get_container(request)
        workflows = await container.repository.list_workflows()
        return [await serialize_workflow(container, workflow) for workflow in workflows]

    @app.get("/api/v1/workflows/{workflow_id}")
    async def get_workflow(workflow_id: str, request: Request) -> dict[str, object]:
        container = get_container(request)
        workflow = await container.repository.get_workflow(workflow_id)
        return await serialize_workflow(container, workflow)

    @app.get("/api/v1/workflows/{workflow_id}/events")
    async def workflow_events(
        workflow_id: str,
        request: Request,
        last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    ) -> StreamingResponse:
        container = get_container(request)
        await container.repository.get_workflow(workflow_id)
        start_id = int(last_event_id or 0)

        async def stream() -> AsyncIterator[str]:
            event_id = start_id
            while not await request.is_disconnected():
                events = await container.repository.list_events(workflow_id, event_id)
                for event in events:
                    event_id = event.id or event_id
                    payload = json.dumps(event.model_dump(mode="json"))
                    yield f"id: {event_id}\nevent: {event.kind}\ndata: {payload}\n\n"
                workflow = await container.repository.get_workflow(workflow_id)
                if workflow.state in {
                    WorkflowState.COMPLETED,
                    WorkflowState.CANCELLED,
                    WorkflowState.PREPARATION_FAILED,
                    WorkflowState.PRINT_FAILED,
                }:
                    yield "event: stream.complete\ndata: {}\n\n"
                    return
                yield ": keepalive\n\n"
                await asyncio.sleep(1)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get(
        "/api/v1/workflows/{workflow_id}/artifacts/{version}/package",
        response_class=FileResponse,
    )
    async def artifact_package(
        workflow_id: str,
        version: int,
        request: Request,
    ) -> FileResponse:
        await get_container(request).repository.get_artifact(workflow_id, version)
        path = get_container(request).artifacts.build_artifact_bundle(workflow_id, version)
        return FileResponse(
            path,
            media_type="application/zip",
            filename=f"artifact-{workflow_id[:8]}-v{version}.zip",
        )

    @app.get(
        "/api/v1/workflows/{workflow_id}/artifacts/{version}/{filename:path}",
        response_class=FileResponse,
    )
    async def artifact_file(
        workflow_id: str,
        version: int,
        filename: str,
        request: Request,
    ) -> FileResponse:
        artifact = await get_container(request).repository.get_artifact(workflow_id, version)
        path = get_container(request).artifacts.resolve_artifact_file(
            workflow_id,
            version,
            filename,
        )
        aliases = {
            "model.stl": "model/stl",
            "model.3mf": "model/3mf",
            "source.scad": "text/plain; charset=utf-8",
            "manifest.json": "application/json",
        }
        media_type = aliases.get(filename)
        if media_type is None:
            media_type = next(
                (item.media_type for item in artifact.files if item.path == filename),
                "application/octet-stream",
            )
        return FileResponse(path, media_type=media_type, filename=Path(filename).name)

    @app.post("/api/v1/workflows/{workflow_id}/approval", status_code=202)
    async def approve_artifact(
        workflow_id: str,
        body: ApproveArtifactRequest,
        request: Request,
    ) -> dict[str, str]:
        await get_container(request).application.approve(
            workflow_id,
            body.artifact_version,
            body.manifest_digest,
            body.approved_by,
        )
        return {"status": "approved"}

    @app.post("/api/v1/workflows/{workflow_id}/revisions", status_code=202)
    async def request_revision(
        workflow_id: str,
        body: RevisionRequestBody,
        request: Request,
    ) -> dict[str, str]:
        await get_container(request).application.request_revision(
            workflow_id,
            body.mode,
            body.feedback,
            body.allowed_part_ids,
        )
        return {"status": "revision_queued"}

    @app.post("/api/v1/workflows/{workflow_id}/print", status_code=202)
    async def request_print(workflow_id: str, request: Request) -> dict[str, str]:
        await get_container(request).application.request_print(workflow_id)
        return {"status": "print_queued"}

    @app.post("/api/v1/workflows/{workflow_id}/copies", status_code=201)
    async def copy_workflow(
        workflow_id: str,
        request: Request,
    ) -> dict[str, object]:
        container = get_container(request)
        workflow = await container.application.copy_workflow(workflow_id)
        return await serialize_workflow(container, workflow)

    @app.post(
        "/api/v1/workflows/{workflow_id}/slicing-copies",
        status_code=201,
    )
    async def create_slicing_copy(
        workflow_id: str,
        body: CreateSlicingCopyRequest,
        request: Request,
    ) -> dict[str, object]:
        container = get_container(request)
        workflow = await container.application.copy_workflow(
            workflow_id,
            target_printer_name=body.profile_id,
            overrides=body.overrides,
        )
        return await serialize_workflow(container, workflow)

    @app.post("/api/v1/workflows/{workflow_id}/archive")
    async def archive_workflow(
        workflow_id: str,
        request: Request,
    ) -> dict[str, object]:
        container = get_container(request)
        workflow = await container.application.archive_workflow(workflow_id)
        return await serialize_workflow(container, workflow)

    @app.post("/api/v1/workflows/{workflow_id}/restore")
    async def restore_workflow(
        workflow_id: str,
        request: Request,
    ) -> dict[str, object]:
        container = get_container(request)
        workflow = await container.application.restore_workflow(workflow_id)
        return await serialize_workflow(container, workflow)

    @app.post("/api/v1/workflows/{workflow_id}/cancel", status_code=202)
    async def cancel_workflow(workflow_id: str, request: Request) -> dict[str, str]:
        await get_container(request).application.cancel(workflow_id)
        return {"status": "cancelled"}

    @app.get("/api/v1/printers")
    async def list_printers(request: Request) -> list[dict[str, object]]:
        capabilities = await get_container(request).printers.capabilities()
        return [item.model_dump(mode="json") for item in capabilities]

    @app.get("/api/v1/slicing-profiles")
    async def list_slicing_profiles(request: Request) -> list[dict[str, object]]:
        profiles = await get_container(request).repository.list_printer_profiles()
        return [_masked_profile(item) for item in profiles]

    @app.get("/api/v1/slicing/readiness")
    async def slicing_readiness(
        request: Request,
        profile_id: str | None = None,
        profile_revision: int | None = None,
    ) -> list[dict[str, object]]:
        if profile_revision is not None and profile_id is None:
            raise ValidationError(
                "profile_revision requires a matching profile_id"
            )
        return await get_container(request).application.fabrication_readiness(
            profile_id,
            profile_revision,
        )

    @app.get("/api/v1/cloud-credential-status")
    async def cloud_credential_status(
        request: Request,
    ) -> dict[str, object]:
        status = await asyncio.to_thread(
            get_container(request).cloud_credentials.status
        )
        return status.model_dump(mode="json")

    @app.get("/api/v1/local-account-management")
    async def local_account_management(
        request: Request,
    ) -> dict[str, bool]:
        require_local_credential_request(request)
        return {"available": True}

    @app.get("/api/v1/bambu-studio-session")
    async def bambu_studio_session_status(
        request: Request,
    ) -> dict[str, object]:
        require_local_credential_request(request)
        return await bambu_studio_connection_status(get_container(request))

    @app.post("/api/v1/bambu-studio-session/open")
    async def open_bambu_studio(
        request: Request,
    ) -> dict[str, object]:
        require_local_credential_request(request)
        container = get_container(request)
        try:
            await asyncio.to_thread(
                container.bambu_studio_session.open
            )
        except BambuStudioSessionError as exc:
            raise ValidationError(exc.message) from exc
        return await bambu_studio_connection_status(container)

    @app.post("/api/v1/cloud-credentials", status_code=201)
    async def save_cloud_credential(
        body: SaveCloudCredentialRequest,
        request: Request,
    ) -> dict[str, object]:
        require_local_credential_request(request)
        container = get_container(request)
        token = body.access_token.get_secret_value().strip()
        async with credential_mutation_lock:
            try:
                devices = await container.inventory.validate_token(
                    token,
                    body.region,
                )
                status = await asyncio.to_thread(
                    container.cloud_credentials.store,
                    token,
                    body.region,
                )
            except (CredentialStoreError, CloudInventoryError) as exc:
                raise ValidationError(str(exc)) from exc
        return {
            "credential": status.model_dump(mode="json"),
            "devices": [item.masked_dump() for item in devices],
        }

    @app.post(
        "/api/v1/cloud-credentials/import-bambu-studio",
        status_code=201,
    )
    async def import_bambu_studio_credential(
        body: ImportBambuStudioCredentialRequest,
        request: Request,
    ) -> dict[str, object]:
        require_local_credential_request(request)
        container = get_container(request)
        token = ""
        async with credential_mutation_lock:
            try:
                session = await asyncio.to_thread(
                    container.bambu_studio_session.read_signed_in_session
                )
                if not secrets.compare_digest(
                    session.session_ref,
                    body.expected_session_ref,
                ):
                    raise BambuStudioSessionError(
                        "session_unreadable",
                        "Bambu Studio changed accounts or sessions; review the "
                        "latest account before importing",
                    )
                token = session.access_token.get_secret_value()
                devices = await container.inventory.validate_token(
                    token,
                    session.region,
                )
                status = await asyncio.to_thread(
                    container.cloud_credentials.store,
                    token,
                    session.region,
                    source="bambu_studio",
                    account_fingerprint=session.account_fingerprint,
                    account_hint=session.account_hint,
                )
            except (
                BambuStudioSessionError,
                CredentialStoreError,
                CloudInventoryError,
            ) as exc:
                message = (
                    exc.message
                    if isinstance(exc, BambuStudioSessionError)
                    else str(exc)
                )
                raise ValidationError(message) from exc
            finally:
                token = ""
        return {
            "credential": status.model_dump(mode="json"),
            "studio": {
                "region": session.region,
                "region_source": session.region_source,
                "account_hint": session.account_hint,
                "session_updated_at": session.session_updated_at.isoformat(),
            },
            "devices": [item.masked_dump() for item in devices],
        }

    @app.delete("/api/v1/cloud-credentials")
    async def clear_cloud_credential(
        request: Request,
    ) -> dict[str, object]:
        require_local_credential_request(request)
        async with credential_mutation_lock:
            try:
                status = await asyncio.to_thread(
                    get_container(request).cloud_credentials.clear
                )
            except CredentialStoreError as exc:
                raise ValidationError(str(exc)) from exc
        return status.model_dump(mode="json")

    @app.get("/api/v1/cloud-devices")
    async def list_cloud_devices(
        request: Request,
    ) -> list[dict[str, object]]:
        devices = await get_container(request).application.list_cloud_devices()
        return [item.masked_dump() for item in devices]

    @app.post(
        "/api/v1/workflows/{workflow_id}/cloud-snapshot",
        status_code=201,
    )
    async def refresh_cloud_snapshot(
        workflow_id: str,
        body: RefreshCloudSnapshotRequest,
        request: Request,
    ) -> dict[str, object]:
        snapshot = await get_container(
            request
        ).application.refresh_cloud_snapshot(
            workflow_id,
            body.device_id,
        )
        return snapshot.masked_dump()

    @app.post("/api/v1/slicing-profiles/{profile_id}", status_code=201)
    async def save_slicing_profile(
        profile_id: str,
        body: SlicingProfileSpec,
        request: Request,
    ) -> dict[str, object]:
        container = get_container(request)
        body = body.model_copy(
            update={
                "slicer": body.slicer.model_copy(
                    update={
                        "executable_path": container.settings.bambu_studio_path,
                        "resource_root": (
                            str(container.settings.bambu_studio_resource_dir)
                            if container.settings.bambu_studio_resource_dir
                            else None
                        ),
                    }
                )
            }
        )
        raw_expected_revision = request.headers.get("if-match")
        expected_revision: int | None = None
        if raw_expected_revision is not None:
            normalized = raw_expected_revision.strip().strip('"')
            if not normalized.isdigit():
                raise ConflictError("If-Match must contain a printer profile revision")
            expected_revision = int(normalized)
        try:
            current = await container.repository.get_printer_profile(profile_id)
            revision = current.revision + 1
            body = body.model_copy(
                update={
                    "cloud_region": current.spec.cloud_region,
                    "cloud_device_name": current.spec.cloud_device_name,
                    "cloud_device_serial": current.spec.cloud_device_serial,
                }
            )
        except NotFoundError:
            revision = 1
        profile = SlicingProfileRevision(
            profile_id=profile_id,
            revision=revision,
            origin=ProfileOrigin.CUSTOM,
            spec=body,
        ).with_digest()
        await container.repository.save_printer_profile(
            profile,
            expected_revision=expected_revision,
        )
        container.printers.upsert(ProfilePrinterAdapter(profile))
        return _masked_profile(profile)

    @app.post("/api/v1/slicing-profiles/{profile_id}/archive")
    async def archive_slicing_profile(
        profile_id: str,
        request: Request,
    ) -> dict[str, str]:
        await get_container(request).repository.archive_printer_profile(profile_id)
        return {"status": "archived"}

    @app.post(
        "/api/v1/slicing-profiles/{profile_id}/cloud-device",
        status_code=201,
    )
    async def bind_cloud_device(
        profile_id: str,
        body: BindCloudDeviceRequest,
        request: Request,
    ) -> dict[str, object]:
        container = get_container(request)
        profile = await container.application.bind_cloud_device(
            profile_id,
            body.device_ref,
        )
        return _masked_profile(profile)

    @app.get("/api/v1/materials")
    async def list_materials(request: Request) -> list[dict[str, object]]:
        values = await get_container(request).repository.list_material_definitions()
        return [item.model_dump(mode="json") for item in values]

    @app.post("/api/v1/materials/{material_id}", status_code=201)
    async def save_material(
        material_id: str,
        body: MaterialDefinitionSpec,
        request: Request,
    ) -> dict[str, object]:
        repository = get_container(request).repository
        digests: dict[str, str] | None = None
        for profile in await repository.list_printer_profiles():
            root = profile.spec.slicer.resource_root
            if root is None:
                continue
            try:
                digests = await asyncio.to_thread(
                    resolve_profile_dependency_digests,
                    root,
                    body.slicer_filament_profile_id,
                    "filament",
                )
            except PrintingAgentError:
                continue
            if digests:
                break
        if not digests:
            raise ValidationError(
                "Filament profile could not be pinned from an installed slicer "
                "resource directory"
            )
        body = body.model_copy(
            update={
                "slicer_profile_dependency_digests": digests,
                "slicer_profile_digest": next(iter(digests.values())),
            }
        )
        try:
            current = await repository.get_material_definition(material_id)
            revision = current.revision + 1
        except NotFoundError:
            revision = 1
        material = MaterialDefinitionRevision(
            material_id=material_id,
            revision=revision,
            spec=body,
        ).with_digest()
        await repository.save_material_definition(material)
        return material.model_dump(mode="json")

    @app.post(
        "/api/v1/workflows/{workflow_id}/material-assignment",
        status_code=201,
    )
    async def propose_material_assignment(
        workflow_id: str,
        request: Request,
    ) -> dict[str, object]:
        value = await get_container(
            request
        ).application.propose_material_assignment(workflow_id)
        return value.model_dump(mode="json")

    @app.post(
        "/api/v1/workflows/{workflow_id}/material-assignment/confirm"
    )
    async def confirm_material_assignment(
        workflow_id: str,
        body: ConfirmMaterialAssignmentRequest,
        request: Request,
    ) -> dict[str, object]:
        value = await get_container(
            request
        ).application.confirm_material_assignment(
            workflow_id,
            body.assignment_id,
            body.confirmed_by,
            body.spool_overrides,
        )
        return value.model_dump(mode="json")

    @app.post("/api/v1/workflows/{workflow_id}/slice", status_code=202)
    async def request_slice(
        workflow_id: str,
        request: Request,
    ) -> dict[str, object]:
        value = await get_container(request).application.request_slice(workflow_id)
        return value.model_dump(mode="json")

    @app.get("/api/v1/workflows/{workflow_id}/slices/{slice_job_id}/download")
    async def download_slice(
        workflow_id: str,
        slice_job_id: str,
        request: Request,
    ) -> FileResponse:
        artifact = await get_container(request).repository.get_sliced_artifact(
            slice_job_id
        )
        if artifact.workflow_id != workflow_id:
            raise NotFoundError("Sliced artifact was not found")
        path = await asyncio.to_thread(lambda: Path(artifact.path).resolve())
        root = await asyncio.to_thread(
            get_container(request).settings.slice_dir.resolve
        )
        if root not in path.parents or not await asyncio.to_thread(path.is_file):
            raise ConflictError("Sliced artifact path is unavailable")
        return FileResponse(
            path,
            media_type="model/3mf",
            filename=path.name,
        )

    @app.get("/api/v1/workflows/{workflow_id}/slices/{slice_job_id}/manifest")
    async def download_slice_manifest(
        workflow_id: str,
        slice_job_id: str,
        request: Request,
    ) -> FileResponse:
        artifact = await get_container(request).repository.get_sliced_artifact(
            slice_job_id
        )
        if artifact.workflow_id != workflow_id:
            raise NotFoundError("Sliced artifact was not found")
        manifest = await asyncio.to_thread(
            lambda: Path(artifact.path).resolve().parent
            / "slice-manifest.json"
        )
        root = await asyncio.to_thread(
            get_container(request).settings.slice_dir.resolve
        )
        if root not in manifest.parents or not await asyncio.to_thread(
            manifest.is_file
        ):
            raise ConflictError("Slice manifest is unavailable")
        return FileResponse(
            manifest,
            media_type="application/json",
            filename=f"slice-{slice_job_id[:8]}-manifest.json",
        )

    @app.get("/api/v1/workflows/{workflow_id}/slices/{slice_job_id}/thumbnail")
    async def download_slice_thumbnail(
        workflow_id: str,
        slice_job_id: str,
        request: Request,
    ) -> FileResponse:
        artifact = await get_container(request).repository.get_sliced_artifact(
            slice_job_id
        )
        if artifact.workflow_id != workflow_id or not artifact.thumbnail_path:
            raise NotFoundError("Sliced plate thumbnail was not found")
        thumbnail = await asyncio.to_thread(
            lambda: Path(artifact.thumbnail_path).resolve()
        )
        root = await asyncio.to_thread(
            get_container(request).settings.slice_dir.resolve
        )
        if root not in thumbnail.parents or not await asyncio.to_thread(
            thumbnail.is_file
        ):
            raise ConflictError("Sliced plate thumbnail is unavailable")
        media_type = (
            "image/png"
            if thumbnail.suffix.casefold() == ".png"
            else "image/jpeg"
        )
        return FileResponse(thumbnail, media_type=media_type)

    web_dist = Path(__file__).resolve().parents[2] / "web" / "dist"
    if web_dist.is_dir():
        app.mount("/", StaticFiles(directory=web_dist, html=True), name="web")
    return app


app = create_app()


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        "printing_agent.api:app",
        host=settings.api_host,
        port=settings.api_port,
        reload=False,
    )
