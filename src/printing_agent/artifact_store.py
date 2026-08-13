from __future__ import annotations

import hashlib
import json
import os
import shutil
import zipfile
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

    def adopt_project(
        self,
        workflow_id: str,
        version: int,
        project_directory: Path,
    ) -> Path:
        destination = self.artifact_directory(workflow_id, version)
        if destination.exists():
            raise PolicyViolationError(f"Artifact version {version} already exists")
        if not (project_directory / "manifest.json").is_file():
            raise PolicyViolationError("Multipart artifact manifest is missing")
        temporary = destination.with_name(f".{destination.name}.tmp")
        try:
            shutil.copytree(project_directory, temporary)
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(temporary, destination)
            return destination
        except Exception:
            shutil.rmtree(temporary, ignore_errors=True)
            raise

    def resolve_artifact_file(
        self,
        workflow_id: str,
        version: int,
        filename: str,
    ) -> Path:
        artifact_directory = self.artifact_directory(workflow_id, version)
        manifest_path = artifact_directory / "manifest.json"
        manifest: dict[str, object] = {}
        if manifest_path.is_file():
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError, TypeError) as exc:
                raise PolicyViolationError("Artifact manifest is invalid") from exc
        aliases = {
            "source.scad": "project/main.scad",
            "model.stl": "outputs/model.stl",
            "model.3mf": "outputs/model.3mf",
        }
        requested = (
            aliases.get(filename, filename)
            if manifest.get("schema_version") == "2"
            else filename
        ).replace("\\", "/")
        if Path(requested).is_absolute() or ".." in Path(requested).parts:
            raise PolicyViolationError("Unsupported artifact filename")
        allowed = {"manifest.json", "source.scad", "model.stl"}
        files = manifest.get("files", [])
        if isinstance(files, list):
            allowed.update(
                item["path"]
                for item in files
                if isinstance(item, dict) and isinstance(item.get("path"), str)
            )
        if requested not in allowed and filename not in allowed:
            raise PolicyViolationError("Artifact file is not listed in the manifest")
        path = (artifact_directory / requested).resolve()
        if artifact_directory.resolve() not in path.parents or not path.is_file():
            raise PolicyViolationError("Artifact path is not available")
        return path

    def build_artifact_bundle(self, workflow_id: str, version: int) -> Path:
        artifact_directory = self.artifact_directory(workflow_id, version).resolve()
        manifest_path = artifact_directory / "manifest.json"
        if not manifest_path.is_file():
            raise PolicyViolationError("Artifact manifest is missing")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, TypeError) as exc:
            raise PolicyViolationError("Artifact manifest is invalid") from exc
        manifest_digest = str(manifest.get("manifest_digest") or sha256_file(manifest_path))
        bundle_directory = self.workflow_root(workflow_id) / "bundles"
        bundle_directory.mkdir(parents=True, exist_ok=True)
        destination = bundle_directory / f"artifact-v{version}-{manifest_digest[:12]}.zip"
        if destination.is_file():
            return destination

        listed_files = manifest.get("files", [])
        relative_paths = {"manifest.json"}
        if isinstance(listed_files, list):
            relative_paths.update(
                item["path"]
                for item in listed_files
                if isinstance(item, dict) and isinstance(item.get("path"), str)
            )
        if len(relative_paths) == 1:
            relative_paths.update(
                name
                for name in ("source.scad", "model.stl")
                if (artifact_directory / name).is_file()
            )
        entries: list[tuple[str, Path]] = []
        total_size = 0
        for relative in sorted(relative_paths):
            normalized = relative.replace("\\", "/")
            if Path(normalized).is_absolute() or ".." in Path(normalized).parts:
                raise PolicyViolationError("Artifact manifest contains an unsafe path")
            path = (artifact_directory / normalized).resolve()
            if artifact_directory not in path.parents or not path.is_file() or path.is_symlink():
                raise PolicyViolationError("Artifact package file is unavailable")
            total_size += path.stat().st_size
            if total_size > 500 * 1024 * 1024:
                raise PolicyViolationError("Artifact package exceeds the size limit")
            entries.append((normalized, path))

        project = manifest.get("project") if isinstance(manifest.get("project"), dict) else {}
        parts = project.get("parts", []) if isinstance(project, dict) else []
        instances = project.get("instances", []) if isinstance(project, dict) else []
        warnings = project.get("warnings", []) if isinstance(project, dict) else []
        quantities: dict[str, int] = {}
        for instance in instances if isinstance(instances, list) else []:
            if isinstance(instance, dict) and isinstance(instance.get("part_id"), str):
                part_id = instance["part_id"]
                quantities[part_id] = quantities.get(part_id, 0) + 1
        part_lines = [
            f"- {part.get('name', part.get('id', 'Part'))}: quantity "
            f"{quantities.get(str(part.get('id')), 1)}"
            for part in parts
            if isinstance(part, dict)
        ]
        readme = "\n".join(
            [
                f"3D Printing Agent artifact v{version}",
                "",
                "This package is bound to manifest digest:",
                manifest_digest,
                "",
                "Parts:",
                *(part_lines or ["- Historical single-file artifact"]),
                "",
                "Important:",
                "- Multipart transforms describe a separated print layout, not physical assembly.",
                *[
                    f"- {warning}"
                    for warning in warnings
                    if isinstance(warning, str)
                ],
                "",
                "Provenance:",
                json.dumps(manifest.get("provenance", {}), sort_keys=True),
                "",
            ]
        ).encode()
        index = {
            "artifact_version": version,
            "manifest_digest": manifest_digest,
            "files": [
                {
                    "path": relative,
                    "digest": sha256_file(path),
                    "size_bytes": path.stat().st_size,
                }
                for relative, path in entries
            ],
        }
        generated = {
            "README.txt": readme,
            "package-index.json": json.dumps(
                index,
                sort_keys=True,
                indent=2,
            ).encode(),
        }
        temporary = destination.with_suffix(".tmp")
        try:
            with zipfile.ZipFile(
                temporary,
                "w",
                compression=zipfile.ZIP_DEFLATED,
                compresslevel=9,
            ) as archive:
                for relative, path in entries:
                    self._write_zip_entry(archive, relative, path.read_bytes())
                for relative, data in sorted(generated.items()):
                    self._write_zip_entry(archive, relative, data)
            temporary.replace(destination)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        return destination

    @staticmethod
    def _write_zip_entry(
        archive: zipfile.ZipFile,
        relative_path: str,
        data: bytes,
    ) -> None:
        info = zipfile.ZipInfo(relative_path, date_time=(1980, 1, 1, 0, 0, 0))
        info.compress_type = zipfile.ZIP_DEFLATED
        info.create_system = 3
        info.external_attr = 0o100644 << 16
        archive.writestr(info, data)
