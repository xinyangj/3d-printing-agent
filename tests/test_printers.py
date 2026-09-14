from __future__ import annotations

from pathlib import Path

import trimesh

from printing_agent.artifact_store import sha256_file
from printing_agent.domain import (
    ArtifactProvenance,
    Dimensions,
    MeshReport,
    ModelArtifact,
    PrinterCapabilitySummary,
    PrintSettings,
)
from printing_agent.printers import SimulatedPrinterAdapter, select_submission_file


async def test_simulated_printer_submission_is_idempotent(tmp_path: Path) -> None:
    model_path = tmp_path / "model.stl"
    trimesh.creation.box(extents=(10, 10, 10)).export(model_path)
    digest = sha256_file(model_path)
    artifact = ModelArtifact(
        workflow_id="workflow",
        version=1,
        model_path=model_path,
        model_digest=digest,
        manifest_digest="a" * 64,
        mesh=MeshReport(
            dimensions=Dimensions(width_mm=10, depth_mm=10, height_mm=10),
            triangle_count=12,
            connected_components=1,
            watertight=True,
            volume_mm3=1000,
        ),
        provenance=ArtifactProvenance(kind="generated"),
    )
    adapter = SimulatedPrinterAdapter(tmp_path / "spool")

    first = await adapter.submit(
        "workflow",
        artifact,
        PrintSettings(material="PLA"),
        "same-key",
    )
    second = await adapter.submit(
        "workflow",
        artifact,
        PrintSettings(material="PLA"),
        "same-key",
    )

    assert first.external_id == second.external_id
    assert len(list((tmp_path / "spool").glob("*/job.json"))) == 1


def test_submission_prefers_supported_multipart_3mf_and_falls_back_to_stl(
    tmp_path: Path,
) -> None:
    model_path = tmp_path / "model.stl"
    three_mf_path = tmp_path / "model.3mf"
    model_path.write_bytes(b"stl")
    three_mf_path.write_bytes(b"3mf")
    artifact = ModelArtifact(
        workflow_id="workflow",
        version=1,
        model_path=model_path,
        three_mf_path=three_mf_path,
        model_digest="a" * 64,
        three_mf_digest="b" * 64,
        manifest_digest="c" * 64,
        mesh=MeshReport(
            dimensions=Dimensions(width_mm=10, depth_mm=10, height_mm=10),
            triangle_count=12,
            connected_components=1,
            watertight=True,
            volume_mm3=1000,
        ),
        provenance=ArtifactProvenance(kind="generated"),
    )
    volume = Dimensions(width_mm=220, depth_mm=220, height_mm=250)

    assert select_submission_file(
        artifact,
        PrinterCapabilitySummary(
            name="3mf-printer",
            build_volume=volume,
            accepted_formats={"3mf", "stl"},
            supports_multipart_3mf=True,
        ),
    ) == three_mf_path
    assert select_submission_file(
        artifact,
        PrinterCapabilitySummary(
            name="stl-printer",
            build_volume=volume,
            accepted_formats={"3mf", "stl"},
            supports_multipart_3mf=False,
        ),
    ) == model_path
