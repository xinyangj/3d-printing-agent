from __future__ import annotations

import math
from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import Field, model_validator

from printing_agent.domain import (
    Dimensions,
    FrozenModel,
    canonical_digest,
    new_id,
    utc_now,
)


class ProfileOrigin(StrEnum):
    BUILT_IN = "built_in"
    IMPORTED = "imported"
    CUSTOM = "custom"


class SpoolStatus(StrEnum):
    AVAILABLE = "available"
    RESERVED = "reserved"
    LOADED = "loaded"
    DEPLETED = "depleted"
    QUARANTINED = "quarantined"


class SliceJobStatus(StrEnum):
    REQUESTED = "requested"
    SLICING = "slicing"
    VALIDATING = "validating"
    READY = "ready"
    FAILED = "failed"
    CANCELLED = "cancelled"


class HandoffStatus(StrEnum):
    READY = "ready"
    LAUNCHED = "launched"
    EXTERNAL_CONFIRMATION_REQUIRED = "external_confirmation_required"
    USER_CONFIRMED_SUBMITTED = "user_confirmed_submitted"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ToolheadSpec(FrozenModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    name: str = Field(min_length=1, max_length=100)
    nozzle_diameter_mm: float = Field(gt=0, le=2)
    nozzle_material: str = Field(min_length=1, max_length=100)
    max_temperature_c: int = Field(ge=150, le=600)
    supported_material_families: set[str] = Field(default_factory=set)
    hardened: bool = False


class PlateSpec(FrozenModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    name: str = Field(min_length=1, max_length=100)
    slicer_value: str | None = Field(default=None, min_length=1, max_length=100)
    max_temperature_c: int = Field(ge=0, le=200)
    supported_material_families: set[str] = Field(default_factory=set)


class MaterialSlotSpec(FrozenModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    name: str = Field(min_length=1, max_length=100)
    system: Literal["ams", "ams_ht", "external"]
    unit: int = Field(default=1, ge=1, le=16)
    tray: int | None = Field(default=None, ge=1, le=16)
    compatible_toolhead_ids: set[str] = Field(default_factory=set)
    supported_material_families: set[str] = Field(default_factory=set)
    automatic_assignment: bool = True
    forbidden_reason: str | None = Field(default=None, max_length=500)
    manual_swap_required: bool = False


class SlotPolicy(FrozenModel):
    forbidden_slot_ids: set[str] = Field(default_factory=set)
    allowed_slot_ids: set[str] | None = None
    part_allowed_slot_ids: dict[str, set[str]] = Field(default_factory=dict)
    part_forbidden_slot_ids: dict[str, set[str]] = Field(default_factory=dict)
    allow_manual_swaps: bool = True

    @model_validator(mode="after")
    def validate_policy(self) -> SlotPolicy:
        if self.allowed_slot_ids is not None and (
            self.forbidden_slot_ids & self.allowed_slot_ids
        ):
            raise ValueError("A slot cannot be both allowed and forbidden")
        return self


class SlicerProfileSpec(FrozenModel):
    driver_id: str = Field(min_length=1, max_length=100)
    machine_profile_id: str = Field(min_length=1, max_length=300)
    process_profile_id: str = Field(min_length=1, max_length=300)
    filament_profile_ids: dict[str, str] = Field(default_factory=dict)
    executable_path: str | None = Field(default=None, max_length=1_000)
    resource_root: str | None = Field(default=None, max_length=1_000)
    minimum_version: str | None = Field(default=None, max_length=100)
    maximum_version: str | None = Field(default=None, max_length=100)


class SubmissionProfileSpec(FrozenModel):
    driver_id: str = Field(min_length=1, max_length=100)
    mode: Literal["simulator", "bambu_connect_cloud"]
    executable_path: str | None = Field(default=None, max_length=1_000)
    uri_scheme: str = Field(default="bambu-connect", max_length=100)
    expected_printer_name: str | None = Field(default=None, max_length=200)
    expected_printer_serial: str | None = Field(default=None, max_length=200)


class PrinterProfileSpec(FrozenModel):
    display_name: str = Field(min_length=1, max_length=200)
    manufacturer: str = Field(min_length=1, max_length=100)
    model: str = Field(min_length=1, max_length=100)
    technology: Literal["fff"] = "fff"
    build_volume: Dimensions
    accepted_source_formats: set[str] = Field(default_factory=lambda: {"stl", "3mf"})
    accepted_sliced_formats: set[str] = Field(default_factory=lambda: {"gcode.3mf"})
    maximum_plate_count: int = Field(default=1, ge=1, le=64)
    toolheads: list[ToolheadSpec] = Field(min_length=1, max_length=16)
    plates: list[PlateSpec] = Field(min_length=1, max_length=50)
    material_slots: list[MaterialSlotSpec] = Field(default_factory=list, max_length=256)
    default_slot_policy: SlotPolicy = Field(default_factory=SlotPolicy)
    supports_multipart_3mf: bool = True
    supports_color: bool = True
    supports_material_assignments: bool = True
    supported_material_families: set[str] = Field(default_factory=set)
    slicer: SlicerProfileSpec
    submission: SubmissionProfileSpec

    @model_validator(mode="after")
    def validate_references(self) -> PrinterProfileSpec:
        toolhead_ids = {item.id for item in self.toolheads}
        if len(toolhead_ids) != len(self.toolheads):
            raise ValueError("Toolhead IDs must be unique")
        plate_ids = {item.id for item in self.plates}
        if len(plate_ids) != len(self.plates):
            raise ValueError("Plate IDs must be unique")
        slot_ids = {item.id for item in self.material_slots}
        if len(slot_ids) != len(self.material_slots):
            raise ValueError("Material slot IDs must be unique")
        if any(
            not slot.compatible_toolhead_ids.issubset(toolhead_ids)
            for slot in self.material_slots
        ):
            raise ValueError("Slot references unknown toolheads")
        if not self.default_slot_policy.forbidden_slot_ids.issubset(slot_ids):
            raise ValueError("Default policy forbids unknown slots")
        return self


class PrinterProfileRevision(FrozenModel):
    profile_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    revision: int = Field(ge=1)
    origin: ProfileOrigin
    spec: PrinterProfileSpec
    digest: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    created_at: datetime = Field(default_factory=utc_now)

    def with_digest(self) -> PrinterProfileRevision:
        unsigned = self.model_copy(update={"digest": None})
        return self.model_copy(update={"digest": canonical_digest(unsigned)})


class JobOverrides(FrozenModel):
    toolhead_id: str | None = None
    nozzle_diameter_mm: float | None = Field(default=None, gt=0, le=2)
    plate_id: str | None = None
    layer_height_mm: float | None = Field(default=None, gt=0, le=2)
    infill_percent: int | None = Field(default=None, ge=0, le=100)
    supports: bool | None = None
    brim: bool | None = None
    raft: bool | None = None
    timelapse: bool | None = None
    calibration: bool | None = None
    forbidden_slot_ids: set[str] = Field(default_factory=set)
    allowed_slot_ids: set[str] | None = None
    part_allowed_slot_ids: dict[str, set[str]] = Field(default_factory=dict)
    part_forbidden_slot_ids: dict[str, set[str]] = Field(default_factory=dict)
    allow_manual_swaps: bool | None = None
    maximum_color_distance: float = Field(default=12, ge=0, le=100)


class WorkflowPrinterSnapshot(FrozenModel):
    workflow_id: str
    profile_id: str
    profile_revision: int = Field(ge=1)
    profile_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    profile: PrinterProfileSpec
    slicer_profile_file_digests: dict[str, str] = Field(default_factory=dict)
    overrides: JobOverrides = Field(default_factory=JobOverrides)
    resolved_slot_policy: SlotPolicy
    digest: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    created_at: datetime = Field(default_factory=utc_now)

    def with_digest(self) -> WorkflowPrinterSnapshot:
        unsigned = self.model_copy(update={"digest": None})
        return self.model_copy(update={"digest": canonical_digest(unsigned)})


class MaterialDefinitionSpec(FrozenModel):
    display_name: str = Field(min_length=1, max_length=200)
    manufacturer: str | None = Field(default=None, max_length=100)
    product_line: str | None = Field(default=None, max_length=100)
    sku: str | None = Field(default=None, max_length=100)
    family: str = Field(min_length=1, max_length=50)
    modifiers: set[str] = Field(default_factory=set)
    filament_diameter_mm: float = Field(default=1.75, gt=0, le=4)
    nominal_color: str = Field(pattern=r"^#[0-9a-fA-F]{6}$")
    measured_color: str | None = Field(default=None, pattern=r"^#[0-9a-fA-F]{6}$")
    finish: str | None = Field(default=None, max_length=100)
    translucency: str | None = Field(default=None, max_length=100)
    nozzle_temperature_c: tuple[int, int]
    bed_temperature_c: tuple[int, int]
    hardened_nozzle_required: bool = False
    supported_nozzle_diameters_mm: set[float] = Field(default_factory=set)
    supported_plate_ids: set[str] = Field(default_factory=set)
    maximum_volumetric_speed: float | None = Field(default=None, gt=0)
    drying_temperature_c: int | None = Field(default=None, ge=0, le=150)
    drying_hours: float | None = Field(default=None, ge=0, le=72)
    slicer_filament_profile_id: str = Field(min_length=1, max_length=300)
    slicer_profile_digest: str | None = Field(
        default=None, pattern=r"^[a-f0-9]{64}$"
    )
    slicer_profile_dependency_digests: dict[str, str] = Field(
        default_factory=dict
    )

    @property
    def assignment_color(self) -> str:
        return (self.measured_color or self.nominal_color).upper()


class MaterialDefinitionRevision(FrozenModel):
    material_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    revision: int = Field(ge=1)
    spec: MaterialDefinitionSpec
    digest: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    created_at: datetime = Field(default_factory=utc_now)

    def with_digest(self) -> MaterialDefinitionRevision:
        unsigned = self.model_copy(update={"digest": None})
        return self.model_copy(update={"digest": canonical_digest(unsigned)})


class PhysicalSpool(FrozenModel):
    id: str = Field(default_factory=new_id)
    material_id: str
    material_revision: int = Field(ge=1)
    material_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    initial_weight_g: float = Field(gt=0)
    remaining_weight_g: float = Field(ge=0)
    spool_core_weight_g: float | None = Field(default=None, ge=0)
    status: SpoolStatus = SpoolStatus.AVAILABLE
    lot: str | None = Field(default=None, max_length=100)
    barcode: str | None = Field(default=None, max_length=200)
    location: str | None = Field(default=None, max_length=200)
    printer_profile_id: str | None = None
    slot_id: str | None = None
    dried_at: datetime | None = None
    measured_color: str | None = Field(default=None, pattern=r"^#[0-9a-fA-F]{6}$")
    notes: str | None = Field(default=None, max_length=1_000)
    updated_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_assignment(self) -> PhysicalSpool:
        if (self.printer_profile_id is None) != (self.slot_id is None):
            raise ValueError("Printer profile and slot must be assigned together")
        if self.remaining_weight_g > self.initial_weight_g:
            raise ValueError("Remaining weight cannot exceed initial weight")
        return self


class PartMaterialRequest(FrozenModel):
    part_id: str
    part_name: str
    requested_color: str = Field(pattern=r"^#[0-9a-fA-F]{6}$")
    requested_family: str | None = None
    estimated_weight_g: float | None = Field(default=None, ge=0)
    visible: bool = True


class MaterialCandidate(FrozenModel):
    part_id: str
    spool_id: str
    slot_id: str
    material_id: str
    material_revision: int
    material_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    color: str
    color_distance: float = Field(ge=0)
    remaining_weight_g: float = Field(ge=0)
    toolhead_ids: set[str] = Field(default_factory=set)
    slicer_filament_profile_id: str
    slicer_filament_profile_digest: str | None = Field(
        default=None, pattern=r"^[a-f0-9]{64}$"
    )
    slicer_filament_dependency_digests: dict[str, str] = Field(
        default_factory=dict
    )
    warnings: list[str] = Field(default_factory=list)


class PartMaterialAssignment(FrozenModel):
    part_id: str
    spool_id: str
    slot_id: str
    toolhead_id: str
    material_id: str
    material_revision: int
    material_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    slicer_filament_profile_id: str
    slicer_filament_profile_digest: str | None = Field(
        default=None, pattern=r"^[a-f0-9]{64}$"
    )
    slicer_filament_dependency_digests: dict[str, str] = Field(
        default_factory=dict
    )
    color_distance: float = Field(ge=0)
    confidence: float = Field(ge=0, le=1)
    rationale: str = Field(min_length=1, max_length=2_000)
    alternatives: list[str] = Field(default_factory=list, max_length=3)


class MaterialAssignment(FrozenModel):
    id: str = Field(default_factory=new_id)
    workflow_id: str
    artifact_version: int = Field(ge=1)
    artifact_manifest_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    printer_snapshot_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    slot_policy_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    requests: list[PartMaterialRequest] = Field(min_length=1, max_length=500)
    assignments: list[PartMaterialAssignment] = Field(min_length=1, max_length=500)
    requires_confirmation: bool
    confirmed_by: str | None = None
    confirmed_at: datetime | None = None
    digest: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    created_at: datetime = Field(default_factory=utc_now)

    def with_digest(self) -> MaterialAssignment:
        unsigned = self.model_copy(update={"digest": None})
        return self.model_copy(update={"digest": canonical_digest(unsigned)})


class SliceJob(FrozenModel):
    id: str = Field(default_factory=new_id)
    workflow_id: str
    artifact_version: int = Field(ge=1)
    artifact_manifest_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    printer_snapshot_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    material_assignment_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    slicer_driver_id: str
    status: SliceJobStatus = SliceJobStatus.REQUESTED
    idempotency_key: str = Field(pattern=r"^[a-f0-9]{64}$")
    message: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class SpoolReservation(FrozenModel):
    id: str = Field(default_factory=new_id)
    workflow_id: str
    slice_job_id: str
    spool_id: str
    reserved_weight_g: float = Field(ge=0)
    actual_usage_g: float | None = Field(default=None, ge=0)
    remaining_weight_snapshot_g: float = Field(ge=0)
    status: Literal["reserved", "released", "consumed"] = "reserved"
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class SlicedArtifact(FrozenModel):
    slice_job_id: str
    workflow_id: str
    path: str
    digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    size_bytes: int = Field(ge=1)
    format: Literal["gcode.3mf"]
    model_manifest_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    printer_snapshot_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    material_assignment_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    slicer_driver_id: str
    slicer_version: str
    machine_profile_id: str
    process_profile_id: str
    profile_file_digests: dict[str, str] = Field(default_factory=dict)
    plate_count: int = Field(ge=1)
    estimated_time_seconds: int | None = Field(default=None, ge=0)
    filament_usage_g: dict[str, float] = Field(default_factory=dict)
    layer_count: int | None = Field(default=None, ge=1)
    warnings: list[str] = Field(default_factory=list)
    manifest_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    created_at: datetime = Field(default_factory=utc_now)


class SubmissionHandoff(FrozenModel):
    id: str = Field(default_factory=new_id)
    workflow_id: str
    slice_job_id: str
    sliced_artifact_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    submission_driver_id: str
    status: HandoffStatus = HandoffStatus.READY
    local_path: str | None = None
    launch_uri: str | None = None
    launched_at: datetime | None = None
    confirmed_by: str | None = None
    confirmed_at: datetime | None = None
    message: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


def resolve_slot_policy(
    profile: PrinterProfileSpec,
    overrides: JobOverrides,
) -> SlotPolicy:
    slot_ids = {slot.id for slot in profile.material_slots}
    forbidden = set(profile.default_slot_policy.forbidden_slot_ids)
    forbidden.update(
        slot.id for slot in profile.material_slots if not slot.automatic_assignment
    )
    forbidden.update(overrides.forbidden_slot_ids)
    profile_allowed = (
        set(profile.default_slot_policy.allowed_slot_ids)
        if profile.default_slot_policy.allowed_slot_ids is not None
        else None
    )
    override_allowed = (
        set(overrides.allowed_slot_ids)
        if overrides.allowed_slot_ids is not None
        else None
    )
    allowed = (
        profile_allowed & override_allowed
        if profile_allowed is not None and override_allowed is not None
        else profile_allowed
        if profile_allowed is not None
        else override_allowed
    )
    if allowed is not None:
        allowed &= slot_ids
        allowed -= forbidden
    part_ids = set(profile.default_slot_policy.part_allowed_slot_ids) | set(
        overrides.part_allowed_slot_ids
    )
    part_allowed: dict[str, set[str]] = {}
    for part_id in part_ids:
        profile_values = profile.default_slot_policy.part_allowed_slot_ids.get(
            part_id
        )
        override_values = overrides.part_allowed_slot_ids.get(part_id)
        if profile_values is not None and override_values is not None:
            part_allowed[part_id] = set(profile_values) & set(override_values)
        elif profile_values is not None:
            part_allowed[part_id] = set(profile_values)
        elif override_values is not None:
            part_allowed[part_id] = set(override_values)
    part_forbidden = {
        part_id: set(
            profile.default_slot_policy.part_forbidden_slot_ids.get(part_id, set())
        )
        | set(overrides.part_forbidden_slot_ids.get(part_id, set()))
        for part_id in set(profile.default_slot_policy.part_forbidden_slot_ids)
        | set(overrides.part_forbidden_slot_ids)
    }
    return SlotPolicy(
        forbidden_slot_ids=forbidden,
        allowed_slot_ids=allowed,
        part_allowed_slot_ids=part_allowed,
        part_forbidden_slot_ids=part_forbidden,
        allow_manual_swaps=(
            profile.default_slot_policy.allow_manual_swaps
            and overrides.allow_manual_swaps is not False
        ),
    )


def srgb_to_lab(color: str) -> tuple[float, float, float]:
    values = [int(color[index : index + 2], 16) / 255 for index in (1, 3, 5)]
    linear = [
        value / 12.92
        if value <= 0.04045
        else ((value + 0.055) / 1.055) ** 2.4
        for value in values
    ]
    x = (linear[0] * 0.4124564 + linear[1] * 0.3575761 + linear[2] * 0.1804375) / 0.95047
    y = linear[0] * 0.2126729 + linear[1] * 0.7151522 + linear[2] * 0.072175
    z = (linear[0] * 0.0193339 + linear[1] * 0.119192 + linear[2] * 0.9503041) / 1.08883

    def pivot(value: float) -> float:
        return value ** (1 / 3) if value > 0.008856 else 7.787 * value + 16 / 116

    fx, fy, fz = pivot(x), pivot(y), pivot(z)
    return 116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz)


def ciede2000(
    first: tuple[float, float, float],
    second: tuple[float, float, float],
) -> float:
    l1, a1, b1 = first
    l2, a2, b2 = second
    c1 = math.hypot(a1, b1)
    c2 = math.hypot(a2, b2)
    c_bar = (c1 + c2) / 2
    g = 0.5 * (1 - math.sqrt(c_bar**7 / (c_bar**7 + 25**7)))
    a1p, a2p = (1 + g) * a1, (1 + g) * a2
    c1p, c2p = math.hypot(a1p, b1), math.hypot(a2p, b2)
    h1p = math.degrees(math.atan2(b1, a1p)) % 360
    h2p = math.degrees(math.atan2(b2, a2p)) % 360
    dl = l2 - l1
    dc = c2p - c1p
    dh_raw = h2p - h1p
    if c1p * c2p == 0:
        dh = 0
    elif abs(dh_raw) <= 180:
        dh = dh_raw
    elif dh_raw > 180:
        dh = dh_raw - 360
    else:
        dh = dh_raw + 360
    dh_term = 2 * math.sqrt(c1p * c2p) * math.sin(math.radians(dh / 2))
    l_bar = (l1 + l2) / 2
    c_bar_p = (c1p + c2p) / 2
    if c1p * c2p == 0:
        h_bar = h1p + h2p
    elif abs(h1p - h2p) <= 180:
        h_bar = (h1p + h2p) / 2
    elif h1p + h2p < 360:
        h_bar = (h1p + h2p + 360) / 2
    else:
        h_bar = (h1p + h2p - 360) / 2
    t = (
        1
        - 0.17 * math.cos(math.radians(h_bar - 30))
        + 0.24 * math.cos(math.radians(2 * h_bar))
        + 0.32 * math.cos(math.radians(3 * h_bar + 6))
        - 0.20 * math.cos(math.radians(4 * h_bar - 63))
    )
    sl = 1 + 0.015 * (l_bar - 50) ** 2 / math.sqrt(20 + (l_bar - 50) ** 2)
    sc = 1 + 0.045 * c_bar_p
    sh = 1 + 0.015 * c_bar_p * t
    delta_theta = 30 * math.exp(-((h_bar - 275) / 25) ** 2)
    rc = 2 * math.sqrt(c_bar_p**7 / (c_bar_p**7 + 25**7))
    rt = -rc * math.sin(math.radians(2 * delta_theta))
    return math.sqrt(
        (dl / sl) ** 2
        + (dc / sc) ** 2
        + (dh_term / sh) ** 2
        + rt * (dc / sc) * (dh_term / sh)
    )
