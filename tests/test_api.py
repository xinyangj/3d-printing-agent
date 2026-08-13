from __future__ import annotations

from fastapi.testclient import TestClient

from printing_agent.api import create_app
from printing_agent.bootstrap import build_container
from printing_agent.config import Settings


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
