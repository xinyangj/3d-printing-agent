from __future__ import annotations

import json
from dataclasses import dataclass, field

from copilot import CopilotClient, define_tool
from copilot.generated.rpc import PermissionDecisionApproveOnce, PermissionDecisionReject
from copilot.generated.session_events import PermissionRequest
from copilot.session import PermissionRequestResult
from copilot.tools import ToolResult
from pydantic import BaseModel, Field

from printing_agent.config import Settings
from printing_agent.domain import ModelArtifact
from printing_agent.errors import (
    ToolCorrectionExhaustedError,
    ValidationError,
)
from printing_agent.fabrication import (
    JobOverrides,
    MaterialAssignment,
    MaterialCandidate,
    MaterialDefinitionRevision,
    PartMaterialAssignment,
    PartMaterialRequest,
    PhysicalSpool,
    PrinterProfileRevision,
    SlotPolicy,
    SpoolStatus,
    ciede2000,
    srgb_to_lab,
)
from printing_agent.repositories import WorkflowRepository


class AgentPartAssignment(BaseModel):
    part_id: str
    spool_id: str
    toolhead_id: str
    confidence: float = Field(ge=0, le=1)
    rationale: str = Field(min_length=1, max_length=2_000)
    alternatives: list[str] = Field(default_factory=list, max_length=3)


class AgentMaterialAssignments(BaseModel):
    assignments: list[AgentPartAssignment] = Field(min_length=1, max_length=500)
    rationale: str = Field(min_length=1, max_length=4_000)


@dataclass
class _AssignmentState:
    result: AgentMaterialAssignments | None = None
    rejections: list[str] = field(default_factory=list)


class MaterialAssignmentAgent:
    def __init__(
        self,
        settings: Settings,
        repository: WorkflowRepository,
    ) -> None:
        self.settings = settings
        self.repository = repository

    async def assign(
        self,
        workflow_id: str,
        requests: list[PartMaterialRequest],
        candidates: dict[str, list[MaterialCandidate]],
    ) -> AgentMaterialAssignments:
        state = _AssignmentState()
        allowed = {
            (part_id, candidate.spool_id)
            for part_id, values in candidates.items()
            for candidate in values
        }

        @define_tool(
            name="submit_material_assignments",
            description=(
                "Assign every semantic part to one allowed physical spool and compatible "
                "toolhead using only supplied candidates."
            ),
            defer="never",
        )
        async def submit_material_assignments(
            params: AgentMaterialAssignments,
        ) -> ToolResult:
            part_ids = [item.part_id for item in params.assignments]
            required = {request.part_id for request in requests}
            if len(part_ids) != len(set(part_ids)) or set(part_ids) != required:
                message = "Assignments must contain every requested part exactly once."
                state.rejections.append(message)
                return ToolResult(result_type="rejected", text_result_for_llm=message)
            invalid = [
                item.part_id
                for item in params.assignments
                if (item.part_id, item.spool_id) not in allowed
            ]
            if invalid:
                message = (
                    "Assignments reference spools outside the allowed candidate set: "
                    + ", ".join(invalid)
                )
                state.rejections.append(message)
                return ToolResult(result_type="rejected", text_result_for_llm=message)
            state.result = params
            return ToolResult(text_result_for_llm="Material assignments accepted.")

        allowed_tools = {submit_material_assignments.name}

        async def permission_handler(
            request: PermissionRequest,
            invocation: dict[str, str],
        ) -> PermissionRequestResult:
            del invocation
            if request.tool_name in allowed_tools:
                return PermissionDecisionApproveOnce()
            return PermissionDecisionReject(
                feedback="Tool is unavailable to the material assignment role"
            )

        role_dir = (
            self.settings.data_dir
            / "copilot-workspaces"
            / workflow_id
            / "material-assignment"
        )
        role_dir.mkdir(parents=True, exist_ok=True)
        prompt = (
            "Assign the closest usable physical material to every requested semantic part. "
            "Compatibility is already filtered and cannot be overridden. Prefer the lowest "
            "CIEDE2000 distance while considering material family, finish, remaining quantity, "
            "manual swaps, and tool changes. Call submit_material_assignments.\n\n"
            + json.dumps(
                {
                    "requests": [item.model_dump(mode="json") for item in requests],
                    "candidates": {
                        key: [item.model_dump(mode="json") for item in values[:10]]
                        for key, values in candidates.items()
                    },
                },
                sort_keys=True,
                indent=2,
            )
        )
        async with CopilotClient(
            mode="empty",
            base_directory=str(self.settings.data_dir / "copilot"),
            working_directory=str(role_dir),
        ) as client:
            session = await client.create_session(
                on_permission_request=permission_handler,
                model=self.settings.copilot_model,
                tools=[submit_material_assignments],
                available_tools=sorted(allowed_tools),
                system_message={
                    "mode": "replace",
                    "content": (
                        "You assign configured physical materials to printable semantic parts. "
                        "Use only supplied compatible candidates. You cannot change slot policy, "
                        "invent inventory, or select forbidden slots."
                    ),
                },
                working_directory=str(role_dir),
                enable_session_store=False,
                enable_config_discovery=False,
                mcp_servers={},
            )
            try:
                current_prompt = prompt
                for attempt in range(1, 5):
                    await session.send_and_wait(current_prompt, timeout=300)
                    if state.result is not None:
                        return state.result
                    reason = (
                        state.rejections[-1]
                        if state.rejections
                        else "No accepted material assignment was submitted"
                    )
                    await self.repository.record_workflow_event(
                        workflow_id,
                        "role.tool_correction",
                        {
                            "role": "material_assignment",
                            "attempt": attempt,
                            "reason": reason,
                        },
                    )
                    current_prompt = (
                        f"{prompt}\n\nCorrect the previous rejected assignment. "
                        f"Diagnostic: {reason}"
                    )
            finally:
                await session.disconnect()
        raise ToolCorrectionExhaustedError(
            "Material assignment exhausted 4 correction attempts"
        )


class MaterialAssignmentService:
    def __init__(
        self,
        agent: MaterialAssignmentAgent,
    ) -> None:
        self.agent = agent

    @staticmethod
    def part_requests(
        artifact: ModelArtifact,
        default_material_family: str | None = None,
    ) -> list[PartMaterialRequest]:
        if artifact.project is None:
            raise ValidationError("Artifact has no semantic part project")
        colors = {
            material.id: material.color.upper()
            for material in artifact.project.materials
        }
        families = {
            material.id: material.printing_material
            for material in artifact.project.materials
        }
        quantities = {
            part.id: sum(
                instance.part_id == part.id
                for instance in artifact.project.instances
            )
            for part in artifact.project.parts
        }
        fallback_weight = (
            artifact.mesh.volume_mm3
            / 1_000
            * 1.3
            / max(1, len(artifact.project.parts))
        )
        return [
            PartMaterialRequest(
                part_id=part.id,
                part_name=part.name,
                requested_color=colors.get(part.material_id or "", "#B7C4D4"),
                requested_family=(
                    families.get(part.material_id or "")
                    or default_material_family
                ),
                estimated_weight_g=(
                    artifact.part_meshes[part.id].volume_mm3
                    / 1_000
                    * 1.3
                    * max(1, quantities.get(part.id, 1))
                )
                if part.id in artifact.part_meshes
                else fallback_weight * max(1, quantities.get(part.id, 1)),
            )
            for part in artifact.project.parts
        ]

    @staticmethod
    def compatible_candidates(
        *,
        requests: list[PartMaterialRequest],
        profile: PrinterProfileRevision,
        policy: SlotPolicy,
        overrides: JobOverrides,
        spools: list[PhysicalSpool],
        materials: dict[tuple[str, int], MaterialDefinitionRevision],
    ) -> dict[str, list[MaterialCandidate]]:
        slots = {slot.id: slot for slot in profile.spec.material_slots}
        toolheads = {toolhead.id: toolhead for toolhead in profile.spec.toolheads}
        candidates: dict[str, list[MaterialCandidate]] = {}
        for request in requests:
            values: list[MaterialCandidate] = []
            for spool in spools:
                if spool.status not in {SpoolStatus.AVAILABLE, SpoolStatus.LOADED}:
                    continue
                if spool.slot_id is None or spool.printer_profile_id != profile.profile_id:
                    continue
                if spool.slot_id in policy.forbidden_slot_ids:
                    continue
                if policy.allowed_slot_ids is not None and (
                    spool.slot_id not in policy.allowed_slot_ids
                ):
                    continue
                if spool.slot_id in policy.part_forbidden_slot_ids.get(
                    request.part_id, set()
                ):
                    continue
                part_allowed = policy.part_allowed_slot_ids.get(request.part_id)
                if part_allowed is not None and spool.slot_id not in part_allowed:
                    continue
                slot = slots.get(spool.slot_id)
                material = materials.get(
                    (spool.material_id, spool.material_revision)
                )
                if slot is None or material is None:
                    continue
                if slot.manual_swap_required and not policy.allow_manual_swaps:
                    continue
                family = material.spec.family.casefold()
                if request.requested_family and (
                    family != request.requested_family.casefold()
                ):
                    continue
                if (
                    slot.supported_material_families
                    and family not in slot.supported_material_families
                ):
                    continue
                compatible_toolheads = {
                    toolhead_id
                    for toolhead_id in slot.compatible_toolhead_ids
                    if toolhead_id in toolheads
                    and (
                        not toolheads[toolhead_id].supported_material_families
                        or family
                        in toolheads[toolhead_id].supported_material_families
                    )
                    and (
                        not material.spec.hardened_nozzle_required
                        or toolheads[toolhead_id].hardened
                    )
                    and (
                        not material.spec.supported_nozzle_diameters_mm
                        or (
                            overrides.nozzle_diameter_mm
                            if overrides.nozzle_diameter_mm is not None
                            and (
                                overrides.toolhead_id is None
                                or overrides.toolhead_id == toolhead_id
                            )
                            else toolheads[toolhead_id].nozzle_diameter_mm
                        )
                        in material.spec.supported_nozzle_diameters_mm
                    )
                    and (
                        material.spec.nozzle_temperature_c[1]
                        <= toolheads[toolhead_id].max_temperature_c
                    )
                }
                if overrides.toolhead_id is not None:
                    compatible_toolheads &= {overrides.toolhead_id}
                if not compatible_toolheads:
                    continue
                effective_plate_id = (
                    overrides.plate_id
                    or (
                        profile.spec.plates[0].id
                        if profile.spec.plates
                        else None
                    )
                )
                plate = next(
                    (
                        item
                        for item in profile.spec.plates
                        if item.id == effective_plate_id
                    ),
                    None,
                )
                if plate is None:
                    continue
                if (
                    material.spec.supported_plate_ids
                    and plate.id not in material.spec.supported_plate_ids
                ):
                    continue
                if material.spec.bed_temperature_c[1] > plate.max_temperature_c:
                    continue
                if (
                    request.estimated_weight_g is not None
                    and spool.remaining_weight_g < request.estimated_weight_g * 1.1
                ):
                    continue
                spool_color = spool.measured_color or material.spec.assignment_color
                values.append(
                    MaterialCandidate(
                        part_id=request.part_id,
                        spool_id=spool.id,
                        slot_id=spool.slot_id,
                        material_id=material.material_id,
                        material_revision=material.revision,
                        material_digest=material.digest or "0" * 64,
                        color=spool_color,
                        color_distance=ciede2000(
                            srgb_to_lab(request.requested_color),
                            srgb_to_lab(spool_color),
                        ),
                        remaining_weight_g=spool.remaining_weight_g,
                        toolhead_ids=compatible_toolheads,
                        slicer_filament_profile_id=(
                            material.spec.slicer_filament_profile_id
                        ),
                        slicer_filament_profile_digest=(
                            material.spec.slicer_profile_digest
                        ),
                        slicer_filament_dependency_digests=(
                            material.spec.slicer_profile_dependency_digests
                        ),
                        warnings=(
                            ["Manual spool swap required"]
                            if slot.manual_swap_required
                            else []
                        ),
                    )
                )
            values.sort(key=lambda item: (item.color_distance, item.spool_id))
            if not values:
                raise ValidationError(
                    f"No usable configured material is available for '{request.part_name}'"
                )
            candidates[request.part_id] = values
        return candidates

    async def propose(
        self,
        *,
        workflow_id: str,
        artifact: ModelArtifact,
        profile: PrinterProfileRevision,
        printer_snapshot_digest: str,
        policy: SlotPolicy,
        overrides: JobOverrides,
        spools: list[PhysicalSpool],
        materials: dict[tuple[str, int], MaterialDefinitionRevision],
        maximum_color_distance: float,
        default_material_family: str | None = None,
    ) -> MaterialAssignment:
        requests = self.part_requests(artifact, default_material_family)
        candidates = self.compatible_candidates(
            requests=requests,
            profile=profile,
            policy=policy,
            overrides=overrides,
            spools=spools,
            materials=materials,
        )
        result = await self.agent.assign(workflow_id, requests, candidates)
        selected: list[PartMaterialAssignment] = []
        confirmation_required = False
        for assignment in result.assignments:
            candidate = next(
                item
                for item in candidates[assignment.part_id]
                if item.spool_id == assignment.spool_id
            )
            if assignment.toolhead_id not in candidate.toolhead_ids:
                raise ValidationError(
                    f"Toolhead '{assignment.toolhead_id}' is invalid for {assignment.part_id}"
                )
            if (
                candidate.color_distance > maximum_color_distance
                or candidate.spool_id != candidates[assignment.part_id][0].spool_id
                or assignment.confidence < 0.8
                or candidate.warnings
            ):
                confirmation_required = True
            selected.append(
                PartMaterialAssignment(
                    part_id=assignment.part_id,
                    spool_id=candidate.spool_id,
                    slot_id=candidate.slot_id,
                    toolhead_id=assignment.toolhead_id,
                    material_id=candidate.material_id,
                    material_revision=candidate.material_revision,
                    material_digest=candidate.material_digest,
                    slicer_filament_profile_id=candidate.slicer_filament_profile_id,
                    slicer_filament_profile_digest=(
                        candidate.slicer_filament_profile_digest
                    ),
                    slicer_filament_dependency_digests=(
                        candidate.slicer_filament_dependency_digests
                    ),
                    color_distance=candidate.color_distance,
                    confidence=assignment.confidence,
                    rationale=assignment.rationale,
                    alternatives=assignment.alternatives,
                )
            )
        policy_digest = json.dumps(
            policy.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
        )
        import hashlib

        return MaterialAssignment(
            workflow_id=workflow_id,
            artifact_version=artifact.version,
            artifact_manifest_digest=artifact.manifest_digest,
            printer_snapshot_digest=printer_snapshot_digest,
            slot_policy_digest=hashlib.sha256(policy_digest.encode()).hexdigest(),
            requests=requests,
            assignments=selected,
            requires_confirmation=confirmation_required,
        ).with_digest()
