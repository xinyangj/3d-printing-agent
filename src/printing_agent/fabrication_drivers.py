from __future__ import annotations

import asyncio
import copy
import io
import json
import math
import os
import re
import shutil
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from xml.etree import ElementTree

import numpy as np
import trimesh
from PIL import Image, ImageDraw

from printing_agent.artifact_store import sha256_file
from printing_agent.domain import ModelArtifact, canonical_digest
from printing_agent.errors import (
    ConflictError,
    ExternalServiceError,
    NotFoundError,
    PolicyViolationError,
    ValidationError,
)
from printing_agent.fabrication import (
    MaterialAssignment,
    PartMaterialAssignment,
    PrinterProfileRevision,
    SlicedArtifact,
    SliceJob,
    WorkflowPrinterSnapshot,
)
from printing_agent.fabrication_profiles import (
    resolve_profile_dependency_digests,
    resolve_slicer_profile_file_digests,
    resolve_slicer_profile_path,
    resolve_slicer_profile_payload,
)


@dataclass(frozen=True)
class SliceRequest:
    job: SliceJob
    artifact: ModelArtifact
    printer: WorkflowPrinterSnapshot
    material_assignment: MaterialAssignment
    workspace: Path


class SlicerDriver(Protocol):
    @property
    def id(self) -> str: ...

    async def validate_profile(self, profile: PrinterProfileRevision) -> None: ...

    async def slice(self, request: SliceRequest) -> SlicedArtifact: ...

    async def cancel(self, slice_job_id: str) -> None: ...


class SlicerRegistry:
    def __init__(self) -> None:
        self._drivers: dict[str, SlicerDriver] = {}

    def register(self, driver: SlicerDriver) -> None:
        if driver.id in self._drivers:
            raise ConflictError(f"Slicer driver '{driver.id}' is already registered")
        self._drivers[driver.id] = driver

    def get(self, driver_id: str) -> SlicerDriver:
        try:
            return self._drivers[driver_id]
        except KeyError as exc:
            raise NotFoundError(
                f"Slicer driver '{driver_id}' is not registered"
            ) from exc
class BambuStudioCliDriver:
    id = "bambu_studio_cli"
    _cli_error_messages = {
        -2: "invalid command-line parameters",
        -102: "G-code path entered an unprintable area",
    }

    def __init__(
        self,
        executable_path: str | None = None,
        *,
        timeout_seconds: int = 900,
        maximum_output_bytes: int = 2 * 1024 * 1024 * 1024,
    ) -> None:
        self.executable_path = executable_path
        self.timeout_seconds = timeout_seconds
        self.maximum_output_bytes = maximum_output_bytes
        self._processes: dict[str, asyncio.subprocess.Process] = {}
        self._cancelled: set[str] = set()

    async def validate_profile(self, profile: PrinterProfileRevision) -> None:
        executable = self._resolve_executable(profile)
        await asyncio.to_thread(self._resolve_profile_path, profile, "machine")
        await asyncio.to_thread(self._resolve_profile_path, profile, "process")
        if not await asyncio.to_thread(Path(executable).is_file):
            raise ExternalServiceError("Bambu Studio executable is unavailable")

    async def slice(self, request: SliceRequest) -> SlicedArtifact:
        profile = PrinterProfileRevision(
            profile_id=request.printer.profile_id,
            revision=request.printer.profile_revision,
            origin="custom",
            spec=request.printer.profile,
            digest=request.printer.profile_digest,
        )
        await self.validate_profile(profile)
        executable = await asyncio.to_thread(self._resolve_executable, profile)
        machine_path = await asyncio.to_thread(
            self._resolve_profile_path,
            profile,
            "machine",
        )
        process_path = await asyncio.to_thread(
            self._resolve_profile_path,
            profile,
            "process",
        )
        expected_profile_digests = request.printer.slicer_profile_file_digests
        current_profile_digests = await asyncio.to_thread(
            resolve_slicer_profile_file_digests,
            profile,
        )
        if not expected_profile_digests:
            raise ConflictError(
                "Workflow snapshot does not contain pinned machine/process profiles"
            )
        if current_profile_digests != expected_profile_digests:
            raise ConflictError(
                "Bambu Studio profile dependency graph changed after workflow creation"
            )
        filament_paths = await asyncio.to_thread(
            self._resolve_filament_paths,
            request,
            profile,
        )
        input_path = request.artifact.three_mf_path
        if input_path is None or not input_path.is_file():
            raise ValidationError("Verified artifact has no project 3MF to slice")
        workspace = request.workspace.resolve()
        workspace.mkdir(parents=True, exist_ok=False)
        try:
            resolved_machine_path = workspace / "resolved-machine-profile.json"
            await asyncio.to_thread(
                self._write_resolved_profile,
                profile,
                "machine",
                resolved_machine_path,
            )
            resolved_process_path = workspace / "resolved-process-profile.json"
            await asyncio.to_thread(
                self._write_resolved_profile,
                profile,
                "process",
                resolved_process_path,
            )
            resolved_filament_paths = await asyncio.to_thread(
                self._write_resolved_filament_profiles,
                request,
                profile,
                workspace,
            )
            resolved_input = workspace / "resolved-input.3mf"
            await asyncio.to_thread(
                self._prepare_bambu_project,
                input_path,
                resolved_input,
                request,
            )
            output_path = workspace / "job.gcode.3mf"
            log_path = workspace / "bambu-studio.log"
            override_path = workspace / "resolved-job-settings.json"
            override_settings = self._resolved_job_settings(request)
            override_path.write_text(
                json.dumps(override_settings, sort_keys=True, indent=2),
                encoding="utf-8",
            )
            effective_process_path = workspace / "effective-process-profile.json"
            await asyncio.to_thread(
                self._write_effective_process_profile,
                resolved_process_path,
                effective_process_path,
                override_settings,
            )
            args = self.build_arguments(
                input_path=resolved_input,
                output_path=output_path,
                machine_path=resolved_machine_path,
                process_path=effective_process_path,
                filament_paths=resolved_filament_paths,
                filament_map=[
                    str(value) for value in override_settings["filament_map"]
                ],
            )
        except Exception:
            self._cancelled.discard(request.job.id)
            shutil.rmtree(workspace, ignore_errors=True)
            raise
        if request.job.id in self._cancelled:
            self._cancelled.discard(request.job.id)
            shutil.rmtree(workspace, ignore_errors=True)
            raise ConflictError("Slice was cancelled before Bambu Studio started")
        try:
            process = await asyncio.create_subprocess_exec(
                executable,
                *args,
                cwd=workspace,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except FileNotFoundError as exc:
            self._cancelled.discard(request.job.id)
            shutil.rmtree(workspace, ignore_errors=True)
            raise ExternalServiceError("Bambu Studio executable is unavailable") from exc
        self._processes[request.job.id] = process
        if request.job.id in self._cancelled:
            process.kill()
            await process.wait()
            self._processes.pop(request.job.id, None)
            self._cancelled.discard(request.job.id)
            shutil.rmtree(workspace, ignore_errors=True)
            raise ConflictError("Slice was cancelled while Bambu Studio started")
        try:
            try:
                output, _ = await asyncio.wait_for(
                    process.communicate(),
                    timeout=self.timeout_seconds,
                )
            except TimeoutError as exc:
                process.kill()
                await process.wait()
                raise ExternalServiceError("Bambu Studio slicing timed out") from exc
            log_path.write_bytes(output[-2_000_000:])
            if process.returncode != 0:
                returncode = self._normalized_returncode(process.returncode)
                detail = output.decode(errors="replace")[-4_000:].strip()
                if not detail:
                    reason = self._cli_error_messages.get(returncode)
                    detail = f"process exited with code {returncode}"
                    if reason:
                        detail += f" ({reason})"
                raise ValidationError(
                    "Bambu Studio slicing failed: " + detail
                )
            self._raise_if_cancelled(request.job.id)
            output_size = await asyncio.to_thread(
                lambda: output_path.stat().st_size
            )
            if output_size > self.maximum_output_bytes:
                raise PolicyViolationError("Sliced job exceeds the configured size limit")
            thumbnail_model_path: Path | None = None
            thumbnail_model_transform: tuple[float, ...] | None = None
            if (
                request.artifact.project is not None
                and len(request.artifact.project.parts) == 1
                and len(request.artifact.project.instances) == 1
                and request.artifact.model_path.is_file()
                and request.artifact.mesh.triangle_count <= 100_000
                and request.artifact.model_path.stat().st_size <= 32 * 1024 * 1024
            ):
                thumbnail_model_path = request.artifact.model_path
                thumbnail_model_transform = tuple(
                    request.artifact.project.instances[0].transform
                )
            await asyncio.to_thread(
                self._ensure_bambu_connect_metadata,
                output_path,
                self.maximum_output_bytes * 4,
                thumbnail_model_path,
                thumbnail_model_transform,
            )
            output_size = await asyncio.to_thread(
                lambda: output_path.stat().st_size
            )
            if output_size > self.maximum_output_bytes:
                raise PolicyViolationError("Sliced job exceeds the configured size limit")
            self._raise_if_cancelled(request.job.id)
            await asyncio.to_thread(self._validate_gcode_3mf, output_path)
            self._raise_if_cancelled(request.job.id)
            await asyncio.to_thread(
                self._validate_sliced_assignment,
                output_path,
                request,
            )
            self._raise_if_cancelled(request.job.id)
            filament_usage_g = await asyncio.to_thread(
                self._filament_usage_by_spool,
                output_path,
                request,
            )
            thumbnail_path = await asyncio.to_thread(
                self._extract_plate_thumbnail,
                output_path,
                workspace,
            )
            provenance = self._provenance_manifest(
                request,
                override_settings,
            )
            version = await self._version(executable)
            self._raise_if_cancelled(request.job.id)
            output_digest = await asyncio.to_thread(sha256_file, output_path)
            self._raise_if_cancelled(request.job.id)
            manifest = {
                "slice_job_id": request.job.id,
                "model_manifest_digest": request.artifact.manifest_digest,
                "printer_snapshot_digest": request.printer.digest,
                "cloud_snapshot_digest": request.job.cloud_snapshot_digest,
                "material_assignment_digest": request.material_assignment.digest,
                "slicer_driver_id": self.id,
                "slicer_version": version,
                "machine_profile": str(machine_path),
                "process_profile": str(process_path),
                "resolved_machine_profile": str(resolved_machine_path),
                "resolved_process_profile": str(resolved_process_path),
                "effective_process_profile": str(effective_process_path),
                "filament_profiles": [str(path) for path in filament_paths],
                "resolved_filament_profiles": [
                    str(path) for path in resolved_filament_paths
                ],
                "resolved_job_settings": override_settings,
                "resolved_job_settings_digest": sha256_file(override_path),
                "provenance": provenance,
                "filament_usage_g": filament_usage_g,
                "thumbnail_digest": (
                    sha256_file(thumbnail_path)
                    if thumbnail_path is not None
                    else None
                ),
                "output_digest": output_digest,
            }
            manifest_digest = canonical_digest(manifest)
            (workspace / "slice-manifest.json").write_text(
                json.dumps(
                    {**manifest, "manifest_digest": manifest_digest},
                    sort_keys=True,
                    indent=2,
                ),
                encoding="utf-8",
            )
            return SlicedArtifact(
                slice_job_id=request.job.id,
                workflow_id=request.job.workflow_id,
                path=str(output_path),
                digest=output_digest,
                size_bytes=output_size,
                format="gcode.3mf",
                model_manifest_digest=request.artifact.manifest_digest,
                printer_snapshot_digest=request.printer.digest or "0" * 64,
                cloud_snapshot_digest=request.job.cloud_snapshot_digest,
                material_assignment_digest=request.material_assignment.digest
                or "0" * 64,
                slicer_driver_id=self.id,
                slicer_version=version,
                machine_profile_id=profile.spec.slicer.machine_profile_id,
                process_profile_id=profile.spec.slicer.process_profile_id,
                profile_file_digests={
                    "machine": sha256_file(machine_path),
                    "process": sha256_file(process_path),
                    "resolved_machine": sha256_file(resolved_machine_path),
                    "resolved_process": sha256_file(resolved_process_path),
                    "effective_process": sha256_file(effective_process_path),
                    "job_overrides": sha256_file(override_path),
                    **{
                        f"filament_{index}": sha256_file(path)
                        for index, path in enumerate(resolved_filament_paths)
                    },
                },
                plate_count=1,
                warnings=[],
                filament_usage_g=filament_usage_g,
                thumbnail_path=(
                    str(thumbnail_path) if thumbnail_path is not None else None
                ),
                manifest_digest=manifest_digest,
            )
        finally:
            self._processes.pop(request.job.id, None)
            self._cancelled.discard(request.job.id)
            if not (workspace / "slice-manifest.json").is_file():
                shutil.rmtree(workspace, ignore_errors=True)

    @staticmethod
    def _normalized_returncode(returncode: int) -> int:
        if os.name == "nt" and returncode >= 2**31:
                return returncode - 2**32
        return returncode

    async def cancel(self, slice_job_id: str) -> None:
        self._cancelled.add(slice_job_id)
        process = self._processes.get(slice_job_id)
        if process is None or process.returncode is not None:
            return
        try:
            process.kill()
        except ProcessLookupError:
            return
        await process.wait()

    def _raise_if_cancelled(self, slice_job_id: str) -> None:
        if slice_job_id in self._cancelled:
            raise ConflictError("Slice was cancelled during Bambu Studio processing")

    @staticmethod
    def build_arguments(
        *,
        input_path: Path,
        output_path: Path,
        machine_path: Path,
        process_path: Path,
        filament_paths: list[Path],
        filament_map: list[str],
    ) -> list[str]:
        return [
            "--slice=1",
            "--arrange=1",
            "--filament-map-mode=Manual",
            f"--filament-map={','.join(filament_map)}",
            "--load-settings",
            f"{machine_path};{process_path}",
            "--load-filaments",
            ";".join(str(path) for path in filament_paths),
            "--export-3mf",
            str(output_path),
            "--min-save",
            str(input_path),
        ]

    @staticmethod
    def _resolved_job_settings(
        request: SliceRequest,
    ) -> dict[str, str | list[str]]:
        overrides = request.printer.overrides
        filament_assignments = BambuStudioCliDriver._filament_assignments(request)
        toolhead_indices = {
            item.id: index
            for index, item in enumerate(request.printer.profile.toolheads, start=1)
        }
        used_toolhead_ids = {
            assignment.toolhead_id for assignment in filament_assignments
        }
        if not used_toolhead_ids.issubset(toolhead_indices):
            raise ValidationError("Material assignment references an unknown toolhead")
        if overrides.nozzle_diameter_mm is not None:
            mismatched = [
                item.id
                for item in request.printer.profile.toolheads
                if item.id in used_toolhead_ids
                and not math.isclose(
                    item.nozzle_diameter_mm,
                    overrides.nozzle_diameter_mm,
                    rel_tol=0,
                    abs_tol=1e-9,
                )
            ]
            if mismatched:
                raise ValidationError(
                    "Nozzle override requires a versioned machine profile with matching "
                    "toolhead diameters: "
                    + ", ".join(sorted(mismatched))
                )
        settings: dict[str, str | list[str]] = {
            "name": "project_settings",
            "from": "project",
            "filament_map_mode": "Manual",
            "filament_map": [
                str(toolhead_indices[item.toolhead_id])
                for item in filament_assignments
            ],
        }
        if len(filament_assignments) <= 1:
            settings["enable_prime_tower"] = "0"
        else:
            build_volume = request.printer.profile.build_volume
            settings["enable_prime_tower"] = "1"
            settings["wipe_tower_x"] = [
                f"{max(15.0, build_volume.width_mm - 70.0):g}"
            ]
            settings["wipe_tower_y"] = ["20"]
        if overrides.layer_height_mm is not None:
            settings["layer_height"] = f"{overrides.layer_height_mm:g}"
        if overrides.infill_percent is not None:
            settings["sparse_infill_density"] = (
                f"{overrides.infill_percent}%"
            )
        if overrides.supports is not None:
            settings["enable_support"] = "1" if overrides.supports else "0"
        if overrides.brim is not None:
            settings["brim_type"] = (
                "outer_only" if overrides.brim else "no_brim"
            )
        if overrides.raft is not None:
            settings["raft_layers"] = "3" if overrides.raft else "0"
        plate_id = overrides.plate_id or request.printer.profile.plates[0].id
        plate = next(
            (item for item in request.printer.profile.plates if item.id == plate_id),
            None,
        )
        if plate is None:
            raise ValidationError(f"Unknown plate '{plate_id}' in workflow snapshot")
        settings["curr_bed_type"] = plate.slicer_value or plate.name
        settings["nozzle_diameter"] = [
            f"{item.nozzle_diameter_mm:g}"
            for item in request.printer.profile.toolheads
        ]
        settings["filament_settings_id"] = [
            item.slicer_filament_profile_id
            for item in filament_assignments
        ]
        requested_colors = {
            item.part_id: item.requested_color
            for item in request.material_assignment.requests
        }
        selected_colors: dict[str, set[str]] = {}
        for assignment in request.material_assignment.assignments:
            candidate = next(
                (
                    item
                    for item in request.material_assignment.candidate_options.get(
                        assignment.part_id,
                        [],
                    )
                    if item.spool_id == assignment.spool_id
                ),
                None,
            )
            if candidate is not None:
                selected_colors.setdefault(assignment.spool_id, set()).add(
                    candidate.color.upper()
                )
        inconsistent = [
            spool_id
            for spool_id, colors in selected_colors.items()
            if len(colors) != 1
        ]
        if inconsistent:
            raise ValidationError(
                "Selected spool has inconsistent physical colors: "
                + ", ".join(sorted(inconsistent))
            )
        settings["filament_colour"] = [
            (
                next(iter(selected_colors[item.spool_id]))
                if item.spool_id in selected_colors
                else requested_colors[item.part_id]
            )
            for item in filament_assignments
        ]
        return settings

    @staticmethod
    def _write_effective_process_profile(
        source: Path,
        destination: Path,
        settings: dict[str, str | list[str]],
    ) -> None:
        try:
            payload = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValidationError("Bambu Studio process profile is invalid") from exc
        if not isinstance(payload, dict) or payload.get("type") != "process":
            raise ValidationError("Bambu Studio process profile has an invalid type")
        if not isinstance(payload.get("name"), str) or not isinstance(
            payload.get("from"),
            str,
        ):
            raise ValidationError(
                "Bambu Studio process profile lacks required metadata"
            )
        payload.update(
            {
                key: value
                for key, value in settings.items()
                if key not in {"name", "from"}
            }
        )
        BambuStudioCliDriver._validate_full_profile_payload(
            payload,
            "process",
        )
        destination.write_text(
            json.dumps(payload, sort_keys=True, indent=2),
            encoding="utf-8",
        )

    @staticmethod
    def _write_resolved_profile(
        profile: PrinterProfileRevision,
        category: str,
        destination: Path,
    ) -> None:
        root_value = profile.spec.slicer.resource_root
        if root_value is None:
            raise ExternalServiceError(
                f"Bambu Studio {category} profile resource root is not configured"
            )
        profile_id = (
            profile.spec.slicer.machine_profile_id
            if category == "machine"
            else profile.spec.slicer.process_profile_id
        )
        payload = resolve_slicer_profile_payload(
            root_value,
            profile_id,
            category,
        )
        BambuStudioCliDriver._validate_full_profile_payload(
            payload,
            category,
        )
        destination.write_text(
            json.dumps(payload, sort_keys=True, indent=2),
            encoding="utf-8",
        )

    @staticmethod
    def _write_resolved_filament_profiles(
        request: SliceRequest,
        profile: PrinterProfileRevision,
        workspace: Path,
    ) -> list[Path]:
        root_value = profile.spec.slicer.resource_root
        if root_value is None:
            raise ExternalServiceError(
                "Bambu Studio filament profile resource root is not configured"
            )
        output: list[Path] = []
        for index, assignment in enumerate(
            BambuStudioCliDriver._filament_assignments(request),
            start=1,
        ):
            payload = resolve_slicer_profile_payload(
                root_value,
                assignment.slicer_filament_profile_id,
                "filament",
            )
            BambuStudioCliDriver._validate_full_profile_payload(
                payload,
                "filament",
            )
            destination = workspace / f"resolved-filament-{index}.json"
            destination.write_text(
                json.dumps(payload, sort_keys=True, indent=2),
                encoding="utf-8",
            )
            output.append(destination)
        return output

    @staticmethod
    def _validate_full_profile_payload(
        payload: dict[str, object],
        category: str,
    ) -> None:
        if payload.get("type") != category:
            raise ValidationError(
                f"Resolved Bambu Studio {category} profile has an invalid type"
            )
        if payload.get("from") not in {"system", "User", "user"}:
            raise ValidationError(
                f"Resolved Bambu Studio {category} profile has an invalid origin"
            )
        if not isinstance(payload.get("name"), str):
            raise ValidationError(
                f"Resolved Bambu Studio {category} profile has no name"
            )
        for key, value in payload.items():
            if not isinstance(value, (str, list)):
                raise ValidationError(
                    f"Resolved Bambu Studio {category} setting '{key}' has "
                    "an unsupported JSON type"
                )
            if isinstance(value, list) and any(
                not isinstance(item, str) for item in value
            ):
                raise ValidationError(
                    f"Resolved Bambu Studio {category} setting '{key}' must "
                    "contain only strings"
                )

    @staticmethod
    def _provenance_manifest(
        request: SliceRequest,
        native_settings: dict[str, str | list[str]],
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema_version": 1,
            "model_manifest_digest": request.artifact.manifest_digest,
            "printer_snapshot_digest": request.printer.digest,
            "cloud_snapshot_digest": request.job.cloud_snapshot_digest,
            "material_assignment_digest": request.material_assignment.digest,
            "job_overrides": request.printer.overrides.model_dump(mode="json"),
            "native_settings": native_settings,
            "part_material_map": {
                assignment.part_id: {
                    "spool_id": assignment.spool_id,
                    "slot_id": assignment.slot_id,
                    "toolhead_id": assignment.toolhead_id,
                    "material_id": assignment.material_id,
                    "material_revision": assignment.material_revision,
                    "material_digest": assignment.material_digest,
                    "filament_profile_id": (
                        assignment.slicer_filament_profile_id
                    ),
                }
                for assignment in request.material_assignment.assignments
            },
        }
        return {
            **payload,
            "manifest_digest": canonical_digest(payload),
        }

    @staticmethod
    def _filament_assignments(
        request: SliceRequest,
    ) -> list[PartMaterialAssignment]:
        by_spool: dict[str, PartMaterialAssignment] = {}
        output: list[PartMaterialAssignment] = []
        immutable_fields = (
            "slot_id",
            "toolhead_id",
            "material_id",
            "material_revision",
            "material_digest",
            "slicer_filament_profile_id",
            "slicer_filament_profile_digest",
            "slicer_filament_dependency_digests",
        )
        for assignment in request.material_assignment.assignments:
            previous = by_spool.get(assignment.spool_id)
            if previous is None:
                by_spool[assignment.spool_id] = assignment
                output.append(assignment)
                continue
            if any(
                getattr(previous, field) != getattr(assignment, field)
                for field in immutable_fields
            ):
                raise ValidationError(
                    f"Spool '{assignment.spool_id}' has conflicting material mappings"
                )
        return output

    def _resolve_executable(self, profile: PrinterProfileRevision) -> str:
        del profile
        configured = (
            self.executable_path
            or shutil.which("bambu-studio.exe")
            or shutil.which("BambuStudio.exe")
        )
        if not configured:
            raise ExternalServiceError(
                "Bambu Studio is not installed or configured"
            )
        return str(Path(configured).resolve())

    @staticmethod
    def _resolve_profile_path(
        profile: PrinterProfileRevision,
        kind: str,
    ) -> Path:
        value = (
            profile.spec.slicer.machine_profile_id
            if kind == "machine"
            else profile.spec.slicer.process_profile_id
        )
        direct = Path(value)
        if direct.is_file():
            return direct.resolve()
        root_value = profile.spec.slicer.resource_root
        if root_value is None:
            raise ExternalServiceError(
                f"Bambu Studio {kind} profile resource root is not configured"
            )
        return resolve_slicer_profile_path(root_value, value, kind)

    @staticmethod
    def _resolve_filament_paths(
        request: SliceRequest,
        profile: PrinterProfileRevision,
    ) -> list[Path]:
        root_value = profile.spec.slicer.resource_root
        if root_value is None:
            raise ExternalServiceError(
                "Bambu Studio filament profile resource root is not configured"
            )
        resolved: list[tuple[str, dict[str, str]]] = []
        for assignment in BambuStudioCliDriver._filament_assignments(request):
            material_profile = assignment.slicer_filament_profile_id
            digests = assignment.slicer_filament_dependency_digests
            if not digests:
                raise ConflictError(
                    f"Filament profile '{material_profile}' dependency graph is not pinned"
                )
            resolved.append((material_profile, digests))
        output: list[Path] = []
        for profile_id, expected_digests in resolved:
            output.append(
                resolve_slicer_profile_path(
                    root_value,
                    profile_id,
                    "filament",
                )
            )
            current_digests = resolve_profile_dependency_digests(
                root_value,
                profile_id,
                "filament",
            )
            if current_digests != expected_digests:
                raise ConflictError(
                    f"Filament profile '{profile_id}' dependency graph changed "
                    "after material configuration"
                )
        return output

    @staticmethod
    def _prepare_bambu_project(
        source: Path,
        destination: Path,
        request: SliceRequest,
    ) -> None:
        filament_assignments = BambuStudioCliDriver._filament_assignments(request)
        filament_index_by_spool = {
            assignment.spool_id: index
            for index, assignment in enumerate(filament_assignments, start=1)
        }
        assignment_index = {
            assignment.part_id: filament_index_by_spool[assignment.spool_id]
            for assignment in request.material_assignment.assignments
        }
        with zipfile.ZipFile(source) as archive:
            entries = {
                name: archive.read(name)
                for name in archive.namelist()
                if name
                not in {
                    "Metadata/model_settings.config",
                    "Metadata/project_settings.config",
                }
            }
        model_name = next(
            (
                name
                for name in entries
                if name.casefold().endswith(".model")
            ),
            None,
        )
        if model_name is None:
            raise ValidationError("Project 3MF contains no model XML")
        model = ElementTree.fromstring(entries[model_name])
        if (
            request.artifact.project is not None
            and len(request.artifact.project.parts) > 1
        ):
            BambuStudioCliDriver._lay_flat_thin_objects(model)
        object_rows = BambuStudioCliDriver._expand_repeated_build_items(
            model,
            assignment_index,
        )
        ElementTree.register_namespace(
            "",
            "http://schemas.microsoft.com/3dmanufacturing/core/2015/02",
        )
        ElementTree.register_namespace(
            "m",
            "http://schemas.microsoft.com/3dmanufacturing/material/2015/02",
        )
        ElementTree.register_namespace(
            "p",
            "http://schemas.microsoft.com/3dmanufacturing/production/2015/06",
        )
        entries[model_name] = ElementTree.tostring(
            model,
            encoding="utf-8",
            xml_declaration=True,
        )
        missing_parts = sorted(set(assignment_index) - {row[1] for row in object_rows})
        if missing_parts:
            raise ValidationError(
                "3MF object IDs could not be mapped to semantic parts: "
                + ", ".join(missing_parts)
            )
        config = ElementTree.Element("config")
        for object_id, part_id, extruder in object_rows:
            object_element = ElementTree.SubElement(
                config,
                "object",
                {"id": object_id},
            )
            ElementTree.SubElement(
                object_element,
                "metadata",
                {"key": "name", "value": part_id},
            )
            ElementTree.SubElement(
                object_element,
                "metadata",
                {"key": "extruder", "value": str(extruder)},
            )
        project_settings = BambuStudioCliDriver._resolved_job_settings(request)
        entries["Metadata/model_settings.config"] = ElementTree.tostring(
            config,
            encoding="utf-8",
            xml_declaration=True,
        )
        entries["Metadata/project_settings.config"] = json.dumps(
            project_settings,
            sort_keys=True,
            indent=2,
        ).encode()
        with zipfile.ZipFile(
            destination,
            "w",
            compression=zipfile.ZIP_DEFLATED,
        ) as archive:
            for name, data in entries.items():
                archive.writestr(name, data)

    @staticmethod
    def _lay_flat_thin_objects(model: ElementTree.Element) -> None:
        for object_element in model.iter():
            if not object_element.tag.endswith("object"):
                continue
            vertices = [
                element
                for element in object_element.iter()
                if element.tag.endswith("vertex")
            ]
            if not vertices:
                continue
            try:
                coordinates = [
                    [float(vertex.attrib[axis]) for axis in ("x", "y", "z")]
                    for vertex in vertices
                ]
            except (KeyError, ValueError) as exc:
                raise ValidationError(
                    "Project 3MF contains an invalid mesh vertex"
                ) from exc
            minimums = [
                min(values[axis] for values in coordinates)
                for axis in range(3)
            ]
            maximums = [
                max(values[axis] for values in coordinates)
                for axis in range(3)
            ]
            extents = [
                maximums[axis] - minimums[axis]
                for axis in range(3)
            ]
            thinnest_axis = min(range(3), key=extents.__getitem__)
            thickness = extents[thinnest_axis]
            if (
                thinnest_axis == 2
                or thickness <= 0
                or thickness > 1.5
                or extents[2] < thickness * 4
            ):
                continue
            centers = [
                (minimums[axis] + maximums[axis]) / 2
                for axis in range(3)
            ]
            for vertex, values in zip(vertices, coordinates, strict=True):
                relative = [
                    values[axis] - centers[axis]
                    for axis in range(3)
                ]
                if thinnest_axis == 0:
                    rotated = [relative[2], relative[1], -relative[0]]
                else:
                    rotated = [relative[0], relative[2], -relative[1]]
                transformed = [
                    rotated[0] + centers[0],
                    rotated[1] + centers[1],
                    rotated[2] + minimums[2] + thickness / 2,
                ]
                for axis, value in zip(
                    ("x", "y", "z"),
                    transformed,
                    strict=True,
                ):
                    vertex.set(axis, f"{value:.9g}")

    @staticmethod
    def _expand_repeated_build_items(
        model: ElementTree.Element,
        assignment_index: dict[str, int],
    ) -> list[tuple[str, str, int]]:
        resources = next(
            (element for element in model if element.tag.endswith("resources")),
            None,
        )
        build = next(
            (element for element in model if element.tag.endswith("build")),
            None,
        )
        if resources is None or build is None:
            raise ValidationError("Project 3MF lacks resources or build items")
        objects_by_id = {
            element.attrib["id"]: element
            for element in resources
            if element.tag.endswith("object") and element.attrib.get("id")
        }
        resource_ids = [
            int(element.attrib["id"])
            for element in resources
            if element.attrib.get("id", "").isdigit()
        ]
        next_resource_id = max(resource_ids, default=0) + 1
        build_references: dict[str, int] = {}
        for item in build:
            if not item.tag.endswith("item"):
                continue
            object_id = item.attrib.get("objectid")
            source_object = objects_by_id.get(object_id or "")
            if object_id is None or source_object is None:
                continue
            reference_count = build_references.get(object_id, 0)
            build_references[object_id] = reference_count + 1
            if reference_count == 0:
                continue
            clone = copy.deepcopy(source_object)
            while str(next_resource_id) in objects_by_id:
                next_resource_id += 1
            clone_id = str(next_resource_id)
            next_resource_id += 1
            clone.set("id", clone_id)
            for attribute in tuple(clone.attrib):
                if attribute.rsplit("}", 1)[-1].casefold() == "uuid":
                    clone.set(attribute, str(uuid.uuid4()))
            resources.append(clone)
            objects_by_id[clone_id] = clone
            item.set("objectid", clone_id)

        object_rows: list[tuple[str, str, int]] = []
        for element in objects_by_id.values():
            object_id = element.attrib.get("id")
            part_id = (
                element.attrib.get("partnumber")
                or element.attrib.get("name")
                or ""
            )
            if object_id and part_id in assignment_index:
                object_rows.append(
                    (object_id, part_id, assignment_index[part_id])
                )
        return object_rows

    @staticmethod
    def _validate_gcode_3mf(path: Path) -> None:
        if not path.is_file() or path.suffixes[-2:] != [".gcode", ".3mf"]:
            raise ValidationError("Bambu Studio did not create a .gcode.3mf file")
        try:
            with zipfile.ZipFile(path) as archive:
                names = archive.namelist()
        except zipfile.BadZipFile as exc:
            raise ValidationError("Sliced output is not a valid 3MF archive") from exc
        if not any(name.casefold().endswith(".gcode") for name in names):
            raise ValidationError("Sliced 3MF contains no G-code payload")

    @staticmethod
    def _ensure_bambu_connect_metadata(
        path: Path,
        maximum_uncompressed_bytes: int = 8 * 1024 * 1024 * 1024,
        model_path: Path | None = None,
        model_transform: tuple[float, ...] | None = None,
    ) -> None:
        thumbnail_name = "Metadata/plate_1.png"
        temporary = path.with_name(f"{path.name}.connect.tmp")
        try:
            with zipfile.ZipFile(path) as archive:
                names = set(archive.namelist())
                total_uncompressed = sum(
                    item.file_size for item in archive.infolist()
                )
                if total_uncompressed > maximum_uncompressed_bytes:
                    raise PolicyViolationError(
                        "Sliced archive exceeds the expanded size limit"
                    )
                for required, maximum in (
                    ("Metadata/model_settings.config", 2 * 1024 * 1024),
                    ("Metadata/plate_1.json", 10 * 1024 * 1024),
                ):
                    if required in names and archive.getinfo(required).file_size > maximum:
                        raise PolicyViolationError(
                            f"Sliced {required} exceeds the metadata size limit"
                        )
                model_settings = ElementTree.fromstring(
                    archive.read("Metadata/model_settings.config")
                )
                plate = model_settings.find("plate")
                if plate is None:
                    raise ValidationError(
                        "Sliced output lacks plate metadata for Bambu Connect"
                    )
                metadata = {
                    item.get("key"): item
                    for item in plate.findall("metadata")
                }
                current_thumbnail = metadata.get("thumbnail_file")
                if (
                    current_thumbnail is not None
                    and current_thumbnail.get("value") in names
                ):
                    return
                plate_payload = (
                    json.loads(archive.read("Metadata/plate_1.json"))
                    if "Metadata/plate_1.json" in names
                    else {}
                )
                thumbnail = BambuStudioCliDriver._render_plate_thumbnail(
                    plate_payload,
                    model_path,
                    model_transform,
                )
                if current_thumbnail is None:
                    ElementTree.SubElement(
                        plate,
                        "metadata",
                        {"key": "thumbnail_file", "value": thumbnail_name},
                    )
                else:
                    current_thumbnail.set("value", thumbnail_name)
                model_payload = ElementTree.tostring(
                    model_settings,
                    encoding="utf-8",
                    xml_declaration=True,
                )
                with zipfile.ZipFile(
                    temporary,
                    "w",
                    compression=zipfile.ZIP_DEFLATED,
                    allowZip64=True,
                ) as output:
                    for info in archive.infolist():
                        if info.filename in {
                            "Metadata/model_settings.config",
                            thumbnail_name,
                        }:
                            continue
                        with (
                            archive.open(info, "r") as source,
                            output.open(info, "w", force_zip64=True) as target,
                        ):
                            shutil.copyfileobj(
                                source,
                                target,
                                length=1024 * 1024,
                            )
                    output.writestr(
                        "Metadata/model_settings.config",
                        model_payload,
                    )
                    output.writestr(thumbnail_name, thumbnail)
            temporary.replace(path)
        except (
            json.JSONDecodeError,
            KeyError,
            OSError,
            zipfile.BadZipFile,
            ElementTree.ParseError,
        ) as exc:
            raise ValidationError(
                "Sliced output cannot be prepared for Bambu Connect"
            ) from exc
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _render_plate_thumbnail(
        plate_payload: dict[str, object],
        model_path: Path | None = None,
        model_transform: tuple[float, ...] | None = None,
    ) -> bytes:
        if model_path is not None and model_path.is_file():
            try:
                return BambuStudioCliDriver._render_mesh_thumbnail(
                    model_path,
                    model_transform,
                )
            except (Exception, MemoryError):
                pass
        image = Image.new("RGB", (256, 256), "#ECEDE8")
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle(
            (12, 12, 244, 244),
            radius=12,
            fill="#D8DDD8",
            outline="#51615E",
            width=3,
        )
        raw_bbox = plate_payload.get("bbox_all")
        bbox = (
            [float(value) for value in raw_bbox]
            if isinstance(raw_bbox, list) and len(raw_bbox) == 4
            else [0.0, 0.0, 1.0, 1.0]
        )
        width = max(1.0, bbox[2] - bbox[0])
        height = max(1.0, bbox[3] - bbox[1])
        colors = plate_payload.get("filament_colors")
        fill = (
            str(colors[0])
            if isinstance(colors, list)
            and colors
            and re.fullmatch(r"#[0-9A-Fa-f]{6}", str(colors[0]))
            else "#4E8278"
        )
        objects = plate_payload.get("bbox_objects")
        if isinstance(objects, list):
            for item in objects:
                if not isinstance(item, dict):
                    continue
                raw = item.get("bbox")
                if not isinstance(raw, list) or len(raw) != 4:
                    continue
                values = [float(value) for value in raw]
                x1 = 24 + (values[0] - bbox[0]) / width * 208
                x2 = 24 + (values[2] - bbox[0]) / width * 208
                y1 = 232 - (values[3] - bbox[1]) / height * 208
                y2 = 232 - (values[1] - bbox[1]) / height * 208
                draw.rectangle(
                    (x1, y1, max(x1 + 2, x2), max(y1 + 2, y2)),
                    fill=fill,
                    outline="#273432",
                    width=1,
                )
        output = io.BytesIO()
        image.save(output, format="PNG", optimize=True)
        return output.getvalue()

    @staticmethod
    def _render_mesh_thumbnail(
        model_path: Path,
        model_transform: tuple[float, ...] | None = None,
    ) -> bytes:
        if model_path.stat().st_size > 32 * 1024 * 1024:
            raise ValueError("Model mesh is too large for thumbnail rendering")
        loaded = trimesh.load_mesh(model_path, process=False)
        if isinstance(loaded, trimesh.Scene):
            meshes = [
                geometry
                for geometry in loaded.geometry.values()
                if isinstance(geometry, trimesh.Trimesh)
            ]
            if not meshes:
                raise ValueError("Model contains no mesh geometry")
            mesh = trimesh.util.concatenate(meshes)
        elif isinstance(loaded, trimesh.Trimesh):
            mesh = loaded
        else:
            raise ValueError("Model contains unsupported geometry")
        if len(mesh.vertices) == 0 or len(mesh.faces) == 0:
            raise ValueError("Model mesh is empty")
        if len(mesh.faces) > 100_000:
            raise ValueError("Model mesh is too detailed for thumbnail rendering")

        vertices = np.asarray(mesh.vertices, dtype=np.float64)
        if model_transform is not None:
            matrix = np.asarray(model_transform, dtype=np.float64)
            if matrix.size != 16:
                raise ValueError("Model instance transform is invalid")
            vertices = trimesh.transform_points(
                vertices,
                matrix.reshape((4, 4)),
            )
        vertices -= (vertices.min(axis=0) + vertices.max(axis=0)) / 2
        z_angle = np.deg2rad(35)
        x_angle = np.deg2rad(65)
        rotate_z = np.array(
            [
                [np.cos(z_angle), -np.sin(z_angle), 0],
                [np.sin(z_angle), np.cos(z_angle), 0],
                [0, 0, 1],
            ]
        )
        rotate_x = np.array(
            [
                [1, 0, 0],
                [0, np.cos(x_angle), -np.sin(x_angle)],
                [0, np.sin(x_angle), np.cos(x_angle)],
            ]
        )
        transformed = vertices @ rotate_z.T @ rotate_x.T
        projected = transformed[:, :2].copy()
        minimum = projected.min(axis=0)
        maximum = projected.max(axis=0)
        span = np.maximum(maximum - minimum, 1e-9)
        scale = min(218 / span[0], 218 / span[1])
        projected = (projected - (minimum + maximum) / 2) * scale + 128

        image = Image.new("RGB", (256, 256), "#ECEDE8")
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle(
            (8, 8, 248, 248),
            radius=12,
            fill="#D8DDD8",
            outline="#51615E",
            width=2,
        )
        faces = np.asarray(mesh.faces, dtype=np.int64)
        depth = transformed[faces].mean(axis=1)[:, 2]
        order = np.argsort(depth)
        normals = transformed[faces][:, [1, 2, 0]] - transformed[faces][
            :, [0, 0, 1]
        ]
        face_normals = np.cross(normals[:, 0], normals[:, 1])
        lengths = np.linalg.norm(face_normals, axis=1)
        valid = lengths > 1e-12
        face_normals[valid] /= lengths[valid, None]
        light = np.array([0.35, -0.45, 0.82])
        light /= np.linalg.norm(light)
        brightness = np.clip(face_normals @ light, -0.2, 1.0)
        base = np.array([5, 119, 72], dtype=np.float64)
        for index in order:
            points = [
                (float(projected[vertex][0]), float(projected[vertex][1]))
                for vertex in faces[index]
            ]
            intensity = 0.55 + 0.35 * max(0.0, float(brightness[index]))
            color = tuple(
                int(np.clip(channel * intensity + 28, 0, 255))
                for channel in base
            )
            draw.polygon(points, fill=color)
        output = io.BytesIO()
        image.save(output, format="PNG", optimize=True)
        return output.getvalue()

    @staticmethod
    def _validate_bambu_connect_compatibility(path: Path) -> None:
        try:
            with zipfile.ZipFile(path) as archive:
                names = set(archive.namelist())
                root = ElementTree.fromstring(
                    archive.read("Metadata/model_settings.config")
                )
                plate = root.find("plate")
                metadata = {
                    item.get("key"): item.get("value")
                    for item in plate.findall("metadata")
                } if plate is not None else {}
                gcode = metadata.get("gcode_file")
                thumbnail = metadata.get("thumbnail_file")
                if (
                    not gcode
                    or gcode not in names
                    or not thumbnail
                    or thumbnail not in names
                ):
                    raise ValidationError(
                        "Slice lacks Bambu Connect plate metadata; re-slice it"
                    )
                with Image.open(io.BytesIO(archive.read(thumbnail))) as image:
                    image.verify()
        except (
            KeyError,
            OSError,
            zipfile.BadZipFile,
            ElementTree.ParseError,
        ) as exc:
            raise ValidationError(
                "Slice is incompatible with Bambu Connect; re-slice it"
            ) from exc

    @staticmethod
    def _extract_plate_thumbnail(
        path: Path,
        workspace: Path,
    ) -> Path | None:
        with zipfile.ZipFile(path) as archive:
            candidates = [
                name
                for name in archive.namelist()
                if name.casefold().endswith(
                    ("plate_1.png", "plate_1.jpg", "plate_1.jpeg")
                )
            ]
            if not candidates:
                return None
            info = archive.getinfo(candidates[0])
            if info.file_size > 10 * 1024 * 1024:
                raise PolicyViolationError("Sliced plate thumbnail exceeds size limit")
            suffix = Path(candidates[0]).suffix.casefold()
            destination = workspace / f"plate-thumbnail{suffix}"
            destination.write_bytes(archive.read(candidates[0]))
            return destination

    @staticmethod
    def _filament_usage_by_spool(
        path: Path,
        request: SliceRequest,
    ) -> dict[str, float]:
        pattern = re.compile(
            r"^\s*;\s*total\s+filament\s+weight\s*\[g\]\s*:\s*"
            r"(?P<values>[^\r\n]+)\s*$",
            re.IGNORECASE | re.MULTILINE,
        )
        parsed: list[float] | None = None
        with zipfile.ZipFile(path) as archive:
            for name in archive.namelist():
                if not name.casefold().endswith(".gcode"):
                    continue
                text = archive.read(name).decode(errors="replace")
                match = pattern.search(text)
                if match:
                    try:
                        values = [
                            float(value.strip())
                            for value in match.group("values").split(",")
                        ]
                    except ValueError as exc:
                        raise ValidationError(
                            "Sliced G-code has invalid filament weight metadata"
                        ) from exc
                    if parsed is not None:
                        raise ValidationError(
                            "Sliced output contains multiple filament weight vectors"
                        )
                    parsed = values
        if parsed is None:
            raise ValidationError(
                "Sliced G-code does not contain per-filament weight metadata"
            )
        assignments = BambuStudioCliDriver._filament_assignments(request)
        if len(parsed) != len(assignments):
            raise ValidationError(
                "Sliced filament weight vector does not match approved spools"
            )
        if any(not math.isfinite(value) or value <= 0 for value in parsed):
            raise ValidationError(
                "Sliced filament weight metadata must be finite and positive"
            )
        return {
            assignment.spool_id: usage
            for assignment, usage in zip(assignments, parsed, strict=True)
        }

    @staticmethod
    def _validate_sliced_assignment(
        path: Path,
        request: SliceRequest,
    ) -> None:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            text = "\n".join(
                archive.read(name).decode(errors="replace")
                for name in names
                if name.casefold().endswith((".gcode", ".config", ".json"))
            )
            model_settings = (
                archive.read("Metadata/model_settings.config")
                if "Metadata/model_settings.config" in names
                else None
            )
            project_settings = (
                archive.read("Metadata/project_settings.config")
                if "Metadata/project_settings.config" in names
                else None
            )
        if model_settings is None or project_settings is None:
            raise ValidationError(
                "Sliced output lacks Bambu object/material mapping metadata"
            )
        try:
            config = ElementTree.fromstring(model_settings)
            project_payload = json.loads(project_settings)
        except (ElementTree.ParseError, json.JSONDecodeError) as exc:
            raise ValidationError(
                "Sliced output contains invalid Bambu mapping metadata"
            ) from exc
        filament_assignments = BambuStudioCliDriver._filament_assignments(request)
        filament_index_by_spool = {
            assignment.spool_id: index
            for index, assignment in enumerate(filament_assignments, start=1)
        }
        expected_extruders = {
            assignment.part_id: filament_index_by_spool[assignment.spool_id]
            for assignment in request.material_assignment.assignments
        }
        aliases: dict[str, str | None] = {
            part_id.casefold(): part_id
            for part_id in expected_extruders
        }
        if request.artifact.project is not None:
            for part in request.artifact.project.parts:
                for alias in (part.id, part.name, part.module_name):
                    if not alias:
                        continue
                    key = alias.strip().casefold()
                    if key not in aliases:
                        aliases[key] = part.id
                    elif aliases[key] != part.id:
                        aliases[key] = None

        def resolve_part_id(value: str) -> str | None:
            key = value.strip().casefold()
            return aliases.get(key)

        actual_extruders: dict[str, int] = {}
        for object_element in config.findall("object"):
            metadata = {
                item.attrib.get("key"): item.attrib.get("value")
                for item in object_element.findall("metadata")
            }
            if metadata.get("name") and metadata.get("extruder"):
                actual_extruders[str(metadata["name"])] = int(
                    str(metadata["extruder"])
                )
        normalized_extruders: dict[str, int] = {}
        unresolved_extruders = False
        for object_name, filament_index in actual_extruders.items():
            part_id = resolve_part_id(object_name)
            if part_id is None:
                unresolved_extruders = True
                break
            previous = normalized_extruders.get(part_id)
            if previous is not None and previous != filament_index:
                unresolved_extruders = True
                break
            normalized_extruders[part_id] = filament_index
        if unresolved_extruders or normalized_extruders != expected_extruders:
            actual_object_filaments = (
                BambuStudioCliDriver._sliced_object_filaments(path)
            )
            normalized_object_filaments: dict[str, set[int]] = {}
            unresolved_objects = False
            for object_name, filament_indices in actual_object_filaments.items():
                part_id = resolve_part_id(object_name)
                if part_id is None:
                    unresolved_objects = True
                    break
                normalized_object_filaments.setdefault(part_id, set()).update(
                    filament_indices
                )
            expected_object_filaments = {
                part_id: {filament_index}
                for part_id, filament_index in expected_extruders.items()
            }
            if (
                unresolved_objects
                or normalized_object_filaments != expected_object_filaments
            ):
                raise ValidationError(
                    "Sliced output object-to-filament mapping differs from approval"
                )
        expected_native_settings = BambuStudioCliDriver._resolved_job_settings(request)
        for key, expected_value in expected_native_settings.items():
            if key in {"name", "from"}:
                continue
            if project_payload.get(key) != expected_value:
                raise ValidationError(
                    f"Sliced output native setting '{key}' differs from approval"
                )
        root_value = request.printer.profile.slicer.resource_root
        if root_value is None:
            raise ValidationError("Slicer profile resource root is unavailable")
        expected_profile_ids = {
            "printer_settings_id": str(
                resolve_slicer_profile_payload(
                    root_value,
                    request.printer.profile.slicer.machine_profile_id,
                    "machine",
                )["name"]
            ),
            "print_settings_id": str(
                resolve_slicer_profile_payload(
                    root_value,
                    request.printer.profile.slicer.process_profile_id,
                    "process",
                )["name"]
            ),
        }
        for key, expected_value in expected_profile_ids.items():
            if project_payload.get(key) != expected_value:
                raise ValidationError(
                    f"Sliced output profile identity '{key}' differs from approval"
                )
        gcode_filament_map = BambuStudioCliDriver._gcode_integer_vector(
            text,
            "filament_map",
        )
        expected_filament_map = [
            int(value) for value in expected_native_settings["filament_map"]
        ]
        if gcode_filament_map != expected_filament_map:
            raise ValidationError(
                "Sliced G-code physical toolhead map differs from approval"
            )

    @staticmethod
    def _sliced_object_filaments(
        path: Path,
    ) -> dict[str, set[int]]:
        with zipfile.ZipFile(path) as archive:
            try:
                slice_info = ElementTree.fromstring(
                    archive.read("Metadata/slice_info.config")
                )
            except (KeyError, ElementTree.ParseError) as exc:
                raise ValidationError(
                    "Sliced output lacks object mapping metadata"
                ) from exc
            object_names = {
                item.attrib["identify_id"]: item.attrib["name"]
                for item in slice_info.findall(".//object")
                if item.attrib.get("identify_id")
                and item.attrib.get("name")
                and item.attrib.get("skipped", "false").casefold() != "true"
            }
            if not object_names:
                raise ValidationError("Sliced output contains no named objects")
            gcode_names = [
                name
                for name in archive.namelist()
                if name.casefold().endswith(".gcode")
            ]
            if not gcode_names:
                raise ValidationError("Sliced output contains no G-code payload")
            gcode = archive.read(gcode_names[0]).decode(errors="replace")
            object_filaments = {
                int(item.attrib["id"])
                for item in slice_info.findall(".//filament")
                if item.attrib.get("id")
                and item.attrib.get("used_for_object", "false").casefold()
                == "true"
            }
        active_filament: int | None = None
        pending_object: str | None = None
        active_object: str | None = None
        usage: dict[str, set[int]] = {}
        for raw_line in gcode.splitlines():
            tool = re.match(r"^T(?P<index>\d+)(?:\s|$)", raw_line)
            if tool:
                active_filament = int(tool.group("index")) + 1
            object_id = re.search(r"OBJECT_ID:\s*(?P<id>\d+)", raw_line)
            if object_id:
                pending_object = object_id.group("id")
            if "start printing object" in raw_line and pending_object:
                active_object = pending_object
            if (
                active_object is not None
                and active_filament is not None
                and re.match(r"^(?:G0?1|G2|G3)\b", raw_line)
                and re.search(r"\bE-?(?:\d|\.\d)", raw_line)
            ):
                usage.setdefault(active_object, set()).add(active_filament)
            if "stop printing object" in raw_line:
                active_object = None
        by_name: dict[str, set[int]] = {}
        for object_id, filaments in usage.items():
            name = object_names.get(object_id)
            if name is not None:
                by_name.setdefault(name, set()).update(filaments)
        if not by_name and len(object_filaments) == 1:
            return {
                name: set(object_filaments)
                for name in object_names.values()
            }
        return by_name

    @staticmethod
    def _gcode_integer_vector(text: str, key: str) -> list[int] | None:
        match = re.search(
            rf"^\s*;\s*{re.escape(key)}\s*=\s*(?P<values>[^\r\n]+)\s*$",
            text,
            re.IGNORECASE | re.MULTILINE,
        )
        if match is None:
            return None
        try:
            return [
                int(value.strip())
                for value in re.split(r"[,;]", match.group("values"))
                if value.strip()
            ]
        except ValueError:
            return None

    @staticmethod
    async def _version(executable: str) -> str:
        try:
            process = await asyncio.create_subprocess_exec(
                executable,
                "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            output, _ = await asyncio.wait_for(process.communicate(), timeout=20)
            return output.decode(errors="replace").strip()[:200] or "unknown"
        except Exception:
            return "unknown"
