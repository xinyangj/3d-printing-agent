from __future__ import annotations

import zipfile
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
    CandidateFile,
    Dimensions,
    MaterialDefinition,
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
    SelectedCandidateFile,
    SelectedFileRole,
    SelectedSourceSummary,
    WorkflowState,
    WorkKind,
)
from printing_agent.errors import ConflictError, NotFoundError, PolicyViolationError
from printing_agent.modeling import MeshInspector, ModelPipeline, OpenScadSourcePolicy
from printing_agent.multipart import ThreeMFService
from printing_agent.printers import SimulatedPrinterAdapter
from printing_agent.repositories import WorkflowRepository


class BoxRenderer:
    async def render(self, source_path: Path, output_path: Path) -> str:
        trimesh.creation.box(extents=(20, 20, 20)).export(output_path)
        return "rendered fixture"


class PartRenderer:
    async def render(self, source_path: Path, output_path: Path) -> str:
        trimesh.creation.box(extents=(6, 6, 4)).export(output_path)
        return "rendered revised part"


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
    with pytest.raises(NotFoundError):
        await repository.get_approval(copied.id)

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
    first_bundle = store.build_artifact_bundle(workflow.id, artifact.version)
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
            feedback="Make the wheel square",
            part_id="wheel",
        )
    )
    handoff = ModelingHandoff(
        workflow_id=workflow.id,
        version=1,
        requirement=workflow.requirement,
        model_plan=plan,
        decision=ModelDecision.MODIFY,
        required_changes=["Make the wheel square"],
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
        discovery_rationale="Revise only the selected wheel part",
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
    original_axle_transform = next(
        instance.transform
        for instance in artifact.project.instances
        if instance.part_id == "axle"
    )

    revised = await revision_pipeline.adopt_part_revision(
        handoff,
        artifact,
        "wheel",
        'import("source.stl");',
    )

    assert revised.version == 2
    assert revised.project is not None
    assert len(revised.project.instances) == 7
    assert next(part for part in revised.project.parts if part.id == "wheel").geometry_kind == (
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
    assert revised.three_mf_path is not None and revised.three_mf_path.is_file()


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
