from __future__ import annotations

from dataclasses import dataclass

from printing_agent.application import PrintingApplication
from printing_agent.artifact_store import ArtifactStore
from printing_agent.catalogs import ThingiverseCatalog
from printing_agent.config import Settings, get_settings
from printing_agent.copilot_agents import CopilotDiscoveryAgent, CopilotModelingAgent
from printing_agent.modeling import (
    MeshInspector,
    ModelPipeline,
    OpenScadRenderer,
    TrimeshSelectedSourceInspector,
)
from printing_agent.printers import PrinterRegistry, SimulatedPrinterAdapter
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
    application: PrintingApplication
    worker: DurableWorker


async def build_container(settings: Settings | None = None) -> Container:
    settings = settings or get_settings()
    settings.ensure_directories()
    repository = WorkflowRepository(settings.database_url)
    await repository.initialize()
    artifacts = ArtifactStore(settings.artifact_dir)
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
    revision_verifier = RevisionVerifier(CopilotRevisionVerifier(settings))
    printers = PrinterRegistry()
    printers.register(SimulatedPrinterAdapter(settings.simulator_spool_dir))
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
    )
    worker = DurableWorker(repository, application)
    return Container(
        settings=settings,
        repository=repository,
        artifacts=artifacts,
        catalog=catalog,
        printers=printers,
        application=application,
        worker=worker,
    )
