from __future__ import annotations

from pathlib import Path

import trimesh

from printing_agent.artifact_store import sha256_file
from printing_agent.domain import ArtifactProvenance, ModelArtifact
from printing_agent.modeling import MeshInspector, unwrap_base_model_source
from printing_agent.multipart import single_part_project, write_project_artifact
from printing_agent.verification import (
    RevisionVerifier,
    SemanticVerificationParams,
)


class PassingSemanticVerifier:
    async def verify(self, evidence: dict[str, object]) -> SemanticVerificationParams:
        del evidence
        return SemanticVerificationParams(
            verdict="passed",
            repairable=True,
            rationale="The requested change is represented by the packaged artifact.",
        )


async def _artifact(
    root: Path,
    *,
    workflow_id: str,
    version: int,
    extents: tuple[float, float, float],
    source_body: str,
) -> ModelArtifact:
    workspace = root / f"v{version}"
    source_path = root / f"source-{version}.scad"
    model_path = root / f"model-{version}.stl"
    canonical_source = (
        f"module base_model() {{\n{source_body}\n}}\nbase_model();\n"
    )
    source_path.write_text(canonical_source, encoding="utf-8")
    trimesh.creation.box(extents=extents).export(model_path)
    mesh = await MeshInspector().inspect(model_path)
    project = single_part_project(
        source_digest=sha256_file(source_path),
        imported=False,
        source_filename="main.scad",
    )
    manifest, files = write_project_artifact(
        directory=workspace,
        workflow_id=workflow_id,
        version=version,
        project=project,
        source_path=source_path,
        model_path=model_path,
        mesh=mesh,
        provenance=ArtifactProvenance(kind="generated"),
        handoff_digest="a" * 64,
        diagnostics=None,
        imported=False,
    )
    return ModelArtifact(
        schema_version="2",
        workflow_id=workflow_id,
        version=version,
        source_path=workspace / "project" / "main.scad",
        model_path=workspace / "outputs" / "model.stl",
        project_path=workspace / "project" / "project.json",
        three_mf_path=workspace / "outputs" / "model.3mf",
        source_digest=sha256_file(workspace / "project" / "main.scad"),
        model_digest=str(manifest["model_digest"]),
        project_digest=str(manifest["project_digest"]),
        three_mf_digest=str(manifest["three_mf_digest"]),
        manifest_digest=str(manifest["manifest_digest"]),
        mesh=mesh,
        project=project,
        part_meshes={"base_model": mesh},
        files=files,
        provenance=ArtifactProvenance(kind="generated"),
    )


async def test_color_preview_without_packaged_materials_fails_verification(
    tmp_path: Path,
) -> None:
    base = await _artifact(
        tmp_path,
        workflow_id="workflow",
        version=1,
        extents=(20, 10, 8),
        source_body="cube([20, 10, 8]);",
    )
    candidate = await _artifact(
        tmp_path,
        workflow_id="workflow",
        version=2,
        extents=(20, 10, 8),
        source_body=(
            "union() {\n"
            "  color([0.85, 0.02, 0.02]) cube([20, 10, 8]);\n"
            "  color([0.02, 0.20, 0.90]) translate([2, 2, 2]) cube(2);\n"
            "}"
        ),
    )
    verifier = RevisionVerifier(PassingSemanticVerifier())

    result = await verifier.verify(
        workflow_id="workflow",
        handoff_version=2,
        feedback="Make the body red and wheels blue",
        base=base,
        candidate=candidate,
        allowed_part_ids=None,
    )

    assert result.verdict == "failed"
    assert result.repairable is False
    assert {
        check.id for check in result.checks if not check.passed
    } >= {"artifact_changed", "material_representation"}


async def test_measurable_geometry_revision_passes_verification(
    tmp_path: Path,
) -> None:
    base = await _artifact(
        tmp_path,
        workflow_id="workflow",
        version=1,
        extents=(10, 10, 10),
        source_body="cube(10);",
    )
    candidate = await _artifact(
        tmp_path,
        workflow_id="workflow",
        version=2,
        extents=(15, 10, 10),
        source_body="cube([15, 10, 10]);",
    )
    verifier = RevisionVerifier(PassingSemanticVerifier())

    result = await verifier.verify(
        workflow_id="workflow",
        handoff_version=2,
        feedback="Make the model 5 mm wider",
        base=base,
        candidate=candidate,
        allowed_part_ids=None,
    )

    assert result.verdict == "passed"
    assert all(check.passed for check in result.checks)


def test_nested_base_model_source_is_unwrapped_to_one_body() -> None:
    nested = """
module base_model() {
  module base_model() {
    color("red") cube(10);
  }
  base_model();
}
base_model();
"""

    assert unwrap_base_model_source(nested) == 'color("red") cube(10);'
