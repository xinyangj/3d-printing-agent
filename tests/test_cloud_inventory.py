from __future__ import annotations

import base64
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from pydantic import SecretStr

from printing_agent.cloud_credentials import CloudCredentialStore, CredentialStoreError
from printing_agent.cloud_inventory import (
    CHINA_ENDPOINTS,
    GLOBAL_ENDPOINTS,
    BambuCloudInventoryProvider,
    CloudAuthenticationError,
    CloudInventoryError,
    DeviceSummary,
    _jwt_username,
    _normalize_mqtt_username,
    build_read_only_status_request,
    endpoints_for_region,
    parse_h2d_snapshot,
)


class FakeProtector:
    prefix = b"fake-protected:"

    def protect(self, value: bytes) -> bytes:
        return self.prefix + value[::-1]

    def unprotect(self, value: bytes) -> bytes:
        assert value.startswith(self.prefix)
        return value.removeprefix(self.prefix)[::-1]


class FakeMQTTClient:
    def __init__(self, report: dict[str, object]) -> None:
        self.report = report
        self.on_connect = None
        self.on_disconnect = None
        self.on_message = None
        self.published: list[tuple[str, dict[str, object]]] = []
        self.subscribed: list[str] = []

    def tls_set(self, **kwargs) -> None:
        del kwargs

    def username_pw_set(self, username: str, password: str) -> None:
        assert username == "u_test"
        assert password

    def connect_async(self, host: str, port: int, keepalive: int):
        assert host == GLOBAL_ENDPOINTS.mqtt_host
        assert port == 8883
        assert keepalive == 15
        return 0

    def loop_start(self) -> None:
        assert self.on_connect is not None
        self.on_connect(self, None, None, 0)

    def loop_stop(self) -> None:
        return None

    def subscribe(self, topic: str, qos: int = 0):
        assert qos == 0
        self.subscribed.append(topic)
        return (0, 1)

    def publish(self, topic: str, payload: str, qos: int = 0):
        assert qos == 0
        parsed = json.loads(payload)
        self.published.append((topic, parsed))
        if "pushing" in parsed:
            assert self.on_message is not None
            self.on_message(
                self,
                None,
                SimpleNamespace(
                    topic=topic.replace("/request", "/report"),
                    payload=json.dumps(self.report).encode(),
                ),
            )
        return (0, 1)

    def disconnect(self):
        return 0


def test_credential_store_roundtrip_never_exposes_plaintext(tmp_path: Path) -> None:
    path = tmp_path / "credentials.json"
    token = "redacted-access-token"
    store = CloudCredentialStore(
        path,
        protector=FakeProtector(),
        acl_restrictor=lambda _: None,
    )

    status = store.store(token, "global")
    stored = path.read_text(encoding="utf-8")
    loaded = store.load()

    assert status.configured is True
    assert token not in stored
    assert token not in repr(loaded)
    assert token not in loaded.model_dump_json()
    assert loaded.access_token == SecretStr(token)
    assert loaded.masked_dump() == {
        "access_token": "********",
        "region": "global",
    }
    assert store.status().region == "global"
    assert store.status().source == "manual"
    assert store.clear().configured is False
    assert store.status().configured is False


def test_credential_store_encrypts_studio_account_metadata(tmp_path: Path) -> None:
    path = tmp_path / "credentials.json"
    store = CloudCredentialStore(
        path,
        protector=FakeProtector(),
        acl_restrictor=lambda _: None,
    )

    status = store.store(
        "studio-token",
        "china",
        source="bambu_studio",
        account_fingerprint="a" * 64,
        account_hint="***3456",
    )
    stored = path.read_text(encoding="utf-8")
    loaded = store.load()

    assert json.loads(stored)["version"] == 2
    assert "studio-token" not in stored
    assert "***3456" not in stored
    assert "a" * 64 not in stored
    assert loaded.source == "bambu_studio"
    assert loaded.account_fingerprint == "a" * 64
    assert loaded.account_hint == "***3456"
    assert status.account_hint == "***3456"


def test_credential_store_reads_legacy_version_one(tmp_path: Path) -> None:
    path = tmp_path / "credentials.json"
    protector = FakeProtector()
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "region": "global",
                "encrypted_access_token": base64.b64encode(
                    protector.protect(b"legacy-token")
                ).decode("ascii"),
            }
        ),
        encoding="utf-8",
    )
    store = CloudCredentialStore(
        path,
        protector=protector,
        acl_restrictor=lambda _: None,
    )

    loaded = store.load()
    status = store.status()

    assert loaded.access_token.get_secret_value() == "legacy-token"
    assert loaded.source is None
    assert status.configured is True
    assert status.source is None


@pytest.mark.parametrize("document", [[], None, "invalid", 42])
def test_credential_store_rejects_non_object_json(
    tmp_path: Path,
    document: object,
) -> None:
    path = tmp_path / "credentials.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    store = CloudCredentialStore(
        path,
        protector=FakeProtector(),
        acl_restrictor=lambda _: None,
    )

    with pytest.raises(CredentialStoreError):
        store.load()

    assert store.status().configured is False


def test_credential_store_preserves_previous_value_when_replacement_fails(
    tmp_path: Path,
) -> None:
    path = tmp_path / "credentials.json"
    existing = CloudCredentialStore(
        path,
        protector=FakeProtector(),
        acl_restrictor=lambda _: None,
    )
    existing.store("working-token", "global")

    def reject_acl(_: Path) -> None:
        raise OSError("synthetic ACL failure")

    replacement = CloudCredentialStore(
        path,
        protector=FakeProtector(),
        acl_restrictor=reject_acl,
    )

    with pytest.raises(CredentialStoreError):
        replacement.store("replacement-token", "china")

    loaded = existing.load()
    assert loaded.access_token.get_secret_value() == "working-token"
    assert loaded.region == "global"


def test_credential_store_serializes_concurrent_replacements(tmp_path: Path) -> None:
    path = tmp_path / "credentials.json"
    first_acl_started = threading.Event()
    release_first_acl = threading.Event()
    call_lock = threading.Lock()
    acl_calls = 0

    def controlled_acl(_: Path) -> None:
        nonlocal acl_calls
        with call_lock:
            acl_calls += 1
            is_first = acl_calls == 1
        if is_first:
            first_acl_started.set()
            assert release_first_acl.wait(timeout=2)

    first = CloudCredentialStore(
        path,
        protector=FakeProtector(),
        acl_restrictor=controlled_acl,
    )
    second = CloudCredentialStore(
        path,
        protector=FakeProtector(),
        acl_restrictor=controlled_acl,
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        first_write = executor.submit(first.store, "first-token", "global")
        assert first_acl_started.wait(timeout=2)
        second_write = executor.submit(second.store, "second-token", "china")
        time.sleep(0.05)
        release_first_acl.set()
        first_write.result(timeout=2)
        second_write.result(timeout=2)

    loaded = first.load()
    assert loaded.access_token.get_secret_value() == "second-token"
    assert loaded.region == "china"
    assert list(tmp_path.glob("*.pending")) == []


def test_global_and_china_endpoints_are_explicit() -> None:
    assert endpoints_for_region("global") == GLOBAL_ENDPOINTS
    assert GLOBAL_ENDPOINTS.api_base_url == "https://api.bambulab.com"
    assert GLOBAL_ENDPOINTS.mqtt_host == "us.mqtt.bambulab.com"
    assert endpoints_for_region("china") == CHINA_ENDPOINTS
    assert CHINA_ENDPOINTS.api_base_url == "https://api.bambulab.cn"
    assert CHINA_ENDPOINTS.mqtt_host == "cn.mqtt.bambulab.com"


def test_cloud_mqtt_username_is_prefixed_exactly_once() -> None:
    claims = base64.urlsafe_b64encode(
        json.dumps({"username": "216711365"}).encode()
    ).decode().rstrip("=")

    assert _jwt_username(f"header.{claims}.signature") == "u_216711365"
    assert _normalize_mqtt_username("216711365") == "u_216711365"
    assert _normalize_mqtt_username("u_216711365") == "u_216711365"
    assert _normalize_mqtt_username("invalid user") is None


async def test_provider_reports_missing_credentials_as_cloud_authentication(
    tmp_path: Path,
) -> None:
    store = CloudCredentialStore(
        tmp_path / "missing.json",
        protector=FakeProtector(),
        acl_restrictor=lambda _: None,
    )
    provider = BambuCloudInventoryProvider(store)

    with pytest.raises(CloudAuthenticationError, match="connection is unavailable"):
        await provider.list_devices()


def _h2d_report() -> dict[str, object]:
    return {
        "print": {
            "nozzles": [
                {
                    "position": "left",
                    "diameter": "0.4",
                    "type": "hardened_steel",
                },
                {
                    "position": "right",
                    "diameter": "0.6",
                    "type": "stainless_steel",
                },
            ],
            "ams": {
                "ams": [
                    {
                        "id": "0",
                        "tray": [
                            {
                                "id": "0",
                                "tray_type": "PLA",
                                "tray_color": "FF0000FF",
                                "remain": 75,
                                "tray_weight": 1000,
                            },
                            {"id": "1"},
                        ],
                    }
                ],
                "ams_ht": [
                    {
                        "id": "128",
                        "tray": [
                            {
                                "id": "0",
                                "tray_type": "PA-CF",
                                "remain": "20",
                                "tray_weight": "500",
                            }
                        ],
                    }
                ],
                "vt_tray": {
                    "id": "254",
                    "tray_type": "PETG",
                    "remain": 40,
                    "tray_weight": 750,
                },
            },
        }
    }


def test_h2d_parser_covers_dual_nozzle_ams_ht_and_external_slot() -> None:
    device = DeviceSummary(
        device_id="01P00REDACTED1234",
        name="Workshop H2D",
        model="H2D",
        online=True,
    )

    snapshot = parse_h2d_snapshot(
        _h2d_report(),
        device,
        observed_at=datetime(2026, 8, 20, tzinfo=UTC),
    )

    assert [nozzle.position for nozzle in snapshot.installed_nozzles] == [
        "left",
        "right",
    ]
    assert snapshot.ams_units[0].kind == "ams"
    assert snapshot.ams_units[0].trays[0].estimated_remaining_g == 750
    assert snapshot.ams_units[0].trays[0].slot_id == "ams1_1"
    assert snapshot.ams_units[1].kind == "ams_ht"
    assert snapshot.ams_units[1].trays[0].slot_id == "ams_ht1_1"
    assert snapshot.ams_units[1].trays[0].estimated_remaining_g == 100
    assert snapshot.external_trays[0].estimated_remaining_g == 300
    assert snapshot.completeness == "complete"
    assert len(snapshot.digest) == 64


def test_parser_accepts_native_h2d_nested_device_and_ams_shape() -> None:
    report = {
        "print": {
            "device": {
                "nozzle": {
                    "info": [
                        {"id": 0, "diameter": 0.4, "type": "HS01"},
                        {"id": 1, "diameter": 0.4, "type": "HH01"},
                    ]
                }
            },
            "ams": {
                "ams": [
                    {
                        "id": "0",
                        "tray": [
                            {
                                "id": "0",
                                "state": 11,
                                "tray_type": "PLA",
                                "tray_info_idx": "GFA01",
                                "tray_sub_brands": "PLA Matte",
                                "tray_color": "DE4343FF",
                                "remain": 82,
                                "tray_weight": 1000,
                                "tag_uid": "RFID-PRIVATE",
                                "tray_uuid": "UUID-PRIVATE",
                            }
                        ],
                    },
                    {
                        "id": "128",
                        "tray": [
                            {
                                "id": "0",
                                "state": 11,
                                "tray_type": "PA-GF",
                                "tray_info_idx": "GFN08",
                                "tray_color": "000000FF",
                                "remain": 50,
                                "tray_weight": 1000,
                            }
                        ],
                    },
                ]
            },
            "vir_slot": [
                {"id": "254", "state": 0, "tray_type": "", "tag_uid": "0" * 16},
                {"id": "255", "state": 0, "tray_type": "", "tag_uid": "0" * 16},
            ],
        }
    }
    snapshot = parse_h2d_snapshot(
        report,
        DeviceSummary(
            device_id="H2D-PRIVATE-SERIAL",
            name="Workshop H2D",
            model="Bambu Lab H2D",
            online=True,
        ),
        region="china",
    )

    assert snapshot.region == "china"
    assert snapshot.installed_nozzles[0].position == "left"
    assert snapshot.installed_nozzles[0].nozzle_type == "HH01"
    assert snapshot.installed_nozzles[1].position == "right"
    assert snapshot.installed_nozzles[1].nozzle_type == "HS01"
    assert snapshot.ams_units[0].trays[0].material_profile_id == "GFA01"
    assert snapshot.ams_units[0].trays[0].color == "#DE4343"
    assert snapshot.ams_units[0].trays[0].estimated_remaining_g == 820
    assert snapshot.ams_units[1].kind == "ams_ht"
    assert snapshot.external_trays[0].slot_id == "external_left"
    assert snapshot.external_trays[0].material is None
    masked = json.dumps(snapshot.masked_dump())
    assert "RFID-PRIVATE" not in masked
    assert "UUID-PRIVATE" not in masked


def test_snapshot_masking_hides_device_identifier() -> None:
    device = DeviceSummary(
        device_id="01P00REDACTED1234",
        name="Workshop H2D",
        model="H2D",
        online=True,
    )
    snapshot = parse_h2d_snapshot(_h2d_report(), device)

    serialized = json.dumps(snapshot.masked_dump())

    assert device.device_id not in serialized
    assert snapshot.masked_dump()["device"]["device_id"].endswith("1234")


def test_snapshot_masking_hides_external_tray_identifiers() -> None:
    report = _h2d_report()
    external = report["print"]["ams"]["vt_tray"]  # type: ignore[index]
    external["tag_uid"] = "EXTERNAL-RFID-PRIVATE"
    external["tray_uuid"] = "EXTERNAL-UUID-PRIVATE"
    snapshot = parse_h2d_snapshot(
        report,
        DeviceSummary(
            device_id="01P00REDACTED1234",
            name="Workshop H2D",
            model="H2D",
            online=True,
        ),
    )

    serialized = json.dumps(snapshot.masked_dump())

    assert "EXTERNAL-RFID-PRIVATE" not in serialized
    assert "EXTERNAL-UUID-PRIVATE" not in serialized


def test_loaded_tray_without_quantity_is_unavailable_not_global_failure() -> None:
    report = _h2d_report()
    del report["print"]["ams"]["ams"][0]["tray"][0]["remain"]  # type: ignore[index]
    device = DeviceSummary(
        device_id="01P00REDACTED1234",
        name="Workshop H2D",
        model="H2D",
        online=True,
    )

    snapshot = parse_h2d_snapshot(report, device)

    assert snapshot.ams_units[0].trays[0].estimated_remaining_g is None
    assert "no usable quantity estimate" in snapshot.warnings[0]


def test_negative_remaining_sentinel_is_unknown_not_global_failure() -> None:
    report = _h2d_report()
    report["print"]["ams"]["ams"][0]["tray"][0]["remain"] = -1  # type: ignore[index]
    snapshot = parse_h2d_snapshot(
        report,
        DeviceSummary(
            device_id="01P00REDACTED1234",
            name="Workshop H2D",
            model="H2D",
            online=True,
        ),
    )

    tray = snapshot.ams_units[0].trays[0]
    assert tray.material == "PLA"
    assert tray.remain_percentage is None
    assert tray.estimated_remaining_g is None
    assert "no usable quantity estimate" in snapshot.warnings[0]


@pytest.mark.parametrize("command", ["project_file", "print", "control"])
def test_mqtt_allowlist_rejects_non_inventory_commands(command: str) -> None:
    with pytest.raises(CloudInventoryError, match="not permitted"):
        build_read_only_status_request(command)


def test_mqtt_allowlist_contains_only_status_requests() -> None:
    version = json.loads(build_read_only_status_request("get_version", "v1"))
    pushall = json.loads(build_read_only_status_request("pushall", "p1"))

    assert version == {"info": {"command": "get_version", "sequence_id": "v1"}}
    assert pushall == {"pushing": {"command": "pushall", "sequence_id": "p1"}}


async def test_provider_publishes_only_two_status_requests(
    tmp_path: Path,
) -> None:
    store = CloudCredentialStore(
        tmp_path / "credential.json",
        protector=FakeProtector(),
        acl_restrictor=lambda _: None,
    )
    claims = base64.urlsafe_b64encode(
        json.dumps({"username": "u_test"}).encode()
    ).decode().rstrip("=")
    store.store(f"header.{claims}.signature", "global")
    mqtt = FakeMQTTClient(_h2d_report())

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.path == GLOBAL_ENDPOINTS.bound_devices_path
        assert request.headers["Authorization"] == (
            f"Bearer header.{claims}.signature"
        )
        return httpx.Response(
            200,
            json={
                "devices": [
                    {
                        "dev_id": "H2D-SERIAL",
                        "name": "Workshop H2D",
                        "dev_product_name": "H2D",
                        "online": True,
                    }
                ]
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle)
    ) as client:
        provider = BambuCloudInventoryProvider(
            store,
            http_client=client,
            mqtt_client_factory=lambda: mqtt,
        )
        snapshot = await provider.snapshot("H2D-SERIAL")

    assert snapshot.device.name == "Workshop H2D"
    assert mqtt.subscribed == ["device/H2D-SERIAL/report"]
    commands = [
        next(iter(payload.values()))["command"]
        for _, payload in mqtt.published
    ]
    assert commands == ["get_version", "pushall"]
    assert {topic for topic, _ in mqtt.published} == {
        "device/H2D-SERIAL/request"
    }
