from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from printing_agent.bootstrap import Container, build_container
from printing_agent.config import get_settings
from printing_agent.domain import RevisionMode, WorkflowState
from printing_agent.errors import ConflictError, NotFoundError, PrintingAgentError


class CreateWorkflowRequest(BaseModel):
    requirement: str = Field(min_length=1, max_length=20_000)
    printer_name: str = Field(default="simulator", min_length=1, max_length=100)


class ApproveArtifactRequest(BaseModel):
    artifact_version: int = Field(ge=1)
    manifest_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    approved_by: str = Field(default="local-web", min_length=1, max_length=200)


class RevisionRequestBody(BaseModel):
    mode: RevisionMode
    feedback: str = Field(min_length=1, max_length=10_000)


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
        artifact_payload = None
        if artifact is not None:
            artifact_payload = artifact.model_dump(
                mode="json",
                exclude={"source_path", "model_path"},
            )
            artifact_payload["source_available"] = artifact.source_path is not None
        return {
            "workflow": workflow.model_dump(mode="json"),
            "artifact": artifact_payload,
            "job": job.model_dump(mode="json") if job else None,
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
        "/api/v1/workflows/{workflow_id}/artifacts/{version}/{filename}",
        response_class=FileResponse,
    )
    async def artifact_file(
        workflow_id: str,
        version: int,
        filename: str,
        request: Request,
    ) -> FileResponse:
        await get_container(request).repository.get_artifact(workflow_id, version)
        path = get_container(request).artifacts.resolve_artifact_file(
            workflow_id,
            version,
            filename,
        )
        media_type = {
            "model.stl": "model/stl",
            "source.scad": "text/plain; charset=utf-8",
            "manifest.json": "application/json",
        }[filename]
        return FileResponse(path, media_type=media_type, filename=filename)

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

    web_dist = Path(__file__).resolve().parents[2] / "web" / "dist"
    if web_dist.is_dir():
        app.mount("/", StaticFiles(directory=web_dist, html=True), name="web")
    return app


app = create_app()


def main() -> None:
    uvicorn.run("printing_agent.api:app", host="127.0.0.1", port=8000, reload=False)
