from __future__ import annotations

import json
import shutil
from datetime import timedelta
from pathlib import Path

from printing_agent.domain import (
    Dimensions,
    ModelArtifact,
    PrinterCapabilitySummary,
    PrintJob,
    PrintJobStatus,
    PrintSettings,
    new_id,
    utc_now,
)
from printing_agent.errors import ConflictError, NotFoundError, ValidationError


class PrinterRegistry:
    def __init__(self) -> None:
        self._adapters: dict[str, object] = {}

    def register(self, adapter: object) -> None:
        name = adapter.name
        if name in self._adapters:
            raise ConflictError(f"Printer adapter '{name}' is already registered")
        self._adapters[name] = adapter

    def get(self, name: str) -> object:
        try:
            return self._adapters[name]
        except KeyError as exc:
            raise NotFoundError(f"Printer adapter '{name}' is not registered") from exc

    async def capabilities(self) -> list[PrinterCapabilitySummary]:
        return [await adapter.capabilities() for adapter in self._adapters.values()]  # type: ignore[attr-defined]


class SimulatedPrinterAdapter:
    name = "simulator"

    def __init__(
        self,
        spool_dir: Path,
        *,
        build_volume: Dimensions | None = None,
    ) -> None:
        self.spool_dir = spool_dir
        self.spool_dir.mkdir(parents=True, exist_ok=True)
        self._build_volume = build_volume or Dimensions(
            width_mm=220,
            depth_mm=220,
            height_mm=250,
        )

    async def capabilities(self) -> PrinterCapabilitySummary:
        return PrinterCapabilitySummary(
            name=self.name,
            build_volume=self._build_volume,
            accepted_formats={"stl"},
            supported_materials={"pla", "petg", "abs", "tpu"},
        )

    async def validate(self, artifact: ModelArtifact, settings: PrintSettings) -> None:
        capabilities = await self.capabilities()
        if artifact.model_path.suffix.lower().lstrip(".") not in capabilities.accepted_formats:
            raise ValidationError("Simulator accepts STL artifacts only")
        if not artifact.mesh.dimensions.fits(capabilities.build_volume):
            raise ValidationError("Artifact does not fit the simulator build volume")
        if (
            settings.material
            and settings.material.casefold()
            not in {material.casefold() for material in capabilities.supported_materials}
        ):
            raise ValidationError(f"Simulator does not support {settings.material}")

    async def submit(
        self,
        workflow_id: str,
        artifact: ModelArtifact,
        settings: PrintSettings,
        idempotency_key: str,
    ) -> PrintJob:
        await self.validate(artifact, settings)
        existing = self._find_by_idempotency(idempotency_key)
        if existing is not None:
            return existing

        external_id = new_id()
        job_dir = self.spool_dir / external_id
        job_dir.mkdir(parents=False, exist_ok=False)
        shutil.copy2(artifact.model_path, job_dir / "model.stl")
        now = utc_now()
        job = PrintJob(
            workflow_id=workflow_id,
            printer_name=self.name,
            external_id=external_id,
            idempotency_key=idempotency_key,
            status=PrintJobStatus.QUEUED,
            artifact_version=artifact.version,
            message="Model copied to the simulated printer spool",
            created_at=now,
            updated_at=now,
        )
        self._write_job(job, job_dir)
        return job

    async def status(self, external_id: str) -> PrintJob:
        job, job_dir = self._read_job(external_id)
        elapsed = utc_now() - job.created_at
        if job.status == PrintJobStatus.QUEUED and elapsed >= timedelta(seconds=2):
            job = job.model_copy(
                update={
                    "status": PrintJobStatus.PRINTING,
                    "message": "Simulated print is in progress",
                    "updated_at": utc_now(),
                }
            )
            self._write_job(job, job_dir)
        if job.status == PrintJobStatus.PRINTING and elapsed >= timedelta(seconds=8):
            job = job.model_copy(
                update={
                    "status": PrintJobStatus.COMPLETED,
                    "message": "Simulated print completed",
                    "updated_at": utc_now(),
                }
            )
            self._write_job(job, job_dir)
        return job

    async def cancel(self, external_id: str) -> PrintJob:
        job, job_dir = self._read_job(external_id)
        if job.status in {
            PrintJobStatus.COMPLETED,
            PrintJobStatus.FAILED,
            PrintJobStatus.CANCELLED,
        }:
            raise ConflictError(f"Cannot cancel a {job.status.value} print job")
        job = job.model_copy(
            update={
                "status": PrintJobStatus.CANCELLED,
                "message": "Simulated print cancelled",
                "updated_at": utc_now(),
            }
        )
        self._write_job(job, job_dir)
        return job

    def _find_by_idempotency(self, idempotency_key: str) -> PrintJob | None:
        for path in self.spool_dir.glob("*/job.json"):
            job = PrintJob.model_validate_json(path.read_text(encoding="utf-8"))
            if job.idempotency_key == idempotency_key:
                return job
        return None

    def _read_job(self, external_id: str) -> tuple[PrintJob, Path]:
        job_dir = self.spool_dir / external_id
        path = job_dir / "job.json"
        if not path.is_file():
            raise NotFoundError(f"Simulator job '{external_id}' was not found")
        return PrintJob.model_validate_json(path.read_text(encoding="utf-8")), job_dir

    @staticmethod
    def _write_job(job: PrintJob, job_dir: Path) -> None:
        temporary = job_dir / "job.json.tmp"
        temporary.write_text(
            json.dumps(job.model_dump(mode="json"), indent=2, sort_keys=True),
            encoding="utf-8",
        )
        temporary.replace(job_dir / "job.json")
