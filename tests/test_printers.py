from __future__ import annotations

from pathlib import Path

import trimesh

from printing_agent.artifact_store import sha256_file
from printing_agent.domain import (
    ArtifactProvenance,
    Dimensions,
    MeshReport,
    ModelArtifact,
    PrintSettings,
)
from printing_agent.printers import SimulatedPrinterAdapter


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
