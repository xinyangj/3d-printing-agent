from __future__ import annotations

import json
import zipfile
from datetime import timedelta
from pathlib import Path

import aiosqlite
import pytest
import trimesh

from printing_agent.application import PrintingApplication
from printing_agent.artifact_store import ArtifactStore, sha256_file
from printing_agent.cloud_inventory import (
    CloudDeviceSnapshot,
    DeviceSummary,
    InstalledNozzle,
    parse_h2d_snapshot,
)
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
    new_id,
)
from printing_agent.errors import (
    ConflictError,
    MaterialEligibilityError,
    NotFoundError,
    ValidationError,
)
from printing_agent.fabrication import (
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
    SpoolQuantityStatus,
    SpoolReservation,
    SpoolStatus,
    UnknownQuantitySlotAuthorization,
    WorkflowPrinterSnapshot,
    ciede2000,
    resolve_slot_policy,
)
from printing_agent.fabrication_drivers import (
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


class StaticInventoryProvider:
    def __init__(self, snapshot: CloudDeviceSnapshot) -> None:
        self.value = snapshot
        self.snapshot_calls = 0

    async def validate_token(self, access_token: str, region: str):
        del access_token, region
        return (self.value.device,)

    async def list_devices(self):
        return (self.value.device,)

    async def snapshot(self, device_id: str):
        assert device_id == self.value.device.device_id
        self.snapshot_calls += 1
        return self.value.model_copy(update={"id": new_id()})


class CopyInventoryProvider:
    def __init__(self, device_id: str = "H2D-PRIVATE-SERIAL") -> None:
        self.device = DeviceSummary(
            device_id=device_id,
            name="Workshop H2D",
            model="H2D",
            online=True,
        )

    async def list_devices(self):
        return (self.device,)


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
    base_h2d = built_in_h2d_profile()
    h2d = base_h2d.model_copy(
        update={
            "spec": base_h2d.spec.model_copy(
                update={
                    "cloud_region": "global",
                    "cloud_device_name": "Workshop H2D",
                    "cloud_device_serial": "H2D-PRIVATE-SERIAL",
                }
            ),
            "digest": None,
        }
    ).with_digest()
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
    application.inventory = CopyInventoryProvider()
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


async def test_cloud_snapshot_drives_material_review(
    repository: WorkflowRepository,
    tmp_path: Path,
) -> None:
    base_profile = built_in_h2d_profile()
    profile = base_profile.model_copy(
        update={
            "spec": base_profile.spec.model_copy(
                update={
                    "cloud_region": "global",
                    "cloud_device_name": "Workshop H2D",
                    "cloud_device_serial": "H2D-PRIVATE-SERIAL",
                }
            ),
            "digest": None,
        }
    ).with_digest()
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
    await repository.save_plan(
        workflow.id,
        ModelPlan(
            search_query="fixture",
            geometry_summary="A fixture",
            target_dimensions=profile.spec.build_volume,
        ),
    )
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
    slicing_snapshot = WorkflowPrinterSnapshot(
        workflow_id=workflow.id,
        profile_id=profile.profile_id,
        profile_revision=profile.revision,
        profile_digest=profile.digest or "0" * 64,
        profile=profile.spec,
        resolved_slot_policy=SlotPolicy(),
    ).with_digest()
    await repository.save_workflow_printer_snapshot(slicing_snapshot)
    material = _material("red-pla", "#FF0000").model_copy(
        update={
            "spec": _material("red-pla", "#FF0000").spec.model_copy(
                update={"cloud_filament_ids": {"GFA00"}}
            ),
            "digest": None,
        }
    ).with_digest()
    await repository.save_material_definition(material)
    cloud_snapshot = parse_h2d_snapshot(
        {
            "print": {
                "device": {
                    "nozzle": {
                        "info": [
                            {"id": 0, "diameter": 0.4, "type": "hardened_steel"},
                            {"id": 1, "diameter": 0.4, "type": "hardened_steel"},
                        ]
                    }
                },
                "ams": {
                    "ams": [
                        {
                            "id": "0",
                            "tray": [
                                {
                                    "id": "0",
                                    "state": 11,
                                    "tray_type": "PLA",
                                    "tray_info_idx": "GFA00",
                                    "tray_color": "FF0000FF",
                                    "remain": 80,
                                    "tray_weight": 1000,
                                },
                                {"id": "1", "state": 0},
                                {"id": "2", "state": 0},
                                {"id": "3", "state": 0},
                            ],
                        }
                    ],
                },
                "vir_slot": [
                    {
                        "id": "254",
                        "state": 11,
                        "tray_type": "PETG",
                        "remain": 0,
                        "tray_weight": 0,
                    },
                    {"id": "255", "state": 0},
                ],
            }
        },
        DeviceSummary(
            device_id="H2D-PRIVATE-SERIAL",
            name="Workshop H2D",
            model="H2D",
            online=True,
        ),
    )
    application = object.__new__(PrintingApplication)
    application.repository = repository
    expired_source = cloud_snapshot.model_copy(
        update={
            "observed_at": cloud_snapshot.observed_at - timedelta(minutes=2),
            "expires_at": cloud_snapshot.observed_at - timedelta(minutes=1),
        }
    )
    inventory = StaticInventoryProvider(expired_source)
    application.inventory = inventory
    application.material_assignment = MaterialAssignmentService(
        DeterministicAssignmentAgent()  # type: ignore[arg-type]
    )

    observed = await application.refresh_cloud_snapshot(workflow.id)
    inventory.value = cloud_snapshot
    assignment = await application.propose_material_assignment(workflow.id)
    calls_after_assignment = inventory.snapshot_calls
    refreshed = await application.refresh_cloud_snapshot(workflow.id)

    assert observed.digest == cloud_snapshot.digest
    assert assignment.cloud_snapshot_digest == cloud_snapshot.digest
    assert assignment.cloud_snapshot_id != observed.id
    assert calls_after_assignment == 2
    assert assignment.requires_confirmation is True
    assert assignment.assignments[0].slot_id == "ams1_1"
    assert assignment.candidate_options["body"][0].remaining_weight_g == 800
    assert refreshed.digest == observed.digest
    assert (await repository.get_workflow(workflow.id)).state == (
        WorkflowState.AWAITING_MATERIAL_REVIEW
    )


def test_cloud_snapshot_rejects_non_hardened_installed_nozzles() -> None:
    profile = built_in_h2d_profile()
    snapshot = parse_h2d_snapshot(
        {
            "print": {
                "nozzles": [
                    {
                        "position": "left",
                        "diameter": 0.4,
                        "type": "hardened_steel",
                    },
                    {
                        "position": "right",
                        "diameter": 0.4,
                        "type": "hardened_steel",
                    },
                ],
                "ams": {
                    "ams": [
                        {
                            "id": "0",
                            "tray": [
                                {"id": str(index), "state": 0}
                                for index in range(4)
                            ],
                        }
                    ]
                },
                "vir_slot": [
                    {"id": "254", "state": 0},
                    {"id": "255", "state": 0},
                ],
            }
        },
        DeviceSummary(
            device_id="H2D-PRIVATE-SERIAL",
            name="Workshop H2D",
            model="H2D",
            online=True,
        ),
    ).model_copy(
        update={
            "installed_nozzles": (
                InstalledNozzle(
                    position="left",
                    diameter_mm=0.4,
                    nozzle_type="stainless_steel",
                ),
                InstalledNozzle(
                    position="right",
                    diameter_mm=0.4,
                    nozzle_type="stainless_steel",
                ),
            )
        }
    )
    profile = profile.model_copy(
        update={
            "spec": profile.spec.model_copy(
                update={
                    "cloud_region": "global",
                    "cloud_device_serial": "H2D-PRIVATE-SERIAL",
                }
            )
        }
    )

    with pytest.raises(ConflictError, match="nozzle material"):
        PrintingApplication._validate_cloud_snapshot(profile, snapshot)
    assert PrintingApplication._nozzle_material_category("HS00") == (
        "stainless_steel"
    )
    assert PrintingApplication._nozzle_material_category("HS01") == (
        "hardened_steel"
    )
    assert PrintingApplication._nozzle_material_category("HX05") == (
        "tungsten_carbide"
    )


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


async def test_fabrication_copy_rejects_printer_from_previous_account(
    repository: WorkflowRepository,
    tmp_path: Path,
) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    source, _ = await _stored_source_artifact(
        repository,
        store,
        tmp_path,
        schema_version="2",
        approved=True,
    )
    application = await _copy_application(repository, store)
    application.inventory = CopyInventoryProvider("OTHER-ACCOUNT-H2D")

    with pytest.raises(ConflictError, match="unavailable under the connected account"):
        await application.copy_workflow(
            source.id,
            target_printer_name="bambu-h2d",
        )


def test_observed_topology_adds_second_ams_and_preserves_external_slots() -> None:
    profile = built_in_h2d_profile()
    snapshot = parse_h2d_snapshot(
        {
            "print": {
                "nozzles": [
                    {"position": "left", "diameter": 0.4, "type": "HS01"},
                    {"position": "right", "diameter": 0.4, "type": "HS01"},
                ],
                "ams": {
                    "ams": [
                        {"id": "0", "tray": [{"id": str(index)} for index in range(4)]},
                        {"id": "1", "tray": [{"id": str(index)} for index in range(4)]},
                    ],
                    "vt_tray": [
                        {"id": "254", "state": 0},
                        {"id": "255", "state": 0},
                    ],
                },
            }
        },
        DeviceSummary(
            device_id="H2D-PRIVATE-SERIAL",
            name="Workshop H2D",
            model="H2D",
            online=True,
        ),
    )

    slots = PrintingApplication._observed_material_slots(profile, snapshot)

    assert [slot.id for slot in slots] == [
        "ams1_1",
        "ams1_2",
        "ams1_3",
        "ams1_4",
        "ams2_1",
        "ams2_2",
        "ams2_3",
        "ams2_4",
        "external_left",
        "external_right",
    ]
    assert slots[4].unit == 2
    assert slots[4].tray == 1
    assert slots[4].compatible_toolhead_ids == {"left", "right"}
    assert slots[-2].manual_swap_required is True
    assert slots[-1].manual_swap_required is True
    restricted = PrintingApplication._restrict_slot_policy(
        SlotPolicy(
            part_allowed_slot_ids={"body": {"ams1_1"}},
            part_forbidden_slot_ids={"lid": {"ams1_1"}},
        ),
        {"ams2_1"},
    )
    assert restricted.part_allowed_slot_ids == {"body": set()}
    assert restricted.part_forbidden_slot_ids == {}


async def test_profile_slot_observation_is_ephemeral(
    repository: WorkflowRepository,
) -> None:
    base = built_in_h2d_profile()
    profile = base.model_copy(
        update={
            "spec": base.spec.model_copy(
                update={
                    "cloud_region": "global",
                    "cloud_device_name": "Workshop H2D",
                    "cloud_device_serial": "H2D-PRIVATE-SERIAL",
                }
            ),
            "digest": None,
        }
    ).with_digest()
    await repository.save_printer_profile(profile)
    snapshot = parse_h2d_snapshot(
        {
            "print": {
                "nozzles": [
                    {"position": "left", "diameter": 0.4, "type": "HS01"},
                    {"position": "right", "diameter": 0.4, "type": "HS01"},
                ],
                "ams": {
                    "ams": [
                        {
                            "id": "0",
                            "tray": [
                                {
                                    "id": "0",
                                    "state": 11,
                                    "tray_type": "PLA",
                                    "tray_info_idx": "GFA00",
                                    "tray_color": "307FE2FF",
                                    "remain": 50,
                                    "tray_weight": 1000,
                                },
                                *[
                                    {"id": str(index), "state": 0}
                                    for index in range(1, 4)
                                ],
                            ],
                        }
                    ],
                    "vt_tray": [
                        {"id": "254", "state": 0},
                        {"id": "255", "state": 0},
                    ],
                },
            }
        },
        DeviceSummary(
            device_id="H2D-PRIVATE-SERIAL",
            name="Workshop H2D",
            model="H2D",
            online=True,
        ),
    )
    application = object.__new__(PrintingApplication)
    application.repository = repository
    application.inventory = StaticInventoryProvider(snapshot)

    observed_profile, observed, mappings = (
        await application.observe_slicing_profile_slots(profile.profile_id)
    )

    assert observed_profile.digest == profile.digest
    assert observed.digest == snapshot.digest
    assert mappings[0].cloud_filament_id == "GFA00"
    async with aiosqlite.connect(repository.database_path) as db:
        cursor = await db.execute("SELECT COUNT(*) FROM cloud_device_snapshots")
        assert (await cursor.fetchone())[0] == 0


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
        cloud_snapshot_id="cloud-snapshot",
        cloud_snapshot_digest="c" * 64,
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
            cloud_snapshot_id="cloud-snapshot",
            cloud_snapshot_digest="c" * 64,
            policy=SlotPolicy(forbidden_slot_ids={"ams1_1"}),
            overrides=JobOverrides(),
            spools=[],
            materials={},
            maximum_color_distance=5,
            default_material_family="pla",
        )


async def test_unknown_quantity_requires_authorization_and_never_fakes_grams(
    tmp_path: Path,
) -> None:
    artifact = await _artifact(tmp_path)
    profile = built_in_h2d_profile()
    material = _material("generic-pla", "#F98C36")
    unknown = PhysicalSpool(
        id="unknown-generic",
        material_id=material.material_id,
        material_revision=material.revision,
        material_digest=material.digest or "0" * 64,
        quantity_status=SpoolQuantityStatus.UNKNOWN,
        tray_identity_digest="e" * 64,
        status=SpoolStatus.LOADED,
        printer_profile_id=profile.profile_id,
        slot_id="ams1_1",
    )
    service = MaterialAssignmentService(DeterministicAssignmentAgent())  # type: ignore[arg-type]
    arguments = {
        "workflow_id": "workflow",
        "artifact": artifact,
        "profile": profile,
        "printer_snapshot_digest": "b" * 64,
        "cloud_snapshot_id": "cloud-snapshot",
        "cloud_snapshot_digest": "c" * 64,
        "policy": SlotPolicy(),
        "overrides": JobOverrides(),
        "spools": [unknown],
        "materials": {
            (material.material_id, material.revision): material,
        },
        "maximum_color_distance": 5,
        "default_material_family": "pla",
    }

    with pytest.raises(MaterialEligibilityError) as exc_info:
        await service.propose(**arguments)

    rejection = exc_info.value.details["rejections"][0]
    assert rejection["reason_code"] == "quantity_authorization_required"
    assert rejection["authorizable"] is True
    assert rejection["remaining_weight_g"] is None

    assignment = await service.propose(
        **{
            **arguments,
            "spools": [
                unknown.model_copy(
                    update={
                        "quantity_status": (
                            SpoolQuantityStatus.USER_ATTESTED_UNKNOWN
                        )
                    }
                )
            ],
        }
    )

    candidate = assignment.candidate_options[
        assignment.assignments[0].part_id
    ][0]
    assert candidate.remaining_weight_g is None
    assert (
        candidate.quantity_status
        == SpoolQuantityStatus.USER_ATTESTED_UNKNOWN
    )
    assert assignment.assignments[0].quantity_status == candidate.quantity_status


async def test_unknown_quantity_authorization_persists_and_revokes(
    repository: WorkflowRepository,
) -> None:
    authorization = UnknownQuantitySlotAuthorization(
        profile_id="bambu-h2d",
        device_ref="d" * 64,
        slot_id="ams2_3",
        tray_identity_digest="e" * 64,
        cloud_snapshot_digest="c" * 64,
        material_profile_id="GFL99",
        material="PLA",
        color="#F98C36",
        authorized_by="test",
    )

    await repository.save_unknown_quantity_authorization(authorization)
    assert await repository.list_unknown_quantity_authorizations(
        authorization.profile_id,
        authorization.device_ref,
    ) == [authorization]

    await repository.revoke_unknown_quantity_authorization(
        authorization.profile_id,
        authorization.device_ref,
        authorization.slot_id,
    )
    assert (
        await repository.list_unknown_quantity_authorizations(
            authorization.profile_id,
            authorization.device_ref,
        )
        == []
    )


async def test_unknown_quantity_authorization_invalidates_on_detectable_change(
    repository: WorkflowRepository,
) -> None:
    base = built_in_h2d_profile()
    profile = base.model_copy(
        update={
            "spec": base.spec.model_copy(
                update={"cloud_device_serial": "H2D-PRIVATE-SERIAL"}
            ),
            "digest": None,
        }
    ).with_digest()
    report = {
        "print": {
            "device": {
                "nozzle": {
                    "info": [
                        {"id": 0, "diameter": 0.4, "type": "HS01"},
                        {"id": 1, "diameter": 0.4, "type": "HS01"},
                    ]
                }
            },
            "ams": {
                "ams": [
                    {
                        "id": "0",
                        "tray": [
                            {
                                "id": "0",
                                "state": 17,
                                "tray_type": "PLA",
                                "tray_info_idx": "GFL99",
                                "tray_color": "F98C36FF",
                                "remain": -1,
                                "tag_uid": "0" * 16,
                                "tray_uuid": "0" * 32,
                            },
                            *[
                                {"id": str(index), "state": 0}
                                for index in range(1, 4)
                            ],
                        ],
                    }
                ],
                "vt_tray": [
                    {"id": "254", "state": 0},
                    {"id": "255", "state": 0},
                ],
            },
        }
    }
    device = DeviceSummary(
        device_id="H2D-PRIVATE-SERIAL",
        name="Workshop H2D",
        model="H2D",
        online=True,
    )
    snapshot = parse_h2d_snapshot(report, device)
    application = object.__new__(PrintingApplication)
    application.repository = repository
    tray = snapshot.ams_units[0].trays[0]
    identity = application._tray_identity_digest(device.device_id, tray)

    await application._authorize_unknown_quantity_slot(
        profile,
        snapshot,
        tray.slot_id,
        expected_cloud_snapshot_digest=snapshot.digest,
        expected_tray_identity_digest=identity,
        authorized_by="test",
    )
    active = await application.unknown_quantity_authorization_states(
        profile,
        snapshot,
    )
    assert active[0]["status"] == "authorized_unknown"

    assignment = MaterialAssignment(
        workflow_id="workflow",
        artifact_version=1,
        artifact_manifest_digest="a" * 64,
        printer_snapshot_digest="b" * 64,
        cloud_snapshot_id=snapshot.id,
        cloud_snapshot_digest=snapshot.digest,
        slot_policy_digest="c" * 64,
        requests=[
            PartMaterialRequest(
                part_id="body",
                part_name="Body",
                requested_color="#F98C36",
                estimated_weight_g=10,
            )
        ],
        assignments=[
            PartMaterialAssignment(
                part_id="body",
                spool_id="cloud-spool",
                slot_id=tray.slot_id,
                toolhead_id="left",
                material_id="generic-pla",
                material_revision=1,
                material_digest="d" * 64,
                quantity_status=SpoolQuantityStatus.USER_ATTESTED_UNKNOWN,
                slicer_filament_profile_id="Generic PLA @BBL H2D",
                color_distance=0,
                confidence=1,
                rationale="User confirmed sufficient filament.",
            )
        ],
        requires_confirmation=True,
    ).with_digest()
    await application._ensure_unknown_quantity_authorizations_current(
        profile,
        snapshot,
        assignment,
    )
    await repository.revoke_unknown_quantity_authorization(
        profile.profile_id,
        application._device_ref(device.device_id),
        tray.slot_id,
    )
    with pytest.raises(ConflictError, match="no longer valid"):
        await application._ensure_unknown_quantity_authorizations_current(
            profile,
            snapshot,
            assignment,
        )

    changed_report = json.loads(json.dumps(report))
    changed_report["print"]["ams"]["ams"][0]["tray"][0][
        "tray_color"
    ] = "FFFFFF00"
    changed_snapshot = parse_h2d_snapshot(changed_report, device)
    stale = await application.unknown_quantity_authorization_states(
        profile,
        changed_snapshot,
    )
    assert stale[0]["status"] == "authorization_required"

    zero_report = json.loads(json.dumps(report))
    zero_report["print"]["ams"]["ams"][0]["tray"][0]["remain"] = 0
    zero_report["print"]["ams"]["ams"][0]["tray"][0]["tray_weight"] = 1000
    zero_snapshot = parse_h2d_snapshot(zero_report, device)
    assert (
        await application.unknown_quantity_authorization_states(
            profile,
            zero_snapshot,
        )
        == []
    )
    zero_tray = zero_snapshot.ams_units[0].trays[0]
    with pytest.raises(ConflictError, match="measured quantity"):
        await application._authorize_unknown_quantity_slot(
            profile,
            zero_snapshot,
            zero_tray.slot_id,
            expected_cloud_snapshot_digest=zero_snapshot.digest,
            expected_tray_identity_digest=application._tray_identity_digest(
                device.device_id,
                zero_tray,
            ),
            authorized_by="test",
        )


async def test_user_can_select_only_compatible_candidate_trays(
    tmp_path: Path,
) -> None:
    artifact = await _artifact(tmp_path)
    profile = built_in_h2d_profile()
    red = _material("red-pla", "#FF0000")
    orange = _material("orange-pla", "#FF4400")
    spools = [
        PhysicalSpool(
            id="red",
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
            id="orange",
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
    service = MaterialAssignmentService(
        DeterministicAssignmentAgent()  # type: ignore[arg-type]
    )
    assignment = await service.propose(
        workflow_id="workflow",
        artifact=artifact,
        profile=profile,
        printer_snapshot_digest="b" * 64,
        cloud_snapshot_id="cloud-snapshot",
        cloud_snapshot_digest="c" * 64,
        policy=SlotPolicy(),
        overrides=JobOverrides(),
        spools=spools,
        materials={
            (red.material_id, red.revision): red,
            (orange.material_id, orange.revision): orange,
        },
        maximum_color_distance=10,
        default_material_family="pla",
    )

    overridden = service.apply_user_overrides(
        assignment,
        {"body": "orange"},
    )

    assert overridden.assignments[0].slot_id == "ams1_2"
    with pytest.raises(ValidationError, match="not compatible"):
        service.apply_user_overrides(assignment, {"body": "unknown"})


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
        filament_map=["2"],
    )

    assert args[:4] == [
        "--slice=1",
        "--arrange=1",
        "--filament-map-mode=Manual",
        "--filament-map=2",
    ]
    assert args[-1] == str(input_path)
    assert str(output_path) in args
    assert str(filament) in args
    assert "&" not in args


def test_bambu_windows_returncode_normalization(monkeypatch) -> None:
    monkeypatch.setattr("printing_agent.fabrication_drivers.os.name", "nt")

    assert BambuStudioCliDriver._normalized_returncode(4294967294) == -2
    assert BambuStudioCliDriver._normalized_returncode(1) == 1


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
        cloud_snapshot_id="cloud-snapshot",
        cloud_snapshot_digest="c" * 64,
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
        cloud_snapshot_digest="c" * 64,
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
    assert settings["enable_prime_tower"] == "0"
    assert settings["curr_bed_type"] == "High Temp Plate"
    assert settings["nozzle_diameter"] == ["0.4", "0.4"]
    cap_assignment = part_assignment.model_copy(
        update={
            "part_id": "cap",
            "spool_id": "spool-blue",
            "slot_id": "ams1_2",
        }
    )
    multi_assignment = assignment.model_copy(
        update={
            "requests": [
                *assignment.requests,
                PartMaterialRequest(
                    part_id="cap",
                    part_name="Cap",
                    requested_color="#0000FF",
                ),
            ],
            "assignments": [part_assignment, cap_assignment],
            "digest": None,
        }
    ).with_digest()
    multi_settings = BambuStudioCliDriver._resolved_job_settings(
        SliceRequest(
            job=job,
            artifact=artifact,
            printer=snapshot,
            material_assignment=multi_assignment,
            workspace=tmp_path / "multi-slice",
        )
    )
    assert multi_settings["enable_prime_tower"] == "1"
    assert multi_settings["wipe_tower_x"] == ["280"]
    assert multi_settings["wipe_tower_y"] == ["20"]
    rewritten_path = tmp_path / "rewritten-native-map.gcode.3mf"
    rewritten_project_settings = {
        **multi_settings,
        "printer_settings_id": machine_name,
        "print_settings_id": process_name,
    }
    slice_info = (
        '<?xml version="1.0"?><config><plate>'
        '<metadata key="filament_maps" value="2 2"/>'
        '<object identify_id="8" name="body" skipped="false"/>'
        '<object identify_id="12" name="cap" skipped="false"/>'
        "</plate></config>"
    )
    rewritten_gcode = (
        "; filament_map = 2,2\n"
        "T0 H-1\n"
        "; OBJECT_ID: 8\n"
        "; start printing object, unique label id: 8\n"
        "G1 X1 Y1 E1\n"
        "; stop printing object, unique label id: 8\n"
        "T1 H-1\n"
        "; OBJECT_ID: 12\n"
        "; start printing object, unique label id: 12\n"
        "G1 X2 Y2 E1\n"
        "; stop printing object, unique label id: 12\n"
    )
    with zipfile.ZipFile(rewritten_path, "w") as archive:
        archive.writestr(
            "Metadata/model_settings.config",
            '<?xml version="1.0"?><config><plate/></config>',
        )
        archive.writestr(
            "Metadata/project_settings.config",
            json.dumps(rewritten_project_settings),
        )
        archive.writestr("Metadata/slice_info.config", slice_info)
        archive.writestr("Metadata/plate_1.gcode", rewritten_gcode)
    BambuStudioCliDriver._validate_sliced_assignment(
        rewritten_path,
        SliceRequest(
            job=job,
            artifact=artifact,
            printer=snapshot,
            material_assignment=multi_assignment,
            workspace=tmp_path / "multi-slice",
        ),
    )
    with zipfile.ZipFile(rewritten_path, "w") as archive:
        archive.writestr(
            "Metadata/model_settings.config",
            '<?xml version="1.0"?><config><plate/></config>',
        )
        archive.writestr(
            "Metadata/project_settings.config",
            json.dumps(rewritten_project_settings),
        )
        archive.writestr("Metadata/slice_info.config", slice_info)
        archive.writestr(
            "Metadata/plate_1.gcode",
            rewritten_gcode.replace("T0 H-1", "T1 H-1", 1),
        )
    with pytest.raises(ValidationError, match="object-to-filament"):
        BambuStudioCliDriver._validate_sliced_assignment(
            rewritten_path,
            SliceRequest(
                job=job,
                artifact=artifact,
                printer=snapshot,
                material_assignment=multi_assignment,
                workspace=tmp_path / "multi-slice",
            ),
        )
    duplicate_slice_info = slice_info.replace(
        '<object identify_id="12"',
        '<object identify_id="9" name="body" skipped="false"/>'
        '<object identify_id="12"',
    )
    duplicate_gcode = (
        rewritten_gcode
        + "T1 H-1\n"
        + "; OBJECT_ID: 9\n"
        + "; start printing object, unique label id: 9\n"
        + "G1 X3 Y3 E1\n"
        + "; stop printing object, unique label id: 9\n"
    )
    with zipfile.ZipFile(rewritten_path, "w") as archive:
        archive.writestr(
            "Metadata/model_settings.config",
            '<?xml version="1.0"?><config><plate/></config>',
        )
        archive.writestr(
            "Metadata/project_settings.config",
            json.dumps(rewritten_project_settings),
        )
        archive.writestr("Metadata/slice_info.config", duplicate_slice_info)
        archive.writestr("Metadata/plate_1.gcode", duplicate_gcode)
    with pytest.raises(ValidationError, match="object-to-filament"):
        BambuStudioCliDriver._validate_sliced_assignment(
            rewritten_path,
            SliceRequest(
                job=job,
                artifact=artifact,
                printer=snapshot,
                material_assignment=multi_assignment,
                workspace=tmp_path / "multi-slice",
            ),
        )
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


async def test_legacy_combined_fabrication_is_quarantined(
    tmp_path: Path,
) -> None:
    repository = WorkflowRepository(tmp_path / "legacy.db")
    await repository.initialize()
    profile = built_in_h2d_profile()
    await repository.save_printer_profile(profile)
    workflow = await repository.create_workflow("Legacy H2D slice", "bambu-h2d")
    pending = await repository.lease_next()
    assert pending is not None
    await repository.complete_work(pending.id)
    legacy_job = SliceJob(
        workflow_id=workflow.id,
        artifact_version=1,
        artifact_manifest_digest="a" * 64,
        printer_snapshot_digest="b" * 64,
        cloud_snapshot_digest="c" * 64,
        material_assignment_digest="d" * 64,
        slicer_driver_id="legacy",
        idempotency_key="e" * 64,
    )
    await repository.save_slice_job(legacy_job)
    async with aiosqlite.connect(repository.database_path) as db:
        await db.execute(
            "DELETE FROM app_schema_migrations WHERE name = ?",
            ("slice-only-v1-quarantine",),
        )
        await db.execute(
            """
            CREATE TABLE submission_handoffs (
                id TEXT PRIMARY KEY,
                workflow_id TEXT,
                slice_job_id TEXT NOT NULL REFERENCES slice_jobs(id),
                payload_json TEXT
            )
            """
        )
        await db.execute(
            """
            INSERT INTO submission_handoffs (
                id, workflow_id, slice_job_id, payload_json
            ) VALUES (?, ?, ?, ?)
            """,
            ("legacy-handoff", workflow.id, legacy_job.id, "{}"),
        )
        await db.execute(
            "UPDATE workflows SET state = ? WHERE id = ?",
            ("external_confirmation_required", workflow.id),
        )
        await db.execute(
            """
            INSERT INTO approvals (
                workflow_id, artifact_version, manifest_digest,
                approved_by, approved_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                workflow.id,
                1,
                "a" * 64,
                "legacy-test",
                workflow.created_at.isoformat(),
            ),
        )
        await db.commit()

    await repository.initialize()

    assert (await repository.get_workflow(workflow.id)).state == (
        WorkflowState.APPROVED
    )
    assert await repository.list_printer_profiles() == []
    async with aiosqlite.connect(repository.database_path) as db:
        cursor = await db.execute(
            """
            SELECT COUNT(*) FROM sqlite_master
            WHERE type = 'table' AND name = 'submission_handoffs'
            """
        )
        assert (await cursor.fetchone())[0] == 0


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
        cloud_snapshot_id="cloud-snapshot",
        cloud_snapshot_digest="c" * 64,
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
    await repository.transition(workflow.id, WorkflowState.SLICE_SETUP)
    await repository.transition(
        workflow.id,
        WorkflowState.AWAITING_MATERIAL_REVIEW,
    )
    job = SliceJob(
        workflow_id=workflow.id,
        artifact_version=artifact.version,
        artifact_manifest_digest=artifact.manifest_digest,
        printer_snapshot_digest=snapshot.digest or "0" * 64,
        cloud_snapshot_digest="c" * 64,
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
    reconciliation = await repository.reconcile_spool_reservations(
        job.id,
        {spool.id: 40},
    )
    assert reconciliation.shortages == ()
    reconciled = (await repository.list_spool_reservations(job.id))[0]
    assert reconciled.reserved_weight_g == pytest.approx(46)
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
        cloud_snapshot_digest="c" * 64,
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
    assert (await repository.get_workflow(workflow.id)).state == (
        WorkflowState.AWAITING_SLICE_REVIEW
    )
    assert (await repository.list_spool_reservations(job.id))[0].status == (
        "released"
    )
    assert (await repository.get_spool(spool.id)).remaining_weight_g == pytest.approx(
        100
    )
    shortage_job = job.model_copy(
        update={
            "id": new_id(),
            "idempotency_key": "f" * 64,
            "status": SliceJobStatus.REQUESTED,
        }
    )
    shortage_reservation = reservation.model_copy(
        update={
            "id": new_id(),
            "slice_job_id": shortage_job.id,
            "status": "reserved",
        }
    )
    await repository.enqueue_slice(shortage_job, [shortage_reservation])
    shortage = await repository.reconcile_spool_reservations(
        shortage_job.id,
        {spool.id: 90},
    )
    assert shortage.shortages[0].required_weight_g == pytest.approx(103.5)
    assert shortage.shortages[0].available_weight_g == pytest.approx(100)
    assert shortage.shortages[0].shortfall_g == pytest.approx(3.5)
    persisted_shortage = (
        await repository.list_spool_reservations(shortage_job.id)
    )[0]
    assert persisted_shortage.status == "insufficient"
    assert persisted_shortage.actual_usage_g == pytest.approx(90)
