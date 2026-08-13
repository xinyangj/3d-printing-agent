from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from printing_agent.domain import (
    ArtifactApproval,
    Dimensions,
    ModelCandidate,
    ModelDecision,
    ModelingHandoff,
    ModelPlan,
    PrinterCapabilitySummary,
    RevisionMode,
    RevisionRequest,
    SearchRound,
    WorkflowState,
    WorkKind,
)
from printing_agent.errors import ConflictError, InvalidTransitionError
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


async def test_search_round_persists_model_candidate_objects(
    repository: WorkflowRepository,
) -> None:
    workflow = await repository.create_workflow("Print a toy truck", "simulator")
    candidate = ModelCandidate(
        id="truck-1",
        title="Printable toy truck",
        source_url="https://www.thingiverse.com/thing:truck-1",
        license="cc-by",
        allows_derivatives=True,
    )
    search_round = SearchRound(
        workflow_id=workflow.id,
        query="toy truck",
        page=1,
        candidate_ids=[candidate.id],
    )

    await repository.save_search_round(search_round, [candidate])
    loaded_round, candidates = await repository.get_search_round(search_round.id)

    assert loaded_round == search_round
    assert candidates[0]["title"] == candidate.title


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


async def test_failed_preparation_can_request_new_base(
    repository: WorkflowRepository,
) -> None:
    workflow = await repository.create_workflow("Print a small cube", "simulator")
    pending = await repository.lease_next()
    assert pending is not None
    await repository.complete_work(pending.id)
    await repository.transition(workflow.id, WorkflowState.PLANNING)
    await repository.transition(workflow.id, WorkflowState.PREPARATION_FAILED)

    await repository.request_revision(
        RevisionRequest(
            workflow_id=workflow.id,
            mode=RevisionMode.SEARCH_NEW_BASE,
            feedback="Retry catalog discovery",
        )
    )

    revised = await repository.get_workflow(workflow.id)
    assert revised.state == WorkflowState.REVISION_REQUESTED


async def test_workflow_archive_and_restore_preserve_state(
    repository: WorkflowRepository,
) -> None:
    workflow = await repository.create_workflow("Print a small cube", "simulator")
    pending = await repository.lease_next()
    assert pending is not None
    await repository.complete_work(pending.id)
    for state in (
        WorkflowState.PLANNING,
        WorkflowState.DISCOVERING,
        WorkflowState.SELECTING,
        WorkflowState.VALIDATING,
        WorkflowState.AWAITING_APPROVAL,
    ):
        workflow = await repository.transition(workflow.id, state)

    archived = await repository.archive_workflow(workflow.id)

    assert archived.state == WorkflowState.AWAITING_APPROVAL
    assert archived.archived_at is not None
    assert (await repository.get_workflow(workflow.id)).archived_at == archived.archived_at
    with pytest.raises(ConflictError, match="already archived"):
        await repository.archive_workflow(workflow.id)
    with pytest.raises(ConflictError, match="Restore the archived workflow"):
        await repository.transition(workflow.id, WorkflowState.APPROVED)
    with pytest.raises(ConflictError, match="Restore the archived workflow"):
        await repository.approve_artifact(
            ArtifactApproval(
                workflow_id=workflow.id,
                artifact_version=1,
                manifest_digest="a" * 64,
                approved_by="test",
            )
        )
    with pytest.raises(ConflictError, match="Restore the archived workflow"):
        await repository.request_revision(
            RevisionRequest(
                workflow_id=workflow.id,
                mode=RevisionMode.SEARCH_NEW_BASE,
                feedback="Find a rounder model",
            )
        )
    with pytest.raises(ConflictError, match="Restore the archived workflow"):
        await repository.enqueue_print_submission(workflow.id)

    restored = await repository.restore_workflow(workflow.id)

    assert restored.state == WorkflowState.AWAITING_APPROVAL
    assert restored.archived_at is None
    assert [event.kind for event in await repository.list_events(workflow.id)][-2:] == [
        "workflow.archived",
        "workflow.restored",
    ]
    with pytest.raises(ConflictError, match="not archived"):
        await repository.restore_workflow(workflow.id)


async def test_active_workflow_cannot_be_archived(repository: WorkflowRepository) -> None:
    workflow = await repository.create_workflow("Print a small cube", "simulator")

    with pytest.raises(ConflictError, match="preparation or printing is active"):
        await repository.archive_workflow(workflow.id)


async def test_cancelled_workflow_waits_for_durable_work_to_finish(
    repository: WorkflowRepository,
) -> None:
    workflow = await repository.create_workflow("Print a small cube", "simulator")
    await repository.transition(workflow.id, WorkflowState.CANCELLED)

    with pytest.raises(ConflictError, match="background work is still active"):
        await repository.archive_workflow(workflow.id)

    pending = await repository.lease_next()
    assert pending is not None
    await repository.complete_work(pending.id)

    archived = await repository.archive_workflow(workflow.id)
    assert archived.archived_at is not None


async def test_initialize_migrates_existing_workflow_table(tmp_path: Path) -> None:
    database_path = tmp_path / "legacy.db"
    with sqlite3.connect(database_path) as db:
        db.executescript(
            """
            CREATE TABLE workflows (
                id TEXT PRIMARY KEY,
                requirement TEXT NOT NULL,
                printer_name TEXT NOT NULL,
                state TEXT NOT NULL,
                version INTEGER NOT NULL,
                discovery_session_id TEXT,
                modeling_session_id TEXT,
                active_handoff_version INTEGER,
                active_artifact_version INTEGER,
                failure_code TEXT,
                failure_message TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            INSERT INTO workflows (
                id, requirement, printer_name, state, version, created_at, updated_at
            ) VALUES (
                'legacy-workflow', 'Create a cube', 'simulator', 'completed', 3,
                '2026-01-01T00:00:00+00:00', '2026-01-02T00:00:00+00:00'
            );
            """
        )
    repository = WorkflowRepository(database_path)

    await repository.initialize()
    workflow = await repository.get_workflow("legacy-workflow")

    assert workflow.archived_at is None
    assert workflow.state == WorkflowState.COMPLETED


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
