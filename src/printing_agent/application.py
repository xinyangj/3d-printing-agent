from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import shutil
import weakref
from datetime import timedelta
from pathlib import Path
from typing import cast

from printing_agent.artifact_store import ArtifactStore, sha256_file
from printing_agent.bambu_connect import (
    BambuConnectManager,
    BambuConnectReadiness,
)
from printing_agent.catalogs import ThingiverseCatalog
from printing_agent.cloud_inventory import (
    AMSTray,
    CloudDeviceSnapshot,
    CloudInventoryError,
    CloudPrintStatusObservation,
    DeviceSummary,
    InventoryProvider,
)
from printing_agent.config import Settings
from printing_agent.copilot_agents import CopilotDiscoveryAgent, CopilotModelingAgent
from printing_agent.domain import (
    ArtifactApproval,
    ArtifactProvenance,
    CandidateAttempt,
    DiscoveryDecision,
    ModelArtifact,
    ModelDecision,
    ModelingHandoff,
    PrintJobStatus,
    PrintWorkflow,
    RevisionMode,
    RevisionRequest,
    RevisionVerification,
    RevisionVerificationCheck,
    SelectedFileRole,
    SelectedSourceSummary,
    WorkflowState,
    WorkKind,
    canonical_digest,
    new_id,
)
from printing_agent.errors import (
    BudgetExhaustedError,
    CandidateRejectedError,
    ConflictError,
    ExternalServiceError,
    NotFoundError,
    PolicyViolationError,
    PrintingAgentError,
    ValidationError,
)
from printing_agent.fabrication import (
    BambuConnectHandoff,
    BambuConnectHandoffStatus,
    BambuConnectSetupConfirmation,
    JobOverrides,
    MaterialAssignment,
    MaterialCandidate,
    MaterialDefinitionRevision,
    MaterialSlotSpec,
    PartMaterialAssignment,
    PartMaterialRequest,
    PhysicalSpool,
    PrinterProfileRevision,
    ProfileOrigin,
    SliceJob,
    SliceJobStatus,
    SliceMaterialAssessment,
    SlotPolicy,
    SpoolQuantityStatus,
    SpoolReconciliationResult,
    SpoolReservation,
    SpoolStatus,
    UnknownQuantitySlotAuthorization,
    WorkflowPrinterSnapshot,
    resolve_slot_policy,
)
from printing_agent.fabrication import (
    utc_now as fabrication_utc_now,
)
from printing_agent.fabrication_drivers import (
    SliceRequest,
    SlicerRegistry,
)
from printing_agent.fabrication_profiles import (
    built_in_simulator_profile,
    resolve_slicer_profile_file_digests,
)
from printing_agent.filament_profiles import (
    FilamentMappingStatus,
    InstalledFilamentCatalog,
    ResolvedFilamentProfile,
    observed_filament_groups,
)
from printing_agent.material_assignment import MaterialAssignmentService
from printing_agent.modeling import (
    ModelPipeline,
    TrimeshSelectedSourceInspector,
    unwrap_base_model_source,
)
from printing_agent.multipart import (
    normalize_source_set_scale,
    single_part_project,
    write_project_artifact,
)
from printing_agent.ports import PrinterAdapter
from printing_agent.printers import PrinterRegistry, ProfilePrinterAdapter
from printing_agent.repositories import WorkflowRepository
from printing_agent.verification import RevisionVerifier


class PrintingApplication:
    def __init__(
        self,
        settings: Settings,
        repository: WorkflowRepository,
        artifacts: ArtifactStore,
        catalog: ThingiverseCatalog,
        discovery: CopilotDiscoveryAgent,
        modeling: CopilotModelingAgent,
        source_inspector: TrimeshSelectedSourceInspector,
        model_pipeline: ModelPipeline,
        printers: PrinterRegistry,
        revision_verifier: RevisionVerifier,
        slicers: SlicerRegistry,
        inventory: InventoryProvider,
        material_assignment: MaterialAssignmentService,
        bambu_connect: BambuConnectManager,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.artifacts = artifacts
        self.catalog = catalog
        self.discovery = discovery
        self.modeling = modeling
        self.source_inspector = source_inspector
        self.model_pipeline = model_pipeline
        self.printers = printers
        self.revision_verifier = revision_verifier
        self.slicers = slicers
        self.inventory = inventory
        self.material_assignment = material_assignment
        self.bambu_connect = bambu_connect
        self.filament_catalog = InstalledFilamentCatalog()
        self._slicing_operation_locks: weakref.WeakValueDictionary[
            str, asyncio.Lock
        ] = weakref.WeakValueDictionary()
        self._cancel_locks: dict[str, asyncio.Lock] = {}

    async def create_workflow(
        self,
        requirement: str,
        printer_name: str,
        overrides: JobOverrides | None = None,
    ):
        workflow = PrintWorkflow(
            requirement=requirement,
            printer_name=printer_name,
        )
        _, snapshot = await self._build_printer_snapshot(
            workflow.id,
            printer_name,
            overrides or JobOverrides(),
        )
        return await self.repository.create_workflow_with_snapshot(
            workflow,
            snapshot,
        )

    async def _build_printer_snapshot(
        self,
        workflow_id: str,
        printer_name: str,
        resolved_overrides: JobOverrides,
    ) -> tuple[PrinterProfileRevision, WorkflowPrinterSnapshot]:
        profile = await self.repository.get_printer_profile(printer_name)
        self.printers.get(printer_name)
        cloud_binding_available, cloud_binding_message = (
            await self._cloud_binding_status(profile)
        )
        if (
            profile.spec.slicer.driver_id != "simulator_passthrough"
            and not cloud_binding_available
        ):
            raise ConflictError(cloud_binding_message)
        self._validate_job_overrides(profile, resolved_overrides)
        snapshot = WorkflowPrinterSnapshot(
            workflow_id=workflow_id,
            profile_id=profile.profile_id,
            profile_revision=profile.revision,
            profile_digest=profile.digest or "0" * 64,
            profile=profile.spec,
            overrides=resolved_overrides,
            slicer_profile_file_digests=await asyncio.to_thread(
                resolve_slicer_profile_file_digests,
                profile,
            ),
            resolved_slot_policy=resolve_slot_policy(
                profile.spec,
                resolved_overrides,
            ),
        ).with_digest()
        return profile, snapshot

    @staticmethod
    def _validate_job_overrides(
        profile: PrinterProfileRevision,
        overrides: JobOverrides,
    ) -> None:
        if profile.spec.slicer.driver_id == "bambu_studio_cli" and (
            overrides.timelapse is not None
            or overrides.calibration is not None
        ):
            raise ValidationError(
                "Timelapse and calibration are not supported by the slice-only workflow"
            )
        slot_ids = {slot.id for slot in profile.spec.material_slots}
        toolhead_ids = {toolhead.id for toolhead in profile.spec.toolheads}
        plate_ids = {plate.id for plate in profile.spec.plates}
        referenced_slots = set(overrides.forbidden_slot_ids)
        referenced_slots.update(overrides.allowed_slot_ids or set())
        referenced_slots.update(
            slot
            for values in overrides.part_allowed_slot_ids.values()
            for slot in values
        )
        referenced_slots.update(
            slot
            for values in overrides.part_forbidden_slot_ids.values()
            for slot in values
        )
        if not referenced_slots.issubset(slot_ids):
            raise ValidationError("Job overrides reference unknown material slots")
        if (
            overrides.toolhead_id is not None
            and overrides.toolhead_id not in toolhead_ids
        ):
            raise ValidationError("Job overrides reference an unknown toolhead")
        if (
            overrides.plate_id is not None
            and overrides.plate_id not in plate_ids
        ):
            raise ValidationError("Job overrides reference an unknown plate")

    async def fabrication_readiness(
        self,
        profile_id: str | None = None,
        profile_revision: int | None = None,
    ) -> list[dict[str, object]]:
        values: list[dict[str, object]] = []
        if profile_id is not None:
            profiles = [
                await self.repository.get_printer_profile(
                    profile_id,
                    profile_revision,
                )
            ]
        else:
            profiles = await self.repository.list_printer_profiles()
        cloud_devices: tuple[DeviceSummary, ...] | None = None
        cloud_inventory_error: str | None = None
        if any(
            profile.spec.slicer.driver_id != "simulator_passthrough"
            and profile.spec.cloud_device_serial is not None
            for profile in profiles
        ):
            try:
                cloud_devices = await self.inventory.list_devices()
            except CloudInventoryError as exc:
                cloud_inventory_error = str(exc)
        for profile in profiles:
            slicing_capable = (
                profile.spec.slicer.driver_id != "simulator_passthrough"
            )
            slicer_ready = not slicing_capable
            slicer_message = (
                "This profile does not require local slicing."
                if not slicing_capable
                else "Slicer readiness has not been checked."
            )
            if slicing_capable:
                try:
                    await self.slicers.get(
                        profile.spec.slicer.driver_id
                    ).validate_profile(profile)
                    await asyncio.to_thread(
                        resolve_slicer_profile_file_digests,
                        profile,
                    )
                except PrintingAgentError as exc:
                    slicer_ready = False
                    slicer_message = self._readiness_message(profile, exc)
                else:
                    slicer_ready = True
                    slicer_message = (
                        "Bambu Studio and the configured profile resources "
                        "are available."
                    )
            cloud_binding_available, cloud_binding_message = (
                await self._cloud_binding_status(
                    profile,
                    devices=cloud_devices,
                    inventory_error=cloud_inventory_error,
                )
                if slicing_capable
                else (False, "This profile does not use a cloud printer binding.")
            )

            values.append(
                {
                    "profile_id": profile.profile_id,
                    "profile_revision": profile.revision,
                    "display_name": profile.spec.display_name,
                    "slicing_capable": slicing_capable,
                    "ready_for_fabrication": (
                        slicing_capable
                        and slicer_ready
                        and cloud_binding_available
                    ),
                    "slicer": {
                        "ready": slicer_ready,
                        "message": slicer_message,
                    },
                    "cloud_binding": {
                        "configured": (
                            profile.spec.cloud_device_serial is not None
                        ),
                        "available": cloud_binding_available,
                        "device_name": profile.spec.cloud_device_name,
                        "message": cloud_binding_message,
                    },
                }
            )
        return values

    async def _cloud_binding_status(
        self,
        profile: PrinterProfileRevision,
        *,
        devices: tuple[DeviceSummary, ...] | None = None,
        inventory_error: str | None = None,
    ) -> tuple[bool, str]:
        device_id = profile.spec.cloud_device_serial
        if device_id is None:
            return False, "Bind a cloud printer before creating the slicing workflow."
        if inventory_error is not None:
            return False, inventory_error
        if devices is None:
            try:
                devices = await self.inventory.list_devices()
            except CloudInventoryError as exc:
                return False, str(exc)
        if not any(device.device_id == device_id for device in devices):
            return (
                False,
                "The profile's bound printer is unavailable under the connected account.",
            )
        return True, "The bound printer is available under the connected account."

    async def list_cloud_devices(self):
        try:
            return await self.inventory.list_devices()
        except CloudInventoryError as exc:
            raise ExternalServiceError(str(exc)) from exc

    async def bind_cloud_device(
        self,
        profile_id: str,
        device_ref: str,
    ) -> PrinterProfileRevision:
        devices = await self.list_cloud_devices()
        selected = next(
            (
                item
                for item in devices
                if hashlib.sha256(item.device_id.encode()).hexdigest() == device_ref
            ),
            None,
        )
        if selected is None:
            raise NotFoundError("Cloud device reference was not found")
        try:
            snapshot = await self.inventory.snapshot(selected.device_id)
        except CloudInventoryError as exc:
            raise ExternalServiceError(str(exc)) from exc
        current = await self.repository.get_printer_profile(profile_id)
        material_slots = self._observed_material_slots(current, snapshot)
        slot_ids = {slot.id for slot in material_slots}
        policy = self._restrict_slot_policy(
            current.spec.default_slot_policy,
            slot_ids,
        )
        profile = current.model_copy(
            update={
                "revision": current.revision + 1,
                "origin": ProfileOrigin.CUSTOM,
                "spec": current.spec.model_copy(
                    update={
                        "material_slots": material_slots,
                        "default_slot_policy": policy,
                        "cloud_region": snapshot.region,
                        "cloud_device_name": selected.name,
                        "cloud_device_serial": selected.device_id,
                    }
                ),
                "digest": None,
            }
        ).with_digest()
        await self.repository.save_printer_profile(
            profile,
            expected_revision=current.revision,
        )
        self.printers.upsert(ProfilePrinterAdapter(profile))
        await self.synchronize_material_mappings(profile, snapshot)
        return profile

    async def observe_slicing_profile_slots(
        self,
        profile_id: str,
    ) -> tuple[
        PrinterProfileRevision,
        CloudDeviceSnapshot,
        list[FilamentMappingStatus],
    ]:
        profile = await self.repository.get_printer_profile(profile_id)
        device_id = profile.spec.cloud_device_serial
        if not device_id:
            raise ConflictError("Slicing profile has no bound cloud printer")
        try:
            snapshot = await self.inventory.snapshot(device_id)
        except CloudInventoryError as exc:
            raise ExternalServiceError(str(exc)) from exc
        self._validate_cloud_snapshot(profile, snapshot)
        mappings = await self.synchronize_material_mappings(profile, snapshot)
        return profile, snapshot, mappings

    @staticmethod
    def _device_ref(device_id: str) -> str:
        return hashlib.sha256(device_id.encode()).hexdigest()

    @staticmethod
    def _tray_identity_digest(device_id: str, tray: AMSTray) -> str:
        return canonical_digest(
            {
                "device_ref": PrintingApplication._device_ref(device_id),
                "slot_id": tray.slot_id,
                "material": tray.material,
                "material_profile_id": tray.material_profile_id,
                "material_sub_brand": tray.material_sub_brand,
                "color": tray.color,
                "rfid_uid": tray.rfid_uid,
                "tray_uuid": tray.tray_uuid,
            }
        )

    @staticmethod
    def _snapshot_trays(snapshot: CloudDeviceSnapshot) -> list[AMSTray]:
        trays = [
            tray
            for unit in snapshot.ams_units
            for tray in unit.trays
        ]
        trays.extend(snapshot.external_trays)
        return trays

    async def unknown_quantity_authorization_states(
        self,
        profile: PrinterProfileRevision,
        snapshot: CloudDeviceSnapshot,
    ) -> list[dict[str, object]]:
        device_ref = self._device_ref(snapshot.device.device_id)
        authorizations = {
            item.slot_id: item
            for item in await self.repository.list_unknown_quantity_authorizations(
                profile.profile_id,
                device_ref,
            )
        }
        output: list[dict[str, object]] = []
        for tray in self._snapshot_trays(snapshot):
            if (
                tray.material is None
                or tray.estimated_remaining_g is not None
            ):
                continue
            identity = self._tray_identity_digest(snapshot.device.device_id, tray)
            authorization = authorizations.get(tray.slot_id)
            active = (
                authorization is not None
                and authorization.tray_identity_digest == identity
            )
            output.append(
                {
                    "slot_id": tray.slot_id,
                    "tray_identity_digest": identity,
                    "status": (
                        "authorized_unknown"
                        if active
                        else "authorization_required"
                    ),
                    "authorized_at": (
                        authorization.authorized_at.isoformat()
                        if active and authorization is not None
                        else None
                    ),
                }
            )
        return output

    async def _authorize_unknown_quantity_slot(
        self,
        profile: PrinterProfileRevision,
        snapshot: CloudDeviceSnapshot,
        slot_id: str,
        *,
        expected_cloud_snapshot_digest: str,
        expected_tray_identity_digest: str,
        authorized_by: str,
    ) -> UnknownQuantitySlotAuthorization:
        if snapshot.digest != expected_cloud_snapshot_digest:
            raise ConflictError(
                "Cloud inventory changed; review the slot before authorizing it"
            )
        tray = next(
            (
                item
                for item in self._snapshot_trays(snapshot)
                if item.slot_id == slot_id
            ),
            None,
        )
        if tray is None or tray.material is None:
            raise ConflictError("The selected slot is no longer loaded")
        if tray.estimated_remaining_g is not None:
            raise ConflictError("The selected slot now has a measured quantity")
        if not tray.material_profile_id:
            raise ConflictError("The selected slot has no Bambu filament identifier")
        identity = self._tray_identity_digest(snapshot.device.device_id, tray)
        if identity != expected_tray_identity_digest:
            raise ConflictError(
                "The loaded tray changed; review it before authorizing quantity"
            )
        authorization = UnknownQuantitySlotAuthorization(
            profile_id=profile.profile_id,
            device_ref=self._device_ref(snapshot.device.device_id),
            slot_id=slot_id,
            tray_identity_digest=identity,
            cloud_snapshot_digest=snapshot.digest,
            material_profile_id=tray.material_profile_id,
            material=tray.material,
            color=tray.color,
            authorized_by=authorized_by,
        )
        await self.repository.save_unknown_quantity_authorization(authorization)
        return authorization

    async def authorize_profile_unknown_quantity_slot(
        self,
        profile_id: str,
        slot_id: str,
        *,
        expected_cloud_snapshot_digest: str,
        expected_tray_identity_digest: str,
        authorized_by: str,
    ) -> UnknownQuantitySlotAuthorization:
        profile, snapshot, _ = await self.observe_slicing_profile_slots(profile_id)
        return await self._authorize_unknown_quantity_slot(
            profile,
            snapshot,
            slot_id,
            expected_cloud_snapshot_digest=expected_cloud_snapshot_digest,
            expected_tray_identity_digest=expected_tray_identity_digest,
            authorized_by=authorized_by,
        )

    async def revoke_profile_unknown_quantity_slot(
        self,
        profile_id: str,
        slot_id: str,
    ) -> None:
        profile = await self.repository.get_printer_profile(profile_id)
        device_id = profile.spec.cloud_device_serial
        if not device_id:
            raise ConflictError("Slicing profile has no bound cloud printer")
        await self.repository.revoke_unknown_quantity_authorization(
            profile_id,
            self._device_ref(device_id),
            slot_id,
        )

    async def authorize_workflow_unknown_quantity_slot_and_retry(
        self,
        workflow_id: str,
        slot_id: str,
        *,
        expected_cloud_snapshot_digest: str,
        expected_tray_identity_digest: str,
        authorized_by: str,
    ) -> MaterialAssignment:
        await self.repository.get_workflow(workflow_id)
        async with self._slicing_operation_lock(workflow_id):
            workflow = await self.repository.get_workflow(workflow_id)
            PrintingApplication._ensure_not_archived(workflow)
            if workflow.state != WorkflowState.SLICE_SETUP:
                raise ConflictError(
                    "Quantity authorization is unavailable in the current state"
                )
            printer_snapshot = (
                await self.repository.get_workflow_printer_snapshot(workflow_id)
            )
            profile = await self.repository.get_printer_profile(
                printer_snapshot.profile_id,
                printer_snapshot.profile_revision,
            )
            snapshot = await self.repository.get_latest_cloud_device_snapshot(
                workflow_id,
                printer_snapshot.digest,
            )
            await self._authorize_unknown_quantity_slot(
                profile,
                snapshot,
                slot_id,
                expected_cloud_snapshot_digest=expected_cloud_snapshot_digest,
                expected_tray_identity_digest=expected_tray_identity_digest,
                authorized_by=authorized_by,
            )
            return await self._propose_material_assignment_unlocked(workflow_id)

    @staticmethod
    def _observed_material_slots(
        profile: PrinterProfileRevision,
        snapshot: CloudDeviceSnapshot,
    ) -> list[MaterialSlotSpec]:
        existing = {slot.id: slot for slot in profile.spec.material_slots}
        all_toolheads = {toolhead.id for toolhead in profile.spec.toolheads}
        material_families = set(profile.spec.supported_material_families)
        slots: list[MaterialSlotSpec] = []
        for unit in snapshot.ams_units:
            for fallback_tray, tray in enumerate(unit.trays, start=1):
                if tray.slot_id in existing:
                    slots.append(existing[tray.slot_id])
                    continue
                match = re.fullmatch(r"ams(_ht)?(\d+)_(\d+)", tray.slot_id)
                unit_number = int(match.group(2)) if match else len(slots) // 4 + 1
                tray_number = int(match.group(3)) if match else fallback_tray
                system = "ams_ht" if unit.kind == "ams_ht" else "ams"
                label = "AMS HT" if system == "ams_ht" else "AMS"
                slots.append(
                    MaterialSlotSpec(
                        id=tray.slot_id,
                        name=f"{label} {unit_number} slot {tray_number}",
                        system=system,
                        unit=unit_number,
                        tray=tray_number,
                        compatible_toolhead_ids=all_toolheads,
                        supported_material_families=material_families,
                    )
                )
        observed_external_ids = {tray.slot_id for tray in snapshot.external_trays}
        for slot_id in sorted(observed_external_ids):
            if slot_id in existing:
                slots.append(existing[slot_id])
                continue
            toolheads = (
                {"left"}
                if slot_id.endswith("left")
                else {"right"}
                if slot_id.endswith("right")
                else all_toolheads
            )
            name = (
                "External spool — left toolhead"
                if slot_id.endswith("left")
                else "External spool — right toolhead"
                if slot_id.endswith("right")
                else f"External spool {slot_id}"
            )
            slots.append(
                MaterialSlotSpec(
                    id=slot_id,
                    name=name,
                    system="external",
                    compatible_toolhead_ids=toolheads,
                    supported_material_families=material_families,
                    manual_swap_required=True,
                )
            )
        return slots

    @staticmethod
    def _restrict_slot_policy(
        policy: SlotPolicy,
        slot_ids: set[str],
    ) -> SlotPolicy:
        def restrict_forbidden_parts(
            values: dict[str, set[str]],
        ) -> dict[str, set[str]]:
            return {
                part_id: restricted
                for part_id, slots in values.items()
                if (restricted := slots & slot_ids)
            }

        return SlotPolicy(
            forbidden_slot_ids=policy.forbidden_slot_ids & slot_ids,
            allowed_slot_ids=(
                policy.allowed_slot_ids & slot_ids
                if policy.allowed_slot_ids is not None
                else None
            ),
            part_allowed_slot_ids={
                part_id: slots & slot_ids
                for part_id, slots in policy.part_allowed_slot_ids.items()
            },
            part_forbidden_slot_ids=restrict_forbidden_parts(
                policy.part_forbidden_slot_ids
            ),
            allow_manual_swaps=policy.allow_manual_swaps,
        )

    async def refresh_cloud_snapshot(
        self,
        workflow_id: str,
        device_id: str | None = None,
    ) -> CloudDeviceSnapshot:
        workflow = await self.repository.get_workflow(workflow_id)
        PrintingApplication._ensure_not_archived(workflow)
        if workflow.state not in {
            WorkflowState.APPROVED,
            WorkflowState.SLICE_SETUP,
            WorkflowState.AWAITING_MATERIAL_REVIEW,
            WorkflowState.SLICE_FAILED,
            WorkflowState.AWAITING_SLICE_REVIEW,
        }:
            raise ConflictError(
                "Cloud inventory can be refreshed only during slicing setup or review"
            )
        profile_snapshot = (
            await self.repository.get_workflow_printer_snapshot(workflow_id)
        )
        profile = await self.repository.get_printer_profile(
            profile_snapshot.profile_id,
            profile_snapshot.profile_revision,
        )
        selected_device = device_id or profile.spec.cloud_device_serial
        if not selected_device:
            raise ValidationError(
                "The slicing profile is not bound to a Bambu Cloud H2D device"
            )
        try:
            snapshot = await self.inventory.snapshot(selected_device)
        except CloudInventoryError as exc:
            raise ExternalServiceError(str(exc)) from exc
        self._validate_cloud_snapshot(profile, snapshot)
        try:
            previous_snapshot = (
                await self.repository.get_latest_cloud_device_snapshot(
                    workflow_id,
                    profile_snapshot.digest,
                )
            )
        except NotFoundError:
            previous_snapshot = None
        mapping_before = await self._material_mapping_signature(
            profile,
            snapshot,
        )
        await self.repository.save_cloud_device_snapshot(
            workflow_id,
            profile.profile_id,
            snapshot,
            profile_snapshot.digest,
        )
        await self.synchronize_material_mappings(profile, snapshot)
        mapping_after = await self._material_mapping_signature(
            profile,
            snapshot,
        )
        mapping_changed = mapping_before != mapping_after
        assignment_stale = await self._workflow_material_assignment_stale(
            workflow_id
        )
        inventory_changed = (
            previous_snapshot is not None
            and previous_snapshot.digest != snapshot.digest
        )
        if workflow.state == WorkflowState.APPROVED or (
            workflow.state != WorkflowState.SLICE_SETUP
            and (inventory_changed or mapping_changed or assignment_stale)
        ):
            await self.repository.transition(
                workflow_id,
                WorkflowState.SLICE_SETUP,
                event_kind="cloud.snapshot_refreshed",
                payload={
                    "cloud_snapshot_id": snapshot.id,
                    "cloud_snapshot_digest": snapshot.digest,
                    "inventory_changed": inventory_changed,
                    "material_mapping_changed": mapping_changed,
                    "material_assignment_stale": assignment_stale,
                },
            )
        else:
            await self.repository.record_workflow_event(
                workflow_id,
                "cloud.snapshot_refreshed",
                {
                    "cloud_snapshot_id": snapshot.id,
                    "cloud_snapshot_digest": snapshot.digest,
                    "material_mapping_changed": mapping_changed,
                    "material_assignment_stale": assignment_stale,
                },
            )
        await self._retry_material_recovery_after_refresh(
            workflow_id,
            profile,
            snapshot,
        )
        return snapshot

    async def _retry_material_recovery_after_refresh(
        self,
        workflow_id: str,
        profile: PrinterProfileRevision,
        snapshot: CloudDeviceSnapshot,
    ) -> None:
        try:
            job = await self.repository.get_latest_slice_job(workflow_id)
            assignment = await self.repository.get_latest_material_assignment(
                workflow_id
            )
        except NotFoundError:
            return
        assessment = job.material_assessment
        if assessment is None or assessment.status != "load_required":
            return
        printer_snapshot = (
            await self.repository.get_workflow_printer_snapshot(workflow_id)
        )
        if (
            job.printer_snapshot_digest != printer_snapshot.digest
            or assignment.printer_snapshot_digest != printer_snapshot.digest
        ):
            return
        try:
            spools, materials = await self._observed_cloud_spools(profile, snapshot)
            available = await self.repository.available_spool_weights(
                {item.id for item in spools}
            )
            spools = [
                item.model_copy(
                    update={
                        "remaining_weight_g": available.get(
                            item.id,
                            item.remaining_weight_g,
                        )
                    }
                )
                for item in spools
            ]
            proposed = self._build_usage_recovery_assignment(
                assignment,
                assessment,
                profile,
                printer_snapshot,
                snapshot,
                spools,
                materials,
            )
        except PrintingAgentError as exc:
            await self._save_blocked_recovery_assignment(
                job,
                assignment,
                assessment,
                str(exc),
                cloud_snapshot=snapshot,
            )
            return
        recovered_assessment = assessment.model_copy(
            update={"status": "replacement_proposed", "digest": None}
        ).with_digest()
        proposed = proposed.model_copy(
            update={
                "source_assessment_digest": recovered_assessment.digest,
                "digest": None,
            }
        ).with_digest()
        await self.repository.save_slice_job(
            job.model_copy(
                update={
                    "material_assessment": recovered_assessment,
                    "message": "Sufficient replacement material is ready for review",
                    "updated_at": fabrication_utc_now(),
                }
            )
        )
        await self.repository.save_material_assignment(proposed)
        workflow = await self.repository.get_workflow(workflow_id)
        if workflow.state == WorkflowState.SLICE_SETUP:
            await self.repository.transition(
                workflow_id,
                WorkflowState.AWAITING_MATERIAL_REVIEW,
                event_kind="material.recovery_proposed",
                payload={
                    "assignment_id": proposed.id,
                    "assessment_digest": recovered_assessment.digest,
                },
            )

    async def _material_mapping_signature(
        self,
        profile: PrinterProfileRevision,
        snapshot: CloudDeviceSnapshot,
    ) -> str:
        cloud_ids = set(observed_filament_groups(snapshot))
        definitions = await self.repository.list_material_definitions()
        values = [
            {
                "material_id": item.material_id,
                "revision": item.revision,
                "digest": item.digest,
            }
            for item in definitions
            if item.spec.cloud_filament_ids & cloud_ids
            and (
                item.spec.mapping_origin == "manual"
                or self._get_filament_catalog().profile_is_compatible(
                    item.spec.source_profile_id,
                    profile,
                )
            )
        ]
        return canonical_digest(sorted(values, key=lambda item: item["material_id"]))

    async def synchronize_material_mappings(
        self,
        profile: PrinterProfileRevision,
        snapshot: CloudDeviceSnapshot,
    ) -> list[FilamentMappingStatus]:
        return await self._material_mapping_statuses(
            profile,
            snapshot,
            synchronize_exact=True,
        )

    async def material_mapping_statuses(
        self,
        profile: PrinterProfileRevision,
        snapshot: CloudDeviceSnapshot,
    ) -> list[FilamentMappingStatus]:
        return await self._material_mapping_statuses(
            profile,
            snapshot,
            synchronize_exact=False,
        )

    async def _material_mapping_statuses(
        self,
        profile: PrinterProfileRevision,
        snapshot: CloudDeviceSnapshot,
        *,
        synchronize_exact: bool,
    ) -> list[FilamentMappingStatus]:
        definitions = await self.repository.list_material_definitions()
        groups = observed_filament_groups(snapshot)
        output: list[FilamentMappingStatus] = []
        for cloud_id in sorted(groups):
            group = groups[cloud_id]
            observed_materials = sorted(group["materials"])  # type: ignore[arg-type]
            matches = [
                definition
                for definition in definitions
                if cloud_id in definition.spec.cloud_filament_ids
            ]
            manual_matches = [
                item for item in matches if item.spec.mapping_origin == "manual"
            ]
            if len(manual_matches) > 1:
                output.append(
                    self._mapping_status(
                        cloud_id,
                        group,
                        state="ambiguous",
                        reason=(
                            "Multiple explicit manual mappings claim this filament ID."
                        ),
                    )
                )
                continue
            if len(manual_matches) == 1:
                current = manual_matches[0]
                if synchronize_exact:
                    competing = {
                        item.material_id
                        for item in matches
                        if item.material_id != current.material_id
                    }
                    await self.repository.disable_material_definitions(competing)
                    definitions = [
                        item
                        for item in definitions
                        if item.material_id not in competing
                    ]
                output.append(
                    self._mapping_status(
                        cloud_id,
                        group,
                        state="manual",
                        material=current,
                        reason="Using the explicit user-created material mapping.",
                    )
                )
                continue
            matches = [
                item
                for item in matches
                if self._get_filament_catalog().profile_is_compatible(
                    item.spec.source_profile_id,
                    profile,
                )
            ]
            if len(matches) > 1:
                output.append(
                    self._mapping_status(
                        cloud_id,
                        group,
                        state="ambiguous",
                        reason="Multiple active material definitions claim this filament ID.",
                    )
                )
                continue
            catalog_error: str | None = None
            try:
                exact = await asyncio.to_thread(
                    self._get_filament_catalog().resolve_exact,
                    cloud_id,
                    profile,
                )
            except PrintingAgentError as exc:
                exact = None
                catalog_error = str(exc)
            if matches:
                current = matches[0]
                origin = current.spec.mapping_origin
                if origin == "generic_confirmed" and exact is not None:
                    output.append(
                        self._mapping_status(
                            cloud_id,
                            group,
                            state="upgrade_available",
                            material=current,
                            proposal=exact,
                            reason=(
                                "An exact official Studio preset is now available; "
                                "confirmation is required to upgrade."
                            ),
                        )
                    )
                    continue
                if origin == "studio_exact":
                    if exact is None:
                        if synchronize_exact:
                            await self.repository.disable_material_definitions(
                                {current.material_id}
                            )
                            definitions = [
                                item
                                for item in definitions
                                if item.material_id != current.material_id
                            ]
                        output.append(
                            self._mapping_status(
                                cloud_id,
                                group,
                                state="missing",
                                reason=(
                                    catalog_error
                                    or "The previously mapped exact Studio preset "
                                    "is no longer available."
                                ),
                            )
                        )
                        continue
                    if synchronize_exact and current.spec != exact.spec:
                        current = await self._save_resolved_mapping(
                            exact,
                        )
                    output.append(
                        self._mapping_status(
                            cloud_id,
                            group,
                            state=(
                                "manual"
                                if current.spec.mapping_origin == "manual"
                                else "official_exact"
                            ),
                            material=current,
                            reason=(
                                "Using the explicit user-created material mapping."
                                if current.spec.mapping_origin == "manual"
                                else "Mapped to the exact installed Bambu Studio preset."
                            ),
                        )
                    )
                    continue
                if origin == "generic_confirmed":
                    try:
                        generic = await asyncio.to_thread(
                            self._get_filament_catalog().suggest_generic,
                            cloud_id,
                            (
                                observed_materials[0]
                                if len(observed_materials) == 1
                                else None
                            ),
                            profile,
                        )
                    except PrintingAgentError as exc:
                        generic = None
                        catalog_error = str(exc)
                    if generic is None:
                        if synchronize_exact:
                            await self.repository.disable_material_definitions(
                                {current.material_id}
                            )
                        output.append(
                            self._mapping_status(
                                cloud_id,
                                group,
                                state="missing",
                                reason=(
                                    catalog_error
                                    or "The confirmed generic Studio preset is "
                                    "no longer available."
                                ),
                            )
                        )
                        continue
                    if current.spec != generic.spec:
                        if synchronize_exact:
                            await self.repository.disable_material_definitions(
                                {current.material_id}
                            )
                        output.append(
                            self._mapping_status(
                                cloud_id,
                                group,
                                state="confirmation_required",
                                proposal=generic,
                                reason=(
                                    "The generic preset dependency graph changed; "
                                    "confirmation is required again."
                                ),
                            )
                        )
                        continue
                output.append(
                    self._mapping_status(
                        cloud_id,
                        group,
                        state="generic_confirmed",
                        material=current,
                        reason="Using a previously confirmed generic Studio preset.",
                    )
                )
                continue

            if exact is not None:
                if synchronize_exact:
                    material = await self._save_resolved_mapping(exact)
                    definitions.append(material)
                    output.append(
                        self._mapping_status(
                            cloud_id,
                            group,
                            state=(
                                "manual"
                                if material.spec.mapping_origin == "manual"
                                else "official_exact"
                            ),
                            material=material,
                            reason=(
                                "Using the explicit user-created material mapping."
                                if material.spec.mapping_origin == "manual"
                                else "Auto-mapped to the exact installed Studio preset."
                            ),
                        )
                    )
                else:
                    output.append(
                        self._mapping_status(
                            cloud_id,
                            group,
                            state="confirmation_required",
                            proposal=exact,
                            reason="Exact mapping is ready to synchronize on refresh.",
                        )
                    )
                continue

            try:
                suggestion = await asyncio.to_thread(
                    self._get_filament_catalog().suggest_generic,
                    cloud_id,
                    observed_materials[0] if len(observed_materials) == 1 else None,
                    profile,
                )
            except PrintingAgentError as exc:
                suggestion = None
                catalog_error = str(exc)
            output.append(
                self._mapping_status(
                    cloud_id,
                    group,
                    state=(
                        "confirmation_required"
                        if suggestion is not None
                        else "missing"
                    ),
                    proposal=suggestion,
                    reason=(
                        "A generic Studio preset requires reusable user confirmation."
                        if suggestion is not None
                        else (
                            catalog_error
                            or "No unique installed Studio preset could be resolved."
                        )
                    ),
                )
            )
        return output

    async def confirm_material_mapping(
        self,
        workflow_id: str,
        cloud_filament_id: str,
        proposed_profile_id: str,
        proposed_profile_digest: str,
    ) -> MaterialDefinitionRevision:
        workflow = await self.repository.get_workflow(workflow_id)
        if workflow.state not in {
            WorkflowState.APPROVED,
            WorkflowState.SLICE_SETUP,
            WorkflowState.AWAITING_MATERIAL_REVIEW,
            WorkflowState.SLICE_FAILED,
            WorkflowState.AWAITING_SLICE_REVIEW,
        }:
            raise ConflictError(
                "Material mappings cannot change while slicing or after cancellation"
            )
        printer_snapshot = await self.repository.get_workflow_printer_snapshot(
            workflow_id
        )
        profile = await self.repository.get_printer_profile(
            printer_snapshot.profile_id,
            printer_snapshot.profile_revision,
        )
        snapshot = await self.repository.get_latest_cloud_device_snapshot(
            workflow_id,
            printer_snapshot.digest,
        )
        groups = observed_filament_groups(snapshot)
        group = groups.get(cloud_filament_id)
        if group is None:
            raise NotFoundError("Cloud filament ID is not present in this snapshot")
        definitions = await self.repository.list_material_definitions()
        matches = [
            item
            for item in definitions
            if cloud_filament_id in item.spec.cloud_filament_ids
        ]
        manual_matches = [
            item for item in matches if item.spec.mapping_origin == "manual"
        ]
        if manual_matches:
            raise ConflictError("Material mapping cannot replace an explicit or ambiguous mapping")
        matches = [
            item
            for item in matches
            if self._get_filament_catalog().profile_is_compatible(
                item.spec.source_profile_id,
                profile,
            )
        ]
        if len(matches) > 1:
            raise ConflictError("Material mapping is ambiguous for this printer profile")
        exact = await asyncio.to_thread(
            self._get_filament_catalog().resolve_exact,
            cloud_filament_id,
            profile,
        )
        observed_materials = sorted(group["materials"])  # type: ignore[arg-type]
        proposal = exact or await asyncio.to_thread(
            self._get_filament_catalog().suggest_generic,
            cloud_filament_id,
            observed_materials[0] if len(observed_materials) == 1 else None,
            profile,
        )
        if proposal is None:
            raise ValidationError("No confirmable installed filament preset is available")
        if (
            proposal.profile_id != proposed_profile_id
            or proposal.profile_digest != proposed_profile_digest
        ):
            raise ConflictError("Filament mapping proposal is stale; review it again")
        material = await self._save_resolved_mapping(
            proposal,
        )
        if workflow.state not in {
            WorkflowState.APPROVED,
            WorkflowState.SLICE_SETUP,
        }:
            await self.repository.transition(
                workflow_id,
                WorkflowState.SLICE_SETUP,
                event_kind="material.mapping_changed",
                payload={
                    "cloud_filament_id": cloud_filament_id,
                    "material_id": material.material_id,
                    "material_revision": material.revision,
                },
            )
        return material

    async def _save_resolved_mapping(
        self,
        resolved: ResolvedFilamentProfile,
    ) -> MaterialDefinitionRevision:
        material_id = self._get_filament_catalog().material_id(
            resolved.cloud_filament_id,
            resolved.profile_id,
        )
        return await self.repository.upsert_automatic_material_definition(
            material_id,
            resolved.spec,
        )

    def _get_filament_catalog(self) -> InstalledFilamentCatalog:
        catalog = getattr(self, "filament_catalog", None)
        if catalog is None:
            catalog = InstalledFilamentCatalog()
            self.filament_catalog = catalog
        return catalog

    @staticmethod
    def _mapping_status(
        cloud_id: str,
        group: dict[str, object],
        *,
        state,
        reason: str,
        material: MaterialDefinitionRevision | None = None,
        proposal: ResolvedFilamentProfile | None = None,
    ) -> FilamentMappingStatus:
        return FilamentMappingStatus(
            cloud_filament_id=cloud_id,
            slots=tuple(sorted(group["slots"])),  # type: ignore[arg-type]
            observed_material=(
                next(iter(group["materials"]))  # type: ignore[arg-type]
                if len(group["materials"]) == 1  # type: ignore[arg-type]
                else None
            ),
            observed_sub_brands=tuple(
                sorted(group["sub_brands"])  # type: ignore[arg-type]
            ),
            state=state,
            material_id=material.material_id if material else None,
            selected_profile_id=(
                material.spec.slicer_filament_profile_id if material else None
            ),
            proposed_profile_id=proposal.profile_id if proposal else None,
            proposed_profile_digest=proposal.profile_digest if proposal else None,
            reason=reason,
        )

    @staticmethod
    def _validate_cloud_snapshot(
        profile: PrinterProfileRevision,
        snapshot: CloudDeviceSnapshot,
    ) -> None:
        if profile.spec.cloud_region != snapshot.region:
            raise ConflictError(
                "Cloud device region differs from the slicing profile"
            )
        if (
            profile.spec.cloud_device_serial
            and profile.spec.cloud_device_serial != snapshot.device.device_id
        ):
            raise ConflictError(
                "Cloud snapshot device differs from the slicing profile binding"
            )
        if not snapshot.device.online:
            raise ConflictError("The bound H2D is offline")
        if "h2d" not in snapshot.device.model.casefold().replace(" ", ""):
            raise ConflictError("The bound cloud device is not an H2D")
        expected = {
            item.id: item for item in profile.spec.toolheads
        }
        observed = {
            item.position: item for item in snapshot.installed_nozzles
        }
        if set(observed) != {"left", "right"}:
            raise ConflictError("Cloud snapshot does not prove both H2D nozzles")
        for toolhead_id in ("left", "right"):
            configured = expected.get(toolhead_id)
            actual = observed[toolhead_id]
            if configured is None or not math.isclose(
                configured.nozzle_diameter_mm,
                actual.diameter_mm,
                rel_tol=0,
                abs_tol=1e-6,
            ):
                raise ConflictError(
                    f"Observed {toolhead_id} nozzle differs from the slicing profile"
                )
            configured_material = PrintingApplication._nozzle_material_category(
                configured.nozzle_material
            )
            observed_material = PrintingApplication._nozzle_material_category(
                actual.nozzle_type
            )
            if configured_material != observed_material:
                raise ConflictError(
                    f"Observed {toolhead_id} nozzle material differs from "
                    "the slicing profile"
                )
            if configured.hardened and observed_material != "hardened_steel":
                raise ConflictError(
                    f"Observed {toolhead_id} nozzle is not proven hardened"
                )
        expected_slots = {
            item.id for item in profile.spec.material_slots
        }
        observed_slots = {
            tray.slot_id
            for unit in snapshot.ams_units
            for tray in unit.trays
        } | {tray.slot_id for tray in snapshot.external_trays}
        missing_slots = sorted(expected_slots - observed_slots)
        if missing_slots:
            raise ConflictError(
                "Cloud snapshot is missing slicing-profile slots: "
                + ", ".join(missing_slots)
            )

    @staticmethod
    def _nozzle_material_category(value: str) -> str:
        normalized = value.casefold().replace("-", "_").replace(" ", "_")
        code = re.fullmatch(r"h[shx](00|01|05)", normalized)
        if code:
            return {
                "00": "stainless_steel",
                "01": "hardened_steel",
                "05": "tungsten_carbide",
            }[code.group(1)]
        if "hardened" in normalized:
            return "hardened_steel"
        if "tungsten" in normalized or "carbide" in normalized:
            return "tungsten_carbide"
        if "stainless" in normalized:
            return "stainless_steel"
        return normalized

    @staticmethod
    def _readiness_message(
        profile: PrinterProfileRevision,
        error: PrintingAgentError,
    ) -> str:
        message = str(error)
        for value in (
            profile.spec.slicer.executable_path,
            profile.spec.slicer.resource_root,
        ):
            if value:
                message = message.replace(value, "[configured path]")
        return message[-500:]

    @staticmethod
    def _ensure_not_archived(workflow: PrintWorkflow) -> None:
        if workflow.archived_at is not None:
            raise ConflictError("Restore the archived workflow before changing it")

    async def _workflow_profile_adapter(
        self,
        workflow: PrintWorkflow,
    ) -> PrinterAdapter:
        try:
            snapshot = await self.repository.get_workflow_printer_snapshot(
                workflow.id
            )
        except NotFoundError:
            return cast(PrinterAdapter, self.printers.get(workflow.printer_name))
        revision = await self.repository.get_printer_profile(
            snapshot.profile_id,
            snapshot.profile_revision,
        )
        if revision.digest != snapshot.profile_digest:
            raise ConflictError("Workflow printer profile snapshot digest is invalid")
        pinned = revision.model_copy(
            update={
                "spec": snapshot.profile,
                "digest": snapshot.profile_digest,
            }
        )
        return cast(PrinterAdapter, ProfilePrinterAdapter(pinned))

    async def prepare(self, workflow_id: str) -> ModelArtifact:
        workflow = await self.repository.get_workflow(workflow_id)
        PrintingApplication._ensure_not_archived(workflow)
        adapter = await self._workflow_profile_adapter(workflow)
        printer = await adapter.capabilities()

        if workflow.state == WorkflowState.RECEIVED:
            workflow = await self.repository.transition(
                workflow.id,
                WorkflowState.PLANNING,
                event_kind="preparation.started",
            )
            plan, decision = await self.discovery.discover(
                workflow.id,
                workflow.requirement,
                printer,
            )
        elif workflow.state == WorkflowState.REVISION_REQUESTED:
            revision = await self.repository.get_latest_revision(workflow.id)
            if revision.mode == RevisionMode.REFINE_CURRENT:
                try:
                    return await self._refine_current(
                        workflow.id,
                        revision.feedback,
                        revision.allowed_part_ids,
                        revision.base_artifact_version,
                        revision.base_handoff_version,
                        revision.base_state,
                    )
                except PrintingAgentError as exc:
                    return await self._rollback_revision_request_failure(
                        workflow.id,
                        revision,
                        exc,
                    )
                except Exception as exc:
                    return await self._rollback_revision_request_failure(
                        workflow.id,
                        revision,
                        ExternalServiceError(
                            f"Unexpected revision failure: {str(exc)[-1_950:]}"
                        ),
                    )
            await self.repository.transition(
                workflow.id,
                WorkflowState.DISCOVERING,
                event_kind="revision.discovery_restarted",
                payload={"feedback": revision.feedback},
            )
            plan = await self.repository.get_plan(workflow.id)
            decision = await self.discovery.restart_with_feedback(
                workflow.id,
                revision.feedback,
            )
        else:
            raise ConflictError(f"Cannot prepare a workflow in state {workflow.state.value}")

        while True:
            if decision.decision == ModelDecision.CREATE:
                return await self._create_with_modeling(
                    workflow_id,
                    plan,
                    decision,
                    printer,
                    selected_source=None,
                )

            page = await self.repository.get_page_inspection(
                workflow_id,
                str(decision.candidate_id),
            )
            candidate = page.candidate
            included_source_files = [
                item
                for item in decision.selected_files
                if item.role in {
                    SelectedFileRole.UNIQUE_PART,
                    SelectedFileRole.COMBINED_MODEL,
                }
            ]
            selected_file_ids = (
                [item.file_id for item in included_source_files]
                if included_source_files
                else [str(decision.file_id)]
                if decision.file_id is not None
                else []
            )
            try:
                if decision.selected_files and included_source_files:
                    artifact = await self._adopt_source_set(
                        workflow_id,
                        candidate,
                        decision,
                        printer.build_volume,
                    )
                else:
                    artifact = await self._adopt_single_candidate(
                        workflow_id,
                        candidate,
                        decision,
                        plan,
                        printer,
                    )
                await self.repository.save_candidate_attempt(
                    CandidateAttempt(
                        workflow_id=workflow_id,
                        candidate_id=candidate.id,
                        status="adopted",
                        stage="artifact_ready",
                        selected_file_ids=selected_file_ids,
                    )
                )
                return artifact
            except CandidateRejectedError as exc:
                decision = await self._retry_candidate_or_create(
                    workflow_id,
                    candidate.id,
                    selected_file_ids,
                    exc,
                )
                continue

    async def _rollback_revision_request_failure(
        self,
        workflow_id: str,
        revision: RevisionRequest,
        error: PrintingAgentError,
    ) -> ModelArtifact:
        workflow = await self.repository.get_workflow(workflow_id)
        base_artifact_version = (
            revision.base_artifact_version or workflow.active_artifact_version
        )
        if base_artifact_version is None:
            raise error
        base_artifact = await self.repository.get_artifact(
            workflow_id,
            base_artifact_version,
        )
        expected_handoff_version = revision.base_handoff_version
        if expected_handoff_version is None:
            raise error
        candidate_version = max(
            base_artifact_version,
            (await self.repository.next_artifact_version(workflow_id)) - 1,
        )
        verification = RevisionVerification(
            workflow_id=workflow_id,
            handoff_version=expected_handoff_version
            or revision.base_handoff_version
            or 1,
            base_artifact_version=base_artifact_version,
            candidate_artifact_version=candidate_version,
            feedback=revision.feedback,
            verdict="failed",
            repairable=False,
            checks=[
                RevisionVerificationCheck(
                    id="revision_execution",
                    passed=False,
                    repairable=False,
                    message=error.message[:2_000],
                    evidence={"error_code": error.code},
                )
            ],
            rationale=f"The revision could not be completed: {error.message}"[:4_000],
        )
        restore_state = (
            WorkflowState.APPROVED
            if revision.base_state == WorkflowState.APPROVED
            else WorkflowState.AWAITING_APPROVAL
        )
        await self.repository.rollback_failed_revision(
            verification,
            restore_handoff_version=revision.base_handoff_version,
            restore_state=restore_state,
            expected_handoff_version=expected_handoff_version,
            expected_active_artifact_version=base_artifact_version,
        )
        return base_artifact

    async def _adopt_single_candidate(
        self,
        workflow_id: str,
        candidate,
        decision: DiscoveryDecision,
        plan,
        printer,
    ) -> ModelArtifact:
        selected_file = next(
            (item for item in candidate.files if item.id == str(decision.file_id)),
            None,
        )
        if selected_file is None:
            raise CandidateRejectedError(
                "Selected source file is no longer available",
                stage="selection",
            )
        source_format = selected_file.format.casefold().lstrip(".")
        if source_format not in {"stl", "3mf"}:
            raise CandidateRejectedError(
                f"Source format '{source_format}' is not importable",
                stage="selection",
                diagnostics={selected_file.id: "unsupported source format"},
            )
        incoming = (
            self.settings.candidate_cache_dir / "models" / "incoming" / workflow_id
        )
        source_path = incoming / f"{selected_file.id}.{source_format}"
        try:
            await self.repository.transition(
                workflow_id,
                WorkflowState.SOURCE_VALIDATION,
                event_kind="source.download_started",
                payload={"candidate_id": candidate.id, "file_id": selected_file.id},
            )
            await self.catalog.download_file(candidate, selected_file.id, source_path)
            inspection = await self.source_inspector.inspect(
                workflow_id,
                candidate.id,
                selected_file.id,
                source_path,
                printer.build_volume,
            )
            await self.repository.save_source_inspection(inspection)
            if not inspection.accepted:
                raise CandidateRejectedError(
                    inspection.rejection_reason or "Selected source mesh is invalid",
                    stage="source_validation",
                    diagnostics={
                        selected_file.id: inspection.rejection_reason
                        or "invalid source mesh"
                    },
                )
            cache_path = (
                self.settings.candidate_cache_dir
                / "models"
                / f"{inspection.source_digest}.{source_format}"
            )
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            if not cache_path.exists():
                source_path.replace(cache_path)
            else:
                source_path.unlink(missing_ok=True)
            inspection = inspection.model_copy(update={"cached_path": cache_path})
            provenance = ArtifactProvenance(
                kind="catalog",
                candidate_id=candidate.id,
                source_url=candidate.source_url,
                creator=candidate.creator,
                license=candidate.license,
                source_digest=inspection.source_digest,
            )
            if not decision.requires_source_preparation:
                await self.repository.transition(
                    workflow_id,
                    WorkflowState.VALIDATING,
                    event_kind="source.adopting_unchanged",
                )
                try:
                    if source_format == "3mf":
                        return await self.model_pipeline.adopt_existing_3mf(
                            workflow_id,
                            cache_path,
                            provenance,
                            printer.build_volume,
                        )
                    return await self.model_pipeline.adopt_existing(
                        workflow_id,
                        cache_path,
                        provenance,
                        printer.build_volume,
                    )
                except ValidationError as exc:
                    raise CandidateRejectedError(
                        exc.message,
                        stage="artifact_validation",
                        diagnostics={selected_file.id: exc.message},
                    ) from exc
            if source_format != "stl":
                raise CandidateRejectedError(
                    "Structured 3MF sources cannot enter whole-model modification",
                    stage="selection",
                    diagnostics={selected_file.id: "3MF modification is unsupported"},
                )
            selected_source = SelectedSourceSummary(
                filename=f"source.{source_format}",
                format=source_format,
                candidate_id=candidate.id,
                file_id=selected_file.id,
                title=candidate.title,
                creator=candidate.creator,
                license=candidate.license,
                attribution_url=candidate.source_url,
                source_digest=inspection.source_digest,
                mesh=inspection.mesh,
            )
            try:
                return await self._create_with_modeling(
                    workflow_id,
                    plan,
                    decision,
                    printer,
                    selected_source=selected_source,
                )
            except BudgetExhaustedError as exc:
                raise CandidateRejectedError(
                    exc.message,
                    stage="modification",
                    diagnostics={selected_file.id: exc.message},
                ) from exc
        except PolicyViolationError as exc:
            raise CandidateRejectedError(
                exc.message,
                stage="download_policy",
                diagnostics={selected_file.id: exc.message},
            ) from exc
        finally:
            shutil.rmtree(incoming, ignore_errors=True)

    async def _retry_candidate_or_create(
        self,
        workflow_id: str,
        candidate_id: str,
        selected_file_ids: list[str],
        error: CandidateRejectedError,
    ) -> DiscoveryDecision:
        attempt = CandidateAttempt(
            workflow_id=workflow_id,
            candidate_id=candidate_id,
            status="rejected",
            stage=error.stage,
            selected_file_ids=selected_file_ids,
            diagnostics=error.diagnostics or {"candidate": error.message},
        )
        await self.repository.save_candidate_attempt(attempt)
        rejected_count = await self.repository.count_rejected_candidates(workflow_id)
        workflow = await self.repository.get_workflow(workflow_id)
        if error.stage == "modification" and workflow.active_handoff_version is not None:
            shutil.rmtree(
                self.artifacts.workflow_root(workflow_id)
                / "attempts"
                / f"handoff-{workflow.active_handoff_version}",
                ignore_errors=True,
            )
            await self.repository.patch_workflow(
                workflow_id,
                active_handoff_version=None,
                modeling_session_id=None,
                event_kind="candidate.modification_abandoned",
                payload={
                    "candidate_id": candidate_id,
                    "handoff_version": workflow.active_handoff_version,
                },
            )
            workflow = await self.repository.get_workflow(workflow_id)
        elif error.stage == "source_preparation":
            await self.repository.patch_workflow(
                workflow_id,
                modeling_session_id=None,
                event_kind="candidate.source_preparation_abandoned",
                payload={"candidate_id": candidate_id},
            )
            workflow = await self.repository.get_workflow(workflow_id)
        if workflow.state != WorkflowState.DISCOVERING:
            await self.repository.transition(
                workflow_id,
                WorkflowState.DISCOVERING,
                event_kind="candidate.rejected",
                payload={
                    "candidate_id": candidate_id,
                    "stage": error.stage,
                    "diagnostics": attempt.diagnostics,
                    "rejected_count": rejected_count,
                    "next_action": (
                        "generate"
                        if rejected_count
                        >= self.settings.post_selection_failure_budget
                        else "next_candidate"
                    ),
                },
            )
        if rejected_count >= self.settings.post_selection_failure_budget:
            await self.repository.transition(
                workflow_id,
                WorkflowState.SELECTING,
                event_kind="candidate.generation_fallback",
                payload={
                    "rejected_candidate_ids": sorted(
                        await self.repository.list_rejected_candidate_ids(workflow_id)
                    ),
                    "budget": self.settings.post_selection_failure_budget,
                },
            )
            decision = DiscoveryDecision(
                decision=ModelDecision.CREATE,
                rationale=(
                    "Catalog candidates exhausted the technical validation budget; "
                    "generate a new model from the persisted plan."
                ),
            )
            await self.repository.save_discovery_decision(workflow_id, decision)
            return decision
        return await self.discovery.resume_after_candidate_rejection(
            workflow_id,
            attempt,
        )

    async def _adopt_source_set(
        self,
        workflow_id: str,
        candidate,
        decision: DiscoveryDecision,
        build_volume,
    ) -> ModelArtifact:
        selected = [
            item
            for item in decision.selected_files
            if item.role in {
                SelectedFileRole.UNIQUE_PART,
                SelectedFileRole.COMBINED_MODEL,
            }
        ]
        if not selected:
            raise CandidateRejectedError(
                "Source set contains no included files",
                stage="selection",
            )
        candidate_files = {item.id: item for item in candidate.files}
        await self.repository.transition(
            workflow_id,
            WorkflowState.SOURCE_VALIDATION,
            event_kind="source_set.download_started",
            payload={
                "candidate_id": candidate.id,
                "file_ids": [item.file_id for item in selected],
            },
        )
        incoming = (
            self.settings.candidate_cache_dir
            / "models"
            / "incoming"
            / workflow_id
        )
        shutil.rmtree(incoming, ignore_errors=True)
        preparation_workspace = None
        try:
            downloaded = []
            diagnostics: dict[str, str] = {}
            for selection in selected:
                candidate_file = candidate_files.get(selection.file_id)
                if candidate_file is None:
                    diagnostics[selection.file_id] = "selected file is unavailable"
                    continue
                source_format = candidate_file.format.casefold().lstrip(".")
                if source_format != "stl":
                    diagnostics[candidate_file.id] = (
                        "multi-file source sets currently require STL files"
                    )
                    continue
                source_path = incoming / f"{candidate_file.id}.{source_format}"
                await self.catalog.download_file(candidate, candidate_file.id, source_path)
                inspection = await self.source_inspector.inspect(
                    workflow_id,
                    candidate.id,
                    candidate_file.id,
                    source_path,
                    build_volume,
                )
                await self.repository.save_source_inspection(inspection)
                if not inspection.accepted:
                    diagnostics[candidate_file.id] = (
                        inspection.rejection_reason or "invalid source part"
                    )
                downloaded.append((selection, candidate_file, source_path, inspection))
            if diagnostics:
                raise CandidateRejectedError(
                    "One or more required source-set files failed validation",
                    stage="source_validation",
                    diagnostics=diagnostics,
                )

            prepared = []
            for selection, candidate_file, source_path, inspection in downloaded:
                prepared.append(
                    (
                        selection,
                        candidate_file,
                        source_path,
                        inspection.mesh,
                    )
                )
            if decision.requires_source_preparation:
                try:
                    prepared, preparation_workspace = (
                        await self.modeling.prepare_source_set(
                            workflow_id,
                            prepared,
                            decision.preparation_changes,
                        )
                    )
                except BudgetExhaustedError as exc:
                    raise CandidateRejectedError(
                        exc.message,
                        stage="source_preparation",
                        diagnostics={"source_set": exc.message},
                    ) from exc

            shared_scale = normalize_source_set_scale(
                prepared,
                shared_scale=decision.shared_scale,
                max_layout_width=build_volume.width_mm,
            )
            if shared_scale != decision.shared_scale:
                await self.repository.record_workflow_event(
                    workflow_id,
                    "source_set.units_normalized",
                    {
                        "declared_scale": decision.shared_scale,
                        "effective_scale": shared_scale,
                        "reason": "Unitless STL dimensions are consistent with inches",
                    },
                )
            source_set_digest = canonical_digest(
                {
                    "files": [
                        {
                            "file_id": selection.file_id,
                            "digest": hashlib.sha256(path.read_bytes()).hexdigest(),
                        }
                        for selection, _, path, _ in prepared
                    ],
                    "shared_scale": shared_scale,
                }
            )
            provenance = ArtifactProvenance(
                kind="catalog",
                candidate_id=candidate.id,
                source_url=candidate.source_url,
                creator=candidate.creator,
                license=candidate.license,
                source_digest=source_set_digest,
            )
            await self.repository.transition(
                workflow_id,
                WorkflowState.VALIDATING,
                event_kind="source_set.adopting",
                payload={"part_count": len(prepared)},
            )
            try:
                return await self.model_pipeline.adopt_existing_set(
                    workflow_id,
                    prepared,
                    provenance,
                    build_volume,
                    shared_scale=shared_scale,
                    description=f"{candidate.introduction}\n{candidate.instructions}",
                    classification=decision.selected_files,
                )
            except ValidationError as exc:
                raise CandidateRejectedError(
                    exc.message,
                    stage="artifact_validation",
                    diagnostics={"source_set": exc.message},
                ) from exc
        except PolicyViolationError as exc:
            raise CandidateRejectedError(
                exc.message,
                stage="download_policy",
                diagnostics={"candidate": exc.message},
            ) from exc
        finally:
            shutil.rmtree(incoming, ignore_errors=True)
            if preparation_workspace is not None:
                shutil.rmtree(preparation_workspace, ignore_errors=True)

    async def _create_with_modeling(
        self,
        workflow_id: str,
        plan,
        decision: DiscoveryDecision,
        printer,
        *,
        selected_source: SelectedSourceSummary | None,
    ) -> ModelArtifact:
        version = await self.repository.next_handoff_version(workflow_id)
        handoff = ModelingHandoff(
            workflow_id=workflow_id,
            version=version,
            requirement=(await self.repository.get_workflow(workflow_id)).requirement,
            model_plan=plan,
            decision=(
                ModelDecision.MODIFY
                if selected_source is not None
                else ModelDecision.CREATE
            ),
            required_changes=(
                decision.preparation_changes
                if decision.requires_source_preparation
                else decision.required_changes
            ),
            selected_source=selected_source,
            target_printer=printer,
            discovery_rationale=decision.rationale,
            evidence_digests=(
                [selected_source.source_digest] if selected_source is not None else []
            ),
        ).with_digest()
        await self.repository.save_handoff(handoff)
        await self.repository.patch_workflow(
            workflow_id,
            modeling_session_id=None,
            event_kind="modeling.new_session_required",
            payload={"handoff_version": version, "handoff_digest": handoff.digest},
        )
        await self.repository.transition(
            workflow_id,
            WorkflowState.HANDOFF_READY,
            event_kind="modeling.handoff_ready",
            payload={"handoff_version": version, "handoff_digest": handoff.digest},
        )
        await self.repository.transition(
            workflow_id,
            WorkflowState.GENERATING,
            event_kind="modeling.started",
            payload={"handoff_version": version},
        )
        return await self.modeling.build(handoff)

    async def _refine_current(
        self,
        workflow_id: str,
        feedback: str,
        allowed_part_ids: list[str] | None,
        requested_base_artifact_version: int | None,
        requested_base_handoff_version: int | None,
        requested_base_state: WorkflowState | None,
    ) -> ModelArtifact:
        workflow = await self.repository.get_workflow(workflow_id)
        base_artifact_version = (
            requested_base_artifact_version or workflow.active_artifact_version
        )
        base_handoff_version = (
            requested_base_handoff_version or workflow.active_handoff_version
        )
        if base_artifact_version is None:
            raise ConflictError("Workflow has no active artifact")
        artifact = await self.repository.get_artifact(
            workflow_id,
            base_artifact_version,
        )
        if artifact.project is not None and len(artifact.project.parts) > 1:
            part_ids = {part.id for part in artifact.project.parts}
            if allowed_part_ids is not None and not set(allowed_part_ids).issubset(part_ids):
                unknown = sorted(set(allowed_part_ids) - part_ids)
                raise ValidationError(f"Unknown edit-scope parts: {', '.join(unknown)}")
            reference_part_id = (
                allowed_part_ids[0] if allowed_part_ids else artifact.project.parts[0].id
            )
            part = next(
                (
                    candidate
                    for candidate in artifact.project.parts
                    if candidate.id == reference_part_id
                ),
                None,
            )
            assert part is not None
            adapter = await self._workflow_profile_adapter(workflow)
            printer = await adapter.capabilities()
            selected_source = SelectedSourceSummary(
                filename=f"{reference_part_id}.stl",
                candidate_id=artifact.provenance.candidate_id or workflow_id,
                file_id=reference_part_id,
                title=part.name,
                creator=artifact.provenance.creator or "unknown",
                license=artifact.provenance.license or "unknown",
                attribution_url=artifact.provenance.source_url or "local-artifact",
                source_digest=sha256_file(
                    artifact.model_path.parent / f"{reference_part_id}.stl"
                ),
                mesh=artifact.part_meshes[reference_part_id],
            )
            old_handoff = ModelingHandoff(
                workflow_id=workflow_id,
                version=max(workflow.active_handoff_version or 1, 1),
                requirement=workflow.requirement,
                model_plan=await self.repository.get_plan(workflow_id),
                decision=ModelDecision.MODIFY,
                selected_source=selected_source,
                target_printer=printer,
                discovery_rationale="Refine one retained source-set part.",
                evidence_digests=[selected_source.source_digest],
            ).with_digest()
            current_source = 'import("source.stl");\n'
        else:
            if allowed_part_ids is not None:
                part_ids = (
                    {part.id for part in artifact.project.parts}
                    if artifact.project is not None
                    else set()
                )
                if not set(allowed_part_ids).issubset(part_ids):
                    unknown = sorted(set(allowed_part_ids) - part_ids)
                    raise ValidationError(
                        f"Unknown edit-scope parts: {', '.join(unknown)}"
                    )
            if base_handoff_version is None:
                raise ConflictError("Current artifact was not produced by a modeling handoff")
            old_handoff = await self.repository.get_handoff(
                workflow_id,
                base_handoff_version,
            )
            if artifact.source_path is None or not artifact.source_path.is_file():
                raise ConflictError(
                    "This artifact has no editable OpenSCAD source; search for a new base instead"
                )
            current_source = unwrap_base_model_source(
                artifact.source_path.read_text(encoding="utf-8")
            )
        version = await self.repository.next_handoff_version(workflow_id)
        handoff = old_handoff.model_copy(
            update={
                "version": version,
                "required_changes": [*old_handoff.required_changes, feedback],
                "digest": None,
            }
        ).with_digest()
        await self.repository.save_handoff(handoff)
        await self.repository.patch_workflow(
            workflow_id,
            modeling_session_id=None,
            event_kind="revision.modeling_session_replaced",
            payload={
                "previous_handoff_version": old_handoff.version,
                "handoff_version": version,
                "artifact_version": artifact.version,
            },
        )
        await self.repository.transition(
            workflow_id,
            WorkflowState.HANDOFF_READY,
            event_kind="revision.handoff_ready",
            payload={"handoff_version": version},
        )
        await self.repository.transition(
            workflow_id,
            WorkflowState.GENERATING,
            event_kind="revision.modeling_started",
        )
        repair_feedback = feedback
        restore_state = (
            WorkflowState.APPROVED
            if requested_base_state == WorkflowState.APPROVED
            else WorkflowState.AWAITING_APPROVAL
        )

        async def rollback_execution_failure(
            error: PrintingAgentError,
            candidate_version: int,
        ) -> ModelArtifact:
            verification = RevisionVerification(
                workflow_id=workflow_id,
                handoff_version=handoff.version,
                base_artifact_version=artifact.version,
                candidate_artifact_version=candidate_version,
                feedback=feedback,
                verdict="failed",
                repairable=False,
                checks=[
                    RevisionVerificationCheck(
                        id="revision_execution",
                        passed=False,
                        repairable=False,
                        message=error.message[:2_000],
                        evidence={"error_code": error.code},
                    )
                ],
                rationale=f"The revision could not be completed: {error.message}"[
                    :4_000
                ],
            )
            await self.repository.rollback_failed_revision(
                verification,
                restore_handoff_version=base_handoff_version,
                restore_state=restore_state,
                expected_handoff_version=handoff.version,
                expected_active_artifact_version=artifact.version,
            )
            return artifact

        while True:
            try:
                candidate = await self.modeling.revise(
                    handoff,
                    artifact,
                    repair_feedback,
                    current_source,
                    old_handoff,
                    allowed_part_ids,
                    recorded_feedback=feedback,
                )
            except PrintingAgentError as exc:
                return await rollback_execution_failure(exc, artifact.version)
            except Exception as exc:
                return await rollback_execution_failure(
                    ExternalServiceError(
                        f"Unexpected modeling revision failure: {str(exc)[-1_940:]}"
                    ),
                    artifact.version,
                )
            try:
                verification = await self.revision_verifier.verify(
                    workflow_id=workflow_id,
                    handoff_version=handoff.version,
                    feedback=feedback,
                    base=artifact,
                    candidate=candidate,
                    allowed_part_ids=allowed_part_ids,
                )
            except PrintingAgentError as exc:
                return await rollback_execution_failure(exc, candidate.version)
            except Exception as exc:
                return await rollback_execution_failure(
                    ExternalServiceError(
                        f"Unexpected revision verifier failure: {str(exc)[-1_940:]}"
                    ),
                    candidate.version,
                )
            if verification.verdict == "passed":
                await self.repository.complete_verified_revision(verification)
                return candidate
            diagnostics = json.dumps(
                verification.model_dump(mode="json"),
                sort_keys=True,
            )[-8_000:]
            await self.repository.mark_latest_source_attempt_verification_rejected(
                workflow_id,
                handoff.version,
                diagnostics,
            )
            attempts = await self.repository.count_source_attempts(
                workflow_id,
                handoff.version,
            )
            if (
                verification.repairable
                and attempts < self.settings.generation_attempt_budget
            ):
                await self.repository.prepare_revision_verification_retry(verification)
                failed_reasons = [
                    check.message for check in verification.checks if not check.passed
                ]
                verifier_guidance = (
                    "Independent verifier findings to correct:\n- "
                    + "\n- ".join(failed_reasons)
                )
                repair_feedback = (
                    f"{feedback[:7_000]}\n\n{verifier_guidance}"
                )[:10_000]
                continue
            await self.repository.rollback_failed_revision(
                verification,
                restore_handoff_version=base_handoff_version,
                restore_state=restore_state,
                expected_handoff_version=handoff.version,
                expected_active_artifact_version=artifact.version,
            )
            return artifact

    async def request_revision(
        self,
        workflow_id: str,
        mode: RevisionMode,
        feedback: str,
        allowed_part_ids: list[str] | None = None,
    ) -> None:
        workflow = await self.repository.get_workflow(workflow_id)
        PrintingApplication._ensure_not_archived(workflow)
        if workflow.state not in {
            WorkflowState.AWAITING_APPROVAL,
            WorkflowState.APPROVED,
            WorkflowState.PREPARATION_FAILED,
            WorkflowState.SLICE_FAILED,
        }:
            raise ConflictError("Only an unsubmitted or failed workflow can be revised")
        if mode == RevisionMode.REFINE_CURRENT:
            if workflow.active_artifact_version is None:
                raise ConflictError("Workflow has no active artifact to refine")
            artifact = await self.repository.get_artifact(
                workflow_id,
                workflow.active_artifact_version,
            )
            if artifact.source_path is None or not artifact.source_path.is_file():
                raise ConflictError(
                    "This artifact has no editable OpenSCAD source; search for a new base instead"
                )
            if artifact.project is not None and len(artifact.project.parts) > 1:
                part_ids = {part.id for part in artifact.project.parts}
                if allowed_part_ids is not None and not set(allowed_part_ids).issubset(
                    part_ids
                ):
                    unknown = sorted(set(allowed_part_ids) - part_ids)
                    raise ValidationError(
                        f"Unknown edit-scope parts: {', '.join(unknown)}"
                    )
            elif allowed_part_ids is not None:
                part_ids = (
                    {part.id for part in artifact.project.parts}
                    if artifact.project is not None
                    else set()
                )
                if not set(allowed_part_ids).issubset(part_ids):
                    unknown = sorted(set(allowed_part_ids) - part_ids)
                    raise ValidationError(
                        f"Unknown edit-scope parts: {', '.join(unknown)}"
                    )
        revision = RevisionRequest(
            workflow_id=workflow_id,
            mode=mode,
            feedback=feedback,
            allowed_part_ids=allowed_part_ids,
        )
        await self.repository.request_revision(revision)

    async def approve(
        self,
        workflow_id: str,
        artifact_version: int,
        manifest_digest: str,
        approved_by: str,
    ) -> None:
        workflow = await self.repository.get_workflow(workflow_id)
        PrintingApplication._ensure_not_archived(workflow)
        if (
            workflow.state != WorkflowState.AWAITING_APPROVAL
            or workflow.active_artifact_version != artifact_version
        ):
            raise ConflictError("Approval references a stale or unavailable artifact")
        artifact = await self.repository.get_artifact(workflow_id, artifact_version)
        if artifact.manifest_digest != manifest_digest:
            raise ValidationError("Approval digest does not match the inspected artifact")
        adapter = await self._workflow_profile_adapter(workflow)
        plan = await self.repository.get_plan(workflow_id)
        await adapter.validate(artifact, plan.print_settings)
        await self.repository.approve_artifact(
            ArtifactApproval(
                workflow_id=workflow_id,
                artifact_version=artifact_version,
                manifest_digest=manifest_digest,
                approved_by=approved_by,
            )
        )

    async def revise_slicing_configuration(
        self,
        workflow_id: str,
        overrides: JobOverrides,
        *,
        expected_configuration_revision: int,
        expected_snapshot_digest: str,
        created_by: str,
    ) -> MaterialAssignment:
        await self.repository.get_workflow(workflow_id)
        async with self._slicing_operation_lock(workflow_id):
            workflow = await self.repository.get_workflow(workflow_id)
            PrintingApplication._ensure_not_archived(workflow)
            current = await self.repository.get_workflow_printer_snapshot(
                workflow_id
            )
            if (
                current.configuration_revision
                != expected_configuration_revision
                or current.digest != expected_snapshot_digest
            ):
                raise ConflictError(
                    "Slicing settings changed; refresh the workspace and retry"
                )
            profile = await self.repository.get_printer_profile(
                current.profile_id,
                current.profile_revision,
            )
            self._validate_job_overrides(profile, overrides)
            if overrides == current.overrides:
                raise ConflictError("No slicing settings changed")
            try:
                handoff = await self.repository.get_latest_bambu_connect_handoff(
                    workflow_id
                )
            except NotFoundError:
                handoff = None
            if (
                handoff is not None
                and handoff.status
                not in {
                    BambuConnectHandoffStatus.COMPLETED,
                    BambuConnectHandoffStatus.FAILED,
                    BambuConnectHandoffStatus.TIMED_OUT,
                    BambuConnectHandoffStatus.CANCELLED,
                }
            ):
                await self._stop_bambu_connect_monitoring_unlocked(workflow_id)
            reason = (
                "post_slice_revision"
                if workflow.state
                in {
                    WorkflowState.AWAITING_SLICE_REVIEW,
                    WorkflowState.SLICE_FAILED,
                }
                else "settings_applied"
            )
            revised = current.model_copy(
                update={
                    "configuration_revision": (
                        current.configuration_revision + 1
                    ),
                    "revision_reason": reason,
                    "created_by": created_by,
                    "overrides": overrides,
                    "resolved_slot_policy": resolve_slot_policy(
                        profile.spec,
                        overrides,
                    ),
                    "digest": None,
                    "created_at": fabrication_utc_now(),
                }
            ).with_digest()
            await self.repository.revise_workflow_printer_snapshot(
                revised,
                expected_configuration_revision=(
                    expected_configuration_revision
                ),
                expected_digest=expected_snapshot_digest,
            )
            await self.refresh_cloud_snapshot(workflow_id)
            return await self._propose_material_assignment_unlocked(workflow_id)

    async def propose_material_assignment(
        self,
        workflow_id: str,
    ) -> MaterialAssignment:
        await self.repository.get_workflow(workflow_id)
        async with self._slicing_operation_lock(workflow_id):
            return await self._propose_material_assignment_unlocked(workflow_id)

    async def _propose_material_assignment_unlocked(
        self,
        workflow_id: str,
    ) -> MaterialAssignment:
        workflow = await self.repository.get_workflow(workflow_id)
        PrintingApplication._ensure_not_archived(workflow)
        if workflow.state != WorkflowState.SLICE_SETUP:
            raise ConflictError(
                "Refresh a complete cloud device snapshot before assigning materials"
            )
        snapshot = await self.repository.get_workflow_printer_snapshot(workflow_id)
        try:
            latest_job = await self.repository.get_latest_slice_job(workflow_id)
        except NotFoundError:
            latest_job = None
        if (
            latest_job is not None
            and latest_job.printer_snapshot_digest == snapshot.digest
            and latest_job.material_assessment is not None
            and latest_job.material_assessment.status != "sufficient"
        ):
            raise ConflictError(
                "Resolve the active sliced-usage material recovery before "
                "creating a new assignment"
            )
        if workflow.active_artifact_version is None:
            raise ConflictError("Workflow has no active artifact")
        artifact = await self.repository.get_artifact(
            workflow_id,
            workflow.active_artifact_version,
        )
        profile = await self.repository.get_printer_profile(
            snapshot.profile_id,
            snapshot.profile_revision,
        )
        cloud_snapshot = (
            await self.repository.get_latest_cloud_device_snapshot(
                workflow_id,
                snapshot.digest,
            )
        )
        if fabrication_utc_now() >= cloud_snapshot.expires_at:
            cloud_snapshot = await self.refresh_cloud_snapshot(
                workflow_id,
            )
        spools, materials = await self._observed_cloud_spools(
            profile,
            cloud_snapshot,
        )
        plan = await self.repository.get_plan(workflow_id)
        assignment = await self.material_assignment.propose(
            workflow_id=workflow_id,
            artifact=artifact,
            profile=profile,
            printer_snapshot_digest=snapshot.digest or "0" * 64,
            cloud_snapshot_id=cloud_snapshot.id,
            cloud_snapshot_digest=cloud_snapshot.digest,
            policy=snapshot.resolved_slot_policy,
            overrides=snapshot.overrides,
            spools=spools,
            materials=materials,
            maximum_color_distance=snapshot.overrides.maximum_color_distance,
            default_material_family=plan.print_settings.material,
        )
        assignment = assignment.model_copy(
            update={"requires_confirmation": True, "digest": None}
        ).with_digest()
        await self.repository.save_material_assignment(assignment)
        await self.repository.transition(
            workflow_id,
            WorkflowState.AWAITING_MATERIAL_REVIEW,
            event_kind="material.assignment_ready",
            payload={
                "assignment_id": assignment.id,
                "cloud_snapshot_digest": cloud_snapshot.digest,
            },
        )
        return assignment

    def _slicing_operation_lock(self, workflow_id: str) -> asyncio.Lock:
        locks = getattr(self, "_slicing_operation_locks", None)
        if locks is None:
            locks = weakref.WeakValueDictionary()
            self._slicing_operation_locks = locks
        return locks.setdefault(workflow_id, asyncio.Lock())

    async def prepare_slicing_materials(
        self,
        workflow_id: str,
    ) -> MaterialAssignment:
        await self.repository.get_workflow(workflow_id)
        async with self._slicing_operation_lock(workflow_id):
            workflow = await self.repository.get_workflow(workflow_id)
            PrintingApplication._ensure_not_archived(workflow)
            if workflow.state == WorkflowState.AWAITING_MATERIAL_REVIEW:
                return await self.repository.get_latest_material_assignment(workflow_id)
            if workflow.state == WorkflowState.APPROVED:
                await self.refresh_cloud_snapshot(workflow_id)
                workflow = await self.repository.get_workflow(workflow_id)
            if workflow.state != WorkflowState.SLICE_SETUP:
                raise ConflictError(
                    "Slicing material preparation is unavailable in the current state"
                )
            return await self._propose_material_assignment_unlocked(workflow_id)

    async def confirm_materials_and_request_slice(
        self,
        workflow_id: str,
        assignment_id: str,
        confirmed_by: str,
        spool_overrides: dict[str, str] | None = None,
    ) -> SliceJob:
        await self.repository.get_workflow(workflow_id)
        async with self._slicing_operation_lock(workflow_id):
            await self._confirm_material_assignment_unlocked(
                workflow_id,
                assignment_id,
                confirmed_by,
                spool_overrides,
            )
            return await self._request_slice_unlocked(workflow_id)

    async def _observed_cloud_spools(
        self,
        profile: PrinterProfileRevision,
        cloud_snapshot: CloudDeviceSnapshot,
    ) -> tuple[
        list[PhysicalSpool],
        dict[tuple[str, int], MaterialDefinitionRevision],
    ]:
        definitions = await self.repository.list_material_definitions()
        by_cloud_id: dict[str, list] = {}
        for definition in definitions:
            if (
                definition.spec.mapping_origin != "manual"
                and not self._get_filament_catalog().profile_is_compatible(
                    definition.spec.source_profile_id,
                    profile,
                )
            ):
                continue
            for cloud_id in definition.spec.cloud_filament_ids:
                by_cloud_id.setdefault(cloud_id, []).append(definition)
        slot_ids = {slot.id for slot in profile.spec.material_slots}
        trays = [
            tray
            for unit in cloud_snapshot.ams_units
            for tray in unit.trays
            if tray.material is not None
        ]
        trays.extend(
            tray
            for tray in cloud_snapshot.external_trays
            if tray.material is not None
        )
        spools: list[PhysicalSpool] = []
        materials: dict[tuple[str, int], MaterialDefinitionRevision] = {}
        device_ref = self._device_ref(cloud_snapshot.device.device_id)
        authorizations = {
            item.slot_id: item
            for item in await self.repository.list_unknown_quantity_authorizations(
                profile.profile_id,
                device_ref,
            )
        }
        for tray in trays:
            if tray.slot_id not in slot_ids:
                raise ConflictError(
                    f"Observed cloud slot '{tray.slot_id}' is not defined "
                    "by the slicing profile"
                )
            measured_quantity = (
                tray.nominal_tray_weight_g is not None
                and tray.estimated_remaining_g is not None
            )
            if not tray.material_profile_id and measured_quantity:
                raise ConflictError(
                    f"Observed slot '{tray.slot_id}' has no Bambu filament identifier"
                )
            if not tray.material_profile_id:
                continue
            matches = by_cloud_id.get(tray.material_profile_id, [])
            manual_matches = [
                item for item in matches if item.spec.mapping_origin == "manual"
            ]
            if len(manual_matches) == 1:
                matches = manual_matches
            if len(matches) != 1:
                continue
            material = matches[0]
            tray_identity_digest = self._tray_identity_digest(
                cloud_snapshot.device.device_id,
                tray,
            )
            authorization = authorizations.get(tray.slot_id)
            authorization_active = (
                authorization is not None
                and authorization.tray_identity_digest == tray_identity_digest
            )
            quantity_status = (
                SpoolQuantityStatus.CLOUD_ESTIMATE
                if measured_quantity
                else SpoolQuantityStatus.USER_ATTESTED_UNKNOWN
                if authorization_active
                else SpoolQuantityStatus.UNKNOWN
            )
            spool_id = (
                "cloud-"
                + hashlib.sha256(
                    (
                        f"{cloud_snapshot.device.device_id}:{tray.slot_id}"
                    ).encode()
                ).hexdigest()[:24]
            )
            spool = PhysicalSpool(
                id=spool_id,
                material_id=material.material_id,
                material_revision=material.revision,
                material_digest=material.digest or "0" * 64,
                initial_weight_g=(
                    tray.nominal_tray_weight_g if measured_quantity else None
                ),
                remaining_weight_g=(
                    tray.estimated_remaining_g if measured_quantity else None
                ),
                quantity_status=quantity_status,
                tray_identity_digest=tray_identity_digest,
                status=SpoolStatus.LOADED,
                location=f"cloud snapshot {cloud_snapshot.id}",
                printer_profile_id=profile.profile_id,
                slot_id=tray.slot_id,
                measured_color=(
                    tray.color
                    if tray.color and tray.color.startswith("#")
                    else None
                ),
                cloud_snapshot_digest=cloud_snapshot.digest,
            )
            if quantity_status != SpoolQuantityStatus.UNKNOWN:
                await self.repository.save_spool(spool)
            spools.append(spool)
            materials[(material.material_id, material.revision)] = material
        if not spools:
            raise ConflictError("Cloud snapshot contains no usable loaded material")
        return spools, materials

    async def confirm_material_assignment(
        self,
        workflow_id: str,
        assignment_id: str,
        confirmed_by: str,
        spool_overrides: dict[str, str] | None = None,
    ) -> MaterialAssignment:
        await self.repository.get_workflow(workflow_id)
        async with self._slicing_operation_lock(workflow_id):
            return await self._confirm_material_assignment_unlocked(
                workflow_id,
                assignment_id,
                confirmed_by,
                spool_overrides,
            )

    async def _confirm_material_assignment_unlocked(
        self,
        workflow_id: str,
        assignment_id: str,
        confirmed_by: str,
        spool_overrides: dict[str, str] | None = None,
    ) -> MaterialAssignment:
        workflow = await self.repository.get_workflow(workflow_id)
        if workflow.state != WorkflowState.AWAITING_MATERIAL_REVIEW:
            raise ConflictError("Workflow is not awaiting material review")
        assignment = await self.repository.get_latest_material_assignment(workflow_id)
        if assignment.id != assignment_id:
            raise ConflictError("Material assignment is stale")
        try:
            latest_job = await self.repository.get_latest_slice_job(workflow_id)
            assessment = (
                latest_job.material_assessment
                if latest_job.printer_snapshot_digest
                == assignment.printer_snapshot_digest
                else None
            )
        except NotFoundError:
            assessment = None
        if assessment is not None and assessment.status != "sufficient":
            if (
                assessment.status != "replacement_proposed"
                or assignment.usage_basis != "sliced_usage"
                or assignment.source_assessment_digest != assessment.digest
            ):
                raise ConflictError(
                    "Load a sufficient compatible spool and refresh inventory "
                    "before confirming material recovery"
                )
        if spool_overrides:
            assignment = self.material_assignment.apply_user_overrides(
                assignment,
                spool_overrides,
            )
        assignment = self._normalize_assignment_toolheads(assignment)
        await self._ensure_assignment_materials_current(workflow, assignment)
        printer_snapshot = (
            await self.repository.get_workflow_printer_snapshot(workflow_id)
        )
        profile = await self.repository.get_printer_profile(
            printer_snapshot.profile_id,
            printer_snapshot.profile_revision,
        )
        cloud_snapshot = (
            await self.repository.get_latest_cloud_device_snapshot(
                workflow_id,
                printer_snapshot.digest,
            )
        )
        await self._ensure_unknown_quantity_authorizations_current(
            profile,
            cloud_snapshot,
            assignment,
        )
        if assignment.usage_basis == "sliced_usage":
            self._validate_recovery_assignment_capacity(
                assignment,
                printer_snapshot.overrides.material_safety_margin_percent,
            )
        confirmed = assignment.model_copy(
            update={
                "confirmed_by": confirmed_by,
                "confirmed_at": fabrication_utc_now(),
                "digest": None,
            }
        ).with_digest()
        await self.repository.save_material_assignment(confirmed)
        if assessment is not None and assessment.status == "replacement_proposed":
            sufficient = assessment.model_copy(
                update={"status": "sufficient", "digest": None}
            ).with_digest()
            await self.repository.save_slice_job(
                latest_job.model_copy(
                    update={
                        "material_assessment": sufficient,
                        "message": "Recovered material assignment confirmed",
                        "updated_at": fabrication_utc_now(),
                    }
                )
            )
        return confirmed

    async def _ensure_unknown_quantity_authorizations_current(
        self,
        profile: PrinterProfileRevision,
        snapshot: CloudDeviceSnapshot,
        assignment: MaterialAssignment,
    ) -> None:
        selected_slots = {
            item.slot_id
            for item in assignment.assignments
            if item.quantity_status
            == SpoolQuantityStatus.USER_ATTESTED_UNKNOWN
        }
        if not selected_slots:
            return
        active_slots = {
            str(item["slot_id"])
            for item in await self.unknown_quantity_authorization_states(
                profile,
                snapshot,
            )
            if item["status"] == "authorized_unknown"
        }
        invalid = sorted(selected_slots - active_slots)
        if invalid:
            raise ConflictError(
                "Quantity confirmation is no longer valid for: "
                + ", ".join(invalid)
                + ". Review material assignment again."
            )

    @staticmethod
    def _normalize_assignment_toolheads(
        assignment: MaterialAssignment,
    ) -> MaterialAssignment:
        candidates = {
            (part_id, item.spool_id): item
            for part_id, values in assignment.candidate_options.items()
            for item in values
        }
        by_spool: dict[str, list[PartMaterialAssignment]] = {}
        for item in assignment.assignments:
            by_spool.setdefault(item.spool_id, []).append(item)
        normalized: list[PartMaterialAssignment] = []
        for spool_id, items in by_spool.items():
            common: set[str] | None = None
            for item in items:
                candidate = candidates.get((item.part_id, spool_id))
                if candidate is None:
                    raise ValidationError(
                        f"Material selection for '{item.part_id}' is unavailable"
                    )
                common = (
                    set(candidate.toolhead_ids)
                    if common is None
                    else common & candidate.toolhead_ids
                )
            if not common:
                raise ValidationError(
                    f"Spool '{spool_id}' has no common compatible toolhead"
                )
            preferred = next(
                (item.toolhead_id for item in items if item.toolhead_id in common),
                sorted(common)[0],
            )
            normalized.extend(
                item.model_copy(update={"toolhead_id": preferred})
                for item in items
            )
        normalized.sort(
            key=lambda item: next(
                index
                for index, current in enumerate(assignment.assignments)
                if current.part_id == item.part_id
            )
        )
        return assignment.model_copy(
            update={"assignments": normalized, "digest": None}
        ).with_digest()

    @staticmethod
    def _validate_recovery_assignment_capacity(
        assignment: MaterialAssignment,
        safety_margin_percent: float,
    ) -> None:
        requests = {
            item.part_id: item for item in assignment.requests
        }
        candidate_by_spool = {
            (part_id, candidate.spool_id): candidate
            for part_id, values in assignment.candidate_options.items()
            for candidate in values
        }
        required_by_spool: dict[str, float] = {}
        capacity_by_spool: dict[str, float | None] = {}
        margin = 1 + safety_margin_percent / 100
        for selected in assignment.assignments:
            candidate = candidate_by_spool.get(
                (selected.part_id, selected.spool_id)
            )
            if candidate is None:
                raise ValidationError(
                    f"Recovery selection for '{selected.part_id}' is no longer available"
                )
            required_by_spool[selected.spool_id] = (
                required_by_spool.get(selected.spool_id, 0)
                + float(requests[selected.part_id].estimated_weight_g or 0)
                * margin
            )
            capacity_by_spool[selected.spool_id] = candidate.remaining_weight_g
        insufficient = [
            spool_id
            for spool_id, required in required_by_spool.items()
            if capacity_by_spool[spool_id] is not None
            and required > cast(float, capacity_by_spool[spool_id])
        ]
        if insufficient:
            raise ValidationError(
                "Recovery selection exceeds available material for: "
                + ", ".join(sorted(insufficient))
            )

    async def request_slice(self, workflow_id: str) -> SliceJob:
        await self.repository.get_workflow(workflow_id)
        async with self._slicing_operation_lock(workflow_id):
            return await self._request_slice_unlocked(workflow_id)

    async def _request_slice_unlocked(self, workflow_id: str) -> SliceJob:
        workflow = await self.repository.get_workflow(workflow_id)
        PrintingApplication._ensure_not_archived(workflow)
        if workflow.state not in {
            WorkflowState.AWAITING_MATERIAL_REVIEW,
            WorkflowState.SLICE_FAILED,
            WorkflowState.AWAITING_SLICE_REVIEW,
        }:
            raise ConflictError("Workflow is not ready for slicing or reslicing")
        if workflow.state == WorkflowState.AWAITING_SLICE_REVIEW:
            try:
                existing_handoff = (
                    await self.repository.get_latest_bambu_connect_handoff(
                        workflow_id
                    )
                )
            except NotFoundError:
                existing_handoff = None
            if (
                existing_handoff is not None
                and existing_handoff.status
                not in {
                    BambuConnectHandoffStatus.COMPLETED,
                    BambuConnectHandoffStatus.FAILED,
                    BambuConnectHandoffStatus.TIMED_OUT,
                    BambuConnectHandoffStatus.CANCELLED,
                }
            ):
                await self.repository.save_bambu_connect_handoff(
                    existing_handoff.model_copy(
                        update={
                            "status": BambuConnectHandoffStatus.CANCELLED,
                            "message": (
                                "Handoff cancelled because a new slice was requested."
                            ),
                            "updated_at": fabrication_utc_now(),
                        }
                    )
                )
        if workflow.active_artifact_version is None:
            raise ConflictError("Workflow has no active artifact")
        artifact = await self.repository.get_artifact(
            workflow_id,
            workflow.active_artifact_version,
        )
        snapshot = await self.repository.get_workflow_printer_snapshot(workflow_id)
        profile = await self.repository.get_printer_profile(
            snapshot.profile_id,
            snapshot.profile_revision,
        )
        assignment = await self.repository.get_latest_material_assignment(workflow_id)
        await self._ensure_assignment_materials_current(workflow, assignment)
        try:
            previous_job = await self.repository.get_latest_slice_job(workflow_id)
            active_assessment = previous_job.material_assessment
        except NotFoundError:
            active_assessment = None
        if active_assessment is not None and active_assessment.status != "sufficient":
            raise ConflictError(
                "Material recovery must be confirmed before requesting another slice"
            )
        if assignment.confirmed_at is None:
            raise ConflictError("Material assignment requires user confirmation")
        if (
            assignment.artifact_version != artifact.version
            or assignment.artifact_manifest_digest != artifact.manifest_digest
            or assignment.printer_snapshot_digest != snapshot.digest
        ):
            raise ConflictError("Material assignment no longer matches the workflow")
        selected_device = profile.spec.cloud_device_serial
        if not selected_device:
            raise ConflictError("Slicing profile has no bound cloud H2D device")
        try:
            fresh_cloud_snapshot = await self.inventory.snapshot(selected_device)
        except CloudInventoryError as exc:
            raise ExternalServiceError(str(exc)) from exc
        self._validate_cloud_snapshot(profile, fresh_cloud_snapshot)
        await self.repository.save_cloud_device_snapshot(
            workflow_id,
            profile.profile_id,
            fresh_cloud_snapshot,
            snapshot.digest,
        )
        if fresh_cloud_snapshot.digest != assignment.cloud_snapshot_digest:
            if workflow.state != WorkflowState.SLICE_SETUP:
                await self.repository.transition(
                    workflow_id,
                    WorkflowState.SLICE_SETUP,
                    event_kind="cloud.inventory_changed",
                    payload={
                        "previous_digest": assignment.cloud_snapshot_digest,
                        "current_digest": fresh_cloud_snapshot.digest,
                    },
                )
            raise ConflictError(
                "Cloud printer or AMS inventory changed; review materials again"
            )
        await self._ensure_unknown_quantity_authorizations_current(
            profile,
            fresh_cloud_snapshot,
            assignment,
        )
        idempotency_key = hashlib.sha256(
            (
                f"{workflow_id}:{artifact.manifest_digest}:{snapshot.digest}:"
                f"{assignment.digest}:{workflow.version}"
            ).encode()
        ).hexdigest()
        job = SliceJob(
            workflow_id=workflow_id,
            artifact_version=artifact.version,
            artifact_manifest_digest=artifact.manifest_digest,
            printer_snapshot_digest=snapshot.digest or "0" * 64,
            cloud_snapshot_digest=fresh_cloud_snapshot.digest,
            material_assignment_digest=assignment.digest or "0" * 64,
            slicer_driver_id=snapshot.profile.slicer.driver_id,
            material_safety_margin_percent=(
                snapshot.overrides.material_safety_margin_percent
            ),
            recovery_round=assignment.recovery_round,
            idempotency_key=idempotency_key,
        )
        requested_weights = {
            request.part_id: request.estimated_weight_g
            for request in assignment.requests
        }
        if any(weight is None or weight <= 0 for weight in requested_weights.values()):
            raise ConflictError(
                "Every material assignment requires a positive pre-slice weight estimate"
            )
        reservation_weights: dict[str, float] = {}
        for part_assignment in assignment.assignments:
            reservation_weights[part_assignment.spool_id] = (
                reservation_weights.get(part_assignment.spool_id, 0)
                + float(requested_weights[part_assignment.part_id])
                * (
                    1
                    + snapshot.overrides.material_safety_margin_percent
                    / 100
                )
            )
        reservations = []
        for spool_id, weight in reservation_weights.items():
            spool = await self.repository.get_spool(spool_id)
            if spool.cloud_snapshot_digest != fresh_cloud_snapshot.digest:
                raise ConflictError(
                    f"Observed spool '{spool_id}' is stale; refresh materials"
                )
            reservations.append(
                SpoolReservation(
                    workflow_id=workflow_id,
                    slice_job_id=job.id,
                    spool_id=spool_id,
                    reserved_weight_g=weight,
                    remaining_weight_snapshot_g=spool.remaining_weight_g,
                    quantity_status=spool.quantity_status,
                )
            )
        await self.repository.enqueue_slice(job, reservations)
        return job

    async def _ensure_assignment_materials_current(
        self,
        workflow: PrintWorkflow,
        assignment: MaterialAssignment,
    ) -> None:
        if await self._assignment_materials_are_current(assignment):
            return
        if workflow.state != WorkflowState.SLICE_SETUP and workflow.state in {
            WorkflowState.AWAITING_MATERIAL_REVIEW,
            WorkflowState.SLICE_FAILED,
            WorkflowState.AWAITING_SLICE_REVIEW,
        }:
            await self.repository.transition(
                workflow.id,
                WorkflowState.SLICE_SETUP,
                event_kind="material.assignment_stale",
                payload={"assignment_id": assignment.id},
            )
        raise ConflictError(
            "Material mapping changed; refresh inventory and recommend materials again"
        )

    async def _assignment_materials_are_current(
        self,
        assignment: MaterialAssignment,
    ) -> bool:
        active = {
            (item.material_id, item.revision): item.digest
            for item in await self.repository.list_material_definitions()
        }
        return all(
            active.get((item.material_id, item.material_revision))
            == item.material_digest
            for item in assignment.assignments
        )

    async def _workflow_material_assignment_stale(
        self,
        workflow_id: str,
    ) -> bool:
        try:
            assignment = await self.repository.get_latest_material_assignment(
                workflow_id
            )
        except NotFoundError:
            return False
        return not await self._assignment_materials_are_current(assignment)

    async def slice_workflow(self, workflow_id: str) -> None:
        job = await self.repository.get_latest_slice_job(workflow_id)
        workspace = None
        try:
            job = job.model_copy(
                update={
                    "status": SliceJobStatus.SLICING,
                    "updated_at": fabrication_utc_now(),
                    "message": "Bambu Studio slicing started",
                }
            )
            await self.repository.save_slice_job(job)
            await self.repository.transition(
                workflow_id,
                WorkflowState.SLICING,
                event_kind="slice.started",
                payload={"slice_job_id": job.id},
            )
            artifact = await self.repository.get_artifact(
                workflow_id,
                job.artifact_version,
            )
            snapshot = await self.repository.get_workflow_printer_snapshot(workflow_id)
            assignment = await self.repository.get_latest_material_assignment(workflow_id)
            if assignment.digest != job.material_assignment_digest:
                raise ConflictError("Slice job material assignment is stale")
            driver = self.slicers.get(job.slicer_driver_id)
            workspace = self.settings.slice_dir / workflow_id / job.id
            sliced = await driver.slice(
                SliceRequest(
                    job=job,
                    artifact=artifact,
                    printer=snapshot,
                    material_assignment=assignment,
                    workspace=workspace,
                )
            )
            if any(
                item.quantity_status
                == SpoolQuantityStatus.USER_ATTESTED_UNKNOWN
                for item in assignment.assignments
            ):
                sliced = sliced.model_copy(
                    update={
                        "warnings": [
                            *sliced.warnings,
                            (
                                "One or more selected spools have unknown quantity; "
                                "sufficient filament was user-confirmed."
                            ),
                        ]
                    }
                )
            reconciliation = await self.repository.reconcile_spool_reservations(
                job.id,
                sliced.filament_usage_g,
                job.material_safety_margin_percent,
            )
            if reconciliation.shortages:
                await self._recover_insufficient_material(
                    job,
                    artifact,
                    snapshot,
                    assignment,
                    reconciliation,
                )
                if workspace is not None:
                    await asyncio.to_thread(shutil.rmtree, workspace, True)
                return
            job = job.model_copy(
                update={
                    "status": SliceJobStatus.VALIDATING,
                    "updated_at": fabrication_utc_now(),
                    "message": "Validating sliced G-code 3MF",
                }
            )
            await self.repository.save_slice_job(job)
            await self.repository.transition(
                workflow_id,
                WorkflowState.SLICE_VALIDATING,
                event_kind="slice.validating",
                payload={"slice_job_id": job.id},
            )
            ready_job = job.model_copy(
                update={
                    "status": SliceJobStatus.READY,
                    "updated_at": fabrication_utc_now(),
                    "message": "Sliced job is ready for review",
                }
            )
            await self.repository.finalize_slice(ready_job, sliced)
        except Exception as exc:
            try:
                await self.repository.fail_slice(job, str(exc)[-2_000:])
            finally:
                if workspace is not None:
                    await asyncio.to_thread(
                        shutil.rmtree,
                        workspace,
                        True,
                    )

    async def _recover_insufficient_material(
        self,
        job: SliceJob,
        artifact: ModelArtifact,
        printer_snapshot: WorkflowPrinterSnapshot,
        assignment: MaterialAssignment,
        reconciliation: SpoolReconciliationResult,
    ) -> None:
        recovery_round = assignment.recovery_round + 1
        parts_by_spool: dict[str, list[str]] = {}
        for item in assignment.assignments:
            parts_by_spool.setdefault(item.spool_id, []).append(item.part_id)
        requirements = tuple(
            item.model_copy(
                update={
                    "affected_part_ids": tuple(
                        sorted(parts_by_spool.get(item.spool_id, []))
                    )
                }
            )
            for item in reconciliation.requirements
        )
        status = (
            "manual_intervention_required"
            if recovery_round > 3
            else "replacement_proposed"
        )
        assessment = SliceMaterialAssessment(
            slice_job_id=job.id,
            material_assignment_digest=assignment.digest or "0" * 64,
            cloud_snapshot_digest=job.cloud_snapshot_digest,
            recovery_round=min(recovery_round, 3),
            requirements=requirements,
            status=status,
        ).with_digest()
        failed_job = job.model_copy(
            update={
                "status": SliceJobStatus.FAILED,
                "message": "Actual sliced usage requires material reassignment",
                "failure_category": "insufficient_material",
                "recovery_round": min(recovery_round, 3),
                "material_assessment": assessment,
                "updated_at": fabrication_utc_now(),
            }
        )
        await self.repository.fail_slice(failed_job, failed_job.message or "")
        if recovery_round > 3:
            await self._save_blocked_recovery_assignment(
                failed_job,
                assignment,
                assessment,
                "Manual intervention required after three recovery rounds.",
            )
            return
        try:
            fresh_snapshot = await self.refresh_cloud_snapshot(job.workflow_id)
            profile = await self.repository.get_printer_profile(
                printer_snapshot.profile_id,
                printer_snapshot.profile_revision,
            )
            spools, materials = await self._observed_cloud_spools(
                profile,
                fresh_snapshot,
            )
            available = await self.repository.available_spool_weights(
                {item.id for item in spools}
            )
            spools = [
                item.model_copy(
                    update={
                        "remaining_weight_g": available.get(
                            item.id,
                            item.remaining_weight_g,
                        )
                    }
                )
                for item in spools
            ]
            recovered = self._build_usage_recovery_assignment(
                assignment,
                assessment,
                profile,
                printer_snapshot,
                fresh_snapshot,
                spools,
                materials,
            )
        except PrintingAgentError as exc:
            blocked = assessment.model_copy(
                update={
                    "status": "load_required",
                    "digest": None,
                }
            ).with_digest()
            failed_job = failed_job.model_copy(
                update={
                    "material_assessment": blocked,
                    "message": str(exc)[-2_000:],
                    "updated_at": fabrication_utc_now(),
                }
            )
            await self.repository.save_slice_job(failed_job)
            await self._save_blocked_recovery_assignment(
                failed_job,
                assignment,
                blocked,
                str(exc),
            )
            return
        failed_job = failed_job.model_copy(
            update={
                "material_assessment": assessment,
                "updated_at": fabrication_utc_now(),
            }
        )
        await self.repository.save_slice_job(failed_job)
        workflow = await self.repository.get_workflow(job.workflow_id)
        if workflow.state == WorkflowState.SLICE_FAILED:
            await self.repository.transition(
                job.workflow_id,
                WorkflowState.SLICE_SETUP,
                event_kind="material.recovery_started",
                payload={
                    "slice_job_id": job.id,
                    "assessment_digest": assessment.digest,
                    "recovery_round": recovery_round,
                },
            )
        await self.repository.save_material_assignment(recovered)
        await self.repository.transition(
            job.workflow_id,
            WorkflowState.AWAITING_MATERIAL_REVIEW,
            event_kind="material.recovery_proposed",
            payload={
                "assignment_id": recovered.id,
                "assessment_digest": assessment.digest,
                "recovery_round": recovery_round,
            },
        )

    def _build_usage_recovery_assignment(
        self,
        assignment: MaterialAssignment,
        assessment: SliceMaterialAssessment,
        profile: PrinterProfileRevision,
        printer_snapshot: WorkflowPrinterSnapshot,
        cloud_snapshot: CloudDeviceSnapshot,
        spools: list[PhysicalSpool],
        materials: dict[tuple[str, int], MaterialDefinitionRevision],
    ) -> MaterialAssignment:
        requests = {item.part_id: item for item in assignment.requests}
        selected = {item.part_id: item for item in assignment.assignments}
        requirement_by_spool = {
            item.spool_id: item for item in assessment.requirements
        }
        allocated_usage: dict[str, float] = {}
        recovered_requests: list[PartMaterialRequest] = []
        for spool_id, requirement in requirement_by_spool.items():
            part_ids = [
                item.part_id
                for item in assignment.assignments
                if item.spool_id == spool_id
            ]
            total_estimate = sum(
                float(requests[part_id].estimated_weight_g or 0)
                for part_id in part_ids
            )
            for index, part_id in enumerate(part_ids):
                if index + 1 == len(part_ids):
                    used = requirement.actual_usage_g - sum(
                        allocated_usage.get(value, 0) for value in part_ids
                    )
                else:
                    estimate = float(requests[part_id].estimated_weight_g or 0)
                    used = (
                        requirement.actual_usage_g * estimate / total_estimate
                        if total_estimate > 0
                        else requirement.actual_usage_g / max(1, len(part_ids))
                    )
                allocated_usage[part_id] = max(0.001, used)
        for request in assignment.requests:
            recovered_requests.append(
                request.model_copy(
                    update={
                        "estimated_weight_g": allocated_usage.get(
                            request.part_id,
                            request.estimated_weight_g,
                        )
                    }
                )
            )
        candidates = self.material_assignment.compatible_candidates(
            requests=recovered_requests,
            profile=profile,
            policy=printer_snapshot.resolved_slot_policy,
            overrides=printer_snapshot.overrides,
            spools=spools,
            materials=materials,
        )
        margin = 1 + printer_snapshot.overrides.material_safety_margin_percent / 100
        sufficient_existing = {
            item.spool_id
            for item in assessment.requirements
            if item.shortfall_g == 0
        }
        required_by_part = {
            item.part_id: float(item.estimated_weight_g or 0) * margin
            for item in recovered_requests
        }
        ordered_requests = sorted(
            recovered_requests,
            key=lambda item: (
                len(candidates[item.part_id]),
                -required_by_part[item.part_id],
                item.part_id,
            ),
        )
        search_budget = 10_000
        searched = 0

        def solve(
            index: int,
            allocated: dict[str, float],
            toolhead_by_spool: dict[str, str],
            chosen: dict[str, tuple[MaterialCandidate, str]],
        ) -> dict[str, tuple[MaterialCandidate, str]] | None:
            nonlocal searched
            searched += 1
            if searched > search_budget:
                return None
            if index == len(ordered_requests):
                return chosen
            request = ordered_requests[index]
            required = required_by_part[request.part_id]
            options = sorted(
                candidates[request.part_id],
                key=lambda candidate: (
                    0
                    if candidate.spool_id in allocated
                    or candidate.spool_id in sufficient_existing
                    else 1,
                    candidate.color_distance,
                    bool(candidate.warnings),
                    candidate.slot_id,
                ),
            )
            for candidate in options:
                total = allocated.get(candidate.spool_id, 0) + required
                if (
                    candidate.remaining_weight_g is not None
                    and total > candidate.remaining_weight_g
                ):
                    continue
                existing_toolhead = toolhead_by_spool.get(candidate.spool_id)
                if existing_toolhead is not None:
                    toolhead_options = (
                        [existing_toolhead]
                        if existing_toolhead in candidate.toolhead_ids
                        else []
                    )
                else:
                    previous = selected[request.part_id].toolhead_id
                    toolhead_options = sorted(
                        candidate.toolhead_ids,
                        key=lambda value: (value != previous, value),
                    )
                for toolhead_id in toolhead_options:
                    result = solve(
                        index + 1,
                        {**allocated, candidate.spool_id: total},
                        {**toolhead_by_spool, candidate.spool_id: toolhead_id},
                        {**chosen, request.part_id: (candidate, toolhead_id)},
                    )
                    if result is not None:
                        return result
            return None

        chosen = solve(0, {}, {}, {})
        if chosen is None:
            raise ConflictError(
                "No loaded compatible spool combination can satisfy actual sliced usage"
            )
        recovered_assignments: list[PartMaterialAssignment] = []
        for request in recovered_requests:
            candidate, toolhead_id = chosen[request.part_id]
            recovered_assignments.append(
                PartMaterialAssignment(
                    part_id=request.part_id,
                    spool_id=candidate.spool_id,
                    slot_id=candidate.slot_id,
                    toolhead_id=toolhead_id,
                    material_id=candidate.material_id,
                    material_revision=candidate.material_revision,
                    material_digest=candidate.material_digest,
                    slicer_filament_profile_id=candidate.slicer_filament_profile_id,
                    slicer_filament_profile_digest=(
                        candidate.slicer_filament_profile_digest
                    ),
                    slicer_filament_dependency_digests=(
                        candidate.slicer_filament_dependency_digests
                    ),
                    color_distance=candidate.color_distance,
                    confidence=1,
                    rationale=(
                        "Recovery proposal uses actual sliced usage and selects "
                        "a compatible spool with sufficient capacity."
                    ),
                    alternatives=[
                        value.spool_id
                        for value in candidates[request.part_id]
                        if value.spool_id != candidate.spool_id
                    ][:3],
                )
            )
        recovered_assignments.sort(
            key=lambda item: next(
                index
                for index, request in enumerate(assignment.requests)
                if request.part_id == item.part_id
            )
        )
        return MaterialAssignment(
            workflow_id=assignment.workflow_id,
            artifact_version=assignment.artifact_version,
            artifact_manifest_digest=assignment.artifact_manifest_digest,
            printer_snapshot_digest=assignment.printer_snapshot_digest,
            cloud_snapshot_id=cloud_snapshot.id,
            cloud_snapshot_digest=cloud_snapshot.digest,
            slot_policy_digest=assignment.slot_policy_digest,
            requests=recovered_requests,
            assignments=recovered_assignments,
            candidate_options=candidates,
            requires_confirmation=True,
            recovery_round=assessment.recovery_round,
            usage_basis="sliced_usage",
            source_assessment_digest=assessment.digest,
            recovery_explanation=(
                "Actual sliced usage exceeded one or more selected spools; "
                "sufficient replacements were preselected."
            ),
        ).with_digest()

    async def _save_blocked_recovery_assignment(
        self,
        job: SliceJob,
        assignment: MaterialAssignment,
        assessment: SliceMaterialAssessment,
        explanation: str,
        *,
        cloud_snapshot: CloudDeviceSnapshot | None = None,
    ) -> None:
        blocked = assignment.model_copy(
            update={
                "id": new_id(),
                "confirmed_by": None,
                "confirmed_at": None,
                "requires_confirmation": True,
                "recovery_round": assessment.recovery_round,
                "usage_basis": "sliced_usage",
                "source_assessment_digest": assessment.digest,
                "recovery_explanation": explanation[:2_000],
                "cloud_snapshot_id": (
                    cloud_snapshot.id
                    if cloud_snapshot is not None
                    else assignment.cloud_snapshot_id
                ),
                "cloud_snapshot_digest": (
                    cloud_snapshot.digest
                    if cloud_snapshot is not None
                    else assignment.cloud_snapshot_digest
                ),
                "digest": None,
                "created_at": fabrication_utc_now(),
            }
        ).with_digest()
        workflow = await self.repository.get_workflow(job.workflow_id)
        if workflow.state == WorkflowState.SLICE_FAILED:
            await self.repository.transition(
                job.workflow_id,
                WorkflowState.SLICE_SETUP,
                event_kind="material.recovery_blocked",
                payload={"assessment_digest": assessment.digest},
            )
        await self.repository.save_material_assignment(blocked)
        workflow = await self.repository.get_workflow(job.workflow_id)
        if workflow.state == WorkflowState.SLICE_SETUP:
            await self.repository.transition(
                job.workflow_id,
                WorkflowState.AWAITING_MATERIAL_REVIEW,
                event_kind="material.load_required",
                payload={
                    "assignment_id": blocked.id,
                    "assessment_digest": assessment.digest,
                },
            )

    async def bambu_connect_readiness(self) -> BambuConnectReadiness:
        return await asyncio.to_thread(self.bambu_connect.readiness)

    async def bambu_connect_setup_status(
        self,
        profile_id: str,
        profile_revision: int | None = None,
    ) -> dict[str, object]:
        profile = await self.repository.get_printer_profile(
            profile_id,
            profile_revision,
        )
        readiness = await self.bambu_connect_readiness()
        device_id = profile.spec.cloud_device_serial
        device_ref = self._device_ref(device_id) if device_id else None
        try:
            confirmation = (
                await self.repository.get_bambu_connect_setup_confirmation(
                    profile_id,
                    device_ref or "",
                )
            )
        except NotFoundError:
            confirmation = None
        active = bool(
            readiness.ready
            and readiness.installation_digest
            and readiness.signer_thumbprint
            and readiness.file_version
            and device_ref
            and confirmation is not None
            and confirmation.device_ref == device_ref
            and confirmation.installation_digest
            == readiness.installation_digest
            and confirmation.signer_thumbprint
            == readiness.signer_thumbprint
            and confirmation.file_version == readiness.file_version
        )
        if not readiness.ready:
            status = "connect_not_ready"
            message = readiness.message
        elif not device_ref:
            status = "device_not_bound"
            message = "Bind an H2D before confirming Bambu Connect setup."
        elif active:
            status = "confirmed"
            message = (
                "Bambu Connect setup is user-confirmed for "
                f"{profile.spec.cloud_device_name or profile.spec.display_name}."
            )
        elif confirmation is not None:
            status = "stale"
            message = (
                "Bambu Connect installation or bound H2D changed; "
                "confirm setup again."
            )
        else:
            status = "confirmation_required"
            message = (
                "Open Bambu Connect, sign in, and confirm the bound H2D is visible."
            )
        return {
            "status": status,
            "message": message,
            "active": active,
            "profile_id": profile.profile_id,
            "device_ref": device_ref,
            "device_name": profile.spec.cloud_device_name,
            "readiness": readiness.model_dump(mode="json"),
            "confirmation": (
                confirmation.model_dump(
                    mode="json",
                    exclude={"device_ref"},
                )
                if confirmation is not None
                else None
            ),
        }

    async def open_bambu_connect(self) -> BambuConnectReadiness:
        return await asyncio.to_thread(self.bambu_connect.open)

    async def confirm_bambu_connect_setup(
        self,
        profile_id: str,
        *,
        profile_revision: int,
        expected_device_ref: str,
        expected_installation_digest: str,
        confirmed_by: str,
    ) -> BambuConnectSetupConfirmation:
        profile = await self.repository.get_printer_profile(
            profile_id,
            profile_revision,
        )
        device_id = profile.spec.cloud_device_serial
        if not device_id:
            raise ConflictError("Slicing profile has no bound cloud H2D device")
        device_ref = self._device_ref(device_id)
        readiness = await self.bambu_connect_readiness()
        if (
            not readiness.ready
            or not readiness.installation_digest
            or not readiness.signer_thumbprint
            or not readiness.file_version
        ):
            raise ConflictError("Bambu Connect is not ready")
        if device_ref != expected_device_ref:
            raise ConflictError("The bound H2D changed; review Connect setup again")
        if readiness.installation_digest != expected_installation_digest:
            raise ConflictError(
                "Bambu Connect installation changed; review setup again"
            )
        confirmation = BambuConnectSetupConfirmation(
            profile_id=profile_id,
            device_ref=device_ref,
            installation_digest=readiness.installation_digest,
            signer_thumbprint=readiness.signer_thumbprint,
            file_version=readiness.file_version,
            confirmed_by=confirmed_by,
        )
        await self.repository.save_bambu_connect_setup_confirmation(
            confirmation
        )
        return confirmation

    async def revoke_bambu_connect_setup(
        self,
        profile_id: str,
        profile_revision: int | None = None,
    ) -> None:
        profile = await self.repository.get_printer_profile(
            profile_id,
            profile_revision,
        )
        device_id = profile.spec.cloud_device_serial
        if not device_id:
            raise ConflictError("Slicing profile has no bound cloud H2D device")
        await self.repository.revoke_bambu_connect_setup_confirmation(
            profile_id,
            self._device_ref(device_id),
        )

    async def install_bambu_connect(self) -> BambuConnectReadiness:
        return await asyncio.to_thread(self.bambu_connect.install)

    async def launch_bambu_connect_handoff(
        self,
        workflow_id: str,
        *,
        expected_slice_job_id: str,
        expected_artifact_digest: str,
    ) -> BambuConnectHandoff:
        async with self._slicing_operation_lock(workflow_id):
            workflow = await self.repository.get_workflow(workflow_id)
            PrintingApplication._ensure_not_archived(workflow)
            if workflow.state != WorkflowState.AWAITING_SLICE_REVIEW:
                raise ConflictError(
                    "A reviewed ready slice is required before opening Bambu Connect"
                )
            job = await self.repository.get_latest_slice_job(workflow_id)
            if job.id != expected_slice_job_id or job.status != SliceJobStatus.READY:
                raise ConflictError("The selected slice job is stale or not ready")
            sliced = await self.repository.get_sliced_artifact(job.id)
            if sliced.digest != expected_artifact_digest:
                raise ConflictError("The selected sliced artifact digest is stale")
            latest: BambuConnectHandoff | None = None
            try:
                latest = await self.repository.get_latest_bambu_connect_handoff(
                    workflow_id
                )
            except NotFoundError:
                pass
            active_statuses = {
                BambuConnectHandoffStatus.READY,
                BambuConnectHandoffStatus.CONNECT_OPENED,
                BambuConnectHandoffStatus.WAITING_FOR_MATCH,
                BambuConnectHandoffStatus.ACTIVITY_UNVERIFIED,
                BambuConnectHandoffStatus.PRINT_MATCHED,
                BambuConnectHandoffStatus.PRINTING,
            }
            if (
                latest is not None
                and latest.slice_job_id == job.id
                and latest.status in active_statuses
            ):
                return latest
            profile, baseline = await self._validate_bambu_connect_preflight(
                workflow_id,
                job,
                sliced,
            )
            attempt = (
                latest.attempt + 1
                if latest is not None and latest.slice_job_id == job.id
                else 1
            )
            handoff = await asyncio.to_thread(
                self.bambu_connect.prepare_handoff,
                workflow_id=workflow_id,
                sliced=sliced,
                expected_device_ref=self._device_ref(
                    profile.spec.cloud_device_serial or ""
                ),
                expected_device_name=profile.spec.cloud_device_name
                or profile.spec.display_name,
                attempt=attempt,
                baseline_state=baseline.state,
            )
            await self.repository.save_bambu_connect_handoff(handoff)
            try:
                await asyncio.to_thread(self.bambu_connect.launch, handoff)
            except Exception as exc:
                failed = handoff.model_copy(
                    update={
                        "status": BambuConnectHandoffStatus.FAILED,
                        "message": str(exc)[-2_000:],
                        "updated_at": fabrication_utc_now(),
                    }
                )
                await self.repository.save_bambu_connect_handoff(failed)
                raise
            now = fabrication_utc_now()
            opened = handoff.model_copy(
                update={
                    "status": BambuConnectHandoffStatus.WAITING_FOR_MATCH,
                    "launched_at": now,
                    "match_deadline": now
                    + timedelta(
                        seconds=self.settings.bambu_connect_match_timeout_seconds
                    ),
                    "message": (
                        "Bambu Connect opened. Select the expected H2D, keep the "
                        "correlation name unchanged, and press Print/Send in Connect."
                    ),
                    "updated_at": now,
                }
            )
            await self.repository.save_bambu_connect_handoff(opened)
            await self.repository.enqueue(
                workflow_id,
                WorkKind.MONITOR_CONNECT,
                delay_seconds=2,
            )
            return opened

    async def _validate_bambu_connect_preflight(
        self,
        workflow_id: str,
        job: SliceJob,
        sliced,
    ) -> tuple[PrinterProfileRevision, CloudPrintStatusObservation]:
        if sha256_file(Path(sliced.path)) != sliced.digest:
            raise ConflictError("Sliced artifact digest changed before handoff")
        printer_snapshot = (
            await self.repository.get_workflow_printer_snapshot(workflow_id)
        )
        profile = await self.repository.get_printer_profile(
            printer_snapshot.profile_id,
            printer_snapshot.profile_revision,
        )
        device_id = profile.spec.cloud_device_serial
        if not device_id:
            raise ConflictError("Slicing profile has no bound cloud H2D device")
        connect_status = await self.bambu_connect_setup_status(
            profile.profile_id,
            profile.revision,
        )
        if not connect_status["active"]:
            raise ConflictError(
                "Confirm Bambu Connect setup for the bound H2D before handoff"
            )
        try:
            fresh_snapshot = await self.inventory.snapshot(device_id)
            baseline = await self.inventory.print_status(device_id)
        except CloudInventoryError as exc:
            raise ExternalServiceError(str(exc)) from exc
        self._validate_cloud_snapshot(profile, fresh_snapshot)
        if baseline.state in {"preparing", "printing", "paused"}:
            raise ConflictError("The expected H2D is already busy")
        assignment = await self.repository.get_latest_material_assignment(workflow_id)
        if assignment.digest != job.material_assignment_digest:
            raise ConflictError("Material assignment changed after slicing")
        await self._ensure_unknown_quantity_authorizations_current(
            profile,
            fresh_snapshot,
            assignment,
        )
        reservations = await self.repository.list_spool_reservations(job.id)
        if not reservations or any(
            item.status not in {"reserved", "released"} for item in reservations
        ):
            raise ConflictError(
                "Slice material reservations are unavailable or insufficient"
            )
        current_profile = await self.repository.get_printer_profile(
            printer_snapshot.profile_id
        )
        current_policy = resolve_slot_policy(
            current_profile.spec,
            printer_snapshot.overrides,
        )
        trays = {
            item.slot_id: item
            for item in self._snapshot_trays(fresh_snapshot)
        }
        required_by_spool = {
            spool_id: usage
            * (1 + job.material_safety_margin_percent / 100)
            for spool_id, usage in sliced.filament_usage_g.items()
        }
        checked_spools: set[str] = set()
        for selected in assignment.assignments:
            if (
                selected.slot_id in current_policy.forbidden_slot_ids
                or (
                    current_policy.allowed_slot_ids is not None
                    and selected.slot_id not in current_policy.allowed_slot_ids
                )
                or selected.slot_id
                in current_policy.part_forbidden_slot_ids.get(
                    selected.part_id,
                    set(),
                )
            ):
                raise ConflictError(
                    f"Material slot '{selected.slot_id}' is no longer allowed"
                )
            part_allowed = current_policy.part_allowed_slot_ids.get(
                selected.part_id
            )
            if part_allowed is not None and selected.slot_id not in part_allowed:
                raise ConflictError(
                    f"Material slot '{selected.slot_id}' is no longer allowed "
                    f"for '{selected.part_id}'"
                )
            if selected.spool_id in checked_spools:
                continue
            checked_spools.add(selected.spool_id)
            tray = trays.get(selected.slot_id)
            if tray is None or tray.material is None:
                raise ConflictError(
                    f"Material slot '{selected.slot_id}' is no longer loaded"
                )
            spool = await self.repository.get_spool(selected.spool_id)
            identity = self._tray_identity_digest(device_id, tray)
            if spool.tray_identity_digest != identity:
                raise ConflictError(
                    f"Material in slot '{selected.slot_id}' changed after slicing"
                )
            material = await self.repository.get_material_definition(
                selected.material_id,
                selected.material_revision,
            )
            if (
                tray.material_profile_id is None
                or tray.material_profile_id not in material.spec.cloud_filament_ids
            ):
                raise ConflictError(
                    f"Material mapping for slot '{selected.slot_id}' changed"
                )
            if (
                selected.quantity_status
                == SpoolQuantityStatus.CLOUD_ESTIMATE
            ):
                required = required_by_spool.get(selected.spool_id)
                if (
                    required is not None
                    and (
                        tray.estimated_remaining_g is None
                        or tray.estimated_remaining_g < required
                    )
                ):
                    raise ConflictError(
                        f"Slot '{selected.slot_id}' no longer has enough material"
                    )
        return profile, baseline

    @staticmethod
    def _connect_status_matches(
        handoff: BambuConnectHandoff,
        observation: CloudPrintStatusObservation,
    ) -> bool:
        expected = handoff.correlation_name.casefold()

        def normalized(value: str) -> str:
            name = Path(value.replace("\\", "/")).name.casefold()
            for suffix in (".gcode.3mf", ".3mf", ".gcode"):
                if name.endswith(suffix):
                    return name[: -len(suffix)]
            return name

        values = [
            observation.gcode_file,
            observation.subtask_name,
        ]
        return any(
            normalized(value) == expected
            for value in values
            if value
        )

    async def monitor_bambu_connect_handoff(self, workflow_id: str) -> None:
        async with self._slicing_operation_lock(workflow_id):
            await self._monitor_bambu_connect_handoff_unlocked(workflow_id)

    async def _monitor_bambu_connect_handoff_unlocked(
        self,
        workflow_id: str,
    ) -> None:
        handoff = await self.repository.get_latest_bambu_connect_handoff(
            workflow_id
        )
        terminal = {
            BambuConnectHandoffStatus.COMPLETED,
            BambuConnectHandoffStatus.FAILED,
            BambuConnectHandoffStatus.TIMED_OUT,
            BambuConnectHandoffStatus.CANCELLED,
        }
        if handoff.status in terminal:
            return
        current_job = await self.repository.get_latest_slice_job(workflow_id)
        if current_job.id != handoff.slice_job_id:
            stale = handoff.model_copy(
                update={
                    "status": BambuConnectHandoffStatus.CANCELLED,
                    "message": "Handoff no longer matches the current slice job.",
                    "updated_at": fabrication_utc_now(),
                }
            )
            await self.repository.save_bambu_connect_handoff(stale)
            return
        printer_snapshot = (
            await self.repository.get_workflow_printer_snapshot(workflow_id)
        )
        device_id = printer_snapshot.profile.cloud_device_serial
        if not device_id:
            raise ConflictError("Slicing profile has no bound cloud H2D device")
        now = fabrication_utc_now()
        if (
            handoff.matched_at is None
            and handoff.match_deadline is not None
            and now >= handoff.match_deadline
        ):
            timed_out = handoff.model_copy(
                update={
                    "status": BambuConnectHandoffStatus.TIMED_OUT,
                    "message": (
                        "No strictly matching H2D job was observed within 15 minutes."
                    ),
                    "updated_at": now,
                }
            )
            await self.repository.save_bambu_connect_handoff(timed_out)
            return
        try:
            observation = await self.inventory.print_status(device_id)
        except CloudInventoryError as exc:
            last_success = handoff.last_observed_at or handoff.matched_at
            if (
                handoff.matched_at is not None
                and last_success is not None
                and (
                    now - last_success
                ).total_seconds()
                >= self.settings.bambu_connect_monitor_failure_timeout_seconds
            ):
                failed = handoff.model_copy(
                    update={
                        "status": BambuConnectHandoffStatus.FAILED,
                        "message": (
                            "Printer monitoring was unavailable for too long; "
                            "physical print status is unknown."
                        ),
                        "updated_at": now,
                    }
                )
                await self.repository.save_bambu_connect_handoff(failed)
                workflow = await self.repository.get_workflow(workflow_id)
                if workflow.state == WorkflowState.PRINTING:
                    await self.repository.transition(
                        workflow_id,
                        WorkflowState.PRINT_FAILED,
                        event_kind="bambu_connect.monitoring_lost",
                        payload={"handoff_id": handoff.id},
                    )
                return
            waiting = handoff.model_copy(
                update={
                    "message": f"Printer status is temporarily unavailable: {exc}",
                    "updated_at": now,
                }
            )
            await self.repository.save_bambu_connect_handoff(waiting)
            await self.repository.enqueue(
                workflow_id,
                WorkKind.MONITOR_CONNECT,
                delay_seconds=5,
            )
            return
        matches = self._connect_status_matches(handoff, observation)
        active = observation.state in {"preparing", "printing", "paused"}
        matched = handoff.matched_at is not None
        if not matched and active and matches:
            matched = True
            handoff = handoff.model_copy(
                update={
                    "status": BambuConnectHandoffStatus.PRINT_MATCHED,
                    "matched_at": observation.observed_at,
                    "matched_task_id": observation.task_id,
                    "matched_file": observation.gcode_file,
                    "matched_name": observation.subtask_name,
                    "message": "Matching H2D print job detected.",
                }
            )
            workflow = await self.repository.get_workflow(workflow_id)
            if workflow.state == WorkflowState.AWAITING_SLICE_REVIEW:
                await self.repository.transition(
                    workflow_id,
                    WorkflowState.PRINTING,
                    event_kind="bambu_connect.print_matched",
                    payload={"handoff_id": handoff.id},
                )
        elif not matched and active:
            handoff = handoff.model_copy(
                update={
                    "status": BambuConnectHandoffStatus.ACTIVITY_UNVERIFIED,
                    "message": (
                        "Printer activity was detected, but filename/task metadata "
                        "does not match this artifact."
                    ),
                }
            )
        elif not matched:
            handoff = handoff.model_copy(
                update={
                    "status": BambuConnectHandoffStatus.WAITING_FOR_MATCH,
                    "message": "Waiting for a matching job on the expected H2D.",
                }
            )

        if matched:
            if (
                handoff.matched_task_id
                and observation.task_id
                and handoff.matched_task_id != observation.task_id
            ) or (
                (observation.gcode_file or observation.subtask_name)
                and not matches
            ):
                failed = handoff.model_copy(
                    update={
                        "status": BambuConnectHandoffStatus.FAILED,
                        "message": "Matched printer task identity changed.",
                        "last_observed_at": observation.observed_at,
                        "updated_at": now,
                    }
                )
                await self.repository.save_bambu_connect_handoff(failed)
                workflow = await self.repository.get_workflow(workflow_id)
                if workflow.state == WorkflowState.PRINTING:
                    await self.repository.transition(
                        workflow_id,
                        WorkflowState.PRINT_FAILED,
                        event_kind="bambu_connect.identity_changed",
                        payload={"handoff_id": handoff.id},
                    )
                return
            if observation.state == "completed":
                status = BambuConnectHandoffStatus.COMPLETED
                message = "Matching H2D print completed."
            elif observation.state == "failed":
                status = BambuConnectHandoffStatus.FAILED
                message = "Matching H2D print failed."
            elif observation.state == "idle":
                status = BambuConnectHandoffStatus.FAILED
                message = (
                    "The matched H2D returned to idle without a verified completion; "
                    "review the job in Bambu Connect."
                )
            elif observation.state == "unknown":
                status = BambuConnectHandoffStatus.PRINT_MATCHED
                message = "Matching job identity is retained; printer state is unknown."
            else:
                status = BambuConnectHandoffStatus.PRINTING
                message = (
                    "Matching H2D print is paused."
                    if observation.state == "paused"
                    else "Matching H2D print is active."
                )
            handoff = handoff.model_copy(
                update={
                    "status": status,
                    "matched_task_id": (
                        handoff.matched_task_id or observation.task_id
                    ),
                    "matched_file": handoff.matched_file
                    or observation.gcode_file,
                    "matched_name": handoff.matched_name
                    or observation.subtask_name,
                    "progress_percent": observation.progress_percent,
                    "remaining_time_seconds": (
                        observation.remaining_time_seconds
                    ),
                    "printer_state": observation.state,
                    "printer_error_code": observation.error_code,
                    "last_observed_at": observation.observed_at,
                    "message": message,
                    "updated_at": now,
                }
            )
            await self.repository.save_bambu_connect_handoff(handoff)
            workflow = await self.repository.get_workflow(workflow_id)
            if status == BambuConnectHandoffStatus.COMPLETED:
                if workflow.state == WorkflowState.PRINTING:
                    await self.repository.transition(
                        workflow_id,
                        WorkflowState.COMPLETED,
                        event_kind="bambu_connect.completed",
                        payload={"handoff_id": handoff.id},
                    )
                return
            if status == BambuConnectHandoffStatus.FAILED:
                if workflow.state == WorkflowState.PRINTING:
                    await self.repository.transition(
                        workflow_id,
                        WorkflowState.PRINT_FAILED,
                        event_kind="bambu_connect.failed",
                        payload={
                            "handoff_id": handoff.id,
                            "printer_error_code": observation.error_code,
                        },
                    )
                return
        else:
            handoff = handoff.model_copy(
                update={
                    "printer_state": observation.state,
                    "progress_percent": observation.progress_percent,
                    "remaining_time_seconds": (
                        observation.remaining_time_seconds
                    ),
                    "last_observed_at": observation.observed_at,
                    "updated_at": now,
                }
            )
            await self.repository.save_bambu_connect_handoff(handoff)
        await self.repository.enqueue(
            workflow_id,
            WorkKind.MONITOR_CONNECT,
            delay_seconds=5,
        )

    async def stop_bambu_connect_monitoring(
        self,
        workflow_id: str,
    ) -> BambuConnectHandoff:
        async with self._slicing_operation_lock(workflow_id):
            return await self._stop_bambu_connect_monitoring_unlocked(
                workflow_id
            )

    async def _stop_bambu_connect_monitoring_unlocked(
        self,
        workflow_id: str,
    ) -> BambuConnectHandoff:
        handoff = await self.repository.get_latest_bambu_connect_handoff(
            workflow_id
        )
        if handoff.status in {
            BambuConnectHandoffStatus.COMPLETED,
            BambuConnectHandoffStatus.FAILED,
            BambuConnectHandoffStatus.TIMED_OUT,
            BambuConnectHandoffStatus.CANCELLED,
        }:
            return handoff
        matched = handoff.status in {
            BambuConnectHandoffStatus.PRINT_MATCHED,
            BambuConnectHandoffStatus.PRINTING,
        }
        cancelled = handoff.model_copy(
            update={
                "status": (
                    BambuConnectHandoffStatus.FAILED
                    if matched
                    else BambuConnectHandoffStatus.CANCELLED
                ),
                "message": (
                    "Monitoring stopped; physical print status is unknown. "
                    "The physical printer was not controlled."
                    if matched
                    else (
                        "Monitoring stopped. The physical printer was not controlled."
                    )
                ),
                "updated_at": fabrication_utc_now(),
            }
        )
        await self.repository.save_bambu_connect_handoff(cancelled)
        workflow = await self.repository.get_workflow(workflow_id)
        if matched and workflow.state == WorkflowState.PRINTING:
            await self.repository.transition(
                workflow_id,
                WorkflowState.PRINT_FAILED,
                event_kind="bambu_connect.monitoring_stopped",
                payload={"handoff_id": handoff.id},
            )
        return cancelled

    async def request_print(self, workflow_id: str) -> None:
        workflow = await self.repository.get_workflow(workflow_id)
        PrintingApplication._ensure_not_archived(workflow)
        try:
            snapshot = await self.repository.get_workflow_printer_snapshot(workflow_id)
        except NotFoundError:
            snapshot = None
        if (
            snapshot is not None
            and snapshot.profile.slicer.driver_id != "simulator_passthrough"
        ):
            raise ConflictError(
                "This printer requires material assignment and slicing before submission"
            )
        await self.repository.enqueue_print_submission(workflow_id)

    async def archive_workflow(self, workflow_id: str) -> PrintWorkflow:
        return await self.repository.archive_workflow(workflow_id)

    async def restore_workflow(self, workflow_id: str) -> PrintWorkflow:
        return await self.repository.restore_workflow(workflow_id)

    async def submit_print(self, workflow_id: str) -> None:
        workflow = await self.repository.get_workflow(workflow_id)
        PrintingApplication._ensure_not_archived(workflow)
        if workflow.state in {
            WorkflowState.QUEUED,
            WorkflowState.PRINTING,
            WorkflowState.COMPLETED,
        }:
            if await self.repository.get_latest_job(workflow_id) is not None:
                return
        if workflow.state != WorkflowState.SUBMITTING:
            raise ConflictError("Print submission is not active for this workflow")
        approval = await self.repository.get_approval(workflow_id)
        artifact = await self.repository.get_artifact(
            workflow_id,
            approval.artifact_version,
        )
        if artifact.manifest_digest != approval.manifest_digest:
            raise ValidationError("Approved artifact digest no longer matches")
        adapter = cast(PrinterAdapter, self.printers.get(workflow.printer_name))
        plan = await self.repository.get_plan(workflow_id)
        idempotency_key = hashlib.sha256(
            f"{workflow_id}:{approval.manifest_digest}:{workflow.printer_name}".encode()
        ).hexdigest()
        job = await adapter.submit(
            workflow_id,
            artifact,
            plan.print_settings,
            idempotency_key,
        )
        await self.repository.finalize_print_submission(job)

    async def copy_workflow(
        self,
        workflow_id: str,
        *,
        target_printer_name: str | None = None,
        overrides: JobOverrides | None = None,
    ) -> PrintWorkflow:
        source_workflow = await self.repository.get_workflow(workflow_id)
        PrintingApplication._ensure_not_archived(source_workflow)
        if source_workflow.active_artifact_version is None:
            raise ConflictError("Workflow has no artifact to copy")
        source_artifact = await self.repository.get_artifact(
            workflow_id,
            source_workflow.active_artifact_version,
        )
        source_handoff = (
            await self.repository.get_handoff(
                workflow_id,
                source_workflow.active_handoff_version,
            )
            if source_workflow.active_handoff_version is not None
            else None
        )
        plan = await self.repository.get_plan(workflow_id)
        fabrication_copy = target_printer_name is not None
        printer_name = target_printer_name or source_workflow.printer_name
        copied_workflow = PrintWorkflow(
            requirement=source_workflow.requirement,
            printer_name=printer_name,
            state=WorkflowState.AWAITING_APPROVAL,
            active_handoff_version=1 if source_handoff is not None else None,
            active_artifact_version=1,
        )
        copied_handoff = (
            source_handoff.model_copy(
                update={
                    "workflow_id": copied_workflow.id,
                    "version": 1,
                    "digest": None,
                }
            ).with_digest()
            if source_handoff is not None
            else None
        )
        try:
            if fabrication_copy:
                target_profile, copied_snapshot = await self._build_printer_snapshot(
                    copied_workflow.id,
                    printer_name,
                    overrides or JobOverrides(),
                )
                if (
                    target_profile.spec.slicer.driver_id
                    == "simulator_passthrough"
                ):
                    raise ValidationError(
                        "Fabrication copies require a slicing-capable printer profile"
                    )
                await self.slicers.get(
                    target_profile.spec.slicer.driver_id
                ).validate_profile(target_profile)
            else:
                target_profile = None
                try:
                    source_snapshot = (
                        await self.repository.get_workflow_printer_snapshot(
                            workflow_id
                        )
                    )
                except NotFoundError:
                    if source_workflow.printer_name != "simulator":
                        raise
                    profile = built_in_simulator_profile()
                    try:
                        await self.repository.get_printer_profile(
                            profile.profile_id,
                            profile.revision,
                        )
                    except NotFoundError:
                        await self.repository.save_printer_profile(profile)
                    source_snapshot = WorkflowPrinterSnapshot(
                        workflow_id=workflow_id,
                        profile_id=profile.profile_id,
                        profile_revision=profile.revision,
                        profile_digest=profile.digest or "0" * 64,
                        profile=profile.spec,
                        slicer_profile_file_digests=await asyncio.to_thread(
                            resolve_slicer_profile_file_digests,
                            profile,
                        ),
                        resolved_slot_policy=resolve_slot_policy(
                            profile.spec,
                            JobOverrides(),
                        ),
                    ).with_digest()
                copied_snapshot = source_snapshot.model_copy(
                    update={
                        "workflow_id": copied_workflow.id,
                        "digest": None,
                    }
                ).with_digest()

            copied_from = {
                "workflow_id": workflow_id,
                "artifact_version": source_artifact.version,
                "manifest_digest": source_artifact.manifest_digest,
            }
            if source_artifact.schema_version == "2":
                source_directory = self.artifacts.artifact_directory(
                    workflow_id,
                    source_artifact.version,
                )
                staging = (
                    self.artifacts.workflow_root(copied_workflow.id)
                    / "staging"
                    / "artifact-1"
                )
                shutil.copytree(source_directory, staging)
                manifest_path = staging / "manifest.json"
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                manifest.update(
                    {
                        "workflow_id": copied_workflow.id,
                        "artifact_version": 1,
                        "copied_from": copied_from,
                        "handoff_digest": (
                            copied_handoff.digest if copied_handoff is not None else None
                        ),
                    }
                )
                manifest.pop("manifest_digest", None)
                manifest_digest = canonical_digest(manifest)
                manifest["manifest_digest"] = manifest_digest
                manifest_path.write_text(
                    json.dumps(manifest, sort_keys=True, indent=2),
                    encoding="utf-8",
                )
                adopted = self.artifacts.adopt_project(copied_workflow.id, 1, staging)
                shutil.rmtree(staging)
                source_relative = (
                    source_artifact.source_path.relative_to(source_directory)
                    if source_artifact.source_path is not None
                    else None
                )
                model_relative = source_artifact.model_path.relative_to(source_directory)
                project_relative = (
                    source_artifact.project_path.relative_to(source_directory)
                    if source_artifact.project_path is not None
                    else None
                )
                three_mf_relative = (
                    source_artifact.three_mf_path.relative_to(source_directory)
                    if source_artifact.three_mf_path is not None
                    else None
                )
                copied_artifact = source_artifact.model_copy(
                    update={
                        "workflow_id": copied_workflow.id,
                        "version": 1,
                        "source_path": adopted / source_relative if source_relative else None,
                        "model_path": adopted / model_relative,
                        "project_path": adopted / project_relative if project_relative else None,
                        "three_mf_path": (
                            adopted / three_mf_relative if three_mf_relative else None
                        ),
                        "manifest_digest": manifest_digest,
                    }
                )
            elif fabrication_copy:
                staging = (
                    self.artifacts.workflow_root(copied_workflow.id)
                    / "staging"
                    / "artifact-1"
                )
                source_path = (
                    source_artifact.source_path or source_artifact.model_path
                )
                imported = (
                    source_artifact.source_path is None
                    or source_path.suffix.casefold() != ".scad"
                )
                project = single_part_project(
                    source_digest=(
                        source_artifact.source_digest
                        or source_artifact.model_digest
                    ),
                    imported=imported,
                    source_filename=source_path.name,
                )
                manifest, files = await asyncio.to_thread(
                    write_project_artifact,
                    directory=staging,
                    workflow_id=copied_workflow.id,
                    version=1,
                    project=project,
                    source_path=source_path,
                    model_path=source_artifact.model_path,
                    mesh=source_artifact.mesh,
                    provenance=source_artifact.provenance,
                    handoff_digest=(
                        copied_handoff.digest
                        if copied_handoff is not None
                        else None
                    ),
                    diagnostics=None,
                    imported=imported,
                )
                manifest["copied_from"] = copied_from
                manifest.pop("manifest_digest", None)
                manifest_digest = canonical_digest(manifest)
                manifest["manifest_digest"] = manifest_digest
                (staging / "manifest.json").write_text(
                    json.dumps(manifest, sort_keys=True, indent=2),
                    encoding="utf-8",
                )
                adopted = self.artifacts.adopt_project(
                    copied_workflow.id,
                    1,
                    staging,
                )
                shutil.rmtree(staging)
                copied_artifact = ModelArtifact(
                    schema_version="2",
                    workflow_id=copied_workflow.id,
                    version=1,
                    source_path=adopted / "project" / "main.scad",
                    model_path=(
                        adopted
                        / "outputs"
                        / "parts"
                        / "base_model.stl"
                    ),
                    project_path=adopted / "project" / "project.json",
                    three_mf_path=adopted / "outputs" / "model.3mf",
                    source_digest=(
                        source_artifact.source_digest
                        or source_artifact.model_digest
                    ),
                    model_digest=str(manifest["model_digest"]),
                    project_digest=str(manifest["project_digest"]),
                    three_mf_digest=str(manifest["three_mf_digest"]),
                    manifest_digest=manifest_digest,
                    mesh=source_artifact.mesh,
                    project=project,
                    part_meshes={"base_model": source_artifact.mesh},
                    files=files,
                    provenance=source_artifact.provenance,
                    revision=source_artifact.revision,
                )
            else:
                manifest = {
                    "workflow_id": copied_workflow.id,
                    "artifact_version": 1,
                    "source_digest": source_artifact.source_digest,
                    "model_digest": source_artifact.model_digest,
                    "mesh": source_artifact.mesh.model_dump(mode="json"),
                    "provenance": source_artifact.provenance.model_dump(mode="json"),
                    "copied_from": copied_from,
                }
                if copied_handoff is not None:
                    manifest["handoff_digest"] = copied_handoff.digest
                manifest_digest = canonical_digest(manifest)
                manifest["manifest_digest"] = manifest_digest
                adopted_source, adopted_model, _ = self.artifacts.adopt(
                    copied_workflow.id,
                    1,
                    source_artifact.source_path,
                    source_artifact.model_path,
                    manifest,
                )
                copied_artifact = source_artifact.model_copy(
                    update={
                        "workflow_id": copied_workflow.id,
                        "version": 1,
                        "source_path": adopted_source,
                        "model_path": adopted_model,
                        "manifest_digest": manifest_digest,
                    }
                )

            carried_approval = None
            if fabrication_copy and source_artifact.schema_version == "2":
                try:
                    source_approval = await self.repository.get_approval(
                        workflow_id
                    )
                except NotFoundError:
                    source_approval = None
                if (
                    source_approval is not None
                    and source_approval.artifact_version
                    == source_artifact.version
                    and source_approval.manifest_digest
                    == source_artifact.manifest_digest
                    and copied_artifact.three_mf_path is not None
                    and self._same_printable_content(
                        source_artifact,
                        copied_artifact,
                    )
                ):
                    assert target_profile is not None
                    await ProfilePrinterAdapter(target_profile).validate(
                        copied_artifact,
                        plan.print_settings,
                    )
                    carried_approval = ArtifactApproval(
                        workflow_id=copied_workflow.id,
                        artifact_version=copied_artifact.version,
                        manifest_digest=copied_artifact.manifest_digest,
                        approved_by=(
                            "carried-from:"
                            f"{workflow_id}:{source_approval.approved_by}"
                        ),
                    )
                    copied_workflow = copied_workflow.model_copy(
                        update={"state": WorkflowState.APPROVED}
                    )

            await self.repository.create_copy(
                copied_workflow,
                plan,
                copied_handoff,
                copied_artifact,
                copied_snapshot,
                carried_approval,
                source_workflow_id=workflow_id,
                source_artifact_version=source_artifact.version,
                fabrication=fabrication_copy,
            )
            return copied_workflow
        except Exception:
            shutil.rmtree(
                self.artifacts.workflow_root(copied_workflow.id),
                ignore_errors=True,
            )
            raise

    @staticmethod
    def _same_printable_content(
        source: ModelArtifact,
        copied: ModelArtifact,
    ) -> bool:
        if any(
            getattr(source, field) != getattr(copied, field)
            for field in (
                "source_digest",
                "model_digest",
                "project_digest",
                "three_mf_digest",
            )
        ):
            return False
        source_files = sorted(
            (
                item.role,
                item.path,
                item.digest,
                item.size_bytes,
                item.part_id,
            )
            for item in source.files
        )
        copied_files = sorted(
            (
                item.role,
                item.path,
                item.digest,
                item.size_bytes,
                item.part_id,
            )
            for item in copied.files
        )
        return source_files == copied_files

    async def refresh_print(self, workflow_id: str) -> None:
        workflow = await self.repository.get_workflow(workflow_id)
        PrintingApplication._ensure_not_archived(workflow)
        job = await self.repository.get_latest_job(workflow_id)
        if job is None:
            raise ConflictError("Workflow has no printer job")
        adapter = cast(PrinterAdapter, self.printers.get(job.printer_name))
        refreshed = await adapter.status(job.external_id)
        await self.repository.save_job(refreshed)
        target = {
            PrintJobStatus.QUEUED: WorkflowState.QUEUED,
            PrintJobStatus.PRINTING: WorkflowState.PRINTING,
            PrintJobStatus.COMPLETED: WorkflowState.COMPLETED,
            PrintJobStatus.FAILED: WorkflowState.PRINT_FAILED,
            PrintJobStatus.CANCELLED: WorkflowState.CANCELLED,
        }[refreshed.status]
        if target != workflow.state:
            await self.repository.transition(
                workflow_id,
                target,
                event_kind=f"print.{refreshed.status.value}",
                payload={"message": refreshed.message},
            )
        if target in {WorkflowState.QUEUED, WorkflowState.PRINTING}:
            await self.repository.enqueue(
                workflow_id,
                WorkKind.REFRESH_PRINT,
                delay_seconds=2,
            )

    async def cancel(self, workflow_id: str) -> None:
        lock = self._cancel_locks.setdefault(workflow_id, asyncio.Lock())
        async with lock:
            workflow = await self.repository.get_workflow(workflow_id)
            PrintingApplication._ensure_not_archived(workflow)
            if workflow.state == WorkflowState.CANCELLED:
                return
            if workflow.state == WorkflowState.PRINTING:
                try:
                    connect_handoff = (
                        await self.repository.get_latest_bambu_connect_handoff(
                            workflow_id
                        )
                    )
                except NotFoundError:
                    connect_handoff = None
                if (
                    connect_handoff is not None
                    and connect_handoff.status
                    in {
                        BambuConnectHandoffStatus.PRINT_MATCHED,
                        BambuConnectHandoffStatus.PRINTING,
                    }
                ):
                    raise ConflictError(
                        "Use Bambu Connect or the printer to control the physical job"
                    )
            if workflow.state in {WorkflowState.QUEUED, WorkflowState.PRINTING}:
                job = await self.repository.get_latest_job(workflow_id)
                if job is None:
                    raise ConflictError("Workflow has no printer job")
                adapter = cast(PrinterAdapter, self.printers.get(job.printer_name))
                cancelled = await adapter.cancel(job.external_id)
                await self.repository.save_job(cancelled)
            if workflow.state in {
                WorkflowState.SLICE_REQUESTED,
                WorkflowState.SLICING,
                WorkflowState.SLICE_VALIDATING,
                WorkflowState.AWAITING_SLICE_REVIEW,
            }:
                job = await self.repository.get_latest_slice_job(workflow_id)
                if workflow.state == WorkflowState.SLICING:
                    await self.slicers.get(job.slicer_driver_id).cancel(job.id)
                await self.repository.complete_spool_reservations(
                    job.id,
                )
            await self.repository.transition(
                workflow_id,
                WorkflowState.CANCELLED,
                event_kind="workflow.cancelled",
            )
