from __future__ import annotations

import asyncio
import json
import shutil
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
import trimesh

from printing_agent.application import PrintingApplication
from printing_agent.artifact_store import ArtifactStore, sha256_file
from printing_agent.config import Settings
from printing_agent.domain import (
    AnnotationOrigin,
    ArtifactApproval,
    ArtifactProvenance,
    CandidateAttempt,
    CandidateFile,
    Dimensions,
    DiscoveryDecision,
    MaterialDefinition,
    ModelCandidate,
    ModelDecision,
    ModelingHandoff,
    ModelPlan,
    PartDefinition,
    PartGeometryKind,
    PartInstance,
    PartProject,
    PrinterCapabilitySummary,
    PrintJob,
    PrintJobStatus,
    PrintSettings,
    RevisionMode,
    RevisionRequest,
    RevisionVerification,
    RevisionVerificationCheck,
    SelectedCandidateFile,
    SelectedFileRole,
    SelectedSourceSummary,
    WorkflowState,
    WorkKind,
)
from printing_agent.errors import (
    CandidateRejectedError,
    ConflictError,
    ExternalServiceError,
    NotFoundError,
    PolicyViolationError,
    ValidationError,
)
from printing_agent.modeling import (
    MeshInspector,
    ModelPipeline,
    OpenScadSourcePolicy,
    TrimeshSelectedSourceInspector,
)
from printing_agent.multipart import ThreeMFService, _module_name
from printing_agent.printers import SimulatedPrinterAdapter
from printing_agent.repositories import WorkflowRepository


class BoxRenderer:
    async def render(self, source_path: Path, output_path: Path) -> str:
        trimesh.creation.box(extents=(20, 20, 20)).export(output_path)
        return "rendered fixture"


class PartRenderer:
    async def render(self, source_path: Path, output_path: Path) -> str:
        mesh = trimesh.load_mesh(source_path.parent / "source.stl", force="mesh")
        mesh.apply_scale(3)
        mesh.export(output_path)
        return "rendered revised part"


class FailingPartRenderer(PartRenderer):
    async def render(self, source_path: Path, output_path: Path) -> str:
        if source_path.parent.name == "axle":
            raise ValidationError("fixture axle render failed")
        return await super().render(source_path, output_path)


class UnavailableRenderer:
    async def render(self, source_path: Path, output_path: Path) -> str:
        del source_path, output_path
        raise ExternalServiceError("OpenSCAD service is unavailable")


class CopyCatalog:
    def __init__(self, paths: dict[str, Path]) -> None:
        self.paths = paths

    async def download_file(
        self,
        candidate: ModelCandidate,
        file_id: str,
        destination: Path,
    ) -> Path:
        del candidate
        await asyncio.to_thread(destination.parent.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(shutil.copy2, self.paths[file_id], destination)
        return destination


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


def test_openscad_module_names_are_valid_and_collision_resistant() -> None:
    hyphenated = _module_name("front-wheel")
    underscored = _module_name("front_wheel")

    assert hyphenated != underscored
    assert hyphenated[0].isalpha()
    assert all(character.isalnum() or character == "_" for character in hyphenated)


async def test_source_inspection_rejects_non_watertight_mesh(
    tmp_path: Path,
) -> None:
    valid_path = tmp_path / "valid.stl"
    invalid_path = tmp_path / "invalid.stl"
    invalid_3mf_path = tmp_path / "invalid.3mf"
    trimesh.creation.box(extents=(10, 10, 10)).export(valid_path)
    invalid = trimesh.creation.box(extents=(10, 10, 10))
    invalid.update_faces(range(len(invalid.faces) - 1))
    invalid.remove_unreferenced_vertices()
    invalid.export(invalid_path)
    invalid_3mf_path.write_bytes(b"not a 3mf archive")
    inspector = TrimeshSelectedSourceInspector(MeshInspector())
    volume = Dimensions(width_mm=220, depth_mm=220, height_mm=250)

    valid = await inspector.inspect("workflow", "candidate", "valid", valid_path, volume)
    rejected = await inspector.inspect(
        "workflow", "candidate", "invalid", invalid_path, volume
    )
    rejected_3mf = await inspector.inspect(
        "workflow", "candidate", "invalid-3mf", invalid_3mf_path, volume
    )

    assert valid.accepted is True
    assert rejected.accepted is False
    assert rejected.mesh.watertight is False
    assert "not watertight" in (rejected.rejection_reason or "")
    assert rejected_3mf.accepted is False
    assert "Could not parse 3MF" in (rejected_3mf.rejection_reason or "")


async def test_source_preparation_can_edit_one_file_and_preserve_another(
    settings: Settings,
    repository: WorkflowRepository,
    tmp_path: Path,
) -> None:
    body_path = tmp_path / "body.stl"
    wheel_path = tmp_path / "wheel.stl"
    trimesh.creation.box(extents=(20, 10, 5)).export(body_path)
    trimesh.creation.cylinder(radius=3, height=2).export(wheel_path)
    inspector = MeshInspector()
    body_selection = SelectedCandidateFile(
        file_id="body-file",
        role=SelectedFileRole.UNIQUE_PART,
        part_id="body",
        part_name="Body",
        rationale="Car body",
        confidence=1,
    )
    wheel_selection = SelectedCandidateFile(
        file_id="wheel-file",
        role=SelectedFileRole.UNIQUE_PART,
        part_id="wheel",
        part_name="Wheel",
        quantity=4,
        rationale="Car wheels",
        confidence=1,
    )
    selected = [
        (
            body_selection,
            CandidateFile(id="body-file", name="body.stl", format="stl"),
            body_path,
            await inspector.inspect(body_path),
        ),
        (
            wheel_selection,
            CandidateFile(id="wheel-file", name="wheel.stl", format="stl"),
            wheel_path,
            await inspector.inspect(wheel_path),
        ),
    ]
    pipeline = ModelPipeline(
        settings,
        repository,
        ArtifactStore(settings.artifact_dir),
        PartRenderer(),  # type: ignore[arg-type]
        inspector,
    )
    wheel_digest = sha256_file(wheel_path)

    prepared, workspace = await pipeline.prepare_source_set_files(
        str(uuid4()),
        selected,
        [("body", 'import("source.stl");', "Scale the body")],
    )

    prepared_by_id = {item[0].part_id: item for item in prepared}
    assert prepared_by_id["body"][3].dimensions.width_mm == pytest.approx(60)
    assert prepared_by_id["wheel"][2] == wheel_path
    assert sha256_file(prepared_by_id["wheel"][2]) == wheel_digest
    shutil.rmtree(workspace)


async def test_source_set_validation_is_atomic_before_cache_promotion(
    settings: Settings,
    repository: WorkflowRepository,
    tmp_path: Path,
) -> None:
    workflow = await repository.create_workflow("Create a car", "simulator")
    pending = await repository.lease_next()
    assert pending is not None
    await repository.complete_work(pending.id)
    for state in (
        WorkflowState.PLANNING,
        WorkflowState.DISCOVERING,
        WorkflowState.SELECTING,
    ):
        await repository.transition(workflow.id, state)
    body_path = tmp_path / "body.stl"
    wheel_path = tmp_path / "wheel.stl"
    body = trimesh.creation.box(extents=(40, 20, 10))
    body.update_faces(range(len(body.faces) - 1))
    body.remove_unreferenced_vertices()
    body.export(body_path)
    trimesh.creation.cylinder(radius=5, height=4).export(wheel_path)
    candidate = ModelCandidate(
        id="car",
        title="Car",
        introduction="Body and wheels",
        source_url="https://example.test/car",
        creator="maker",
        license="CC BY",
        files=[
            CandidateFile(id="body-file", name="body.stl", format="stl"),
            CandidateFile(id="wheel-file", name="wheel.stl", format="stl"),
        ],
    )
    selections = [
        SelectedCandidateFile(
            file_id="body-file",
            role=SelectedFileRole.UNIQUE_PART,
            part_id="body",
            part_name="Body",
            rationale="Required body",
            confidence=1,
        ),
        SelectedCandidateFile(
            file_id="wheel-file",
            role=SelectedFileRole.UNIQUE_PART,
            part_id="wheel",
            part_name="Wheel",
            quantity=4,
            rationale="Required wheels",
            confidence=1,
        ),
    ]
    decision = DiscoveryDecision(
        decision=ModelDecision.USE_AS_IS,
        candidate_id=candidate.id,
        selected_files=selections,
        rationale="Use complete car set",
    )
    application = SimpleNamespace(
        settings=settings,
        repository=repository,
        catalog=CopyCatalog({"body-file": body_path, "wheel-file": wheel_path}),
        source_inspector=TrimeshSelectedSourceInspector(MeshInspector()),
    )

    with pytest.raises(CandidateRejectedError, match="required source-set"):
        await PrintingApplication._adopt_source_set(  # type: ignore[arg-type]
            application,
            workflow.id,
            candidate,
            decision,
            Dimensions(width_mm=220, depth_mm=220, height_mm=250),
        )

    assert not list((settings.candidate_cache_dir / "models").glob("*.stl"))
    assert not (
        settings.candidate_cache_dir / "models" / "incoming" / workflow.id
    ).exists()


async def test_candidate_rejections_advance_then_fall_back_to_creation(
    settings: Settings,
    repository: WorkflowRepository,
) -> None:
    workflow = await repository.create_workflow("Create a car", "simulator")
    pending = await repository.lease_next()
    assert pending is not None
    await repository.complete_work(pending.id)
    await repository.transition(workflow.id, WorkflowState.PLANNING)
    plan = ModelPlan(search_query="car", geometry_summary="A toy car")
    await repository.save_plan(workflow.id, plan)
    for state in (
        WorkflowState.DISCOVERING,
        WorkflowState.SELECTING,
        WorkflowState.SOURCE_VALIDATION,
    ):
        await repository.transition(workflow.id, state)
    abandoned_handoff = ModelingHandoff(
        workflow_id=workflow.id,
        version=1,
        requirement=workflow.requirement,
        model_plan=plan,
        decision=ModelDecision.CREATE,
        target_printer=PrinterCapabilitySummary(
            name="simulator",
            build_volume=Dimensions(width_mm=220, depth_mm=220, height_mm=250),
            accepted_formats={"stl"},
        ),
        discovery_rationale="Fixture abandoned handoff",
    ).with_digest()
    await repository.save_handoff(abandoned_handoff)
    await repository.patch_workflow(
        workflow.id,
        modeling_session_id="abandoned-session",
    )

    class RetryDiscovery:
        def __init__(self) -> None:
            self.attempts: list[CandidateAttempt] = []

        async def resume_after_candidate_rejection(
            self,
            workflow_id: str,
            attempt: CandidateAttempt,
        ) -> DiscoveryDecision:
            self.attempts.append(attempt)
            next_id = f"candidate-{len(self.attempts) + 1}"
            await repository.transition(
                workflow_id,
                WorkflowState.SELECTING,
                event_kind="fixture.next_candidate",
            )
            return DiscoveryDecision(
                decision=ModelDecision.USE_AS_IS,
                candidate_id=next_id,
                file_id=f"file-{next_id}",
                rationale="Try the next ranked candidate",
            )

    discovery = RetryDiscovery()
    application = SimpleNamespace(
        settings=settings,
        repository=repository,
        discovery=discovery,
        artifacts=ArtifactStore(settings.artifact_dir),
    )

    first = await PrintingApplication._retry_candidate_or_create(  # type: ignore[arg-type]
        application,
        workflow.id,
        "candidate-1",
        ["file-1"],
        CandidateRejectedError(
            "body is not watertight",
            stage="modification",
            diagnostics={"file-1": "mesh is not watertight"},
        ),
    )
    assert first.candidate_id == "candidate-2"
    after_first = await repository.get_workflow(workflow.id)
    assert after_first.active_handoff_version is None
    assert after_first.modeling_session_id is None
    await repository.transition(workflow.id, WorkflowState.SOURCE_VALIDATION)
    second = await PrintingApplication._retry_candidate_or_create(  # type: ignore[arg-type]
        application,
        workflow.id,
        "candidate-2",
        ["file-2"],
        CandidateRejectedError(
            "model is too large",
            stage="artifact_validation",
            diagnostics={"file-2": "layout exceeds build volume"},
        ),
    )
    assert second.candidate_id == "candidate-3"
    await repository.transition(workflow.id, WorkflowState.SOURCE_VALIDATION)
    fallback = await PrintingApplication._retry_candidate_or_create(  # type: ignore[arg-type]
        application,
        workflow.id,
        "candidate-3",
        ["file-3"],
        CandidateRejectedError(
            "model is invalid",
            stage="source_validation",
            diagnostics={"file-3": "mesh is not watertight"},
        ),
    )

    assert fallback.decision == ModelDecision.CREATE
    assert len(discovery.attempts) == 2
    assert await repository.list_rejected_candidate_ids(workflow.id) == {
        "candidate-1",
        "candidate-2",
        "candidate-3",
    }
    assert await repository.count_rejected_candidates(workflow.id) == 3
    assert (await repository.get_workflow(workflow.id)).state == WorkflowState.SELECTING


def test_artifact_store_preserves_legacy_root_file_aliases(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    workflow_id = str(uuid4())
    directory = store.artifact_directory(workflow_id, 1)
    directory.mkdir(parents=True)
    (directory / "model.stl").write_bytes(b"legacy")
    (directory / "source.scad").write_text("cube(1);", encoding="utf-8")
    (directory / "manifest.json").write_text("{}", encoding="utf-8")

    assert store.resolve_artifact_file(workflow_id, 1, "model.stl").read_bytes() == b"legacy"
    assert (
        store.resolve_artifact_file(workflow_id, 1, "source.scad").read_text(encoding="utf-8")
        == "cube(1);"
    )


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
    assert artifact.schema_version == "2"
    assert artifact.project_path is not None and artifact.project_path.is_file()
    assert artifact.three_mf_path is not None and artifact.three_mf_path.is_file()
    assert artifact.project is not None and artifact.project.parts[0].id == "base_model"
    assert {item.role for item in artifact.files} >= {
        "part_project",
        "openscad_source",
        "part_stl",
        "combined_stl",
        "multipart_3mf",
    }
    store = ArtifactStore(settings.artifact_dir)
    assert store.resolve_artifact_file(workflow.id, artifact.version, "model.3mf").is_file()
    assert store.resolve_artifact_file(
        workflow.id,
        artifact.version,
        "outputs/parts/base_model.stl",
    ).is_file()
    with pytest.raises(PolicyViolationError):
        store.resolve_artifact_file(workflow.id, artifact.version, "../manifest.json")
    assert artifact.mesh.watertight
    assert artifact.manifest_digest

    application = SimpleNamespace(
        repository=repository,
        artifacts=ArtifactStore(settings.artifact_dir),
    )
    copied = await PrintingApplication.copy_workflow(application, workflow.id)  # type: ignore[arg-type]
    copied_artifact = await repository.get_artifact(copied.id, 1)
    copied_handoff = await repository.get_handoff(copied.id, 1)
    assert copied.id != workflow.id
    assert copied.state == WorkflowState.AWAITING_APPROVAL
    assert copied_artifact.model_digest == artifact.model_digest
    assert copied_artifact.manifest_digest != artifact.manifest_digest
    assert copied_artifact.source_path is not None and copied_artifact.source_path.is_file()
    assert copied_artifact.project_path is not None and copied_artifact.project_path.is_file()
    assert copied_artifact.project_path != artifact.project_path
    assert copied_artifact.three_mf_path is not None and copied_artifact.three_mf_path.is_file()
    assert copied_handoff.workflow_id == copied.id
    assert artifact.model_path.is_file()
    with pytest.raises(NotFoundError):
        await repository.get_approval(copied.id)

    await repository.approve_artifact(
        ArtifactApproval(
            workflow_id=copied.id,
            artifact_version=1,
            manifest_digest=copied_artifact.manifest_digest,
            approved_by="test",
        )
    )
    await repository.request_revision(
        RevisionRequest(
            workflow_id=copied.id,
            mode=RevisionMode.REFINE_CURRENT,
            feedback="Make the top rounder",
        )
    )
    revised = await repository.get_workflow(copied.id)
    revision_work = await repository.lease_next()
    assert revised.state == WorkflowState.REVISION_REQUESTED
    assert revision_work is not None and revision_work.kind == WorkKind.PREPARE
    await repository.complete_work(revision_work.id)
    assert (await repository.get_approval(copied.id)).artifact_version == 1

    class FailedRevisionModeling:
        async def revise(self, handoff, base_artifact, *args):
            del args
            await repository.save_source_attempt(
                handoff.workflow_id,
                handoff.version,
                1,
                "adopted",
                "d" * 64,
            )
            await repository.transition(handoff.workflow_id, WorkflowState.RENDERING)
            await repository.transition(handoff.workflow_id, WorkflowState.VALIDATING)
            candidate = base_artifact.model_copy(
                update={
                    "version": 2,
                    "manifest_digest": "d" * 64,
                }
            )
            await repository.save_artifact(candidate, activate=False)
            return candidate

    class FailedRevisionVerifier:
        async def verify(self, **kwargs):
            assert (
                await repository.get_workflow(kwargs["workflow_id"])
            ).active_artifact_version == 1
            return RevisionVerification(
                workflow_id=kwargs["workflow_id"],
                handoff_version=kwargs["handoff_version"],
                base_artifact_version=kwargs["base"].version,
                candidate_artifact_version=kwargs["candidate"].version,
                feedback=kwargs["feedback"],
                verdict="failed",
                repairable=False,
                checks=[
                    RevisionVerificationCheck(
                        id="semantic_intent",
                        passed=False,
                        repairable=False,
                        message="Requested change is absent from packaged outputs",
                    )
                ],
                rationale="The edit was not represented.",
            )

    rolled_back = await PrintingApplication._refine_current(  # type: ignore[arg-type]
        SimpleNamespace(
            repository=repository,
            modeling=FailedRevisionModeling(),
            revision_verifier=FailedRevisionVerifier(),
            settings=settings,
        ),
        copied.id,
        "Make the top rounder",
        None,
        1,
        1,
        WorkflowState.APPROVED,
    )
    restored_copy = await repository.get_workflow(copied.id)
    assert rolled_back.version == 1
    assert restored_copy.state == WorkflowState.APPROVED
    assert restored_copy.active_artifact_version == 1
    assert restored_copy.active_handoff_version == 1
    assert (await repository.get_approval(copied.id)).artifact_version == 1
    assert (await repository.get_active_revision_failure(copied.id)) is not None

    await repository.approve_artifact(
        ArtifactApproval(
            workflow_id=workflow.id,
            artifact_version=artifact.version,
            manifest_digest=artifact.manifest_digest,
            approved_by="test",
        )
    )
    approved = await repository.get_workflow(workflow.id)
    assert await repository.lease_next() is None

    await repository.enqueue_print_submission(workflow.id)
    with pytest.raises(ConflictError):
        await repository.enqueue_print_submission(workflow.id)
    submission = await repository.lease_next()
    submitting = await repository.get_workflow(workflow.id)
    assert approved.state == WorkflowState.APPROVED
    assert submitting.state == WorkflowState.SUBMITTING
    assert submission is not None and submission.kind == WorkKind.SUBMIT
    job = PrintJob(
        workflow_id=workflow.id,
        printer_name="simulator",
        external_id="simulated-job",
        idempotency_key="submission-key",
        status=PrintJobStatus.QUEUED,
        artifact_version=artifact.version,
    )
    await repository.finalize_print_submission(job)
    queued = await repository.get_workflow(workflow.id)
    assert queued.state == WorkflowState.QUEUED
    assert (await repository.get_latest_job(workflow.id)) == job
    await PrintingApplication.submit_print(  # type: ignore[arg-type]
        SimpleNamespace(repository=repository),
        workflow.id,
    )
    listed = await repository.list_workflows()
    assert {item.id for item in listed} == {workflow.id, copied.id}


async def test_infrastructure_failure_does_not_consume_generation_budget(
    settings: Settings,
    repository: WorkflowRepository,
) -> None:
    workflow = await repository.create_workflow("Create a cube", "simulator")
    pending = await repository.lease_next()
    assert pending is not None
    await repository.complete_work(pending.id)
    plan = ModelPlan(search_query="cube", geometry_summary="A cube")
    await repository.transition(workflow.id, WorkflowState.PLANNING)
    await repository.save_plan(workflow.id, plan)
    await repository.transition(workflow.id, WorkflowState.DISCOVERING)
    await repository.transition(workflow.id, WorkflowState.SELECTING)
    handoff = ModelingHandoff(
        workflow_id=workflow.id,
        version=1,
        requirement=workflow.requirement,
        model_plan=plan,
        decision=ModelDecision.CREATE,
        target_printer=PrinterCapabilitySummary(
            name="simulator",
            build_volume=Dimensions(width_mm=220, depth_mm=220, height_mm=250),
            accepted_formats={"stl"},
        ),
        discovery_rationale="Create a cube",
    ).with_digest()
    await repository.save_handoff(handoff)
    await repository.transition(workflow.id, WorkflowState.HANDOFF_READY)
    await repository.transition(workflow.id, WorkflowState.GENERATING)
    pipeline = ModelPipeline(
        settings,
        repository,
        ArtifactStore(settings.artifact_dir),
        UnavailableRenderer(),  # type: ignore[arg-type]
        MeshInspector(),
    )

    with pytest.raises(ExternalServiceError, match="unavailable"):
        await pipeline.adopt_source(handoff, "cube(10);")

    assert await repository.count_source_attempts(workflow.id, handoff.version) == 0
    assert (await repository.get_workflow(workflow.id)).state == WorkflowState.RENDERING


async def test_source_set_publishes_separate_parts_instances_and_package(
    settings: Settings,
    repository: WorkflowRepository,
    tmp_path: Path,
) -> None:
    workflow = await repository.create_workflow("Create a red truck with blue wheels", "simulator")
    pending = await repository.lease_next()
    assert pending is not None
    await repository.complete_work(pending.id)
    for state in (
        WorkflowState.PLANNING,
        WorkflowState.DISCOVERING,
        WorkflowState.SELECTING,
        WorkflowState.SOURCE_VALIDATION,
        WorkflowState.VALIDATING,
    ):
        await repository.transition(workflow.id, state)
    plan = ModelPlan(
        search_query="truck",
        geometry_summary="A body, four wheels, and two axles",
    )
    await repository.save_plan(workflow.id, plan)
    source_specs = [
        (
            "body-file",
            "truck.stl",
            "body",
            "Body",
            1,
            "#ff0000",
            trimesh.creation.box((40, 20, 12)),
        ),
        (
            "wheel-file",
            "wheel.stl",
            "wheel",
            "Wheel",
            4,
            "#0000ff",
            trimesh.creation.cylinder(radius=5, height=4),
        ),
        (
            "axle-file",
            "axle.stl",
            "axle",
            "Axle",
            2,
            "#b7c4d4",
            trimesh.creation.cylinder(radius=1.5, height=25),
        ),
    ]
    selected = []
    classification = []
    inspector = MeshInspector()
    for file_id, filename, part_id, part_name, quantity, color, mesh in source_specs:
        path = tmp_path / filename
        mesh.export(path)
        selection = SelectedCandidateFile(
            file_id=file_id,
            role=SelectedFileRole.UNIQUE_PART,
            part_id=part_id,
            part_name=part_name,
            quantity=quantity,
            color=color,
            rationale="Required by publisher instructions",
            confidence=0.95,
        )
        candidate_file = CandidateFile(id=file_id, name=filename, format="stl")
        selected.append((selection, candidate_file, path, await inspector.inspect(path)))
        classification.append(selection)
    pipeline = ModelPipeline(
        settings,
        repository,
        ArtifactStore(settings.artifact_dir),
        BoxRenderer(),  # type: ignore[arg-type]
        inspector,
    )

    artifact = await pipeline.adopt_existing_set(
        workflow.id,
        selected,
        ArtifactProvenance(
            kind="catalog",
            candidate_id="truck",
            source_url="https://example.test/truck",
            creator="maker",
            license="CC BY",
        ),
        Dimensions(width_mm=220, depth_mm=220, height_mm=250),
        shared_scale=1,
        description="Print four wheels, two axles, and one frame. Assemble wheels and axles.",
        classification=classification,
    )

    assert artifact.project is not None
    assert {part.id for part in artifact.project.parts} == {"body", "wheel", "axle"}
    assert len(artifact.project.instances) == 7
    assert artifact.project.assembly_status == "not_provided"
    assert {item.role for item in artifact.files} >= {"part_stl", "multipart_3mf"}
    assert "combined_stl" not in {item.role for item in artifact.files}
    assert artifact.three_mf_path is not None and artifact.three_mf_path.is_file()
    store = ArtifactStore(settings.artifact_dir)
    with ThreadPoolExecutor(max_workers=4) as executor:
        bundles = list(
            executor.map(
                lambda _: store.build_artifact_bundle(workflow.id, artifact.version),
                range(4),
            )
        )
    assert len({path.read_bytes() for path in bundles}) == 1
    first_bundle = bundles[0]
    first_bytes = first_bundle.read_bytes()
    assert store.build_artifact_bundle(workflow.id, artifact.version).read_bytes() == first_bytes
    with zipfile.ZipFile(first_bundle) as archive:
        names = set(archive.namelist())
        assert {
            "manifest.json",
            "README.txt",
            "package-index.json",
            "outputs/model.3mf",
            "outputs/parts/body.stl",
            "outputs/parts/wheel.stl",
            "outputs/parts/axle.stl",
        } <= names
        assert "not physical assembly" in archive.read("README.txt").decode().casefold()

    copied = await PrintingApplication.copy_workflow(  # type: ignore[arg-type]
        SimpleNamespace(
            repository=repository,
            artifacts=ArtifactStore(settings.artifact_dir),
        ),
        workflow.id,
    )
    copied_artifact = await repository.get_artifact(copied.id, 1)
    assert copied_artifact.model_path.is_file()
    assert copied_artifact.model_path.name == "body.stl"
    assert copied_artifact.three_mf_path is not None
    assert copied_artifact.three_mf_path.is_file()

    adapter = SimulatedPrinterAdapter(tmp_path / "spool")
    print_job = await adapter.submit(
        workflow.id,
        artifact,
        PrintSettings(),
        "multipart-layout",
    )
    submitted_layout = tmp_path / "spool" / print_job.external_id / "model.stl"
    assert submitted_layout.is_file()
    submitted_mesh = trimesh.load_mesh(submitted_layout, force="mesh")
    assert len(submitted_mesh.split(only_watertight=False)) == 7

    await repository.request_revision(
        RevisionRequest(
            workflow_id=workflow.id,
            mode=RevisionMode.REFINE_CURRENT,
            feedback="Make the wheels and axles three times bigger",
        )
    )
    handoff = ModelingHandoff(
        workflow_id=workflow.id,
        version=1,
        requirement=workflow.requirement,
        model_plan=plan,
        decision=ModelDecision.MODIFY,
        required_changes=["Make the wheels and axles three times bigger"],
        selected_source=SelectedSourceSummary(
            filename="wheel.stl",
            candidate_id="truck",
            file_id="wheel",
            title="Wheel",
            creator="maker",
            license="CC BY",
            attribution_url="https://example.test/truck",
            source_digest=sha256_file(
                artifact.model_path.parent / "wheel.stl"
            ),
            mesh=artifact.part_meshes["wheel"],
        ),
        target_printer=PrinterCapabilitySummary(
            name="simulator",
            build_volume=Dimensions(width_mm=220, depth_mm=220, height_mm=250),
            accepted_formats={"stl"},
        ),
        discovery_rationale="Revise the affected source-set parts",
    ).with_digest()
    await repository.save_handoff(handoff)
    await repository.transition(workflow.id, WorkflowState.HANDOFF_READY)
    await repository.transition(workflow.id, WorkflowState.GENERATING)
    revision_pipeline = ModelPipeline(
        settings,
        repository,
        ArtifactStore(settings.artifact_dir),
        PartRenderer(),  # type: ignore[arg-type]
        inspector,
    )
    unchanged_body_digest = sha256_file(artifact.model_path.parent / "body.stl")
    original_main_source = artifact.source_path.read_text(encoding="utf-8")
    original_axle_transform = next(
        instance.transform
        for instance in artifact.project.instances
        if instance.part_id == "axle"
    )

    revised = await revision_pipeline.adopt_part_revisions(
        handoff,
        artifact,
        [
            ("wheel", 'import("source.stl");', "Scale every wheel uniformly by three"),
            ("axle", 'import("source.stl");', "Scale every axle uniformly by three"),
        ],
        feedback="Make the wheels and axles three times bigger",
        rationale="The request explicitly names both wheel and axle parts.",
        allowed_part_ids=None,
    )

    assert revised.version == 2
    assert revised.project is not None
    assert len(revised.project.instances) == 7
    assert next(part for part in revised.project.parts if part.id == "wheel").geometry_kind == (
        PartGeometryKind.DERIVED_MESH
    )
    assert next(part for part in revised.project.parts if part.id == "axle").geometry_kind == (
        PartGeometryKind.DERIVED_MESH
    )
    assert sha256_file(revised.model_path.parent / "body.stl") == unchanged_body_digest
    assert sha256_file(revised.model_path.parent / "wheel.stl") != sha256_file(
        artifact.model_path.parent / "wheel.stl"
    )
    assert next(
        instance.transform
        for instance in revised.project.instances
        if instance.part_id == "axle"
    ) != original_axle_transform
    assert revised.source_path.read_text(encoding="utf-8") != original_main_source
    assert revised.three_mf_path is not None and revised.three_mf_path.is_file()
    assert revised.revision is not None
    assert revised.revision.affected_part_ids == ["wheel", "axle"]
    assert revised.revision.allowed_part_ids is None
    assert revised.part_meshes["wheel"].dimensions.width_mm == pytest.approx(
        artifact.part_meshes["wheel"].dimensions.width_mm * 3
    )
    assert revised.part_meshes["axle"].dimensions.height_mm == pytest.approx(
        artifact.part_meshes["axle"].dimensions.height_mm * 3
    )
    manifest = json.loads(
        (revised.project_path.parent.parent / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["revision"]["affected_part_ids"] == ["wheel", "axle"]
    with pytest.raises(ValidationError, match="outside the allowed edit scope"):
        await revision_pipeline.adopt_part_revisions(
            handoff,
            revised,
            [("axle", 'import("source.stl");', "Change the axle")],
            feedback="Change the axle",
            rationale="Axle requested",
            allowed_part_ids=["wheel"],
        )

    await repository.request_revision(
        RevisionRequest(
            workflow_id=workflow.id,
            mode=RevisionMode.REFINE_CURRENT,
            feedback="Change the wheels and axles again",
            allowed_part_ids=["wheel", "axle"],
        )
    )
    failed_handoff = handoff.model_copy(
        update={
            "version": 2,
            "required_changes": [
                *handoff.required_changes,
                "Change the wheels and axles again",
            ],
            "digest": None,
        }
    ).with_digest()
    await repository.save_handoff(failed_handoff)
    await repository.transition(workflow.id, WorkflowState.HANDOFF_READY)
    await repository.transition(workflow.id, WorkflowState.GENERATING)
    failing_pipeline = ModelPipeline(
        settings,
        repository,
        ArtifactStore(settings.artifact_dir),
        FailingPartRenderer(),  # type: ignore[arg-type]
        inspector,
    )

    with pytest.raises(ValidationError, match="fixture axle render failed"):
        await failing_pipeline.adopt_part_revisions(
            failed_handoff,
            revised,
            [
                ("wheel", 'import("source.stl");', "Change wheel"),
                ("axle", 'import("source.stl");', "Change axle"),
            ],
            feedback="Change the wheels and axles again",
            rationale="Both named parts are affected.",
            allowed_part_ids=["wheel", "axle"],
        )

    assert (await repository.get_workflow(workflow.id)).active_artifact_version == 2
    with pytest.raises(NotFoundError):
        await repository.get_artifact(workflow.id, 3)


def test_three_mf_round_trip_preserves_named_parts_instances_and_colors(tmp_path: Path) -> None:
    body_path = tmp_path / "body.stl"
    wheel_path = tmp_path / "wheel.stl"
    trimesh.creation.box(extents=(20, 10, 5)).export(body_path)
    trimesh.creation.cylinder(radius=2, height=2).export(wheel_path)
    project = PartProject(
        parts=[
            PartDefinition(
                id="body",
                name="Truck body",
                geometry_kind=PartGeometryKind.PARAMETRIC,
                material_id="body_color",
                annotation_origin=AnnotationOrigin.SOURCE_ANNOTATION,
                confidence=1,
            ),
            PartDefinition(
                id="wheel",
                name="Wheel",
                geometry_kind=PartGeometryKind.PARAMETRIC,
                material_id="wheel_color",
                annotation_origin=AnnotationOrigin.SOURCE_ANNOTATION,
                confidence=1,
            ),
        ],
        instances=[
            PartInstance(
                id="body_1",
                part_id="body",
                name="Truck body",
                annotation_origin=AnnotationOrigin.SOURCE_ANNOTATION,
                confidence=1,
            ),
            PartInstance(
                id="wheel_1",
                part_id="wheel",
                name="Front wheel",
                transform=(1, 0, 0, 8, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1),
                annotation_origin=AnnotationOrigin.SOURCE_ANNOTATION,
                confidence=1,
            ),
            PartInstance(
                id="wheel_2",
                part_id="wheel",
                name="Rear wheel",
                transform=(1, 0, 0, -8, 0, 1, 0, 0, 0, 1, 0, 0, 0, 0, 0, 1),
                annotation_origin=AnnotationOrigin.SOURCE_ANNOTATION,
                confidence=1,
            ),
        ],
        materials=[
            MaterialDefinition(id="body_color", name="Body", color="#ff0000"),
            MaterialDefinition(id="wheel_color", name="Wheel", color="#222222"),
        ],
    )
    path = tmp_path / "truck.3mf"

    service = ThreeMFService()
    service.write(project, {"body": body_path, "wheel": wheel_path}, path)
    imported, meshes, combined = service.read(path)

    assert {part.name for part in imported.parts} == {"Truck body", "Wheel"}
    assert len(imported.instances) == 3
    assert {material.color for material in imported.materials} == {"#ff0000", "#222222"}
    assert set(meshes) == {"body", "wheel"}
    assert len(combined.faces) > 0
