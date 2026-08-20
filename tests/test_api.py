from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from printing_agent.api import RevisionRequestBody, create_app
from printing_agent.bootstrap import build_container
from printing_agent.cloud_credentials import CloudCredentialStore
from printing_agent.cloud_inventory import DeviceSummary
from printing_agent.config import Settings
from printing_agent.domain import RevisionMode, WorkflowState


class FakeProtector:
    def protect(self, value: bytes) -> bytes:
        return b"protected:" + value[::-1]

    def unprotect(self, value: bytes) -> bytes:
        return value.removeprefix(b"protected:")[::-1]


class FakeInventoryProvider:
    async def validate_token(self, access_token: str, region: str):
        assert access_token == "private-cloud-token"
        assert region in {"global", "china"}
        return (
            DeviceSummary(
                device_id="H2D-PRIVATE-SERIAL",
                name="Workshop H2D",
                model="H2D",
                online=True,
            ),
        )

    async def list_devices(self):
        return await self.validate_token("private-cloud-token", "global")

    async def snapshot(self, device_id: str):
        raise AssertionError(f"Unexpected snapshot request for {device_id}")


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


async def test_slicing_profiles_and_material_mapping_api(
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
        profiles = client.get("/api/v1/slicing-profiles")
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

    assert h2d["spec"]["slicer"]["driver_id"] == "bambu_studio_cli"
    assert material.json()["spec"]["display_name"] == "Red PLA"


async def test_api_exposes_slicing_without_submission_routes(
    settings: Settings,
) -> None:
    app = create_app(await build_container(settings))
    paths = {
        getattr(route, "path", "")
        for route in app.routes
    }

    assert "/api/v1/slicing-profiles" in paths
    assert "/api/v1/workflows/{workflow_id}/slice" in paths
    assert not any(
        "submission-handoff" in path or "bambu-connect" in path
        for path in paths
    )


async def test_fabrication_readiness_contains_no_submission_surface(
    settings: Settings,
) -> None:
    container = await build_container(settings)
    app = create_app(container)

    with TestClient(app) as client:
        response = client.get("/api/v1/slicing/readiness")

    assert response.status_code == 200
    h2d = next(
        item for item in response.json() if item["profile_id"] == "bambu-h2d"
    )
    assert h2d["slicing_capable"] is True
    assert "submission" not in h2d
    serialized = json.dumps(h2d).casefold()
    assert "password" not in serialized
    assert "access_token" not in serialized
    assert "cookie" not in serialized


async def test_cloud_token_setup_is_local_encrypted_and_proxy_blocked(
    settings: Settings,
    tmp_path: Path,
) -> None:
    container = await build_container(settings)
    credential_path = tmp_path / "cloud-credential.json"
    credential_store = CloudCredentialStore(
        credential_path,
        protector=FakeProtector(),
        acl_restrictor=lambda _: None,
    )
    app = create_app(
        replace(
            container,
            cloud_credentials=credential_store,
            inventory=FakeInventoryProvider(),  # type: ignore[arg-type]
        )
    )
    payload = {
        "access_token": "private-cloud-token",
        "region": "global",
        "experimental_acknowledged": True,
    }

    with TestClient(app) as client:
        saved = client.post("/api/v1/cloud-credentials", json=payload)
        rejected = client.post(
            "/api/v1/cloud-credentials",
            json={**payload, "experimental_acknowledged": False},
        )
        proxied = client.post(
            "/api/v1/cloud-credentials",
            headers={"X-Forwarded-For": "100.64.0.2"},
            json=payload,
        )
        status = client.get("/api/v1/cloud-credential-status")
    with TestClient(app, client=("10.18.0.99", 50000)) as client:
        remote = client.post("/api/v1/cloud-credentials", json=payload)

    assert saved.status_code == 201
    assert rejected.status_code == 422
    assert proxied.status_code == 404
    assert remote.status_code == 404
    assert status.json()["configured"] is True
    assert "private-cloud-token" not in saved.text
    assert "private-cloud-token" not in rejected.text
    assert "private-cloud-token" not in credential_path.read_text(encoding="utf-8")


async def test_printer_profile_update_rejects_stale_revision(
    settings: Settings,
) -> None:
    container = await build_container(settings)
    app = create_app(container)

    with TestClient(app) as client:
        profile = client.get("/api/v1/slicing-profiles").json()[0]
        revision = profile["revision"]
        first = client.post(
            f"/api/v1/slicing-profiles/{profile['profile_id']}",
            headers={"If-Match": str(revision)},
            json=profile["spec"],
        )
        stale = client.post(
            f"/api/v1/slicing-profiles/{profile['profile_id']}",
            headers={"If-Match": str(revision)},
            json=profile["spec"],
        )

    assert first.status_code == 201
    assert stale.status_code == 409
    assert "reload" in stale.json()["error"]["message"]


async def test_slicing_profile_cannot_override_server_executable(
    settings: Settings,
) -> None:
    container = await build_container(settings)
    app = create_app(container)
    with TestClient(app) as client:
        profile = next(
            item
            for item in client.get("/api/v1/slicing-profiles").json()
            if item["profile_id"] == "bambu-h2d"
        )
        profile["spec"]["slicer"]["executable_path"] = (
            r"\\attacker\share\payload.exe"
        )
        invalid_profile = json.loads(json.dumps(profile))
        invalid_profile["spec"]["slicer"]["machine_profile_id"] = (
            "C:pyproject.toml"
        )
        rejected = client.post(
            "/api/v1/slicing-profiles/bambu-h2d",
            headers={"If-Match": str(profile["revision"])},
            json=invalid_profile["spec"],
        )
        response = client.post(
            "/api/v1/slicing-profiles/bambu-h2d",
            headers={"If-Match": str(profile["revision"])},
            json=profile["spec"],
        )

    assert rejected.status_code == 422
    assert response.status_code == 201
    assert response.json()["spec"]["slicer"]["executable_path"] is None


def test_fastapi_bind_rejects_non_loopback(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        Settings(
            data_dir=tmp_path,
            database_url=tmp_path / "agent.db",
            artifact_dir=tmp_path / "artifacts",
            candidate_cache_dir=tmp_path / "cache",
            simulator_spool_dir=tmp_path / "spool",
            api_host="0.0.0.0",
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
