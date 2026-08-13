from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any, Literal

import aiosqlite

from printing_agent.domain import (
    ArtifactApproval,
    CandidatePageInspection,
    DiscoveryDecision,
    ModelArtifact,
    ModelingHandoff,
    ModelPlan,
    PrintJob,
    PrintWorkflow,
    RevisionMode,
    RevisionRequest,
    SearchRound,
    SelectedSourceInspection,
    WorkflowEvent,
    WorkflowState,
    WorkItem,
    WorkKind,
    assert_transition,
    utc_now,
)
from printing_agent.errors import ConflictError, NotFoundError

_SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS workflows (
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

CREATE TABLE IF NOT EXISTS workflow_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_id TEXT NOT NULL REFERENCES workflows(id),
    kind TEXT NOT NULL,
    state TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_workflow_events_workflow
ON workflow_events(workflow_id, id);

CREATE TABLE IF NOT EXISTS model_plans (
    workflow_id TEXT PRIMARY KEY REFERENCES workflows(id),
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS search_rounds (
    id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL REFERENCES workflows(id),
    query_key TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    candidates_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(workflow_id, query_key)
);

CREATE TABLE IF NOT EXISTS page_inspections (
    workflow_id TEXT NOT NULL REFERENCES workflows(id),
    candidate_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (workflow_id, candidate_id)
);

CREATE TABLE IF NOT EXISTS discovery_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_id TEXT NOT NULL REFERENCES workflows(id),
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS source_inspections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_id TEXT NOT NULL REFERENCES workflows(id),
    candidate_id TEXT NOT NULL,
    file_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS modeling_handoffs (
    workflow_id TEXT NOT NULL REFERENCES workflows(id),
    version INTEGER NOT NULL,
    digest TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (workflow_id, version),
    UNIQUE(workflow_id, digest)
);

CREATE TABLE IF NOT EXISTS source_attempts (
    workflow_id TEXT NOT NULL REFERENCES workflows(id),
    handoff_version INTEGER NOT NULL,
    attempt INTEGER NOT NULL,
    status TEXT NOT NULL,
    source_digest TEXT NOT NULL,
    diagnostics TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (workflow_id, handoff_version, attempt)
);

CREATE TABLE IF NOT EXISTS artifacts (
    workflow_id TEXT NOT NULL REFERENCES workflows(id),
    version INTEGER NOT NULL,
    manifest_digest TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (workflow_id, version),
    UNIQUE(workflow_id, manifest_digest)
);

CREATE TABLE IF NOT EXISTS approvals (
    workflow_id TEXT PRIMARY KEY REFERENCES workflows(id),
    artifact_version INTEGER NOT NULL,
    manifest_digest TEXT NOT NULL,
    approved_by TEXT NOT NULL,
    approved_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS revision_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_id TEXT NOT NULL REFERENCES workflows(id),
    mode TEXT NOT NULL,
    feedback TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS print_jobs (
    id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL REFERENCES workflows(id),
    printer_name TEXT NOT NULL,
    external_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_print_jobs_workflow
ON print_jobs(workflow_id, created_at DESC);

CREATE TABLE IF NOT EXISTS work_items (
    id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL REFERENCES workflows(id),
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    available_at TEXT NOT NULL,
    attempts INTEGER NOT NULL,
    leased_until TEXT,
    last_error TEXT
);

CREATE INDEX IF NOT EXISTS idx_work_items_ready
ON work_items(status, available_at);
"""


def _json(value: Any) -> str:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


class WorkflowRepository:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path

    async def initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.database_path) as db:
            await db.executescript(_SCHEMA)
            await db.commit()

    async def _connect(self) -> aiosqlite.Connection:
        db = await aiosqlite.connect(self.database_path)
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA foreign_keys = ON")
        return db

    @staticmethod
    def _workflow_from_row(row: aiosqlite.Row) -> PrintWorkflow:
        return PrintWorkflow.model_validate(dict(row))

    async def create_workflow(self, requirement: str, printer_name: str) -> PrintWorkflow:
        workflow = PrintWorkflow(requirement=requirement, printer_name=printer_name)
        work_item = WorkItem(workflow_id=workflow.id, kind=WorkKind.PREPARE)
        db = await self._connect()
        try:
            await db.execute("BEGIN IMMEDIATE")
            await db.execute(
                """
                INSERT INTO workflows (
                    id, requirement, printer_name, state, version,
                    discovery_session_id, modeling_session_id,
                    active_handoff_version, active_artifact_version,
                    failure_code, failure_message, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, NULL, NULL, ?, ?)
                """,
                (
                    workflow.id,
                    workflow.requirement,
                    workflow.printer_name,
                    workflow.state.value,
                    workflow.version,
                    workflow.created_at.isoformat(),
                    workflow.updated_at.isoformat(),
                ),
            )
            await self._insert_event(
                db,
                workflow.id,
                "workflow.created",
                workflow.state,
                {"printer_name": printer_name},
            )
            await self._insert_work_item(db, work_item)
            await db.commit()
            return workflow
        except Exception:
            await db.rollback()
            raise
        finally:
            await db.close()

    async def get_workflow(self, workflow_id: str) -> PrintWorkflow:
        db = await self._connect()
        try:
            cursor = await db.execute("SELECT * FROM workflows WHERE id = ?", (workflow_id,))
            row = await cursor.fetchone()
            if row is None:
                raise NotFoundError(f"Workflow '{workflow_id}' was not found")
            return self._workflow_from_row(row)
        finally:
            await db.close()

    async def list_workflows(self) -> list[PrintWorkflow]:
        db = await self._connect()
        try:
            cursor = await db.execute(
                "SELECT * FROM workflows ORDER BY updated_at DESC, created_at DESC"
            )
            return [self._workflow_from_row(row) for row in await cursor.fetchall()]
        finally:
            await db.close()

    async def transition(
        self,
        workflow_id: str,
        target: WorkflowState,
        *,
        event_kind: str | None = None,
        payload: dict[str, Any] | None = None,
        failure_code: str | None = None,
        failure_message: str | None = None,
    ) -> PrintWorkflow:
        db = await self._connect()
        try:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                "SELECT * FROM workflows WHERE id = ?",
                (workflow_id,),
            )
            row = await cursor.fetchone()
            if row is None:
                raise NotFoundError(f"Workflow '{workflow_id}' was not found")
            current = self._workflow_from_row(row)
            assert_transition(current.state, target)
            now = utc_now()
            cursor = await db.execute(
                """
                UPDATE workflows
                SET state = ?, version = version + 1, failure_code = ?,
                    failure_message = ?, updated_at = ?
                WHERE id = ? AND version = ?
                """,
                (
                    target.value,
                    failure_code,
                    failure_message,
                    now.isoformat(),
                    workflow_id,
                    current.version,
                ),
            )
            if cursor.rowcount != 1:
                raise ConflictError("Workflow was modified by another worker")
            await self._insert_event(
                db,
                workflow_id,
                event_kind or f"workflow.{target.value}",
                target,
                payload or {},
            )
            await db.commit()
            return current.model_copy(
                update={
                    "state": target,
                    "version": current.version + 1,
                    "failure_code": failure_code,
                    "failure_message": failure_message,
                    "updated_at": now,
                }
            )
        except Exception:
            await db.rollback()
            raise
        finally:
            await db.close()

    async def patch_workflow(
        self,
        workflow_id: str,
        *,
        discovery_session_id: str | None | Literal[False] = False,
        modeling_session_id: str | None | Literal[False] = False,
        active_handoff_version: int | None | Literal[False] = False,
        active_artifact_version: int | None | Literal[False] = False,
        event_kind: str = "workflow.updated",
        payload: dict[str, Any] | None = None,
    ) -> PrintWorkflow:
        updates: dict[str, Any] = {}
        for key, value in (
            ("discovery_session_id", discovery_session_id),
            ("modeling_session_id", modeling_session_id),
            ("active_handoff_version", active_handoff_version),
            ("active_artifact_version", active_artifact_version),
        ):
            if value is not False:
                updates[key] = value
        if not updates:
            return await self.get_workflow(workflow_id)

        db = await self._connect()
        try:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute("SELECT * FROM workflows WHERE id = ?", (workflow_id,))
            row = await cursor.fetchone()
            if row is None:
                raise NotFoundError(f"Workflow '{workflow_id}' was not found")
            current = self._workflow_from_row(row)
            now = utc_now()
            assignments = ", ".join(f"{column} = ?" for column in updates)
            values = [*updates.values(), now.isoformat(), workflow_id, current.version]
            cursor = await db.execute(
                f"""
                UPDATE workflows SET {assignments}, version = version + 1, updated_at = ?
                WHERE id = ? AND version = ?
                """,
                values,
            )
            if cursor.rowcount != 1:
                raise ConflictError("Workflow was modified by another worker")
            await self._insert_event(
                db,
                workflow_id,
                event_kind,
                current.state,
                payload or updates,
            )
            await db.commit()
            return current.model_copy(
                update={**updates, "version": current.version + 1, "updated_at": now}
            )
        except Exception:
            await db.rollback()
            raise
        finally:
            await db.close()

    async def save_plan(self, workflow_id: str, plan: ModelPlan) -> None:
        await self._upsert_payload(
            "model_plans",
            workflow_id,
            plan,
            conflict_column="workflow_id",
        )

    async def get_plan(self, workflow_id: str) -> ModelPlan:
        payload = await self._get_payload("model_plans", "workflow_id", workflow_id)
        return ModelPlan.model_validate_json(payload)

    async def save_search_round(
        self,
        search_round: SearchRound,
        candidates: list[Any],
    ) -> None:
        query_key = f"{search_round.query.strip().casefold()}:{search_round.page}"
        db = await self._connect()
        try:
            await db.execute(
                """
                INSERT INTO search_rounds (
                    id, workflow_id, query_key, payload_json, candidates_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    search_round.id,
                    search_round.workflow_id,
                    query_key,
                    search_round.model_dump_json(),
                    _json(candidates),
                    search_round.created_at.isoformat(),
                ),
            )
            await db.commit()
        except aiosqlite.IntegrityError as exc:
            raise ConflictError("Equivalent search query was already issued") from exc
        finally:
            await db.close()

    async def get_search_round(
        self,
        search_round_id: str,
    ) -> tuple[SearchRound, list[dict[str, Any]]]:
        db = await self._connect()
        try:
            cursor = await db.execute(
                "SELECT payload_json, candidates_json FROM search_rounds WHERE id = ?",
                (search_round_id,),
            )
            row = await cursor.fetchone()
            if row is None:
                raise NotFoundError(f"Search round '{search_round_id}' was not found")
            return (
                SearchRound.model_validate_json(row["payload_json"]),
                json.loads(row["candidates_json"]),
            )
        finally:
            await db.close()

    async def count_search_rounds(self, workflow_id: str) -> int:
        return await self._count("search_rounds", workflow_id)

    async def save_page_inspection(self, inspection: CandidatePageInspection) -> None:
        db = await self._connect()
        try:
            await db.execute(
                """
                INSERT INTO page_inspections (
                    workflow_id, candidate_id, payload_json, created_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(workflow_id, candidate_id) DO UPDATE SET
                    payload_json = excluded.payload_json,
                    created_at = excluded.created_at
                """,
                (
                    inspection.workflow_id,
                    inspection.candidate.id,
                    inspection.model_dump_json(),
                    inspection.inspected_at.isoformat(),
                ),
            )
            await db.commit()
        finally:
            await db.close()

    async def get_page_inspection(
        self,
        workflow_id: str,
        candidate_id: str,
    ) -> CandidatePageInspection:
        db = await self._connect()
        try:
            cursor = await db.execute(
                """
                SELECT payload_json FROM page_inspections
                WHERE workflow_id = ? AND candidate_id = ?
                """,
                (workflow_id, candidate_id),
            )
            row = await cursor.fetchone()
            if row is None:
                raise NotFoundError(f"Candidate '{candidate_id}' has not been inspected")
            return CandidatePageInspection.model_validate_json(row["payload_json"])
        finally:
            await db.close()

    async def count_page_inspections(self, workflow_id: str) -> int:
        return await self._count("page_inspections", workflow_id)

    async def save_discovery_decision(
        self,
        workflow_id: str,
        decision: DiscoveryDecision,
    ) -> None:
        await self._insert_versioned_payload(
            "discovery_decisions",
            workflow_id,
            decision,
        )

    async def save_source_inspection(self, inspection: SelectedSourceInspection) -> None:
        db = await self._connect()
        try:
            await db.execute(
                """
                INSERT INTO source_inspections (
                    workflow_id, candidate_id, file_id, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    inspection.workflow_id,
                    inspection.candidate_id,
                    inspection.file_id,
                    inspection.model_dump_json(),
                    inspection.inspected_at.isoformat(),
                ),
            )
            await db.commit()
        finally:
            await db.close()

    async def count_rejected_sources(self, workflow_id: str) -> int:
        db = await self._connect()
        try:
            cursor = await db.execute(
                "SELECT payload_json FROM source_inspections WHERE workflow_id = ?",
                (workflow_id,),
            )
            rows = await cursor.fetchall()
            return sum(
                not SelectedSourceInspection.model_validate_json(row["payload_json"]).accepted
                for row in rows
            )
        finally:
            await db.close()

    async def next_handoff_version(self, workflow_id: str) -> int:
        return await self._next_version("modeling_handoffs", workflow_id)

    async def save_handoff(self, handoff: ModelingHandoff) -> None:
        if not handoff.digest:
            raise ValueError("Modeling handoff must be signed with a digest")
        db = await self._connect()
        try:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                "SELECT version FROM workflows WHERE id = ?",
                (handoff.workflow_id,),
            )
            workflow_row = await cursor.fetchone()
            if workflow_row is None:
                raise NotFoundError(f"Workflow '{handoff.workflow_id}' was not found")
            workflow_version = int(workflow_row["version"])
            await db.execute(
                """
                INSERT INTO modeling_handoffs (
                    workflow_id, version, digest, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    handoff.workflow_id,
                    handoff.version,
                    handoff.digest,
                    handoff.model_dump_json(),
                    utc_now().isoformat(),
                ),
            )
            cursor = await db.execute(
                """
                UPDATE workflows
                SET active_handoff_version = ?, version = version + 1, updated_at = ?
                WHERE id = ? AND version = ?
                """,
                (
                    handoff.version,
                    utc_now().isoformat(),
                    handoff.workflow_id,
                    workflow_version,
                ),
            )
            if cursor.rowcount != 1:
                raise ConflictError("Workflow was modified while saving the handoff")
            await db.commit()
        except Exception:
            await db.rollback()
            raise
        finally:
            await db.close()

    async def get_handoff(self, workflow_id: str, version: int) -> ModelingHandoff:
        payload = await self._get_composite_payload(
            "modeling_handoffs",
            workflow_id,
            version,
        )
        return ModelingHandoff.model_validate_json(payload)

    async def next_source_attempt(self, workflow_id: str, handoff_version: int) -> int:
        db = await self._connect()
        try:
            cursor = await db.execute(
                """
                SELECT COALESCE(MAX(attempt), 0) + 1 AS next_attempt
                FROM source_attempts
                WHERE workflow_id = ? AND handoff_version = ?
                """,
                (workflow_id, handoff_version),
            )
            row = await cursor.fetchone()
            return int(row["next_attempt"])
        finally:
            await db.close()

    async def save_source_attempt(
        self,
        workflow_id: str,
        handoff_version: int,
        attempt: int,
        status: str,
        source_digest: str,
        diagnostics: str | None = None,
    ) -> None:
        db = await self._connect()
        try:
            await db.execute(
                """
                INSERT INTO source_attempts (
                    workflow_id, handoff_version, attempt, status,
                    source_digest, diagnostics, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(workflow_id, handoff_version, attempt) DO UPDATE SET
                    status = excluded.status,
                    diagnostics = excluded.diagnostics
                """,
                (
                    workflow_id,
                    handoff_version,
                    attempt,
                    status,
                    source_digest,
                    diagnostics,
                    utc_now().isoformat(),
                ),
            )
            await db.commit()
        finally:
            await db.close()

    async def count_source_attempts(self, workflow_id: str, handoff_version: int) -> int:
        db = await self._connect()
        try:
            cursor = await db.execute(
                """
                SELECT COUNT(*) AS count FROM source_attempts
                WHERE workflow_id = ? AND handoff_version = ?
                """,
                (workflow_id, handoff_version),
            )
            return int((await cursor.fetchone())["count"])
        finally:
            await db.close()

    async def next_artifact_version(self, workflow_id: str) -> int:
        return await self._next_version("artifacts", workflow_id)

    async def save_artifact(self, artifact: ModelArtifact) -> None:
        db = await self._connect()
        try:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                "SELECT version FROM workflows WHERE id = ?",
                (artifact.workflow_id,),
            )
            workflow_row = await cursor.fetchone()
            if workflow_row is None:
                raise NotFoundError(f"Workflow '{artifact.workflow_id}' was not found")
            workflow_version = int(workflow_row["version"])
            await db.execute(
                """
                INSERT INTO artifacts (
                    workflow_id, version, manifest_digest, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    artifact.workflow_id,
                    artifact.version,
                    artifact.manifest_digest,
                    artifact.model_dump_json(),
                    artifact.created_at.isoformat(),
                ),
            )
            cursor = await db.execute(
                """
                UPDATE workflows
                SET active_artifact_version = ?, version = version + 1, updated_at = ?
                WHERE id = ? AND version = ?
                """,
                (
                    artifact.version,
                    utc_now().isoformat(),
                    artifact.workflow_id,
                    workflow_version,
                ),
            )
            if cursor.rowcount != 1:
                raise ConflictError("Workflow was modified while saving the artifact")
            await db.commit()
        except Exception:
            await db.rollback()
            raise
        finally:
            await db.close()

    async def get_artifact(self, workflow_id: str, version: int) -> ModelArtifact:
        payload = await self._get_composite_payload("artifacts", workflow_id, version)
        return ModelArtifact.model_validate_json(payload)

    async def approve_artifact(self, approval: ArtifactApproval) -> None:
        db = await self._connect()
        try:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                """
                SELECT state, version, active_artifact_version
                FROM workflows WHERE id = ?
                """,
                (approval.workflow_id,),
            )
            workflow = await cursor.fetchone()
            if workflow is None:
                raise NotFoundError(f"Workflow '{approval.workflow_id}' was not found")
            if (
                workflow["state"] != WorkflowState.AWAITING_APPROVAL.value
                or workflow["active_artifact_version"] != approval.artifact_version
            ):
                raise ConflictError("Approval references a stale workflow artifact")
            cursor = await db.execute(
                """
                SELECT manifest_digest FROM artifacts
                WHERE workflow_id = ? AND version = ?
                """,
                (approval.workflow_id, approval.artifact_version),
            )
            artifact = await cursor.fetchone()
            if artifact is None or artifact["manifest_digest"] != approval.manifest_digest:
                raise ConflictError("Approval manifest does not match the active artifact")
            await db.execute(
                """
                INSERT INTO approvals (
                    workflow_id, artifact_version, manifest_digest,
                    approved_by, approved_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    approval.workflow_id,
                    approval.artifact_version,
                    approval.manifest_digest,
                    approval.approved_by,
                    approval.approved_at.isoformat(),
                ),
            )
            cursor = await db.execute(
                """
                UPDATE workflows
                SET state = ?, version = version + 1, updated_at = ?
                WHERE id = ? AND version = ?
                """,
                (
                    WorkflowState.APPROVED.value,
                    utc_now().isoformat(),
                    approval.workflow_id,
                    workflow["version"],
                ),
            )
            if cursor.rowcount != 1:
                raise ConflictError("Workflow changed during approval")
            await self._insert_event(
                db,
                approval.workflow_id,
                "artifact.approved",
                WorkflowState.APPROVED,
                {
                    "artifact_version": approval.artifact_version,
                    "manifest_digest": approval.manifest_digest,
                },
            )
            await db.commit()
        except aiosqlite.IntegrityError as exc:
            await db.rollback()
            raise ConflictError("Workflow already has an approval") from exc
        except Exception:
            await db.rollback()
            raise
        finally:
            await db.close()

    async def enqueue_print_submission(self, workflow_id: str) -> WorkItem:
        item = WorkItem(workflow_id=workflow_id, kind=WorkKind.SUBMIT)
        db = await self._connect()
        try:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                "SELECT state, version, active_artifact_version FROM workflows WHERE id = ?",
                (workflow_id,),
            )
            workflow = await cursor.fetchone()
            if workflow is None:
                raise NotFoundError(f"Workflow '{workflow_id}' was not found")
            if workflow["state"] != WorkflowState.APPROVED.value:
                raise ConflictError("Only an approved artifact can be sent to the printer")
            cursor = await db.execute(
                """
                SELECT artifact_version, manifest_digest FROM approvals
                WHERE workflow_id = ?
                """,
                (workflow_id,),
            )
            approval = await cursor.fetchone()
            if (
                approval is None
                or approval["artifact_version"] != workflow["active_artifact_version"]
            ):
                raise ConflictError("The active artifact does not have a valid approval")
            cursor = await db.execute(
                """
                SELECT manifest_digest FROM artifacts
                WHERE workflow_id = ? AND version = ?
                """,
                (workflow_id, approval["artifact_version"]),
            )
            artifact = await cursor.fetchone()
            if artifact is None or artifact["manifest_digest"] != approval["manifest_digest"]:
                raise ConflictError("The approved artifact manifest no longer matches")
            now = utc_now()
            cursor = await db.execute(
                """
                UPDATE workflows
                SET state = ?, version = version + 1, updated_at = ?
                WHERE id = ? AND version = ?
                """,
                (
                    WorkflowState.SUBMITTING.value,
                    now.isoformat(),
                    workflow_id,
                    workflow["version"],
                ),
            )
            if cursor.rowcount != 1:
                raise ConflictError("Workflow changed during print submission")
            await self._insert_event(
                db,
                workflow_id,
                "print.submitting",
                WorkflowState.SUBMITTING,
                {
                    "artifact_version": approval["artifact_version"],
                    "manifest_digest": approval["manifest_digest"],
                },
            )
            await self._insert_work_item(db, item)
            await db.commit()
            return item
        except Exception:
            await db.rollback()
            raise
        finally:
            await db.close()

    async def get_approval(self, workflow_id: str) -> ArtifactApproval:
        db = await self._connect()
        try:
            cursor = await db.execute(
                "SELECT * FROM approvals WHERE workflow_id = ?",
                (workflow_id,),
            )
            row = await cursor.fetchone()
            if row is None:
                raise NotFoundError("Workflow has not been approved")
            return ArtifactApproval(
                workflow_id=row["workflow_id"],
                artifact_version=row["artifact_version"],
                manifest_digest=row["manifest_digest"],
                approved_by=row["approved_by"],
                approved_at=row["approved_at"],
            )
        finally:
            await db.close()

    async def save_revision(self, revision: RevisionRequest) -> None:
        db = await self._connect()
        try:
            await db.execute("BEGIN IMMEDIATE")
            await db.execute(
                """
                INSERT INTO revision_requests (workflow_id, mode, feedback, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    revision.workflow_id,
                    revision.mode.value,
                    revision.feedback,
                    revision.created_at.isoformat(),
                ),
            )
            await db.execute("DELETE FROM approvals WHERE workflow_id = ?", (revision.workflow_id,))
            await db.commit()
        except Exception:
            await db.rollback()
            raise
        finally:
            await db.close()

    async def request_revision(self, revision: RevisionRequest) -> WorkItem:
        item = WorkItem(workflow_id=revision.workflow_id, kind=WorkKind.PREPARE)
        db = await self._connect()
        try:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                "SELECT state, version FROM workflows WHERE id = ?",
                (revision.workflow_id,),
            )
            workflow = await cursor.fetchone()
            if workflow is None:
                raise NotFoundError(f"Workflow '{revision.workflow_id}' was not found")
            current_state = WorkflowState(workflow["state"])
            if current_state not in {
                WorkflowState.AWAITING_APPROVAL,
                WorkflowState.APPROVED,
            }:
                raise ConflictError("Only an unsubmitted artifact can be revised")
            assert_transition(current_state, WorkflowState.REVISION_REQUESTED)
            await db.execute(
                """
                INSERT INTO revision_requests (workflow_id, mode, feedback, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    revision.workflow_id,
                    revision.mode.value,
                    revision.feedback,
                    revision.created_at.isoformat(),
                ),
            )
            await db.execute(
                "DELETE FROM approvals WHERE workflow_id = ?",
                (revision.workflow_id,),
            )
            now = utc_now()
            cursor = await db.execute(
                """
                UPDATE workflows
                SET state = ?, version = version + 1, updated_at = ?
                WHERE id = ? AND version = ?
                """,
                (
                    WorkflowState.REVISION_REQUESTED.value,
                    now.isoformat(),
                    revision.workflow_id,
                    workflow["version"],
                ),
            )
            if cursor.rowcount != 1:
                raise ConflictError("Workflow changed during revision request")
            await self._insert_event(
                db,
                revision.workflow_id,
                "revision.requested",
                WorkflowState.REVISION_REQUESTED,
                {"mode": revision.mode.value, "feedback": revision.feedback},
            )
            await self._insert_work_item(db, item)
            await db.commit()
            return item
        except Exception:
            await db.rollback()
            raise
        finally:
            await db.close()

    async def create_copy(
        self,
        workflow: PrintWorkflow,
        plan: ModelPlan,
        handoff: ModelingHandoff | None,
        artifact: ModelArtifact,
        *,
        source_workflow_id: str,
        source_artifact_version: int,
    ) -> PrintWorkflow:
        db = await self._connect()
        try:
            await db.execute("BEGIN IMMEDIATE")
            await db.execute(
                """
                INSERT INTO workflows (
                    id, requirement, printer_name, state, version,
                    discovery_session_id, modeling_session_id,
                    active_handoff_version, active_artifact_version,
                    failure_code, failure_message, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, NULL, NULL, ?, ?, NULL, NULL, ?, ?)
                """,
                (
                    workflow.id,
                    workflow.requirement,
                    workflow.printer_name,
                    workflow.state.value,
                    workflow.version,
                    workflow.active_handoff_version,
                    workflow.active_artifact_version,
                    workflow.created_at.isoformat(),
                    workflow.updated_at.isoformat(),
                ),
            )
            await db.execute(
                """
                INSERT INTO model_plans (workflow_id, payload_json, created_at)
                VALUES (?, ?, ?)
                """,
                (workflow.id, plan.model_dump_json(), utc_now().isoformat()),
            )
            if handoff is not None:
                await db.execute(
                    """
                    INSERT INTO modeling_handoffs (
                        workflow_id, version, digest, payload_json, created_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        workflow.id,
                        handoff.version,
                        handoff.digest,
                        handoff.model_dump_json(),
                        utc_now().isoformat(),
                    ),
                )
            await db.execute(
                """
                INSERT INTO artifacts (
                    workflow_id, version, manifest_digest, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    workflow.id,
                    artifact.version,
                    artifact.manifest_digest,
                    artifact.model_dump_json(),
                    artifact.created_at.isoformat(),
                ),
            )
            await self._insert_event(
                db,
                workflow.id,
                "workflow.copied",
                workflow.state,
                {
                    "source_workflow_id": source_workflow_id,
                    "source_artifact_version": source_artifact_version,
                },
            )
            await db.commit()
            return workflow
        except Exception:
            await db.rollback()
            raise
        finally:
            await db.close()

    async def get_latest_revision(self, workflow_id: str) -> RevisionRequest:
        db = await self._connect()
        try:
            cursor = await db.execute(
                """
                SELECT * FROM revision_requests
                WHERE workflow_id = ? ORDER BY id DESC LIMIT 1
                """,
                (workflow_id,),
            )
            row = await cursor.fetchone()
            if row is None:
                raise NotFoundError("Workflow has no revision request")
            return RevisionRequest(
                workflow_id=row["workflow_id"],
                mode=RevisionMode(row["mode"]),
                feedback=row["feedback"],
                created_at=row["created_at"],
            )
        finally:
            await db.close()

    async def save_job(self, job: PrintJob) -> None:
        db = await self._connect()
        try:
            await db.execute(
                """
                INSERT INTO print_jobs (
                    id, workflow_id, printer_name, external_id, idempotency_key,
                    status, payload_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(idempotency_key) DO UPDATE SET
                    status = excluded.status,
                    payload_json = excluded.payload_json,
                    updated_at = excluded.updated_at
                """,
                (
                    job.id,
                    job.workflow_id,
                    job.printer_name,
                    job.external_id,
                    job.idempotency_key,
                    job.status.value,
                    job.model_dump_json(),
                    job.created_at.isoformat(),
                    job.updated_at.isoformat(),
                ),
            )
            await db.commit()
        finally:
            await db.close()

    async def finalize_print_submission(self, job: PrintJob) -> None:
        refresh = WorkItem(
            workflow_id=job.workflow_id,
            kind=WorkKind.REFRESH_PRINT,
            available_at=utc_now() + timedelta(seconds=2),
        )
        db = await self._connect()
        try:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                "SELECT state, version FROM workflows WHERE id = ?",
                (job.workflow_id,),
            )
            workflow = await cursor.fetchone()
            if workflow is None:
                raise NotFoundError(f"Workflow '{job.workflow_id}' was not found")
            if workflow["state"] != WorkflowState.SUBMITTING.value:
                raise ConflictError("Workflow is no longer submitting to a printer")
            await db.execute(
                """
                INSERT INTO print_jobs (
                    id, workflow_id, printer_name, external_id, idempotency_key,
                    status, payload_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(idempotency_key) DO UPDATE SET
                    status = excluded.status,
                    payload_json = excluded.payload_json,
                    updated_at = excluded.updated_at
                """,
                (
                    job.id,
                    job.workflow_id,
                    job.printer_name,
                    job.external_id,
                    job.idempotency_key,
                    job.status.value,
                    job.model_dump_json(),
                    job.created_at.isoformat(),
                    job.updated_at.isoformat(),
                ),
            )
            now = utc_now()
            cursor = await db.execute(
                """
                UPDATE workflows
                SET state = ?, version = version + 1, updated_at = ?
                WHERE id = ? AND version = ?
                """,
                (
                    WorkflowState.QUEUED.value,
                    now.isoformat(),
                    job.workflow_id,
                    workflow["version"],
                ),
            )
            if cursor.rowcount != 1:
                raise ConflictError("Workflow changed while finalizing print submission")
            await self._insert_event(
                db,
                job.workflow_id,
                "print.queued",
                WorkflowState.QUEUED,
                {"job_id": job.id, "external_id": job.external_id},
            )
            await self._insert_work_item(db, refresh)
            await db.commit()
        except Exception:
            await db.rollback()
            raise
        finally:
            await db.close()

    async def get_latest_job(self, workflow_id: str) -> PrintJob | None:
        db = await self._connect()
        try:
            cursor = await db.execute(
                """
                SELECT payload_json FROM print_jobs
                WHERE workflow_id = ? ORDER BY created_at DESC LIMIT 1
                """,
                (workflow_id,),
            )
            row = await cursor.fetchone()
            return PrintJob.model_validate_json(row["payload_json"]) if row else None
        finally:
            await db.close()

    async def list_events(self, workflow_id: str, after_id: int = 0) -> list[WorkflowEvent]:
        db = await self._connect()
        try:
            cursor = await db.execute(
                """
                SELECT * FROM workflow_events
                WHERE workflow_id = ? AND id > ? ORDER BY id
                """,
                (workflow_id, after_id),
            )
            return [
                WorkflowEvent(
                    id=row["id"],
                    workflow_id=row["workflow_id"],
                    kind=row["kind"],
                    state=WorkflowState(row["state"]),
                    payload=json.loads(row["payload_json"]),
                    created_at=row["created_at"],
                )
                for row in await cursor.fetchall()
            ]
        finally:
            await db.close()

    async def enqueue(
        self,
        workflow_id: str,
        kind: WorkKind,
        *,
        delay_seconds: float = 0,
    ) -> WorkItem:
        item = WorkItem(
            workflow_id=workflow_id,
            kind=kind,
            available_at=utc_now() + timedelta(seconds=delay_seconds),
        )
        db = await self._connect()
        try:
            await self._insert_work_item(db, item)
            await db.commit()
            return item
        finally:
            await db.close()

    async def lease_next(self, lease_seconds: int = 60) -> WorkItem | None:
        db = await self._connect()
        try:
            await db.execute("BEGIN IMMEDIATE")
            now = utc_now()
            cursor = await db.execute(
                """
                SELECT * FROM work_items
                WHERE (
                    status = 'queued'
                    OR (status = 'running' AND leased_until < ?)
                ) AND available_at <= ?
                ORDER BY available_at, attempts
                LIMIT 1
                """,
                (now.isoformat(), now.isoformat()),
            )
            row = await cursor.fetchone()
            if row is None:
                await db.commit()
                return None
            leased_until = now + timedelta(seconds=lease_seconds)
            await db.execute(
                """
                UPDATE work_items
                SET status = 'running', attempts = attempts + 1, leased_until = ?
                WHERE id = ?
                """,
                (leased_until.isoformat(), row["id"]),
            )
            await db.commit()
            return WorkItem(
                id=row["id"],
                workflow_id=row["workflow_id"],
                kind=WorkKind(row["kind"]),
                available_at=row["available_at"],
                attempts=row["attempts"] + 1,
                leased_until=leased_until,
            )
        except Exception:
            await db.rollback()
            raise
        finally:
            await db.close()

    async def complete_work(self, work_item_id: str) -> None:
        await self._update_work_status(work_item_id, "completed")

    async def fail_work(self, work_item_id: str, error: str) -> None:
        await self._update_work_status(work_item_id, "failed", error)

    async def renew_work_lease(
        self,
        work_item_id: str,
        *,
        lease_seconds: int = 60,
    ) -> None:
        db = await self._connect()
        try:
            leased_until = utc_now() + timedelta(seconds=lease_seconds)
            cursor = await db.execute(
                """
                UPDATE work_items SET leased_until = ?
                WHERE id = ? AND status = 'running'
                """,
                (leased_until.isoformat(), work_item_id),
            )
            if cursor.rowcount != 1:
                raise ConflictError("Cannot renew a work item that is not running")
            await db.commit()
        finally:
            await db.close()

    async def _update_work_status(
        self,
        work_item_id: str,
        status: str,
        error: str | None = None,
    ) -> None:
        db = await self._connect()
        try:
            await db.execute(
                """
                UPDATE work_items SET status = ?, leased_until = NULL, last_error = ?
                WHERE id = ?
                """,
                (status, error, work_item_id),
            )
            await db.commit()
        finally:
            await db.close()

    async def _insert_event(
        self,
        db: aiosqlite.Connection,
        workflow_id: str,
        kind: str,
        state: WorkflowState,
        payload: dict[str, Any],
    ) -> None:
        await db.execute(
            """
            INSERT INTO workflow_events (
                workflow_id, kind, state, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                workflow_id,
                kind,
                state.value,
                _json(payload),
                utc_now().isoformat(),
            ),
        )

    async def _insert_work_item(
        self,
        db: aiosqlite.Connection,
        item: WorkItem,
    ) -> None:
        await db.execute(
            """
            INSERT INTO work_items (
                id, workflow_id, kind, status, available_at,
                attempts, leased_until, last_error
            ) VALUES (?, ?, ?, 'queued', ?, ?, NULL, NULL)
            """,
            (
                item.id,
                item.workflow_id,
                item.kind.value,
                item.available_at.isoformat(),
                item.attempts,
            ),
        )

    async def _count(self, table: str, workflow_id: str) -> int:
        db = await self._connect()
        try:
            cursor = await db.execute(
                f"SELECT COUNT(*) AS count FROM {table} WHERE workflow_id = ?",
                (workflow_id,),
            )
            return int((await cursor.fetchone())["count"])
        finally:
            await db.close()

    async def _next_version(self, table: str, workflow_id: str) -> int:
        db = await self._connect()
        try:
            cursor = await db.execute(
                f"""
                SELECT COALESCE(MAX(version), 0) + 1 AS next_version
                FROM {table} WHERE workflow_id = ?
                """,
                (workflow_id,),
            )
            return int((await cursor.fetchone())["next_version"])
        finally:
            await db.close()

    async def _upsert_payload(
        self,
        table: str,
        workflow_id: str,
        value: Any,
        *,
        conflict_column: str,
    ) -> None:
        db = await self._connect()
        try:
            await db.execute(
                f"""
                INSERT INTO {table} ({conflict_column}, payload_json, created_at)
                VALUES (?, ?, ?)
                ON CONFLICT({conflict_column}) DO UPDATE SET
                    payload_json = excluded.payload_json,
                    created_at = excluded.created_at
                """,
                (workflow_id, _json(value), utc_now().isoformat()),
            )
            await db.commit()
        finally:
            await db.close()

    async def _insert_versioned_payload(
        self,
        table: str,
        workflow_id: str,
        value: Any,
    ) -> None:
        db = await self._connect()
        try:
            await db.execute(
                f"""
                INSERT INTO {table} (workflow_id, payload_json, created_at)
                VALUES (?, ?, ?)
                """,
                (workflow_id, _json(value), utc_now().isoformat()),
            )
            await db.commit()
        finally:
            await db.close()

    async def _get_payload(self, table: str, column: str, value: Any) -> str:
        db = await self._connect()
        try:
            cursor = await db.execute(
                f"SELECT payload_json FROM {table} WHERE {column} = ?",
                (value,),
            )
            row = await cursor.fetchone()
            if row is None:
                raise NotFoundError(f"No {table} record found")
            return str(row["payload_json"])
        finally:
            await db.close()

    async def _get_composite_payload(
        self,
        table: str,
        workflow_id: str,
        version: int,
    ) -> str:
        db = await self._connect()
        try:
            cursor = await db.execute(
                f"""
                SELECT payload_json FROM {table}
                WHERE workflow_id = ? AND version = ?
                """,
                (workflow_id, version),
            )
            row = await cursor.fetchone()
            if row is None:
                raise NotFoundError(f"No {table} version {version} found")
            return str(row["payload_json"])
        finally:
            await db.close()
