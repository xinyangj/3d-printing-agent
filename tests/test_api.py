from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from printing_agent.api import RevisionRequestBody, create_app
from printing_agent.bambu_studio_session import (
    BambuStudioSessionStatus,
    ImportedBambuStudioSession,
)
from printing_agent.bootstrap import build_container
from printing_agent.cloud_credentials import CloudCredentialStore
from printing_agent.cloud_inventory import CloudInventoryError, DeviceSummary
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


class EmptyInventoryProvider(FakeInventoryProvider):
    async def validate_token(self, access_token: str, region: str):
        assert access_token == "private-cloud-token"
        assert region == "china"
        return ()


class RejectingInventoryProvider(FakeInventoryProvider):
    async def validate_token(self, access_token: str, region: str):
        del access_token, region
        raise CloudInventoryError("The Bambu Cloud session was rejected")


class FakeStudioSessionAdapter:
    def __init__(self) -> None:
        self.opened = False
        self.token = "private-cloud-token"
        self.account_hint = "***3456"
        self.account_fingerprint = "a" * 64
        self.session_ref = "c" * 64

    def status(self) -> BambuStudioSessionStatus:
        return BambuStudioSessionStatus(
            installed=True,
            running=self.opened,
            session_present=True,
            signed_in=True,
            region="china",
            region_source="session",
            account_hint=self.account_hint,
            session_updated_at=datetime(2026, 8, 21, tzinfo=UTC),
            message="Bambu Studio is signed in and ready to import",
        )

    def inspect(
        self,
    ) -> tuple[BambuStudioSessionStatus, ImportedBambuStudioSession]:
        return self.status(), self.read_signed_in_session()

    def open(self) -> BambuStudioSessionStatus:
        self.opened = True
        return self.status()

    def read_signed_in_session(self) -> ImportedBambuStudioSession:
        return ImportedBambuStudioSession(
            access_token=self.token,
            region="china",
            region_source="session",
            session_ref=self.session_ref,
            account_hint=self.account_hint,
            account_fingerprint=self.account_fingerprint,
            session_updated_at=datetime(2026, 8, 21, tzinfo=UTC),
        )


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


async def test_bambu_studio_import_is_explicit_local_and_secret_free(
    settings: Settings,
    tmp_path: Path,
) -> None:
    container = await build_container(settings)
    credential_path = tmp_path / "studio-cloud-credential.json"
    credential_store = CloudCredentialStore(
        credential_path,
        protector=FakeProtector(),
        acl_restrictor=lambda _: None,
    )
    studio = FakeStudioSessionAdapter()
    app = create_app(
        replace(
            container,
            bambu_studio_session=studio,  # type: ignore[arg-type]
            cloud_credentials=credential_store,
            inventory=FakeInventoryProvider(),  # type: ignore[arg-type]
        )
    )

    with TestClient(app) as client:
        capability = client.get("/api/v1/local-account-management")
        status = client.get("/api/v1/bambu-studio-session")
        opened = client.post("/api/v1/bambu-studio-session/open")
        rejected = client.post(
            "/api/v1/cloud-credentials/import-bambu-studio",
            json={
                "experimental_acknowledged": False,
                "expected_session_ref": "c" * 64,
            },
        )
        imported = client.post(
            "/api/v1/cloud-credentials/import-bambu-studio",
            json={
                "experimental_acknowledged": True,
                "expected_session_ref": "c" * 64,
            },
        )
        studio.session_ref = "d" * 64
        stale_import = client.post(
            "/api/v1/cloud-credentials/import-bambu-studio",
            json={
                "experimental_acknowledged": True,
                "expected_session_ref": "c" * 64,
            },
        )
        studio.session_ref = "c" * 64
        current = client.get("/api/v1/bambu-studio-session")
        studio.token = "refreshed-private-cloud-token"
        refreshed = client.get("/api/v1/bambu-studio-session")
        studio.token = "private-cloud-token"
        studio.account_hint = "***9999"
        studio.account_fingerprint = "b" * 64
        switched = client.get("/api/v1/bambu-studio-session")
        proxied = client.get(
            "/api/v1/bambu-studio-session",
            headers={"X-Forwarded-For": "100.64.0.2"},
        )
        proxied_import = client.post(
            "/api/v1/cloud-credentials/import-bambu-studio",
            headers={"X-Forwarded-For": "100.64.0.2"},
            json={
                "experimental_acknowledged": True,
                "expected_session_ref": "c" * 64,
            },
        )
        rebound_host = client.get(
            "/api/v1/bambu-studio-session",
            headers={"Host": "attacker.example"},
        )
        rebound_origin = client.post(
            "/api/v1/cloud-credentials/import-bambu-studio",
            headers={"Origin": "https://attacker.example"},
            json={
                "experimental_acknowledged": True,
                "expected_session_ref": "c" * 64,
            },
        )
        proxied_capability = client.get(
            "/api/v1/local-account-management",
            headers={"X-Forwarded-For": "100.64.0.2"},
        )
    with TestClient(app, client=("10.18.0.99", 50000)) as client:
        remote = client.post("/api/v1/bambu-studio-session/open")

    assert capability.json() == {"available": True}
    assert status.status_code == 200
    assert status.json()["account_hint"] == "***3456"
    assert opened.status_code == 200
    assert rejected.status_code == 422
    assert imported.status_code == 201
    assert imported.json()["credential"]["configured"] is True
    assert imported.json()["credential"]["region"] == "china"
    assert imported.json()["credential"]["source"] == "bambu_studio"
    assert imported.json()["credential"]["account_hint"] == "***3456"
    assert len(imported.json()["devices"]) == 1
    assert stale_import.status_code == 422
    assert "review the latest account" in stale_import.json()["error"]["message"]
    assert current.json()["connection_relation"] == "same_account_current_session"
    assert current.json()["import_required"] is False
    assert refreshed.json()["connection_relation"] == "same_account_new_session"
    assert refreshed.json()["import_required"] is True
    assert switched.json()["connection_relation"] == "different_account"
    assert switched.json()["import_required"] is True
    assert switched.json()["connected_account_hint"] == "***3456"
    assert "private-cloud-token" not in imported.text
    assert "synthetic-refresh-token" not in imported.text
    assert "a" * 64 not in imported.text
    assert "private-cloud-token" not in credential_path.read_text(encoding="utf-8")
    assert proxied.status_code == 404
    assert proxied_import.status_code == 404
    assert rebound_host.status_code == 404
    assert rebound_origin.status_code == 404
    assert proxied_capability.status_code == 404
    assert remote.status_code == 404


async def test_bambu_studio_import_accepts_account_with_no_bound_devices(
    settings: Settings,
    tmp_path: Path,
) -> None:
    container = await build_container(settings)
    credential_store = CloudCredentialStore(
        tmp_path / "empty-device-credential.json",
        protector=FakeProtector(),
        acl_restrictor=lambda _: None,
    )
    app = create_app(
        replace(
            container,
            bambu_studio_session=FakeStudioSessionAdapter(),  # type: ignore[arg-type]
            cloud_credentials=credential_store,
            inventory=EmptyInventoryProvider(),  # type: ignore[arg-type]
        )
    )

    with TestClient(app) as client:
        imported = client.post(
            "/api/v1/cloud-credentials/import-bambu-studio",
            json={
                "experimental_acknowledged": True,
                "expected_session_ref": "c" * 64,
            },
        )

    assert imported.status_code == 201
    assert imported.json()["devices"] == []
    assert imported.json()["credential"]["configured"] is True
    assert credential_store.load().region == "china"


async def test_failed_studio_refresh_preserves_existing_credential(
    settings: Settings,
    tmp_path: Path,
) -> None:
    container = await build_container(settings)
    credential_store = CloudCredentialStore(
        tmp_path / "existing-credential.json",
        protector=FakeProtector(),
        acl_restrictor=lambda _: None,
    )
    credential_store.store("working-token", "global")
    app = create_app(
        replace(
            container,
            bambu_studio_session=FakeStudioSessionAdapter(),  # type: ignore[arg-type]
            cloud_credentials=credential_store,
            inventory=RejectingInventoryProvider(),  # type: ignore[arg-type]
        )
    )

    with TestClient(app) as client:
        rejected = client.post(
            "/api/v1/cloud-credentials/import-bambu-studio",
            json={
                "experimental_acknowledged": True,
                "expected_session_ref": "c" * 64,
            },
        )

    assert rejected.status_code == 422
    existing = credential_store.load()
    assert existing.access_token.get_secret_value() == "working-token"
    assert existing.region == "global"


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
    assert response.json()["spec"]["slicer"]["executable_path"] == (
        settings.bambu_studio_path
    )


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
