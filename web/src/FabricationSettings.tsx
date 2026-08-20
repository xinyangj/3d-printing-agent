import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

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
    default_slot_policy: { forbidden_slot_ids: string[] }
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
  }
}

type CredentialStatus = {
  configured: boolean
  region: 'global' | 'china' | null
  protection: string
}

type CloudDevice = {
  device_id: string
  device_ref: string
  name: string
  model: string
  online: boolean
}

export function FabricationSettings() {
  const queryClient = useQueryClient()
  const localCredentialSetup = ['127.0.0.1', 'localhost', '::1'].includes(
    window.location.hostname,
  )
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
  const [cloudRegion, setCloudRegion] = useState<'global' | 'china'>('global')
  const [cloudToken, setCloudToken] = useState('')
  const [riskAccepted, setRiskAccepted] = useState(false)
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
  const [profileJson, setProfileJson] = useState('')

  useEffect(() => {
    setProfileJson(selectedProfile ? JSON.stringify(selectedProfile.spec, null, 2) : '')
  }, [selectedProfile])

  const saveCredential = useMutation({
    mutationFn: () =>
      request('/cloud-credentials', {
        method: 'POST',
        body: JSON.stringify({
          access_token: cloudToken,
          region: cloudRegion,
          experimental_acknowledged: riskAccepted,
        }),
      }),
    onSuccess: () => {
      setCloudToken('')
      void queryClient.invalidateQueries({ queryKey: ['cloud-credential-status'] })
      void queryClient.invalidateQueries({ queryKey: ['cloud-devices'] })
    },
  })
  const clearCredential = useMutation({
    mutationFn: () => request('/cloud-credentials', { method: 'DELETE' }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['cloud-credential-status'] })
      queryClient.removeQueries({ queryKey: ['cloud-devices'] })
    },
  })
  const saveProfile = useMutation({
    mutationFn: (profile: SlicingProfile) =>
      request(`/slicing-profiles/${profile.profile_id}`, {
        method: 'POST',
        headers: { 'If-Match': String(profile.revision) },
        body: JSON.stringify(profile.spec),
      }),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ['slicing-profiles'] })
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
    if (forbidden.has(slotId)) forbidden.delete(slotId)
    else forbidden.add(slotId)
    saveProfile.mutate({
      ...selectedProfile,
      spec: {
        ...selectedProfile.spec,
        default_slot_policy: {
          ...selectedProfile.spec.default_slot_policy,
          forbidden_slot_ids: [...forbidden],
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
        <section className="inspection-panel">
          <span className="section-label">Experimental Bambu Cloud inventory</span>
          <strong>
            {credentials.data?.configured
              ? `Token protected with DPAPI · ${credentials.data.region}`
              : 'No cloud token configured'}
          </strong>
          <p>
            This uses an unsupported private API only to read bound devices and AMS state. It
            cannot upload files, submit jobs, or control a printer.
          </p>
          {localCredentialSetup ? (
            <>
              <label>
                Region
                <select
                  value={cloudRegion}
                  onChange={(event) =>
                    setCloudRegion(event.target.value as 'global' | 'china')
                  }
                >
                  <option value="global">Global</option>
                  <option value="china">China</option>
                </select>
              </label>
              <label>
                Existing Bambu access token
                <input
                  autoComplete="off"
                  type="password"
                  value={cloudToken}
                  onChange={(event) => setCloudToken(event.target.value)}
                />
              </label>
              <label className="checkbox-label">
                <input
                  type="checkbox"
                  checked={riskAccepted}
                  onChange={(event) => setRiskAccepted(event.target.checked)}
                />
                I understand this private API is unofficial and may change or revoke access.
              </label>
              <div className="settings-actions">
                <button
                  className="primary-action"
                  disabled={!cloudToken.trim() || !riskAccepted || saveCredential.isPending}
                  onClick={() => saveCredential.mutate()}
                >
                  Validate &amp; save encrypted token
                </button>
                {credentials.data?.configured && (
                  <button
                    className="danger-action"
                    disabled={clearCredential.isPending}
                    onClick={() => clearCredential.mutate()}
                  >
                    Remove cloud token
                  </button>
                )}
              </div>
            </>
          ) : (
            <p className="part-warning">
              Token setup is available only from the server at http://127.0.0.1:8000.
            </p>
          )}
          {(saveCredential.error || clearCredential.error) && (
            <p className="error-copy">
              {saveCredential.error?.message ?? clearCredential.error?.message}
            </p>
          )}
        </section>

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
              <div className="slot-grid">
                {selectedProfile.spec.material_slots.map((slot) => {
                  const forbidden =
                    selectedProfile.spec.default_slot_policy.forbidden_slot_ids.includes(
                      slot.id,
                    )
                  return (
                    <button
                      key={slot.id}
                      className={`slot-card ${forbidden ? 'forbidden' : ''}`}
                      disabled={saveProfile.isPending}
                      onClick={() => toggleForbidden(slot.id)}
                    >
                      <strong>{slot.name}</strong>
                      <small>Observed state appears in the slicing workspace</small>
                      <span>{forbidden ? 'Forbidden for assignment' : 'Allowed for assignment'}</span>
                    </button>
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
              </div>
            ))}
          </div>
        </section>
      </section>
    </main>
  )
}
