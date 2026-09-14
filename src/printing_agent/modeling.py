from __future__ import annotations

import asyncio
import hashlib
import json
import re
import shutil
from pathlib import Path

import trimesh
from lib3mf import Lib3MF

from printing_agent.artifact_store import ArtifactStore, sha256_file
from printing_agent.config import Settings
from printing_agent.domain import (
    AnnotationOrigin,
    ArtifactProvenance,
    ArtifactRevision,
    CandidateFile,
    Dimensions,
    MeshReport,
    ModelArtifact,
    ModelDecision,
    ModelingHandoff,
    PartGeometryKind,
    SelectedCandidateFile,
    SelectedFileRole,
    SelectedSourceInspection,
    SourceAsset,
    WorkflowState,
)
from printing_agent.errors import (
    BudgetExhaustedError,
    ExternalServiceError,
    PolicyViolationError,
    SourceCacheError,
    ValidationError,
)
from printing_agent.multipart import (
    ThreeMFService,
    build_source_set_project,
    rebuild_part_project_artifact,
    repack_project_instances,
    single_part_project,
    write_project_artifact,
    write_source_set_artifact,
    write_structured_import_artifact,
)
from printing_agent.repositories import WorkflowRepository

_FORBIDDEN_TOKENS = re.compile(r"\b(include|use|surface)\b", re.IGNORECASE)
_IMPORT_TOKEN = re.compile(r"\bimport\b", re.IGNORECASE)
_LITERAL_IMPORT = re.compile(
    r'\bimport\s*\(\s*(?:file\s*=\s*)?"(?P<path>[^"]+)"',
    re.IGNORECASE,
)
_BASE_MODEL_MODULE = re.compile(r"^\s*module\s+base_model\s*\(\s*\)\s*\{")


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


def unwrap_base_model_source(source: str) -> str:
    current = source.strip()
    while True:
        match = _BASE_MODEL_MODULE.match(current)
        if match is None:
            return current
        open_brace = current.find("{", match.start(), match.end())
        depth = 0
        in_string = False
        line_comment = False
        block_comment = False
        close_brace = None
        index = open_brace
        while index < len(current):
            character = current[index]
            following = current[index + 1] if index + 1 < len(current) else ""
            if line_comment:
                if character in "\r\n":
                    line_comment = False
                index += 1
                continue
            if block_comment:
                if character == "*" and following == "/":
                    block_comment = False
                    index += 2
                    continue
                index += 1
                continue
            if in_string:
                if character == "\\" and following:
                    index += 2
                    continue
                if character == '"':
                    in_string = False
                index += 1
                continue
            if character == "/" and following == "/":
                line_comment = True
                index += 2
                continue
            if character == "/" and following == "*":
                block_comment = True
                index += 2
                continue
            if character == '"':
                in_string = True
            elif character == "{":
                depth += 1
            elif character == "}":
                depth -= 1
                if depth == 0:
                    close_brace = index
                    break
            index += 1
        if close_brace is None:
            return current
        if current[close_brace + 1 :].strip() != "base_model();":
            return current
        current = current[open_brace + 1 : close_brace].strip()


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
            raise ExternalServiceError(
                "OpenSCAD rendering exceeded "
                f"{self.settings.openscad_timeout_seconds} seconds"
            ) from exc
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
                try:
                    _, _, combined = await asyncio.to_thread(ThreeMFService().read, path)
                except Lib3MF.ELib3MFException as exc:
                    raise ValidationError(f"Could not parse 3MF: {exc}") from exc
                temporary_stl = path.with_suffix(".inspection.stl")
                await asyncio.to_thread(combined.export, temporary_stl)
                mesh = await self.mesh_inspector.inspect(temporary_stl)
            else:
                mesh = await self.mesh_inspector.inspect(path)
            findings: list[str] = []
            if not mesh.finite:
                findings.append("mesh contains non-finite vertices")
            if mesh.triangle_count <= 0:
                findings.append("mesh contains no triangles")
            if not mesh.watertight:
                findings.append("mesh is not watertight")
            if mesh.volume_mm3 <= 0:
                findings.append("mesh has no validated positive volume")
            accepted = not findings
            reason = "; ".join(findings) if findings else None
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

    async def prepare_source_set_files(
        self,
        workflow_id: str,
        selected: list[
            tuple[SelectedCandidateFile, CandidateFile, Path, MeshReport]
        ],
        revisions: list[tuple[str, str, str]],
    ) -> tuple[
        list[tuple[SelectedCandidateFile, CandidateFile, Path, MeshReport]],
        Path,
    ]:
        selected_by_part = {
            selection.part_id: (selection, candidate_file, path, report)
            for selection, candidate_file, path, report in selected
            if selection.role == SelectedFileRole.UNIQUE_PART
            and selection.part_id is not None
        }
        revision_ids = [part_id for part_id, _, _ in revisions]
        if not revision_ids:
            raise ValidationError("Source preparation must revise at least one included part")
        if len(revision_ids) != len(set(revision_ids)):
            raise ValidationError("Prepared part IDs must be unique")
        unknown = sorted(set(revision_ids) - set(selected_by_part))
        if unknown:
            raise ValidationError(
                f"Source preparation references unknown parts: {', '.join(unknown)}"
            )
        workspace = (
            self.artifacts.workflow_root(workflow_id)
            / "staging"
            / "source-preparation"
        )
        shutil.rmtree(workspace, ignore_errors=True)
        prepared_paths: dict[str, tuple[Path, MeshReport]] = {}
        try:
            for part_id, source_code, _ in revisions:
                self.source_policy.validate(source_code, "source.stl")
                _, _, original_path, _ = selected_by_part[part_id]
                part_directory = workspace / part_id
                part_directory.mkdir(parents=True, exist_ok=False)
                shutil.copy2(original_path, part_directory / "source.stl")
                source_path = part_directory / "source.scad"
                source_path.write_text(
                    (
                        "module prepared_part() {\n"
                        f"{source_code}\n"
                        "}\n"
                        "prepared_part();\n"
                    ),
                    encoding="utf-8",
                )
                output_path = part_directory / "prepared.stl"
                await self.renderer.render(source_path, output_path)
                report = await self.mesh_inspector.inspect(output_path)
                if not report.watertight or report.volume_mm3 <= 0:
                    raise ValidationError(
                        f"Prepared part '{part_id}' must be watertight with positive volume"
                    )
                prepared_paths[part_id] = (output_path, report)
            prepared = [
                (
                    selection,
                    candidate_file,
                    prepared_paths.get(selection.part_id, (path, report))[0],
                    prepared_paths.get(selection.part_id, (path, report))[1],
                )
                for selection, candidate_file, path, report in selected
            ]
            return prepared, workspace
        except Exception:
            shutil.rmtree(workspace, ignore_errors=True)
            raise

    async def adopt_source(
        self,
        handoff: ModelingHandoff,
        source_code: str,
        *,
        defer_ready: bool = False,
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
                    raise SourceCacheError("Selected source cache entry is missing")
                if sha256_file(selected_path) != handoff.selected_source.source_digest:
                    raise SourceCacheError("Selected source digest changed")
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
            await self.repository.save_artifact(artifact, activate=not defer_ready)
            await self.repository.save_source_attempt(
                handoff.workflow_id,
                handoff.version,
                attempt,
                "adopted",
                source_digest,
            )
            if not defer_ready:
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
            candidate_failure = isinstance(
                exc,
                (PolicyViolationError, ValidationError),
            )
            await self.repository.save_source_attempt(
                handoff.workflow_id,
                handoff.version,
                attempt,
                "rejected" if candidate_failure else "infrastructure_error",
                source_digest,
                str(exc)[-2_000:],
            )
            workflow = await self.repository.get_workflow(handoff.workflow_id)
            if candidate_failure and workflow.state in {
                WorkflowState.RENDERING,
                WorkflowState.VALIDATING,
            }:
                await self.repository.transition(
                    handoff.workflow_id,
                    WorkflowState.GENERATING,
                    event_kind="model.repair_requested",
                    payload={"attempt": attempt, "diagnostics": str(exc)[-2_000:]},
                )
            raise

    async def adopt_part_revisions(
        self,
        handoff: ModelingHandoff,
        artifact: ModelArtifact,
        revisions: list[tuple[str, str, str]],
        *,
        feedback: str,
        rationale: str,
        allowed_part_ids: list[str] | None,
        defer_ready: bool = False,
    ) -> ModelArtifact:
        if artifact.project is None or len(artifact.project.parts) < 2:
            raise ValidationError("Batch part revision requires a multipart artifact")
        if artifact.project_path is None:
            raise ValidationError("Multipart project metadata is unavailable")
        if artifact.project.assembly_status != "not_provided":
            raise ValidationError(
                "Multipart mesh revisions currently require a separated print-layout project"
            )
        if not revisions:
            raise ValidationError("At least one affected part is required")
        revision_ids = [part_id for part_id, _, _ in revisions]
        if len(revision_ids) != len(set(revision_ids)):
            raise ValidationError("Affected part IDs must be unique")
        parts_by_id = {part.id: part for part in artifact.project.parts}
        unknown = sorted(set(revision_ids) - set(parts_by_id))
        if unknown:
            raise ValidationError(f"Unknown target parts: {', '.join(unknown)}")
        if allowed_part_ids is not None:
            outside_scope = sorted(set(revision_ids) - set(allowed_part_ids))
            if outside_scope:
                raise ValidationError(
                    f"Parts outside the allowed edit scope: {', '.join(outside_scope)}"
                )
        attempts = await self.repository.count_source_attempts(
            handoff.workflow_id, handoff.version
        )
        if attempts >= self.settings.generation_attempt_budget:
            raise BudgetExhaustedError("OpenSCAD generation attempt budget was exhausted")
        attempt = await self.repository.next_source_attempt(
            handoff.workflow_id, handoff.version
        )
        canonical_sources: dict[str, str] = {}
        for part_id, source_code, _ in revisions:
            part = parts_by_id[part_id]
            module_name = part.module_name or f"part_{part_id}"
            canonical_sources[part_id] = (
                f"module {module_name}() {{\n{source_code}\n}}\n{module_name}();\n"
            )
        source_digest = hashlib.sha256(
            json.dumps(canonical_sources, sort_keys=True).encode()
        ).hexdigest()
        await self.repository.save_source_attempt(
            handoff.workflow_id,
            handoff.version,
            attempt,
            "received",
            source_digest,
        )
        try:
            attempt_dir = self.artifacts.attempt_directory(
                handoff.workflow_id, handoff.version, attempt
            )
            await self.repository.transition(
                handoff.workflow_id,
                WorkflowState.RENDERING,
                event_kind="model.parts_rendering",
                payload={"part_ids": revision_ids, "attempt": attempt},
            )
            rendered_paths: dict[str, Path] = {}
            source_directory = artifact.project_path.parent.parent
            for part_id, source_code, _ in revisions:
                self.source_policy.validate(source_code, "source.stl")
                part_attempt = attempt_dir / "parts" / part_id
                part_attempt.mkdir(parents=True, exist_ok=False)
                source_path = part_attempt / "source.scad"
                source_path.write_text(canonical_sources[part_id], encoding="utf-8")
                shutil.copy2(
                    source_directory / "outputs" / "parts" / f"{part_id}.stl",
                    part_attempt / "source.stl",
                )
                temporary_model = part_attempt / "part.tmp.stl"
                await self.renderer.render(source_path, temporary_model)
                rendered_paths[part_id] = temporary_model
            await self.repository.transition(
                handoff.workflow_id,
                WorkflowState.VALIDATING,
                event_kind="model.parts_validating",
                payload={"part_ids": revision_ids, "attempt": attempt},
            )
            revised_reports: dict[str, MeshReport] = {}
            for part_id, rendered_path in rendered_paths.items():
                report = await self.mesh_inspector.inspect(rendered_path)
                if not report.watertight or report.volume_mm3 <= 0:
                    raise ValidationError(
                        f"Revised part '{part_id}' must be watertight with positive volume"
                    )
                revised_reports[part_id] = report

            version = await self.repository.next_artifact_version(handoff.workflow_id)
            staging = attempt_dir / "artifact"
            shutil.copytree(source_directory, staging)
            imports_directory = staging / "project" / "imports"
            revised_parts_by_id = dict(parts_by_id)
            revised_assets: list[SourceAsset] = []
            for part_id, _, _ in revisions:
                derived_name = f"{part_id}-revision-source-v{artifact.version}.stl"
                derived_path = imports_directory / derived_name
                shutil.copy2(
                    source_directory / "outputs" / "parts" / f"{part_id}.stl",
                    derived_path,
                )
                rendered_paths[part_id].replace(
                    staging / "outputs" / "parts" / f"{part_id}.stl"
                )
                canonical_part_source = canonical_sources[part_id].replace(
                    "source.stl", f"../imports/{derived_name}"
                )
                (staging / "project" / "parts" / f"{part_id}.scad").write_text(
                    canonical_part_source, encoding="utf-8"
                )
                part_suffix = hashlib.sha256(part_id.encode()).hexdigest()[:8]
                asset_id = f"revision-{part_id[:30]}-{part_suffix}-{version}"
                revised_assets.append(
                    SourceAsset(
                        id=asset_id,
                        filename=derived_name,
                        format="stl",
                        digest=sha256_file(derived_path),
                        path=f"project/imports/{derived_name}",
                        role="revision_input",
                        original_cad=False,
                        annotation_origin=AnnotationOrigin.AGENT_INFERENCE,
                    )
                )
                revised_parts_by_id[part_id] = parts_by_id[part_id].model_copy(
                    update={
                        "geometry_kind": PartGeometryKind.DERIVED_MESH,
                        "source_asset_id": asset_id,
                        "annotation_origin": AnnotationOrigin.AGENT_INFERENCE,
                    }
                )
            project = artifact.project.model_copy(
                update={
                    "parts": [
                        revised_parts_by_id[part.id] for part in artifact.project.parts
                    ],
                    "source_assets": [
                        *artifact.project.source_assets,
                        *revised_assets,
                    ],
                }
            )
            part_reports = dict(artifact.part_meshes)
            part_reports.update(revised_reports)
            meshes: dict[str, trimesh.Trimesh] = {}
            for project_part in project.parts:
                loaded = trimesh.load_mesh(
                    staging / "outputs" / "parts" / f"{project_part.id}.stl",
                    force="mesh",
                )
                meshes[project_part.id] = loaded
            project, layout = repack_project_instances(
                project,
                meshes,
                max_layout_width=handoff.target_printer.build_volume.width_mm,
            )
            layout_path = attempt_dir / "layout.stl"
            await asyncio.to_thread(layout.export, layout_path)
            mesh = await self.mesh_inspector.inspect(layout_path)
            if not mesh.dimensions.fits(handoff.target_printer.build_volume):
                raise ValidationError("Revised print layout exceeds the target build volume")
            revision = ArtifactRevision(
                feedback=feedback,
                affected_part_ids=revision_ids,
                allowed_part_ids=allowed_part_ids,
                rationale=rationale,
            )
            old_manifest = artifact.project_path.parent.parent / "manifest.json"
            old_data = json.loads(old_manifest.read_text(encoding="utf-8"))
            manifest, files = await asyncio.to_thread(
                rebuild_part_project_artifact,
                directory=staging,
                workflow_id=handoff.workflow_id,
                version=version,
                project=project,
                mesh=mesh,
                part_reports=part_reports,
                provenance=artifact.provenance,
                classification=list(old_data.get("source_set_classification", [])),
                handoff_digest=handoff.digest or "",
                revision=revision,
            )
            adopted = self.artifacts.adopt_project(
                handoff.workflow_id, version, staging
            )
            primary_part = project.parts[0].id
            adopted_source = adopted / "project" / "main.scad"
            revised_artifact = ModelArtifact(
                schema_version="2",
                workflow_id=handoff.workflow_id,
                version=version,
                source_path=adopted_source,
                model_path=adopted / "outputs" / "parts" / f"{primary_part}.stl",
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
                provenance=artifact.provenance,
                revision=revision,
            )
            await self.repository.save_artifact(
                revised_artifact,
                activate=not defer_ready,
            )
            await self.repository.save_source_attempt(
                handoff.workflow_id,
                handoff.version,
                attempt,
                "adopted",
                source_digest,
            )
            if not defer_ready:
                await self.repository.transition(
                    handoff.workflow_id,
                    WorkflowState.AWAITING_APPROVAL,
                    event_kind="artifact.multipart_revision_ready",
                    payload={
                        "artifact_version": version,
                        "affected_part_ids": revision_ids,
                        "allowed_part_ids": allowed_part_ids,
                        "rationale": rationale,
                        "manifest_digest": revised_artifact.manifest_digest,
                    },
                )
            return revised_artifact
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
                    event_kind="model.parts_repair_requested",
                    payload={
                        "part_ids": revision_ids,
                        "diagnostics": str(exc)[-2_000:],
                    },
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

    async def adopt_existing_set(
        self,
        workflow_id: str,
        selected: list[
            tuple[SelectedCandidateFile, CandidateFile, Path, MeshReport]
        ],
        provenance: ArtifactProvenance,
        build_volume: Dimensions,
        *,
        shared_scale: float,
        description: str,
        classification: list[SelectedCandidateFile],
    ) -> ModelArtifact:
        version = await self.repository.next_artifact_version(workflow_id)
        project, part_meshes, layout_mesh = await asyncio.to_thread(
            build_source_set_project,
            selected,
            shared_scale=shared_scale,
            max_layout_width=build_volume.width_mm,
            description=description,
        )
        workspace = (
            self.artifacts.workflow_root(workflow_id)
            / "staging"
            / f"source-set-{version}"
        )
        shutil.rmtree(workspace, ignore_errors=True)
        inspection_directory = workspace / "inspection"
        inspection_directory.mkdir(parents=True, exist_ok=False)
        try:
            part_reports: dict[str, MeshReport] = {}
            for part_id, part_mesh in part_meshes.items():
                part_path = inspection_directory / f"{part_id}.stl"
                await asyncio.to_thread(part_mesh.export, part_path)
                report = await self.mesh_inspector.inspect(part_path)
                if not report.watertight or report.volume_mm3 <= 0:
                    raise ValidationError(f"Source-set part '{part_id}' must be watertight")
                part_reports[part_id] = report
            layout_path = inspection_directory / "layout.stl"
            await asyncio.to_thread(layout_mesh.export, layout_path)
            mesh = await self.mesh_inspector.inspect(layout_path)
            if not mesh.dimensions.fits(build_volume):
                raise ValidationError("Source-set print layout exceeds the target build volume")

            staging = workspace / "artifact"
            manifest, files = await asyncio.to_thread(
                write_source_set_artifact,
                directory=staging,
                workflow_id=workflow_id,
                version=version,
                project=project,
                selected=selected,
                part_meshes=part_meshes,
                layout_mesh=layout_mesh,
                mesh=mesh,
                part_reports=part_reports,
                provenance=provenance,
                classification=classification,
            )
            adopted = self.artifacts.adopt_project(workflow_id, version, staging)
        finally:
            shutil.rmtree(workspace, ignore_errors=True)
        primary_part = project.parts[0].id
        source_path = adopted / "project" / "main.scad"
        artifact = ModelArtifact(
            schema_version="2",
            workflow_id=workflow_id,
            version=version,
            source_path=source_path,
            model_path=adopted / "outputs" / "parts" / f"{primary_part}.stl",
            project_path=adopted / "project" / "project.json",
            three_mf_path=adopted / "outputs" / "model.3mf",
            source_digest=sha256_file(source_path),
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
        workflow = await self.repository.get_workflow(workflow_id)
        if workflow.state == WorkflowState.VALIDATING:
            await self.repository.transition(
                workflow_id,
                WorkflowState.AWAITING_APPROVAL,
                event_kind="artifact.ready",
                payload={
                    "artifact_version": version,
                    "manifest_digest": artifact.manifest_digest,
                    "part_count": len(project.parts),
                    "instance_count": len(project.instances),
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
