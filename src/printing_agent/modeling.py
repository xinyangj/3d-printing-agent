from __future__ import annotations

import asyncio
import hashlib
import re
import shutil
from pathlib import Path

import trimesh

from printing_agent.artifact_store import ArtifactStore, sha256_file
from printing_agent.config import Settings
from printing_agent.domain import (
    ArtifactProvenance,
    Dimensions,
    MeshReport,
    ModelArtifact,
    ModelDecision,
    ModelingHandoff,
    SelectedSourceInspection,
    WorkflowState,
)
from printing_agent.errors import (
    BudgetExhaustedError,
    ExternalServiceError,
    PolicyViolationError,
    ValidationError,
)
from printing_agent.multipart import (
    ThreeMFService,
    single_part_project,
    write_project_artifact,
    write_structured_import_artifact,
)
from printing_agent.repositories import WorkflowRepository

_FORBIDDEN_TOKENS = re.compile(r"\b(include|use|surface)\b", re.IGNORECASE)
_IMPORT_TOKEN = re.compile(r"\bimport\b", re.IGNORECASE)
_LITERAL_IMPORT = re.compile(
    r'\bimport\s*\(\s*(?:file\s*=\s*)?"(?P<path>[^"]+)"',
    re.IGNORECASE,
)


def _without_comments(source: str) -> str:
    output: list[str] = []
    index = 0
    in_string = False
    while index < len(source):
        character = source[index]
        next_character = source[index + 1] if index + 1 < len(source) else ""
        if in_string:
            output.append(character)
            if character == "\\" and next_character:
                output.append(next_character)
                index += 2
                continue
            if character == '"':
                in_string = False
            index += 1
            continue
        if character == '"':
            in_string = True
            output.append(character)
            index += 1
            continue
        if character == "/" and next_character == "/":
            index += 2
            while index < len(source) and source[index] not in "\r\n":
                index += 1
            output.append("\n")
            continue
        if character == "/" and next_character == "*":
            end = source.find("*/", index + 2)
            if end < 0:
                raise PolicyViolationError("OpenSCAD contains an unterminated block comment")
            output.append(" ")
            index = end + 2
            continue
        output.append(character)
        index += 1
    if in_string:
        raise PolicyViolationError("OpenSCAD contains an unterminated string")
    return "".join(output)


class OpenScadSourcePolicy:
    def __init__(self, max_source_bytes: int = 100_000) -> None:
        self.max_source_bytes = max_source_bytes

    def validate(self, source: str, allowed_import: str | None) -> None:
        encoded = source.encode("utf-8")
        if not encoded or len(encoded) > self.max_source_bytes:
            raise PolicyViolationError(
                f"OpenSCAD source must be between 1 and {self.max_source_bytes} bytes"
            )
        uncommented = _without_comments(source)
        if _FORBIDDEN_TOKENS.search(uncommented):
            raise PolicyViolationError("OpenSCAD include, use, and surface are not allowed")

        import_tokens = list(_IMPORT_TOKEN.finditer(uncommented))
        literal_imports = list(_LITERAL_IMPORT.finditer(uncommented))
        if len(import_tokens) != len(literal_imports):
            raise PolicyViolationError("Every OpenSCAD import must use a literal filename")
        imported_paths = [match.group("path") for match in literal_imports]
        if allowed_import is None and imported_paths:
            raise PolicyViolationError("Generated-from-scratch models cannot import files")
        for imported_path in imported_paths:
            if (
                imported_path != allowed_import
                or Path(imported_path).is_absolute()
                or ".." in Path(imported_path).parts
                or ":" in imported_path
                or "\\" in imported_path
            ):
                raise PolicyViolationError("OpenSCAD imports may reference only source.stl")
        if allowed_import is not None and allowed_import not in imported_paths:
            raise PolicyViolationError("Modification source must import source.stl")


class MeshInspector:
    async def inspect(self, path: Path) -> MeshReport:
        return await asyncio.to_thread(self._inspect_sync, path)

    @staticmethod
    def _inspect_sync(path: Path) -> MeshReport:
        if not path.is_file() or path.stat().st_size == 0:
            raise ValidationError("STL output is missing or empty")
        try:
            loaded = trimesh.load(path, file_type="stl", force="mesh")
            if isinstance(loaded, trimesh.Scene):
                if not loaded.geometry:
                    raise ValidationError("STL contains no geometry")
                mesh = trimesh.util.concatenate(tuple(loaded.geometry.values()))
            else:
                mesh = loaded
        except ValidationError:
            raise
        except Exception as exc:
            raise ValidationError(f"Could not parse STL: {exc}") from exc

        if len(mesh.faces) == 0 or len(mesh.vertices) == 0:
            raise ValidationError("STL contains no triangles")
        if not bool(mesh.vertices.dtype.kind in {"f", "i", "u"}):
            raise ValidationError("STL vertices are invalid")
        import numpy as np

        finite = bool(np.isfinite(mesh.vertices).all())
        if not finite:
            raise ValidationError("STL contains non-finite vertices")
        extents = mesh.extents
        if not np.isfinite(extents).all() or (extents <= 0).any():
            raise ValidationError("STL has invalid dimensions")
        components = mesh.split(only_watertight=False)
        volume = abs(float(mesh.volume)) if mesh.is_volume else 0.0
        return MeshReport(
            dimensions=Dimensions(
                width_mm=float(extents[0]),
                depth_mm=float(extents[1]),
                height_mm=float(extents[2]),
            ),
            triangle_count=int(len(mesh.faces)),
            connected_components=max(1, len(components)),
            watertight=bool(mesh.is_watertight),
            volume_mm3=volume,
            finite=finite,
        )


class OpenScadRenderer:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def render(self, source_path: Path, output_path: Path) -> str:
        try:
            process = await asyncio.create_subprocess_exec(
                self.settings.openscad_path,
                "--hardwarnings",
                "--export-format",
                "binstl",
                "-o",
                output_path.name,
                source_path.name,
                cwd=source_path.parent,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise ExternalServiceError(
                f"OpenSCAD executable '{self.settings.openscad_path}' was not found"
            ) from exc
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=self.settings.openscad_timeout_seconds,
            )
        except TimeoutError as exc:
            process.kill()
            await process.wait()
            raise ExternalServiceError("OpenSCAD rendering timed out") from exc
        diagnostics = (stdout + stderr).decode("utf-8", errors="replace")[-8_000:]
        if process.returncode != 0:
            raise ValidationError(
                f"OpenSCAD exited with status {process.returncode}: {diagnostics}"
            )
        await asyncio.to_thread(
            self._validate_output,
            output_path,
            diagnostics,
            self.settings.max_download_bytes,
        )
        return diagnostics

    @staticmethod
    def _validate_output(
        output_path: Path,
        diagnostics: str,
        max_output_bytes: int,
    ) -> None:
        if not output_path.is_file():
            raise ValidationError(f"OpenSCAD did not create an STL: {diagnostics}")
        if output_path.stat().st_size > max_output_bytes:
            output_path.unlink(missing_ok=True)
            raise ValidationError("Rendered STL exceeds the configured size limit")


class TrimeshSelectedSourceInspector:
    def __init__(self, mesh_inspector: MeshInspector) -> None:
        self.mesh_inspector = mesh_inspector

    async def inspect(
        self,
        workflow_id: str,
        candidate_id: str,
        file_id: str,
        path: Path,
        build_volume: Dimensions,
    ) -> SelectedSourceInspection:
        temporary_stl: Path | None = None
        try:
            if path.suffix.casefold() == ".3mf":
                _, _, combined = await asyncio.to_thread(ThreeMFService().read, path)
                temporary_stl = path.with_suffix(".inspection.stl")
                await asyncio.to_thread(combined.export, temporary_stl)
                mesh = await self.mesh_inspector.inspect(temporary_stl)
            else:
                mesh = await self.mesh_inspector.inspect(path)
            accepted = mesh.finite and mesh.triangle_count > 0
            reason = None
        except ValidationError as exc:
            mesh = MeshReport(
                dimensions=Dimensions(width_mm=1, depth_mm=1, height_mm=1),
                triangle_count=1,
                connected_components=1,
                watertight=False,
                volume_mm3=0,
                finite=False,
            )
            accepted = False
            reason = exc.message
        finally:
            if temporary_stl is not None:
                temporary_stl.unlink(missing_ok=True)
        return SelectedSourceInspection(
            workflow_id=workflow_id,
            candidate_id=candidate_id,
            file_id=file_id,
            source_digest=sha256_file(path),
            cached_path=path,
            mesh=mesh,
            accepted=accepted,
            rejection_reason=reason,
        )


class ModelPipeline:
    def __init__(
        self,
        settings: Settings,
        repository: WorkflowRepository,
        artifacts: ArtifactStore,
        renderer: OpenScadRenderer,
        mesh_inspector: MeshInspector,
        source_policy: OpenScadSourcePolicy | None = None,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.artifacts = artifacts
        self.renderer = renderer
        self.mesh_inspector = mesh_inspector
        self.source_policy = source_policy or OpenScadSourcePolicy()

    async def adopt_source(
        self,
        handoff: ModelingHandoff,
        source_code: str,
    ) -> ModelArtifact:
        attempts = await self.repository.count_source_attempts(
            handoff.workflow_id,
            handoff.version,
        )
        if attempts >= self.settings.generation_attempt_budget:
            raise BudgetExhaustedError("OpenSCAD generation attempt budget was exhausted")
        attempt = await self.repository.next_source_attempt(
            handoff.workflow_id,
            handoff.version,
        )
        canonical_source = f"module base_model() {{\n{source_code}\n}}\nbase_model();\n"
        source_digest = hashlib.sha256(canonical_source.encode()).hexdigest()
        await self.repository.save_source_attempt(
            handoff.workflow_id,
            handoff.version,
            attempt,
            "received",
            source_digest,
        )
        allowed_import = "source.stl" if handoff.decision == ModelDecision.MODIFY else None
        try:
            self.source_policy.validate(source_code, allowed_import)
            attempt_dir = self.artifacts.attempt_directory(
                handoff.workflow_id,
                handoff.version,
                attempt,
            )
            source_path = attempt_dir / "source.scad"
            source_path.write_text(canonical_source, encoding="utf-8")
            if handoff.selected_source is not None:
                selected_path = (
                    self.settings.candidate_cache_dir
                    / "models"
                    / f"{handoff.selected_source.source_digest}.stl"
                )
                if not selected_path.is_file():
                    raise PolicyViolationError("Selected source cache entry is missing")
                if sha256_file(selected_path) != handoff.selected_source.source_digest:
                    raise PolicyViolationError("Selected source digest changed")
                shutil.copy2(selected_path, attempt_dir / "source.stl")

            await self.repository.transition(
                handoff.workflow_id,
                WorkflowState.RENDERING,
                event_kind="model.rendering",
                payload={"handoff_version": handoff.version, "attempt": attempt},
            )
            temporary_model = attempt_dir / "model.tmp.stl"
            diagnostics = await self.renderer.render(source_path, temporary_model)
            await self.repository.transition(
                handoff.workflow_id,
                WorkflowState.VALIDATING,
                event_kind="model.validating",
                payload={"handoff_version": handoff.version, "attempt": attempt},
            )
            mesh = await self.mesh_inspector.inspect(temporary_model)
            if not mesh.watertight or mesh.volume_mm3 <= 0:
                raise ValidationError("Rendered model must be watertight with positive volume")
            if not mesh.dimensions.fits(handoff.target_printer.build_volume):
                raise ValidationError("Rendered model exceeds the target printer build volume")
            target = handoff.model_plan.target_dimensions
            if target is not None and not self._within_dimension_tolerance(mesh.dimensions, target):
                raise ValidationError("Rendered model dimensions differ from the requested size")

            model_path = attempt_dir / "model.stl"
            temporary_model.replace(model_path)
            version = await self.repository.next_artifact_version(handoff.workflow_id)
            provenance = self._provenance(handoff)
            project = single_part_project(
                source_digest=source_digest,
                imported=False,
                source_filename="main.scad",
            )
            staging = attempt_dir / "artifact"
            manifest, files = await asyncio.to_thread(
                write_project_artifact,
                directory=staging,
                workflow_id=handoff.workflow_id,
                version=version,
                project=project,
                source_path=source_path,
                model_path=model_path,
                mesh=mesh,
                provenance=provenance,
                handoff_digest=handoff.digest,
                diagnostics=diagnostics,
                imported=False,
            )
            adopted = self.artifacts.adopt_project(
                handoff.workflow_id,
                version,
                staging,
            )
            manifest_digest = str(manifest["manifest_digest"])
            artifact = ModelArtifact(
                schema_version="2",
                workflow_id=handoff.workflow_id,
                version=version,
                source_path=adopted / "project" / "main.scad",
                model_path=adopted / "outputs" / "model.stl",
                project_path=adopted / "project" / "project.json",
                three_mf_path=adopted / "outputs" / "model.3mf",
                source_digest=source_digest,
                model_digest=str(manifest["model_digest"]),
                project_digest=str(manifest["project_digest"]),
                three_mf_digest=str(manifest["three_mf_digest"]),
                manifest_digest=manifest_digest,
                mesh=mesh,
                project=project,
                part_meshes={"base_model": mesh},
                files=files,
                provenance=provenance,
            )
            await self.repository.save_artifact(artifact)
            await self.repository.save_source_attempt(
                handoff.workflow_id,
                handoff.version,
                attempt,
                "adopted",
                source_digest,
            )
            await self.repository.transition(
                handoff.workflow_id,
                WorkflowState.AWAITING_APPROVAL,
                event_kind="artifact.ready",
                payload={
                    "artifact_version": version,
                    "manifest_digest": manifest_digest,
                },
            )
            return artifact
        except Exception as exc:
            await self.repository.save_source_attempt(
                handoff.workflow_id,
                handoff.version,
                attempt,
                "rejected",
                source_digest,
                str(exc)[-2_000:],
            )
            workflow = await self.repository.get_workflow(handoff.workflow_id)
            if workflow.state in {WorkflowState.RENDERING, WorkflowState.VALIDATING}:
                await self.repository.transition(
                    handoff.workflow_id,
                    WorkflowState.GENERATING,
                    event_kind="model.repair_requested",
                    payload={"attempt": attempt, "diagnostics": str(exc)[-2_000:]},
                )
            raise

    async def adopt_existing(
        self,
        workflow_id: str,
        model_path: Path,
        provenance: ArtifactProvenance,
        build_volume: Dimensions,
    ) -> ModelArtifact:
        mesh = await self.mesh_inspector.inspect(model_path)
        if not mesh.watertight or mesh.volume_mm3 <= 0:
            raise ValidationError("Selected model must be watertight with positive volume")
        if not mesh.dimensions.fits(build_volume):
            raise ValidationError("Selected model exceeds the target printer build volume")
        version = await self.repository.next_artifact_version(workflow_id)
        model_digest = sha256_file(model_path)
        project = single_part_project(
            source_digest=model_digest,
            imported=True,
            source_filename=model_path.name,
        )
        staging = (
            self.artifacts.workflow_root(workflow_id)
            / "staging"
            / f"artifact-{version}"
        )
        manifest, files = await asyncio.to_thread(
            write_project_artifact,
            directory=staging,
            workflow_id=workflow_id,
            version=version,
            project=project,
            source_path=model_path,
            model_path=model_path,
            mesh=mesh,
            provenance=provenance,
            handoff_digest=None,
            diagnostics=None,
            imported=True,
        )
        adopted = self.artifacts.adopt_project(
            workflow_id,
            version,
            staging,
        )
        source_path = adopted / "project" / "main.scad"
        manifest_digest = str(manifest["manifest_digest"])
        artifact = ModelArtifact(
            schema_version="2",
            workflow_id=workflow_id,
            version=version,
            source_path=source_path,
            model_path=adopted / "outputs" / "model.stl",
            project_path=adopted / "project" / "project.json",
            three_mf_path=adopted / "outputs" / "model.3mf",
            source_digest=sha256_file(source_path),
            model_digest=str(manifest["model_digest"]),
            project_digest=str(manifest["project_digest"]),
            three_mf_digest=str(manifest["three_mf_digest"]),
            manifest_digest=manifest_digest,
            mesh=mesh,
            project=project,
            part_meshes={"base_model": mesh},
            files=files,
            provenance=provenance,
        )
        await self.repository.save_artifact(artifact)
        await self.repository.transition(
            workflow_id,
            WorkflowState.AWAITING_APPROVAL,
            event_kind="artifact.ready",
            payload={"artifact_version": version, "manifest_digest": manifest_digest},
        )
        return artifact

    async def adopt_existing_3mf(
        self,
        workflow_id: str,
        source_path: Path,
        provenance: ArtifactProvenance,
        build_volume: Dimensions,
    ) -> ModelArtifact:
        version = await self.repository.next_artifact_version(workflow_id)
        project, part_meshes, combined_mesh = await asyncio.to_thread(
            ThreeMFService().read,
            source_path,
        )
        workspace = (
            self.artifacts.workflow_root(workflow_id)
            / "staging"
            / f"three-mf-{version}"
        )
        inspection_directory = workspace / "inspection"
        inspection_directory.mkdir(parents=True, exist_ok=False)
        combined_path = inspection_directory / "model.stl"
        await asyncio.to_thread(combined_mesh.export, combined_path)
        mesh = await self.mesh_inspector.inspect(combined_path)
        if not mesh.dimensions.fits(build_volume):
            raise ValidationError("Selected 3MF exceeds the target printer build volume")
        part_reports: dict[str, MeshReport] = {}
        for part_id, part_mesh in part_meshes.items():
            part_path = inspection_directory / f"{part_id}.stl"
            await asyncio.to_thread(part_mesh.export, part_path)
            report = await self.mesh_inspector.inspect(part_path)
            if not report.finite or report.triangle_count <= 0:
                raise ValidationError(f"3MF part '{part_id}' is invalid")
            part_reports[part_id] = report

        staging = workspace / "artifact"
        manifest, files = await asyncio.to_thread(
            write_structured_import_artifact,
            directory=staging,
            workflow_id=workflow_id,
            version=version,
            project=project,
            source_path=source_path,
            part_meshes=part_meshes,
            combined_mesh=combined_mesh,
            mesh=mesh,
            part_reports=part_reports,
            provenance=provenance,
        )
        adopted = self.artifacts.adopt_project(workflow_id, version, staging)
        adopted_source = adopted / "project" / "main.scad"
        artifact = ModelArtifact(
            schema_version="2",
            workflow_id=workflow_id,
            version=version,
            source_path=adopted_source,
            model_path=adopted / "outputs" / "model.stl",
            project_path=adopted / "project" / "project.json",
            three_mf_path=adopted / "outputs" / "model.3mf",
            source_digest=sha256_file(adopted_source),
            model_digest=str(manifest["model_digest"]),
            project_digest=str(manifest["project_digest"]),
            three_mf_digest=str(manifest["three_mf_digest"]),
            manifest_digest=str(manifest["manifest_digest"]),
            mesh=mesh,
            project=project,
            part_meshes=part_reports,
            files=files,
            provenance=provenance,
        )
        await self.repository.save_artifact(artifact)
        await self.repository.transition(
            workflow_id,
            WorkflowState.AWAITING_APPROVAL,
            event_kind="artifact.ready",
            payload={
                "artifact_version": version,
                "manifest_digest": artifact.manifest_digest,
                "part_count": len(project.parts),
            },
        )
        return artifact

    @staticmethod
    def _within_dimension_tolerance(actual: Dimensions, target: Dimensions) -> bool:
        for measured, expected in (
            (actual.width_mm, target.width_mm),
            (actual.depth_mm, target.depth_mm),
            (actual.height_mm, target.height_mm),
        ):
            tolerance = max(1.0, expected * 0.1)
            if abs(measured - expected) > tolerance:
                return False
        return True

    @staticmethod
    def _provenance(handoff: ModelingHandoff) -> ArtifactProvenance:
        if handoff.selected_source is None:
            return ArtifactProvenance(kind="generated")
        source = handoff.selected_source
        return ArtifactProvenance(
            kind="catalog",
            candidate_id=source.candidate_id,
            source_url=source.attribution_url,
            creator=source.creator,
            license=source.license,
            source_digest=source.source_digest,
        )
