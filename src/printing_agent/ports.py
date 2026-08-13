from __future__ import annotations

from pathlib import Path
from typing import Protocol

from printing_agent.domain import (
    CandidatePageInspection,
    Dimensions,
    DiscoveryDecision,
    ModelArtifact,
    ModelCandidate,
    ModelingHandoff,
    ModelPlan,
    PrinterCapabilitySummary,
    PrintJob,
    PrintSettings,
    SelectedSourceInspection,
)


class DiscoveryAgent(Protocol):
    async def discover(
        self,
        workflow_id: str,
        requirement: str,
        printer: PrinterCapabilitySummary,
    ) -> tuple[ModelPlan, DiscoveryDecision]: ...

    async def resume_after_source_rejection(
        self,
        workflow_id: str,
        inspection: SelectedSourceInspection,
    ) -> DiscoveryDecision: ...


class ModelingAgent(Protocol):
    async def build(self, handoff: ModelingHandoff) -> ModelArtifact: ...

    async def revise(
        self,
        handoff: ModelingHandoff,
        artifact: ModelArtifact,
        feedback: str,
    ) -> ModelArtifact: ...


class ModelCatalog(Protocol):
    async def search(self, query: str, page: int, limit: int) -> list[ModelCandidate]: ...

    async def inspect_page(
        self,
        workflow_id: str,
        search_round_id: str,
        candidate_id: str,
    ) -> CandidatePageInspection: ...

    async def download_file(
        self,
        candidate: ModelCandidate,
        file_id: str,
        destination: Path,
    ) -> Path: ...


class SelectedSourceInspector(Protocol):
    async def inspect(
        self,
        workflow_id: str,
        candidate: ModelCandidate,
        file_id: str,
        path: Path,
        build_volume: Dimensions,
    ) -> SelectedSourceInspection: ...


class PrinterAdapter(Protocol):
    @property
    def name(self) -> str: ...

    async def capabilities(self) -> PrinterCapabilitySummary: ...

    async def validate(self, artifact: ModelArtifact, settings: PrintSettings) -> None: ...

    async def submit(
        self,
        workflow_id: str,
        artifact: ModelArtifact,
        settings: PrintSettings,
        idempotency_key: str,
    ) -> PrintJob: ...

    async def status(self, external_id: str) -> PrintJob: ...

    async def cancel(self, external_id: str) -> PrintJob: ...
