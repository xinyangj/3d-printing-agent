from __future__ import annotations

import difflib
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

import numpy as np
import trimesh
from copilot import CopilotClient, define_tool
from copilot.generated.rpc import PermissionDecisionApproveOnce, PermissionDecisionReject
from copilot.generated.session_events import PermissionRequest
from copilot.session import PermissionRequestResult
from copilot.tools import ToolResult
from pydantic import BaseModel, Field

from printing_agent.config import Settings
from printing_agent.domain import (
    ModelArtifact,
    RevisionVerification,
    RevisionVerificationCheck,
)
from printing_agent.errors import ExternalServiceError
from printing_agent.modeling import unwrap_base_model_source
from printing_agent.multipart import ThreeMFService

_COLOR_CALL = re.compile(
    r"\bcolor\s*\(\s*(?P<value>\[[^\]]+\]|\"[^\"]+\")",
    re.IGNORECASE,
)


class SemanticVerificationParams(BaseModel):
    verdict: Literal["passed", "failed"]
    repairable: bool
    reasons: list[str] = Field(default_factory=list, max_length=20)
    rationale: str = Field(min_length=1, max_length=4_000)


class SemanticVerifier(Protocol):
    async def verify(self, evidence: dict[str, Any]) -> SemanticVerificationParams: ...


@dataclass
class _SemanticState:
    result: SemanticVerificationParams | None = None


class CopilotRevisionVerifier:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def verify(self, evidence: dict[str, Any]) -> SemanticVerificationParams:
        state = _SemanticState()

        @define_tool(
            name="submit_revision_verification",
            description=(
                "Submit the semantic verdict for whether the provisional artifact satisfies "
                "the exact revision feedback using only the supplied evidence."
            ),
            defer="never",
        )
        async def submit_revision_verification(
            params: SemanticVerificationParams,
        ) -> ToolResult:
            state.result = params
            return ToolResult(text_result_for_llm="Revision verification accepted.")

        allowed = {submit_revision_verification.name}

        async def permission_handler(
            request: PermissionRequest,
            invocation: dict[str, str],
        ) -> PermissionRequestResult:
            del invocation
            if request.tool_name in allowed:
                return PermissionDecisionApproveOnce()
            return PermissionDecisionReject(
                feedback="Tool is unavailable to the verification role"
            )

        async def on_pre_tool_use(
            input_data: dict[str, object],
            invocation: object,
        ) -> dict[str, str]:
            del invocation
            if str(input_data.get("toolName", "")) in allowed:
                return {"permissionDecision": "allow"}
            return {
                "permissionDecision": "deny",
                "permissionDecisionReason": "Tool is unavailable to the verification role",
            }

        role_dir = self.settings.data_dir / "copilot-workspaces" / str(
            evidence["workflow_id"]
        ) / "verification"
        role_dir.mkdir(parents=True, exist_ok=True)
        async with CopilotClient(
            mode="empty",
            base_directory=str(self.settings.data_dir / "copilot"),
            working_directory=str(role_dir),
        ) as client:
            session = await client.create_session(
                on_permission_request=permission_handler,
                model=self.settings.copilot_model,
                tools=[submit_revision_verification],
                available_tools=sorted(allowed),
                system_message={
                    "mode": "replace",
                    "content": (
                        "You independently verify 3D-model revisions. Use only supplied evidence "
                        "and submit_revision_verification. Source preview colors do not prove that "
                        "STL or 3MF outputs retain colors. Fail requests whose requested semantic "
                        "change is absent from packaged geometry/material evidence. Mark a failure "
                        "repairable only when the current modeling tool can express the correction."
                    ),
                },
                hooks={"on_pre_tool_use": on_pre_tool_use},
                working_directory=str(role_dir),
                enable_session_store=False,
                enable_config_discovery=False,
                mcp_servers={},
            )
            try:
                prompt_evidence = self._bounded_prompt_evidence(evidence)
                await session.send_and_wait(
                    (
                        "Verify whether this provisional revision satisfies the exact feedback. "
                        "Mandatory deterministic failures cannot be overridden. Call the verdict "
                        "tool exactly once.\n\n"
                        f"{json.dumps(prompt_evidence, sort_keys=False, indent=2)}"
                    ),
                    timeout=300,
                )
            finally:
                await session.disconnect()
        if state.result is None:
            raise ExternalServiceError(
                "Revision verifier ended without submitting a verdict"
            )
        return state.result

    @staticmethod
    def _bounded_prompt_evidence(evidence: dict[str, Any]) -> dict[str, Any]:
        changed_source_diffs: dict[str, str] = {}
        remaining = 16_000
        for part_id, diff in evidence.get("source_diffs", {}).items():
            if not diff or remaining <= 0:
                continue
            value = str(diff)[: min(4_000, remaining)]
            changed_source_diffs[str(part_id)] = value
            remaining -= len(value)

        def bounded_mapping(name: str) -> dict[str, Any]:
            value = evidence.get(name, {})
            if not isinstance(value, dict):
                return {}
            return dict(list(value.items())[:100])

        def bounded_semantics(name: str) -> dict[str, Any]:
            value = evidence.get(name, {})
            if not isinstance(value, dict):
                return {}
            return {
                "parts": list(value.get("parts", []))[:100],
                "materials": list(value.get("materials", []))[:100],
                "instances": list(value.get("instances", []))[:100],
                "instance_count": len(value.get("instances", [])),
            }

        return {
            "workflow_id": evidence["workflow_id"],
            "feedback": evidence["feedback"],
            "base_artifact_version": evidence["base_artifact_version"],
            "candidate_artifact_version": evidence["candidate_artifact_version"],
            "deterministic_checks": [
                {
                    "id": check["id"],
                    "passed": check["passed"],
                    "mandatory": check["mandatory"],
                    "repairable": check["repairable"],
                    "message": check["message"],
                }
                for check in evidence.get("deterministic_checks", [])
            ],
            "source_diffs": changed_source_diffs,
            "geometry_changed": evidence["geometry_changed"],
            "base_part_dimensions": bounded_mapping("base_part_dimensions"),
            "candidate_part_dimensions": bounded_mapping(
                "candidate_part_dimensions"
            ),
            "base_part_geometry": bounded_mapping("base_part_geometry"),
            "candidate_part_geometry": bounded_mapping("candidate_part_geometry"),
            "changed_part_ids": sorted(
                part_id
                for part_id in set(evidence.get("base_part_geometry", {}))
                | set(evidence.get("candidate_part_geometry", {}))
                if evidence.get("base_part_geometry", {}).get(part_id)
                != evidence.get("candidate_part_geometry", {}).get(part_id)
            )[:100],
            "base_semantics": bounded_semantics("base_semantics"),
            "candidate_semantics": bounded_semantics("candidate_semantics"),
            "source_color_calls": list(evidence["source_color_calls"])[:100],
            "source_color_call_count": len(evidence["source_color_calls"]),
            "packaged_colors": evidence["packaged_colors"],
        }


class RevisionVerifier:
    def __init__(self, semantic_verifier: SemanticVerifier) -> None:
        self.semantic_verifier = semantic_verifier

    async def verify(
        self,
        *,
        workflow_id: str,
        handoff_version: int,
        feedback: str,
        base: ModelArtifact,
        candidate: ModelArtifact,
        allowed_part_ids: list[str] | None,
    ) -> RevisionVerification:
        evidence, checks = await self._build_evidence(
            workflow_id=workflow_id,
            feedback=feedback,
            base=base,
            candidate=candidate,
            allowed_part_ids=allowed_part_ids,
        )
        semantic = await self.semantic_verifier.verify(evidence)
        semantic_check = RevisionVerificationCheck(
            id="semantic_intent",
            passed=semantic.verdict == "passed",
            mandatory=True,
            repairable=semantic.repairable,
            message=(
                semantic.rationale
                if semantic.verdict == "passed"
                else "; ".join(semantic.reasons) or semantic.rationale
            )[:2_000],
            evidence={"reasons": semantic.reasons},
        )
        checks.append(semantic_check)
        failed = [check for check in checks if not check.passed]
        verdict: Literal["passed", "failed"] = "failed" if failed else "passed"
        return RevisionVerification(
            workflow_id=workflow_id,
            handoff_version=handoff_version,
            base_artifact_version=base.version,
            candidate_artifact_version=candidate.version,
            feedback=feedback,
            verdict=verdict,
            repairable=bool(failed) and all(check.repairable for check in failed),
            checks=checks,
            rationale=(
                semantic.rationale
                if not failed
                else "; ".join(check.message for check in failed)
            )[:4_000],
        )

    async def _build_evidence(
        self,
        *,
        workflow_id: str,
        feedback: str,
        base: ModelArtifact,
        candidate: ModelArtifact,
        allowed_part_ids: list[str] | None,
    ) -> tuple[dict[str, Any], list[RevisionVerificationCheck]]:
        base_sources = self._source_map(base)
        candidate_sources = self._source_map(candidate)
        source_diffs = self._source_diffs(
            base_sources,
            candidate_sources,
            base.version,
            candidate.version,
        )
        base_geometry = self._part_geometry_signatures(base)
        candidate_geometry = self._part_geometry_signatures(candidate)
        geometry_changed = base_geometry != candidate_geometry
        base_semantics = self._semantic_snapshot(base)
        candidate_semantics = self._semantic_snapshot(candidate)
        semantic_package_changed = base_semantics != candidate_semantics
        source_colors = sorted(
            {
                color
                for source in candidate_sources.values()
                for color in self._source_colors(source)
            }
        )
        packaged_colors = sorted(
            {
                material.color.casefold()
                for material in candidate.project.materials
            }
            if candidate.project is not None
            else set()
        )
        if candidate.three_mf_path is None:
            reopened_project = None
            reopened_meshes: dict[str, trimesh.Trimesh] = {}
        else:
            reopened_project, reopened_meshes, _ = ThreeMFService().read(
                candidate.three_mf_path
            )
        reopened_semantics = self._project_snapshot(reopened_project)
        reopened_geometry = {
            part_id: self._mesh_signature(mesh)
            for part_id, mesh in reopened_meshes.items()
        }
        package_consistent = (
            candidate_semantics == reopened_semantics
            and candidate_geometry == reopened_geometry
        )
        output_roles = {item.role for item in candidate.files}
        expected_roles = {
            "openscad_source",
            "part_project",
            "part_stl",
            "multipart_3mf",
        }
        checks = [
            RevisionVerificationCheck(
                id="artifact_changed",
                passed=geometry_changed or semantic_package_changed,
                repairable=True,
                message=(
                    "Packaged geometry or semantic material metadata changed"
                    if geometry_changed or semantic_package_changed
                    else "The packaged artifact is semantically unchanged from the base version"
                ),
                evidence={
                    "geometry_changed": geometry_changed,
                    "semantic_package_changed": semantic_package_changed,
                },
            ),
            RevisionVerificationCheck(
                id="package_consistency",
                passed=package_consistent,
                repairable=True,
                message=(
                    "Project metadata agrees with reopened 3MF structure"
                    if package_consistent
                    else (
                        "Project/STL geometry, transforms, or material metadata do not "
                        "match the reopened 3MF"
                    )
                ),
                evidence={
                    "project": candidate_semantics,
                    "reopened_3mf": reopened_semantics,
                    "part_geometry": candidate_geometry,
                    "reopened_3mf_geometry": reopened_geometry,
                },
            ),
            RevisionVerificationCheck(
                id="material_representation",
                passed=set(source_colors).issubset(packaged_colors),
                repairable=False,
                message=(
                    "Requested source colors are represented by packaged materials"
                    if set(source_colors).issubset(packaged_colors)
                    else (
                        "Distinct OpenSCAD preview colors are not represented in packaged "
                        "STL/3MF material assignments"
                    )
                ),
                evidence={
                    "source_color_calls": source_colors,
                    "packaged_colors": packaged_colors,
                    "part_count": len(candidate.project.parts)
                    if candidate.project is not None
                    else 0,
                },
            ),
            RevisionVerificationCheck(
                id="scope_preservation",
                passed=self._scope_preserved(base, candidate, allowed_part_ids),
                repairable=True,
                message=(
                    "Parts outside the explicit edit scope were preserved"
                    if self._scope_preserved(base, candidate, allowed_part_ids)
                    else "A part outside the explicit edit scope changed"
                ),
                evidence={"allowed_part_ids": allowed_part_ids},
            ),
            RevisionVerificationCheck(
                id="output_completeness",
                passed=expected_roles.issubset(output_roles),
                repairable=True,
                message=(
                    "Required CAD, STL, 3MF, and project outputs are present"
                    if expected_roles.issubset(output_roles)
                    else "One or more required revision outputs are missing"
                ),
                evidence={
                    "required_roles": sorted(expected_roles),
                    "actual_roles": sorted(output_roles),
                },
            ),
        ]
        evidence = {
            "workflow_id": workflow_id,
            "feedback": feedback,
            "base_artifact_version": base.version,
            "candidate_artifact_version": candidate.version,
            "base_dimensions": base.mesh.dimensions.model_dump(mode="json"),
            "candidate_dimensions": candidate.mesh.dimensions.model_dump(mode="json"),
            "source_diffs": source_diffs,
            "geometry_changed": geometry_changed,
            "base_part_geometry": base_geometry,
            "candidate_part_geometry": candidate_geometry,
            "base_part_dimensions": {
                part_id: report.dimensions.model_dump(mode="json")
                for part_id, report in base.part_meshes.items()
            },
            "candidate_part_dimensions": {
                part_id: report.dimensions.model_dump(mode="json")
                for part_id, report in candidate.part_meshes.items()
            },
            "base_semantics": base_semantics,
            "candidate_semantics": candidate_semantics,
            "source_color_calls": source_colors,
            "packaged_colors": packaged_colors,
            "deterministic_checks": [
                check.model_dump(mode="json") for check in checks
            ],
        }
        return evidence, checks

    @staticmethod
    def _source_map(artifact: ModelArtifact) -> dict[str, str]:
        root = (
            artifact.project_path.parent.parent
            if artifact.project_path is not None
            else artifact.model_path.parent
        )
        sources: dict[str, str] = {}
        for item in artifact.files:
            if item.role != "openscad_source":
                continue
            path = root / item.path
            if not path.is_file():
                continue
            key = (
                path.stem
                if item.path.startswith("project/parts/")
                else "base_model"
            )
            content = path.read_text(encoding="utf-8")
            sources[key] = (
                unwrap_base_model_source(content)
                if key == "base_model"
                else content.strip()
            )
        if (
            not sources
            and artifact.source_path is not None
            and artifact.source_path.is_file()
        ):
            sources["base_model"] = unwrap_base_model_source(
                artifact.source_path.read_text(encoding="utf-8")
            )
        return sources

    @staticmethod
    def _source_diffs(
        base_sources: dict[str, str],
        candidate_sources: dict[str, str],
        base_version: int,
        candidate_version: int,
    ) -> dict[str, str]:
        output: dict[str, str] = {}
        for part_id in sorted(set(base_sources) | set(candidate_sources)):
            output[part_id] = "\n".join(
                difflib.unified_diff(
                    base_sources.get(part_id, "").splitlines(),
                    candidate_sources.get(part_id, "").splitlines(),
                    fromfile=f"artifact-v{base_version}/{part_id}",
                    tofile=f"artifact-v{candidate_version}/{part_id}",
                    lineterm="",
                )
            )[:8_000]
        return output

    @staticmethod
    def _source_colors(source: str) -> set[str]:
        colors: set[str] = set()
        for value in _COLOR_CALL.findall(source):
            if value.startswith('"'):
                colors.add(value.strip('"').casefold())
                continue
            try:
                channels = json.loads(value)
            except json.JSONDecodeError:
                colors.add(value.casefold())
                continue
            if (
                isinstance(channels, list)
                and len(channels) >= 3
                and all(isinstance(channel, int | float) for channel in channels[:3])
            ):
                rgb = [
                    max(0, min(255, round(float(channel) * 255)))
                    for channel in channels[:3]
                ]
                colors.add(f"#{rgb[0]:02x}{rgb[1]:02x}{rgb[2]:02x}")
            else:
                colors.add(value.casefold())
        return colors

    @staticmethod
    def _geometry_signature(path: Path) -> str:
        mesh = trimesh.load_mesh(path, force="mesh")
        return RevisionVerifier._mesh_signature(mesh)

    @staticmethod
    def _mesh_signature(mesh: trimesh.Trimesh) -> str:
        vertices = np.round(np.asarray(mesh.vertices), 6)
        vertex_order = np.lexsort((vertices[:, 2], vertices[:, 1], vertices[:, 0]))
        triangles = np.round(np.asarray(mesh.triangles), 6)
        centroids = triangles.mean(axis=1)
        edge_lengths = np.sort(
            np.linalg.norm(triangles - np.roll(triangles, -1, axis=1), axis=2),
            axis=1,
        )
        descriptors = np.column_stack((centroids, edge_lengths))
        triangle_order = np.lexsort(tuple(descriptors[:, index] for index in range(5, -1, -1)))
        digest = hashlib.sha256()
        digest.update(vertices[vertex_order].tobytes())
        digest.update(descriptors[triangle_order].tobytes())
        return digest.hexdigest()

    @classmethod
    def _part_geometry_signatures(
        cls,
        artifact: ModelArtifact,
    ) -> dict[str, str]:
        root = (
            artifact.project_path.parent.parent
            if artifact.project_path is not None
            else artifact.model_path.parent
        )
        signatures = {
            item.part_id: cls._geometry_signature(root / item.path)
            for item in artifact.files
            if item.role == "part_stl" and item.part_id is not None
        }
        return signatures or {"base_model": cls._geometry_signature(artifact.model_path)}

    @classmethod
    def _semantic_snapshot(cls, artifact: ModelArtifact) -> dict[str, Any]:
        return cls._project_snapshot(artifact.project)

    @staticmethod
    def _project_snapshot(project: Any) -> dict[str, Any]:
        if project is None:
            return {"parts": [], "materials": [], "instances": []}
        material_colors = {material.id: material.color.casefold() for material in project.materials}
        return {
            "parts": sorted(
                (part.id, material_colors.get(part.material_id or ""))
                for part in project.parts
            ),
            "materials": sorted(material_colors.values()),
            "instances": sorted(
                (
                    instance.part_id,
                    tuple(round(float(value), 6) for value in instance.transform),
                )
                for instance in project.instances
            ),
        }

    @staticmethod
    def _scope_preserved(
        base: ModelArtifact,
        candidate: ModelArtifact,
        allowed_part_ids: list[str] | None,
    ) -> bool:
        if allowed_part_ids is None:
            return True
        allowed = set(allowed_part_ids)
        base_parts = {
            item.part_id: item.digest
            for item in base.files
            if item.role == "part_stl" and item.part_id is not None
        }
        candidate_parts = {
            item.part_id: item.digest
            for item in candidate.files
            if item.role == "part_stl" and item.part_id is not None
        }
        return all(
            candidate_parts.get(part_id) == digest
            for part_id, digest in base_parts.items()
            if part_id not in allowed
        )
