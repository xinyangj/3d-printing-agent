from __future__ import annotations

import asyncio
import json
from pathlib import Path

from printing_agent.application import PrintingApplication
from printing_agent.cloud_inventory import DeviceSummary, parse_h2d_snapshot
from printing_agent.fabrication import MaterialDefinitionRevision
from printing_agent.fabrication_profiles import built_in_h2d_profile
from printing_agent.filament_profiles import InstalledFilamentCatalog
from printing_agent.repositories import WorkflowRepository


def _profile_root(tmp_path: Path) -> Path:
    filament = tmp_path / "BBL" / "filament"
    filament.mkdir(parents=True)
    (filament / "fdm_filament_pla.json").write_text(
        json.dumps(
            {
                "type": "filament",
                "name": "fdm_filament_pla",
                "from": "system",
                "filament_type": ["PLA"],
                "filament_vendor": ["Generic"],
                "filament_diameter": ["1.75"],
                "nozzle_temperature_range_low": ["190"],
                "nozzle_temperature_range_high": ["240"],
                "textured_plate_temp": ["55"],
                "filament_max_volumetric_speed": ["12"],
                "required_nozzle_HRC": ["3"],
            }
        ),
        encoding="utf-8",
    )
    (filament / "Bambu PLA Basic @base.json").write_text(
        json.dumps(
            {
                "type": "filament",
                "name": "Bambu PLA Basic @base",
                "from": "system",
                "inherits": "fdm_filament_pla",
                "filament_id": "GFA00",
                "filament_vendor": ["Bambu Lab"],
            }
        ),
        encoding="utf-8",
    )
    (filament / "Bambu PLA Basic @BBL H2D.json").write_text(
        json.dumps(
            {
                "type": "filament",
                "name": "Bambu PLA Basic @BBL H2D",
                "from": "system",
                "inherits": "Bambu PLA Basic @base",
                "instantiation": "true",
                "filament_max_volumetric_speed": ["25"],
            }
        ),
        encoding="utf-8",
    )
    (filament / "Generic PLA @base.json").write_text(
        json.dumps(
            {
                "type": "filament",
                "name": "Generic PLA @base",
                "from": "system",
                "inherits": "fdm_filament_pla",
                "filament_id": "GFL99",
            }
        ),
        encoding="utf-8",
    )
    (filament / "Generic PLA @BBL H2D.json").write_text(
        json.dumps(
            {
                "type": "filament",
                "name": "Generic PLA @BBL H2D",
                "from": "system",
                "inherits": "Generic PLA @base",
                "instantiation": "true",
            }
        ),
        encoding="utf-8",
    )
    (filament / "Generic PLA High Speed @base.json").write_text(
        json.dumps(
            {
                "type": "filament",
                "name": "Generic PLA High Speed @base",
                "from": "system",
                "inherits": "fdm_filament_pla",
                "filament_id": "GFL96",
            }
        ),
        encoding="utf-8",
    )
    (filament / "Generic PLA High Speed @BBL H2D.json").write_text(
        json.dumps(
            {
                "type": "filament",
                "name": "Generic PLA High Speed @BBL H2D",
                "from": "system",
                "inherits": "Generic PLA High Speed @base",
                "instantiation": "true",
            }
        ),
        encoding="utf-8",
    )
    return tmp_path


def _printer(root: Path):
    profile = built_in_h2d_profile()
    return profile.model_copy(
        update={
            "spec": profile.spec.model_copy(
                update={
                    "slicer": profile.spec.slicer.model_copy(
                        update={"resource_root": str(root)}
                    )
                }
            ),
            "digest": None,
        }
    ).with_digest()


def test_catalog_resolves_exact_h2d_profile_and_pins_dependencies(
    tmp_path: Path,
) -> None:
    root = _profile_root(tmp_path)
    resolved = InstalledFilamentCatalog().resolve_exact("GFA00", _printer(root))

    assert resolved is not None
    assert resolved.profile_id == "Bambu PLA Basic @BBL H2D"
    assert resolved.spec.mapping_origin == "studio_exact"
    assert resolved.spec.family == "pla"
    assert resolved.spec.manufacturer == "Bambu Lab"
    assert resolved.spec.maximum_volumetric_speed == 25
    assert resolved.spec.cloud_filament_ids == {"GFA00"}
    assert len(resolved.dependency_digests) == 3
    assert len(resolved.profile_digest) == 64


def test_catalog_suggests_generic_without_claiming_exact_match(
    tmp_path: Path,
) -> None:
    root = _profile_root(tmp_path)
    resolved = InstalledFilamentCatalog().suggest_generic(
        "UNKNOWN01",
        "PLA",
        _printer(root),
    )

    assert resolved is not None
    assert resolved.profile_id == "Generic PLA @BBL H2D"
    assert resolved.match == "generic"
    assert resolved.spec.mapping_origin == "generic_confirmed"
    assert resolved.spec.cloud_filament_ids == {"UNKNOWN01"}
    assert resolved.spec.source_profile_match == "generic"


def test_catalog_uses_machine_nozzle_specific_profile_name(tmp_path: Path) -> None:
    root = _profile_root(tmp_path)
    printer = _printer(root)
    printer = printer.model_copy(
        update={
            "spec": printer.spec.model_copy(
                update={
                    "toolheads": [
                        item.model_copy(update={"nozzle_diameter_mm": 0.6})
                        for item in printer.spec.toolheads
                    ]
                }
            )
        }
    )

    assert InstalledFilamentCatalog().resolve_exact("GFA00", printer) is None


def test_confirmation_digest_includes_inherited_dependency_content(
    tmp_path: Path,
) -> None:
    root = _profile_root(tmp_path)
    catalog = InstalledFilamentCatalog()
    before = catalog.resolve_exact("GFA00", _printer(root))
    assert before is not None
    base = root / "BBL" / "filament" / "Bambu PLA Basic @base.json"
    payload = json.loads(base.read_text(encoding="utf-8"))
    payload["filament_density"] = ["1.30"]
    base.write_text(json.dumps(payload), encoding="utf-8")

    after = catalog.resolve_exact("GFA00", _printer(root))

    assert after is not None
    assert before.profile_digest != after.profile_digest
    assert before.dependency_digests != after.dependency_digests


def test_catalog_rejects_dependency_path_escape(tmp_path: Path) -> None:
    root = _profile_root(tmp_path)
    instance = root / "BBL" / "filament" / "Bambu PLA Basic @BBL H2D.json"
    payload = json.loads(instance.read_text(encoding="utf-8"))
    payload["include"] = "../outside"
    instance.write_text(json.dumps(payload), encoding="utf-8")

    assert InstalledFilamentCatalog().resolve_exact(
        "GFA00",
        _printer(root),
    ) is None


def _snapshot(cloud_id: str = "GFA00"):
    return parse_h2d_snapshot(
        {
            "print": {
                "nozzles": [
                    {"position": "left", "diameter": 0.4, "type": "HS01"},
                    {"position": "right", "diameter": 0.4, "type": "HS01"},
                ],
                "ams": {
                    "ams": [
                        {
                            "id": "0",
                            "tray": [
                                {
                                    "id": "0",
                                    "state": 11,
                                    "tray_type": "PLA",
                                    "tray_info_idx": cloud_id,
                                    "tray_sub_brands": "PLA Basic",
                                    "tray_color": "307FE2FF",
                                    "remain": 50,
                                    "tray_weight": 1000,
                                }
                            ],
                        }
                    ]
                },
            }
        },
        DeviceSummary(
            device_id="H2D-PRIVATE-SERIAL",
            name="Workshop H2D",
            model="H2D",
            online=True,
        ),
    )


async def test_exact_mapping_sync_is_idempotent_and_versioned(
    repository: WorkflowRepository,
    tmp_path: Path,
) -> None:
    root = _profile_root(tmp_path)
    printer = _printer(root)
    application = object.__new__(PrintingApplication)
    application.repository = repository
    application.filament_catalog = InstalledFilamentCatalog()

    synchronized = await asyncio.gather(
        application.synchronize_material_mappings(printer, _snapshot()),
        application.synchronize_material_mappings(printer, _snapshot()),
    )
    first = synchronized[0]
    material_id = application.filament_catalog.material_id(
        "GFA00",
        "Bambu PLA Basic @BBL H2D",
    )
    first_definition = await repository.get_material_definition(material_id)
    second = await application.synchronize_material_mappings(
        printer,
        _snapshot(),
    )
    second_definition = await repository.get_material_definition(material_id)

    assert first[0].state == "official_exact"
    assert first[0].selected_profile_id == "Bambu PLA Basic @BBL H2D"
    assert second[0].state == "official_exact"
    assert first_definition.revision == second_definition.revision == 1
    await repository.disable_material_definitions({material_id})
    restored = await application.synchronize_material_mappings(printer, _snapshot())
    assert restored[0].state == "official_exact"
    assert [item.material_id for item in await repository.list_material_definitions()] == [
        material_id
    ]

    profile_path = (
        root / "BBL" / "filament" / "Bambu PLA Basic @BBL H2D.json"
    )
    payload = json.loads(profile_path.read_text(encoding="utf-8"))
    payload["filament_max_volumetric_speed"] = ["30"]
    profile_path.write_text(json.dumps(payload), encoding="utf-8")

    await application.synchronize_material_mappings(printer, _snapshot())
    updated = await repository.get_material_definition(material_id)

    assert updated.revision == 2
    assert updated.spec.maximum_volumetric_speed == 30


async def test_manual_mapping_prevents_automatic_replacement(
    repository: WorkflowRepository,
    tmp_path: Path,
) -> None:
    root = _profile_root(tmp_path)
    printer = _printer(root)
    catalog = InstalledFilamentCatalog()
    resolved = catalog.resolve_exact("GFA00", printer)
    assert resolved is not None
    automatic_id = catalog.material_id("GFA00", resolved.profile_id)
    automatic = MaterialDefinitionRevision(
        material_id=automatic_id,
        revision=1,
        spec=resolved.spec,
    ).with_digest()
    await repository.save_material_definition(automatic)
    manual = MaterialDefinitionRevision(
        material_id="manual-pla",
        revision=1,
        spec=resolved.spec.model_copy(
            update={
                "mapping_origin": "manual",
                "source_cloud_filament_id": None,
                "source_profile_id": None,
                "source_profile_match": None,
            }
        ),
    ).with_digest()
    await repository.save_material_definition(manual)
    application = object.__new__(PrintingApplication)
    application.repository = repository
    application.filament_catalog = catalog

    statuses = await application.synchronize_material_mappings(
        printer,
        _snapshot(),
    )

    assert statuses[0].state == "manual"
    assert statuses[0].material_id == "manual-pla"
    assert len(await repository.list_material_definitions()) == 1


async def test_duplicate_manual_mappings_are_reported_without_refresh_failure(
    repository: WorkflowRepository,
    tmp_path: Path,
) -> None:
    root = _profile_root(tmp_path)
    printer = _printer(root)
    resolved = InstalledFilamentCatalog().resolve_exact("GFA00", printer)
    assert resolved is not None
    manual_spec = resolved.spec.model_copy(
        update={
            "mapping_origin": "manual",
            "source_cloud_filament_id": None,
            "source_profile_id": None,
            "source_profile_match": None,
        }
    )
    for material_id in ("manual-one", "manual-two"):
        await repository.save_material_definition(
            MaterialDefinitionRevision(
                material_id=material_id,
                revision=1,
                spec=manual_spec,
            ).with_digest()
        )
    application = object.__new__(PrintingApplication)
    application.repository = repository
    application.filament_catalog = InstalledFilamentCatalog()

    statuses = await application.synchronize_material_mappings(
        printer,
        _snapshot(),
    )

    assert statuses[0].state == "ambiguous"
    assert len(await repository.list_material_definitions()) == 2


def test_automatic_material_ids_are_scoped_by_printer_nozzle(
    tmp_path: Path,
) -> None:
    root = _profile_root(tmp_path)
    source = root / "BBL" / "filament" / "Bambu PLA Basic @BBL H2D.json"
    payload = json.loads(source.read_text(encoding="utf-8"))
    payload["name"] = "Bambu PLA Basic @BBL H2D 0.6 nozzle"
    (root / "BBL" / "filament" / "Bambu PLA Basic @BBL H2D 0.6 nozzle.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )
    catalog = InstalledFilamentCatalog()
    printer_04 = _printer(root)
    printer_06 = printer_04.model_copy(
        update={
            "spec": printer_04.spec.model_copy(
                update={
                    "toolheads": [
                        item.model_copy(update={"nozzle_diameter_mm": 0.6})
                        for item in printer_04.spec.toolheads
                    ]
                }
            )
        }
    )
    resolved_04 = catalog.resolve_exact("GFA00", printer_04)
    resolved_06 = catalog.resolve_exact("GFA00", printer_06)
    assert resolved_04 is not None
    assert resolved_06 is not None

    assert catalog.material_id(
        "GFA00",
        resolved_04.profile_id,
    ) != catalog.material_id("GFA00", resolved_06.profile_id)
