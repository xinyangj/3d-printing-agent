from __future__ import annotations

import asyncio
import contextlib

from printing_agent.application import PrintingApplication
from printing_agent.domain import WorkflowState, WorkKind
from printing_agent.errors import PrintingAgentError
from printing_agent.repositories import WorkflowRepository


class DurableWorker:
    def __init__(
        self,
        repository: WorkflowRepository,
        application: PrintingApplication,
    ) -> None:
        self.repository = repository
        self.application = application
        self._stop = asyncio.Event()

    async def run(self) -> None:
        while not self._stop.is_set():
            item = await self.repository.lease_next()
            if item is None:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), timeout=0.5)
                continue
            heartbeat = asyncio.create_task(self._renew_lease(item.id))
            try:
                if item.kind == WorkKind.PREPARE:
                    await self.application.prepare(item.workflow_id)
                elif item.kind == WorkKind.SUBMIT:
                    await self.application.submit_print(item.workflow_id)
                elif item.kind == WorkKind.REFRESH_PRINT:
                    await self.application.refresh_print(item.workflow_id)
                await self.repository.complete_work(item.id)
            except Exception as exc:
                await self.repository.fail_work(item.id, str(exc)[-2_000:])
                await self._record_failure(item.workflow_id, item.kind, exc)
            finally:
                heartbeat.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await heartbeat

    def stop(self) -> None:
        self._stop.set()

    async def _renew_lease(self, work_item_id: str) -> None:
        while True:
            await asyncio.sleep(20)
            with contextlib.suppress(Exception):
                await self.repository.renew_work_lease(work_item_id, lease_seconds=60)

    async def _record_failure(
        self,
        workflow_id: str,
        kind: WorkKind,
        error: Exception,
    ) -> None:
        try:
            workflow = await self.repository.get_workflow(workflow_id)
            if workflow.state in {
                WorkflowState.COMPLETED,
                WorkflowState.CANCELLED,
                WorkflowState.PREPARATION_FAILED,
                WorkflowState.PRINT_FAILED,
            }:
                return
            target = (
                WorkflowState.PRINT_FAILED
                if kind in {WorkKind.SUBMIT, WorkKind.REFRESH_PRINT}
                else WorkflowState.PREPARATION_FAILED
            )
            await self.repository.transition(
                workflow_id,
                target,
                event_kind="workflow.failed",
                payload={"message": str(error)[-2_000:]},
                failure_code=(
                    error.code if isinstance(error, PrintingAgentError) else "unexpected_error"
                ),
                failure_message=str(error)[-2_000:],
            )
        except Exception:
            return
