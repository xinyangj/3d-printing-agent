from __future__ import annotations

import json
import math
from datetime import timedelta
from pathlib import Path
from typing import Any, Literal

import aiosqlite

from printing_agent.cloud_inventory import CloudDeviceSnapshot
from printing_agent.domain import (
    ArtifactApproval,
    CandidateAttempt,
    CandidatePageInspection,
    DiscoveryDecision,
    ModelArtifact,
    ModelingHandoff,
    ModelPlan,
    PrintJob,
    PrintWorkflow,
    RevisionMode,
    RevisionRequest,
    RevisionVerification,
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
from printing_agent.fabrication import (
    MaterialAssignment,
    MaterialDefinitionRevision,
    MaterialDefinitionSpec,
    PhysicalSpool,
    PrinterProfileRevision,
    SlicedArtifact,
    SliceJob,
    SliceJobStatus,
    SpoolQuantityStatus,
    SpoolReconciliationResult,
    SpoolReservation,
    SpoolStatus,
    SpoolUsageRequirement,
    UnknownQuantitySlotAuthorization,
    WorkflowPrinterSnapshot,
)

_SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS app_schema_migrations (
    name TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL
);

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
    archived_at TEXT,
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

CREATE TABLE IF NOT EXISTS candidate_attempts (
    workflow_id TEXT NOT NULL REFERENCES workflows(id),
    candidate_id TEXT NOT NULL,
    status TEXT NOT NULL,
    stage TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (workflow_id, candidate_id)
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
    part_id TEXT,
    allowed_part_ids_json TEXT,
    base_artifact_version INTEGER,
    base_handoff_version INTEGER,
    base_state TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS revision_verifications (
    id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL REFERENCES workflows(id),
    handoff_version INTEGER NOT NULL,
    base_artifact_version INTEGER NOT NULL,
    candidate_artifact_version INTEGER NOT NULL,
    verdict TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS active_revision_failures (
    workflow_id TEXT PRIMARY KEY REFERENCES workflows(id),
    verification_id TEXT NOT NULL REFERENCES revision_verifications(id),
    payload_json TEXT NOT NULL,
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

CREATE TABLE IF NOT EXISTS printer_profiles (
    id TEXT PRIMARY KEY,
    active_revision INTEGER NOT NULL,
    enabled INTEGER NOT NULL,
    archived_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS printer_profile_revisions (
    profile_id TEXT NOT NULL REFERENCES printer_profiles(id),
    revision INTEGER NOT NULL,
    digest TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (profile_id, revision),
    UNIQUE(profile_id, digest)
);

CREATE TABLE IF NOT EXISTS workflow_printer_snapshots (
    workflow_id TEXT PRIMARY KEY REFERENCES workflows(id),
    profile_id TEXT NOT NULL,
    profile_revision INTEGER NOT NULL,
    digest TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cloud_device_snapshots (
    id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL REFERENCES workflows(id),
    profile_id TEXT NOT NULL,
    digest TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_cloud_device_snapshots_workflow
ON cloud_device_snapshots(workflow_id, observed_at DESC);

CREATE TABLE IF NOT EXISTS material_definitions (
    id TEXT PRIMARY KEY,
    active_revision INTEGER NOT NULL,
    enabled INTEGER NOT NULL,
    archived_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS material_definition_revisions (
    material_id TEXT NOT NULL REFERENCES material_definitions(id),
    revision INTEGER NOT NULL,
    digest TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (material_id, revision),
    UNIQUE(material_id, digest)
);

CREATE TABLE IF NOT EXISTS physical_spools (
    id TEXT PRIMARY KEY,
    material_id TEXT NOT NULL,
    material_revision INTEGER NOT NULL,
    status TEXT NOT NULL,
    printer_profile_id TEXT,
    slot_id TEXT,
    payload_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(printer_profile_id, slot_id)
);

CREATE TABLE IF NOT EXISTS unknown_quantity_slot_authorizations (
    profile_id TEXT NOT NULL,
    device_ref TEXT NOT NULL,
    slot_id TEXT NOT NULL,
    tray_identity_digest TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    authorized_at TEXT NOT NULL,
    PRIMARY KEY(profile_id, device_ref, slot_id)
);

CREATE TABLE IF NOT EXISTS material_assignments (
    id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL REFERENCES workflows(id),
    artifact_version INTEGER NOT NULL,
    digest TEXT NOT NULL,
    confirmed INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_material_assignments_workflow
ON material_assignments(workflow_id, created_at DESC);

CREATE TABLE IF NOT EXISTS spool_reservations (
    id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL REFERENCES workflows(id),
    slice_job_id TEXT,
    spool_id TEXT NOT NULL REFERENCES physical_spools(id),
    reserved_weight_g REAL NOT NULL,
    actual_usage_g REAL,
    status TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS slice_jobs (
    id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL REFERENCES workflows(id),
    status TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sliced_artifacts (
    slice_job_id TEXT PRIMARY KEY REFERENCES slice_jobs(id),
    workflow_id TEXT NOT NULL REFERENCES workflows(id),
    digest TEXT NOT NULL,
    manifest_digest TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


def _json(value: Any) -> str:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_model,
    )


def _json_model(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


class WorkflowRepository:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path

    async def initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.database_path) as db:
            await db.executescript(_SCHEMA)
            cursor = await db.execute("PRAGMA table_info(workflows)")
            columns = {row[1] for row in await cursor.fetchall()}
            if "archived_at" not in columns:
                await db.execute("ALTER TABLE workflows ADD COLUMN archived_at TEXT")
            cursor = await db.execute("PRAGMA table_info(revision_requests)")
            revision_columns = {row[1] for row in await cursor.fetchall()}
            if "part_id" not in revision_columns:
                await db.execute("ALTER TABLE revision_requests ADD COLUMN part_id TEXT")
            if "allowed_part_ids_json" not in revision_columns:
                await db.execute(
                    "ALTER TABLE revision_requests ADD COLUMN allowed_part_ids_json TEXT"
                )
            if "base_artifact_version" not in revision_columns:
                await db.execute(
                    "ALTER TABLE revision_requests ADD COLUMN base_artifact_version INTEGER"
                )
            if "base_handoff_version" not in revision_columns:
                await db.execute(
                    "ALTER TABLE revision_requests ADD COLUMN base_handoff_version INTEGER"
                )
            if "base_state" not in revision_columns:
                await db.execute("ALTER TABLE revision_requests ADD COLUMN base_state TEXT")
            cursor = await db.execute("PRAGMA table_info(spool_reservations)")
            reservation_columns = {row[1] for row in await cursor.fetchall()}
            if "actual_usage_g" not in reservation_columns:
                await db.execute(
                    "ALTER TABLE spool_reservations ADD COLUMN actual_usage_g REAL"
                )
            await self._migrate_legacy_combined_fabrication(db)
            await db.commit()

    @staticmethod
    async def _migrate_legacy_combined_fabrication(
        db: aiosqlite.Connection,
    ) -> None:
        migration = "slice-only-v1-quarantine"
        cursor = await db.execute(
            "SELECT 1 FROM app_schema_migrations WHERE name = ?",
            (migration,),
        )
        if await cursor.fetchone() is not None:
            return
        legacy_table = "submission_" + "handoffs"
        cursor = await db.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (legacy_table,),
        )
        if await cursor.fetchone() is not None:
            slice_states = (
                "slice_setup",
                "awaiting_material_review",
                "slice_requested",
                "slicing",
                "slice_validating",
                "awaiting_slice_review",
                "slice_failed",
                "submission_handoff_requested",
                "external_confirmation_required",
                "user_confirmed_submitted",
            )
            placeholders = ",".join("?" for _ in slice_states)
            await db.execute(
                f"""
                UPDATE workflows
                SET state = CASE
                    WHEN EXISTS (
                        SELECT 1 FROM approvals a
                        WHERE a.workflow_id = workflows.id
                    ) THEN 'approved'
                    ELSE 'awaiting_approval'
                END,
                failure_code = NULL,
                failure_message = NULL,
                version = version + 1,
                updated_at = ?
                WHERE state IN ({placeholders})
                """,
                (utc_now().isoformat(), *slice_states),
            )
            await db.execute(
                "DELETE FROM work_items WHERE kind = ?",
                (WorkKind.SLICE.value,),
            )
            await db.execute(f"DROP TABLE {legacy_table}")
            for table in (
                "sliced_artifacts",
                "spool_reservations",
                "slice_jobs",
                "material_assignments",
                "physical_spools",
                "material_definition_revisions",
                "material_definitions",
                "cloud_device_snapshots",
                "workflow_printer_snapshots",
                "printer_profile_revisions",
                "printer_profiles",
            ):
                await db.execute(f"DELETE FROM {table}")
        await db.execute(
            """
            INSERT INTO app_schema_migrations (name, applied_at)
            VALUES (?, ?)
            """,
            (migration, utc_now().isoformat()),
        )

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
                    failure_code, failure_message, archived_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, NULL, NULL, NULL, ?, ?)
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

    async def create_workflow_with_snapshot(
        self,
        workflow: PrintWorkflow,
        snapshot: WorkflowPrinterSnapshot,
    ) -> PrintWorkflow:
        if snapshot.workflow_id != workflow.id or snapshot.digest is None:
            raise ValueError("Workflow printer snapshot does not match workflow")
        item = WorkItem(workflow_id=workflow.id, kind=WorkKind.PREPARE)
        db = await self._connect()
        try:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                """
                SELECT p.active_revision, r.digest
                FROM printer_profiles p
                JOIN printer_profile_revisions r
                  ON r.profile_id = p.id AND r.revision = p.active_revision
                WHERE p.id = ? AND p.enabled = 1 AND p.archived_at IS NULL
                """,
                (snapshot.profile_id,),
            )
            profile = await cursor.fetchone()
            if (
                profile is None
                or int(profile["active_revision"]) != snapshot.profile_revision
                or profile["digest"] != snapshot.profile_digest
            ):
                raise ConflictError(
                    "Printer profile changed or was archived before workflow creation"
                )
            await db.execute(
                """
                INSERT INTO workflows (
                    id, requirement, printer_name, state, version,
                    discovery_session_id, modeling_session_id,
                    active_handoff_version, active_artifact_version,
                    failure_code, failure_message, archived_at,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    workflow.id,
                    workflow.requirement,
                    workflow.printer_name,
                    workflow.state.value,
                    workflow.version,
                    workflow.discovery_session_id,
                    workflow.modeling_session_id,
                    workflow.active_handoff_version,
                    workflow.active_artifact_version,
                    workflow.failure_code,
                    workflow.failure_message,
                    workflow.archived_at.isoformat()
                    if workflow.archived_at
                    else None,
                    workflow.created_at.isoformat(),
                    workflow.updated_at.isoformat(),
                ),
            )
            await db.execute(
                """
                INSERT INTO workflow_printer_snapshots (
                    workflow_id, profile_id, profile_revision, digest,
                    payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot.workflow_id,
                    snapshot.profile_id,
                    snapshot.profile_revision,
                    snapshot.digest,
                    snapshot.model_dump_json(),
                    snapshot.created_at.isoformat(),
                ),
            )
            await self._insert_event(
                db,
                workflow.id,
                "workflow.created",
                workflow.state,
                {
                    "printer_name": workflow.printer_name,
                    "printer_snapshot_digest": snapshot.digest,
                },
            )
            await self._insert_work_item(db, item)
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

    async def archive_workflow(self, workflow_id: str) -> PrintWorkflow:
        allowed_states = {
            WorkflowState.AWAITING_APPROVAL,
            WorkflowState.APPROVED,
            WorkflowState.COMPLETED,
            WorkflowState.PREPARATION_FAILED,
            WorkflowState.PRINT_FAILED,
            WorkflowState.CANCELLED,
        }
        db = await self._connect()
        try:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute("SELECT * FROM workflows WHERE id = ?", (workflow_id,))
            row = await cursor.fetchone()
            if row is None:
                raise NotFoundError(f"Workflow '{workflow_id}' was not found")
            workflow = self._workflow_from_row(row)
            if workflow.archived_at is not None:
                raise ConflictError("Workflow is already archived")
            if workflow.state not in allowed_states:
                raise ConflictError(
                    "Workflow cannot be archived while preparation or printing is active"
                )
            cursor = await db.execute(
                """
                SELECT COUNT(*) AS count FROM work_items
                WHERE workflow_id = ? AND status IN ('queued', 'running')
                """,
                (workflow_id,),
            )
            active_work = int((await cursor.fetchone())["count"])
            if active_work:
                raise ConflictError(
                    "Workflow cannot be archived while background work is still active"
                )
            now = utc_now()
            cursor = await db.execute(
                """
                UPDATE workflows
                SET archived_at = ?, version = version + 1, updated_at = ?
                WHERE id = ? AND version = ? AND archived_at IS NULL
                """,
                (now.isoformat(), now.isoformat(), workflow_id, workflow.version),
            )
            if cursor.rowcount != 1:
                raise ConflictError("Workflow changed during archive")
            await self._insert_event(
                db,
                workflow_id,
                "workflow.archived",
                workflow.state,
                {"archived_at": now.isoformat()},
            )
            await db.commit()
            return workflow.model_copy(
                update={
                    "archived_at": now,
                    "version": workflow.version + 1,
                    "updated_at": now,
                }
            )
        except Exception:
            await db.rollback()
            raise
        finally:
            await db.close()

    async def restore_workflow(self, workflow_id: str) -> PrintWorkflow:
        db = await self._connect()
        try:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute("SELECT * FROM workflows WHERE id = ?", (workflow_id,))
            row = await cursor.fetchone()
            if row is None:
                raise NotFoundError(f"Workflow '{workflow_id}' was not found")
            workflow = self._workflow_from_row(row)
            if workflow.archived_at is None:
                raise ConflictError("Workflow is not archived")
            now = utc_now()
            cursor = await db.execute(
                """
                UPDATE workflows
                SET archived_at = NULL, version = version + 1, updated_at = ?
                WHERE id = ? AND version = ? AND archived_at IS NOT NULL
                """,
                (now.isoformat(), workflow_id, workflow.version),
            )
            if cursor.rowcount != 1:
                raise ConflictError("Workflow changed during restore")
            await self._insert_event(
                db,
                workflow_id,
                "workflow.restored",
                workflow.state,
                {},
            )
            await db.commit()
            return workflow.model_copy(
                update={
                    "archived_at": None,
                    "version": workflow.version + 1,
                    "updated_at": now,
                }
            )
        except Exception:
            await db.rollback()
            raise
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
            if current.archived_at is not None:
                raise ConflictError("Restore the archived workflow before changing it")
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

    async def save_candidate_attempt(self, attempt: CandidateAttempt) -> None:
        db = await self._connect()
        try:
            await db.execute(
                """
                INSERT INTO candidate_attempts (
                    workflow_id, candidate_id, status, stage, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(workflow_id, candidate_id) DO UPDATE SET
                    status = excluded.status,
                    stage = excluded.stage,
                    payload_json = excluded.payload_json,
                    created_at = excluded.created_at
                """,
                (
                    attempt.workflow_id,
                    attempt.candidate_id,
                    attempt.status,
                    attempt.stage,
                    attempt.model_dump_json(),
                    attempt.created_at.isoformat(),
                ),
            )
            await db.commit()
        finally:
            await db.close()

    async def list_rejected_candidate_ids(self, workflow_id: str) -> set[str]:
        db = await self._connect()
        try:
            cursor = await db.execute(
                """
                SELECT candidate_id FROM candidate_attempts
                WHERE workflow_id = ? AND status = 'rejected'
                """,
                (workflow_id,),
            )
            return {str(row["candidate_id"]) for row in await cursor.fetchall()}
        finally:
            await db.close()

    async def count_rejected_candidates(self, workflow_id: str) -> int:
        db = await self._connect()
        try:
            cursor = await db.execute(
                """
                SELECT COUNT(*) AS count FROM candidate_attempts
                WHERE workflow_id = ? AND status = 'rejected'
                """,
                (workflow_id,),
            )
            return int((await cursor.fetchone())["count"])
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
                  AND status IN ('rejected', 'verification_rejected')
                """,
                (workflow_id, handoff_version),
            )
            return int((await cursor.fetchone())["count"])
        finally:
            await db.close()

    async def next_artifact_version(self, workflow_id: str) -> int:
        return await self._next_version("artifacts", workflow_id)

    async def save_artifact(
        self,
        artifact: ModelArtifact,
        *,
        activate: bool = True,
    ) -> None:
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
            if activate:
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
            else:
                cursor = await db.execute(
                    """
                    UPDATE workflows
                    SET version = version + 1, updated_at = ?
                    WHERE id = ? AND version = ?
                    """,
                    (
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

    async def save_revision_verification(
        self,
        verification: RevisionVerification,
        *,
        active_failure: bool = False,
    ) -> None:
        db = await self._connect()
        try:
            await db.execute("BEGIN IMMEDIATE")
            await self._insert_revision_verification(db, verification)
            if active_failure:
                await self._set_active_revision_failure(db, verification)
            elif verification.verdict == "passed":
                await db.execute(
                    "DELETE FROM active_revision_failures WHERE workflow_id = ?",
                    (verification.workflow_id,),
                )
            await db.commit()
        except Exception:
            await db.rollback()
            raise
        finally:
            await db.close()

    async def get_active_revision_failure(
        self,
        workflow_id: str,
    ) -> RevisionVerification | None:
        db = await self._connect()
        try:
            cursor = await db.execute(
                """
                SELECT payload_json FROM active_revision_failures
                WHERE workflow_id = ?
                """,
                (workflow_id,),
            )
            row = await cursor.fetchone()
            return (
                RevisionVerification.model_validate_json(row["payload_json"])
                if row is not None
                else None
            )
        finally:
            await db.close()

    async def get_latest_revision_verification(
        self,
        workflow_id: str,
    ) -> RevisionVerification | None:
        db = await self._connect()
        try:
            cursor = await db.execute(
                """
                SELECT payload_json FROM revision_verifications
                WHERE workflow_id = ?
                ORDER BY created_at DESC LIMIT 1
                """,
                (workflow_id,),
            )
            row = await cursor.fetchone()
            return (
                RevisionVerification.model_validate_json(row["payload_json"])
                if row is not None
                else None
            )
        finally:
            await db.close()

    async def prepare_revision_verification_retry(
        self,
        verification: RevisionVerification,
    ) -> None:
        db = await self._connect()
        try:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                "SELECT * FROM workflows WHERE id = ?",
                (verification.workflow_id,),
            )
            row = await cursor.fetchone()
            if row is None:
                raise NotFoundError(
                    f"Workflow '{verification.workflow_id}' was not found"
                )
            current = self._workflow_from_row(row)
            assert_transition(current.state, WorkflowState.GENERATING)
            if current.active_handoff_version != verification.handoff_version:
                raise ConflictError("Active handoff changed before verification retry")
            if current.active_artifact_version != verification.base_artifact_version:
                raise ConflictError("Active artifact changed before verification retry")
            await self._insert_revision_verification(db, verification)
            now = utc_now()
            cursor = await db.execute(
                """
                UPDATE workflows
                SET state = ?, active_artifact_version = ?, modeling_session_id = NULL,
                    version = version + 1, updated_at = ?
                WHERE id = ? AND version = ?
                """,
                (
                    WorkflowState.GENERATING.value,
                    verification.base_artifact_version,
                    now.isoformat(),
                    verification.workflow_id,
                    current.version,
                ),
            )
            if cursor.rowcount != 1:
                raise ConflictError("Workflow changed during verification retry")
            await self._insert_event(
                db,
                verification.workflow_id,
                "revision.verification_repair_requested",
                WorkflowState.GENERATING,
                {
                    "verification_id": verification.id,
                    "candidate_artifact_version": verification.candidate_artifact_version,
                    "reasons": [
                        check.message for check in verification.checks if not check.passed
                    ],
                },
            )
            await db.commit()
        except Exception:
            await db.rollback()
            raise
        finally:
            await db.close()

    async def complete_verified_revision(
        self,
        verification: RevisionVerification,
    ) -> None:
        if verification.verdict != "passed":
            raise ValueError("Only passing verification can complete a revision")
        db = await self._connect()
        try:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                "SELECT * FROM workflows WHERE id = ?",
                (verification.workflow_id,),
            )
            row = await cursor.fetchone()
            if row is None:
                raise NotFoundError(
                    f"Workflow '{verification.workflow_id}' was not found"
                )
            current = self._workflow_from_row(row)
            assert_transition(current.state, WorkflowState.AWAITING_APPROVAL)
            if current.active_handoff_version != verification.handoff_version:
                raise ConflictError(
                    "Active handoff changed before verification completion"
                )
            if current.active_artifact_version != verification.base_artifact_version:
                raise ConflictError(
                    "Active artifact changed before verification completion"
                )
            await self._insert_revision_verification(db, verification)
            await db.execute(
                "DELETE FROM active_revision_failures WHERE workflow_id = ?",
                (verification.workflow_id,),
            )
            await db.execute(
                "DELETE FROM approvals WHERE workflow_id = ?",
                (verification.workflow_id,),
            )
            now = utc_now()
            cursor = await db.execute(
                """
                UPDATE workflows
                SET state = ?, active_artifact_version = ?,
                    version = version + 1, updated_at = ?,
                    failure_code = NULL, failure_message = NULL
                WHERE id = ? AND version = ?
                """,
                (
                    WorkflowState.AWAITING_APPROVAL.value,
                    verification.candidate_artifact_version,
                    now.isoformat(),
                    verification.workflow_id,
                    current.version,
                ),
            )
            if cursor.rowcount != 1:
                raise ConflictError("Workflow changed during verification completion")
            await self._insert_event(
                db,
                verification.workflow_id,
                "revision.verification_passed",
                WorkflowState.AWAITING_APPROVAL,
                {
                    "verification_id": verification.id,
                    "artifact_version": verification.candidate_artifact_version,
                },
            )
            await db.commit()
        except Exception:
            await db.rollback()
            raise
        finally:
            await db.close()

    async def rollback_failed_revision(
        self,
        verification: RevisionVerification,
        *,
        restore_handoff_version: int | None,
        restore_state: WorkflowState,
        expected_handoff_version: int | None,
        expected_active_artifact_version: int,
    ) -> None:
        if verification.verdict != "failed":
            raise ValueError("Only failed verification can roll back a revision")
        db = await self._connect()
        try:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                "SELECT * FROM workflows WHERE id = ?",
                (verification.workflow_id,),
            )
            row = await cursor.fetchone()
            if row is None:
                raise NotFoundError(
                    f"Workflow '{verification.workflow_id}' was not found"
                )
            current = self._workflow_from_row(row)
            if current.state not in {
                WorkflowState.REVISION_REQUESTED,
                WorkflowState.HANDOFF_READY,
                WorkflowState.GENERATING,
                WorkflowState.RENDERING,
                WorkflowState.VALIDATING,
            }:
                raise ConflictError(
                    "Workflow changed before failed revision could be rolled back"
                )
            if current.active_handoff_version != expected_handoff_version:
                raise ConflictError(
                    "Active handoff changed before failed revision rollback"
                )
            if current.active_artifact_version != expected_active_artifact_version:
                raise ConflictError(
                    "Active artifact changed before failed revision rollback"
                )
            await self._insert_revision_verification(db, verification)
            await self._set_active_revision_failure(db, verification)
            now = utc_now()
            cursor = await db.execute(
                """
                UPDATE workflows
                SET state = ?, active_artifact_version = ?,
                    active_handoff_version = ?, modeling_session_id = NULL,
                    version = version + 1, updated_at = ?,
                    failure_code = NULL, failure_message = NULL
                WHERE id = ? AND version = ?
                """,
                (
                    restore_state.value,
                    verification.base_artifact_version,
                    restore_handoff_version,
                    now.isoformat(),
                    verification.workflow_id,
                    current.version,
                ),
            )
            if cursor.rowcount != 1:
                raise ConflictError("Workflow changed during revision rollback")
            await self._insert_event(
                db,
                verification.workflow_id,
                "revision.verification_failed",
                restore_state,
                {
                    "verification_id": verification.id,
                    "failed_artifact_version": verification.candidate_artifact_version,
                    "restored_artifact_version": verification.base_artifact_version,
                    "reasons": [
                        check.message for check in verification.checks if not check.passed
                    ],
                },
            )
            await db.commit()
        except Exception:
            await db.rollback()
            raise
        finally:
            await db.close()

    async def mark_latest_source_attempt_verification_rejected(
        self,
        workflow_id: str,
        handoff_version: int,
        diagnostics: str,
    ) -> None:
        db = await self._connect()
        try:
            cursor = await db.execute(
                """
                UPDATE source_attempts
                SET status = 'verification_rejected', diagnostics = ?
                WHERE workflow_id = ? AND handoff_version = ?
                  AND attempt = (
                    SELECT MAX(attempt) FROM source_attempts
                    WHERE workflow_id = ? AND handoff_version = ?
                  )
                """,
                (
                    diagnostics,
                    workflow_id,
                    handoff_version,
                    workflow_id,
                    handoff_version,
                ),
            )
            if cursor.rowcount != 1:
                raise ConflictError("No source attempt is available for verification")
            await db.commit()
        finally:
            await db.close()

    @staticmethod
    async def _insert_revision_verification(
        db: aiosqlite.Connection,
        verification: RevisionVerification,
    ) -> None:
        await db.execute(
            """
            INSERT INTO revision_verifications (
                id, workflow_id, handoff_version, base_artifact_version,
                candidate_artifact_version, verdict, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                verification.id,
                verification.workflow_id,
                verification.handoff_version,
                verification.base_artifact_version,
                verification.candidate_artifact_version,
                verification.verdict,
                verification.model_dump_json(),
                verification.created_at.isoformat(),
            ),
        )

    @staticmethod
    async def _set_active_revision_failure(
        db: aiosqlite.Connection,
        verification: RevisionVerification,
    ) -> None:
        await db.execute(
            """
            INSERT INTO active_revision_failures (
                workflow_id, verification_id, payload_json, created_at
            ) VALUES (?, ?, ?, ?)
            ON CONFLICT(workflow_id) DO UPDATE SET
                verification_id = excluded.verification_id,
                payload_json = excluded.payload_json,
                created_at = excluded.created_at
            """,
            (
                verification.workflow_id,
                verification.id,
                verification.model_dump_json(),
                verification.created_at.isoformat(),
            ),
        )

    async def approve_artifact(self, approval: ArtifactApproval) -> None:
        db = await self._connect()
        try:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                """
                SELECT state, version, active_artifact_version
                     , archived_at
                FROM workflows WHERE id = ?
                """,
                (approval.workflow_id,),
            )
            workflow = await cursor.fetchone()
            if workflow is None:
                raise NotFoundError(f"Workflow '{approval.workflow_id}' was not found")
            if workflow["archived_at"] is not None:
                raise ConflictError("Restore the archived workflow before changing it")
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
                """
                SELECT state, version, active_artifact_version, archived_at
                FROM workflows WHERE id = ?
                """,
                (workflow_id,),
            )
            workflow = await cursor.fetchone()
            if workflow is None:
                raise NotFoundError(f"Workflow '{workflow_id}' was not found")
            if workflow["archived_at"] is not None:
                raise ConflictError("Restore the archived workflow before changing it")
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

    async def enqueue_slice(
        self,
        job: SliceJob,
        reservations: list[SpoolReservation],
    ) -> WorkItem:
        item = WorkItem(workflow_id=job.workflow_id, kind=WorkKind.SLICE)
        db = await self._connect()
        try:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                """
                SELECT state, version, active_artifact_version, archived_at
                FROM workflows WHERE id = ?
                """,
                (job.workflow_id,),
            )
            workflow = await cursor.fetchone()
            if workflow is None:
                raise NotFoundError(f"Workflow '{job.workflow_id}' was not found")
            if workflow["archived_at"] is not None:
                raise ConflictError("Restore the archived workflow before slicing")
            if workflow["state"] not in {
                WorkflowState.AWAITING_MATERIAL_REVIEW.value,
                WorkflowState.SLICE_FAILED.value,
                WorkflowState.AWAITING_SLICE_REVIEW.value,
            }:
                raise ConflictError("Workflow is not ready for slicing or reslicing")
            cursor = await db.execute(
                """
                SELECT artifact_version, manifest_digest FROM approvals
                WHERE workflow_id = ?
                """,
                (job.workflow_id,),
            )
            approval = await cursor.fetchone()
            if (
                approval is None
                or approval["artifact_version"] != workflow["active_artifact_version"]
                or approval["manifest_digest"] != job.artifact_manifest_digest
            ):
                raise ConflictError("Slice job does not match the approved artifact")
            if workflow["state"] != WorkflowState.AWAITING_MATERIAL_REVIEW.value:
                await db.execute(
                    """
                    UPDATE spool_reservations
                    SET status = 'released', updated_at = ?
                    WHERE workflow_id = ? AND status = 'reserved'
                    """,
                    (utc_now().isoformat(), job.workflow_id),
                )
            await db.execute(
                """
                INSERT INTO slice_jobs (
                    id, workflow_id, status, idempotency_key,
                    payload_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job.id,
                    job.workflow_id,
                    job.status.value,
                    job.idempotency_key,
                    job.model_dump_json(),
                    job.created_at.isoformat(),
                    job.updated_at.isoformat(),
                ),
            )
            for reservation in reservations:
                cursor = await db.execute(
                    "SELECT payload_json FROM physical_spools WHERE id = ?",
                    (reservation.spool_id,),
                )
                spool_row = await cursor.fetchone()
                if spool_row is None:
                    raise ConflictError(
                        f"Spool '{reservation.spool_id}' is unavailable"
                    )
                spool = PhysicalSpool.model_validate_json(spool_row["payload_json"])
                if spool.status not in {SpoolStatus.AVAILABLE, SpoolStatus.LOADED}:
                    raise ConflictError(
                        f"Spool '{reservation.spool_id}' is not available"
                    )
                cursor = await db.execute(
                    """
                    SELECT COALESCE(SUM(reserved_weight_g), 0) AS reserved
                    FROM spool_reservations
                    WHERE spool_id = ? AND status = 'reserved'
                    """,
                    (reservation.spool_id,),
                )
                reserved = float((await cursor.fetchone())["reserved"])
                if (
                    spool.quantity_status == SpoolQuantityStatus.CLOUD_ESTIMATE
                    and spool.remaining_weight_g is not None
                    and spool.remaining_weight_g - reserved
                    < reservation.reserved_weight_g
                ):
                    raise ConflictError(
                        f"Spool '{reservation.spool_id}' has insufficient material"
                    )
                if (
                    spool.quantity_status != SpoolQuantityStatus.CLOUD_ESTIMATE
                    and spool.quantity_status
                    != SpoolQuantityStatus.USER_ATTESTED_UNKNOWN
                ):
                    raise ConflictError(
                        f"Spool '{reservation.spool_id}' has unconfirmed quantity"
                    )
                await db.execute(
                    """
                    INSERT INTO spool_reservations (
                        id, workflow_id, slice_job_id, spool_id,
                        reserved_weight_g, actual_usage_g, status,
                        payload_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        reservation.id,
                        reservation.workflow_id,
                        reservation.slice_job_id,
                        reservation.spool_id,
                        reservation.reserved_weight_g,
                        reservation.actual_usage_g,
                        reservation.status,
                        reservation.model_dump_json(),
                        reservation.created_at.isoformat(),
                        reservation.updated_at.isoformat(),
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
                    WorkflowState.SLICE_REQUESTED.value,
                    now.isoformat(),
                    job.workflow_id,
                    workflow["version"],
                ),
            )
            if cursor.rowcount != 1:
                raise ConflictError("Workflow changed while requesting slicing")
            await self._insert_event(
                db,
                job.workflow_id,
                "slice.requested",
                WorkflowState.SLICE_REQUESTED,
                {"slice_job_id": job.id},
            )
            await self._insert_work_item(db, item)
            await db.commit()
            return item
        except Exception:
            await db.rollback()
            raise
        finally:
            await db.close()

    async def finalize_slice(
        self,
        job: SliceJob,
        artifact: SlicedArtifact,
    ) -> None:
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
            if workflow["state"] != WorkflowState.SLICE_VALIDATING.value:
                raise ConflictError("Workflow is no longer validating a slice")
            await db.execute(
                """
                UPDATE slice_jobs SET status = ?, payload_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    job.status.value,
                    job.model_dump_json(),
                    job.updated_at.isoformat(),
                    job.id,
                ),
            )
            await db.execute(
                """
                INSERT INTO sliced_artifacts (
                    slice_job_id, workflow_id, digest, manifest_digest,
                    payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    artifact.slice_job_id,
                    artifact.workflow_id,
                    artifact.digest,
                    artifact.manifest_digest,
                    artifact.model_dump_json(),
                    artifact.created_at.isoformat(),
                ),
            )
            await db.execute(
                """
                UPDATE spool_reservations
                SET status = 'released', updated_at = ?
                WHERE slice_job_id = ? AND status = 'reserved'
                """,
                (utc_now().isoformat(), job.id),
            )
            now = utc_now()
            cursor = await db.execute(
                """
                UPDATE workflows SET state = ?, version = version + 1, updated_at = ?
                WHERE id = ? AND version = ?
                """,
                (
                    WorkflowState.AWAITING_SLICE_REVIEW.value,
                    now.isoformat(),
                    job.workflow_id,
                    workflow["version"],
                ),
            )
            if cursor.rowcount != 1:
                raise ConflictError("Workflow changed while finalizing slice")
            await self._insert_event(
                db,
                job.workflow_id,
                "slice.ready",
                WorkflowState.AWAITING_SLICE_REVIEW,
                {
                    "slice_job_id": job.id,
                    "sliced_artifact_digest": artifact.digest,
                },
            )
            await db.commit()
        except Exception:
            await db.rollback()
            raise
        finally:
            await db.close()

    async def fail_slice(self, job: SliceJob, message: str) -> None:
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
            if workflow["state"] == WorkflowState.CANCELLED.value:
                cancelled_job = job.model_copy(
                    update={
                        "status": SliceJobStatus.CANCELLED,
                        "message": "Slice cancelled",
                        "updated_at": utc_now(),
                    }
                )
                await db.execute(
                    """
                    UPDATE slice_jobs SET status = ?, payload_json = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        cancelled_job.status.value,
                        cancelled_job.model_dump_json(),
                        cancelled_job.updated_at.isoformat(),
                        cancelled_job.id,
                    ),
                )
                await db.execute(
                    """
                    UPDATE spool_reservations SET status = 'released', updated_at = ?
                    WHERE slice_job_id = ? AND status = 'reserved'
                    """,
                    (utc_now().isoformat(), job.id),
                )
                await db.commit()
                return
            if workflow["state"] not in {
                WorkflowState.SLICE_REQUESTED.value,
                WorkflowState.SLICING.value,
                WorkflowState.SLICE_VALIDATING.value,
            }:
                raise ConflictError("Workflow is no longer running this slice")
            failed_job = job.model_copy(
                update={
                    "status": SliceJobStatus.FAILED,
                    "message": message[-2_000:],
                    "updated_at": utc_now(),
                }
            )
            await db.execute(
                """
                UPDATE slice_jobs SET status = ?, payload_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    failed_job.status.value,
                    failed_job.model_dump_json(),
                    failed_job.updated_at.isoformat(),
                    failed_job.id,
                ),
            )
            await db.execute(
                """
                UPDATE spool_reservations
                SET status = 'released', updated_at = ?
                WHERE slice_job_id = ? AND status = 'reserved'
                """,
                (utc_now().isoformat(), job.id),
            )
            now = utc_now()
            cursor = await db.execute(
                """
                UPDATE workflows SET state = ?, version = version + 1, updated_at = ?
                WHERE id = ? AND version = ?
                """,
                (
                    WorkflowState.SLICE_FAILED.value,
                    now.isoformat(),
                    job.workflow_id,
                    workflow["version"],
                ),
            )
            if cursor.rowcount != 1:
                raise ConflictError("Workflow changed while failing slice")
            await self._insert_event(
                db,
                job.workflow_id,
                "slice.failed",
                WorkflowState.SLICE_FAILED,
                {"slice_job_id": job.id, "message": message[-2_000:]},
            )
            await db.commit()
        except Exception:
            await db.rollback()
            raise
        finally:
            await db.close()

    async def list_spool_reservations(
        self,
        slice_job_id: str,
    ) -> list[SpoolReservation]:
        db = await self._connect()
        try:
            cursor = await db.execute(
                """
                SELECT payload_json, status, actual_usage_g
                FROM spool_reservations
                WHERE slice_job_id = ? ORDER BY spool_id
                """,
                (slice_job_id,),
            )
            return [
                SpoolReservation.model_validate_json(row["payload_json"]).model_copy(
                    update={
                        "status": row["status"],
                        "actual_usage_g": row["actual_usage_g"],
                    }
                )
                for row in await cursor.fetchall()
            ]
        finally:
            await db.close()

    async def available_spool_weights(
        self,
        spool_ids: set[str],
    ) -> dict[str, float | None]:
        if not spool_ids:
            return {}
        db = await self._connect()
        try:
            output: dict[str, float | None] = {}
            for spool_id in sorted(spool_ids):
                cursor = await db.execute(
                    "SELECT payload_json FROM physical_spools WHERE id = ?",
                    (spool_id,),
                )
                row = await cursor.fetchone()
                if row is None:
                    continue
                spool = PhysicalSpool.model_validate_json(row["payload_json"])
                cursor = await db.execute(
                    """
                    SELECT COALESCE(SUM(reserved_weight_g), 0) AS reserved
                    FROM spool_reservations
                    WHERE spool_id = ? AND status = 'reserved'
                    """,
                    (spool_id,),
                )
                reserved = float((await cursor.fetchone())["reserved"])
                output[spool_id] = (
                    None
                    if spool.remaining_weight_g is None
                    else max(0.0, spool.remaining_weight_g - reserved)
                )
            return output
        finally:
            await db.close()

    async def reconcile_spool_reservations(
        self,
        slice_job_id: str,
        usage_by_spool: dict[str, float],
        material_safety_margin_percent: float = 15,
    ) -> SpoolReconciliationResult:
        if not usage_by_spool:
            raise ConflictError("Sliced output has no reconcilable filament usage")
        db = await self._connect()
        try:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                """
                SELECT id, spool_id, payload_json FROM spool_reservations
                WHERE slice_job_id = ? AND status = 'reserved'
                """,
                (slice_job_id,),
            )
            reservation_rows = await cursor.fetchall()
            reservations = {
                str(row["spool_id"]): row for row in reservation_rows
            }
            reserved_spool_ids = set(reservations)
            if set(usage_by_spool) != reserved_spool_ids:
                raise ConflictError(
                    "Sliced filament usage does not match every reserved spool"
                )
            requirements: list[SpoolUsageRequirement] = []
            for spool_id, usage in usage_by_spool.items():
                if not math.isfinite(usage) or usage <= 0:
                    raise ConflictError(
                        f"Sliced usage for spool '{spool_id}' must be finite and positive"
                    )
                required = usage * (
                    1 + material_safety_margin_percent / 100
                )
                cursor = await db.execute(
                    "SELECT payload_json FROM physical_spools WHERE id = ?",
                    (spool_id,),
                )
                row = await cursor.fetchone()
                if row is None:
                    raise ConflictError(f"Spool '{spool_id}' was removed")
                spool = PhysicalSpool.model_validate_json(row["payload_json"])
                cursor = await db.execute(
                    """
                    SELECT COALESCE(SUM(reserved_weight_g), 0) AS reserved
                    FROM spool_reservations
                    WHERE spool_id = ? AND status = 'reserved'
                      AND slice_job_id != ?
                    """,
                    (spool_id, slice_job_id),
                )
                other_reserved = float((await cursor.fetchone())["reserved"])
                available = (
                    None
                    if spool.remaining_weight_g is None
                    else max(0.0, spool.remaining_weight_g - other_reserved)
                )
                requirements.append(
                    SpoolUsageRequirement(
                        spool_id=spool_id,
                        slot_id=spool.slot_id,
                        actual_usage_g=usage,
                        safety_margin_percent=material_safety_margin_percent,
                        required_weight_g=required,
                        available_weight_g=available,
                        quantity_status=spool.quantity_status,
                        other_reserved_weight_g=(
                            0
                            if spool.quantity_status
                            == SpoolQuantityStatus.USER_ATTESTED_UNKNOWN
                            else other_reserved
                        ),
                        shortfall_g=(
                            0
                            if available is None
                            else max(0.0, required - available)
                        ),
                    )
                )
            result = SpoolReconciliationResult(requirements=tuple(requirements))
            has_shortage = bool(result.shortages)
            for requirement in requirements:
                spool_id = requirement.spool_id
                usage = requirement.actual_usage_g
                required = requirement.required_weight_g
                status = (
                    "insufficient"
                    if requirement.shortfall_g > 0
                    else "released"
                    if has_shortage
                    else "reserved"
                )
                cursor = await db.execute(
                    """
                    UPDATE spool_reservations
                    SET reserved_weight_g = ?, actual_usage_g = ?, status = ?,
                        payload_json = ?, updated_at = ?
                    WHERE spool_id = ? AND slice_job_id = ?
                      AND status = 'reserved'
                    """,
                    (
                        required,
                        usage,
                        status,
                        SpoolReservation.model_validate_json(
                            reservations[spool_id]["payload_json"]
                        ).model_copy(
                            update={
                                "reserved_weight_g": required,
                                "actual_usage_g": usage,
                                "status": status,
                                "updated_at": utc_now(),
                            }
                        ).model_dump_json(),
                        utc_now().isoformat(),
                        spool_id,
                        slice_job_id,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ConflictError(
                        f"Reservation for spool '{spool_id}' is no longer active"
                    )
            await db.commit()
            return result
        except Exception:
            await db.rollback()
            raise
        finally:
            await db.close()

    async def complete_spool_reservations(
        self,
        slice_job_id: str,
    ) -> None:
        db = await self._connect()
        try:
            await db.execute("BEGIN IMMEDIATE")
            now = utc_now()
            await db.execute(
                """
                UPDATE spool_reservations
                SET status = 'released', updated_at = ?
                WHERE slice_job_id = ? AND status = 'reserved'
                """,
                (now.isoformat(), slice_job_id),
            )
            await db.commit()
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
                INSERT INTO revision_requests (
                    workflow_id, mode, feedback, part_id, allowed_part_ids_json,
                    base_artifact_version, base_handoff_version, base_state, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    revision.workflow_id,
                    revision.mode.value,
                    revision.feedback,
                    None,
                    (
                        json.dumps(revision.allowed_part_ids)
                        if revision.allowed_part_ids is not None
                        else None
                    ),
                    revision.base_artifact_version,
                    revision.base_handoff_version,
                    revision.base_state.value if revision.base_state is not None else None,
                    revision.created_at.isoformat(),
                ),
            )
            await db.execute(
                "DELETE FROM active_revision_failures WHERE workflow_id = ?",
                (revision.workflow_id,),
            )
            if revision.mode == RevisionMode.SEARCH_NEW_BASE:
                await db.execute(
                    "DELETE FROM approvals WHERE workflow_id = ?",
                    (revision.workflow_id,),
                )
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
                """
                SELECT state, version, archived_at, active_artifact_version,
                       active_handoff_version
                FROM workflows WHERE id = ?
                """,
                (revision.workflow_id,),
            )
            workflow = await cursor.fetchone()
            if workflow is None:
                raise NotFoundError(f"Workflow '{revision.workflow_id}' was not found")
            if workflow["archived_at"] is not None:
                raise ConflictError("Restore the archived workflow before changing it")
            current_state = WorkflowState(workflow["state"])
            if current_state not in {
                WorkflowState.AWAITING_APPROVAL,
                WorkflowState.APPROVED,
                WorkflowState.PREPARATION_FAILED,
            }:
                raise ConflictError("Only an unsubmitted or failed preparation can be revised")
            assert_transition(current_state, WorkflowState.REVISION_REQUESTED)
            await db.execute(
                """
                INSERT INTO revision_requests (
                    workflow_id, mode, feedback, part_id, allowed_part_ids_json,
                    base_artifact_version, base_handoff_version, base_state, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    revision.workflow_id,
                    revision.mode.value,
                    revision.feedback,
                    None,
                    (
                        json.dumps(revision.allowed_part_ids)
                        if revision.allowed_part_ids is not None
                        else None
                    ),
                    workflow["active_artifact_version"],
                    workflow["active_handoff_version"],
                    current_state.value,
                    revision.created_at.isoformat(),
                ),
            )
            await db.execute(
                "DELETE FROM active_revision_failures WHERE workflow_id = ?",
                (revision.workflow_id,),
            )
            if revision.mode == RevisionMode.SEARCH_NEW_BASE:
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
                {
                    "mode": revision.mode.value,
                    "feedback": revision.feedback,
                    "allowed_part_ids": revision.allowed_part_ids,
                    "base_artifact_version": workflow["active_artifact_version"],
                    "base_handoff_version": workflow["active_handoff_version"],
                    "base_state": current_state.value,
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

    async def create_copy(
        self,
        workflow: PrintWorkflow,
        plan: ModelPlan,
        handoff: ModelingHandoff | None,
        artifact: ModelArtifact,
        snapshot: WorkflowPrinterSnapshot,
        approval: ArtifactApproval | None,
        *,
        source_workflow_id: str,
        source_artifact_version: int,
        fabrication: bool = False,
    ) -> PrintWorkflow:
        if snapshot.workflow_id != workflow.id:
            raise ValueError("Copy printer snapshot must match the new workflow")
        if approval is not None and (
            approval.workflow_id != workflow.id
            or approval.artifact_version != artifact.version
            or approval.manifest_digest != artifact.manifest_digest
            or workflow.state != WorkflowState.APPROVED
        ):
            raise ValueError("Carried approval must match the copied artifact")
        db = await self._connect()
        try:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                """
                SELECT archived_at, active_artifact_version
                FROM workflows WHERE id = ?
                """,
                (source_workflow_id,),
            )
            source = await cursor.fetchone()
            if source is None:
                raise NotFoundError(f"Workflow '{source_workflow_id}' was not found")
            if source["archived_at"] is not None:
                raise ConflictError("Restore the archived workflow before copying it")
            if int(source["active_artifact_version"] or 0) != source_artifact_version:
                raise ConflictError(
                    "Source workflow artifact changed before the copy was created"
                )
            if fabrication:
                cursor = await db.execute(
                    """
                    SELECT p.active_revision, r.digest
                    FROM printer_profiles p
                    JOIN printer_profile_revisions r
                      ON r.profile_id = p.id AND r.revision = p.active_revision
                    WHERE p.id = ? AND p.enabled = 1 AND p.archived_at IS NULL
                    """,
                    (snapshot.profile_id,),
                )
                profile_row = await cursor.fetchone()
                profile_valid = (
                    profile_row is not None
                    and int(profile_row["active_revision"])
                    == snapshot.profile_revision
                    and profile_row["digest"] == snapshot.profile_digest
                )
            else:
                cursor = await db.execute(
                    """
                    SELECT digest FROM printer_profile_revisions
                    WHERE profile_id = ? AND revision = ?
                    """,
                    (snapshot.profile_id, snapshot.profile_revision),
                )
                profile_row = await cursor.fetchone()
                profile_valid = (
                    profile_row is not None
                    and profile_row["digest"] == snapshot.profile_digest
                )
            if not profile_valid:
                raise ConflictError(
                    "Printer profile changed or was unavailable before copy creation"
                )
            await db.execute(
                """
                INSERT INTO workflows (
                    id, requirement, printer_name, state, version,
                    discovery_session_id, modeling_session_id,
                    active_handoff_version, active_artifact_version,
                    failure_code, failure_message, archived_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, NULL, NULL, ?, ?, NULL, NULL, NULL, ?, ?)
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
            await db.execute(
                """
                INSERT INTO workflow_printer_snapshots (
                    workflow_id, profile_id, profile_revision,
                    digest, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot.workflow_id,
                    snapshot.profile_id,
                    snapshot.profile_revision,
                    snapshot.digest,
                    snapshot.model_dump_json(),
                    snapshot.created_at.isoformat(),
                ),
            )
            if approval is not None:
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
            await self._insert_event(
                db,
                workflow.id,
                (
                    "workflow.fabrication_copy_created"
                    if fabrication
                    else "workflow.copied"
                ),
                workflow.state,
                {
                    "source_workflow_id": source_workflow_id,
                    "source_artifact_version": source_artifact_version,
                    "target_profile_id": snapshot.profile_id,
                    "approval_carried": approval is not None,
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
            allowed_part_ids = (
                json.loads(row["allowed_part_ids_json"])
                if row["allowed_part_ids_json"] is not None
                else [row["part_id"]]
                if row["part_id"] is not None
                else None
            )
            return RevisionRequest(
                workflow_id=row["workflow_id"],
                mode=RevisionMode(row["mode"]),
                feedback=row["feedback"],
                allowed_part_ids=allowed_part_ids,
                base_artifact_version=row["base_artifact_version"],
                base_handoff_version=row["base_handoff_version"],
                base_state=(
                    WorkflowState(row["base_state"])
                    if row["base_state"] is not None
                    else None
                ),
                created_at=row["created_at"],
            )
        finally:
            await db.close()

    async def save_printer_profile(
        self,
        profile: PrinterProfileRevision,
        *,
        enabled: bool = True,
        expected_revision: int | None = None,
    ) -> None:
        if profile.digest is None:
            raise ValueError("Printer profile revision requires a digest")
        db = await self._connect()
        try:
            await db.execute("BEGIN IMMEDIATE")
            if expected_revision is not None:
                cursor = await db.execute(
                    """
                    SELECT active_revision FROM printer_profiles
                    WHERE id = ? AND archived_at IS NULL
                    """,
                    (profile.profile_id,),
                )
                row = await cursor.fetchone()
                current_revision = (
                    int(row["active_revision"]) if row is not None else 0
                )
                if current_revision != expected_revision:
                    raise ConflictError(
                        "Printer profile changed; reload before saving a new revision"
                    )
            await db.execute(
                """
                INSERT INTO printer_profiles (
                    id, active_revision, enabled, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    active_revision = excluded.active_revision,
                    enabled = excluded.enabled,
                    archived_at = NULL,
                    updated_at = excluded.updated_at
                """,
                (
                    profile.profile_id,
                    profile.revision,
                    int(enabled),
                    profile.created_at.isoformat(),
                    utc_now().isoformat(),
                ),
            )
            await db.execute(
                """
                INSERT INTO printer_profile_revisions (
                    profile_id, revision, digest, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    profile.profile_id,
                    profile.revision,
                    profile.digest,
                    profile.model_dump_json(),
                    profile.created_at.isoformat(),
                ),
            )
            await db.commit()
        except aiosqlite.IntegrityError as exc:
            await db.rollback()
            raise ConflictError("Printer profile revision already exists") from exc
        except Exception:
            await db.rollback()
            raise
        finally:
            await db.close()

    async def get_printer_profile(
        self,
        profile_id: str,
        revision: int | None = None,
    ) -> PrinterProfileRevision:
        db = await self._connect()
        try:
            if revision is None:
                cursor = await db.execute(
                    """
                    SELECT active_revision FROM printer_profiles
                    WHERE id = ? AND enabled = 1 AND archived_at IS NULL
                    """,
                    (profile_id,),
                )
                row = await cursor.fetchone()
                if row is None:
                    raise NotFoundError(f"Printer profile '{profile_id}' was not found")
                revision = int(row["active_revision"])
            cursor = await db.execute(
                """
                SELECT payload_json FROM printer_profile_revisions
                WHERE profile_id = ? AND revision = ?
                """,
                (profile_id, revision),
            )
            row = await cursor.fetchone()
            if row is None:
                raise NotFoundError(
                    f"Printer profile '{profile_id}' revision {revision} was not found"
                )
            return PrinterProfileRevision.model_validate_json(row["payload_json"])
        finally:
            await db.close()

    async def list_printer_profiles(
        self,
        *,
        include_archived: bool = False,
    ) -> list[PrinterProfileRevision]:
        db = await self._connect()
        try:
            cursor = await db.execute(
                """
                SELECT r.payload_json
                FROM printer_profiles p
                JOIN printer_profile_revisions r
                  ON r.profile_id = p.id AND r.revision = p.active_revision
                WHERE (? OR p.archived_at IS NULL)
                ORDER BY p.id
                """,
                (int(include_archived),),
            )
            return [
                PrinterProfileRevision.model_validate_json(row["payload_json"])
                for row in await cursor.fetchall()
            ]
        finally:
            await db.close()

    async def archive_printer_profile(self, profile_id: str) -> None:
        db = await self._connect()
        try:
            cursor = await db.execute(
                """
                UPDATE printer_profiles
                SET archived_at = ?, enabled = 0, updated_at = ?
                WHERE id = ? AND archived_at IS NULL
                """,
                (utc_now().isoformat(), utc_now().isoformat(), profile_id),
            )
            if cursor.rowcount != 1:
                raise NotFoundError(
                    f"Active printer profile '{profile_id}' was not found"
                )
            await db.commit()
        finally:
            await db.close()

    async def save_workflow_printer_snapshot(
        self,
        snapshot: WorkflowPrinterSnapshot,
    ) -> None:
        if snapshot.digest is None:
            raise ValueError("Workflow printer snapshot requires a digest")
        db = await self._connect()
        try:
            await db.execute(
                """
                INSERT INTO workflow_printer_snapshots (
                    workflow_id, profile_id, profile_revision, digest,
                    payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot.workflow_id,
                    snapshot.profile_id,
                    snapshot.profile_revision,
                    snapshot.digest,
                    snapshot.model_dump_json(),
                    snapshot.created_at.isoformat(),
                ),
            )
            await db.commit()
        except aiosqlite.IntegrityError as exc:
            raise ConflictError("Workflow printer snapshot already exists") from exc
        finally:
            await db.close()

    async def get_workflow_printer_snapshot(
        self,
        workflow_id: str,
    ) -> WorkflowPrinterSnapshot:
        payload = await self._get_single_payload(
            "workflow_printer_snapshots",
            "workflow_id",
            workflow_id,
        )
        return WorkflowPrinterSnapshot.model_validate_json(payload)

    async def save_cloud_device_snapshot(
        self,
        workflow_id: str,
        profile_id: str,
        snapshot: CloudDeviceSnapshot,
    ) -> None:
        db = await self._connect()
        try:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                "SELECT id FROM workflows WHERE id = ?",
                (workflow_id,),
            )
            if await cursor.fetchone() is None:
                raise NotFoundError(f"Workflow '{workflow_id}' was not found")
            await db.execute(
                """
                INSERT INTO cloud_device_snapshots (
                    id, workflow_id, profile_id, digest, observed_at,
                    expires_at, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot.id,
                    workflow_id,
                    profile_id,
                    snapshot.digest,
                    snapshot.observed_at.isoformat(),
                    snapshot.expires_at.isoformat(),
                    snapshot.model_dump_json(),
                    utc_now().isoformat(),
                ),
            )
            await db.commit()
        except Exception:
            await db.rollback()
            raise
        finally:
            await db.close()

    async def get_latest_cloud_device_snapshot(
        self,
        workflow_id: str,
    ) -> CloudDeviceSnapshot:
        db = await self._connect()
        try:
            cursor = await db.execute(
                """
                SELECT payload_json FROM cloud_device_snapshots
                WHERE workflow_id = ?
                ORDER BY observed_at DESC, created_at DESC LIMIT 1
                """,
                (workflow_id,),
            )
            row = await cursor.fetchone()
            if row is None:
                raise NotFoundError("Workflow has no cloud device snapshot")
            return CloudDeviceSnapshot.model_validate_json(row["payload_json"])
        finally:
            await db.close()

    async def save_material_definition(
        self,
        material: MaterialDefinitionRevision,
        *,
        enabled: bool = True,
        disable_material_ids: set[str] | None = None,
        disable_cloud_filament_ids: set[str] | None = None,
    ) -> None:
        if material.digest is None:
            raise ValueError("Material definition revision requires a digest")
        db = await self._connect()
        try:
            await db.execute("BEGIN IMMEDIATE")
            disabled = set(disable_material_ids or set())
            if disable_cloud_filament_ids:
                cursor = await db.execute(
                    """
                    SELECT m.id, r.payload_json
                    FROM material_definitions m
                    JOIN material_definition_revisions r
                      ON r.material_id = m.id AND r.revision = m.active_revision
                    WHERE m.enabled = 1 AND m.archived_at IS NULL
                    """
                )
                for row in await cursor.fetchall():
                    definition = MaterialDefinitionRevision.model_validate_json(
                        row["payload_json"]
                    )
                    if (
                        definition.material_id != material.material_id
                        and definition.spec.cloud_filament_ids
                        & disable_cloud_filament_ids
                    ):
                        disabled.add(definition.material_id)
            for material_id in sorted(disabled):
                if material_id == material.material_id:
                    continue
                await db.execute(
                    """
                    UPDATE material_definitions
                    SET enabled = 0, archived_at = ?, updated_at = ?
                    WHERE id = ? AND enabled = 1
                    """,
                    (
                        utc_now().isoformat(),
                        utc_now().isoformat(),
                        material_id,
                    ),
                )
            await db.execute(
                """
                INSERT INTO material_definitions (
                    id, active_revision, enabled, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    active_revision = excluded.active_revision,
                    enabled = excluded.enabled,
                    archived_at = NULL,
                    updated_at = excluded.updated_at
                """,
                (
                    material.material_id,
                    material.revision,
                    int(enabled),
                    material.created_at.isoformat(),
                    utc_now().isoformat(),
                ),
            )
            await db.execute(
                """
                INSERT INTO material_definition_revisions (
                    material_id, revision, digest, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    material.material_id,
                    material.revision,
                    material.digest,
                    material.model_dump_json(),
                    material.created_at.isoformat(),
                ),
            )
            await db.commit()
        except aiosqlite.IntegrityError as exc:
            await db.rollback()
            raise ConflictError("Material definition revision already exists") from exc
        finally:
            await db.close()

    async def disable_material_definitions(
        self,
        material_ids: set[str],
    ) -> None:
        if not material_ids:
            return
        db = await self._connect()
        try:
            await db.execute("BEGIN IMMEDIATE")
            now = utc_now().isoformat()
            for material_id in sorted(material_ids):
                await db.execute(
                    """
                    UPDATE material_definitions
                    SET enabled = 0, archived_at = ?, updated_at = ?
                    WHERE id = ? AND enabled = 1
                    """,
                    (now, now, material_id),
                )
            await db.commit()
        except Exception:
            await db.rollback()
            raise
        finally:
            await db.close()

    async def get_material_definition(
        self,
        material_id: str,
        revision: int | None = None,
    ) -> MaterialDefinitionRevision:
        db = await self._connect()
        try:
            if revision is None:
                cursor = await db.execute(
                    "SELECT active_revision FROM material_definitions WHERE id = ?",
                    (material_id,),
                )
                row = await cursor.fetchone()
                if row is None:
                    raise NotFoundError(
                        f"Material definition '{material_id}' was not found"
                    )
                revision = int(row["active_revision"])
            cursor = await db.execute(
                """
                SELECT payload_json FROM material_definition_revisions
                WHERE material_id = ? AND revision = ?
                """,
                (material_id, revision),
            )
            row = await cursor.fetchone()
            if row is None:
                raise NotFoundError(
                    f"Material definition '{material_id}' revision {revision} was not found"
                )
            return MaterialDefinitionRevision.model_validate_json(
                row["payload_json"]
            )
        finally:
            await db.close()

    async def upsert_automatic_material_definition(
        self,
        material_id: str,
        spec: MaterialDefinitionSpec,
    ) -> MaterialDefinitionRevision:
        if spec.mapping_origin == "manual":
            raise ValueError("Automatic material upsert requires automatic provenance")
        db = await self._connect()
        try:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                """
                SELECT m.id, r.payload_json
                FROM material_definitions m
                JOIN material_definition_revisions r
                  ON r.material_id = m.id AND r.revision = m.active_revision
                WHERE m.enabled = 1 AND m.archived_at IS NULL
                """
            )
            active = [
                MaterialDefinitionRevision.model_validate_json(row["payload_json"])
                for row in await cursor.fetchall()
            ]
            manual = [
                item
                for item in active
                if item.spec.mapping_origin == "manual"
                and item.spec.cloud_filament_ids & spec.cloud_filament_ids
            ]
            if len(manual) > 1:
                raise ConflictError(
                    "Multiple manual material mappings claim this cloud filament ID"
                )
            if manual:
                winner = manual[0]
                now = utc_now().isoformat()
                for item in active:
                    if (
                        item.material_id != winner.material_id
                        and item.spec.mapping_origin != "manual"
                        and item.spec.cloud_filament_ids & spec.cloud_filament_ids
                    ):
                        await db.execute(
                            """
                            UPDATE material_definitions
                            SET enabled = 0, archived_at = ?, updated_at = ?
                            WHERE id = ?
                            """,
                            (now, now, item.material_id),
                        )
                await db.commit()
                return winner

            cursor = await db.execute(
                """
                SELECT r.payload_json
                FROM material_definitions m
                JOIN material_definition_revisions r
                  ON r.material_id = m.id AND r.revision = m.active_revision
                WHERE m.id = ?
                """,
                (material_id,),
            )
            row = await cursor.fetchone()
            current = (
                MaterialDefinitionRevision.model_validate_json(row["payload_json"])
                if row is not None
                else None
            )
            if current is not None and current.spec.mapping_origin == "manual":
                raise ConflictError(
                    f"Material ID '{material_id}' is reserved by a manual mapping"
                )
            now = utc_now()
            if current is not None and current.spec == spec:
                await db.execute(
                    """
                    UPDATE material_definitions
                    SET enabled = 1, archived_at = NULL, updated_at = ?
                    WHERE id = ?
                    """,
                    (now.isoformat(), material_id),
                )
                await db.commit()
                return current
            material = MaterialDefinitionRevision(
                material_id=material_id,
                revision=(current.revision + 1 if current else 1),
                spec=spec,
                created_at=now,
            ).with_digest()
            await db.execute(
                """
                INSERT INTO material_definitions (
                    id, active_revision, enabled, created_at, updated_at
                ) VALUES (?, ?, 1, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    active_revision = excluded.active_revision,
                    enabled = 1,
                    archived_at = NULL,
                    updated_at = excluded.updated_at
                """,
                (
                    material.material_id,
                    material.revision,
                    material.created_at.isoformat(),
                    now.isoformat(),
                ),
            )
            await db.execute(
                """
                INSERT INTO material_definition_revisions (
                    material_id, revision, digest, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    material.material_id,
                    material.revision,
                    material.digest,
                    material.model_dump_json(),
                    material.created_at.isoformat(),
                ),
            )
            await db.commit()
            return material
        except Exception:
            await db.rollback()
            raise
        finally:
            await db.close()

    async def list_material_definitions(self) -> list[MaterialDefinitionRevision]:
        db = await self._connect()
        try:
            cursor = await db.execute(
                """
                SELECT r.payload_json
                FROM material_definitions m
                JOIN material_definition_revisions r
                  ON r.material_id = m.id AND r.revision = m.active_revision
                WHERE m.archived_at IS NULL AND m.enabled = 1
                ORDER BY m.id
                """
            )
            return [
                MaterialDefinitionRevision.model_validate_json(row["payload_json"])
                for row in await cursor.fetchall()
            ]
        finally:
            await db.close()

    async def save_spool(self, spool: PhysicalSpool) -> None:
        db = await self._connect()
        try:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                """
                SELECT COUNT(*) AS count FROM spool_reservations
                WHERE spool_id = ? AND status = 'reserved'
                """,
                (spool.id,),
            )
            has_reservation = int((await cursor.fetchone())["count"]) > 0
            if has_reservation:
                cursor = await db.execute(
                    "SELECT payload_json FROM physical_spools WHERE id = ?",
                    (spool.id,),
                )
                row = await cursor.fetchone()
                if row is None:
                    raise ConflictError("Reserved spool record is unavailable")
                current = PhysicalSpool.model_validate_json(row["payload_json"])
                if (
                    current.material_id != spool.material_id
                    or current.material_revision != spool.material_revision
                    or current.material_digest != spool.material_digest
                    or (
                        not current.id.startswith("cloud-")
                        and current.printer_profile_id != spool.printer_profile_id
                    )
                    or current.slot_id != spool.slot_id
                    or current.status != spool.status
                    or current.quantity_status != spool.quantity_status
                    or current.tray_identity_digest != spool.tray_identity_digest
                    or current.remaining_weight_g != spool.remaining_weight_g
                    or (
                        current.quantity_status
                        == SpoolQuantityStatus.CLOUD_ESTIMATE
                        and current.cloud_snapshot_digest
                        != spool.cloud_snapshot_digest
                    )
                ):
                    raise ConflictError(
                        "Reserved spool material, slot, status, and quantity are immutable"
                    )
            await db.execute(
                """
                INSERT INTO physical_spools (
                    id, material_id, material_revision, status,
                    printer_profile_id, slot_id, payload_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    material_id = excluded.material_id,
                    material_revision = excluded.material_revision,
                    status = excluded.status,
                    printer_profile_id = excluded.printer_profile_id,
                    slot_id = excluded.slot_id,
                    payload_json = excluded.payload_json,
                    updated_at = excluded.updated_at
                """,
                (
                    spool.id,
                    spool.material_id,
                    spool.material_revision,
                    spool.status.value,
                    spool.printer_profile_id,
                    spool.slot_id,
                    spool.model_dump_json(),
                    spool.updated_at.isoformat(),
                ),
            )
            await db.commit()
        except aiosqlite.IntegrityError as exc:
            await db.rollback()
            raise ConflictError("Material slot already contains another spool") from exc
        except Exception:
            await db.rollback()
            raise
        finally:
            await db.close()

    async def save_unknown_quantity_authorization(
        self,
        authorization: UnknownQuantitySlotAuthorization,
    ) -> None:
        db = await self._connect()
        try:
            await db.execute(
                """
                INSERT INTO unknown_quantity_slot_authorizations (
                    profile_id, device_ref, slot_id, tray_identity_digest,
                    payload_json, authorized_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(profile_id, device_ref, slot_id) DO UPDATE SET
                    tray_identity_digest = excluded.tray_identity_digest,
                    payload_json = excluded.payload_json,
                    authorized_at = excluded.authorized_at
                """,
                (
                    authorization.profile_id,
                    authorization.device_ref,
                    authorization.slot_id,
                    authorization.tray_identity_digest,
                    authorization.model_dump_json(),
                    authorization.authorized_at.isoformat(),
                ),
            )
            await db.commit()
        finally:
            await db.close()

    async def list_unknown_quantity_authorizations(
        self,
        profile_id: str,
        device_ref: str,
    ) -> list[UnknownQuantitySlotAuthorization]:
        db = await self._connect()
        try:
            cursor = await db.execute(
                """
                SELECT payload_json
                FROM unknown_quantity_slot_authorizations
                WHERE profile_id = ? AND device_ref = ?
                ORDER BY slot_id
                """,
                (profile_id, device_ref),
            )
            return [
                UnknownQuantitySlotAuthorization.model_validate_json(
                    row["payload_json"]
                )
                for row in await cursor.fetchall()
            ]
        finally:
            await db.close()

    async def revoke_unknown_quantity_authorization(
        self,
        profile_id: str,
        device_ref: str,
        slot_id: str,
    ) -> None:
        db = await self._connect()
        try:
            await db.execute(
                """
                DELETE FROM unknown_quantity_slot_authorizations
                WHERE profile_id = ? AND device_ref = ? AND slot_id = ?
                """,
                (profile_id, device_ref, slot_id),
            )
            await db.commit()
        finally:
            await db.close()

    async def get_spool(self, spool_id: str) -> PhysicalSpool:
        payload = await self._get_single_payload("physical_spools", "id", spool_id)
        return PhysicalSpool.model_validate_json(payload)

    async def list_spools(
        self,
        *,
        printer_profile_id: str | None = None,
    ) -> list[PhysicalSpool]:
        db = await self._connect()
        try:
            if printer_profile_id is None:
                cursor = await db.execute(
                    "SELECT payload_json FROM physical_spools ORDER BY id"
                )
            else:
                cursor = await db.execute(
                    """
                    SELECT payload_json FROM physical_spools
                    WHERE printer_profile_id = ? ORDER BY slot_id, id
                    """,
                    (printer_profile_id,),
                )
            return [
                PhysicalSpool.model_validate_json(row["payload_json"])
                for row in await cursor.fetchall()
            ]
        finally:
            await db.close()

    async def save_material_assignment(
        self,
        assignment: MaterialAssignment,
    ) -> None:
        if assignment.digest is None:
            raise ValueError("Material assignment requires a digest")
        db = await self._connect()
        try:
            await db.execute(
                """
                INSERT INTO material_assignments (
                    id, workflow_id, artifact_version, digest,
                    confirmed, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    digest = excluded.digest,
                    confirmed = excluded.confirmed,
                    payload_json = excluded.payload_json,
                    created_at = excluded.created_at
                """,
                (
                    assignment.id,
                    assignment.workflow_id,
                    assignment.artifact_version,
                    assignment.digest,
                    int(assignment.confirmed_at is not None),
                    assignment.model_dump_json(),
                    assignment.created_at.isoformat(),
                ),
            )
            await db.commit()
        finally:
            await db.close()

    async def get_latest_material_assignment(
        self,
        workflow_id: str,
    ) -> MaterialAssignment:
        db = await self._connect()
        try:
            cursor = await db.execute(
                """
                SELECT payload_json FROM material_assignments
                WHERE workflow_id = ? ORDER BY created_at DESC LIMIT 1
                """,
                (workflow_id,),
            )
            row = await cursor.fetchone()
            if row is None:
                raise NotFoundError("Workflow has no material assignment")
            return MaterialAssignment.model_validate_json(row["payload_json"])
        finally:
            await db.close()

    async def save_slice_job(self, job: SliceJob) -> None:
        db = await self._connect()
        try:
            await db.execute(
                """
                INSERT INTO slice_jobs (
                    id, workflow_id, status, idempotency_key,
                    payload_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(idempotency_key) DO UPDATE SET
                    status = excluded.status,
                    payload_json = excluded.payload_json,
                    updated_at = excluded.updated_at
                """,
                (
                    job.id,
                    job.workflow_id,
                    job.status.value,
                    job.idempotency_key,
                    job.model_dump_json(),
                    job.created_at.isoformat(),
                    job.updated_at.isoformat(),
                ),
            )
            await db.commit()
        finally:
            await db.close()

    async def get_slice_job(self, job_id: str) -> SliceJob:
        payload = await self._get_single_payload("slice_jobs", "id", job_id)
        return SliceJob.model_validate_json(payload)

    async def get_latest_slice_job(self, workflow_id: str) -> SliceJob:
        db = await self._connect()
        try:
            cursor = await db.execute(
                """
                SELECT payload_json FROM slice_jobs
                WHERE workflow_id = ? ORDER BY created_at DESC LIMIT 1
                """,
                (workflow_id,),
            )
            row = await cursor.fetchone()
            if row is None:
                raise NotFoundError("Workflow has no slice job")
            return SliceJob.model_validate_json(row["payload_json"])
        finally:
            await db.close()

    async def save_sliced_artifact(self, artifact: SlicedArtifact) -> None:
        db = await self._connect()
        try:
            await db.execute(
                """
                INSERT INTO sliced_artifacts (
                    slice_job_id, workflow_id, digest, manifest_digest,
                    payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    artifact.slice_job_id,
                    artifact.workflow_id,
                    artifact.digest,
                    artifact.manifest_digest,
                    artifact.model_dump_json(),
                    artifact.created_at.isoformat(),
                ),
            )
            await db.commit()
        finally:
            await db.close()

    async def get_sliced_artifact(self, slice_job_id: str) -> SlicedArtifact:
        payload = await self._get_single_payload(
            "sliced_artifacts",
            "slice_job_id",
            slice_job_id,
        )
        return SlicedArtifact.model_validate_json(payload)

    async def _get_single_payload(
        self,
        table: str,
        key_column: str,
        key_value: str,
    ) -> str:
        allowed = {
            ("workflow_printer_snapshots", "workflow_id"),
            ("physical_spools", "id"),
            ("slice_jobs", "id"),
            ("sliced_artifacts", "slice_job_id"),
        }
        if (table, key_column) not in allowed:
            raise ValueError("Unsupported payload lookup")
        db = await self._connect()
        try:
            cursor = await db.execute(
                f"SELECT payload_json FROM {table} WHERE {key_column} = ?",
                (key_value,),
            )
            row = await cursor.fetchone()
            if row is None:
                raise NotFoundError(f"{table} record '{key_value}' was not found")
            return str(row["payload_json"])
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

    async def record_workflow_event(
        self,
        workflow_id: str,
        kind: str,
        payload: dict[str, Any],
    ) -> None:
        db = await self._connect()
        try:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                "SELECT state FROM workflows WHERE id = ?",
                (workflow_id,),
            )
            row = await cursor.fetchone()
            if row is None:
                raise NotFoundError(f"Workflow '{workflow_id}' was not found")
            await self._insert_event(
                db,
                workflow_id,
                kind,
                WorkflowState(row["state"]),
                payload,
            )
            await db.commit()
        except Exception:
            await db.rollback()
            raise
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
