from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from printing_agent.errors import InvalidTransitionError


def utc_now() -> datetime:
    return datetime.now(UTC)


def new_id() -> str:
    return str(uuid4())


def canonical_digest(value: BaseModel | dict[str, Any]) -> str:
    payload = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class WorkflowState(StrEnum):
    RECEIVED = "received"
    PLANNING = "planning"
    DISCOVERING = "discovering"
    SEARCHING = "searching"
    PAGE_INSPECTION = "page_inspection"
    SELECTING = "selecting"
    SOURCE_VALIDATION = "source_validation"
    HANDOFF_READY = "handoff_ready"
    GENERATING = "generating"
    RENDERING = "rendering"
    VALIDATING = "validating"
    AWAITING_APPROVAL = "awaiting_approval"
    REVISION_REQUESTED = "revision_requested"
    APPROVED = "approved"
    SUBMITTING = "submitting"
    QUEUED = "queued"
    PRINTING = "printing"
    COMPLETED = "completed"
    PREPARATION_FAILED = "preparation_failed"
    PRINT_FAILED = "print_failed"
    CANCELLED = "cancelled"


class ModelDecision(StrEnum):
    MODIFY = "modify"
    USE_AS_IS = "use_as_is"
    CREATE = "create"


class PrintJobStatus(StrEnum):
    QUEUED = "queued"
    PRINTING = "printing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class WorkKind(StrEnum):
    PREPARE = "prepare"
    SUBMIT = "submit"
    REFRESH_PRINT = "refresh_print"


class RevisionMode(StrEnum):
    REFINE_CURRENT = "refine_current"
    SEARCH_NEW_BASE = "search_new_base"


class PartGeometryKind(StrEnum):
    PARAMETRIC = "parametric"
    IMPORTED_MESH = "imported_mesh"
    DERIVED_MESH = "derived_mesh"


class AnnotationOrigin(StrEnum):
    SOURCE_ANNOTATION = "source_annotation"
    THREE_MF_STRUCTURE = "3mf_structure"
    CATALOG_METADATA = "catalog_metadata"
    TOPOLOGY_INFERENCE = "topology_inference"
    AGENT_INFERENCE = "agent_inference"


class Dimensions(FrozenModel):
    width_mm: float = Field(gt=0)
    depth_mm: float = Field(gt=0)
    height_mm: float = Field(gt=0)

    def fits(self, volume: Dimensions) -> bool:
        return (
            self.width_mm <= volume.width_mm
            and self.depth_mm <= volume.depth_mm
            and self.height_mm <= volume.height_mm
        )


class PrintSettings(FrozenModel):
    quantity: int = Field(default=1, ge=1, le=100)
    material: str | None = Field(default=None, max_length=50)
    color: str | None = Field(default=None, max_length=50)
    layer_height_mm: float | None = Field(default=None, gt=0, le=2)
    infill_percent: int | None = Field(default=None, ge=0, le=100)


class ModelPlan(FrozenModel):
    search_query: str = Field(min_length=2, max_length=200)
    geometry_summary: str = Field(min_length=1, max_length=4000)
    target_dimensions: Dimensions | None = None
    constraints: list[str] = Field(default_factory=list, max_length=30)
    required_features: list[str] = Field(default_factory=list, max_length=30)
    print_settings: PrintSettings = Field(default_factory=PrintSettings)


class CandidateFile(FrozenModel):
    id: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=255)
    format: str = Field(min_length=1, max_length=20)
    size_bytes: int | None = Field(default=None, ge=0)
    download_url: str | None = None


class ModelCandidate(FrozenModel):
    id: str = Field(min_length=1, max_length=100)
    title: str = Field(min_length=1, max_length=500)
    introduction: str = Field(default="", max_length=20_000)
    instructions: str = Field(default="", max_length=20_000)
    tags: list[str] = Field(default_factory=list, max_length=100)
    source_url: str
    gallery_urls: list[str] = Field(default_factory=list, max_length=20)
    files: list[CandidateFile] = Field(default_factory=list, max_length=100)
    creator: str = Field(default="unknown", max_length=300)
    license: str = Field(default="unknown", max_length=300)
    allows_derivatives: bool | None = None
    popularity: dict[str, int] = Field(default_factory=dict)


class SearchRound(FrozenModel):
    id: str = Field(default_factory=new_id)
    workflow_id: str
    query: str
    page: int = Field(ge=1)
    candidate_ids: list[str]
    created_at: datetime = Field(default_factory=utc_now)


class CandidatePageInspection(FrozenModel):
    workflow_id: str
    search_round_id: str
    candidate: ModelCandidate
    image_digests: list[str] = Field(default_factory=list)
    inspected_at: datetime = Field(default_factory=utc_now)


class MeshReport(FrozenModel):
    dimensions: Dimensions
    triangle_count: int = Field(gt=0)
    connected_components: int = Field(ge=1)
    watertight: bool
    volume_mm3: float = Field(ge=0)
    finite: bool = True


class SelectedSourceInspection(FrozenModel):
    workflow_id: str
    candidate_id: str
    file_id: str
    source_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    cached_path: Path
    mesh: MeshReport
    accepted: bool
    rejection_reason: str | None = Field(default=None, max_length=2000)
    inspected_at: datetime = Field(default_factory=utc_now)


class DiscoveryDecision(FrozenModel):
    decision: ModelDecision
    candidate_id: str | None = None
    file_id: str | None = None
    rationale: str = Field(min_length=1, max_length=2000)
    required_changes: list[str] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def validate_source_choice(self) -> DiscoveryDecision:
        if self.decision == ModelDecision.CREATE:
            if self.candidate_id is not None or self.file_id is not None:
                raise ValueError("Creation decisions cannot select a candidate file")
        elif not self.candidate_id or not self.file_id:
            raise ValueError("A candidate and file are required for modify/use_as_is")
        return self


class SelectedSourceSummary(FrozenModel):
    filename: str = Field(default="source.stl", min_length=1, max_length=255)
    format: str = Field(default="stl", min_length=1, max_length=20)
    candidate_id: str
    file_id: str
    title: str
    creator: str
    license: str
    attribution_url: str
    source_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    mesh: MeshReport


class PrinterCapabilitySummary(FrozenModel):
    name: str
    build_volume: Dimensions
    accepted_formats: set[str]
    supported_materials: set[str] = Field(default_factory=set)
    supports_multipart_3mf: bool = False
    supports_color: bool = False
    supports_material_assignments: bool = False
    slices_locally: bool = False


class ModelingHandoff(FrozenModel):
    schema_version: Literal["1"] = "1"
    workflow_id: str
    version: int = Field(ge=1)
    requirement: str = Field(min_length=1, max_length=20_000)
    model_plan: ModelPlan
    decision: Literal[ModelDecision.MODIFY, ModelDecision.CREATE]
    required_changes: list[str] = Field(default_factory=list, max_length=20)
    selected_source: SelectedSourceSummary | None = None
    target_printer: PrinterCapabilitySummary
    discovery_rationale: str = Field(min_length=1, max_length=2000)
    evidence_digests: list[str] = Field(default_factory=list, max_length=50)
    digest: str | None = None

    @model_validator(mode="after")
    def validate_handoff(self) -> ModelingHandoff:
        if self.decision == ModelDecision.MODIFY and self.selected_source is None:
            raise ValueError("Modification handoffs require a selected source")
        if self.decision == ModelDecision.CREATE and self.selected_source is not None:
            raise ValueError("Creation handoffs cannot contain a selected source")
        return self

    def with_digest(self) -> ModelingHandoff:
        unsigned = self.model_copy(update={"digest": None})
        return self.model_copy(update={"digest": canonical_digest(unsigned)})


class ArtifactProvenance(FrozenModel):
    kind: Literal["catalog", "generated"]
    candidate_id: str | None = None
    source_url: str | None = None
    creator: str | None = None
    license: str | None = None
    source_digest: str | None = None


class SourceAsset(FrozenModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    filename: str = Field(min_length=1, max_length=255)
    format: str = Field(min_length=1, max_length=20)
    digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    path: str = Field(min_length=1, max_length=500)
    role: str = Field(min_length=1, max_length=100)
    original_cad: bool = False
    annotation_origin: AnnotationOrigin


class MaterialDefinition(FrozenModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    name: str = Field(min_length=1, max_length=100)
    color: str = Field(default="#b7c4d4", pattern=r"^#[0-9a-fA-F]{6}$")
    printing_material: str | None = Field(default=None, max_length=50)


class PartDefinition(FrozenModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    name: str = Field(min_length=1, max_length=200)
    geometry_kind: PartGeometryKind
    module_name: str | None = Field(default=None, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    source_asset_id: str | None = None
    material_id: str | None = None
    parameters: dict[str, float | int | str | bool] = Field(default_factory=dict)
    annotation_origin: AnnotationOrigin
    confidence: float = Field(ge=0, le=1)


class PartInstance(FrozenModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    part_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    name: str = Field(min_length=1, max_length=200)
    transform: tuple[
        float,
        float,
        float,
        float,
        float,
        float,
        float,
        float,
        float,
        float,
        float,
        float,
        float,
        float,
        float,
        float,
    ] = (1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1)
    parent_id: str | None = None
    annotation_origin: AnnotationOrigin
    confidence: float = Field(ge=0, le=1)


class PartProject(FrozenModel):
    schema_version: Literal["1"] = "1"
    units: Literal["millimeter"] = "millimeter"
    parts: list[PartDefinition] = Field(min_length=1, max_length=500)
    instances: list[PartInstance] = Field(min_length=1, max_length=5_000)
    materials: list[MaterialDefinition] = Field(default_factory=list, max_length=100)
    source_assets: list[SourceAsset] = Field(default_factory=list, max_length=500)
    warnings: list[str] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def validate_references(self) -> PartProject:
        part_ids = [part.id for part in self.parts]
        if len(part_ids) != len(set(part_ids)):
            raise ValueError("Part IDs must be unique")
        instance_ids = [instance.id for instance in self.instances]
        if len(instance_ids) != len(set(instance_ids)):
            raise ValueError("Part instance IDs must be unique")
        if any(instance.part_id not in part_ids for instance in self.instances):
            raise ValueError("Every instance must reference a known part")
        material_ids = {material.id for material in self.materials}
        if any(part.material_id not in material_ids for part in self.parts if part.material_id):
            raise ValueError("Every part material must reference a known material")
        asset_ids = {asset.id for asset in self.source_assets}
        if any(
            part.source_asset_id not in asset_ids
            for part in self.parts
            if part.source_asset_id
        ):
            raise ValueError("Every part source asset must reference a known source asset")
        return self


class ArtifactFile(FrozenModel):
    role: str = Field(min_length=1, max_length=100)
    path: str = Field(min_length=1, max_length=500)
    media_type: str = Field(min_length=1, max_length=100)
    digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    size_bytes: int = Field(ge=0)
    part_id: str | None = None


class ModelArtifact(FrozenModel):
    schema_version: Literal["1", "2"] = "1"
    workflow_id: str
    version: int = Field(ge=1)
    source_path: Path | None = None
    model_path: Path
    project_path: Path | None = None
    three_mf_path: Path | None = None
    source_digest: str | None = None
    model_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    project_digest: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    three_mf_digest: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    manifest_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    mesh: MeshReport
    project: PartProject | None = None
    part_meshes: dict[str, MeshReport] = Field(default_factory=dict)
    files: list[ArtifactFile] = Field(default_factory=list)
    provenance: ArtifactProvenance
    created_at: datetime = Field(default_factory=utc_now)


class PrintWorkflow(FrozenModel):
    id: str = Field(default_factory=new_id)
    requirement: str = Field(min_length=1, max_length=20_000)
    printer_name: str = Field(min_length=1, max_length=100)
    state: WorkflowState = WorkflowState.RECEIVED
    version: int = Field(default=1, ge=1)
    discovery_session_id: str | None = None
    modeling_session_id: str | None = None
    active_handoff_version: int | None = None
    active_artifact_version: int | None = None
    failure_code: str | None = None
    failure_message: str | None = None
    archived_at: datetime | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class WorkflowEvent(FrozenModel):
    id: int | None = None
    workflow_id: str
    kind: str
    state: WorkflowState
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)


class ArtifactApproval(FrozenModel):
    workflow_id: str
    artifact_version: int
    manifest_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    approved_by: str
    approved_at: datetime = Field(default_factory=utc_now)


class RevisionRequest(FrozenModel):
    workflow_id: str
    mode: RevisionMode
    feedback: str = Field(min_length=1, max_length=10_000)
    part_id: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    created_at: datetime = Field(default_factory=utc_now)


class PrintJob(FrozenModel):
    id: str = Field(default_factory=new_id)
    workflow_id: str
    printer_name: str
    external_id: str
    idempotency_key: str
    status: PrintJobStatus
    artifact_version: int
    message: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class WorkItem(FrozenModel):
    id: str = Field(default_factory=new_id)
    workflow_id: str
    kind: WorkKind
    available_at: datetime = Field(default_factory=utc_now)
    attempts: int = 0
    leased_until: datetime | None = None


_ALLOWED_TRANSITIONS: dict[WorkflowState, set[WorkflowState]] = {
    WorkflowState.RECEIVED: {WorkflowState.PLANNING, WorkflowState.CANCELLED},
    WorkflowState.PLANNING: {
        WorkflowState.DISCOVERING,
        WorkflowState.PREPARATION_FAILED,
        WorkflowState.CANCELLED,
    },
    WorkflowState.DISCOVERING: {
        WorkflowState.SEARCHING,
        WorkflowState.PAGE_INSPECTION,
        WorkflowState.SELECTING,
        WorkflowState.PREPARATION_FAILED,
        WorkflowState.CANCELLED,
    },
    WorkflowState.SEARCHING: {
        WorkflowState.DISCOVERING,
        WorkflowState.PREPARATION_FAILED,
        WorkflowState.CANCELLED,
    },
    WorkflowState.PAGE_INSPECTION: {
        WorkflowState.DISCOVERING,
        WorkflowState.PREPARATION_FAILED,
        WorkflowState.CANCELLED,
    },
    WorkflowState.SELECTING: {
        WorkflowState.SOURCE_VALIDATION,
        WorkflowState.HANDOFF_READY,
        WorkflowState.VALIDATING,
        WorkflowState.PREPARATION_FAILED,
        WorkflowState.CANCELLED,
    },
    WorkflowState.SOURCE_VALIDATION: {
        WorkflowState.DISCOVERING,
        WorkflowState.HANDOFF_READY,
        WorkflowState.VALIDATING,
        WorkflowState.PREPARATION_FAILED,
        WorkflowState.CANCELLED,
    },
    WorkflowState.HANDOFF_READY: {
        WorkflowState.GENERATING,
        WorkflowState.PREPARATION_FAILED,
        WorkflowState.CANCELLED,
    },
    WorkflowState.GENERATING: {
        WorkflowState.RENDERING,
        WorkflowState.PREPARATION_FAILED,
        WorkflowState.CANCELLED,
    },
    WorkflowState.RENDERING: {
        WorkflowState.VALIDATING,
        WorkflowState.GENERATING,
        WorkflowState.PREPARATION_FAILED,
        WorkflowState.CANCELLED,
    },
    WorkflowState.VALIDATING: {
        WorkflowState.AWAITING_APPROVAL,
        WorkflowState.GENERATING,
        WorkflowState.DISCOVERING,
        WorkflowState.PREPARATION_FAILED,
        WorkflowState.CANCELLED,
    },
    WorkflowState.AWAITING_APPROVAL: {
        WorkflowState.REVISION_REQUESTED,
        WorkflowState.APPROVED,
        WorkflowState.CANCELLED,
    },
    WorkflowState.REVISION_REQUESTED: {
        WorkflowState.DISCOVERING,
        WorkflowState.HANDOFF_READY,
        WorkflowState.GENERATING,
        WorkflowState.PREPARATION_FAILED,
        WorkflowState.CANCELLED,
    },
    WorkflowState.APPROVED: {
        WorkflowState.REVISION_REQUESTED,
        WorkflowState.SUBMITTING,
        WorkflowState.PRINT_FAILED,
        WorkflowState.CANCELLED,
    },
    WorkflowState.SUBMITTING: {
        WorkflowState.QUEUED,
        WorkflowState.PRINT_FAILED,
        WorkflowState.CANCELLED,
    },
    WorkflowState.QUEUED: {
        WorkflowState.PRINTING,
        WorkflowState.COMPLETED,
        WorkflowState.PRINT_FAILED,
        WorkflowState.CANCELLED,
    },
    WorkflowState.PRINTING: {
        WorkflowState.COMPLETED,
        WorkflowState.PRINT_FAILED,
        WorkflowState.CANCELLED,
    },
    WorkflowState.PREPARATION_FAILED: {WorkflowState.REVISION_REQUESTED},
    WorkflowState.PRINT_FAILED: set(),
    WorkflowState.COMPLETED: set(),
    WorkflowState.CANCELLED: set(),
}


def assert_transition(current: WorkflowState, target: WorkflowState) -> None:
    if target not in _ALLOWED_TRANSITIONS[current]:
        raise InvalidTransitionError(f"Cannot transition workflow from {current} to {target}")
