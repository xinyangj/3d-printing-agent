from __future__ import annotations

import pytest

from printing_agent.domain import (
    Dimensions,
    ModelDecision,
    ModelingHandoff,
    ModelPlan,
    PrinterCapabilitySummary,
    WorkflowState,
    WorkKind,
)
from printing_agent.errors import InvalidTransitionError
from printing_agent.repositories import WorkflowRepository


async def test_workflow_transitions_are_persisted_with_events(
    repository: WorkflowRepository,
) -> None:
    workflow = await repository.create_workflow("Print a small cube", "simulator")
    planning = await repository.transition(workflow.id, WorkflowState.PLANNING)

    assert planning.state == WorkflowState.PLANNING
    events = await repository.list_events(workflow.id)
    assert [event.state for event in events] == [
        WorkflowState.RECEIVED,
        WorkflowState.PLANNING,
    ]


async def test_illegal_transition_is_rejected(repository: WorkflowRepository) -> None:
    workflow = await repository.create_workflow("Print a small cube", "simulator")

    with pytest.raises(InvalidTransitionError):
        await repository.transition(workflow.id, WorkflowState.SUBMITTING)


async def test_approved_workflow_can_record_submission_failure(
    repository: WorkflowRepository,
) -> None:
    workflow = await repository.create_workflow("Print a small cube", "simulator")
    item = await repository.lease_next()
    assert item is not None and item.kind == WorkKind.PREPARE
    await repository.complete_work(item.id)
    for state in (
        WorkflowState.PLANNING,
        WorkflowState.DISCOVERING,
        WorkflowState.SELECTING,
        WorkflowState.VALIDATING,
        WorkflowState.AWAITING_APPROVAL,
        WorkflowState.APPROVED,
        WorkflowState.PRINT_FAILED,
    ):
        workflow = await repository.transition(workflow.id, state)

    assert workflow.state == WorkflowState.PRINT_FAILED


def test_modeling_handoff_digest_is_stable_and_mode_is_validated() -> None:
    handoff = ModelingHandoff(
        workflow_id="workflow",
        version=1,
        requirement="Create a cube",
        model_plan=ModelPlan(
            search_query="cube",
            geometry_summary="A simple cube",
            target_dimensions=Dimensions(width_mm=20, depth_mm=20, height_mm=20),
        ),
        decision=ModelDecision.CREATE,
        target_printer=PrinterCapabilitySummary(
            name="simulator",
            build_volume=Dimensions(width_mm=220, depth_mm=220, height_mm=250),
            accepted_formats={"stl"},
        ),
        discovery_rationale="No suitable source was found",
    ).with_digest()

    assert handoff.digest
    assert handoff.with_digest().digest == handoff.digest
