from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path
from uuid import UUID

from printing_agent.errors import PolicyViolationError


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class ArtifactStore:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def workflow_root(self, workflow_id: str) -> Path:
        UUID(workflow_id)
        return self.root / workflow_id

    def attempt_directory(
        self,
        workflow_id: str,
        handoff_version: int,
        attempt: int,
    ) -> Path:
        path = (
            self.workflow_root(workflow_id)
            / "attempts"
            / f"handoff-{handoff_version}"
            / f"attempt-{attempt}"
        )
        path.mkdir(parents=True, exist_ok=False)
        return path

    def artifact_directory(self, workflow_id: str, version: int) -> Path:
        return self.workflow_root(workflow_id) / "artifacts" / f"v{version}"

    def adopt(
        self,
        workflow_id: str,
        version: int,
        source: Path | None,
        model: Path,
        manifest: dict[str, object],
    ) -> tuple[Path | None, Path, Path]:
        destination = self.artifact_directory(workflow_id, version)
        if destination.exists():
            raise PolicyViolationError(f"Artifact version {version} already exists")
        temporary = destination.with_name(f".{destination.name}.tmp")
        temporary.mkdir(parents=True, exist_ok=False)
        try:
            adopted_source = None
            if source is not None:
                adopted_source = temporary / "source.scad"
                shutil.copy2(source, adopted_source)
            adopted_model = temporary / "model.stl"
            shutil.copy2(model, adopted_model)
            manifest_path = temporary / "manifest.json"
            manifest_path.write_text(
                json.dumps(manifest, sort_keys=True, indent=2),
                encoding="utf-8",
            )
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(temporary, destination)
            return (
                destination / "source.scad" if adopted_source else None,
                destination / "model.stl",
                destination / "manifest.json",
            )
        except Exception:
            shutil.rmtree(temporary, ignore_errors=True)
            raise

    def resolve_artifact_file(
        self,
        workflow_id: str,
        version: int,
        filename: str,
    ) -> Path:
        if filename not in {"source.scad", "model.stl", "manifest.json"}:
            raise PolicyViolationError("Unsupported artifact filename")
        path = (self.artifact_directory(workflow_id, version) / filename).resolve()
        if self.root not in path.parents or not path.is_file():
            raise PolicyViolationError("Artifact path is not available")
        return path
