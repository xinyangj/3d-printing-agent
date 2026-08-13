from __future__ import annotations

import hashlib
from typing import cast

from printing_agent.artifact_store import ArtifactStore
from printing_agent.catalogs import ThingiverseCatalog
from printing_agent.config import Settings
from printing_agent.copilot_agents import CopilotDiscoveryAgent, CopilotModelingAgent
from printing_agent.domain import (
    ArtifactApproval,
    ArtifactProvenance,
    DiscoveryDecision,
    ModelArtifact,
    ModelDecision,
    ModelingHandoff,
    PrintJobStatus,
    RevisionMode,
    RevisionRequest,
    SelectedSourceSummary,
    WorkflowState,
    WorkKind,
)
from printing_agent.errors import (
    BudgetExhaustedError,
    ConflictError,
    ValidationError,
)
from printing_agent.modeling import ModelPipeline, TrimeshSelectedSourceInspector
from printing_agent.ports import PrinterAdapter
from printing_agent.printers import PrinterRegistry
from printing_agent.repositories import WorkflowRepository


class PrintingApplication:
    def __init__(
        self,
        settings: Settings,
        repository: WorkflowRepository,
        artifacts: ArtifactStore,
        catalog: ThingiverseCatalog,
        discovery: CopilotDiscoveryAgent,
        modeling: CopilotModelingAgent,
        source_inspector: TrimeshSelectedSourceInspector,
        model_pipeline: ModelPipeline,
        printers: PrinterRegistry,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.artifacts = artifacts
        self.catalog = catalog
        self.discovery = discovery
        self.modeling = modeling
        self.source_inspector = source_inspector
        self.model_pipeline = model_pipeline
        self.printers = printers

    async def create_workflow(self, requirement: str, printer_name: str):
        self.printers.get(printer_name)
        return await self.repository.create_workflow(requirement, printer_name)

    async def prepare(self, workflow_id: str) -> ModelArtifact:
        workflow = await self.repository.get_workflow(workflow_id)
        adapter = cast(PrinterAdapter, self.printers.get(workflow.printer_name))
        printer = await adapter.capabilities()

        if workflow.state == WorkflowState.RECEIVED:
            workflow = await self.repository.transition(
                workflow.id,
                WorkflowState.PLANNING,
                event_kind="preparation.started",
            )
            plan, decision = await self.discovery.discover(
                workflow.id,
                workflow.requirement,
                printer,
            )
        elif workflow.state == WorkflowState.REVISION_REQUESTED:
            revision = await self.repository.get_latest_revision(workflow.id)
            if revision.mode == RevisionMode.REFINE_CURRENT:
                return await self._refine_current(workflow.id, revision.feedback)
            await self.repository.transition(
                workflow.id,
                WorkflowState.DISCOVERING,
                event_kind="revision.discovery_restarted",
                payload={"feedback": revision.feedback},
            )
            plan = await self.repository.get_plan(workflow.id)
            decision = await self.discovery.restart_with_feedback(
                workflow.id,
                revision.feedback,
            )
        else:
            raise ConflictError(f"Cannot prepare a workflow in state {workflow.state.value}")

        while True:
            if decision.decision == ModelDecision.CREATE:
                return await self._create_with_modeling(
                    workflow_id,
                    plan,
                    decision,
                    printer,
                    selected_source=None,
                )

            page = await self.repository.get_page_inspection(
                workflow_id,
                str(decision.candidate_id),
            )
            candidate = page.candidate
            source_path = (
                self.settings.candidate_cache_dir
                / "models"
                / "incoming"
                / workflow_id
                / f"{decision.file_id}.stl"
            )
            await self.repository.transition(
                workflow_id,
                WorkflowState.SOURCE_VALIDATION,
                event_kind="source.download_started",
                payload={
                    "candidate_id": decision.candidate_id,
                    "file_id": decision.file_id,
                },
            )
            await self.catalog.download_file(candidate, str(decision.file_id), source_path)
            inspection = await self.source_inspector.inspect(
                workflow_id,
                str(decision.candidate_id),
                str(decision.file_id),
                source_path,
                printer.build_volume,
            )
            if inspection.accepted:
                cache_path = (
                    self.settings.candidate_cache_dir
                    / "models"
                    / f"{inspection.source_digest}.stl"
                )
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                if not cache_path.exists():
                    source_path.replace(cache_path)
                else:
                    source_path.unlink(missing_ok=True)
                inspection = inspection.model_copy(update={"cached_path": cache_path})
            await self.repository.save_source_inspection(inspection)

            if not inspection.accepted:
                source_path.unlink(missing_ok=True)
                rejected = await self.repository.count_rejected_sources(workflow_id)
                if rejected >= self.settings.post_selection_failure_budget:
                    raise BudgetExhaustedError(
                        "Selected source validation failure budget was exhausted"
                    )
                await self.repository.transition(
                    workflow_id,
                    WorkflowState.DISCOVERING,
                    event_kind="source.rejected",
                    payload={"reason": inspection.rejection_reason},
                )
                decision = await self.discovery.resume_after_source_rejection(
                    workflow_id,
                    inspection,
                )
                continue

            provenance = ArtifactProvenance(
                kind="catalog",
                candidate_id=candidate.id,
                source_url=candidate.source_url,
                creator=candidate.creator,
                license=candidate.license,
                source_digest=inspection.source_digest,
            )
            if decision.decision == ModelDecision.USE_AS_IS:
                await self.repository.transition(
                    workflow_id,
                    WorkflowState.VALIDATING,
                    event_kind="source.adopting_unchanged",
                )
                return await self.model_pipeline.adopt_existing(
                    workflow_id,
                    inspection.cached_path,
                    provenance,
                    printer.build_volume,
                )

            selected_source = SelectedSourceSummary(
                candidate_id=candidate.id,
                file_id=str(decision.file_id),
                title=candidate.title,
                creator=candidate.creator,
                license=candidate.license,
                attribution_url=candidate.source_url,
                source_digest=inspection.source_digest,
                mesh=inspection.mesh,
            )
            return await self._create_with_modeling(
                workflow_id,
                plan,
                decision,
                printer,
                selected_source=selected_source,
            )

    async def _create_with_modeling(
        self,
        workflow_id: str,
        plan,
        decision: DiscoveryDecision,
        printer,
        *,
        selected_source: SelectedSourceSummary | None,
    ) -> ModelArtifact:
        version = await self.repository.next_handoff_version(workflow_id)
        handoff = ModelingHandoff(
            workflow_id=workflow_id,
            version=version,
            requirement=(await self.repository.get_workflow(workflow_id)).requirement,
            model_plan=plan,
            decision=(
                ModelDecision.MODIFY
                if decision.decision == ModelDecision.MODIFY
                else ModelDecision.CREATE
            ),
            required_changes=decision.required_changes,
            selected_source=selected_source,
            target_printer=printer,
            discovery_rationale=decision.rationale,
            evidence_digests=(
                [selected_source.source_digest] if selected_source is not None else []
            ),
        ).with_digest()
        await self.repository.save_handoff(handoff)
        await self.repository.patch_workflow(
            workflow_id,
            modeling_session_id=None,
            event_kind="modeling.new_session_required",
            payload={"handoff_version": version, "handoff_digest": handoff.digest},
        )
        await self.repository.transition(
            workflow_id,
            WorkflowState.HANDOFF_READY,
            event_kind="modeling.handoff_ready",
            payload={"handoff_version": version, "handoff_digest": handoff.digest},
        )
        await self.repository.transition(
            workflow_id,
            WorkflowState.GENERATING,
            event_kind="modeling.started",
            payload={"handoff_version": version},
        )
        return await self.modeling.build(handoff)

    async def _refine_current(self, workflow_id: str, feedback: str) -> ModelArtifact:
        workflow = await self.repository.get_workflow(workflow_id)
        if workflow.active_handoff_version is None or workflow.active_artifact_version is None:
            raise ConflictError("Current artifact was not produced by a modeling handoff")
        old_handoff = await self.repository.get_handoff(
            workflow_id,
            workflow.active_handoff_version,
        )
        artifact = await self.repository.get_artifact(
            workflow_id,
            workflow.active_artifact_version,
        )
        version = await self.repository.next_handoff_version(workflow_id)
        handoff = old_handoff.model_copy(
            update={
                "version": version,
                "required_changes": [*old_handoff.required_changes, feedback],
                "digest": None,
            }
        ).with_digest()
        await self.repository.save_handoff(handoff)
        await self.repository.transition(
            workflow_id,
            WorkflowState.HANDOFF_READY,
            event_kind="revision.handoff_ready",
            payload={"handoff_version": version},
        )
        await self.repository.transition(
            workflow_id,
            WorkflowState.GENERATING,
            event_kind="revision.modeling_started",
        )
        return await self.modeling.revise(handoff, artifact, feedback)

    async def request_revision(
        self,
        workflow_id: str,
        mode: RevisionMode,
        feedback: str,
    ) -> None:
        workflow = await self.repository.get_workflow(workflow_id)
        if workflow.state != WorkflowState.AWAITING_APPROVAL:
            raise ConflictError("Only an artifact awaiting approval can be revised")
        revision = RevisionRequest(
            workflow_id=workflow_id,
            mode=mode,
            feedback=feedback,
        )
        await self.repository.save_revision(revision)
        await self.repository.transition(
            workflow_id,
            WorkflowState.REVISION_REQUESTED,
            event_kind="revision.requested",
            payload={"mode": mode.value, "feedback": feedback},
        )
        await self.repository.enqueue(workflow_id, WorkKind.PREPARE)

    async def approve(
        self,
        workflow_id: str,
        artifact_version: int,
        manifest_digest: str,
        approved_by: str,
    ) -> None:
        workflow = await self.repository.get_workflow(workflow_id)
        if (
            workflow.state != WorkflowState.AWAITING_APPROVAL
            or workflow.active_artifact_version != artifact_version
        ):
            raise ConflictError("Approval references a stale or unavailable artifact")
        artifact = await self.repository.get_artifact(workflow_id, artifact_version)
        if artifact.manifest_digest != manifest_digest:
            raise ValidationError("Approval digest does not match the inspected artifact")
        adapter = cast(PrinterAdapter, self.printers.get(workflow.printer_name))
        plan = await self.repository.get_plan(workflow_id)
        await adapter.validate(artifact, plan.print_settings)
        await self.repository.approve_and_enqueue(
            ArtifactApproval(
                workflow_id=workflow_id,
                artifact_version=artifact_version,
                manifest_digest=manifest_digest,
                approved_by=approved_by,
            )
        )

    async def submit_print(self, workflow_id: str) -> None:
        workflow = await self.repository.get_workflow(workflow_id)
        approval = await self.repository.get_approval(workflow_id)
        artifact = await self.repository.get_artifact(
            workflow_id,
            approval.artifact_version,
        )
        if artifact.manifest_digest != approval.manifest_digest:
            raise ValidationError("Approved artifact digest no longer matches")
        adapter = cast(PrinterAdapter, self.printers.get(workflow.printer_name))
        plan = await self.repository.get_plan(workflow_id)
        await self.repository.transition(
            workflow_id,
            WorkflowState.SUBMITTING,
            event_kind="print.submitting",
        )
        idempotency_key = hashlib.sha256(
            f"{workflow_id}:{approval.manifest_digest}:{workflow.printer_name}".encode()
        ).hexdigest()
        job = await adapter.submit(
            workflow_id,
            artifact,
            plan.print_settings,
            idempotency_key,
        )
        await self.repository.save_job(job)
        await self.repository.transition(
            workflow_id,
            WorkflowState.QUEUED,
            event_kind="print.queued",
            payload={"job_id": job.id, "external_id": job.external_id},
        )
        await self.repository.enqueue(
            workflow_id,
            WorkKind.REFRESH_PRINT,
            delay_seconds=2,
        )

    async def refresh_print(self, workflow_id: str) -> None:
        workflow = await self.repository.get_workflow(workflow_id)
        job = await self.repository.get_latest_job(workflow_id)
        if job is None:
            raise ConflictError("Workflow has no printer job")
        adapter = cast(PrinterAdapter, self.printers.get(job.printer_name))
        refreshed = await adapter.status(job.external_id)
        await self.repository.save_job(refreshed)
        target = {
            PrintJobStatus.QUEUED: WorkflowState.QUEUED,
            PrintJobStatus.PRINTING: WorkflowState.PRINTING,
            PrintJobStatus.COMPLETED: WorkflowState.COMPLETED,
            PrintJobStatus.FAILED: WorkflowState.PRINT_FAILED,
            PrintJobStatus.CANCELLED: WorkflowState.CANCELLED,
        }[refreshed.status]
        if target != workflow.state:
            await self.repository.transition(
                workflow_id,
                target,
                event_kind=f"print.{refreshed.status.value}",
                payload={"message": refreshed.message},
            )
        if target in {WorkflowState.QUEUED, WorkflowState.PRINTING}:
            await self.repository.enqueue(
                workflow_id,
                WorkKind.REFRESH_PRINT,
                delay_seconds=2,
            )

    async def cancel(self, workflow_id: str) -> None:
        workflow = await self.repository.get_workflow(workflow_id)
        if workflow.state in {WorkflowState.QUEUED, WorkflowState.PRINTING}:
            job = await self.repository.get_latest_job(workflow_id)
            if job is None:
                raise ConflictError("Workflow has no printer job")
            adapter = cast(PrinterAdapter, self.printers.get(job.printer_name))
            cancelled = await adapter.cancel(job.external_id)
            await self.repository.save_job(cancelled)
        await self.repository.transition(
            workflow_id,
            WorkflowState.CANCELLED,
            event_kind="workflow.cancelled",
        )
