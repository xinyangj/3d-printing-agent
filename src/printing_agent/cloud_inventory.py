from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import queue
import re
import ssl
import threading
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Protocol
from uuid import uuid4

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator
from pydantic import ValidationError as PydanticValidationError

from printing_agent.cloud_credentials import (
    CloudCredentials,
    CloudCredentialStatus,
    CloudCredentialStore,
    CloudRegion,
    CredentialStoreError,
)


class CloudInventoryError(RuntimeError):
    """A safe, non-secret-bearing cloud inventory failure."""


class CloudAuthenticationError(CloudInventoryError):
    pass


class CloudInventoryUnavailableError(CloudInventoryError):
    pass


class CloudInventoryIncompleteError(CloudInventoryError):
    pass


class _FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


def mask_identifier(value: str) -> str:
    if len(value) <= 4:
        return "*" * len(value)
    if len(value) <= 8:
        return f"{value[:1]}{'*' * (len(value) - 2)}{value[-1:]}"
    return f"{value[:3]}{'*' * (len(value) - 7)}{value[-4:]}"


class CloudEndpoints(_FrozenModel):
    api_base_url: str
    mqtt_host: str
    bound_devices_path: str = "/v1/iot-service/api/user/bind"
    preference_path: str = "/v1/design-user-service/my/preference"
    mqtt_port: int = 8883


GLOBAL_ENDPOINTS = CloudEndpoints(
    api_base_url="https://api.bambulab.com",
    mqtt_host="us.mqtt.bambulab.com",
)
CHINA_ENDPOINTS = CloudEndpoints(
    api_base_url="https://api.bambulab.cn",
    mqtt_host="cn.mqtt.bambulab.com",
)


def endpoints_for_region(region: CloudRegion) -> CloudEndpoints:
    if region == "global":
        return GLOBAL_ENDPOINTS
    if region == "china":
        return CHINA_ENDPOINTS
    raise CloudInventoryError("Unsupported Bambu Cloud region")


class DeviceSummary(_FrozenModel):
    device_id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=255)
    model: str = Field(min_length=1, max_length=128)
    online: bool

    def masked_dump(self) -> dict[str, Any]:
        value = self.model_dump(mode="json")
        value["device_id"] = mask_identifier(self.device_id)
        value["device_ref"] = hashlib.sha256(
            self.device_id.encode()
        ).hexdigest()
        return value


class InstalledNozzle(_FrozenModel):
    position: Literal["left", "right"]
    diameter_mm: float = Field(gt=0, le=2)
    nozzle_type: str = Field(min_length=1, max_length=100)


class AMSTray(_FrozenModel):
    slot_id: str = Field(min_length=1, max_length=64)
    material: str | None = Field(default=None, max_length=100)
    material_profile_id: str | None = Field(default=None, max_length=100)
    material_sub_brand: str | None = Field(default=None, max_length=100)
    color: str | None = Field(default=None, max_length=32)
    rfid_uid: str | None = Field(default=None, max_length=128)
    tray_uuid: str | None = Field(default=None, max_length=128)
    state: int | None = None
    nozzle_temperature_min_c: int | None = None
    nozzle_temperature_max_c: int | None = None
    remain_percentage: float | None = Field(default=None, ge=0, le=100)
    nominal_tray_weight_g: float | None = Field(default=None, gt=0)
    estimated_remaining_g: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def derive_remaining_grams(self) -> AMSTray:
        if self.material is None:
            if self.estimated_remaining_g is not None:
                raise ValueError("An empty tray cannot have estimated remaining filament")
            return self
        if self.remain_percentage is None or self.nominal_tray_weight_g is None:
            return self
        estimate = round(self.nominal_tray_weight_g * self.remain_percentage / 100, 2)
        if self.estimated_remaining_g is not None and abs(
            self.estimated_remaining_g - estimate
        ) > 0.01:
            raise ValueError("Estimated remaining filament is inconsistent")
        object.__setattr__(self, "estimated_remaining_g", estimate)
        return self


class AMSUnit(_FrozenModel):
    unit_id: str = Field(min_length=1, max_length=64)
    kind: Literal["ams", "ams_ht"]
    trays: tuple[AMSTray, ...] = ()


class CloudDeviceSnapshot(_FrozenModel):
    id: str = Field(default_factory=lambda: uuid4().hex)
    region: CloudRegion
    device: DeviceSummary
    installed_nozzles: tuple[InstalledNozzle, ...]
    ams_units: tuple[AMSUnit, ...] = ()
    external_trays: tuple[AMSTray, ...] = ()
    completeness: Literal["complete"]
    warnings: tuple[str, ...] = ()
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    observed_at: datetime
    expires_at: datetime

    @model_validator(mode="after")
    def validate_snapshot(self) -> CloudDeviceSnapshot:
        if self.expires_at <= self.observed_at:
            raise ValueError("Snapshot expiry must follow observation time")
        if {nozzle.position for nozzle in self.installed_nozzles} != {"left", "right"}:
            raise ValueError("An H2D snapshot requires both installed nozzles")
        content = {
            "region": self.region,
            "device": self.device.model_dump(mode="json"),
            "installed_nozzles": [
                item.model_dump(mode="json") for item in self.installed_nozzles
            ],
            "ams_units": [item.model_dump(mode="json") for item in self.ams_units],
            "external_trays": [
                item.model_dump(mode="json") for item in self.external_trays
            ],
            "completeness": self.completeness,
            "warnings": list(self.warnings),
        }
        if self.digest != _snapshot_digest(content):
            raise ValueError("Snapshot digest is inconsistent")
        return self

    def masked_dump(self) -> dict[str, Any]:
        value = self.model_dump(mode="json")
        value["device"] = self.device.masked_dump()
        for unit in value["ams_units"]:
            for tray in unit["trays"]:
                if tray["rfid_uid"]:
                    tray["rfid_uid"] = mask_identifier(tray["rfid_uid"])
                if tray["tray_uuid"]:
                    tray["tray_uuid"] = mask_identifier(tray["tray_uuid"])
        for tray in value["external_trays"]:
            if tray["rfid_uid"]:
                tray["rfid_uid"] = mask_identifier(tray["rfid_uid"])
            if tray["tray_uuid"]:
                tray["tray_uuid"] = mask_identifier(tray["tray_uuid"])
        return value


class InventoryProvider(Protocol):
    async def validate_token(
        self,
        access_token: str,
        region: CloudRegion,
    ) -> tuple[DeviceSummary, ...]: ...

    async def list_devices(self) -> tuple[DeviceSummary, ...]: ...

    async def snapshot(self, device_id: str) -> CloudDeviceSnapshot: ...


class _MQTTMessage(Protocol):
    topic: str
    payload: bytes


class _MQTTClient(Protocol):
    on_connect: Callable[..., None] | None
    on_disconnect: Callable[..., None] | None
    on_message: Callable[..., None] | None

    def tls_set(self, **kwargs: Any) -> None: ...

    def username_pw_set(self, username: str, password: str) -> None: ...

    def connect_async(self, host: str, port: int, keepalive: int) -> Any: ...

    def loop_start(self) -> None: ...

    def loop_stop(self) -> None: ...

    def subscribe(self, topic: str, qos: int = 0) -> Any: ...

    def publish(self, topic: str, payload: str, qos: int = 0) -> Any: ...

    def disconnect(self) -> Any: ...


_READ_ONLY_COMMANDS = ("get_version", "pushall")


def build_read_only_status_request(command: str, sequence_id: str | None = None) -> str:
    if command not in _READ_ONLY_COMMANDS:
        raise CloudInventoryError("MQTT command is not permitted for read-only inventory")
    section = "info" if command == "get_version" else "pushing"
    request = {
        section: {
            "command": command,
            "sequence_id": sequence_id or uuid4().hex,
        }
    }
    return json.dumps(request, separators=(",", ":"), sort_keys=True)


def _number(value: Any, field: str) -> float:
    if isinstance(value, bool):
        raise CloudInventoryIncompleteError(f"Inventory field {field} is invalid")
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise CloudInventoryIncompleteError(f"Inventory field {field} is missing") from exc


def _optional_number(value: Any, field: str) -> float | None:
    if value is None or value == "":
        return None
    return _number(value, field)


def _string(value: Any, field: str) -> str:
    if not isinstance(value, (str, int)) or not str(value).strip():
        raise CloudInventoryIncompleteError(f"Inventory field {field} is missing")
    return str(value).strip()


def _print_payload(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    value = payload.get("print", payload)
    if not isinstance(value, Mapping):
        raise CloudInventoryIncompleteError("Printer inventory report is missing")
    return value


def _parse_nozzle_item(
    value: Mapping[str, Any],
    position: Literal["left", "right"],
) -> InstalledNozzle:
    diameter = value.get("diameter_mm", value.get("diameter", value.get("nozzle_diameter")))
    nozzle_type = value.get("nozzle_type", value.get("type", value.get("material")))
    return InstalledNozzle(
        position=position,
        diameter_mm=_number(diameter, f"{position} nozzle diameter"),
        nozzle_type=_string(nozzle_type, f"{position} nozzle type"),
    )


def _parse_nozzles(report: Mapping[str, Any]) -> tuple[InstalledNozzle, ...]:
    device = report.get("device")
    device_nozzle = (
        device.get("nozzle")
        if isinstance(device, Mapping)
        else None
    )
    raw = report.get(
        "nozzles",
        report.get(
            "nozzle",
            device_nozzle.get("info")
            if isinstance(device_nozzle, Mapping)
            else None,
        ),
    )
    if isinstance(raw, list):
        parsed: dict[str, InstalledNozzle] = {}
        for index, item in enumerate(raw):
            if not isinstance(item, Mapping):
                continue
            raw_position = str(item.get("position", item.get("id", ""))).casefold()
            if raw_position in {"left", "1", "deputy", "secondary"}:
                position: Literal["left", "right"] = "left"
            elif raw_position in {"right", "0", "main", "primary"}:
                position = "right"
            else:
                position = "right" if index == 0 else "left"
            parsed[position] = _parse_nozzle_item(item, position)
        if set(parsed) == {"left", "right"}:
            return parsed["left"], parsed["right"]

    pairs = (
        (
            report.get("nozzle_diameter_left", report.get("nozzle_diameter")),
            report.get("nozzle_type_left", report.get("nozzle_type")),
        ),
        (
            report.get(
                "nozzle_diameter_right",
                report.get("nozzle_diameter_2", report.get("nozzle_diameter_second")),
            ),
            report.get(
                "nozzle_type_right",
                report.get("nozzle_type_2", report.get("nozzle_type_second")),
            ),
        ),
    )
    return (
        _parse_nozzle_item(
            {"diameter": pairs[0][0], "type": pairs[0][1]},
            "left",
        ),
        _parse_nozzle_item(
            {"diameter": pairs[1][0], "type": pairs[1][1]},
            "right",
        ),
    )


def _tray_is_loaded(value: Mapping[str, Any]) -> bool:
    candidates = (
        value.get("material"),
        value.get("tray_type"),
        value.get("filament_type"),
        value.get("tray_info_idx"),
    )
    configured = any(
        candidate is not None
        and str(candidate).strip().casefold() not in {"", "0", "none", "null", "empty"}
        for candidate in candidates
    )
    try:
        present_state = int(value.get("state", 0)) != 0
    except (TypeError, ValueError):
        present_state = False
    return configured or present_state


def _parse_tray(
    value: Mapping[str, Any],
    fallback_id: str,
    *,
    slot_id: str | None = None,
) -> AMSTray:
    resolved_slot_id = slot_id or _string(
        value.get("slot_id", value.get("id", value.get("tray_id", fallback_id))),
        "tray slot",
    )
    if not _tray_is_loaded(value):
        return AMSTray(
            slot_id=resolved_slot_id,
            state=int(value["state"]) if value.get("state") is not None else None,
        )
    material = _string(
        value.get("material", value.get("tray_type", value.get("filament_type"))),
        f"tray {resolved_slot_id} material",
    )
    remain = value.get(
        "remain_percentage",
        value.get("remain", value.get("remaining_percent")),
    )
    tray_weight = value.get(
        "nominal_tray_weight_g",
        value.get("tray_weight", value.get("weight")),
    )
    remain_value = _optional_number(
        remain,
        f"tray {resolved_slot_id} remaining percentage",
    )
    tray_weight_value = _optional_number(
        tray_weight,
        f"tray {resolved_slot_id} nominal weight",
    )
    color_value = value.get("color", value.get("tray_color"))
    color = str(color_value).strip() if color_value is not None and color_value != "" else None
    if color and re.fullmatch(r"[0-9A-Fa-f]{8}", color):
        color = f"#{color[:6].upper()}"
    return AMSTray(
        slot_id=resolved_slot_id,
        material=material,
        material_profile_id=(
            str(value["tray_info_idx"]).strip()
            if value.get("tray_info_idx")
            else None
        ),
        material_sub_brand=(
            str(value["tray_sub_brands"]).strip()
            if value.get("tray_sub_brands")
            else None
        ),
        color=color,
        rfid_uid=str(value["tag_uid"]).strip() if value.get("tag_uid") else None,
        tray_uuid=str(value["tray_uuid"]).strip() if value.get("tray_uuid") else None,
        state=int(value["state"]) if value.get("state") is not None else None,
        nozzle_temperature_min_c=(
            int(value["nozzle_temp_min"])
            if value.get("nozzle_temp_min") is not None
            else None
        ),
        nozzle_temperature_max_c=(
            int(value["nozzle_temp_max"])
            if value.get("nozzle_temp_max") is not None
            else None
        ),
        remain_percentage=remain_value,
        nominal_tray_weight_g=(
            tray_weight_value
            if tray_weight_value is not None and tray_weight_value > 0
            else None
        ),
    )


def _unit_kind(value: Mapping[str, Any], fallback: Literal["ams", "ams_ht"]) -> Literal[
    "ams", "ams_ht"
]:
    marker = str(value.get("kind", value.get("type", ""))).casefold().replace("-", "_")
    marker = str(value.get("ams_type", marker)).casefold().replace("-", "_")
    if marker in {"ams_ht", "amsht", "ht"} or value.get("is_ams_ht") is True:
        return "ams_ht"
    try:
        if 128 <= int(str(value.get("unit_id", value.get("id", "-1")))) < 254:
            return "ams_ht"
    except ValueError:
        pass
    return fallback


def _parse_units(
    ams: Mapping[str, Any],
    key: str,
    kind: Literal["ams", "ams_ht"],
) -> list[AMSUnit]:
    raw_units = ams.get(key, ())
    if isinstance(raw_units, Mapping):
        raw_units = (raw_units,)
    if not isinstance(raw_units, (list, tuple)):
        raise CloudInventoryIncompleteError(f"{kind} inventory is invalid")
    units: list[AMSUnit] = []
    kind_counts = {"ams": 0, "ams_ht": 0}
    for unit_index, raw_unit in enumerate(raw_units):
        if not isinstance(raw_unit, Mapping):
            raise CloudInventoryIncompleteError(f"{kind} unit is invalid")
        unit_id = _string(
            raw_unit.get("unit_id", raw_unit.get("id", unit_index)),
            f"{kind} unit id",
        )
        raw_trays = raw_unit.get("trays", raw_unit.get("tray", ()))
        if isinstance(raw_trays, Mapping):
            raw_trays = (raw_trays,)
        if not isinstance(raw_trays, (list, tuple)):
            raise CloudInventoryIncompleteError(f"{kind} tray inventory is invalid")
        if any(not isinstance(tray, Mapping) for tray in raw_trays):
            raise CloudInventoryIncompleteError(f"{kind} tray inventory is invalid")
        resolved_kind = _unit_kind(raw_unit, kind)
        kind_counts[resolved_kind] += 1
        if resolved_kind == "ams":
            try:
                unit_number = int(unit_id) + 1
            except ValueError:
                unit_number = unit_index + 1
            slot_prefix = f"ams{unit_number}"
        else:
            slot_prefix = f"ams_ht{kind_counts['ams_ht']}"
        trays = tuple(
            _parse_tray(
                tray,
                str(index),
                slot_id=f"{slot_prefix}_{index + 1}",
            )
            for index, tray in enumerate(raw_trays)
        )
        units.append(AMSUnit(unit_id=unit_id, kind=resolved_kind, trays=trays))
    return units


def _snapshot_digest(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def _parse_h2d_snapshot(
    payload: Mapping[str, Any],
    device: DeviceSummary,
    *,
    observed_at: datetime | None = None,
    ttl: timedelta = timedelta(seconds=60),
    region: CloudRegion = "global",
) -> CloudDeviceSnapshot:
    if not _is_h2d(device.model):
        raise CloudInventoryError("Selected printer is not an H2D")
    report = _print_payload(payload)
    nozzles = _parse_nozzles(report)
    raw_ams = report.get("ams", {})
    if raw_ams is None:
        raw_ams = {}
    if not isinstance(raw_ams, Mapping):
        raise CloudInventoryIncompleteError("AMS inventory is invalid")
    units = _parse_units(raw_ams, "ams", "ams")
    units.extend(_parse_units(raw_ams, "ams_ht", "ams_ht"))

    raw_external = report.get("vir_slot")
    if raw_external is None:
        raw_external = raw_ams.get(
            "vt_tray",
            report.get("vt_tray", report.get("external_tray")),
        )
    external: tuple[AMSTray, ...]
    if raw_external is None:
        external = ()
    elif isinstance(raw_external, Mapping):
        external = (
            _parse_tray(
                raw_external,
                "external",
                slot_id=(
                    "external_right"
                    if str(raw_external.get("id", "")) == "255"
                    else "external_left"
                ),
            ),
        )
    elif isinstance(raw_external, (list, tuple)):
        if any(not isinstance(tray, Mapping) for tray in raw_external):
            raise CloudInventoryIncompleteError("External tray inventory is invalid")
        external = tuple(
            _parse_tray(
                tray,
                f"external-{index}",
                slot_id=(
                    "external_left"
                    if str(tray.get("id", index)) == "254"
                    else "external_right"
                    if str(tray.get("id", index)) == "255"
                    else f"external_{index + 1}"
                ),
            )
            for index, tray in enumerate(raw_external)
        )
    else:
        raise CloudInventoryIncompleteError("External tray inventory is invalid")

    observed = observed_at or datetime.now(UTC)
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=UTC)
    all_trays = [
        tray for unit in units for tray in unit.trays
    ] + list(external)
    warnings = [
        f"Loaded slot '{tray.slot_id}' has no usable quantity estimate"
        for tray in all_trays
        if tray.material is not None and tray.estimated_remaining_g is None
    ]
    content = {
        "region": region,
        "device": device.model_dump(mode="json"),
        "installed_nozzles": [item.model_dump(mode="json") for item in nozzles],
        "ams_units": [item.model_dump(mode="json") for item in units],
        "external_trays": [item.model_dump(mode="json") for item in external],
        "completeness": "complete",
        "warnings": warnings,
    }
    return CloudDeviceSnapshot(
        **content,
        digest=_snapshot_digest(content),
        observed_at=observed,
        expires_at=observed + ttl,
    )


def parse_h2d_snapshot(
    payload: Mapping[str, Any],
    device: DeviceSummary,
    *,
    observed_at: datetime | None = None,
    ttl: timedelta = timedelta(seconds=60),
    region: CloudRegion = "global",
) -> CloudDeviceSnapshot:
    try:
        return _parse_h2d_snapshot(
            payload,
            device,
            observed_at=observed_at,
            ttl=ttl,
            region=region,
        )
    except PydanticValidationError as exc:
        raise CloudInventoryIncompleteError("Printer inventory fields are invalid") from exc


def _is_h2d(model: str) -> bool:
    return "h2d" in re.sub(r"[^a-z0-9]", "", model.casefold())


def _nested_mapping(value: Any, *keys: str) -> Any:
    if isinstance(value, Mapping):
        for key in keys:
            if key in value:
                return value[key]
        for nested in value.values():
            found = _nested_mapping(nested, *keys)
            if found is not None:
                return found
    elif isinstance(value, list):
        for nested in value:
            found = _nested_mapping(nested, *keys)
            if found is not None:
                return found
    return None


def _jwt_username(token: str) -> str | None:
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        encoded = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(encoded))
    except (ValueError, TypeError, json.JSONDecodeError):
        return None
    username = claims.get("username") if isinstance(claims, Mapping) else None
    return _normalize_mqtt_username(username)


def _normalize_mqtt_username(value: object) -> str | None:
    if not isinstance(value, (str, int)):
        return None
    username = str(value).strip()
    if not username:
        return None
    if not username.startswith("u_"):
        username = f"u_{username}"
    if not re.fullmatch(r"u_[A-Za-z0-9_-]{1,126}", username):
        return None
    return username


def _deep_merge(target: dict[str, Any], source: Mapping[str, Any]) -> None:
    for key, value in source.items():
        if isinstance(value, Mapping) and isinstance(target.get(key), dict):
            _deep_merge(target[key], value)
        else:
            target[key] = value


def _mqtt_success(result: Any) -> bool:
    code = result[0] if isinstance(result, tuple) else getattr(result, "rc", result)
    return code in {0, None}


def _default_mqtt_client() -> _MQTTClient:
    import paho.mqtt.client as mqtt

    return mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        protocol=mqtt.MQTTv311,
    )


class BambuCloudInventoryProvider:
    def __init__(
        self,
        credential_store: CloudCredentialStore,
        *,
        http_client: httpx.AsyncClient | None = None,
        mqtt_client_factory: Callable[[], _MQTTClient] = _default_mqtt_client,
        snapshot_timeout_seconds: float = 12,
        snapshot_ttl: timedelta = timedelta(seconds=60),
    ) -> None:
        if snapshot_timeout_seconds <= 0 or snapshot_timeout_seconds > 60:
            raise ValueError("Snapshot timeout must be between 0 and 60 seconds")
        self._credential_store = credential_store
        self._http_client = http_client
        self._mqtt_client_factory = mqtt_client_factory
        self._snapshot_timeout = snapshot_timeout_seconds
        self._snapshot_ttl = snapshot_ttl

    async def list_devices(self) -> tuple[DeviceSummary, ...]:
        credentials = self._load_credentials()
        return await self._list_devices(credentials)

    async def validate_token(
        self,
        access_token: str,
        region: CloudRegion,
    ) -> tuple[DeviceSummary, ...]:
        credentials = CloudCredentials(
            access_token=SecretStr(access_token),
            region=region,
        )
        return await self._list_devices(credentials)

    async def validate_credentials(self) -> CloudCredentialStatus:
        credentials = self._load_credentials()
        await self._list_devices(credentials)
        return CloudCredentialStatus(configured=True, region=credentials.region)

    async def _list_devices(
        self,
        credentials: CloudCredentials,
    ) -> tuple[DeviceSummary, ...]:
        endpoints = endpoints_for_region(credentials.region)
        response = await self._get(
            f"{endpoints.api_base_url}{endpoints.bound_devices_path}",
            credentials,
        )
        if response.status_code in {401, 403}:
            raise CloudAuthenticationError("Bambu Cloud access token is invalid")
        if response.status_code != 200:
            raise CloudInventoryUnavailableError("Bambu Cloud device inventory is unavailable")
        try:
            body = response.json()
            raw_devices = _nested_mapping(body, "devices", "device_list")
            if not isinstance(raw_devices, list):
                raise ValueError
            devices = tuple(self._parse_device(item) for item in raw_devices)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise CloudInventoryIncompleteError(
                "Bambu Cloud returned an invalid device inventory"
            ) from exc
        return devices

    async def snapshot(self, device_id: str) -> CloudDeviceSnapshot:
        credentials = self._load_credentials()
        devices = await self._list_devices(credentials)
        device = next((item for item in devices if item.device_id == device_id), None)
        if device is None:
            raise CloudInventoryError("Selected printer is not bound to this account")
        if not device.online:
            raise CloudInventoryUnavailableError("Selected printer is offline")
        if not _is_h2d(device.model):
            raise CloudInventoryError("Selected printer is not an H2D")

        endpoints = endpoints_for_region(credentials.region)
        token = credentials.access_token.get_secret_value()
        username = _jwt_username(token)
        if username is None:
            username = await self._preference_username(credentials, endpoints)
        payload = await asyncio.to_thread(
            self._collect_payload,
            endpoints,
            device,
            username,
            token,
        )
        return parse_h2d_snapshot(
            payload,
            device,
            ttl=self._snapshot_ttl,
            region=credentials.region,
        )

    def _load_credentials(self) -> CloudCredentials:
        try:
            return self._credential_store.load()
        except CredentialStoreError as exc:
            raise CloudAuthenticationError(
                "Bambu Cloud account connection is unavailable"
            ) from exc

    async def get_snapshot(self, device_id: str) -> CloudDeviceSnapshot:
        return await self.snapshot(device_id)

    async def _preference_username(
        self,
        credentials: CloudCredentials,
        endpoints: CloudEndpoints,
    ) -> str:
        response = await self._get(
            f"{endpoints.api_base_url}{endpoints.preference_path}",
            credentials,
        )
        if response.status_code in {401, 403}:
            raise CloudAuthenticationError("Bambu Cloud access token is invalid")
        if response.status_code != 200:
            raise CloudInventoryUnavailableError(
                "Bambu Cloud account preference is unavailable"
            )
        try:
            username = _nested_mapping(response.json(), "username", "uid")
        except (ValueError, json.JSONDecodeError) as exc:
            raise CloudInventoryIncompleteError(
                "Bambu Cloud account preference is invalid"
            ) from exc
        normalized = _normalize_mqtt_username(username)
        if normalized is None:
            raise CloudInventoryIncompleteError(
                "Bambu Cloud account preference has no MQTT username"
            )
        return normalized

    async def _get(
        self,
        url: str,
        credentials: CloudCredentials,
    ) -> httpx.Response:
        headers = {
            "Authorization": f"Bearer {credentials.access_token.get_secret_value()}",
            "Accept": "application/json",
        }
        try:
            if self._http_client is not None:
                return await self._http_client.get(url, headers=headers, timeout=10)
            async with httpx.AsyncClient() as client:
                return await client.get(url, headers=headers, timeout=10)
        except httpx.HTTPError as exc:
            raise CloudInventoryUnavailableError("Bambu Cloud request failed") from exc

    @staticmethod
    def _parse_device(value: Any) -> DeviceSummary:
        if not isinstance(value, Mapping):
            raise ValueError
        device_id = value.get("dev_id", value.get("device_id", value.get("sn")))
        name = value.get("name", value.get("dev_name", "Bambu printer"))
        model = value.get(
            "model",
            value.get("dev_product_name", value.get("dev_model_name")),
        )
        online_value = value.get("online", value.get("is_online"))
        if isinstance(online_value, str):
            if online_value.casefold() not in {"true", "false", "1", "0"}:
                raise ValueError
            online = online_value.casefold() in {"true", "1"}
        elif isinstance(online_value, (bool, int)):
            online = bool(online_value)
        else:
            raise ValueError
        return DeviceSummary(
            device_id=_string(device_id, "device id"),
            name=_string(name, "device name"),
            model=_string(model, "device model"),
            online=online,
        )

    def _collect_payload(
        self,
        endpoints: CloudEndpoints,
        device: DeviceSummary,
        username: str,
        token: str,
    ) -> dict[str, Any]:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", device.device_id):
            raise CloudInventoryError("Selected printer identifier is invalid")
        report_topic = f"device/{device.device_id}/report"
        request_topic = f"device/{device.device_id}/request"
        messages: queue.Queue[dict[str, Any]] = queue.Queue()
        connected = threading.Event()
        disconnected = threading.Event()
        failure: list[str] = []
        client = self._mqtt_client_factory()

        def on_connect(
            _client: _MQTTClient,
            _userdata: Any,
            _flags: Any,
            reason_code: Any,
            _properties: Any = None,
        ) -> None:
            if reason_code == 0:
                connected.set()
            else:
                failure.append("Bambu Cloud MQTT authentication failed")
                connected.set()

        def on_disconnect(
            _client: _MQTTClient,
            _userdata: Any,
            _flags: Any,
            reason_code: Any,
            _properties: Any = None,
        ) -> None:
            if reason_code != 0:
                failure.append("Bambu Cloud MQTT connection was lost")
            disconnected.set()

        def on_message(
            _client: _MQTTClient,
            _userdata: Any,
            message: _MQTTMessage,
        ) -> None:
            if message.topic != report_topic:
                return
            try:
                decoded = json.loads(message.payload.decode("utf-8"))
            except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
                return
            if isinstance(decoded, dict):
                messages.put(decoded)

        client.on_connect = on_connect
        client.on_disconnect = on_disconnect
        client.on_message = on_message
        started = False
        deadline = time.monotonic() + self._snapshot_timeout
        try:
            client.tls_set(tls_version=ssl.PROTOCOL_TLS_CLIENT)
            client.username_pw_set(username, token)
            if not _mqtt_success(
                client.connect_async(endpoints.mqtt_host, endpoints.mqtt_port, 15)
            ):
                raise CloudInventoryUnavailableError("Bambu Cloud MQTT connection failed")
            client.loop_start()
            started = True
            if not connected.wait(max(0, deadline - time.monotonic())):
                raise CloudInventoryUnavailableError("Bambu Cloud MQTT connection timed out")
            if failure:
                raise CloudAuthenticationError(failure[0])
            if not _mqtt_success(client.subscribe(report_topic, qos=0)):
                raise CloudInventoryUnavailableError("Bambu Cloud MQTT subscription failed")
            for command in _READ_ONLY_COMMANDS:
                payload = build_read_only_status_request(command)
                if not _mqtt_success(client.publish(request_topic, payload, qos=0)):
                    raise CloudInventoryUnavailableError(
                        "Bambu Cloud status request failed"
                    )

            merged: dict[str, Any] = {}
            last_incomplete: CloudInventoryError | None = None
            while time.monotonic() < deadline:
                if failure or disconnected.is_set():
                    raise CloudInventoryUnavailableError(
                        failure[0] if failure else "Bambu Cloud MQTT disconnected"
                    )
                remaining = deadline - time.monotonic()
                try:
                    message = messages.get(timeout=min(remaining, 0.25))
                except queue.Empty:
                    continue
                _deep_merge(merged, message)
                try:
                    parse_h2d_snapshot(merged, device, ttl=self._snapshot_ttl)
                    return merged
                except CloudInventoryIncompleteError as exc:
                    last_incomplete = exc
            if last_incomplete is not None:
                raise last_incomplete
            raise CloudInventoryUnavailableError("Bambu Cloud inventory report timed out")
        finally:
            if started:
                client.loop_stop()
            try:
                client.disconnect()
            except Exception:
                pass
