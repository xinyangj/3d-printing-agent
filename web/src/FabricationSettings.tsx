import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

const API = '/api/v1'

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const token = sessionStorage.getItem('printing-agent-admin-token')
  const response = await fetch(`${API}${path}`, {
    ...init,
    headers: {
      'Content-Type': 'application/json',
      ...(token ? { 'X-Printing-Agent-Token': token } : {}),
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
  forbidden_reason: string | null
}

type PrinterProfile = {
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
    submission: { driver_id: string; mode: string }
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
  }
}

type Spool = {
  id: string
  material_id: string
  material_revision: number
  material_digest: string
  initial_weight_g: number
  remaining_weight_g: number
  status: string
  printer_profile_id: string | null
  slot_id: string | null
}

export function FabricationSettings() {
  const queryClient = useQueryClient()
  const profiles = useQuery({
    queryKey: ['printer-profiles'],
    queryFn: () => request<PrinterProfile[]>('/printer-profiles'),
  })
  const materials = useQuery({
    queryKey: ['materials'],
    queryFn: () => request<MaterialDefinition[]>('/materials'),
  })
  const spools = useQuery({
    queryKey: ['spools'],
    queryFn: () => request<Spool[]>('/spools'),
  })
  const [selectedProfileId, setSelectedProfileId] = useState('bambu-h2d')
  const [adminToken, setAdminToken] = useState(
    sessionStorage.getItem('printing-agent-admin-token') ?? '',
  )
  const [materialId, setMaterialId] = useState('red-pla')
  const [materialName, setMaterialName] = useState('Red PLA')
  const [materialFamily, setMaterialFamily] = useState('pla')
  const [materialColor, setMaterialColor] = useState('#FF0000')
  const [filamentProfile, setFilamentProfile] = useState('Bambu PLA Basic @BBL H2D')
  const [spoolId, setSpoolId] = useState('')
  const [spoolMaterialId, setSpoolMaterialId] = useState('')
  const [spoolSlot, setSpoolSlot] = useState('')
  const [remainingWeight, setRemainingWeight] = useState(1000)
  const selectedProfile = useMemo(
    () => profiles.data?.find((item) => item.profile_id === selectedProfileId),
    [profiles.data, selectedProfileId],
  )
  const [profileJson, setProfileJson] = useState('')
  useEffect(() => {
    setProfileJson(
      selectedProfile ? JSON.stringify(selectedProfile.spec, null, 2) : '',
    )
  }, [selectedProfile])

  const saveProfile = useMutation({
    mutationFn: (profile: PrinterProfile) =>
      request(`/printer-profiles/${profile.profile_id}`, {
        method: 'POST',
        headers: { 'If-Match': String(profile.revision) },
        body: JSON.stringify(profile.spec),
      }),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ['printer-profiles'] })
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
          slicer_profile_digest: null,
          slicer_profile_dependency_digests: {},
        }),
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['materials'] })
      setSpoolMaterialId(materialId)
    },
  })
  const saveSpool = useMutation({
    mutationFn: () => {
      const material = materials.data?.find((item) => item.material_id === spoolMaterialId)
      if (!material) throw new Error('Select a saved material')
      return request('/spools', {
        method: 'POST',
        body: JSON.stringify({
          id: spoolId || crypto.randomUUID(),
          material_id: material.material_id,
          material_revision: material.revision,
          material_digest: material.digest,
          initial_weight_g: 1000,
          remaining_weight_g: remainingWeight,
          spool_core_weight_g: null,
          status: 'loaded',
          lot: null,
          barcode: null,
          location: 'H2D',
          printer_profile_id: selectedProfileId,
          slot_id: spoolSlot,
          dried_at: null,
          measured_color: null,
          notes: null,
          updated_at: new Date().toISOString(),
        }),
      })
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['spools'] })
      setSpoolId('')
    },
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
          <div className="eyebrow">Fabrication configuration</div>
          <h1>Printers, materials &amp; slots</h1>
          <p>Reusable profiles are versioned. Workflows retain immutable snapshots.</p>
        </div>
      </header>

      <section className="settings-grid">
        <section className="inspection-panel">
          <span className="section-label">Remote mutation authorization</span>
          <label>
            Admin API token
            <input
              type="password"
              value={adminToken}
              onChange={(event) => setAdminToken(event.target.value)}
            />
          </label>
          <button
            className="secondary-action"
            onClick={() => {
              if (adminToken) sessionStorage.setItem('printing-agent-admin-token', adminToken)
              else sessionStorage.removeItem('printing-agent-admin-token')
            }}
          >
            Save token for this browser session
          </button>
          <p>
            Required for mutations over direct LAN access. The token is kept in session storage.
          </p>
        </section>
        <section className="inspection-panel">
          <span className="section-label">Printer profile</span>
          <select value={selectedProfileId} onChange={(event) => setSelectedProfileId(event.target.value)}>
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
              <div className="slot-grid">
                {selectedProfile.spec.material_slots.map((slot) => {
                  const forbidden =
                    selectedProfile.spec.default_slot_policy.forbidden_slot_ids.includes(slot.id)
                  const loaded = spools.data?.find(
                    (spool) =>
                      spool.printer_profile_id === selectedProfile.profile_id &&
                      spool.slot_id === slot.id,
                  )
                  return (
                    <button
                      key={slot.id}
                      className={`slot-card ${forbidden ? 'forbidden' : ''}`}
                      disabled={saveProfile.isPending}
                      onClick={() => toggleForbidden(slot.id)}
                    >
                      <strong>{slot.name}</strong>
                      <small>{loaded ? `${loaded.material_id} · ${loaded.remaining_weight_g} g` : 'Empty'}</small>
                      <span>{forbidden ? 'Forbidden for agent' : 'Allowed for agent'}</span>
                    </button>
                  )
                })}
              </div>
              <details>
                <summary>Advanced printer specification JSON</summary>
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
                      window.alert('Printer profile JSON is invalid')
                    }
                  }}
                >
                  Save new profile revision
                </button>
              </details>
            </>
          )}
          {saveProfile.error && <p className="error-copy">{saveProfile.error.message}</p>}
        </section>

        <section className="inspection-panel">
          <span className="section-label">Reusable material</span>
          <label>Material ID<input value={materialId} onChange={(e) => setMaterialId(e.target.value)} /></label>
          <label>Name<input value={materialName} onChange={(e) => setMaterialName(e.target.value)} /></label>
          <label>Family<input value={materialFamily} onChange={(e) => setMaterialFamily(e.target.value)} /></label>
          <label>Color<input type="color" value={materialColor} onChange={(e) => setMaterialColor(e.target.value)} /></label>
          <label>Filament profile<input value={filamentProfile} onChange={(e) => setFilamentProfile(e.target.value)} /></label>
          <p>The server pins the complete installed filament-profile dependency graph.</p>
          <button className="primary-action" onClick={() => saveMaterial.mutate()}>
            Save material revision
          </button>
          <div className="material-list">
            {(materials.data ?? []).map((material) => (
              <div key={material.material_id}>
                <span style={{ background: material.spec.measured_color ?? material.spec.nominal_color }} />
                <strong>{material.spec.display_name}</strong>
                <small>{material.spec.family} · revision {material.revision}</small>
              </div>
            ))}
          </div>
        </section>

        <section className="inspection-panel">
          <span className="section-label">Physical spool</span>
          <label>Spool ID<input value={spoolId} placeholder="Auto-generated" onChange={(e) => setSpoolId(e.target.value)} /></label>
          <label>
            Material
            <select value={spoolMaterialId} onChange={(e) => setSpoolMaterialId(e.target.value)}>
              <option value="">Select material</option>
              {(materials.data ?? []).map((material) => (
                <option key={material.material_id} value={material.material_id}>
                  {material.spec.display_name}
                </option>
              ))}
            </select>
          </label>
          <label>
            Slot
            <select value={spoolSlot} onChange={(e) => setSpoolSlot(e.target.value)}>
              <option value="">Select slot</option>
              {(selectedProfile?.spec.material_slots ?? []).map((slot) => (
                <option key={slot.id} value={slot.id}>{slot.name}</option>
              ))}
            </select>
          </label>
          <label>Remaining grams<input type="number" value={remainingWeight} onChange={(e) => setRemainingWeight(Number(e.target.value))} /></label>
          <button
            className="primary-action"
            disabled={!spoolMaterialId || !spoolSlot}
            onClick={() => saveSpool.mutate()}
          >
            Save spool
          </button>
          {saveSpool.error && <p className="error-copy">{saveSpool.error.message}</p>}
        </section>
      </section>
    </main>
  )
}
