from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from printing_agent.cloud_inventory import CloudDeviceSnapshot
from printing_agent.domain import canonical_digest
from printing_agent.errors import ValidationError
from printing_agent.fabrication import (
    MaterialDefinitionSpec,
    PrinterProfileRevision,
)

MappingState = Literal[
    "official_exact",
    "manual",
    "generic_confirmed",
    "confirmation_required",
    "upgrade_available",
    "ambiguous",
    "missing",
]


class FilamentMappingStatus(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    cloud_filament_id: str
    slots: tuple[str, ...]
    observed_material: str | None = None
    observed_sub_brands: tuple[str, ...] = ()
    state: MappingState
    material_id: str | None = None
    selected_profile_id: str | None = None
    proposed_profile_id: str | None = None
    proposed_profile_digest: str | None = None
    reason: str


@dataclass(frozen=True)
class ResolvedFilamentProfile:
    cloud_filament_id: str
    profile_id: str
    profile_digest: str
    dependency_digests: dict[str, str]
    spec: MaterialDefinitionSpec
    match: Literal["exact", "generic"]


class InstalledFilamentCatalog:
    _max_profile_bytes = 1024 * 1024

    def resolve_exact(
        self,
        cloud_filament_id: str,
        printer: PrinterProfileRevision,
    ) -> ResolvedFilamentProfile | None:
        root = self._root(printer)
        bases = self._base_profiles(root, cloud_filament_id=cloud_filament_id)
        if len(bases) != 1:
            return None
        return self._resolve_base(
            root,
            bases[0],
            cloud_filament_id,
            printer,
            match="exact",
        )

    def suggest_generic(
        self,
        cloud_filament_id: str,
        material_family: str | None,
        printer: PrinterProfileRevision,
    ) -> ResolvedFilamentProfile | None:
        if not material_family:
            return None
        root = self._root(printer)
        normalized = material_family.strip().casefold()
        family_bases = [
            item
            for item in self._base_profiles(root)
            if (
                self._family(item[1])
                or self._family_from_name(str(item[1].get("name", "")))
            )
            == normalized
            and str(item[1].get("name", "")).casefold().startswith("generic ")
        ]
        preferred_name = f"generic {normalized} @base"
        preferred = [
            item
            for item in family_bases
            if str(item[1].get("name", "")).casefold() == preferred_name
        ]
        bases = preferred if len(preferred) == 1 else family_bases
        if len(bases) != 1:
            return None
        return self._resolve_base(
            root,
            bases[0],
            cloud_filament_id,
            printer,
            match="generic",
        )

    def material_id(
        self,
        cloud_filament_id: str,
        profile_id: str,
    ) -> str:
        normalized = re.sub(
            r"[^a-z0-9]+",
            "-",
            cloud_filament_id.casefold(),
        ).strip("-")
        if not normalized:
            raise ValidationError("Cloud filament ID cannot form a material ID")
        profile_scope = (
            profile_id.rsplit(" @BBL ", 1)[1]
            if " @BBL " in profile_id
            else profile_id
        )
        profile_key = hashlib.sha256(profile_scope.encode()).hexdigest()[:8]
        return f"bambu-{normalized}-{profile_key}"[:64].rstrip("-")

    def profile_is_compatible(
        self,
        profile_id: str | None,
        printer: PrinterProfileRevision,
    ) -> bool:
        if not profile_id:
            return False
        marker = " @BBL "
        if marker not in profile_id:
            return False
        _, suffix = profile_id.rsplit(marker, 1)
        nozzle_diameters = {
            toolhead.nozzle_diameter_mm for toolhead in printer.spec.toolheads
        }
        if len(nozzle_diameters) != 1:
            return False
        diameter = next(iter(nozzle_diameters))
        expected = (
            printer.spec.model
            if abs(diameter - 0.4) < 0.0001
            else f"{printer.spec.model} {diameter:g} nozzle"
        )
        return suffix == expected

    def _resolve_base(
        self,
        root: Path,
        base: tuple[Path, dict[str, object]],
        cloud_filament_id: str,
        printer: PrinterProfileRevision,
        *,
        match: Literal["exact", "generic"],
    ) -> ResolvedFilamentProfile | None:
        _, base_payload = base
        base_name = str(base_payload.get("name", "")).removesuffix(" @base")
        profile_id = self._instantiated_profile_id(base_name, printer)
        try:
            path = root / "BBL" / "filament" / f"{profile_id}.json"
            self._validate_vendor_path(root, path)
            payload, digests = self._load_validated_graph(root, path)
        except ValidationError:
            return None
        resolved_id = str(payload.get("filament_id", "")).strip()
        if match == "exact" and resolved_id != cloud_filament_id:
            return None
        selected_key = next(
            (
                key
                for key in digests
                if key.casefold().endswith(f"/{profile_id}.json".casefold())
            ),
            None,
        )
        if selected_key is None:
            return None
        spec = self._material_spec(
            payload,
            printer,
            cloud_filament_id,
            profile_id,
            digests,
            digests[selected_key],
            match,
        )
        confirmation_digest = canonical_digest(
            {
                "profile_id": profile_id,
                "dependency_digests": digests,
                "effective_spec": spec.model_dump(mode="json"),
            }
        )
        return ResolvedFilamentProfile(
            cloud_filament_id=cloud_filament_id,
            profile_id=profile_id,
            profile_digest=confirmation_digest,
            dependency_digests=digests,
            spec=spec,
            match=match,
        )

    def _material_spec(
        self,
        payload: dict[str, object],
        printer: PrinterProfileRevision,
        cloud_filament_id: str,
        profile_id: str,
        digests: dict[str, str],
        profile_digest: str,
        match: Literal["exact", "generic"],
    ) -> MaterialDefinitionSpec:
        family = self._family(payload)
        if not family:
            raise ValidationError("Installed filament profile has no material family")
        nozzle_low = self._first_number(payload, "nozzle_temperature_range_low")
        nozzle_high = self._first_number(payload, "nozzle_temperature_range_high")
        bed_values = [
            number
            for key in (
                "textured_plate_temp",
                "hot_plate_temp",
                "eng_plate_temp",
                "cool_plate_temp",
            )
            for number in self._numbers(payload.get(key))
        ]
        if nozzle_low is None or nozzle_high is None or not bed_values:
            raise ValidationError("Installed filament profile lacks temperature limits")
        vendors = self._strings(payload.get("filament_vendor"))
        diameters = self._numbers(payload.get("filament_diameter"))
        speeds = [
            item
            for item in self._numbers(payload.get("filament_max_volumetric_speed"))
            if item > 0
        ]
        hrc = self._numbers(payload.get("required_nozzle_HRC"))
        origin = "studio_exact" if match == "exact" else "generic_confirmed"
        return MaterialDefinitionSpec(
            display_name=profile_id.split(" @", 1)[0],
            manufacturer=vendors[0] if vendors else None,
            product_line=profile_id.split(" @", 1)[0],
            family=family,
            modifiers=self._modifiers(profile_id, family),
            filament_diameter_mm=diameters[0] if diameters else 1.75,
            nominal_color="#B7C4D4",
            nozzle_temperature_c=(int(nozzle_low), int(nozzle_high)),
            bed_temperature_c=(int(min(bed_values)), int(max(bed_values))),
            hardened_nozzle_required=bool(hrc and max(hrc) > 0),
            supported_nozzle_diameters_mm={
                toolhead.nozzle_diameter_mm for toolhead in printer.spec.toolheads
            },
            supported_plate_ids={plate.id for plate in printer.spec.plates},
            maximum_volumetric_speed=min(speeds) if speeds else None,
            slicer_filament_profile_id=profile_id,
            cloud_filament_ids={cloud_filament_id},
            slicer_profile_digest=profile_digest,
            slicer_profile_dependency_digests=digests,
            mapping_origin=origin,
            source_cloud_filament_id=cloud_filament_id,
            source_profile_id=profile_id,
            source_profile_match=match,
        )

    def _base_profiles(
        self,
        root: Path,
        *,
        cloud_filament_id: str | None = None,
    ) -> list[tuple[Path, dict[str, object]]]:
        vendor = (root / "BBL" / "filament").resolve()
        try:
            vendor.relative_to(root)
        except ValueError as exc:
            raise ValidationError("Bambu filament profile root is invalid") from exc
        if not vendor.is_dir() or vendor.is_symlink():
            raise ValidationError("Bambu filament profile directory is unavailable")
        output: list[tuple[Path, dict[str, object]]] = []
        for path in vendor.glob("*@base.json"):
            if path.is_symlink() or not path.is_file():
                continue
            if path.stat().st_size <= 0 or path.stat().st_size > self._max_profile_bytes:
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if (
                not isinstance(payload, dict)
                or payload.get("type") != "filament"
                or payload.get("from") != "system"
            ):
                continue
            filament_id = payload.get("filament_id")
            if cloud_filament_id is not None and filament_id != cloud_filament_id:
                continue
            output.append((path.resolve(), payload))
        return output

    @staticmethod
    def _instantiated_profile_id(
        base_name: str,
        printer: PrinterProfileRevision,
    ) -> str:
        nozzle_diameters = {
            toolhead.nozzle_diameter_mm for toolhead in printer.spec.toolheads
        }
        if len(nozzle_diameters) != 1:
            raise ValidationError(
                "Automatic filament mapping requires one configured nozzle diameter"
            )
        diameter = next(iter(nozzle_diameters))
        model = printer.spec.model.strip()
        suffix = (
            f" @BBL {model}"
            if abs(diameter - 0.4) < 0.0001
            else f" @BBL {model} {diameter:g} nozzle"
        )
        return f"{base_name}{suffix}"

    @staticmethod
    def _root(printer: PrinterProfileRevision) -> Path:
        root_value = printer.spec.slicer.resource_root
        if root_value is None:
            raise ValidationError("Bambu Studio profile resource root is not configured")
        root = Path(root_value).resolve()
        if not root.is_dir():
            raise ValidationError("Bambu Studio profile resource root is unavailable")
        return root

    @staticmethod
    def _validate_vendor_path(root: Path, path: Path) -> None:
        expected = (root / "BBL" / "filament").resolve()
        if path.is_symlink() or not path.is_file():
            raise ValidationError("Automatic filament preset is unavailable")
        try:
            path.resolve().relative_to(expected)
        except ValueError as exc:
            raise ValidationError("Automatic filament preset is outside BBL resources") from exc

    def _load_validated_graph(
        self,
        root: Path,
        start: Path,
    ) -> tuple[dict[str, object], dict[str, str]]:
        vendor = (root / "BBL" / "filament").resolve()
        cache: dict[Path, dict[str, object]] = {}
        active: set[Path] = set()
        digests: dict[str, str] = {}

        def load(path: Path) -> dict[str, object]:
            if path.is_symlink():
                raise ValidationError("Filament profile dependency cannot be a symlink")
            resolved = path.resolve()
            if resolved in cache:
                return cache[resolved]
            if resolved in active:
                raise ValidationError("Filament profile dependency graph is cyclic")
            self._validate_vendor_path(root, resolved)
            if (
                resolved.stat().st_size <= 0
                or resolved.stat().st_size > self._max_profile_bytes
            ):
                raise ValidationError("Filament profile dependency has an invalid size")
            try:
                payload = json.loads(resolved.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ValidationError("Filament profile dependency is invalid") from exc
            if not isinstance(payload, dict):
                raise ValidationError("Filament profile dependency is not an object")
            active.add(resolved)
            relative = resolved.relative_to(root).as_posix()
            digests[f"filament:{relative}"] = hashlib.sha256(
                resolved.read_bytes()
            ).hexdigest()
            merged: dict[str, object] = {}
            for key in ("inherits", "include"):
                raw = payload.get(key, [])
                values = [raw] if isinstance(raw, str) else raw
                if not isinstance(values, list):
                    raise ValidationError("Filament profile dependency list is invalid")
                for dependency in values:
                    if (
                        not isinstance(dependency, str)
                        or dependency in {".", ".."}
                        or any(character in dependency for character in ":/\\*?[]")
                    ):
                        raise ValidationError(
                            "Filament profile dependency identifier is invalid"
                        )
                    filename = (
                        dependency
                        if dependency.casefold().endswith(".json")
                        else f"{dependency}.json"
                    )
                    local = path.parent / filename
                    if local.is_file():
                        candidate = local
                    else:
                        candidates = list(vendor.rglob(filename))
                        if len(candidates) != 1:
                            raise ValidationError(
                                "Filament profile dependency is ambiguous or missing"
                            )
                        candidate = candidates[0]
                    if candidate.is_symlink():
                        raise ValidationError(
                            "Filament profile dependency cannot be a symlink"
                        )
                    try:
                        candidate.resolve().relative_to(vendor)
                    except ValueError as exc:
                        raise ValidationError(
                            "Filament profile dependency escapes BBL resources"
                        ) from exc
                    merged.update(load(candidate))
            merged.update(payload)
            merged.pop("inherits", None)
            merged.pop("include", None)
            active.remove(resolved)
            cache[resolved] = merged
            return merged

        return load(start), digests

    @staticmethod
    def _strings(value: object) -> list[str]:
        values = value if isinstance(value, list) else [value]
        return [
            str(item).strip()
            for item in values
            if isinstance(item, (str, int, float)) and str(item).strip()
        ]

    @classmethod
    def _numbers(cls, value: object) -> list[float]:
        output: list[float] = []
        for item in cls._strings(value):
            try:
                output.append(float(item))
            except ValueError:
                continue
        return output

    @classmethod
    def _first_number(cls, payload: dict[str, object], key: str) -> float | None:
        values = cls._numbers(payload.get(key))
        return values[0] if values else None

    @classmethod
    def _family(cls, payload: dict[str, object]) -> str:
        values = cls._strings(payload.get("filament_type"))
        return values[0].casefold() if values else ""

    @staticmethod
    def _family_from_name(value: str) -> str:
        words = value.removesuffix(" @base").split()
        return words[1].casefold() if len(words) >= 2 else ""

    @staticmethod
    def _modifiers(profile_id: str, family: str) -> set[str]:
        base = profile_id.split(" @", 1)[0].casefold()
        words = set(re.findall(r"[a-z0-9]+", base))
        words.discard(family.casefold())
        words.discard("bambu")
        words.discard("generic")
        return words


def observed_filament_groups(
    snapshot: CloudDeviceSnapshot,
) -> dict[str, dict[str, object]]:
    output: dict[str, dict[str, object]] = {}
    trays = [
        tray for unit in snapshot.ams_units for tray in unit.trays
    ] + list(snapshot.external_trays)
    for tray in trays:
        cloud_id = tray.material_profile_id
        if not cloud_id:
            continue
        value = output.setdefault(
            cloud_id,
            {
                "slots": set(),
                "materials": set(),
                "sub_brands": set(),
            },
        )
        value["slots"].add(tray.slot_id)  # type: ignore[union-attr]
        if tray.material:
            value["materials"].add(tray.material)  # type: ignore[union-attr]
        if tray.material_sub_brand:
            value["sub_brands"].add(tray.material_sub_brand)  # type: ignore[union-attr]
    return output
