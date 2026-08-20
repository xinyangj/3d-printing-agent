from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest
import trimesh

from printing_agent.application import PrintingApplication
from printing_agent.artifact_store import ArtifactStore, sha256_file
from printing_agent.domain import (
    AnnotationOrigin,
    ArtifactApproval,
    ArtifactProvenance,
    MaterialDefinition,
    ModelArtifact,
    ModelPlan,
    PartDefinition,
    PartGeometryKind,
    PartInstance,
    PartProject,
    PrintWorkflow,
    WorkflowState,
)
from printing_agent.errors import ConflictError, NotFoundError, ValidationError
from printing_agent.fabrication import (
    HandoffStatus,
    JobOverrides,
    MaterialAssignment,
    MaterialDefinitionRevision,
    MaterialDefinitionSpec,
    PartMaterialAssignment,
    PartMaterialRequest,
    PhysicalSpool,
    SlicedArtifact,
    SliceJob,
    SliceJobStatus,
    SlotPolicy,
    SpoolReservation,
    SpoolStatus,
    SubmissionHandoff,
    WorkflowPrinterSnapshot,
    ciede2000,
    resolve_slot_policy,
)
from printing_agent.fabrication_drivers import (
    BambuConnectCloudDriver,
    BambuStudioCliDriver,
    SliceRequest,
    SlicerRegistry,
)
from printing_agent.fabrication_profiles import (
    built_in_h2d_profile,
    built_in_simulator_profile,
    resolve_profile_dependency_digests,
    resolve_slicer_profile_payload,
)
from printing_agent.material_assignment import (
    AgentMaterialAssignments,
    AgentPartAssignment,
    MaterialAssignmentService,
)
from printing_agent.modeling import MeshInspector
from printing_agent.multipart import single_part_project, write_project_artifact
from printing_agent.printers import PrinterRegistry, ProfilePrinterAdapter
from printing_agent.repositories import WorkflowRepository


class DeterministicAssignmentAgent:
    async def assign(
        self,
        workflow_id: str,
        requests,
        candidates,
    ) -> AgentMaterialAssignments:
        del workflow_id
        return AgentMaterialAssignments(
            assignments=[
                AgentPartAssignment(
                    part_id=request.part_id,
                    spool_id=candidates[request.part_id][0].spool_id,
                    toolhead_id=sorted(
                        candidates[request.part_id][0].toolhead_ids
                    )[0],
                    confidence=0.95,
                    rationale="Use the closest compatible configured spool.",
                )
                for request in requests
            ],
            rationale="Closest compatible colors selected.",
        )


class ReadySlicer:
    id = "bambu_studio_cli"

    async def validate_profile(self, profile) -> None:
        del profile

    async def slice(self, request):
        raise AssertionError(f"Unexpected slice request: {request}")

    async def cancel(self, slice_job_id: str) -> None:
        del slice_job_id


def _material(
    material_id: str,
    color: str,
    *,
    family: str = "pla",
) -> MaterialDefinitionRevision:
    return MaterialDefinitionRevision(
        material_id=material_id,
        revision=1,
        spec=MaterialDefinitionSpec(
            display_name=material_id,
            family=family,
            nominal_color=color,
            nozzle_temperature_c=(190, 230),
            bed_temperature_c=(35, 65),
            slicer_filament_profile_id=f"{material_id}-profile",
        ),
    ).with_digest()


async def _artifact(
    tmp_path: Path,
    workflow_id: str = "workflow",
) -> ModelArtifact:
    path = tmp_path / "model.stl"
    trimesh.creation.box(extents=(20, 10, 5)).export(path)
    mesh = await MeshInspector().inspect(path)
    project = PartProject(
        parts=[
            PartDefinition(
                id="body",
                name="Body",
                geometry_kind=PartGeometryKind.IMPORTED_MESH,
                material_id="red",
                annotation_origin=AnnotationOrigin.AGENT_INFERENCE,
                confidence=1,
            )
        ],
        instances=[
            PartInstance(
                id="body_1",
                part_id="body",
                name="Body",
                annotation_origin=AnnotationOrigin.AGENT_INFERENCE,
                confidence=1,
            )
        ],
        materials=[
            MaterialDefinition(id="red", name="Red", color="#FF0000")
        ],
    )
    return ModelArtifact(
        workflow_id=workflow_id,
        version=1,
        model_path=path,
        model_digest=sha256_file(path),
        manifest_digest="a" * 64,
        mesh=mesh,
        project=project,
        provenance=ArtifactProvenance(kind="generated"),
    )


async def _stored_source_artifact(
    repository: WorkflowRepository,
    store: ArtifactStore,
    tmp_path: Path,
    *,
    schema_version: str,
    approved: bool,
) -> tuple[PrintWorkflow, ModelArtifact]:
    profile = built_in_simulator_profile()
    try:
        await repository.get_printer_profile(profile.profile_id, profile.revision)
    except NotFoundError:
        await repository.save_printer_profile(profile)
    workflow = await repository.create_workflow("Create a printable cube", "simulator")
    pending = await repository.lease_next()
    assert pending is not None
    await repository.complete_work(pending.id)
    await repository.transition(workflow.id, WorkflowState.PLANNING)
    await repository.save_plan(
        workflow.id,
        ModelPlan(
            search_query="cube",
            geometry_summary="A printable cube",
            target_dimensions=profile.spec.build_volume,
        ),
    )
    for state in (
        WorkflowState.DISCOVERING,
        WorkflowState.SELECTING,
        WorkflowState.VALIDATING,
    ):
        await repository.transition(workflow.id, state)
    source_dir = tmp_path / workflow.id
    source_dir.mkdir()
    base = await _artifact(source_dir, workflow.id)
    if schema_version == "2":
        staging = store.workflow_root(workflow.id) / "staging" / "artifact-1"
        project = single_part_project(
            source_digest=base.model_digest,
            imported=True,
            source_filename=base.model_path.name,
        )
        manifest, files = write_project_artifact(
            directory=staging,
            workflow_id=workflow.id,
            version=1,
            project=project,
            source_path=base.model_path,
            model_path=base.model_path,
            mesh=base.mesh,
            provenance=base.provenance,
            handoff_digest=None,
            diagnostics=None,
            imported=True,
        )
        adopted = store.adopt_project(workflow.id, 1, staging)
        artifact = ModelArtifact(
            schema_version="2",
            workflow_id=workflow.id,
            version=1,
            source_path=adopted / "project" / "main.scad",
            model_path=adopted / "outputs" / "parts" / "base_model.stl",
            project_path=adopted / "project" / "project.json",
            three_mf_path=adopted / "outputs" / "model.3mf",
            source_digest=base.model_digest,
            model_digest=str(manifest["model_digest"]),
            project_digest=str(manifest["project_digest"]),
            three_mf_digest=str(manifest["three_mf_digest"]),
            manifest_digest=str(manifest["manifest_digest"]),
            mesh=base.mesh,
            project=project,
            part_meshes={"base_model": base.mesh},
            files=files,
            provenance=base.provenance,
        )
    else:
        artifact = base
    await repository.save_artifact(artifact)
    await repository.transition(workflow.id, WorkflowState.AWAITING_APPROVAL)
    if approved:
        await repository.approve_artifact(
            ArtifactApproval(
                workflow_id=workflow.id,
                artifact_version=artifact.version,
                manifest_digest=artifact.manifest_digest,
                approved_by="test",
            )
        )
    return await repository.get_workflow(workflow.id), artifact


async def _copy_application(
    repository: WorkflowRepository,
    store: ArtifactStore,
) -> PrintingApplication:
    h2d = built_in_h2d_profile()
    try:
        await repository.get_printer_profile(h2d.profile_id, h2d.revision)
    except NotFoundError:
        await repository.save_printer_profile(h2d)
    printers = PrinterRegistry()
    printers.register(ProfilePrinterAdapter(h2d))
    slicers = SlicerRegistry()
    slicers.register(ReadySlicer())
    application = object.__new__(PrintingApplication)
    application.repository = repository
    application.artifacts = store
    application.printers = printers
    application.slicers = slicers
    return application


def test_ciede2000_matches_reference_pair() -> None:
    assert ciede2000(
        (50.0, 2.6772, -79.7751),
        (50.0, 0.0, -82.7485),
    ) == pytest.approx(2.0425, abs=0.0001)


def test_job_policy_cannot_reenable_profile_forbidden_slot() -> None:
    profile = built_in_h2d_profile().spec.model_copy(
        update={
            "default_slot_policy": SlotPolicy(
                forbidden_slot_ids={"ams1_1"}
            )
        }
    )

    resolved = resolve_slot_policy(
        profile,
        JobOverrides(
            allowed_slot_ids={"ams1_1", "ams1_2"},
            forbidden_slot_ids={"ams1_3"},
        ),
    )

    assert resolved.forbidden_slot_ids == {"ams1_1", "ams1_3"}
    assert resolved.allowed_slot_ids == {"ams1_2"}


async def test_fabrication_copy_carries_exact_schema_v2_approval(
    repository: WorkflowRepository,
    tmp_path: Path,
) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    source, source_artifact = await _stored_source_artifact(
        repository,
        store,
        tmp_path,
        schema_version="2",
        approved=True,
    )
    application = await _copy_application(repository, store)

    copied = await application.copy_workflow(
        source.id,
        target_printer_name="bambu-h2d",
        overrides=JobOverrides(
            plate_id="smooth_pei",
            forbidden_slot_ids={"ams1_4"},
        ),
    )

    copied_artifact = await repository.get_artifact(copied.id, 1)
    copied_approval = await repository.get_approval(copied.id)
    copied_snapshot = await repository.get_workflow_printer_snapshot(copied.id)
    assert copied.state == WorkflowState.APPROVED
    assert copied.printer_name == "bambu-h2d"
    assert copied_approval.manifest_digest == copied_artifact.manifest_digest
    assert copied_approval.approved_by.startswith(f"carried-from:{source.id}:")
    assert copied_snapshot.overrides.plate_id == "smooth_pei"
    assert copied_snapshot.resolved_slot_policy.forbidden_slot_ids == {"ams1_4"}
    assert copied_artifact.manifest_digest != source_artifact.manifest_digest
    assert copied_artifact.model_digest == source_artifact.model_digest
    assert copied_artifact.three_mf_digest == source_artifact.three_mf_digest
    assert copied_artifact.files == source_artifact.files


async def test_fabrication_copy_upgrades_legacy_artifact_without_approval(
    repository: WorkflowRepository,
    tmp_path: Path,
) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    source, _ = await _stored_source_artifact(
        repository,
        store,
        tmp_path,
        schema_version="1",
        approved=True,
    )
    application = await _copy_application(repository, store)

    copied = await application.copy_workflow(
        source.id,
        target_printer_name="bambu-h2d",
    )

    copied_artifact = await repository.get_artifact(copied.id, 1)
    assert copied.state == WorkflowState.AWAITING_APPROVAL
    assert copied_artifact.schema_version == "2"
    assert copied_artifact.three_mf_path is not None
    assert copied_artifact.three_mf_path.is_file()
    with pytest.raises(NotFoundError, match="not been approved"):
        await repository.get_approval(copied.id)


async def test_forbidden_spools_are_removed_before_assignment(
    tmp_path: Path,
) -> None:
    artifact = await _artifact(tmp_path)
    profile = built_in_h2d_profile()
    red = _material("red-pla", "#FF0000")
    orange = _material("orange-pla", "#FF4400")
    spools = [
        PhysicalSpool(
            id="forbidden-red",
            material_id=red.material_id,
            material_revision=red.revision,
            material_digest=red.digest or "0" * 64,
            initial_weight_g=1000,
            remaining_weight_g=800,
            status=SpoolStatus.LOADED,
            printer_profile_id=profile.profile_id,
            slot_id="ams1_1",
        ),
        PhysicalSpool(
            id="allowed-orange",
            material_id=orange.material_id,
            material_revision=orange.revision,
            material_digest=orange.digest or "0" * 64,
            initial_weight_g=1000,
            remaining_weight_g=800,
            status=SpoolStatus.LOADED,
            printer_profile_id=profile.profile_id,
            slot_id="ams1_2",
        ),
    ]
    service = MaterialAssignmentService(DeterministicAssignmentAgent())  # type: ignore[arg-type]
    policy = SlotPolicy(forbidden_slot_ids={"ams1_1"})

    assignment = await service.propose(
        workflow_id="workflow",
        artifact=artifact,
        profile=profile,
        printer_snapshot_digest="b" * 64,
        policy=policy,
        overrides=JobOverrides(),
        spools=spools,
        materials={
            (red.material_id, red.revision): red,
            (orange.material_id, orange.revision): orange,
        },
        maximum_color_distance=5,
        default_material_family="pla",
    )

    assert assignment.assignments[0].spool_id == "allowed-orange"
    assert assignment.requires_confirmation is True


async def test_no_usable_material_blocks_assignment(tmp_path: Path) -> None:
    artifact = await _artifact(tmp_path)
    profile = built_in_h2d_profile()
    service = MaterialAssignmentService(DeterministicAssignmentAgent())  # type: ignore[arg-type]

    with pytest.raises(ValidationError, match="No usable configured material"):
        await service.propose(
            workflow_id="workflow",
            artifact=artifact,
            profile=profile,
            printer_snapshot_digest="b" * 64,
            policy=SlotPolicy(forbidden_slot_ids={"ams1_1"}),
            overrides=JobOverrides(),
            spools=[],
            materials={},
            maximum_color_distance=5,
            default_material_family="pla",
        )


def test_bambu_cli_arguments_preserve_untrusted_paths_as_single_arguments(
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "input & model.3mf"
    output_path = tmp_path / "output.gcode.3mf"
    machine = tmp_path / "machine profile.json"
    process = tmp_path / "process profile.json"
    filament = tmp_path / "red;profile.json"

    args = BambuStudioCliDriver.build_arguments(
        input_path=input_path,
        output_path=output_path,
        machine_path=machine,
        process_path=process,
        filament_paths=[filament],
    )

    assert args[-1] == str(input_path)
    assert str(output_path) in args
    assert str(filament) in args
    assert "&" not in args


def test_sliced_3mf_requires_gcode_payload(tmp_path: Path) -> None:
    path = tmp_path / "job.gcode.3mf"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("Metadata/model_settings.config", "{}")

    with pytest.raises(ValidationError, match="no G-code"):
        BambuStudioCliDriver._validate_gcode_3mf(path)

    with zipfile.ZipFile(path, "a") as archive:
        archive.writestr("Metadata/plate_1.gcode", "G28\n")
    BambuStudioCliDriver._validate_gcode_3mf(path)


def test_profile_dependency_digest_includes_inherited_and_included_files(
    tmp_path: Path,
) -> None:
    profile_dir = tmp_path / "BBL" / "machine"
    profile_dir.mkdir(parents=True)
    other_dir = tmp_path / "Other" / "machine"
    other_dir.mkdir(parents=True)
    (profile_dir / "machine.json").write_text(
        '{"type":"machine","name":"machine","from":"system",'
        '"inherits":"base","include":["start-gcode"],"current":"1"}',
        encoding="utf-8",
    )
    (profile_dir / "base.json").write_text(
        '{"base":"1","overridden":"base"}',
        encoding="utf-8",
    )
    (profile_dir / "start-gcode.json").write_text(
        '{"included":"1","overridden":"include"}',
        encoding="utf-8",
    )
    (other_dir / "base.json").write_text("{}", encoding="utf-8")
    (other_dir / "start-gcode.json").write_text("{}", encoding="utf-8")
    full_dir = tmp_path / "BBL" / "machine_full"
    full_dir.mkdir()
    (full_dir / "resolved.json").write_text("{}", encoding="utf-8")
    (profile_dir / "resolved.json").write_text("{}", encoding="utf-8")

    digests = resolve_profile_dependency_digests(
        str(tmp_path),
        "machine",
        "machine",
    )

    assert set(digests) == {
        "machine:BBL/machine/machine.json",
        "machine:BBL/machine/base.json",
        "machine:BBL/machine/start-gcode.json",
    }
    assert set(
        resolve_profile_dependency_digests(
            str(tmp_path),
            "resolved",
            "machine",
        )
    ) == {"machine:BBL/machine_full/resolved.json"}
    assert resolve_slicer_profile_payload(
        str(tmp_path),
        "machine",
        "machine",
    ) == {
        "type": "machine",
        "name": "machine",
        "from": "system",
        "base": "1",
        "included": "1",
        "overridden": "include",
        "current": "1",
    }


async def test_bambu_native_settings_bind_single_right_tool_and_usage(
    tmp_path: Path,
) -> None:
    artifact = await _artifact(tmp_path)
    resource_root = tmp_path / "profiles"
    machine_dir = resource_root / "BBL" / "machine_full"
    process_dir = resource_root / "BBL" / "process_full"
    machine_dir.mkdir(parents=True)
    process_dir.mkdir(parents=True)
    machine_name = "Bambu Lab H2D 0.4 nozzle"
    process_name = "0.20mm Standard @BBL H2D"
    (machine_dir / f"{machine_name}.json").write_text(
        json.dumps(
            {
                "type": "machine",
                "name": machine_name,
                "from": "system",
            }
        ),
        encoding="utf-8",
    )
    (process_dir / f"{process_name}.json").write_text(
        json.dumps(
            {
                "type": "process",
                "name": process_name,
                "from": "system",
            }
        ),
        encoding="utf-8",
    )
    base_profile = built_in_h2d_profile()
    profile = base_profile.model_copy(
        update={
            "spec": base_profile.spec.model_copy(
                update={
                    "slicer": base_profile.spec.slicer.model_copy(
                        update={"resource_root": str(resource_root)}
                    )
                }
            ),
            "digest": None,
        }
    ).with_digest()
    snapshot = WorkflowPrinterSnapshot(
        workflow_id=artifact.workflow_id,
        profile_id=profile.profile_id,
        profile_revision=profile.revision,
        profile_digest=profile.digest or "0" * 64,
        profile=profile.spec,
        overrides=JobOverrides(plate_id="smooth_pei"),
        resolved_slot_policy=SlotPolicy(),
    ).with_digest()
    material = _material("red-pla", "#FF0000")
    part_assignment = PartMaterialAssignment(
        part_id="body",
        spool_id="spool-red",
        slot_id="ams1_1",
        toolhead_id="right",
        material_id=material.material_id,
        material_revision=material.revision,
        material_digest=material.digest or "0" * 64,
        slicer_filament_profile_id=material.spec.slicer_filament_profile_id,
        slicer_filament_profile_digest=material.spec.slicer_profile_digest,
        slicer_filament_dependency_digests={
            "filament:red.json": "f" * 64
        },
        color_distance=0,
        confidence=1,
        rationale="Exact configured red.",
    )
    assignment = MaterialAssignment(
        workflow_id=artifact.workflow_id,
        artifact_version=artifact.version,
        artifact_manifest_digest=artifact.manifest_digest,
        printer_snapshot_digest=snapshot.digest or "0" * 64,
        slot_policy_digest="c" * 64,
        requests=[
            PartMaterialRequest(
                part_id="body",
                part_name="Body",
                requested_color="#FF0000",
            )
        ],
        assignments=[part_assignment],
        requires_confirmation=False,
        confirmed_by="test",
        confirmed_at=artifact.created_at,
    ).with_digest()
    job = SliceJob(
        workflow_id=artifact.workflow_id,
        artifact_version=artifact.version,
        artifact_manifest_digest=artifact.manifest_digest,
        printer_snapshot_digest=snapshot.digest or "0" * 64,
        material_assignment_digest=assignment.digest or "0" * 64,
        slicer_driver_id="bambu_studio_cli",
        idempotency_key="d" * 64,
    )
    request = SliceRequest(
        job=job,
        artifact=artifact,
        printer=snapshot,
        material_assignment=assignment,
        workspace=tmp_path / "slice",
    )
    settings = BambuStudioCliDriver._resolved_job_settings(request)
    output_settings = {
        **settings,
        "printer_settings_id": machine_name,
        "print_settings_id": process_name,
    }
    model_settings = (
        '<?xml version="1.0"?><config><object id="1">'
        '<metadata key="name" value="body"/>'
        '<metadata key="extruder" value="1"/>'
        "</object></config>"
    )
    sliced_path = tmp_path / "native-map.gcode.3mf"
    with zipfile.ZipFile(sliced_path, "w") as archive:
        archive.writestr("Metadata/model_settings.config", model_settings)
        archive.writestr(
            "Metadata/project_settings.config",
            json.dumps(output_settings),
        )
        archive.writestr(
            "Metadata/plate_1.gcode",
            "; filament_map = 2\n"
            "; total filament weight [g] : 12.50\n",
        )

    assert settings["filament_map"] == ["2"]
    assert settings["filament_map_mode"] == "Manual"
    assert settings["curr_bed_type"] == "High Temp Plate"
    assert settings["nozzle_diameter"] == ["0.4", "0.4"]
    assert BambuStudioCliDriver._filament_usage_by_spool(
        sliced_path,
        request,
    ) == {"spool-red": 12.5}
    BambuStudioCliDriver._validate_sliced_assignment(sliced_path, request)

    with zipfile.ZipFile(sliced_path, "w") as archive:
        archive.writestr("Metadata/model_settings.config", model_settings)
        archive.writestr(
            "Metadata/project_settings.config",
            json.dumps(output_settings),
        )
        archive.writestr(
            "Metadata/plate_1.gcode",
            "; filament_map = 1\n"
            "; total filament weight [g] : 12.50\n",
        )
    with pytest.raises(ValidationError, match="physical toolhead map"):
        BambuStudioCliDriver._validate_sliced_assignment(sliced_path, request)

    source_process = tmp_path / "process.json"
    source_process.write_text(
        json.dumps(
            {
                "type": "process",
                "name": "H2D standard",
                "from": "system",
                "layer_height": "0.28",
            }
        ),
        encoding="utf-8",
    )
    effective_process = tmp_path / "effective-process.json"
    BambuStudioCliDriver._write_effective_process_profile(
        source_process,
        effective_process,
        settings,
    )
    effective_payload = json.loads(effective_process.read_text(encoding="utf-8"))
    assert effective_payload["type"] == "process"
    assert effective_payload["name"] == "H2D standard"
    assert effective_payload["from"] == "system"
    assert effective_payload["curr_bed_type"] == "High Temp Plate"
    assert effective_payload["filament_map"] == ["2"]


async def test_cancel_completed_slicer_marks_post_processing_cancelled() -> None:
    class CompletedProcess:
        returncode = 0
        killed = False

        def kill(self) -> None:
            self.killed = True

        async def wait(self) -> int:
            return 0

    driver = BambuStudioCliDriver()
    process = CompletedProcess()
    driver._processes["slice"] = process  # type: ignore[assignment]

    await driver.cancel("slice")

    assert not process.killed
    with pytest.raises(ConflictError, match="cancelled"):
        driver._raise_if_cancelled("slice")


async def test_bambu_connect_handoff_contains_no_credentials(
    tmp_path: Path,
) -> None:
    path = tmp_path / "job.gcode.3mf"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("Metadata/plate_1.gcode", "G28\n")
    sliced = SlicedArtifact(
        slice_job_id="slice",
        workflow_id="workflow",
        path=str(path),
        digest=sha256_file(path),
        size_bytes=path.stat().st_size,
        format="gcode.3mf",
        model_manifest_digest="b" * 64,
        printer_snapshot_digest="c" * 64,
        material_assignment_digest="d" * 64,
        slicer_driver_id="bambu_studio_cli",
        slicer_version="test",
        machine_profile_id="h2d",
        process_profile_id="standard",
        plate_count=1,
        manifest_digest="e" * 64,
    )
    driver = BambuConnectCloudDriver(
        tmp_path,
        require_registration=False,
    )

    handoff = await driver.prepare_handoff(
        "workflow",
        sliced,
        built_in_h2d_profile(),
    )

    assert handoff.launch_uri is not None
    assert handoff.launch_uri.startswith("bambu-connect://import-file?")
    assert "password" not in handoff.launch_uri.casefold()
    assert "token" not in handoff.launch_uri.casefold()


async def test_profile_snapshot_and_spool_slot_are_persisted_immutably(
    repository: WorkflowRepository,
) -> None:
    profile = built_in_h2d_profile()
    await repository.save_printer_profile(profile)
    workflow = await repository.create_workflow("Create a fixture", profile.profile_id)
    snapshot = WorkflowPrinterSnapshot(
        workflow_id=workflow.id,
        profile_id=profile.profile_id,
        profile_revision=profile.revision,
        profile_digest=profile.digest or "0" * 64,
        profile=profile.spec,
        resolved_slot_policy=SlotPolicy(forbidden_slot_ids={"ams1_4"}),
    ).with_digest()
    await repository.save_workflow_printer_snapshot(snapshot)
    material = _material("red-pla", "#FF0000")
    await repository.save_material_definition(material)
    first = PhysicalSpool(
        id="spool-1",
        material_id=material.material_id,
        material_revision=material.revision,
        material_digest=material.digest or "0" * 64,
        initial_weight_g=1000,
        remaining_weight_g=900,
        status=SpoolStatus.LOADED,
        printer_profile_id=profile.profile_id,
        slot_id="ams1_1",
    )
    await repository.save_spool(first)

    assert await repository.get_workflow_printer_snapshot(workflow.id) == snapshot
    assert (await repository.get_spool(first.id)).slot_id == "ams1_1"
    with pytest.raises(ConflictError, match="already contains"):
        await repository.save_spool(first.model_copy(update={"id": "spool-2"}))

    await repository.archive_printer_profile(profile.profile_id)
    with pytest.raises(NotFoundError, match="not found"):
        await repository.get_printer_profile(profile.profile_id)
    assert (
        await repository.get_printer_profile(profile.profile_id, profile.revision)
    ) == profile


async def test_slice_request_reserves_spools_atomically(
    repository: WorkflowRepository,
    tmp_path: Path,
) -> None:
    profile = built_in_h2d_profile()
    await repository.save_printer_profile(profile)
    workflow = await repository.create_workflow("Create a fixture", profile.profile_id)
    pending = await repository.lease_next()
    assert pending is not None
    await repository.complete_work(pending.id)
    for state in (
        WorkflowState.PLANNING,
        WorkflowState.DISCOVERING,
        WorkflowState.SELECTING,
        WorkflowState.VALIDATING,
    ):
        await repository.transition(workflow.id, state)
    artifact = await _artifact(tmp_path, workflow.id)
    await repository.save_artifact(artifact)
    await repository.transition(workflow.id, WorkflowState.AWAITING_APPROVAL)
    await repository.approve_artifact(
        ArtifactApproval(
            workflow_id=workflow.id,
            artifact_version=artifact.version,
            manifest_digest=artifact.manifest_digest,
            approved_by="test",
        )
    )
    snapshot = WorkflowPrinterSnapshot(
        workflow_id=workflow.id,
        profile_id=profile.profile_id,
        profile_revision=profile.revision,
        profile_digest=profile.digest or "0" * 64,
        profile=profile.spec,
        resolved_slot_policy=SlotPolicy(),
    ).with_digest()
    await repository.save_workflow_printer_snapshot(snapshot)
    material = _material("red-pla", "#FF0000")
    await repository.save_material_definition(material)
    spool = PhysicalSpool(
        id="spool-red",
        material_id=material.material_id,
        material_revision=material.revision,
        material_digest=material.digest or "0" * 64,
        initial_weight_g=1000,
        remaining_weight_g=100,
        status=SpoolStatus.LOADED,
        printer_profile_id=profile.profile_id,
        slot_id="ams1_1",
    )
    await repository.save_spool(spool)
    assignment = MaterialAssignment(
        workflow_id=workflow.id,
        artifact_version=artifact.version,
        artifact_manifest_digest=artifact.manifest_digest,
        printer_snapshot_digest=snapshot.digest or "0" * 64,
        slot_policy_digest="c" * 64,
        requests=[
            PartMaterialRequest(
                part_id="body",
                part_name="Body",
                requested_color="#FF0000",
                estimated_weight_g=40,
            )
        ],
        assignments=[
            PartMaterialAssignment(
                part_id="body",
                spool_id=spool.id,
                slot_id="ams1_1",
                toolhead_id="left",
                material_id=material.material_id,
                material_revision=material.revision,
                material_digest=material.digest or "0" * 64,
                slicer_filament_profile_id=(
                    material.spec.slicer_filament_profile_id
                ),
                slicer_filament_profile_digest=material.spec.slicer_profile_digest,
                slicer_filament_dependency_digests=(
                    material.spec.slicer_profile_dependency_digests
                ),
                color_distance=0,
                confidence=1,
                rationale="Exact configured red.",
            )
        ],
        requires_confirmation=False,
        confirmed_by="test",
        confirmed_at=workflow.updated_at,
    ).with_digest()
    await repository.save_material_assignment(assignment)
    job = SliceJob(
        workflow_id=workflow.id,
        artifact_version=artifact.version,
        artifact_manifest_digest=artifact.manifest_digest,
        printer_snapshot_digest=snapshot.digest or "0" * 64,
        material_assignment_digest=assignment.digest or "0" * 64,
        slicer_driver_id="bambu_studio_cli",
        idempotency_key="d" * 64,
    )
    reservation = SpoolReservation(
        workflow_id=workflow.id,
        slice_job_id=job.id,
        spool_id=spool.id,
        reserved_weight_g=44,
        remaining_weight_snapshot_g=100,
    )

    await repository.enqueue_slice(job, [reservation])

    assert (await repository.get_workflow(workflow.id)).state == (
        WorkflowState.SLICE_REQUESTED
    )
    assert (await repository.list_spool_reservations(job.id))[0].status == (
        "reserved"
    )
    with pytest.raises(ConflictError, match="immutable"):
        await repository.save_spool(
            spool.model_copy(update={"remaining_weight_g": 90})
        )
    with pytest.raises(ConflictError, match="no reconcilable"):
        await repository.reconcile_spool_reservations(job.id, {})
    with pytest.raises(ConflictError, match="every reserved spool"):
        await repository.reconcile_spool_reservations(
            job.id,
            {"other-spool": 40},
        )
    await repository.reconcile_spool_reservations(job.id, {spool.id: 40})
    reconciled = (await repository.list_spool_reservations(job.id))[0]
    assert reconciled.reserved_weight_g == pytest.approx(44)
    assert reconciled.actual_usage_g == pytest.approx(40)

    await repository.transition(workflow.id, WorkflowState.SLICING)
    await repository.transition(workflow.id, WorkflowState.SLICE_VALIDATING)
    sliced_path = tmp_path / "job.gcode.3mf"
    with zipfile.ZipFile(sliced_path, "w") as archive:
        archive.writestr("Metadata/plate_1.gcode", "G28\n")
    sliced = SlicedArtifact(
        slice_job_id=job.id,
        workflow_id=workflow.id,
        path=str(sliced_path),
        digest=sha256_file(sliced_path),
        size_bytes=sliced_path.stat().st_size,
        format="gcode.3mf",
        model_manifest_digest=artifact.manifest_digest,
        printer_snapshot_digest=snapshot.digest or "0" * 64,
        material_assignment_digest=assignment.digest or "0" * 64,
        slicer_driver_id="fake",
        slicer_version="test",
        machine_profile_id="h2d",
        process_profile_id="standard",
        plate_count=1,
        filament_usage_g={spool.id: 40},
        manifest_digest="e" * 64,
    )
    ready_job = job.model_copy(
        update={
            "status": SliceJobStatus.READY,
            "updated_at": workflow.updated_at,
        }
    )
    await repository.finalize_slice(ready_job, sliced)
    handoff = SubmissionHandoff(
        workflow_id=workflow.id,
        slice_job_id=job.id,
        sliced_artifact_digest=sliced.digest,
        submission_driver_id="bambu_connect_cloud",
        status=HandoffStatus.EXTERNAL_CONFIRMATION_REQUIRED,
    )
    await repository.save_submission_handoff(handoff)
    await repository.transition(
        workflow.id,
        WorkflowState.EXTERNAL_CONFIRMATION_REQUIRED,
    )

    confirmed = await repository.confirm_submission_handoff(
        workflow.id,
        handoff.id,
        submitted=True,
        confirmed_by="test",
    )

    assert confirmed.status == HandoffStatus.USER_CONFIRMED_SUBMITTED
    assert (await repository.get_workflow(workflow.id)).state == (
        WorkflowState.USER_CONFIRMED_SUBMITTED
    )
    assert (await repository.list_spool_reservations(job.id))[0].status == (
        "consumed"
    )
    assert (await repository.get_spool(spool.id)).remaining_weight_g == pytest.approx(
        60
    )
