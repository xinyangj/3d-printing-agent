from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

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


async def test_fabrication_profiles_and_material_inventory_api(
    settings: Settings,
) -> None:
    resource_root = settings.data_dir / "profiles"
    filament_dir = resource_root / "BBL" / "filament"
    filament_dir.mkdir(parents=True)
    (filament_dir / "Bambu PLA Basic @BBL H2D.json").write_text(
        '{"type":"filament","name":"Bambu PLA Basic @BBL H2D"}',
        encoding="utf-8",
    )
    settings = settings.model_copy(
        update={"bambu_studio_resource_dir": resource_root}
    )
    container = await build_container(settings)
    app = create_app(container)

    with TestClient(app) as client:
        profiles = client.get("/api/v1/printer-profiles")
        assert profiles.status_code == 200
        h2d = next(
            item for item in profiles.json() if item["profile_id"] == "bambu-h2d"
        )
        material = client.post(
            "/api/v1/materials/red-pla",
            json={
                "display_name": "Red PLA",
                "family": "pla",
                "modifiers": [],
                "filament_diameter_mm": 1.75,
                "nominal_color": "#FF0000",
                "nozzle_temperature_c": [190, 230],
                "bed_temperature_c": [35, 65],
                "hardened_nozzle_required": False,
                "supported_nozzle_diameters_mm": [0.4],
                "supported_plate_ids": ["textured_pei"],
                "slicer_filament_profile_id": "Bambu PLA Basic @BBL H2D",
            },
        )
        assert material.status_code == 201
        assert material.json()["spec"]["slicer_profile_dependency_digests"]
        spool = client.post(
            "/api/v1/spools",
            json={
                "id": "spool-red",
                "material_id": "red-pla",
                "material_revision": 1,
                "material_digest": material.json()["digest"],
                "initial_weight_g": 1000,
                "remaining_weight_g": 900,
                "status": "loaded",
                "printer_profile_id": "bambu-h2d",
                "slot_id": "ams1_1",
            },
        )
        assert spool.status_code == 201
        listing = client.get(
            "/api/v1/spools?printer_profile_id=bambu-h2d"
        )

    assert h2d["spec"]["submission"]["driver_id"] == "bambu_connect_cloud"
    assert listing.json()[0]["slot_id"] == "ams1_1"


async def test_remote_mutations_require_admin_token(settings: Settings) -> None:
    secured = settings.model_copy(
        update={"admin_api_token": SecretStr("secret-token")}
    )
    container = await build_container(secured)
    app = create_app(container)
    profile = (
        await container.repository.get_printer_profile("bambu-h2d")
    )

    with TestClient(app, client=("10.18.0.99", 50000)) as client:
        denied = client.post(
            "/api/v1/printer-profiles/test-profile",
            json=profile.spec.model_dump(mode="json"),
        )
        allowed = client.post(
            "/api/v1/printer-profiles/test-profile",
            headers={"X-Printing-Agent-Token": "secret-token"},
            json=profile.spec.model_dump(mode="json"),
        )
    with TestClient(app, client=("127.0.0.1", 50001)) as client:
        loopback_denied = client.post(
            "/api/v1/printer-profiles/loopback-profile",
            json=profile.spec.model_dump(mode="json"),
        )

    assert denied.status_code == 401
    assert allowed.status_code == 201
    assert loopback_denied.status_code == 401


async def test_printer_profile_update_rejects_stale_revision(
    settings: Settings,
) -> None:
    container = await build_container(settings)
    app = create_app(container)

    with TestClient(app) as client:
        profile = client.get("/api/v1/printer-profiles").json()[0]
        revision = profile["revision"]
        first = client.post(
            f"/api/v1/printer-profiles/{profile['profile_id']}",
            headers={"If-Match": str(revision)},
            json=profile["spec"],
        )
        stale = client.post(
            f"/api/v1/printer-profiles/{profile['profile_id']}",
            headers={"If-Match": str(revision)},
            json=profile["spec"],
        )

    assert first.status_code == 201
    assert stale.status_code == 409
    assert "reload" in stale.json()["error"]["message"]


def test_admin_token_rejects_short_values(tmp_path) -> None:
    with pytest.raises(ValidationError, match="at least 32"):
        Settings(
            data_dir=tmp_path,
            database_url=tmp_path / "agent.db",
            artifact_dir=tmp_path / "artifacts",
            candidate_cache_dir=tmp_path / "cache",
            simulator_spool_dir=tmp_path / "spool",
            admin_api_token="short",
        )


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
