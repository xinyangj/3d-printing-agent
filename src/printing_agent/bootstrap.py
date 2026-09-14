from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import timedelta

from printing_agent.application import PrintingApplication
from printing_agent.artifact_store import ArtifactStore
from printing_agent.bambu_connect import BambuConnectManager
from printing_agent.bambu_studio_session import BambuStudioSessionAdapter
from printing_agent.catalogs import ThingiverseCatalog
from printing_agent.cloud_credentials import CloudCredentialStore
from printing_agent.cloud_inventory import (
    BambuCloudInventoryProvider,
    InventoryProvider,
)
from printing_agent.config import Settings, get_settings
from printing_agent.copilot_agents import CopilotDiscoveryAgent, CopilotModelingAgent
from printing_agent.errors import NotFoundError
from printing_agent.fabrication import JobOverrides, WorkflowPrinterSnapshot, resolve_slot_policy
from printing_agent.fabrication_drivers import (
    BambuStudioCliDriver,
    SlicerRegistry,
)
from printing_agent.fabrication_profiles import (
    built_in_profiles,
    resolve_slicer_profile_file_digests,
)
from printing_agent.material_assignment import (
    MaterialAssignmentAgent,
    MaterialAssignmentService,
)
from printing_agent.modeling import (
    MeshInspector,
    ModelPipeline,
    OpenScadRenderer,
    TrimeshSelectedSourceInspector,
)
from printing_agent.printers import (
    PrinterRegistry,
    ProfilePrinterAdapter,
    SimulatedPrinterAdapter,
)
from printing_agent.repositories import WorkflowRepository
from printing_agent.verification import CopilotRevisionVerifier, RevisionVerifier
from printing_agent.worker import DurableWorker


@dataclass(frozen=True)
class Container:
    settings: Settings
    repository: WorkflowRepository
    artifacts: ArtifactStore
    catalog: ThingiverseCatalog
    printers: PrinterRegistry
    slicers: SlicerRegistry
    bambu_studio_session: BambuStudioSessionAdapter
    cloud_credentials: CloudCredentialStore
    inventory: InventoryProvider
    material_assignment: MaterialAssignmentService
    bambu_connect: BambuConnectManager
    application: PrintingApplication
    worker: DurableWorker


async def build_container(settings: Settings | None = None) -> Container:
    settings = settings or get_settings()
    settings.ensure_directories()
    repository = WorkflowRepository(settings.database_url)
    await repository.initialize()
    for profile in built_in_profiles():
        if profile.profile_id == "bambu-h2d":
            profile = profile.model_copy(
                update={
                    "spec": profile.spec.model_copy(
                        update={
                            "slicer": profile.spec.slicer.model_copy(
                                update={
                                    "executable_path": settings.bambu_studio_path,
                                    "resource_root": (
                                        str(settings.bambu_studio_resource_dir)
                                        if settings.bambu_studio_resource_dir
                                        else None
                                    ),
                                }
                            )
                        }
                    ),
                    "digest": None,
                }
            ).with_digest()
        try:
            await repository.get_printer_profile(
                profile.profile_id,
                profile.revision,
            )
        except NotFoundError:
            await repository.save_printer_profile(profile)
            continue
        try:
            current = await repository.get_printer_profile(profile.profile_id)
        except NotFoundError:
            continue
        if current.origin.value == "built_in" and current.spec != profile.spec:
            updated = profile.model_copy(
                update={
                    "revision": current.revision + 1,
                    "digest": None,
                }
            ).with_digest()
            await repository.save_printer_profile(updated)
    artifacts = ArtifactStore(settings.artifact_dir)
    for workflow in await repository.list_workflows():
        try:
            await repository.get_workflow_printer_snapshot(workflow.id)
        except NotFoundError:
            profile = await repository.get_printer_profile(workflow.printer_name)
            snapshot = WorkflowPrinterSnapshot(
                workflow_id=workflow.id,
                profile_id=profile.profile_id,
                profile_revision=profile.revision,
                profile_digest=profile.digest or "0" * 64,
                profile=profile.spec,
                overrides=JobOverrides(),
                slicer_profile_file_digests=await asyncio.to_thread(
                    resolve_slicer_profile_file_digests,
                    profile,
                ),
                resolved_slot_policy=resolve_slot_policy(
                    profile.spec,
                    JobOverrides(),
                ),
            ).with_digest()
            await repository.save_workflow_printer_snapshot(snapshot)
    catalog = ThingiverseCatalog(settings)
    mesh_inspector = MeshInspector()
    model_pipeline = ModelPipeline(
        settings,
        repository,
        artifacts,
        OpenScadRenderer(settings),
        mesh_inspector,
    )
    discovery = CopilotDiscoveryAgent(settings, repository, catalog)
    modeling = CopilotModelingAgent(settings, repository, model_pipeline)
    revision_verifier = RevisionVerifier(
        CopilotRevisionVerifier(settings, repository)
    )
    printers = PrinterRegistry()
    printers.register(SimulatedPrinterAdapter(settings.simulator_spool_dir))
    for profile in await repository.list_printer_profiles():
        if profile.profile_id != "simulator":
            printers.upsert(ProfilePrinterAdapter(profile))
    slicers = SlicerRegistry()
    slicers.register(BambuStudioCliDriver(settings.bambu_studio_path))
    bambu_studio_session = BambuStudioSessionAdapter(
        settings.bambu_studio_path,
        settings.bambu_studio_config_dir,
    )
    cloud_credentials = CloudCredentialStore(
        settings.bambu_cloud_credential_path
    )
    inventory = BambuCloudInventoryProvider(
        cloud_credentials,
        snapshot_timeout_seconds=(
            settings.bambu_cloud_snapshot_timeout_seconds
        ),
        snapshot_ttl=timedelta(
            seconds=settings.bambu_cloud_snapshot_ttl_seconds
        ),
    )
    material_assignment = MaterialAssignmentService(
        MaterialAssignmentAgent(settings, repository)
    )
    bambu_connect = BambuConnectManager(settings)
    application = PrintingApplication(
        settings,
        repository,
        artifacts,
        catalog,
        discovery,
        modeling,
        TrimeshSelectedSourceInspector(mesh_inspector),
        model_pipeline,
        printers,
        revision_verifier,
        slicers,
        inventory,
        material_assignment,
        bambu_connect,
    )
    worker = DurableWorker(repository, application)
    return Container(
        settings=settings,
        repository=repository,
        artifacts=artifacts,
        catalog=catalog,
        printers=printers,
        slicers=slicers,
        bambu_studio_session=bambu_studio_session,
        cloud_credentials=cloud_credentials,
        inventory=inventory,
        material_assignment=material_assignment,
        bambu_connect=bambu_connect,
        application=application,
        worker=worker,
    )
