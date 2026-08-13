from __future__ import annotations

from pathlib import Path

import pytest
import trimesh

from printing_agent.artifact_store import ArtifactStore
from printing_agent.config import Settings
from printing_agent.domain import (
    ArtifactApproval,
    Dimensions,
    ModelDecision,
    ModelingHandoff,
    ModelPlan,
    PrinterCapabilitySummary,
    WorkflowState,
    WorkKind,
)
from printing_agent.errors import PolicyViolationError
from printing_agent.modeling import MeshInspector, ModelPipeline, OpenScadSourcePolicy
from printing_agent.repositories import WorkflowRepository


class BoxRenderer:
    async def render(self, source_path: Path, output_path: Path) -> str:
        trimesh.creation.box(extents=(20, 20, 20)).export(output_path)
        return "rendered fixture"


def test_source_policy_rejects_external_capabilities() -> None:
    policy = OpenScadSourcePolicy()

    with pytest.raises(PolicyViolationError):
        policy.validate('include <secrets.scad>; cube(10);', None)
    with pytest.raises(PolicyViolationError):
        policy.validate('import("../source.stl");', "source.stl")
    with pytest.raises(PolicyViolationError):
        policy.validate('import("other.stl");', "source.stl")

    policy.validate("cube([10, 10, 10]);", None)
    policy.validate('import("source.stl");', "source.stl")


async def test_source_is_adopted_only_after_mesh_validation(
    settings: Settings,
    repository: WorkflowRepository,
) -> None:
    workflow = await repository.create_workflow("Create a 20 mm cube", "simulator")
    initial_work = await repository.lease_next()
    assert initial_work is not None
    await repository.complete_work(initial_work.id)
    await repository.transition(workflow.id, WorkflowState.PLANNING)
    await repository.save_plan(
        workflow.id,
        ModelPlan(
            search_query="cube",
            geometry_summary="A cube",
            target_dimensions=Dimensions(width_mm=20, depth_mm=20, height_mm=20),
        ),
    )
    await repository.transition(workflow.id, WorkflowState.DISCOVERING)
    await repository.transition(workflow.id, WorkflowState.SELECTING)
    handoff = ModelingHandoff(
        workflow_id=workflow.id,
        version=1,
        requirement=workflow.requirement,
        model_plan=await repository.get_plan(workflow.id),
        decision=ModelDecision.CREATE,
        target_printer=PrinterCapabilitySummary(
            name="simulator",
            build_volume=Dimensions(width_mm=220, depth_mm=220, height_mm=250),
            accepted_formats={"stl"},
        ),
        discovery_rationale="Create a deterministic fixture",
    ).with_digest()
    await repository.save_handoff(handoff)
    await repository.transition(workflow.id, WorkflowState.HANDOFF_READY)
    await repository.transition(workflow.id, WorkflowState.GENERATING)
    pipeline = ModelPipeline(
        settings,
        repository,
        ArtifactStore(settings.artifact_dir),
        BoxRenderer(),  # type: ignore[arg-type]
        MeshInspector(),
    )

    artifact = await pipeline.adopt_source(handoff, "cube([20, 20, 20]);")

    loaded = await repository.get_workflow(workflow.id)
    assert loaded.state == WorkflowState.AWAITING_APPROVAL
    assert artifact.model_path.is_file()
    assert artifact.mesh.watertight
    assert artifact.manifest_digest

    await repository.approve_and_enqueue(
        ArtifactApproval(
            workflow_id=workflow.id,
            artifact_version=artifact.version,
            manifest_digest=artifact.manifest_digest,
            approved_by="test",
        )
    )
    approved = await repository.get_workflow(workflow.id)
    submission = await repository.lease_next()
    assert approved.state == WorkflowState.APPROVED
    assert submission is not None and submission.kind == WorkKind.SUBMIT
