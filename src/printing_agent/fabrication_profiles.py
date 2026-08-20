from __future__ import annotations

import json
from pathlib import Path

from printing_agent.artifact_store import sha256_file
from printing_agent.domain import Dimensions
from printing_agent.errors import ValidationError
from printing_agent.fabrication import (
    MaterialSlotSpec,
    PlateSpec,
    PrinterProfileRevision,
    PrinterProfileSpec,
    ProfileOrigin,
    SlicerProfileSpec,
    SlotPolicy,
    ToolheadSpec,
)


def built_in_simulator_profile() -> PrinterProfileRevision:
    return PrinterProfileRevision(
        profile_id="simulator",
        revision=1,
        origin=ProfileOrigin.BUILT_IN,
        spec=PrinterProfileSpec(
            display_name="Simulator",
            manufacturer="3D Printing Agent",
            model="Simulator",
            build_volume=Dimensions(
                width_mm=220,
                depth_mm=220,
                height_mm=250,
            ),
            accepted_sliced_formats={"stl"},
            toolheads=[
                ToolheadSpec(
                    id="toolhead_1",
                    name="Simulated toolhead",
                    nozzle_diameter_mm=0.4,
                    nozzle_material="hardened_steel",
                    max_temperature_c=350,
                    supported_material_families={"pla", "petg", "abs", "tpu"},
                    hardened=True,
                )
            ],
            plates=[
                PlateSpec(
                    id="simulated_plate",
                    name="Simulated plate",
                    max_temperature_c=120,
                    supported_material_families={"pla", "petg", "abs", "tpu"},
                )
            ],
            material_slots=[
                MaterialSlotSpec(
                    id="external",
                    name="External spool",
                    system="external",
                    compatible_toolhead_ids={"toolhead_1"},
                    supported_material_families={"pla", "petg", "abs", "tpu"},
                )
            ],
            default_slot_policy=SlotPolicy(),
            supports_multipart_3mf=False,
            supports_color=False,
            supports_material_assignments=False,
            supported_material_families={"pla", "petg", "abs", "tpu"},
            slicer=SlicerProfileSpec(
                driver_id="simulator_passthrough",
                machine_profile_id="simulator",
                process_profile_id="simulator",
            ),
        ),
    ).with_digest()


def built_in_h2d_profile() -> PrinterProfileRevision:
    material_families = {
        "pla",
        "petg",
        "abs",
        "asa",
        "tpu",
        "pa",
        "pc",
        "pva",
        "support",
    }
    toolhead_ids = {"left", "right"}
    slots = [
        MaterialSlotSpec(
            id=f"ams1_{index}",
            name=f"AMS 1 slot {index}",
            system="ams",
            unit=1,
            tray=index,
            compatible_toolhead_ids=toolhead_ids,
            supported_material_families=material_families,
        )
        for index in range(1, 5)
    ]
    slots.extend(
        [
            MaterialSlotSpec(
                id="external_left",
                name="External spool — left toolhead",
                system="external",
                compatible_toolhead_ids={"left"},
                supported_material_families=material_families,
                manual_swap_required=True,
            ),
            MaterialSlotSpec(
                id="external_right",
                name="External spool — right toolhead",
                system="external",
                compatible_toolhead_ids={"right"},
                supported_material_families=material_families,
                manual_swap_required=True,
            ),
        ]
    )
    return PrinterProfileRevision(
        profile_id="bambu-h2d",
        revision=1,
        origin=ProfileOrigin.BUILT_IN,
        spec=PrinterProfileSpec(
            display_name="Bambu Lab H2D",
            manufacturer="Bambu Lab",
            model="H2D",
            build_volume=Dimensions(
                width_mm=350,
                depth_mm=320,
                height_mm=325,
            ),
            maximum_plate_count=1,
            toolheads=[
                ToolheadSpec(
                    id="left",
                    name="Left toolhead",
                    nozzle_diameter_mm=0.4,
                    nozzle_material="hardened_steel",
                    max_temperature_c=350,
                    supported_material_families=material_families,
                    hardened=True,
                ),
                ToolheadSpec(
                    id="right",
                    name="Right toolhead",
                    nozzle_diameter_mm=0.4,
                    nozzle_material="hardened_steel",
                    max_temperature_c=350,
                    supported_material_families=material_families,
                    hardened=True,
                ),
            ],
            plates=[
                PlateSpec(
                    id="textured_pei",
                    name="Textured PEI Plate",
                    slicer_value="Textured PEI Plate",
                    max_temperature_c=120,
                    supported_material_families=material_families,
                ),
                PlateSpec(
                    id="smooth_pei",
                    name="Smooth PEI Plate",
                    slicer_value="High Temp Plate",
                    max_temperature_c=120,
                    supported_material_families=material_families,
                ),
            ],
            material_slots=slots,
            default_slot_policy=SlotPolicy(),
            supported_material_families=material_families,
            slicer=SlicerProfileSpec(
                driver_id="bambu_studio_cli",
                machine_profile_id="Bambu Lab H2D 0.4 nozzle",
                process_profile_id="0.20mm Standard @BBL H2D",
            ),
            cloud_region="global",
        ),
    ).with_digest()


def built_in_profiles() -> list[PrinterProfileRevision]:
    return [built_in_simulator_profile(), built_in_h2d_profile()]


def resolve_slicer_profile_file_digests(
    profile: PrinterProfileRevision,
) -> dict[str, str]:
    if profile.spec.slicer.driver_id == "simulator_passthrough":
        return {}
    root_value = profile.spec.slicer.resource_root
    if root_value is None:
        return {}
    output: dict[str, str] = {}
    for category, value in (
        ("machine", profile.spec.slicer.machine_profile_id),
        ("process", profile.spec.slicer.process_profile_id),
    ):
        output.update(
            resolve_profile_dependency_digests(
                root_value,
                value,
                category,
            )
        )
    return output


def resolve_slicer_profile_path(
    root_value: str,
    profile_id: str,
    category: str,
) -> Path:
    return _resolve_slicer_profile_path(
        Path(root_value).resolve(),
        profile_id,
        category,
    )


def _resolve_slicer_profile_path(
    root: Path,
    profile_id: str,
    category: str,
    parent: Path | None = None,
) -> Path:
    filename = (
        profile_id
        if profile_id.casefold().endswith(".json")
        else f"{profile_id}.json"
    )
    local = parent / filename if parent is not None else None
    if local is not None and local.is_file():
        return local.resolve()
    candidates = list(root.rglob(filename))
    full_candidates = [
        candidate
        for candidate in candidates
        if candidate.parent.name.casefold() == f"{category}_full"
    ]
    if len(full_candidates) == 1:
        return full_candidates[0].resolve()
    if len(candidates) == 1:
        return candidates[0].resolve()
    raise ValidationError(
        f"Could not uniquely resolve {category} profile dependency '{profile_id}'"
    )


def resolve_slicer_profile_payload(
    root_value: str,
    profile_id: str,
    category: str,
) -> dict[str, object]:
    root = Path(root_value).resolve()
    cache: dict[Path, dict[str, object]] = {}
    active: set[Path] = set()

    def load(path: Path) -> dict[str, object]:
        resolved = path.resolve()
        if resolved in cache:
            return cache[resolved]
        if resolved in active:
            raise ValidationError(
                f"Cyclic {category} profile dependency at '{resolved.name}'"
            )
        active.add(resolved)
        try:
            payload = json.loads(resolved.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValidationError(
                f"Could not parse {category} profile dependency '{resolved.name}'"
            ) from exc
        if not isinstance(payload, dict):
            raise ValidationError(
                f"{category.title()} profile '{resolved.name}' is not an object"
            )
        if resolved.parent.name.casefold() == f"{category}_full":
            merged = dict(payload)
        else:
            merged: dict[str, object] = {}
            for key in ("inherits", "include"):
                dependencies = payload.get(key, [])
                values = (
                    [dependencies]
                    if isinstance(dependencies, str)
                    else dependencies
                )
                if not isinstance(values, list):
                    raise ValidationError(
                        f"{category.title()} profile '{resolved.name}' has "
                        f"invalid {key} dependencies"
                    )
                for dependency in values:
                    if not isinstance(dependency, str):
                        raise ValidationError(
                            f"{category.title()} profile '{resolved.name}' has "
                            f"an invalid {key} dependency"
                        )
                    dependency_path = _resolve_slicer_profile_path(
                        root,
                        dependency,
                        category,
                        resolved.parent,
                    )
                    merged.update(load(dependency_path))
            merged.update(payload)
            merged.pop("inherits", None)
            merged.pop("include", None)
        active.remove(resolved)
        cache[resolved] = merged
        return merged

    path = _resolve_slicer_profile_path(root, profile_id, category)
    return load(path)


def resolve_profile_dependency_digests(
    root_value: str,
    profile_id: str,
    category: str,
) -> dict[str, str]:
    root = Path(root_value).resolve()
    output: dict[str, str] = {}
    visited: set[Path] = set()

    def resolve(value: str, parent: Path | None = None) -> Path:
        return _resolve_slicer_profile_path(
            root,
            value,
            category,
            parent,
        )

    def visit(path: Path, category: str) -> None:
        resolved = path.resolve()
        if resolved in visited:
            return
        visited.add(resolved)
        try:
            relative = resolved.relative_to(root).as_posix()
        except ValueError:
            relative = resolved.name
        output[f"{category}:{relative}"] = sha256_file(resolved)
        if resolved.parent.name.casefold() == f"{category}_full":
            return
        try:
            payload = json.loads(resolved.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValidationError(
                f"Could not parse {category} profile dependency '{resolved.name}'"
            ) from exc
        for key in ("inherits", "include"):
            dependencies = payload.get(key, [])
            values = (
                [dependencies]
                if isinstance(dependencies, str)
                else dependencies
            )
            if not isinstance(values, list):
                continue
            for dependency in values:
                if not isinstance(dependency, str):
                    continue
                dependency_path = resolve(dependency, resolved.parent)
                visit(dependency_path, category)

    path = resolve(profile_id)
    visit(path, category)
    return output
