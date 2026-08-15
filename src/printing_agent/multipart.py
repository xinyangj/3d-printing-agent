from __future__ import annotations

import hashlib
import json
import re
import shutil
from pathlib import Path

import lib3mf
import numpy as np
import trimesh
from lib3mf import Lib3MF

from printing_agent.artifact_store import sha256_file
from printing_agent.domain import (
    AnnotationOrigin,
    ArtifactFile,
    ArtifactProvenance,
    ArtifactRevision,
    CandidateFile,
    InterfaceEvidence,
    MaterialDefinition,
    MatingInterface,
    MeshReport,
    PartDefinition,
    PartGeometryKind,
    PartInstance,
    PartProject,
    SelectedCandidateFile,
    SelectedFileRole,
    SourceAsset,
    canonical_digest,
)
from printing_agent.errors import ValidationError


def _safe_id(value: str, fallback: str) -> str:
    normalized = re.sub(r"[^a-z0-9_-]+", "_", value.casefold()).strip("_")
    return (normalized or fallback)[:64]


def _module_name(part_id: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_]+", "_", part_id)
    if not normalized or normalized[0].isdigit():
        normalized = f"part_{normalized}"
    suffix = hashlib.sha256(part_id.encode()).hexdigest()[:8]
    return f"{normalized[:54]}_{suffix}"


def _safe_color(value: str) -> Lib3MF.Color:
    color = Lib3MF.Color()
    color.Red = int(value[1:3], 16)
    color.Green = int(value[3:5], 16)
    color.Blue = int(value[5:7], 16)
    color.Alpha = 255
    return color


def _mesh(path: Path) -> trimesh.Trimesh:
    loaded = trimesh.load(path, file_type="stl", force="mesh")
    if isinstance(loaded, trimesh.Scene):
        if not loaded.geometry:
            raise ValidationError(f"Part STL '{path.name}' contains no geometry")
        return trimesh.util.concatenate(tuple(loaded.geometry.values()))
    return loaded


def _layout_instances(
    parts: list[tuple[PartDefinition, int]],
    meshes: dict[str, trimesh.Trimesh],
    max_width: float,
    gap: float = 5,
) -> tuple[list[PartInstance], trimesh.Trimesh]:
    instances: list[PartInstance] = []
    placed: list[trimesh.Trimesh] = []
    cursor_x = 0.0
    cursor_y = 0.0
    row_depth = 0.0
    for part, quantity in parts:
        mesh = meshes[part.id]
        width, depth, _ = (float(value) for value in mesh.extents)
        for index in range(quantity):
            if cursor_x > 0 and cursor_x + width > max_width:
                cursor_x = 0
                cursor_y += row_depth + gap
                row_depth = 0
            translation = np.asarray(
                [
                    cursor_x - float(mesh.bounds[0][0]),
                    cursor_y - float(mesh.bounds[0][1]),
                    -float(mesh.bounds[0][2]),
                ]
            )
            matrix = np.eye(4)
            matrix[:3, 3] = translation
            instances.append(
                PartInstance(
                    id=f"{part.id}_{index + 1}",
                    part_id=part.id,
                    name=f"{part.name} {index + 1}" if quantity > 1 else part.name,
                    transform=tuple(float(value) for value in matrix.reshape(16)),
                    annotation_origin=AnnotationOrigin.CATALOG_METADATA,
                    confidence=part.confidence,
                )
            )
            positioned = mesh.copy()
            positioned.apply_transform(matrix)
            placed.append(positioned)
            cursor_x += width + gap
            row_depth = max(row_depth, depth)
    return instances, trimesh.util.concatenate(placed)


def repack_project_instances(
    project: PartProject,
    meshes: dict[str, trimesh.Trimesh],
    *,
    max_layout_width: float,
) -> tuple[PartProject, trimesh.Trimesh]:
    quantities = [
        (
            part,
            sum(instance.part_id == part.id for instance in project.instances),
        )
        for part in project.parts
    ]
    instances, layout = _layout_instances(
        quantities,
        meshes,
        max_width=max_layout_width,
    )
    return project.model_copy(update={"instances": instances}), layout


def _write_source_set_main(project_directory: Path, project: PartProject) -> None:
    module_names = {part.id: part.module_name for part in project.parts}
    if any(name is None for name in module_names.values()):
        raise ValidationError("Every source-set part requires an OpenSCAD module name")
    source_lines = [f"use <parts/{part.id}.scad>" for part in project.parts]
    source_lines.append("")
    for instance in project.instances:
        matrix = [
            list(instance.transform[index : index + 4])
            for index in range(0, 16, 4)
        ]
        source_lines.append(
            f"multmatrix({json.dumps(matrix)}) {module_names[instance.part_id]}();"
        )
    (project_directory / "main.scad").write_text(
        "\n".join(source_lines) + "\n",
        encoding="utf-8",
    )


def build_source_set_project(
    selected: list[tuple[SelectedCandidateFile, CandidateFile, Path, MeshReport]],
    *,
    shared_scale: float,
    max_layout_width: float,
    description: str,
) -> tuple[PartProject, dict[str, trimesh.Trimesh], trimesh.Trimesh]:
    parts: list[PartDefinition] = []
    materials: list[MaterialDefinition] = []
    assets: list[SourceAsset] = []
    meshes: dict[str, trimesh.Trimesh] = {}
    quantities: list[tuple[PartDefinition, int]] = []

    for selection, candidate_file, path, _ in selected:
        asset_id = _safe_id(f"source_{candidate_file.id}", "source")
        extension = candidate_file.format.casefold().lstrip(".")
        stored_filename = f"{asset_id}.{extension}"
        assets.append(
            SourceAsset(
                id=asset_id,
                filename=candidate_file.name,
                format=extension,
                digest=sha256_file(path),
                path=f"project/imports/{stored_filename}",
                role=selection.role.value,
                original_cad=False,
                annotation_origin=AnnotationOrigin.CATALOG_METADATA,
            )
        )
        if selection.role != SelectedFileRole.UNIQUE_PART:
            continue
        assert selection.part_id is not None and selection.part_name is not None
        material_id = f"material_{selection.part_id}"
        materials.append(
            MaterialDefinition(
                id=material_id,
                name=f"{selection.part_name} material",
                color=selection.color or "#b7c4d4",
            )
        )
        part = PartDefinition(
            id=selection.part_id,
            name=selection.part_name,
            geometry_kind=PartGeometryKind.IMPORTED_MESH,
            module_name=_module_name(selection.part_id),
            source_asset_id=asset_id,
            material_id=material_id,
            parameters={"shared_scale": shared_scale},
            annotation_origin=AnnotationOrigin.CATALOG_METADATA,
            confidence=selection.confidence,
        )
        parts.append(part)
        quantities.append((part, selection.quantity))
        mesh = _mesh(path).copy()
        mesh.apply_scale(shared_scale)
        meshes[part.id] = mesh

    if not parts:
        raise ValidationError("Source set contains no unique editable parts")
    instances, layout = _layout_instances(
        quantities,
        meshes,
        max_width=max_layout_width,
    )
    part_ids = {part.id for part in parts}
    interfaces: list[MatingInterface] = []
    normalized_description = description.casefold()
    if {"wheel", "axle"}.issubset(part_ids) and "wheel" in normalized_description:
        interfaces.append(
            MatingInterface(
                id="wheel_axle",
                part_ids=["wheel", "axle"],
                interface_type="shaft_bore",
                evidence=InterfaceEvidence.CANDIDATE,
                rationale=(
                    "The publisher states that wheels and axles are assembled, but "
                    "physical fit has not been dimensionally verified."
                ),
            )
        )
    body_id = "body" if "body" in part_ids else "frame" if "frame" in part_ids else None
    if body_id and "axle" in part_ids and "axle" in normalized_description:
        interfaces.append(
            MatingInterface(
                id=f"{body_id}_axle",
                part_ids=[body_id, "axle"],
                interface_type="shaft_channel",
                evidence=InterfaceEvidence.CANDIDATE,
                rationale=(
                    "The publisher states that the axles are assembled with the frame, "
                    "but the mating channel has not been verified."
                ),
            )
        )
    project = PartProject(
        parts=parts,
        instances=instances,
        materials=materials,
        source_assets=assets,
        assembly_status="not_provided",
        interfaces=interfaces,
        warnings=[
            "Separated parts / print layout—not assembled.",
            "Physical fit has not been verified for candidate or unknown interfaces.",
        ],
    )
    return project, meshes, layout


def write_source_set_artifact(
    *,
    directory: Path,
    workflow_id: str,
    version: int,
    project: PartProject,
    selected: list[tuple[SelectedCandidateFile, CandidateFile, Path, MeshReport]],
    part_meshes: dict[str, trimesh.Trimesh],
    layout_mesh: trimesh.Trimesh,
    mesh: MeshReport,
    part_reports: dict[str, MeshReport],
    provenance: ArtifactProvenance,
    classification: list[SelectedCandidateFile],
) -> tuple[dict[str, object], list[ArtifactFile]]:
    project_directory = directory / "project"
    imports_directory = project_directory / "imports"
    sources_directory = project_directory / "parts"
    outputs_directory = directory / "outputs"
    parts_directory = outputs_directory / "parts"
    imports_directory.mkdir(parents=True, exist_ok=True)
    sources_directory.mkdir(parents=True, exist_ok=True)
    parts_directory.mkdir(parents=True, exist_ok=True)

    selected_by_id = {
        selection.file_id: (selection, file, path)
        for selection, file, path, _ in selected
    }
    for asset in project.source_assets:
        selection, _, source = selected_by_id[asset.id.removeprefix("source_")]
        stored_filename = Path(asset.path).name
        destination = imports_directory / stored_filename
        shutil.copy2(source, destination)
        if selection.role != SelectedFileRole.UNIQUE_PART:
            continue
        assert selection.part_id is not None
        scale = next(
            part.parameters["shared_scale"]
            for part in project.parts
            if part.id == selection.part_id
        )
        part_source = sources_directory / f"{selection.part_id}.scad"
        module_name = next(
            part.module_name for part in project.parts if part.id == selection.part_id
        )
        assert module_name is not None
        part_source.write_text(
            f"module {module_name}() {{\n"
            f'  scale([{scale}, {scale}, {scale}]) '
            f'import("../imports/{stored_filename}");\n'
            "}\n",
            encoding="utf-8",
        )
        part_meshes[selection.part_id].export(
            parts_directory / f"{selection.part_id}.stl"
        )
    _write_source_set_main(project_directory, project)
    project_path = project_directory / "project.json"
    project_path.write_text(
        json.dumps(project.model_dump(mode="json"), sort_keys=True, indent=2),
        encoding="utf-8",
    )
    three_mf_path = outputs_directory / "model.3mf"
    ThreeMFService().write(
        project,
        {part.id: parts_directory / f"{part.id}.stl" for part in project.parts},
        three_mf_path,
    )

    combined_selection = next(
        (
            (candidate_file, path)
            for selection, candidate_file, path, _ in selected
            if selection.role == SelectedFileRole.COMBINED_MODEL
        ),
        None,
    )
    publisher_combined_path = None
    if combined_selection is not None:
        _, combined_source = combined_selection
        publisher_combined_path = outputs_directory / "publisher-combined.stl"
        shutil.copy2(combined_source, publisher_combined_path)

    files: list[ArtifactFile] = []
    for path in sorted(item for item in directory.rglob("*") if item.is_file()):
        relative = path.relative_to(directory).as_posix()
        role = (
            "part_project"
            if relative == "project/project.json"
            else "openscad_source"
            if path.suffix == ".scad"
            else "multipart_3mf"
            if relative == "outputs/model.3mf"
            else "part_stl"
            if relative.startswith("outputs/parts/")
            else "publisher_combined_stl"
            if relative == "outputs/publisher-combined.stl"
            else "source_asset"
        )
        media_type = (
            "application/json"
            if path.suffix == ".json"
            else "text/plain; charset=utf-8"
            if path.suffix == ".scad"
            else "model/3mf"
            if path.suffix == ".3mf"
            else "model/stl"
        )
        files.append(
            ArtifactFile(
                role=role,
                path=relative,
                media_type=media_type,
                digest=sha256_file(path),
                size_bytes=path.stat().st_size,
                part_id=path.stem if role == "part_stl" else None,
            )
        )
    primary_path = parts_directory / f"{project.parts[0].id}.stl"
    manifest: dict[str, object] = {
        "schema_version": "2",
        "workflow_id": workflow_id,
        "artifact_version": version,
        "project": project.model_dump(mode="json"),
        "project_digest": sha256_file(project_path),
        "model_digest": sha256_file(primary_path),
        "three_mf_digest": sha256_file(three_mf_path),
        "mesh": mesh.model_dump(mode="json"),
        "part_meshes": {
            part_id: report.model_dump(mode="json")
            for part_id, report in part_reports.items()
        },
        "provenance": provenance.model_dump(mode="json"),
        "source_set_classification": [
            item.model_dump(mode="json") for item in classification
        ],
        "files": [item.model_dump(mode="json") for item in files],
    }
    manifest["manifest_digest"] = canonical_digest(manifest)
    (directory / "manifest.json").write_text(
        json.dumps(manifest, sort_keys=True, indent=2),
        encoding="utf-8",
    )
    return manifest, files


def rebuild_part_project_artifact(
    *,
    directory: Path,
    workflow_id: str,
    version: int,
    project: PartProject,
    mesh: MeshReport,
    part_reports: dict[str, MeshReport],
    provenance: ArtifactProvenance,
    classification: list[dict[str, object]],
    handoff_digest: str,
    revision: ArtifactRevision,
) -> tuple[dict[str, object], list[ArtifactFile]]:
    _write_source_set_main(directory / "project", project)
    project_path = directory / "project" / "project.json"
    project_path.write_text(
        json.dumps(project.model_dump(mode="json"), sort_keys=True, indent=2),
        encoding="utf-8",
    )
    part_paths = {
        part.id: directory / "outputs" / "parts" / f"{part.id}.stl"
        for part in project.parts
    }
    three_mf_path = directory / "outputs" / "model.3mf"
    ThreeMFService().write(project, part_paths, three_mf_path)
    (directory / "manifest.json").unlink(missing_ok=True)
    files: list[ArtifactFile] = []
    for path in sorted(item for item in directory.rglob("*") if item.is_file()):
        relative = path.relative_to(directory).as_posix()
        role = (
            "part_project"
            if relative == "project/project.json"
            else "openscad_source"
            if path.suffix == ".scad"
            else "multipart_3mf"
            if relative == "outputs/model.3mf"
            else "part_stl"
            if relative.startswith("outputs/parts/")
            else "publisher_combined_stl"
            if relative == "outputs/publisher-combined.stl"
            else "source_asset"
        )
        media_type = (
            "application/json"
            if path.suffix == ".json"
            else "text/plain; charset=utf-8"
            if path.suffix == ".scad"
            else "model/3mf"
            if path.suffix == ".3mf"
            else "model/stl"
        )
        files.append(
            ArtifactFile(
                role=role,
                path=relative,
                media_type=media_type,
                digest=sha256_file(path),
                size_bytes=path.stat().st_size,
                part_id=path.stem if role == "part_stl" else None,
            )
        )
    primary_path = part_paths[project.parts[0].id]
    manifest: dict[str, object] = {
        "schema_version": "2",
        "workflow_id": workflow_id,
        "artifact_version": version,
        "handoff_digest": handoff_digest,
        "project": project.model_dump(mode="json"),
        "project_digest": sha256_file(project_path),
        "model_digest": sha256_file(primary_path),
        "three_mf_digest": sha256_file(three_mf_path),
        "mesh": mesh.model_dump(mode="json"),
        "part_meshes": {
            part_id: report.model_dump(mode="json")
            for part_id, report in part_reports.items()
        },
        "provenance": provenance.model_dump(mode="json"),
        "revision": revision.model_dump(mode="json"),
        "source_set_classification": classification,
        "files": [item.model_dump(mode="json") for item in files],
    }
    manifest["manifest_digest"] = canonical_digest(manifest)
    (directory / "manifest.json").write_text(
        json.dumps(manifest, sort_keys=True, indent=2),
        encoding="utf-8",
    )
    return manifest, files


class ThreeMFService:
    def write(
        self,
        project: PartProject,
        part_paths: dict[str, Path],
        output_path: Path,
    ) -> None:
        wrapper = lib3mf.get_wrapper()
        model = wrapper.CreateModel()
        model.SetUnit(Lib3MF.ModelUnit.MilliMeter)
        resources: dict[str, object] = {}
        materials = {material.id: material for material in project.materials}
        color_groups: dict[str, tuple[object, int]] = {}

        for part in project.parts:
            mesh = _mesh(part_paths[part.id])
            vertices = []
            for values in mesh.vertices:
                vertex = Lib3MF.Position()
                vertex.Coordinates[:] = [float(value) for value in values]
                vertices.append(vertex)
            triangles = []
            for values in mesh.faces:
                triangle = Lib3MF.Triangle()
                triangle.Indices[:] = [int(value) for value in values]
                triangles.append(triangle)
            resource = model.AddMeshObject()
            resource.SetName(part.name)
            resource.SetPartNumber(part.id)
            resource.SetType(Lib3MF.ObjectType.Model)
            resource.SetGeometry(vertices, triangles)
            if part.material_id:
                material = materials[part.material_id]
                if material.id not in color_groups:
                    group = model.AddColorGroup()
                    property_id = group.AddColor(_safe_color(material.color))
                    color_groups[material.id] = (group, property_id)
                group, property_id = color_groups[material.id]
                resource.SetObjectLevelProperty(group.GetUniqueResourceID(), property_id)
            resources[part.id] = resource

        for instance in project.instances:
            transform = wrapper.GetIdentityTransform()
            values = instance.transform
            for column in range(4):
                for row in range(3):
                    transform.Fields[column][row] = values[row * 4 + column]
            model.AddBuildItem(resources[instance.part_id], transform)

        output_path.parent.mkdir(parents=True, exist_ok=True)
        model.QueryWriter("3mf").WriteToFile(str(output_path))
        self.validate(output_path, project)

    @staticmethod
    def validate(path: Path, project: PartProject) -> None:
        wrapper = lib3mf.get_wrapper()
        model = wrapper.CreateModel()
        model.QueryReader("3mf").ReadFromFile(str(path))
        objects = model.GetMeshObjects()
        names: set[str] = set()
        count = 0
        while objects.MoveNext():
            current = objects.GetCurrentMeshObject()
            names.add(current.GetName())
            count += 1
        build_items = model.GetBuildItems()
        if (
            count != len(project.parts)
            or names != {part.name for part in project.parts}
            or build_items.Count() != len(project.instances)
        ):
            raise ValidationError("Generated 3MF structure does not match the part project")

    def read(
        self,
        path: Path,
    ) -> tuple[PartProject, dict[str, trimesh.Trimesh], trimesh.Trimesh]:
        wrapper = lib3mf.get_wrapper()
        model = wrapper.CreateModel()
        model.QueryReader("3mf").ReadFromFile(str(path))

        color_values: dict[tuple[int, int], str] = {}
        color_groups = model.GetColorGroups()
        while color_groups.MoveNext():
            group = color_groups.GetCurrent()
            resource_id = group.GetUniqueResourceID()
            for property_id in group.GetAllPropertyIDs():
                color = group.GetColor(property_id)
                color_values[(resource_id, property_id)] = (
                    f"#{color.Red:02x}{color.Green:02x}{color.Blue:02x}"
                )

        parts: list[PartDefinition] = []
        materials: list[MaterialDefinition] = []
        part_meshes: dict[str, trimesh.Trimesh] = {}
        resource_parts: dict[int, str] = {}
        used_ids: set[str] = set()
        total_triangles = 0
        mesh_objects = model.GetMeshObjects()
        while mesh_objects.MoveNext():
            if len(parts) >= 500:
                raise ValidationError("3MF exceeds the 500-part safety limit")
            resource = mesh_objects.GetCurrentMeshObject()
            label = resource.GetName() or resource.GetPartNumber()
            base_id = _safe_id(resource.GetPartNumber() or label, "part")
            part_id = base_id
            suffix = 2
            while part_id in used_ids:
                part_id = f"{base_id[:60]}_{suffix}"
                suffix += 1
            used_ids.add(part_id)
            vertices = [
                [float(value) for value in vertex.Coordinates]
                for vertex in resource.GetVertices()
            ]
            faces = [
                [int(value) for value in triangle.Indices]
                for triangle in resource.GetTriangleIndices()
            ]
            total_triangles += len(faces)
            if total_triangles > 2_000_000:
                raise ValidationError("3MF exceeds the triangle safety limit")
            if not np.isfinite(np.asarray(vertices)).all():
                raise ValidationError("3MF contains non-finite coordinates")
            mesh = trimesh.Trimesh(vertices=vertices, faces=faces, process=False)
            if len(mesh.faces) == 0:
                raise ValidationError("3MF contains an empty mesh object")
            part_meshes[part_id] = mesh
            resource_parts[resource.GetResourceID()] = part_id
            resource_parts[resource.GetModelResourceID()] = part_id
            material_id = None
            property_resource, property_id, has_property = resource.GetObjectLevelProperty()
            color = color_values.get((property_resource, property_id)) if has_property else None
            if color:
                material_id = f"material_{part_id}"
                materials.append(
                    MaterialDefinition(
                        id=material_id,
                        name=f"{label or part_id} material",
                        color=color,
                    )
                )
            parts.append(
                PartDefinition(
                    id=part_id,
                    name=label or f"Part {len(parts) + 1}",
                    geometry_kind=PartGeometryKind.IMPORTED_MESH,
                    source_asset_id="source_3mf",
                    material_id=material_id,
                    annotation_origin=AnnotationOrigin.THREE_MF_STRUCTURE,
                    confidence=1 if label else 0.6,
                )
            )

        if not parts:
            raise ValidationError("3MF contains no mesh objects")

        instances: list[PartInstance] = []
        transformed_meshes: list[trimesh.Trimesh] = []

        def matrix_from_transform(transform: Lib3MF.Transform) -> np.ndarray:
            return np.asarray(
                [
                    [float(transform.Fields[column][row]) for column in range(4)]
                    for row in range(3)
                ]
                + [[0.0, 0.0, 0.0, 1.0]]
            )

        def add_instance(part_id: str, matrix: np.ndarray) -> None:
            flattened = tuple(float(value) for value in matrix.reshape(16))
            instances.append(
                PartInstance(
                    id=f"{part_id}_{len(instances) + 1}",
                    part_id=part_id,
                    name=next(part.name for part in parts if part.id == part_id),
                    transform=flattened,
                    annotation_origin=AnnotationOrigin.THREE_MF_STRUCTURE,
                    confidence=1,
                )
            )
            transformed = part_meshes[part_id].copy()
            transformed.apply_transform(matrix)
            transformed_meshes.append(transformed)

        def expand_resource(resource_id: int, parent: np.ndarray) -> None:
            part_id = resource_parts.get(resource_id)
            if part_id is not None:
                add_instance(part_id, parent)
                return
            try:
                components = model.GetComponentsObjectByID(resource_id)
            except Lib3MF.ELib3MFException:
                return
            for index in range(components.GetComponentCount()):
                component = components.GetComponent(index)
                child_transform = (
                    matrix_from_transform(component.GetTransform())
                    if component.HasTransform()
                    else np.eye(4)
                )
                expand_resource(component.GetObjectResourceID(), parent @ child_transform)

        build_items = model.GetBuildItems()
        while build_items.MoveNext():
            item = build_items.GetCurrent()
            build_transform = (
                matrix_from_transform(item.GetObjectTransform())
                if item.HasObjectTransform()
                else np.eye(4)
            )
            expand_resource(item.GetObjectResourceID(), build_transform)

        warnings: list[str] = []
        if not instances:
            warnings.append(
                "The 3MF build did not directly reference mesh objects; mesh resources "
                "were retained as identity instances."
            )
            for part in parts:
                instances.append(
                    PartInstance(
                        id=f"{part.id}_1",
                        part_id=part.id,
                        name=part.name,
                        annotation_origin=AnnotationOrigin.THREE_MF_STRUCTURE,
                        confidence=0.6,
                    )
                )
                transformed_meshes.append(part_meshes[part.id].copy())

        source = SourceAsset(
            id="source_3mf",
            filename=path.name,
            format="3mf",
            digest=sha256_file(path),
            path="project/imports/source.3mf",
            role="structured_3mf",
            original_cad=False,
            annotation_origin=AnnotationOrigin.THREE_MF_STRUCTURE,
        )
        project = PartProject(
            parts=parts,
            instances=instances,
            materials=materials,
            source_assets=[source],
            warnings=warnings,
        )
        return project, part_meshes, trimesh.util.concatenate(transformed_meshes)


def write_structured_import_artifact(
    *,
    directory: Path,
    workflow_id: str,
    version: int,
    project: PartProject,
    source_path: Path,
    part_meshes: dict[str, trimesh.Trimesh],
    combined_mesh: trimesh.Trimesh,
    mesh: MeshReport,
    part_reports: dict[str, MeshReport],
    provenance: ArtifactProvenance,
) -> tuple[dict[str, object], list[ArtifactFile]]:
    project_directory = directory / "project"
    imports_directory = project_directory / "imports"
    outputs_directory = directory / "outputs"
    parts_directory = outputs_directory / "parts"
    imports_directory.mkdir(parents=True, exist_ok=True)
    parts_directory.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_path, imports_directory / "source.3mf")

    source_lines = []
    for part in project.parts:
        import_name = f"{part.id}.stl"
        import_path = imports_directory / import_name
        part_meshes[part.id].export(import_path)
        part_meshes[part.id].export(parts_directory / import_name)
        source_lines.append(
            f'module {part.id}() {{ import("imports/{import_name}"); }}'
        )
    source_lines.append("")
    for instance in project.instances:
        matrix = [
            list(instance.transform[index : index + 4])
            for index in range(0, 16, 4)
        ]
        source_lines.append(
            f"multmatrix({json.dumps(matrix)}) {instance.part_id}();"
        )
    (project_directory / "main.scad").write_text(
        "\n".join(source_lines) + "\n",
        encoding="utf-8",
    )
    project_path = project_directory / "project.json"
    project_path.write_text(
        json.dumps(project.model_dump(mode="json"), sort_keys=True, indent=2),
        encoding="utf-8",
    )
    combined_path = outputs_directory / "model.stl"
    combined_mesh.export(combined_path)
    three_mf_path = outputs_directory / "model.3mf"
    ThreeMFService().write(
        project,
        {part.id: parts_directory / f"{part.id}.stl" for part in project.parts},
        three_mf_path,
    )

    files: list[ArtifactFile] = []
    for path in sorted(item for item in directory.rglob("*") if item.is_file()):
        relative = path.relative_to(directory).as_posix()
        if relative == "manifest.json":
            continue
        role = (
            "part_project"
            if relative == "project/project.json"
            else "openscad_source"
            if relative == "project/main.scad"
            else "combined_stl"
            if relative == "outputs/model.stl"
            else "multipart_3mf"
            if relative == "outputs/model.3mf"
            else "part_stl"
            if relative.startswith("outputs/parts/")
            else "source_asset"
        )
        media_type = (
            "application/json"
            if path.suffix == ".json"
            else "text/plain; charset=utf-8"
            if path.suffix == ".scad"
            else "model/3mf"
            if path.suffix == ".3mf"
            else "model/stl"
        )
        part_id = path.stem if role == "part_stl" else None
        files.append(
            ArtifactFile(
                role=role,
                path=relative,
                media_type=media_type,
                digest=sha256_file(path),
                size_bytes=path.stat().st_size,
                part_id=part_id,
            )
        )
    manifest: dict[str, object] = {
        "schema_version": "2",
        "workflow_id": workflow_id,
        "artifact_version": version,
        "handoff_digest": None,
        "project": project.model_dump(mode="json"),
        "project_digest": sha256_file(project_path),
        "model_digest": sha256_file(combined_path),
        "three_mf_digest": sha256_file(three_mf_path),
        "mesh": mesh.model_dump(mode="json"),
        "part_meshes": {
            part_id: report.model_dump(mode="json")
            for part_id, report in part_reports.items()
        },
        "provenance": provenance.model_dump(mode="json"),
        "files": [item.model_dump(mode="json") for item in files],
    }
    manifest["manifest_digest"] = canonical_digest(manifest)
    (directory / "manifest.json").write_text(
        json.dumps(manifest, sort_keys=True, indent=2),
        encoding="utf-8",
    )
    return manifest, files


def single_part_project(
    *,
    source_digest: str,
    imported: bool,
    source_filename: str,
) -> PartProject:
    origin = (
        AnnotationOrigin.CATALOG_METADATA
        if imported
        else AnnotationOrigin.AGENT_INFERENCE
    )
    asset = SourceAsset(
        id="source",
        filename=source_filename,
        format=Path(source_filename).suffix.lstrip(".").casefold() or "scad",
        digest=source_digest,
        path="project/imports/source.stl" if imported else "project/main.scad",
        role="imported_base_mesh" if imported else "editable_openscad",
        original_cad=not imported,
        annotation_origin=origin,
    )
    return PartProject(
        parts=[
            PartDefinition(
                id="base_model",
                name="Base model",
                geometry_kind=(
                    PartGeometryKind.IMPORTED_MESH
                    if imported
                    else PartGeometryKind.PARAMETRIC
                ),
                module_name="base_model",
                source_asset_id=asset.id,
                material_id="default",
                annotation_origin=origin,
                confidence=0.35 if imported else 1,
            )
        ],
        instances=[
            PartInstance(
                id="base_model_1",
                part_id="base_model",
                name="Base model",
                annotation_origin=origin,
                confidence=0.35 if imported else 1,
            )
        ],
        materials=[MaterialDefinition(id="default", name="Default")],
        source_assets=[asset],
        warnings=(
            [
                "The imported mesh has no authoritative part annotations; "
                "it is retained as one editable base part."
            ]
            if imported
            else []
        ),
    )


def write_project_artifact(
    *,
    directory: Path,
    workflow_id: str,
    version: int,
    project: PartProject,
    source_path: Path,
    model_path: Path,
    mesh: MeshReport,
    provenance: ArtifactProvenance,
    handoff_digest: str | None,
    diagnostics: str | None,
    imported: bool,
) -> tuple[dict[str, object], list[ArtifactFile]]:
    project_directory = directory / "project"
    outputs_directory = directory / "outputs"
    parts_directory = outputs_directory / "parts"
    project_directory.mkdir(parents=True, exist_ok=True)
    parts_directory.mkdir(parents=True, exist_ok=True)

    main_scad = project_directory / "main.scad"
    if imported:
        imports = project_directory / "imports"
        imports.mkdir()
        shutil.copy2(source_path, imports / "source.stl")
        main_scad.write_text(
            'module base_model() { import("imports/source.stl"); }\nbase_model();\n',
            encoding="utf-8",
        )
    else:
        shutil.copy2(source_path, main_scad)

    project_path = project_directory / "project.json"
    project_path.write_text(
        json.dumps(project.model_dump(mode="json"), sort_keys=True, indent=2),
        encoding="utf-8",
    )
    combined_path = outputs_directory / "model.stl"
    part_path = parts_directory / "base_model.stl"
    shutil.copy2(model_path, combined_path)
    shutil.copy2(model_path, part_path)
    three_mf_path = outputs_directory / "model.3mf"
    ThreeMFService().write(project, {"base_model": part_path}, three_mf_path)

    media_types = {
        "project/project.json": "application/json",
        "project/main.scad": "text/plain; charset=utf-8",
        "project/imports/source.stl": "model/stl",
        "outputs/model.stl": "model/stl",
        "outputs/model.3mf": "model/3mf",
        "outputs/parts/base_model.stl": "model/stl",
    }
    roles = {
        "project/project.json": "part_project",
        "project/main.scad": "openscad_source",
        "project/imports/source.stl": "source_asset",
        "outputs/model.stl": "combined_stl",
        "outputs/model.3mf": "multipart_3mf",
        "outputs/parts/base_model.stl": "part_stl",
    }
    files = []
    for relative_path, media_type in media_types.items():
        path = directory / relative_path
        if not path.is_file():
            continue
        files.append(
            ArtifactFile(
                role=roles[relative_path],
                path=relative_path,
                media_type=media_type,
                digest=sha256_file(path),
                size_bytes=path.stat().st_size,
                part_id="base_model" if roles[relative_path] == "part_stl" else None,
            )
        )
    manifest: dict[str, object] = {
        "schema_version": "2",
        "workflow_id": workflow_id,
        "artifact_version": version,
        "handoff_digest": handoff_digest,
        "project": project.model_dump(mode="json"),
        "project_digest": sha256_file(project_path),
        "model_digest": sha256_file(combined_path),
        "three_mf_digest": sha256_file(three_mf_path),
        "mesh": mesh.model_dump(mode="json"),
        "part_meshes": {"base_model": mesh.model_dump(mode="json")},
        "provenance": provenance.model_dump(mode="json"),
        "files": [item.model_dump(mode="json") for item in files],
    }
    if diagnostics:
        manifest["renderer_diagnostics"] = diagnostics[-2_000:]
    manifest["manifest_digest"] = canonical_digest(manifest)
    (directory / "manifest.json").write_text(
        json.dumps(manifest, sort_keys=True, indent=2),
        encoding="utf-8",
    )
    return manifest, files
