import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  BambuAccountConnection,
  type CloudDevice,
  type CredentialStatus,
} from './BambuAccountConnection'
import { UnknownQuantityAuthorizationDialog } from './UnknownQuantityAuthorizationDialog'

const API = '/api/v1'

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API}${path}`, {
    ...init,
    headers: {
      'Content-Type': 'application/json',
      ...init?.headers,
    },
  })
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}))
    throw new Error(payload.error?.message ?? response.statusText)
  }
  return response.json()
}

type Slot = {
  id: string
  name: string
  system: 'ams' | 'ams_ht' | 'external'
  automatic_assignment: boolean
}

type SlicingProfile = {
  profile_id: string
  revision: number
  origin: string
  digest: string
  cloud_device_ref: string | null
  spec: {
    display_name: string
    manufacturer: string
    model: string
    build_volume: { width_mm: number; depth_mm: number; height_mm: number }
    toolheads: Array<{
      id: string
      name: string
      nozzle_diameter_mm: number
      nozzle_material: string
    }>
    plates: Array<{ id: string; name: string }>
    material_slots: Slot[]
    default_slot_policy: {
      forbidden_slot_ids: string[]
      allowed_slot_ids: string[] | null
    }
    slicer: {
      driver_id: string
      machine_profile_id: string
      process_profile_id: string
      executable_path: string | null
      resource_root: string | null
    }
    cloud_region: 'global' | 'china' | null
    cloud_device_name: string | null
    cloud_device_serial: string | null
  }
}

type MaterialDefinition = {
  material_id: string
  revision: number
  digest: string
  spec: {
    display_name: string
    family: string
    nominal_color: string
    measured_color: string | null
    slicer_filament_profile_id: string
    cloud_filament_ids: string[]
    mapping_origin: 'manual' | 'studio_exact' | 'generic_confirmed'
    source_profile_id: string | null
  }
}

type ObservedTray = {
  slot_id: string
  material: string | null
  material_profile_id: string | null
  material_sub_brand: string | null
  color: string | null
  remain_percentage: number | null
  estimated_remaining_g: number | null
}

type FilamentMappingStatus = {
  cloud_filament_id: string
  state:
    | 'official_exact'
    | 'manual'
    | 'generic_confirmed'
    | 'confirmation_required'
    | 'upgrade_available'
    | 'ambiguous'
    | 'missing'
  selected_profile_id: string | null
  proposed_profile_id: string | null
  reason: string
}

type ProfileSlotObservation = {
  profile_id: string
  profile_revision: number
  profile_digest: string
  snapshot: {
    digest: string
    observed_at: string
    expires_at: string
    ams_units: Array<{ trays: ObservedTray[] }>
    external_trays: ObservedTray[]
    warnings: string[]
  }
  material_mappings: FilamentMappingStatus[]
  quantity_authorizations: QuantityAuthorizationState[]
}

type QuantityAuthorizationState = {
  slot_id: string
  tray_identity_digest: string
  status: 'authorization_required' | 'authorized_unknown'
  authorized_at: string | null
}

function slotDisplayName(slot: Slot): string {
  if (slot.system !== 'ams') return slot.name
  const match = /^ams(\d+)_(\d+)$/.exec(slot.id)
  if (!match) return slot.name
  const unit = Number(match[1])
  if (unit < 1 || unit > 26) return slot.name
  return `AMS-${String.fromCharCode(64 + unit)} slot ${match[2]}`
}

export function FabricationSettings() {
  const queryClient = useQueryClient()
  const localAccountManagement = useQuery({
    queryKey: ['local-account-management'],
    queryFn: () => request<{ available: boolean }>('/local-account-management'),
    retry: false,
  })
  const localCredentialSetup = localAccountManagement.data?.available === true
  const profiles = useQuery({
    queryKey: ['slicing-profiles'],
    queryFn: () => request<SlicingProfile[]>('/slicing-profiles'),
  })
  const materials = useQuery({
    queryKey: ['materials'],
    queryFn: () => request<MaterialDefinition[]>('/materials'),
  })
  const credentials = useQuery({
    queryKey: ['cloud-credential-status'],
    queryFn: () => request<CredentialStatus>('/cloud-credential-status'),
  })
  const devices = useQuery({
    queryKey: ['cloud-devices'],
    queryFn: () => request<CloudDevice[]>('/cloud-devices'),
    enabled: credentials.data?.configured === true,
  })
  const [selectedProfileId, setSelectedProfileId] = useState('bambu-h2d')
  const [selectedDeviceRef, setSelectedDeviceRef] = useState('')
  const [materialId, setMaterialId] = useState('red-pla')
  const [materialName, setMaterialName] = useState('Red PLA')
  const [materialFamily, setMaterialFamily] = useState('pla')
  const [materialColor, setMaterialColor] = useState('#FF0000')
  const [filamentProfile, setFilamentProfile] = useState('Bambu PLA Basic @BBL H2D')
  const [cloudFilamentIds, setCloudFilamentIds] = useState('GFA00')
  const selectedProfile = useMemo(
    () => profiles.data?.find((item) => item.profile_id === selectedProfileId),
    [profiles.data, selectedProfileId],
  )
  const boundDeviceUnavailable =
    credentials.data?.configured === true &&
    devices.isSuccess &&
    Boolean(selectedProfile?.cloud_device_ref) &&
    !(devices.data ?? []).some(
      (device) => device.device_ref === selectedProfile?.cloud_device_ref,
    )
  const slotObservation = useQuery({
    queryKey: [
      'profile-slot-observation',
      selectedProfile?.profile_id,
      selectedProfile?.revision,
      selectedProfile?.cloud_device_ref,
    ],
    queryFn: () =>
      request<ProfileSlotObservation>(
        `/slicing-profiles/${selectedProfile!.profile_id}/cloud-observation`,
        { method: 'POST' },
      ),
    enabled:
      credentials.data?.configured === true &&
      Boolean(selectedProfile?.spec.cloud_device_serial) &&
      !boundDeviceUnavailable,
    staleTime: 60_000,
    refetchOnWindowFocus: false,
    retry: false,
  })
  const observedSlots = useMemo(
    () =>
      new Map(
        [
          ...(slotObservation.data?.snapshot.ams_units.flatMap(
            (unit) => unit.trays,
          ) ?? []),
          ...(slotObservation.data?.snapshot.external_trays ?? []),
        ].map((tray) => [tray.slot_id, tray]),
      ),
    [slotObservation.data],
  )
  const observedMappings = useMemo(
    () =>
      new Map(
        (slotObservation.data?.material_mappings ?? []).map((mapping) => [
          mapping.cloud_filament_id,
          mapping,
        ]),
      ),
    [slotObservation.data],
  )
  const quantityAuthorizations = useMemo(
    () =>
      new Map(
        (slotObservation.data?.quantity_authorizations ?? []).map((item) => [
          item.slot_id,
          item,
        ]),
      ),
    [slotObservation.data],
  )
  const [quantityReview, setQuantityReview] = useState<{
    slot: Slot
    tray: ObservedTray
    mapping: FilamentMappingStatus | undefined
  } | null>(null)
  const [profileJson, setProfileJson] = useState('')

  useEffect(() => {
    setProfileJson(selectedProfile ? JSON.stringify(selectedProfile.spec, null, 2) : '')
  }, [selectedProfile])

  const saveProfile = useMutation({
    mutationFn: (profile: SlicingProfile) =>
      request(`/slicing-profiles/${profile.profile_id}`, {
        method: 'POST',
        headers: { 'If-Match': String(profile.revision) },
        body: JSON.stringify(profile.spec),
      }),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ['slicing-profiles'] })
      await queryClient.invalidateQueries({
        queryKey: ['profile-slot-observation'],
      })
    },
  })
  const authorizeUnknownQuantity = useMutation({
    mutationFn: (value: {
      slot: Slot
      tray: ObservedTray
      mapping: FilamentMappingStatus | undefined
    }) => {
      const authorization = quantityAuthorizations.get(value.slot.id)
      if (!authorization || !slotObservation.data) {
        throw new Error('Refresh the live slot observation before authorizing')
      }
      return request(
        `/slicing-profiles/${selectedProfileId}/unknown-quantity-slots/${encodeURIComponent(
          value.slot.id,
        )}/authorize`,
        {
          method: 'POST',
          body: JSON.stringify({
            cloud_snapshot_digest: slotObservation.data.snapshot.digest,
            tray_identity_digest: authorization.tray_identity_digest,
            acknowledged: true,
            authorized_by: 'local-web',
          }),
        },
      )
    },
    onSuccess: async () => {
      setQuantityReview(null)
      await queryClient.invalidateQueries({
        queryKey: ['profile-slot-observation'],
      })
    },
  })
  const revokeUnknownQuantity = useMutation({
    mutationFn: (slotId: string) =>
      request(
        `/slicing-profiles/${selectedProfileId}/unknown-quantity-slots/${encodeURIComponent(
          slotId,
        )}/authorize`,
        { method: 'DELETE' },
      ),
    onSuccess: async () => {
      await queryClient.invalidateQueries({
        queryKey: ['profile-slot-observation'],
      })
    },
  })
  const bindDevice = useMutation({
    mutationFn: () =>
      request(`/slicing-profiles/${selectedProfileId}/cloud-device`, {
        method: 'POST',
        body: JSON.stringify({ device_ref: selectedDeviceRef }),
      }),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ['slicing-profiles'] })
      await queryClient.invalidateQueries({
        queryKey: ['profile-slot-observation'],
      })
    },
  })
  const saveMaterial = useMutation({
    mutationFn: () =>
      request(`/materials/${materialId}`, {
        method: 'POST',
        body: JSON.stringify({
          display_name: materialName,
          manufacturer: null,
          product_line: null,
          sku: null,
          family: materialFamily,
          modifiers: [],
          filament_diameter_mm: 1.75,
          nominal_color: materialColor,
          measured_color: null,
          finish: 'standard',
          translucency: 'opaque',
          nozzle_temperature_c: [190, 230],
          bed_temperature_c: [35, 65],
          hardened_nozzle_required: false,
          supported_nozzle_diameters_mm: [0.4],
          supported_plate_ids: ['textured_pei', 'smooth_pei'],
          maximum_volumetric_speed: null,
          drying_temperature_c: 55,
          drying_hours: 8,
          slicer_filament_profile_id: filamentProfile,
          cloud_filament_ids: cloudFilamentIds
            .split(',')
            .map((item) => item.trim())
            .filter(Boolean),
          slicer_profile_digest: null,
          slicer_profile_dependency_digests: {},
        }),
      }),
    onSuccess: () =>
      void queryClient.invalidateQueries({ queryKey: ['materials'] }),
  })

  const toggleForbidden = (slotId: string) => {
    if (!selectedProfile) return
    const forbidden = new Set(selectedProfile.spec.default_slot_policy.forbidden_slot_ids)
    const allowedValue = selectedProfile.spec.default_slot_policy.allowed_slot_ids
    const allowed = allowedValue === null ? null : new Set(allowedValue)
    const masked = forbidden.has(slotId) || (allowed !== null && !allowed.has(slotId))
    if (masked) {
      forbidden.delete(slotId)
      allowed?.add(slotId)
    } else {
      forbidden.add(slotId)
      allowed?.delete(slotId)
    }
    saveProfile.mutate({
      ...selectedProfile,
      spec: {
        ...selectedProfile.spec,
        default_slot_policy: {
          ...selectedProfile.spec.default_slot_policy,
          forbidden_slot_ids: [...forbidden],
          allowed_slot_ids: allowed === null ? null : [...allowed],
        },
      },
    })
  }

  return (
    <main className="settings-layout">
      <header className="dashboard-header">
        <div>
          <div className="eyebrow">H2D slicing configuration</div>
          <h1>Slicing profiles &amp; cloud observations</h1>
          <p>
            Profiles are reviewed and versioned. Cloud devices and AMS inventory are live,
            read-only observations.
          </p>
        </div>
      </header>

      <section className="settings-grid">
        <BambuAccountConnection
          credentialStatus={credentials.data}
          devices={devices.data}
          localCredentialSetup={localCredentialSetup}
        />

        <section className="inspection-panel">
          <span className="section-label">Slicing profile</span>
          <select
            value={selectedProfileId}
            onChange={(event) => setSelectedProfileId(event.target.value)}
          >
            {(profiles.data ?? []).map((profile) => (
              <option key={profile.profile_id} value={profile.profile_id}>
                {profile.spec.display_name} · revision {profile.revision}
              </option>
            ))}
          </select>
          {selectedProfile && (
            <>
              <p>
                {selectedProfile.spec.build_volume.width_mm} ×{' '}
                {selectedProfile.spec.build_volume.depth_mm} ×{' '}
                {selectedProfile.spec.build_volume.height_mm} mm ·{' '}
                {selectedProfile.spec.toolheads.length} toolheads
              </p>
              <div className="profile-binding">
                <strong>
                  {selectedProfile.spec.cloud_device_name ?? 'No cloud H2D bound'}
                </strong>
                <small>
                  {selectedProfile.spec.cloud_device_serial
                    ? `Serial ${selectedProfile.spec.cloud_device_serial.slice(0, 3)}••••${selectedProfile.spec.cloud_device_serial.slice(-4)}`
                    : 'Select a bound cloud device below.'}
                </small>
              </div>
              {boundDeviceUnavailable && (
                <p className="part-warning">
                  This profile’s bound printer is unavailable under the connected account. Select
                  a detected H2D below to create a new profile revision.
                </p>
              )}
              <label>
                Bound cloud H2D
                <select
                  value={selectedDeviceRef}
                  onChange={(event) => setSelectedDeviceRef(event.target.value)}
                >
                  <option value="">Select detected H2D</option>
                  {(devices.data ?? [])
                    .filter((device) => device.model.toLowerCase().includes('h2d'))
                    .map((device) => (
                      <option key={device.device_ref} value={device.device_ref}>
                        {device.name} · {device.model} · {device.online ? 'online' : 'offline'}
                      </option>
                    ))}
                </select>
              </label>
              <button
                className="secondary-action"
                disabled={!selectedDeviceRef || bindDevice.isPending}
                onClick={() => bindDevice.mutate()}
              >
                Bind selected device in a new profile revision
              </button>
              <div className="slot-observation-actions">
                <div>
                  <strong>Live slot observation</strong>
                  <small>
                    {slotObservation.data
                      ? `Observed ${new Date(
                          slotObservation.data.snapshot.observed_at,
                        ).toLocaleString()}`
                      : slotObservation.isFetching
                        ? 'Reading H2D and AMS slots…'
                        : 'No live observation loaded.'}
                  </small>
                </div>
                <button
                  className="secondary-action"
                  disabled={
                    !selectedProfile.spec.cloud_device_serial ||
                    boundDeviceUnavailable ||
                    slotObservation.isFetching
                  }
                  onClick={() => void slotObservation.refetch()}
                >
                  {slotObservation.isFetching ? 'Refreshing slots…' : 'Refresh slots'}
                </button>
              </div>
              {slotObservation.error && (
                <p className="part-warning">{slotObservation.error.message}</p>
              )}
              <div className="slot-grid">
                {selectedProfile.spec.material_slots.map((slot) => {
                  const displayName = slotDisplayName(slot)
                  const forbidden =
                    selectedProfile.spec.default_slot_policy.forbidden_slot_ids.includes(
                      slot.id,
                    ) ||
                    (selectedProfile.spec.default_slot_policy.allowed_slot_ids !==
                      null &&
                      !selectedProfile.spec.default_slot_policy.allowed_slot_ids.includes(
                        slot.id,
                      ))
                  const observed = observedSlots.get(slot.id)
                  const mapping = observed?.material_profile_id
                    ? observedMappings.get(observed.material_profile_id)
                    : undefined
                  const mappingUsable = [
                    'official_exact',
                    'manual',
                    'generic_confirmed',
                  ].includes(mapping?.state ?? '') ||
                    (mapping?.state === 'upgrade_available' &&
                      Boolean(mapping.selected_profile_id))
                  const quantityAuthorization = quantityAuthorizations.get(slot.id)
                  const quantityUnknown =
                    Boolean(observed?.material) &&
                    observed?.estimated_remaining_g === null
                  const quantityAuthorized =
                    quantityAuthorization?.status === 'authorized_unknown'
                  const eligibility =
                    slotObservation.isFetching && !slotObservation.data
                      ? 'Reading live slot…'
                      : forbidden
                        ? 'Forbidden by profile'
                        : !observed
                          ? 'Not observed'
                          : !observed.material
                            ? 'Empty'
                            : !mappingUsable
                              ? 'Excluded · filament profile unmapped'
                              : quantityUnknown
                                ? quantityAuthorized
                                  ? '✓ Usable · quantity user-confirmed'
                                  : 'Quantity unknown · confirmation required'
                                : 'Allowed · available for assignment'
                  return (
                    <article
                      key={slot.id}
                      className={`slot-card ${forbidden ? 'forbidden' : ''}`}
                      aria-label={`${displayName}. ${eligibility}.`}
                    >
                      <strong>{displayName}</strong>
                      <small>
                        {slot.id} · {slot.system.toUpperCase()}
                      </small>
                      {observed ? (
                        <>
                          <span className="slot-card-material">
                            {observed.color && (
                              <i
                                aria-label={`Filament color ${observed.color}`}
                                style={{ background: observed.color }}
                              />
                            )}
                            {observed.material ?? 'Empty'}
                            {observed.material_sub_brand
                              ? ` · ${observed.material_sub_brand}`
                              : ''}
                            {observed.color ? ` · ${observed.color}` : ''}
                          </span>
                          {observed.material_profile_id && (
                            <small>
                              {observed.material_profile_id} →{' '}
                              {mapping?.selected_profile_id ??
                                mapping?.proposed_profile_id ??
                                'Unmapped'}
                            </small>
                          )}
                          <small>
                            {observed.estimated_remaining_g !== null
                              ? `${observed.remain_percentage}% · ~${observed.estimated_remaining_g} g`
                              : observed.material
                               ? 'Quantity unknown'
                                : 'No loaded filament'}
                          </small>
                        </>
                      ) : !slotObservation.isFetching ? (
                        <small>Not observed in the latest snapshot</small>
                      ) : null}
                      <span
                        className={`slot-card-eligibility ${
                          quantityUnknown
                            ? `quantity-status ${quantityAuthorized ? 'authorized' : ''}`
                            : ''
                        }`}
                      >
                        {eligibility}
                      </span>
                      {quantityUnknown &&
                        mappingUsable &&
                        quantityAuthorization &&
                        (quantityAuthorized ? (
                          <button
                            type="button"
                            className="slot-card-toggle"
                            disabled={revokeUnknownQuantity.isPending}
                            onClick={() => revokeUnknownQuantity.mutate(slot.id)}
                          >
                            Revoke quantity confirmation
                          </button>
                        ) : (
                          <button
                            type="button"
                            className="slot-card-toggle"
                            disabled={authorizeUnknownQuantity.isPending}
                            onClick={() =>
                              setQuantityReview({
                                slot,
                                tray: observed!,
                                mapping,
                              })
                            }
                          >
                            Mark usable without quantity estimate
                          </button>
                        ))}
                      <button
                        type="button"
                        className="slot-card-toggle"
                        disabled={saveProfile.isPending}
                        onClick={() => toggleForbidden(slot.id)}
                      >
                        {forbidden ? 'Allow slot' : 'Mask slot'}
                      </button>
                    </article>
                  )
                })}
              </div>
              <details>
                <summary>Advanced slicing profile JSON</summary>
                <textarea
                  rows={16}
                  value={profileJson}
                  onChange={(event) => setProfileJson(event.target.value)}
                />
                <button
                  className="secondary-action"
                  disabled={saveProfile.isPending}
                  onClick={() => {
                    try {
                      saveProfile.mutate({
                        ...selectedProfile,
                        spec: JSON.parse(profileJson),
                      })
                    } catch {
                      window.alert('Slicing profile JSON is invalid')
                    }
                  }}
                >
                  Save new profile revision
                </button>
              </details>
            </>
          )}
          {(saveProfile.error || bindDevice.error || devices.error) && (
            <p className="error-copy">
              {saveProfile.error?.message ??
                bindDevice.error?.message ??
                devices.error?.message}
            </p>
          )}
        </section>

        <section className="inspection-panel">
          <span className="section-label">Reusable material mapping</span>
          <label>
            Material ID
            <input value={materialId} onChange={(event) => setMaterialId(event.target.value)} />
          </label>
          <label>
            Name
            <input
              value={materialName}
              onChange={(event) => setMaterialName(event.target.value)}
            />
          </label>
          <label>
            Family
            <input
              value={materialFamily}
              onChange={(event) => setMaterialFamily(event.target.value)}
            />
          </label>
          <label>
            Color
            <input
              type="color"
              value={materialColor}
              onChange={(event) => setMaterialColor(event.target.value)}
            />
          </label>
          <label>
            Installed Bambu Studio filament profile
            <input
              value={filamentProfile}
              onChange={(event) => setFilamentProfile(event.target.value)}
            />
          </label>
          <label>
            Cloud filament IDs (comma separated)
            <input
              value={cloudFilamentIds}
              onChange={(event) => setCloudFilamentIds(event.target.value)}
            />
          </label>
          <p>
            Cloud IDs identify what is physically loaded. The server pins the complete local
            Bambu Studio filament-profile dependency graph used for slicing.
          </p>
          <button className="primary-action" onClick={() => saveMaterial.mutate()}>
            Save material revision
          </button>
          {saveMaterial.error && (
            <p className="error-copy">{saveMaterial.error.message}</p>
          )}
          <div className="material-list">
            {(materials.data ?? []).map((material) => (
              <div key={material.material_id}>
                <span
                  style={{
                    background:
                      material.spec.measured_color ?? material.spec.nominal_color,
                  }}
                />
                <strong>{material.spec.display_name}</strong>
                <small>
                  {material.spec.family} · cloud{' '}
                  {material.spec.cloud_filament_ids.join(', ') || 'unmapped'} · revision{' '}
                  {material.revision}
                </small>
                <small>
                  {material.spec.mapping_origin.replaceAll('_', ' ')} ·{' '}
                  {material.spec.source_profile_id ??
                    material.spec.slicer_filament_profile_id}
                </small>
              </div>
            ))}
          </div>
        </section>
      </section>
      {quantityReview && (
        <UnknownQuantityAuthorizationDialog
          color={quantityReview.tray.color}
          error={authorizeUnknownQuantity.error?.message}
          material={
            quantityReview.tray.material_sub_brand ??
            quantityReview.tray.material ??
            'Loaded filament'
          }
          onClose={() => {
            if (!authorizeUnknownQuantity.isPending) {
              authorizeUnknownQuantity.reset()
              setQuantityReview(null)
            }
          }}
          onConfirm={() => authorizeUnknownQuantity.mutate(quantityReview)}
          pending={authorizeUnknownQuantity.isPending}
          preset={
            quantityReview.mapping?.selected_profile_id ??
            quantityReview.mapping?.proposed_profile_id ??
            null
          }
          slotId={quantityReview.slot.id}
          slotLabel={slotDisplayName(quantityReview.slot)}
        />
      )}
    </main>
  )
}
