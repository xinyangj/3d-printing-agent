from __future__ import annotations

import asyncio
import contextlib
import json
import re
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, model_validator

from printing_agent.bootstrap import Container, build_container
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
    PhysicalSpool,
    PrinterProfileRevision,
    PrinterProfileSpec,
    ProfileOrigin,
)
from printing_agent.fabrication_profiles import (
    resolve_profile_dependency_digests,
)
from printing_agent.printers import ProfilePrinterAdapter


class CreateWorkflowRequest(BaseModel):
    requirement: str = Field(min_length=1, max_length=20_000)
    printer_name: str = Field(default="simulator", min_length=1, max_length=100)
    overrides: JobOverrides = Field(default_factory=JobOverrides)


class ConfirmMaterialAssignmentRequest(BaseModel):
    assignment_id: str
    confirmed_by: str = Field(default="local-web", min_length=1, max_length=200)


class ConfirmSubmissionRequest(BaseModel):
    submitted: bool
    confirmed_by: str = Field(default="local-web", min_length=1, max_length=200)


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
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def protect_remote_mutations(
        request: Request,
        call_next,
    ):
        if (
            request.method not in {"GET", "HEAD", "OPTIONS"}
            and request.url.path.startswith("/api/")
            and request.client is not None
            and request.client.host != "testclient"
        ):
            settings = get_container(request).settings
            configured = (
                settings.admin_api_token.get_secret_value()
                if settings.admin_api_token is not None
                else None
            )
            supplied = request.headers.get("x-printing-agent-token")
            if configured is None:
                return JSONResponse(
                    status_code=503,
                    content={
                        "error": {
                            "code": "mutation_authentication_unconfigured",
                            "message": (
                                "Mutations require "
                                "PRINTING_AGENT_ADMIN_API_TOKEN"
                            ),
                        }
                    },
                )
            if supplied is None or not secrets.compare_digest(configured, supplied):
                return JSONResponse(
                    status_code=401,
                    content={
                        "error": {
                            "code": "authentication_required",
                            "message": "A valid X-Printing-Agent-Token is required",
                        }
                    },
                )
        return await call_next(request)

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
            content={"error": {"code": "invalid_request", "details": error.errors()}},
        )

    def get_container(request: Request) -> Container:
        return request.app.state.container

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
        if slice_job is not None and slice_job.status.value == "ready":
            try:
                sliced_artifact = await container.repository.get_sliced_artifact(
                    slice_job.id
                )
            except NotFoundError:
                sliced_artifact = None
        try:
            submission_handoff = (
                await container.repository.get_latest_submission_handoff(workflow.id)
            )
        except NotFoundError:
            submission_handoff = None
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
                printer_snapshot.model_dump(mode="json")
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
                sliced_artifact.model_dump(mode="json")
                if sliced_artifact is not None
                else None
            ),
            "submission_handoff": (
                submission_handoff.model_dump(mode="json")
                if submission_handoff is not None
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

    @app.get("/api/v1/printer-profiles")
    async def list_printer_profiles(request: Request) -> list[dict[str, object]]:
        profiles = await get_container(request).repository.list_printer_profiles()
        return [item.model_dump(mode="json") for item in profiles]

    @app.post("/api/v1/printer-profiles/{profile_id}", status_code=201)
    async def save_printer_profile(
        profile_id: str,
        body: PrinterProfileSpec,
        request: Request,
    ) -> dict[str, object]:
        container = get_container(request)
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
        except NotFoundError:
            revision = 1
        profile = PrinterProfileRevision(
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
        return profile.model_dump(mode="json")

    @app.post("/api/v1/printer-profiles/{profile_id}/archive")
    async def archive_printer_profile(
        profile_id: str,
        request: Request,
    ) -> dict[str, str]:
        await get_container(request).repository.archive_printer_profile(profile_id)
        return {"status": "archived"}

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

    @app.get("/api/v1/spools")
    async def list_spools(
        request: Request,
        printer_profile_id: str | None = None,
    ) -> list[dict[str, object]]:
        values = await get_container(request).repository.list_spools(
            printer_profile_id=printer_profile_id
        )
        return [item.model_dump(mode="json") for item in values]

    @app.post("/api/v1/spools", status_code=201)
    async def save_spool(
        body: PhysicalSpool,
        request: Request,
    ) -> dict[str, object]:
        repository = get_container(request).repository
        material = await repository.get_material_definition(
            body.material_id,
            body.material_revision,
        )
        if material.digest != body.material_digest:
            raise ConflictError("Spool references a stale material definition")
        if body.printer_profile_id is not None:
            profile = await repository.get_printer_profile(body.printer_profile_id)
            if body.slot_id not in {item.id for item in profile.spec.material_slots}:
                raise ConflictError("Spool references an unknown printer material slot")
        await repository.save_spool(body)
        return body.model_dump(mode="json")

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

    @app.post(
        "/api/v1/workflows/{workflow_id}/submission-handoff",
        status_code=202,
    )
    async def launch_submission_handoff(
        workflow_id: str,
        request: Request,
    ) -> dict[str, object]:
        value = await get_container(
            request
        ).application.launch_submission_handoff(workflow_id)
        return value.model_dump(mode="json")

    @app.post("/api/v1/workflows/{workflow_id}/submission-handoff/confirm")
    async def confirm_submission_handoff(
        workflow_id: str,
        body: ConfirmSubmissionRequest,
        request: Request,
    ) -> dict[str, object]:
        value = await get_container(
            request
        ).application.confirm_submission_handoff(
            workflow_id,
            submitted=body.submitted,
            confirmed_by=body.confirmed_by,
        )
        return value.model_dump(mode="json")

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
