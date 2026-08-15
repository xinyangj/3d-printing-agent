from __future__ import annotations

import hashlib
import json
import shutil
from typing import cast

from printing_agent.artifact_store import ArtifactStore, sha256_file
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
    PrintWorkflow,
    RevisionMode,
    RevisionRequest,
    SelectedFileRole,
    SelectedSourceSummary,
    WorkflowState,
    WorkKind,
    canonical_digest,
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

    @staticmethod
    def _ensure_not_archived(workflow: PrintWorkflow) -> None:
        if workflow.archived_at is not None:
            raise ConflictError("Restore the archived workflow before changing it")

    async def prepare(self, workflow_id: str) -> ModelArtifact:
        workflow = await self.repository.get_workflow(workflow_id)
        PrintingApplication._ensure_not_archived(workflow)
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
                return await self._refine_current(
                    workflow.id,
                    revision.feedback,
                    revision.allowed_part_ids,
                )
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
            included_source_files = [
                item
                for item in decision.selected_files
                if item.role in {
                    SelectedFileRole.UNIQUE_PART,
                    SelectedFileRole.COMBINED_MODEL,
                }
            ]
            if decision.selected_files and included_source_files:
                if decision.decision != ModelDecision.USE_AS_IS:
                    raise ValidationError(
                        "Multipart source sets must be adopted before part-scoped revisions"
                    )
                return await self._adopt_source_set(
                    workflow_id,
                    candidate,
                    decision,
                    printer.build_volume,
                )
            selected_file = next(
                (item for item in candidate.files if item.id == str(decision.file_id)),
                None,
            )
            if selected_file is None:
                raise ValidationError("Selected source file is no longer available")
            source_format = selected_file.format.casefold().lstrip(".")
            if source_format not in {"stl", "3mf"}:
                raise ValidationError(
                    f"Source format '{source_format}' is discoverable but not yet importable"
                )
            source_path = (
                self.settings.candidate_cache_dir
                / "models"
                / "incoming"
                / workflow_id
                / f"{decision.file_id}.{source_format}"
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
                    / f"{inspection.source_digest}.{source_format}"
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
                if source_format == "3mf":
                    return await self.model_pipeline.adopt_existing_3mf(
                        workflow_id,
                        inspection.cached_path,
                        provenance,
                        printer.build_volume,
                    )
                return await self.model_pipeline.adopt_existing(
                    workflow_id,
                    inspection.cached_path,
                    provenance,
                    printer.build_volume,
                )
            if source_format != "stl":
                raise ValidationError(
                    "Structured 3MF sources must be adopted before part-scoped revision"
                )

            selected_source = SelectedSourceSummary(
                filename=f"source.{source_format}",
                format=source_format,
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

    async def _adopt_source_set(
        self,
        workflow_id: str,
        candidate,
        decision: DiscoveryDecision,
        build_volume,
    ) -> ModelArtifact:
        selected = [
            item
            for item in decision.selected_files
            if item.role in {
                SelectedFileRole.UNIQUE_PART,
                SelectedFileRole.COMBINED_MODEL,
            }
        ]
        candidate_files = {item.id: item for item in candidate.files}
        await self.repository.transition(
            workflow_id,
            WorkflowState.SOURCE_VALIDATION,
            event_kind="source_set.download_started",
            payload={
                "candidate_id": candidate.id,
                "file_ids": [item.file_id for item in selected],
            },
        )
        prepared = []
        incoming = (
            self.settings.candidate_cache_dir
            / "models"
            / "incoming"
            / workflow_id
        )
        try:
            for selection in selected:
                candidate_file = candidate_files[selection.file_id]
                source_format = candidate_file.format.casefold().lstrip(".")
                if source_format != "stl":
                    raise ValidationError(
                        "Multi-file source sets currently require STL part files"
                    )
                source_path = incoming / f"{candidate_file.id}.{source_format}"
                await self.catalog.download_file(candidate, candidate_file.id, source_path)
                inspection = await self.source_inspector.inspect(
                    workflow_id,
                    candidate.id,
                    candidate_file.id,
                    source_path,
                    build_volume,
                )
                await self.repository.save_source_inspection(inspection)
                if not inspection.accepted:
                    raise ValidationError(
                        inspection.rejection_reason
                        or f"Source part '{candidate_file.name}' is invalid"
                    )
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
                prepared.append(
                    (
                        selection,
                        candidate_file,
                        cache_path,
                        inspection.mesh,
                    )
                )
        except Exception:
            shutil.rmtree(incoming, ignore_errors=True)
            raise
        finally:
            if incoming.exists() and not any(incoming.iterdir()):
                incoming.rmdir()

        source_set_digest = canonical_digest(
            {
                "files": [
                    {
                        "file_id": selection.file_id,
                        "digest": hashlib.sha256(path.read_bytes()).hexdigest(),
                    }
                    for selection, _, path, _ in prepared
                ],
                "shared_scale": decision.shared_scale,
            }
        )
        provenance = ArtifactProvenance(
            kind="catalog",
            candidate_id=candidate.id,
            source_url=candidate.source_url,
            creator=candidate.creator,
            license=candidate.license,
            source_digest=source_set_digest,
        )
        await self.repository.transition(
            workflow_id,
            WorkflowState.VALIDATING,
            event_kind="source_set.adopting",
            payload={"part_count": len(prepared)},
        )
        return await self.model_pipeline.adopt_existing_set(
            workflow_id,
            prepared,
            provenance,
            build_volume,
            shared_scale=decision.shared_scale,
            description=f"{candidate.introduction}\n{candidate.instructions}",
            classification=decision.selected_files,
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

    async def _refine_current(
        self,
        workflow_id: str,
        feedback: str,
        allowed_part_ids: list[str] | None,
    ) -> ModelArtifact:
        workflow = await self.repository.get_workflow(workflow_id)
        if workflow.active_artifact_version is None:
            raise ConflictError("Workflow has no active artifact")
        artifact = await self.repository.get_artifact(
            workflow_id,
            workflow.active_artifact_version,
        )
        if artifact.project is not None and len(artifact.project.parts) > 1:
            part_ids = {part.id for part in artifact.project.parts}
            if allowed_part_ids is not None and not set(allowed_part_ids).issubset(part_ids):
                unknown = sorted(set(allowed_part_ids) - part_ids)
                raise ValidationError(f"Unknown edit-scope parts: {', '.join(unknown)}")
            reference_part_id = (
                allowed_part_ids[0] if allowed_part_ids else artifact.project.parts[0].id
            )
            part = next(
                (
                    candidate
                    for candidate in artifact.project.parts
                    if candidate.id == reference_part_id
                ),
                None,
            )
            assert part is not None
            adapter = cast(PrinterAdapter, self.printers.get(workflow.printer_name))
            printer = await adapter.capabilities()
            selected_source = SelectedSourceSummary(
                filename=f"{reference_part_id}.stl",
                candidate_id=artifact.provenance.candidate_id or workflow_id,
                file_id=reference_part_id,
                title=part.name,
                creator=artifact.provenance.creator or "unknown",
                license=artifact.provenance.license or "unknown",
                attribution_url=artifact.provenance.source_url or "local-artifact",
                source_digest=sha256_file(
                    artifact.model_path.parent / f"{reference_part_id}.stl"
                ),
                mesh=artifact.part_meshes[reference_part_id],
            )
            old_handoff = ModelingHandoff(
                workflow_id=workflow_id,
                version=max(workflow.active_handoff_version or 1, 1),
                requirement=workflow.requirement,
                model_plan=await self.repository.get_plan(workflow_id),
                decision=ModelDecision.MODIFY,
                selected_source=selected_source,
                target_printer=printer,
                discovery_rationale="Refine one retained source-set part.",
                evidence_digests=[selected_source.source_digest],
            ).with_digest()
            current_source = 'import("source.stl");\n'
        else:
            if workflow.active_handoff_version is None:
                raise ConflictError("Current artifact was not produced by a modeling handoff")
            old_handoff = await self.repository.get_handoff(
                workflow_id,
                workflow.active_handoff_version,
            )
            if artifact.source_path is None or not artifact.source_path.is_file():
                raise ConflictError(
                    "This artifact has no editable OpenSCAD source; search for a new base instead"
                )
            current_source = artifact.source_path.read_text(encoding="utf-8")
        version = await self.repository.next_handoff_version(workflow_id)
        handoff = old_handoff.model_copy(
            update={
                "version": version,
                "required_changes": [*old_handoff.required_changes, feedback],
                "digest": None,
            }
        ).with_digest()
        await self.repository.save_handoff(handoff)
        await self.repository.patch_workflow(
            workflow_id,
            modeling_session_id=None,
            event_kind="revision.modeling_session_replaced",
            payload={
                "previous_handoff_version": old_handoff.version,
                "handoff_version": version,
                "artifact_version": artifact.version,
            },
        )
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
        return await self.modeling.revise(
            handoff,
            artifact,
            feedback,
            current_source,
            old_handoff,
            allowed_part_ids,
        )

    async def request_revision(
        self,
        workflow_id: str,
        mode: RevisionMode,
        feedback: str,
        allowed_part_ids: list[str] | None = None,
    ) -> None:
        workflow = await self.repository.get_workflow(workflow_id)
        PrintingApplication._ensure_not_archived(workflow)
        if workflow.state not in {
            WorkflowState.AWAITING_APPROVAL,
            WorkflowState.APPROVED,
            WorkflowState.PREPARATION_FAILED,
        }:
            raise ConflictError("Only an unsubmitted or failed preparation can be revised")
        if mode == RevisionMode.REFINE_CURRENT:
            if workflow.active_artifact_version is None:
                raise ConflictError("Workflow has no active artifact to refine")
            artifact = await self.repository.get_artifact(
                workflow_id,
                workflow.active_artifact_version,
            )
            if artifact.source_path is None or not artifact.source_path.is_file():
                raise ConflictError(
                    "This artifact has no editable OpenSCAD source; search for a new base instead"
                )
            if artifact.project is not None and len(artifact.project.parts) > 1:
                part_ids = {part.id for part in artifact.project.parts}
                if allowed_part_ids is not None and not set(allowed_part_ids).issubset(
                    part_ids
                ):
                    unknown = sorted(set(allowed_part_ids) - part_ids)
                    raise ValidationError(
                        f"Unknown edit-scope parts: {', '.join(unknown)}"
                    )
            elif allowed_part_ids is not None:
                raise ValidationError(
                    "Explicit part scopes are available only for multipart artifacts"
                )
        revision = RevisionRequest(
            workflow_id=workflow_id,
            mode=mode,
            feedback=feedback,
            allowed_part_ids=allowed_part_ids,
        )
        await self.repository.request_revision(revision)

    async def approve(
        self,
        workflow_id: str,
        artifact_version: int,
        manifest_digest: str,
        approved_by: str,
    ) -> None:
        workflow = await self.repository.get_workflow(workflow_id)
        PrintingApplication._ensure_not_archived(workflow)
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
        await self.repository.approve_artifact(
            ArtifactApproval(
                workflow_id=workflow_id,
                artifact_version=artifact_version,
                manifest_digest=manifest_digest,
                approved_by=approved_by,
            )
        )

    async def request_print(self, workflow_id: str) -> None:
        workflow = await self.repository.get_workflow(workflow_id)
        PrintingApplication._ensure_not_archived(workflow)
        await self.repository.enqueue_print_submission(workflow_id)

    async def archive_workflow(self, workflow_id: str) -> PrintWorkflow:
        return await self.repository.archive_workflow(workflow_id)

    async def restore_workflow(self, workflow_id: str) -> PrintWorkflow:
        return await self.repository.restore_workflow(workflow_id)

    async def submit_print(self, workflow_id: str) -> None:
        workflow = await self.repository.get_workflow(workflow_id)
        PrintingApplication._ensure_not_archived(workflow)
        if workflow.state in {
            WorkflowState.QUEUED,
            WorkflowState.PRINTING,
            WorkflowState.COMPLETED,
        }:
            if await self.repository.get_latest_job(workflow_id) is not None:
                return
        if workflow.state != WorkflowState.SUBMITTING:
            raise ConflictError("Print submission is not active for this workflow")
        approval = await self.repository.get_approval(workflow_id)
        artifact = await self.repository.get_artifact(
            workflow_id,
            approval.artifact_version,
        )
        if artifact.manifest_digest != approval.manifest_digest:
            raise ValidationError("Approved artifact digest no longer matches")
        adapter = cast(PrinterAdapter, self.printers.get(workflow.printer_name))
        plan = await self.repository.get_plan(workflow_id)
        idempotency_key = hashlib.sha256(
            f"{workflow_id}:{approval.manifest_digest}:{workflow.printer_name}".encode()
        ).hexdigest()
        job = await adapter.submit(
            workflow_id,
            artifact,
            plan.print_settings,
            idempotency_key,
        )
        await self.repository.finalize_print_submission(job)

    async def copy_workflow(self, workflow_id: str) -> PrintWorkflow:
        source_workflow = await self.repository.get_workflow(workflow_id)
        PrintingApplication._ensure_not_archived(source_workflow)
        if source_workflow.active_artifact_version is None:
            raise ConflictError("Workflow has no artifact to copy")
        source_artifact = await self.repository.get_artifact(
            workflow_id,
            source_workflow.active_artifact_version,
        )
        source_handoff = (
            await self.repository.get_handoff(
                workflow_id,
                source_workflow.active_handoff_version,
            )
            if source_workflow.active_handoff_version is not None
            else None
        )
        plan = await self.repository.get_plan(workflow_id)
        copied_workflow = PrintWorkflow(
            requirement=source_workflow.requirement,
            printer_name=source_workflow.printer_name,
            state=WorkflowState.AWAITING_APPROVAL,
            active_handoff_version=1 if source_handoff is not None else None,
            active_artifact_version=1,
        )
        copied_handoff = (
            source_handoff.model_copy(
                update={
                    "workflow_id": copied_workflow.id,
                    "version": 1,
                    "digest": None,
                }
            ).with_digest()
            if source_handoff is not None
            else None
        )
        try:
            copied_from = {
                "workflow_id": workflow_id,
                "artifact_version": source_artifact.version,
                "manifest_digest": source_artifact.manifest_digest,
            }
            if source_artifact.schema_version == "2":
                source_directory = self.artifacts.artifact_directory(
                    workflow_id,
                    source_artifact.version,
                )
                staging = (
                    self.artifacts.workflow_root(copied_workflow.id)
                    / "staging"
                    / "artifact-1"
                )
                shutil.copytree(source_directory, staging)
                manifest_path = staging / "manifest.json"
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                manifest.update(
                    {
                        "workflow_id": copied_workflow.id,
                        "artifact_version": 1,
                        "copied_from": copied_from,
                        "handoff_digest": (
                            copied_handoff.digest if copied_handoff is not None else None
                        ),
                    }
                )
                manifest.pop("manifest_digest", None)
                manifest_digest = canonical_digest(manifest)
                manifest["manifest_digest"] = manifest_digest
                manifest_path.write_text(
                    json.dumps(manifest, sort_keys=True, indent=2),
                    encoding="utf-8",
                )
                adopted = self.artifacts.adopt_project(copied_workflow.id, 1, staging)
                source_relative = (
                    source_artifact.source_path.relative_to(source_directory)
                    if source_artifact.source_path is not None
                    else None
                )
                model_relative = source_artifact.model_path.relative_to(source_directory)
                project_relative = (
                    source_artifact.project_path.relative_to(source_directory)
                    if source_artifact.project_path is not None
                    else None
                )
                three_mf_relative = (
                    source_artifact.three_mf_path.relative_to(source_directory)
                    if source_artifact.three_mf_path is not None
                    else None
                )
                copied_artifact = source_artifact.model_copy(
                    update={
                        "workflow_id": copied_workflow.id,
                        "version": 1,
                        "source_path": adopted / source_relative if source_relative else None,
                        "model_path": adopted / model_relative,
                        "project_path": adopted / project_relative if project_relative else None,
                        "three_mf_path": (
                            adopted / three_mf_relative if three_mf_relative else None
                        ),
                        "manifest_digest": manifest_digest,
                    }
                )
            else:
                manifest = {
                    "workflow_id": copied_workflow.id,
                    "artifact_version": 1,
                    "source_digest": source_artifact.source_digest,
                    "model_digest": source_artifact.model_digest,
                    "mesh": source_artifact.mesh.model_dump(mode="json"),
                    "provenance": source_artifact.provenance.model_dump(mode="json"),
                    "copied_from": copied_from,
                }
                if copied_handoff is not None:
                    manifest["handoff_digest"] = copied_handoff.digest
                manifest_digest = canonical_digest(manifest)
                manifest["manifest_digest"] = manifest_digest
                adopted_source, adopted_model, _ = self.artifacts.adopt(
                    copied_workflow.id,
                    1,
                    source_artifact.source_path,
                    source_artifact.model_path,
                    manifest,
                )
                copied_artifact = source_artifact.model_copy(
                    update={
                        "workflow_id": copied_workflow.id,
                        "version": 1,
                        "source_path": adopted_source,
                        "model_path": adopted_model,
                        "manifest_digest": manifest_digest,
                    }
                )
            await self.repository.create_copy(
                copied_workflow,
                plan,
                copied_handoff,
                copied_artifact,
                source_workflow_id=workflow_id,
                source_artifact_version=source_artifact.version,
            )
            return copied_workflow
        except Exception:
            shutil.rmtree(
                self.artifacts.workflow_root(copied_workflow.id),
                ignore_errors=True,
            )
            raise

    async def refresh_print(self, workflow_id: str) -> None:
        workflow = await self.repository.get_workflow(workflow_id)
        PrintingApplication._ensure_not_archived(workflow)
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
        PrintingApplication._ensure_not_archived(workflow)
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
