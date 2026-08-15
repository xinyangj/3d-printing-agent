from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from printing_agent.api import RevisionRequestBody, create_app
from printing_agent.bootstrap import build_container
from printing_agent.config import Settings
from printing_agent.domain import RevisionMode, WorkflowState


def test_revision_body_supports_optional_and_legacy_edit_scopes() -> None:
    unrestricted = RevisionRequestBody(
        mode=RevisionMode.REFINE_CURRENT,
        feedback="Scale the bottle and lid",
    )
    restricted = RevisionRequestBody(
        mode=RevisionMode.REFINE_CURRENT,
        feedback="Scale these parts",
        allowed_part_ids=["body", "lid"],
    )
    legacy = RevisionRequestBody(
        mode=RevisionMode.REFINE_CURRENT,
        feedback="Scale the lid",
        part_id="lid",
    )

    assert unrestricted.allowed_part_ids is None
    assert restricted.allowed_part_ids == ["body", "lid"]
    assert legacy.allowed_part_ids == ["lid"]
    with pytest.raises(ValidationError):
        RevisionRequestBody(
            mode=RevisionMode.REFINE_CURRENT,
            feedback="Invalid mixed scope",
            part_id="lid",
            allowed_part_ids=["body"],
        )


async def test_health_and_printer_discovery(settings: Settings) -> None:
    container = await build_container(settings)
    app = create_app(container)

    with TestClient(app) as client:
        health = client.get("/api/v1/health")
        printers = client.get("/api/v1/printers")

    assert health.json() == {"status": "ok"}
    assert printers.status_code == 200
    assert printers.json()[0]["name"] == "simulator"


async def test_workflow_list_uses_detail_payload_shape(settings: Settings) -> None:
    container = await build_container(settings)
    workflow = await container.repository.create_workflow("Create a cable guide", "simulator")
    pending = await container.repository.lease_next()
    assert pending is not None
    await container.repository.complete_work(pending.id)
    app = create_app(container)

    with TestClient(app) as client:
        listing = client.get("/api/v1/workflows")
        detail = client.get(f"/api/v1/workflows/{workflow.id}")

    assert listing.status_code == 200
    assert listing.json() == [detail.json()]
    assert listing.json()[0]["artifact"] is None
    assert listing.json()[0]["job"] is None


async def test_workflow_can_be_archived_and_restored(settings: Settings) -> None:
    container = await build_container(settings)
    workflow = await container.repository.create_workflow("Create a cable guide", "simulator")
    pending = await container.repository.lease_next()
    assert pending is not None
    await container.repository.complete_work(pending.id)
    for state in (
        WorkflowState.PLANNING,
        WorkflowState.DISCOVERING,
        WorkflowState.SELECTING,
        WorkflowState.VALIDATING,
        WorkflowState.AWAITING_APPROVAL,
    ):
        await container.repository.transition(workflow.id, state)
    app = create_app(container)

    with TestClient(app) as client:
        archived = client.post(f"/api/v1/workflows/{workflow.id}/archive")
        blocked = client.post(
            f"/api/v1/workflows/{workflow.id}/approval",
            json={
                "artifact_version": 1,
                "manifest_digest": "a" * 64,
                "approved_by": "test",
            },
        )
        restored = client.post(f"/api/v1/workflows/{workflow.id}/restore")

    assert archived.status_code == 200
    assert archived.json()["workflow"]["archived_at"] is not None
    assert archived.json()["workflow"]["state"] == "awaiting_approval"
    assert blocked.status_code == 409
    assert "Restore the archived workflow" in blocked.json()["error"]["message"]
    assert restored.status_code == 200
    assert restored.json()["workflow"]["archived_at"] is None
