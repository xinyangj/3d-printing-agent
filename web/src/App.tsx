import { useCallback, useEffect, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import * as THREE from 'three'
import { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js'
import { STLLoader } from 'three/examples/jsm/loaders/STLLoader.js'
import './App.css'
import { FabricationSettings } from './FabricationSettings'
import { MultipartPartsViewer } from './MultipartModelViewer'

type Dimensions = {
  width_mm: number
  depth_mm: number
  height_mm: number
}

type Workflow = {
  id: string
  requirement: string
  printer_name: string
  state: string
  active_artifact_version: number | null
  failure_message: string | null
  archived_at: string | null
  created_at: string
  updated_at: string
}

type Artifact = {
  workflow_id: string
  version: number
  source_available: boolean
  model_digest: string
  manifest_digest: string
  mesh: {
    dimensions: Dimensions
    triangle_count: number
    connected_components: number
    watertight: boolean
    volume_mm3: number
  }
  provenance: {
    kind: 'catalog' | 'generated'
    candidate_id: string | null
    source_url: string | null
    creator: string | null
    license: string | null
  }
  project: {
    parts: Array<{
      id: string
      name: string
      geometry_kind: 'parametric' | 'imported_mesh' | 'derived_mesh'
      annotation_origin: string
      confidence: number
      material_id: string | null
    }>
    instances: Array<{
      id: string
      part_id: string
      name: string
      transform: number[]
    }>
    materials: Array<{ id: string; name: string; color: string }>
    assembly_status: 'provided' | 'not_provided' | 'unknown'
    interfaces: Array<{
      id: string
      part_ids: string[]
      interface_type: string
      evidence: string
      rationale: string
      fit_verified: boolean
    }>
    warnings: string[]
  } | null
  downloads: Array<{
    role: string
    path: string
    media_type: string
    part_id: string | null
  }>
  package: {
    url: string
    size_bytes: number
    filename: string
  }
  revision: {
    feedback: string
    affected_part_ids: string[]
    allowed_part_ids: string[] | null
    rationale: string
  } | null
}

type PrintJob = {
  id: string
  external_id: string
  status: string
  message: string | null
}

type RevisionVerification = {
  id: string
  base_artifact_version: number
  candidate_artifact_version: number
  feedback: string
  verdict: 'passed' | 'failed'
  repairable: boolean
  rationale: string
  checks: Array<{
    id: string
    passed: boolean
    message: string
    evidence: Record<string, unknown>
  }>
  created_at: string
}

type WorkflowResponse = {
  workflow: Workflow
  artifact: Artifact | null
  job: PrintJob | null
  revision_failure: RevisionVerification | null
  revision_verification: RevisionVerification | null
  printer_snapshot: {
    profile_id: string
    profile_revision: number
    digest: string
    profile: PrinterProfile['spec']
    overrides: JobOverrides
    resolved_slot_policy: {
      forbidden_slot_ids: string[]
      allowed_slot_ids: string[] | null
    }
  } | null
  material_assignment: MaterialAssignmentPayload | null
  material_assignment_stale: boolean
  slice_job: SliceJobPayload | null
  sliced_artifact: SlicedArtifactPayload | null
  cloud_snapshot: CloudDeviceSnapshotPayload | null
  material_mappings: FilamentMappingStatus[]
}

type Printer = {
  name: string
  build_volume: Dimensions
  accepted_formats: string[]
  supported_materials: string[]
}

type JobOverrides = {
  toolhead_id: string | null
  nozzle_diameter_mm: number | null
  plate_id: string | null
  layer_height_mm: number | null
  infill_percent: number | null
  supports: boolean | null
  brim: boolean | null
  raft: boolean | null
  timelapse: boolean | null
  calibration: boolean | null
  forbidden_slot_ids: string[]
  allowed_slot_ids: string[] | null
  part_allowed_slot_ids: Record<string, string[]>
  part_forbidden_slot_ids: Record<string, string[]>
  allow_manual_swaps: boolean | null
  maximum_color_distance: number
  material_safety_margin_percent: number
}

type PrinterProfile = {
  profile_id: string
  revision: number
  digest: string
  spec: {
    display_name: string
    build_volume: Dimensions
    toolheads: Array<{ id: string; name: string; nozzle_diameter_mm: number }>
    plates: Array<{ id: string; name: string }>
    material_slots: Array<{ id: string; name: string; automatic_assignment: boolean }>
    slicer: {
      driver_id: string
      machine_profile_id: string
      process_profile_id: string
    }
  }
}

type FabricationReadiness = {
  profile_id: string
  profile_revision: number
  display_name: string
  slicing_capable: boolean
  ready_for_fabrication: boolean
  slicer: { ready: boolean; message: string }
  cloud_binding: {
    configured: boolean
    available: boolean
    device_name: string | null
    message: string
  }
}

type CloudDeviceSnapshotPayload = {
  id: string
  digest: string
  observed_at: string
  expires_at: string
  completeness: 'complete'
  region: 'global' | 'china'
  device: {
    device_id: string
    name: string
    online: boolean
    model: string
  }
  installed_nozzles: Array<{
    position: 'left' | 'right'
    diameter_mm: number
    nozzle_type: string
  }>
  ams_units: Array<{
    unit_id: string
    kind: 'ams' | 'ams_ht'
    trays: Array<{
      slot_id: string
      material: string | null
      material_profile_id: string | null
      color: string | null
      remain_percentage: number | null
      estimated_remaining_g: number | null
    }>
  }>
  external_trays: Array<{
    slot_id: string
    material: string | null
    material_profile_id: string | null
    color: string | null
    remain_percentage: number | null
    estimated_remaining_g: number | null
  }>
  warnings: string[]
}

type FilamentMappingStatus = {
  cloud_filament_id: string
  slots: string[]
  observed_material: string | null
  observed_sub_brands: string[]
  state:
    | 'official_exact'
    | 'manual'
    | 'generic_confirmed'
    | 'confirmation_required'
    | 'upgrade_available'
    | 'ambiguous'
    | 'missing'
  material_id: string | null
  selected_profile_id: string | null
  proposed_profile_id: string | null
  proposed_profile_digest: string | null
  reason: string
}

type CloudCredentialStatusPayload = {
  configured: boolean
  region: 'global' | 'china' | null
}

type MaterialAssignmentPayload = {
  id: string
  requires_confirmation: boolean
  confirmed_at: string | null
  digest: string
  recovery_round: number
  usage_basis: 'geometry_estimate' | 'sliced_usage'
  source_assessment_digest: string | null
  recovery_explanation: string | null
  requests: Array<{
    part_id: string
    part_name: string
    requested_color: string
    estimated_weight_g: number | null
  }>
  assignments: Array<{
    part_id: string
    spool_id: string
    slot_id: string
    toolhead_id: string
    material_id: string
    color_distance: number
    confidence: number
    rationale: string
    alternatives: string[]
  }>
  candidate_options: Record<
    string,
    Array<{
      spool_id: string
      slot_id: string
      material_id: string
      color: string
      color_distance: number
      remaining_weight_g: number
      warnings: string[]
    }>
  >
}

type SliceJobPayload = {
  id: string
  status: string
  message: string | null
  updated_at: string
  recovery_round: number
  failure_category: 'insufficient_material' | 'slicing_error' | null
  material_assessment: {
    recovery_round: number
    status:
      | 'sufficient'
      | 'replacement_proposed'
      | 'load_required'
      | 'manual_intervention_required'
    digest: string
    requirements: Array<{
      spool_id: string
      slot_id: string | null
      actual_usage_g: number
      safety_margin_percent: number
      required_weight_g: number
      available_weight_g: number
      shortfall_g: number
      affected_part_ids: string[]
    }>
  } | null
}

type SlicedArtifactPayload = {
  slice_job_id: string
  digest: string
  size_bytes: number
  slicer_version: string
  machine_profile_id: string
  process_profile_id: string
  estimated_time_seconds: number | null
  filament_usage_g: Record<string, number>
  warnings: string[]
  thumbnail_available: boolean
  manifest_digest: string
}

type WorkflowEvent = {
  id: number
  kind: string
  state: string
  payload: Record<string, unknown>
  created_at: string
}

const API = '/api/v1'

function artifactPreviewUrl(workflowId: string, artifact: Artifact): string {
  const firstPart = artifact.downloads.find((item) => item.role === 'part_stl')
  const path = firstPart?.path ?? 'model.stl'
  return `${API}/workflows/${workflowId}/artifacts/${artifact.version}/${path}`
}
const TERMINAL_STATES = new Set([
  'completed',
  'cancelled',
  'preparation_failed',
  'print_failed',
  'slice_failed',
])
const INSPECTABLE_STATE = new Set([
  'awaiting_approval',
  'approved',
  'submitting',
  'queued',
  'printing',
  'completed',
  'print_failed',
  'slice_requested',
  'slicing',
  'slice_validating',
  'awaiting_slice_review',
  'slice_failed',
])
const ACTIVE_STATES = new Set([
  'received',
  'planning',
  'discovering',
  'searching',
  'page_inspection',
  'selecting',
  'source_validation',
  'handoff_ready',
  'generating',
  'rendering',
  'validating',
  'revision_requested',
  'submitting',
  'queued',
  'printing',
  'slice_requested',
  'slicing',
  'slice_validating',
])
const ARCHIVABLE_STATES = new Set([
  'awaiting_approval',
  'approved',
  'completed',
  'preparation_failed',
  'print_failed',
  'cancelled',
])

type DashboardFilter =
  | 'all'
  | 'in_progress'
  | 'needs_approval'
  | 'approved'
  | 'printing'
  | 'completed'
  | 'failed'
  | 'archived'

const FILTERS: { value: DashboardFilter; label: string }[] = [
  { value: 'all', label: 'All' },
  { value: 'in_progress', label: 'In progress' },
  { value: 'needs_approval', label: 'Needs approval' },
  { value: 'approved', label: 'Approved' },
  { value: 'printing', label: 'Printing' },
  { value: 'completed', label: 'Completed' },
  { value: 'failed', label: 'Failed / cancelled' },
  { value: 'archived', label: 'Archived' },
]

async function api<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(`${API}${path}`, {
    ...options,
    headers: {
      'Content-Type': 'application/json',
      ...options?.headers,
    },
  })
  if (!response.ok) {
    const payload = await response.json().catch(() => null)
    throw new Error(payload?.error?.message ?? `Request failed (${response.status})`)
  }
  return response.json() as Promise<T>
}

function formatState(value: string) {
  return value.replaceAll('_', ' ')
}

function formatNumber(value: number, digits = 1) {
  return new Intl.NumberFormat(undefined, { maximumFractionDigits: digits }).format(value)
}

function matchesFilter(workflow: Workflow, filter: DashboardFilter) {
  if (filter === 'archived') return workflow.archived_at !== null
  if (workflow.archived_at !== null) return false
  const state = workflow.state
  if (filter === 'all') return true
  if (filter === 'in_progress')
    return (
      ACTIVE_STATES.has(state) &&
      !['submitting', 'queued', 'printing', 'slice_requested', 'slicing', 'slice_validating'].includes(
        state,
      )
    )
  if (filter === 'needs_approval') return state === 'awaiting_approval'
  if (filter === 'approved') return state === 'approved'
  if (filter === 'printing')
    return [
      'submitting',
      'queued',
      'printing',
      'slice_setup',
      'awaiting_material_review',
      'slice_requested',
      'slicing',
      'slice_validating',
      'awaiting_slice_review',
    ].includes(state)
  if (filter === 'completed') return state === 'completed'
  return ['preparation_failed', 'print_failed', 'slice_failed', 'cancelled'].includes(state)
}

function useWorkflowEvents(workflowId: string | null) {
  const [events, setEvents] = useState<WorkflowEvent[]>([])
  const queryClient = useQueryClient()

  useEffect(() => {
    setEvents([])
    if (!workflowId) return
    const source = new EventSource(`${API}/workflows/${workflowId}/events`)
    const handleEvent = (message: MessageEvent<string>) => {
      const event = JSON.parse(message.data) as WorkflowEvent
      setEvents((current) =>
        current.some((item) => item.id === event.id) ? current : [...current, event],
      )
      void queryClient.invalidateQueries({ queryKey: ['workflow', workflowId] })
    }
    source.onmessage = handleEvent
    source.addEventListener('workflow.created', handleEvent as EventListener)
    source.addEventListener('stream.complete', () => source.close())
    const knownEvents = [
      'preparation.started',
      'role.tool_correction',
      'discovery.plan_ready',
      'discovery.searching',
      'discovery.search_complete',
      'discovery.page_inspection',
      'discovery.page_inspection_complete',
      'discovery.selection',
      'source.download_started',
      'source.rejected',
      'source_set.download_started',
      'source_set.adopting',
      'candidate.rejected',
      'candidate.generation_fallback',
      'modeling.handoff_ready',
      'modeling.started',
      'modeling.session_retry',
      'model.rendering',
      'model.validating',
      'model.repair_requested',
      'artifact.ready',
      'revision.requested',
      'revision.verification_repair_requested',
      'revision.verification_passed',
      'revision.verification_failed',
      'artifact.approved',
      'print.submitting',
      'print.queued',
      'print.printing',
      'print.completed',
      'workflow.failed',
      'workflow.archived',
      'workflow.restored',
    ]
    knownEvents.forEach((name) => source.addEventListener(name, handleEvent as EventListener))
    return () => source.close()
  }, [queryClient, workflowId])

  return events
}

function ModelViewer({
  url,
  dimensions,
}: {
  url: string
  dimensions: Dimensions
}) {
  const containerRef = useRef<HTMLDivElement>(null)
  const [wireframe, setWireframe] = useState(false)
  const [orthographic, setOrthographic] = useState(false)

  useEffect(() => {
    const container = containerRef.current
    if (!container) return
    const width = container.clientWidth
    const height = container.clientHeight
    const scene = new THREE.Scene()
    scene.background = new THREE.Color('#111519')
    const aspect = width / height
    const camera: THREE.PerspectiveCamera | THREE.OrthographicCamera = orthographic
      ? new THREE.OrthographicCamera(-100 * aspect, 100 * aspect, 100, -100, 0.1, 5000)
      : new THREE.PerspectiveCamera(42, aspect, 0.1, 5000)
    camera.position.set(180, 150, 180)

    const renderer = new THREE.WebGLRenderer({ antialias: true })
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2))
    renderer.setSize(width, height)
    renderer.outputColorSpace = THREE.SRGBColorSpace
    renderer.shadowMap.enabled = true
    container.appendChild(renderer.domElement)

    const controls = new OrbitControls(camera, renderer.domElement)
    controls.enableDamping = true
    controls.dampingFactor = 0.08
    const ambient = new THREE.HemisphereLight('#eaf7ff', '#25301d', 2.1)
    scene.add(ambient)
    const key = new THREE.DirectionalLight('#ffffff', 3.2)
    key.position.set(120, 180, 100)
    key.castShadow = true
    scene.add(key)
    const fill = new THREE.DirectionalLight('#73d7c6', 1.2)
    fill.position.set(-100, 70, -80)
    scene.add(fill)

    const gridSize = Math.max(240, dimensions.width_mm * 1.5, dimensions.depth_mm * 1.5)
    const grid = new THREE.GridHelper(gridSize, 20, '#4e645f', '#25302e')
    scene.add(grid)
    const axes = new THREE.AxesHelper(Math.min(60, gridSize / 4))
    scene.add(axes)

    let mesh: THREE.Mesh | null = null
    let frame = 0
    const loader = new STLLoader()
    loader.load(
      url,
      (geometry) => {
        geometry.computeVertexNormals()
        geometry.center()
        geometry.computeBoundingBox()
        const box = geometry.boundingBox!
        geometry.translate(0, -box.min.y, 0)
        const material = new THREE.MeshStandardMaterial({
          color: '#79e2ca',
          roughness: 0.42,
          metalness: 0.08,
          wireframe,
        })
        mesh = new THREE.Mesh(geometry, material)
        mesh.castShadow = true
        mesh.receiveShadow = true
        scene.add(mesh)
        const bounds = new THREE.Box3().setFromObject(mesh)
        const size = bounds.getSize(new THREE.Vector3())
        const maxDimension = Math.max(size.x, size.y, size.z)
        camera.position.set(maxDimension * 1.5, maxDimension * 1.2, maxDimension * 1.5)
        controls.target.set(0, size.y / 2, 0)
        if (camera instanceof THREE.OrthographicCamera) {
          const span = Math.max(30, maxDimension * 0.9)
          camera.left = -span * aspect
          camera.right = span * aspect
          camera.top = span
          camera.bottom = -span
          camera.updateProjectionMatrix()
        }

        controls.update()
        scene.add(new THREE.Box3Helper(bounds, new THREE.Color('#f2b45e')))
      },
      undefined,
      () => {
        scene.background = new THREE.Color('#351a1d')
      },
    )

    const resize = new ResizeObserver(() => {
      const nextWidth = container.clientWidth
      const nextHeight = container.clientHeight
      renderer.setSize(nextWidth, nextHeight)
      if (camera instanceof THREE.PerspectiveCamera) {
        camera.aspect = nextWidth / nextHeight
      } else {
        const span = (camera.top - camera.bottom) / 2
        const nextAspect = nextWidth / nextHeight
        camera.left = -span * nextAspect
        camera.right = span * nextAspect
      }
      camera.updateProjectionMatrix()
    })
    resize.observe(container)

    const animate = () => {
      controls.update()
      renderer.render(scene, camera)
      frame = requestAnimationFrame(animate)
    }
    animate()
    return () => {
      cancelAnimationFrame(frame)
      resize.disconnect()
      controls.dispose()
      scene.traverse((object) => {
        if (object instanceof THREE.Mesh) {
          object.geometry.dispose()
          if (Array.isArray(object.material)) object.material.forEach((item) => item.dispose())
          else object.material.dispose()
        }
      })
      renderer.dispose()
      container.removeChild(renderer.domElement)
    }
  }, [dimensions.depth_mm, dimensions.height_mm, dimensions.width_mm, orthographic, url, wireframe])

  return (
    <div className="viewer-shell">
      <div className="viewer-toolbar">
        <button className={wireframe ? 'active' : ''} onClick={() => setWireframe(!wireframe)}>
          Wireframe
        </button>
        <button className={orthographic ? 'active' : ''} onClick={() => setOrthographic(!orthographic)}>
          {orthographic ? 'Orthographic' : 'Perspective'}
        </button>
      </div>
      <div className="model-viewer" ref={containerRef} aria-label="Interactive 3D model viewer" />
      <div className="dimension-ribbon">
        <span>W {formatNumber(dimensions.width_mm)} mm</span>
        <span>D {formatNumber(dimensions.depth_mm)} mm</span>
        <span>H {formatNumber(dimensions.height_mm)} mm</span>
      </div>
    </div>
  )
}

function StartPanel({ onCreated }: { onCreated: (id: string) => void }) {
  const [requirement, setRequirement] = useState(
    'Create a compact wall-mounted headphone hook, 70 mm tall, with rounded edges, printed in PLA.',
  )
  const [printer, setPrinter] = useState('simulator')
  const [advanced, setAdvanced] = useState(false)
  const [layerHeight, setLayerHeight] = useState(0.2)
  const [infill, setInfill] = useState(20)
  const [supports, setSupports] = useState(false)
  const [plateId, setPlateId] = useState<string | null>(null)
  const [toolheadId, setToolheadId] = useState<string | null>(null)
  const [forbiddenSlots, setForbiddenSlots] = useState<string[]>([])
  const [maximumColorDistance, setMaximumColorDistance] = useState(12)
  const printers = useQuery({ queryKey: ['printers'], queryFn: () => api<Printer[]>('/printers') })
  const profiles = useQuery({
    queryKey: ['printer-profiles'],
    queryFn: () => api<PrinterProfile[]>('/slicing-profiles'),
  })
  const selectedProfile = profiles.data?.find((item) => item.profile_id === printer)
  const create = useMutation({
    mutationFn: () =>
      api<Workflow>('/workflows', {
        method: 'POST',
        body: JSON.stringify({
          requirement,
          printer_name: printer,
          overrides: {
            toolhead_id: toolheadId,
            nozzle_diameter_mm: null,
            plate_id: plateId,
            layer_height_mm: layerHeight,
            infill_percent: infill,
            supports,
            brim: null,
            raft: null,
            timelapse: null,
            calibration: null,
            forbidden_slot_ids: forbiddenSlots,
            allowed_slot_ids: null,
            part_allowed_slot_ids: {},
            part_forbidden_slot_ids: {},
            allow_manual_swaps: true,
            maximum_color_distance: maximumColorDistance,
            material_safety_margin_percent: 15,
          },
        }),
      }),
    onSuccess: (workflow) => onCreated(workflow.id),
  })

  return (
    <section className="start-layout">
      <section className="hero-copy">
        <div className="eyebrow">Copilot-powered fabrication</div>
        <h1>Describe it. Inspect it. Print it.</h1>
        <p>
          The discovery agent searches model pages and creator galleries. A separate modeling
          agent modifies a close match or generates a printable OpenSCAD design.
        </p>
        <div className="boundary-note">
          <span className="boundary-icon">✓</span>
          No model reaches a printer until you approve its exact validated artifact.
        </div>
      </section>
      <section className="request-card">
        <label htmlFor="requirement">What should we make?</label>
        <textarea
          id="requirement"
          value={requirement}
          onChange={(event) => setRequirement(event.target.value)}
          rows={8}
        />
        <label htmlFor="printer">Target printer</label>
        <select id="printer" value={printer} onChange={(event) => setPrinter(event.target.value)}>
          {(printers.data ?? []).map((item) => (
            <option value={item.name} key={item.name}>
              {item.name} · {item.build_volume.width_mm} × {item.build_volume.depth_mm} ×{' '}
              {item.build_volume.height_mm} mm
            </option>
          ))}
          {!printers.data?.length && <option value="simulator">simulator</option>}
        </select>
        <button className="text-button" onClick={() => setAdvanced(!advanced)}>
          {advanced ? 'Hide job overrides' : 'Configure job overrides'}
        </button>
        {advanced && selectedProfile && (
          <div className="job-overrides">
            <label>
              Toolhead
              <select value={toolheadId ?? ''} onChange={(event) => setToolheadId(event.target.value || null)}>
                <option value="">Profile default</option>
                {selectedProfile.spec.toolheads.map((item) => (
                  <option key={item.id} value={item.id}>
                    {item.name} · {item.nozzle_diameter_mm} mm
                  </option>
                ))}
              </select>
            </label>
            <label>
              Plate
              <select value={plateId ?? ''} onChange={(event) => setPlateId(event.target.value || null)}>
                <option value="">Profile default</option>
                {selectedProfile.spec.plates.map((item) => (
                  <option key={item.id} value={item.id}>{item.name}</option>
                ))}
              </select>
            </label>
            <label>
              Layer height
              <input type="number" min="0.05" max="1" step="0.05" value={layerHeight} onChange={(event) => setLayerHeight(Number(event.target.value))} />
            </label>
            <label>
              Infill %
              <input type="number" min="0" max="100" value={infill} onChange={(event) => setInfill(Number(event.target.value))} />
            </label>
            <label className="checkbox-label">
              <input type="checkbox" checked={supports} onChange={(event) => setSupports(event.target.checked)} />
              Generate supports
            </label>
            <label>
              Maximum color distance
              <input type="number" min="0" max="100" step="0.5" value={maximumColorDistance} onChange={(event) => setMaximumColorDistance(Number(event.target.value))} />
            </label>
            <span className="section-label">Forbidden slots for this job</span>
            <div className="override-slots">
              {selectedProfile.spec.material_slots.map((slot) => (
                <label key={slot.id} className="checkbox-label">
                  <input
                    type="checkbox"
                    checked={forbiddenSlots.includes(slot.id)}
                    onChange={() =>
                      setForbiddenSlots((current) =>
                        current.includes(slot.id)
                          ? current.filter((id) => id !== slot.id)
                          : [...current, slot.id],
                      )
                    }
                  />
                  {slot.name}
                </label>
              ))}
            </div>
          </div>
        )}
        <button
          className="primary-action"
          disabled={!requirement.trim() || create.isPending}
          onClick={() => create.mutate()}
        >
          {create.isPending ? 'Starting workflow…' : 'Prepare model'}
          <span>→</span>
        </button>
        {create.error && <p className="error-copy">{create.error.message}</p>}
      </section>
    </section>
  )
}

function ModelThumbnail({ url }: { url: string }) {
  const containerRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const container = containerRef.current
    if (!container) return
    const scene = new THREE.Scene()
    scene.background = new THREE.Color('#171b1c')
    const camera = new THREE.PerspectiveCamera(38, 1.6, 0.1, 5000)
    const renderer = new THREE.WebGLRenderer({ antialias: true })
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 1.5))
    renderer.setSize(container.clientWidth, container.clientHeight)
    container.appendChild(renderer.domElement)
    scene.add(new THREE.HemisphereLight('#ffffff', '#24332f', 2.4))
    const key = new THREE.DirectionalLight('#ffffff', 2.5)
    key.position.set(100, 140, 80)
    scene.add(key)
    let mesh: THREE.Mesh | null = null
    new STLLoader().load(url, (geometry) => {
      geometry.computeVertexNormals()
      geometry.center()
      mesh = new THREE.Mesh(
        geometry,
        new THREE.MeshStandardMaterial({ color: '#79e2ca', roughness: 0.48 }),
      )
      scene.add(mesh)
      const size = new THREE.Box3().setFromObject(mesh).getSize(new THREE.Vector3())
      const span = Math.max(size.x, size.y, size.z)
      camera.position.set(span * 1.5, span * 1.1, span * 1.5)
      camera.lookAt(0, 0, 0)
    })
    let frame = 0
    const render = () => {
      if (mesh) mesh.rotation.y += 0.003
      renderer.render(scene, camera)
      frame = requestAnimationFrame(render)
    }
    render()
    return () => {
      cancelAnimationFrame(frame)
      if (mesh) {
        mesh.geometry.dispose()
        if (Array.isArray(mesh.material)) mesh.material.forEach((item) => item.dispose())
        else mesh.material.dispose()
      }
      renderer.dispose()
      container.removeChild(renderer.domElement)
    }
  }, [url])

  return <div className="model-thumbnail" ref={containerRef} aria-label="3D model preview" />
}

function PrepareH2DSliceDialog({
  source,
  profiles,
  readiness,
  onClose,
  onCreated,
}: {
  source: WorkflowResponse
  profiles: PrinterProfile[]
  readiness: FabricationReadiness[]
  onClose: () => void
  onCreated: (id: string) => void
}) {
  const slicingProfiles = profiles.filter(
    (profile) => profile.spec.slicer.driver_id !== 'simulator_passthrough',
  )
  const [profileId, setProfileId] = useState(
    slicingProfiles.find((profile) => profile.profile_id === 'bambu-h2d')?.profile_id ??
      slicingProfiles[0]?.profile_id ??
      '',
  )
  const [toolheadId, setToolheadId] = useState('')
  const [plateId, setPlateId] = useState('')
  const [layerHeight, setLayerHeight] = useState(0.2)
  const [infill, setInfill] = useState(20)
  const [supports, setSupports] = useState(false)
  const [allowManualSwaps, setAllowManualSwaps] = useState(true)
  const [maximumColorDistance, setMaximumColorDistance] = useState(12)
  const [safetyMargin, setSafetyMargin] = useState(15)
  const [forbiddenSlots, setForbiddenSlots] = useState<string[]>([])
  const selectedProfile = slicingProfiles.find((profile) => profile.profile_id === profileId)
  const selectedReadiness = readiness.find(
    (item) =>
      item.profile_id === profileId &&
      item.profile_revision === selectedProfile?.revision,
  )
  const credentialStatus = useQuery({
    queryKey: ['cloud-credential-status'],
    queryFn: () =>
      api<CloudCredentialStatusPayload>('/cloud-credential-status'),
  })
  const create = useMutation({
    mutationFn: () =>
      api<WorkflowResponse>(`/workflows/${source.workflow.id}/slicing-copies`, {
        method: 'POST',
        body: JSON.stringify({
          profile_id: profileId,
          overrides: {
            toolhead_id: toolheadId || null,
            nozzle_diameter_mm: null,
            plate_id: plateId || null,
            layer_height_mm: layerHeight,
            infill_percent: infill,
            supports,
            brim: null,
            raft: null,
            timelapse: null,
            calibration: null,
            forbidden_slot_ids: forbiddenSlots,
            allowed_slot_ids: null,
            part_allowed_slot_ids: {},
            part_forbidden_slot_ids: {},
            allow_manual_swaps: allowManualSwaps,
            maximum_color_distance: maximumColorDistance,
            material_safety_margin_percent: safetyMargin,
          } satisfies JobOverrides,
        }),
      }),
    onSuccess: (created) => onCreated(created.workflow.id),
  })
  const canCreate =
    Boolean(selectedProfile) &&
    selectedReadiness?.ready_for_fabrication === true &&
    credentialStatus.data?.configured === true

  const toggleForbidden = (slotId: string) => {
    setForbiddenSlots((current) =>
      current.includes(slotId)
        ? current.filter((candidate) => candidate !== slotId)
        : [...current, slotId],
    )
  }

  return (
    <div
      className="dialog-backdrop"
      onMouseDown={(event) => {
        if (event.currentTarget === event.target && !create.isPending) onClose()
      }}
    >
      <section
        aria-labelledby="print-with-bambu-title"
        aria-modal="true"
        className="fabrication-dialog"
        role="dialog"
      >
        <header>
          <div>
            <span className="eyebrow">Existing model → slicing workflow</span>
            <h2 id="print-with-bambu-title">Prepare H2D slice</h2>
            <p>{source.workflow.requirement}</p>
          </div>
          <button
            aria-label="Close printer setup"
            className="dialog-close"
            disabled={create.isPending}
            onClick={onClose}
          >
            ×
          </button>
        </header>

        <div className="dialog-grid">
          <div className="job-overrides">
            <label>
              Printer profile
              <select
                value={profileId}
                onChange={(event) => {
                  setProfileId(event.target.value)
                  setToolheadId('')
                  setPlateId('')
                  setForbiddenSlots([])
                }}
              >
                {slicingProfiles.map((profile) => (
                  <option key={profile.profile_id} value={profile.profile_id}>
                    {profile.spec.display_name}
                  </option>
                ))}
              </select>
            </label>
            <label>
              Toolhead
              <select value={toolheadId} onChange={(event) => setToolheadId(event.target.value)}>
                <option value="">Profile default</option>
                {(selectedProfile?.spec.toolheads ?? []).map((toolhead) => (
                  <option key={toolhead.id} value={toolhead.id}>
                    {toolhead.name} · {toolhead.nozzle_diameter_mm} mm
                  </option>
                ))}
              </select>
            </label>
            <label>
              Plate
              <select value={plateId} onChange={(event) => setPlateId(event.target.value)}>
                <option value="">Profile default</option>
                {(selectedProfile?.spec.plates ?? []).map((plate) => (
                  <option key={plate.id} value={plate.id}>{plate.name}</option>
                ))}
              </select>
            </label>
            <label>
              Layer height
              <input
                max="1"
                min="0.05"
                onChange={(event) => setLayerHeight(Number(event.target.value))}
                step="0.05"
                type="number"
                value={layerHeight}
              />
            </label>
            <label>
              Infill %
              <input
                max="100"
                min="0"
                onChange={(event) => setInfill(Number(event.target.value))}
                type="number"
                value={infill}
              />
            </label>
            <label>
              Maximum color distance
              <input
                max="100"
                min="0"
                onChange={(event) => setMaximumColorDistance(Number(event.target.value))}
                step="0.5"
                type="number"
                value={maximumColorDistance}
              />
            </label>
            <label>
              Material safety margin %
              <input
                max="100"
                min="0"
                onChange={(event) => setSafetyMargin(Number(event.target.value))}
                step="1"
                type="number"
                value={safetyMargin}
              />
            </label>
            <label className="checkbox-label">
              <input
                checked={supports}
                onChange={(event) => setSupports(event.target.checked)}
                type="checkbox"
              />
              Generate supports
            </label>
            <label className="checkbox-label">
              <input
                checked={allowManualSwaps}
                onChange={(event) => setAllowManualSwaps(event.target.checked)}
                type="checkbox"
              />
              Allow manual spool swaps
            </label>
            <span className="section-label">Forbidden slots for this print</span>
            <div className="override-slots">
              {(selectedProfile?.spec.material_slots ?? []).map((slot) => (
                <label className="checkbox-label" key={slot.id}>
                  <input
                    checked={forbiddenSlots.includes(slot.id)}
                    onChange={() => toggleForbidden(slot.id)}
                    type="checkbox"
                  />
                  {slot.name}
                </label>
              ))}
            </div>
          </div>

          <aside className="readiness-card">
            <span className="section-label">Local slicing</span>
            <strong className={selectedReadiness?.slicer.ready ? 'ready' : 'not-ready'}>
              {selectedReadiness?.slicer.ready ? 'Bambu Studio ready' : 'Bambu Studio setup required'}
            </strong>
            <p>{selectedReadiness?.slicer.message ?? 'No slicing-capable profile is configured.'}</p>

            <span className="section-label">Cloud device snapshot</span>
            <strong
              className={
                selectedReadiness?.cloud_binding.available
                  ? 'ready'
                  : 'not-ready'
              }
            >
              {selectedReadiness?.cloud_binding.available
                ? `${selectedReadiness.cloud_binding.device_name} bound`
                : selectedReadiness?.cloud_binding.configured
                  ? `${selectedReadiness.cloud_binding.device_name} unavailable`
                : 'Bind a cloud H2D before creating the slicing workflow'}
            </strong>
            <p>{selectedReadiness?.cloud_binding.message}</p>
            <strong
              className={
                credentialStatus.data?.configured ? 'ready' : 'not-ready'
              }
            >
              {credentialStatus.data?.configured
                ? `Bambu account connected · ${credentialStatus.data.region}`
                : 'Bambu account connection required on localhost'}
            </strong>
            {!credentialStatus.data?.configured && (
              <a className="text-button" href="#/fabrication">
                Open slicing profile &amp; cloud setup
              </a>
            )}
          </aside>
        </div>

        <footer>
          <button className="secondary-action" disabled={create.isPending} onClick={onClose}>
            Cancel
          </button>
          <button
            className="primary-action"
            disabled={!canCreate || create.isPending}
            onClick={() => create.mutate()}
          >
            {create.isPending ? 'Creating slicing workflow…' : 'Continue to slicing workspace'}
            <span>→</span>
          </button>
        </footer>
        {create.error && <p className="error-copy">{create.error.message}</p>}
      </section>
    </div>
  )
}

function DashboardPanel({
  onOpen,
  onSlice,
}: {
  onOpen: (id: string) => void
  onSlice: (id: string) => void
}) {
  const queryClient = useQueryClient()
  const [filter, setFilter] = useState<DashboardFilter>('all')
  const [printSource, setPrintSource] = useState<WorkflowResponse | null>(null)
  const workflows = useQuery({
    queryKey: ['workflows'],
    queryFn: () => api<WorkflowResponse[]>('/workflows'),
    refetchInterval: (query) =>
      query.state.data?.some(
        (item) => !item.workflow.archived_at && ACTIVE_STATES.has(item.workflow.state),
      )
        ? 2500
        : 10000,
  })
  const profiles = useQuery({
    queryKey: ['printer-profiles'],
    queryFn: () => api<PrinterProfile[]>('/slicing-profiles'),
  })
  const readiness = useQuery({
    queryKey: ['fabrication-readiness'],
    queryFn: () => api<FabricationReadiness[]>('/slicing/readiness'),
  })
  const copy = useMutation({
    mutationFn: (id: string) =>
      api<WorkflowResponse>(`/workflows/${id}/copies`, { method: 'POST' }),
    onSuccess: (data) => {
      void queryClient.invalidateQueries({ queryKey: ['workflows'] })
      onOpen(data.workflow.id)
    },
  })
  const print = useMutation({
    mutationFn: (id: string) => api(`/workflows/${id}/print`, { method: 'POST' }),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ['workflows'] }),
  })
  const archive = useMutation({
    mutationFn: ({ id, restore }: { id: string; restore: boolean }) =>
      api<WorkflowResponse>(`/workflows/${id}/${restore ? 'restore' : 'archive'}`, {
        method: 'POST',
      }),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ['workflows'] }),
  })
  const items = (workflows.data ?? []).filter((item) =>
    matchesFilter(item.workflow, filter),
  )

  const archiveModel = (workflow: Workflow) => {
    if (
      window.confirm(
        'Archive this model? It will be hidden from normal views, but its artifact and history will be preserved.',
      )
    ) {
      archive.mutate({ id: workflow.id, restore: false })
    }
  }

  return (
    <main className="dashboard-layout">
      <header className="dashboard-header">
        <div>
          <span className="eyebrow">Local model library</span>
          <h1>Models &amp; print status</h1>
          <p>Inspect, version, approve, copy, and print every durable model workflow.</p>
        </div>
        <a className="primary-action create-link" href="#/">
          Create model <span>＋</span>
        </a>
      </header>

      <div className="filter-row" role="tablist" aria-label="Model status filters">
        {FILTERS.map((item) => (
          <button
            className={filter === item.value ? 'active' : ''}
            key={item.value}
            onClick={() => setFilter(item.value)}
          >
            {item.label}
            <span>
              {(workflows.data ?? []).filter((entry) =>
                matchesFilter(entry.workflow, item.value),
              ).length}
            </span>
          </button>
        ))}
      </div>

      {workflows.isLoading && <div className="empty-dashboard">Loading models…</div>}
      {workflows.error && <div className="failure-panel">{workflows.error.message}</div>}
      {!workflows.isLoading && items.length === 0 && (
        <div className="empty-dashboard">No models match this lifecycle filter.</div>
      )}
      <section className="model-grid">
        {items.map((entry) => {
          const { workflow, artifact, job, revision_failure, printer_snapshot } = entry
          const modelUrl = artifact
            ? artifactPreviewUrl(workflow.id, artifact)
            : null
          return (
            <article
              className={`model-card ${workflow.archived_at ? 'model-card-archived' : ''}`}
              key={workflow.id}
            >
              {modelUrl ? (
                <ModelThumbnail url={modelUrl} />
              ) : (
                <div className={`model-placeholder state-${workflow.state}`}>
                  <span>{ACTIVE_STATES.has(workflow.state) ? '◇' : '!'}</span>
                  {formatState(workflow.state)}
                </div>
              )}
              <div className="model-card-body">
                <div className="card-state-row">
                  <div className="card-badges">
                    <span className={`state-pill state-${workflow.state}`}>
                      <span />
                      {formatState(workflow.state)}
                    </span>
                    {workflow.archived_at && <span className="archive-pill">Archived</span>}
                    {revision_failure && (
                      <span className="revision-failed-pill">
                        Edit failed · v{revision_failure.base_artifact_version} restored
                      </span>
                    )}
                  </div>
                  <time>{new Date(workflow.updated_at).toLocaleString()}</time>
                </div>
                <h2>{workflow.requirement}</h2>
                <div className="card-metadata">
                  <span>#{workflow.id.slice(0, 8)}</span>
                  <span>{workflow.printer_name}</span>
                  {artifact && <span>artifact v{artifact.version}</span>}
                  {artifact && (
                    <span>
                      {formatNumber(artifact.mesh.dimensions.width_mm, 0)} ×{' '}
                      {formatNumber(artifact.mesh.dimensions.depth_mm, 0)} ×{' '}
                      {formatNumber(artifact.mesh.dimensions.height_mm, 0)} mm
                    </span>
                  )}
                </div>
                <p className="card-provenance">
                  {artifact
                    ? artifact.provenance.kind === 'generated'
                      ? 'Generated model'
                      : `Catalog · ${artifact.provenance.creator ?? 'unknown creator'}`
                    : workflow.failure_message ?? 'Preparing durable artifact'}
                  {job && ` · Print ${formatState(job.status)}`}
                </p>
                <div className="card-actions">
                  <button
                    className="secondary-action"
                    onClick={() =>
                      [
                        'slice_setup',
                        'awaiting_material_review',
                        'slice_requested',
                        'slicing',
                        'slice_validating',
                        'awaiting_slice_review',
                        'slice_failed',
                      ].includes(workflow.state)
                        ? onSlice(workflow.id)
                        : onOpen(workflow.id)
                    }
                  >
                    {workflow.state === 'awaiting_approval'
                      ? 'Inspect & approve'
                      : ['slice_requested', 'slicing', 'slice_validating'].includes(workflow.state)
                        ? 'View slicing'
                        : workflow.state === 'awaiting_slice_review'
                          ? 'Review slice'
                      : ['submitting', 'queued', 'printing', 'completed', 'print_failed'].includes(
                            workflow.state,
                          )
                        ? 'View print status'
                        : 'Open'}
                  </button>
                  {workflow.archived_at ? (
                    <button
                      className="primary-action"
                      disabled={archive.isPending}
                      onClick={() => archive.mutate({ id: workflow.id, restore: true })}
                    >
                      Restore model <span>↺</span>
                    </button>
                  ) : (
                    <>
                      {artifact && (
                        <button
                          className="text-button"
                          disabled={copy.isPending}
                          onClick={() => copy.mutate(workflow.id)}
                        >
                          Make a copy
                        </button>
                      )}
                      {artifact && (
                        <button
                          className="primary-action"
                          onClick={() => setPrintSource(entry)}
                        >
                          Prepare H2D slice <span>→</span>
                        </button>
                      )}
                      {['awaiting_approval', 'approved'].includes(workflow.state) &&
                        artifact?.source_available && (
                          <button className="text-button" onClick={() => onOpen(workflow.id)}>
                            Edit existing
                          </button>
                        )}
                      {ARCHIVABLE_STATES.has(workflow.state) && (
                        <button
                          className="text-button archive-action"
                          disabled={archive.isPending}
                          onClick={() => archiveModel(workflow)}
                        >
                          Archive model
                        </button>
                      )}
                      {workflow.state === 'approved' &&
                        printer_snapshot?.profile.slicer.driver_id ===
                        'simulator_passthrough' && (
                        <button
                          className="primary-action"
                          disabled={print.isPending}
                          onClick={() => print.mutate(workflow.id)}
                        >
                          Send to printer <span>→</span>
                        </button>
                      )}
                      {workflow.state === 'approved' &&
                      printer_snapshot?.profile.slicer.driver_id !==
                        'simulator_passthrough' && (
                        <button
                          className="primary-action"
                          onClick={() => onSlice(workflow.id)}
                        >
                          Continue material setup <span>→</span>
                        </button>
                      )}
                    </>
                  )}
                </div>
              </div>
            </article>
          )
        })}
      </section>
      {(copy.error || print.error || archive.error) && (
        <p className="error-copy">
          {copy.error?.message ?? print.error?.message ?? archive.error?.message}
        </p>
      )}
      {printSource && (
        <PrepareH2DSliceDialog
          onClose={() => setPrintSource(null)}
          onCreated={onSlice}
          profiles={profiles.data ?? []}
          readiness={readiness.data ?? []}
          source={printSource}
        />
      )}
    </main>
  )
}

function FabricationStepper({
  workflow,
  data,
}: {
  workflow: Workflow
  data: WorkflowResponse
}) {
  const approvalComplete = workflow.state !== 'awaiting_approval'
  const cloudComplete = data.cloud_snapshot?.completeness === 'complete'
  const materialComplete = Boolean(data.material_assignment?.confirmed_at)
  const sliceComplete = Boolean(data.sliced_artifact)
  const statuses: Array<'complete' | 'active' | 'pending'> = [
    approvalComplete ? 'complete' : 'active',
    cloudComplete
      ? 'complete'
      : workflow.state === 'approved'
        ? 'active'
        : 'pending',
    materialComplete
      ? 'complete'
      : cloudComplete
        ? 'active'
        : 'pending',
    sliceComplete
      ? 'complete'
      : materialComplete ||
          ['slice_requested', 'slicing', 'slice_validating', 'slice_failed'].includes(
            workflow.state,
          )
        ? 'active'
        : 'pending',
    workflow.state === 'awaiting_slice_review' ? 'active' : 'pending',
  ]
  const steps = [
    ['1', 'Model approval'],
    ['2', 'Cloud printer & AMS'],
    ['3', 'Material assignment'],
    ['4', 'Slice in Bambu Studio'],
    ['5', 'Review & download'],
  ]

  return (
    <ol className="fabrication-stepper">
      {steps.map(([number, label], index) => (
        <li className={statuses[index]} key={number}>
          <span>{statuses[index] === 'complete' ? '✓' : number}</span>
          <strong>{label}</strong>
        </li>
      ))}
    </ol>
  )
}

function SlicingWorkspace({
  workflowId,
  onBack,
}: {
  workflowId: string
  onBack: () => void
}) {
  const queryClient = useQueryClient()
  const [materialOverrides, setMaterialOverrides] = useState<Record<string, string>>({})
  const [submissionNotice, setSubmissionNotice] = useState(false)
  const preparationAttempted = useRef(false)
  const workflowQuery = useQuery({
    queryKey: ['workflow', workflowId],
    queryFn: () => api<WorkflowResponse>(`/workflows/${workflowId}`),
    refetchInterval: (query) =>
      query.state.data &&
      ['slice_requested', 'slicing', 'slice_validating'].includes(
        query.state.data.workflow.state,
      )
        ? 1500
        : 10000,
  })
  const data = workflowQuery.data
  const workflow = data?.workflow
  const artifact = data?.artifact
  const prepareMaterials = useMutation({
    mutationFn: () =>
      api<WorkflowResponse>(`/workflows/${workflowId}/prepare-slicing-materials`, {
        method: 'POST',
      }),
    onSuccess: (prepared) => {
      setMaterialOverrides({})
      queryClient.setQueryData(['workflow', workflowId], prepared)
      void queryClient.invalidateQueries({ queryKey: ['workflows'] })
    },
  })
  const approve = useMutation({
    mutationFn: () =>
      api(`/workflows/${workflowId}/approval`, {
        method: 'POST',
        body: JSON.stringify({
          artifact_version: artifact!.version,
          manifest_digest: artifact!.manifest_digest,
          approved_by: 'local-web',
        }),
      }),
    onSuccess: () =>
      void queryClient.invalidateQueries({ queryKey: ['workflow', workflowId] }),
  })
  const confirmMapping = useMutation({
    mutationFn: (mapping: FilamentMappingStatus) =>
      api(
        `/workflows/${workflowId}/material-mappings/${encodeURIComponent(
          mapping.cloud_filament_id,
        )}/confirm`,
        {
          method: 'POST',
          body: JSON.stringify({
            proposed_profile_id: mapping.proposed_profile_id,
            proposed_profile_digest: mapping.proposed_profile_digest,
          }),
        },
      ),
    onSuccess: () => {
      setMaterialOverrides({})
      void queryClient.invalidateQueries({ queryKey: ['workflow', workflowId] })
    },
  })
  const confirmAndSlice = useMutation({
    mutationFn: () =>
      api(`/workflows/${workflowId}/material-assignment/confirm-and-slice`, {
        method: 'POST',
        body: JSON.stringify({
          assignment_id: data!.material_assignment!.id,
          confirmed_by: 'local-web',
          spool_overrides: materialOverrides,
        }),
      }),
    onSuccess: () =>
      void queryClient.invalidateQueries({ queryKey: ['workflow', workflowId] }),
  })
  const retrySlice = useMutation({
    mutationFn: () => api(`/workflows/${workflowId}/slice`, { method: 'POST' }),
    onSuccess: () =>
      void queryClient.invalidateQueries({ queryKey: ['workflow', workflowId] }),
  })
  const cancel = useMutation({
    mutationFn: () => api(`/workflows/${workflowId}/cancel`, { method: 'POST' }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['workflow', workflowId] })
      void queryClient.invalidateQueries({ queryKey: ['workflows'] })
    },
  })
  const refreshCloud = useMutation({
    mutationFn: () =>
      api(`/workflows/${workflowId}/cloud-snapshot`, {
        method: 'POST',
        body: JSON.stringify({ device_id: null }),
      }),
    onSuccess: () => {
      setMaterialOverrides({})
      confirmMapping.reset()
      confirmAndSlice.reset()
      retrySlice.reset()
      preparationAttempted.current = false
      void queryClient.invalidateQueries({ queryKey: ['workflow', workflowId] })
    },
  })
  const materialOperationPending =
    confirmMapping.isPending ||
    confirmAndSlice.isPending ||
    retrySlice.isPending ||
    prepareMaterials.isPending

  useEffect(() => {
    if (
      !preparationAttempted.current &&
      !prepareMaterials.isPending &&
      workflow &&
      !data?.material_assignment &&
      (!data?.slice_job?.material_assessment ||
        data.slice_job.material_assessment.status === 'sufficient') &&
      ['approved', 'slice_setup'].includes(workflow.state)
    ) {
      preparationAttempted.current = true
      prepareMaterials.mutate()
    }
  }, [
    data?.material_assignment,
    data?.slice_job?.material_assessment,
    prepareMaterials,
    workflow,
  ])

  if (workflowQuery.isLoading) {
    return <div className="center-message">Loading slicing workspace…</div>
  }
  if (workflowQuery.error || !data || !workflow || !artifact) {
    return (
      <div className="center-message error-copy">
        {workflowQuery.error?.message ?? 'Slicing workflow is unavailable'}
      </div>
    )
  }

  const cloudTrays = [
    ...(data.cloud_snapshot?.ams_units.flatMap((unit) => unit.trays) ?? []),
    ...(data.cloud_snapshot?.external_trays ?? []),
  ]
  const materialMappings = new Map(
    data.material_mappings.map((item) => [item.cloud_filament_id, item]),
  )
  const materialRecovery =
    data.slice_job?.material_assessment?.status === 'sufficient'
      ? null
      : data.slice_job?.material_assessment
  const recoveryCanConfirm =
    !materialRecovery || materialRecovery.status === 'replacement_proposed'

  return (
    <main className="slicing-workspace">
      <header className="slicing-header">
        <div className="slicing-header-copy">
          <button className="text-button slicing-header-back" onClick={onBack}>
            ← Models
          </button>
          <div className="slicing-header-context">
            <span className="eyebrow">Slice workflow {workflow.id.slice(0, 8)}</span>
            <h1>Prepare H2D printer-ready artifact</h1>
            <p>{workflow.requirement}</p>
          </div>
        </div>
        <span className={`state-pill state-${workflow.state}`}>
          <span />
          {formatState(workflow.state)}
        </span>
      </header>

      <FabricationStepper data={data} workflow={workflow} />

      <section className="slicing-layout">
        <div className="slicing-preview">
          {artifact.project && artifact.project.parts.length > 1 ? (
            <MultipartPartsViewer
              parts={artifact.project.parts.map((part) => {
                const download = artifact.downloads.find(
                  (item) => item.role === 'part_stl' && item.part_id === part.id,
                )
                const material = artifact.project!.materials.find(
                  (item) => item.id === part.material_id,
                )
                return {
                  id: part.id,
                  name: part.name,
                  color: material?.color ?? '#79e2ca',
                  url: download
                    ? `${API}/workflows/${workflowId}/artifacts/${artifact.version}/${download.path}`
                    : '',
                }
              })}
              instances={artifact.project.instances.map((instance) => ({
                id: instance.id,
                partId: instance.part_id,
                transform: instance.transform,
              }))}
              scopedPartIds={[]}
              onTogglePart={() => undefined}
            />
          ) : (
            <ModelViewer
              url={artifactPreviewUrl(workflowId, artifact)}
              dimensions={artifact.mesh.dimensions}
            />
          )}
          <div className="slice-artifact-identity">
            <strong>Artifact v{artifact.version}</strong>
            <code>{artifact.manifest_digest}</code>
          </div>
        </div>

        <div className="slicing-evidence">
          <section className="slice-step-card">
            <span className="section-label">1 · Model approval</span>
            <strong>
              {workflow.state === 'awaiting_approval'
                ? 'Approval required'
                : 'Exact artifact approved'}
            </strong>
            <p>
              Approval is bound to this artifact version and immutable manifest digest.
            </p>
            {workflow.state === 'awaiting_approval' && (
              <button
                className="primary-action"
                disabled={approve.isPending}
                onClick={() => approve.mutate()}
              >
                Approve exact artifact
              </button>
            )}
          </section>

          <section className="slice-step-card">
            <span className="section-label">Configuration · Slicing profile</span>
            <strong>{data.printer_snapshot?.profile.display_name}</strong>
            <p>
              Revision {data.printer_snapshot?.profile_revision} ·{' '}
              {data.printer_snapshot?.profile.slicer.machine_profile_id} ·{' '}
              {data.printer_snapshot?.profile.slicer.process_profile_id}
            </p>
          </section>

          <section className="slice-step-card">
            <span className="section-label">2 · Cloud device snapshot</span>
            {data.cloud_snapshot ? (
              <>
                <strong>
                  {data.cloud_snapshot.device.name} ·{' '}
                  {data.cloud_snapshot.device.online ? 'online' : 'offline'}
                </strong>
                <p>
                  {data.cloud_snapshot.device.device_id} · {data.cloud_snapshot.region} ·
                  observed {new Date(data.cloud_snapshot.observed_at).toLocaleString()}
                </p>
                <div className="observed-tray-grid">
                  {cloudTrays.map((tray) => {
                    const mapping = tray.material_profile_id
                      ? materialMappings.get(tray.material_profile_id)
                      : undefined
                    return (
                      <div key={tray.slot_id}>
                        <strong>{tray.slot_id}</strong>
                        <span className="observed-tray-material">
                          {tray.color && (
                            <span
                              aria-label={`Filament color ${tray.color}`}
                              className="observed-tray-color"
                              style={{ background: tray.color }}
                            />
                          )}
                          {tray.material ?? 'Empty'}
                          {tray.color ? ` · ${tray.color}` : ''}
                        </span>
                        {tray.material_profile_id && (
                          <small>
                            {tray.material_profile_id} →{' '}
                            {mapping?.selected_profile_id ??
                              mapping?.proposed_profile_id ??
                              'Unmapped'}
                          </small>
                        )}
                        {tray.material && tray.estimated_remaining_g !== null ? (
                          <small>
                            {tray.remain_percentage}% · ~{tray.estimated_remaining_g} g
                          </small>
                        ) : tray.material ? (
                          <small>Quantity unavailable · excluded from assignment</small>
                        ) : null}
                      </div>
                    )
                  })}
                </div>
                <div className="filament-mapping-grid">
                  {data.material_mappings.map((mapping) => (
                    <div key={mapping.cloud_filament_id}>
                      <strong>
                        {mapping.cloud_filament_id} ·{' '}
                        {mapping.state.replaceAll('_', ' ')}
                      </strong>
                      <span>
                        {mapping.selected_profile_id ??
                          mapping.proposed_profile_id ??
                          'No installed preset'}
                      </span>
                      <small>{mapping.slots.join(', ')}</small>
                      <small>{mapping.reason}</small>
                      {['confirmation_required', 'upgrade_available'].includes(
                        mapping.state,
                      ) &&
                        [
                          'approved',
                          'slice_setup',
                          'awaiting_material_review',
                          'slice_failed',
                          'awaiting_slice_review',
                        ].includes(workflow.state) &&
                        mapping.proposed_profile_id &&
                        mapping.proposed_profile_digest && (
                          <button
                            className="secondary-action"
                            disabled={confirmMapping.isPending || refreshCloud.isPending}
                            onClick={() => confirmMapping.mutate(mapping)}
                          >
                            {mapping.state === 'upgrade_available'
                              ? 'Confirm exact-profile upgrade'
                              : 'Confirm reusable generic mapping'}
                          </button>
                        )}
                    </div>
                  ))}
                </div>
                {data.cloud_snapshot.warnings.map((warning) => (
                  <p className="part-warning" key={warning}>{warning}</p>
                ))}
                {confirmMapping.error && (
                  <p className="error-copy">{confirmMapping.error.message}</p>
                )}
              </>
            ) : (
              <p>No immutable cloud observation has been captured.</p>
            )}
            {[
              'approved',
              'slice_setup',
              'awaiting_material_review',
              'slice_failed',
              'awaiting_slice_review',
            ].includes(workflow.state) && (
              <button
                className="primary-action"
                disabled={refreshCloud.isPending || materialOperationPending}
                onClick={() => refreshCloud.mutate()}
              >
                {refreshCloud.isPending ? 'Refreshing cloud state…' : 'Refresh H2D & AMS'}
              </button>
            )}
          </section>

          <section className="slice-step-card">
            <span className="section-label">3 · Material assignment</span>
            {materialRecovery && (
              <div className="material-recovery-panel">
                <strong>
                  Actual sliced usage requires material reassignment · round{' '}
                  {materialRecovery.recovery_round} of 3
                </strong>
                {materialRecovery.requirements
                  .filter((item) => item.shortfall_g > 0)
                  .map((item) => (
                    <div key={item.spool_id}>
                      <span>{item.slot_id ?? 'Unknown slot'}</span>
                      <small>
                        actual {formatNumber(item.actual_usage_g, 2)} g · required{' '}
                        {formatNumber(item.required_weight_g, 2)} g with margin · available{' '}
                        {formatNumber(item.available_weight_g, 2)} g · short{' '}
                        {formatNumber(item.shortfall_g, 2)} g
                      </small>
                    </div>
                  ))}
                <p>
                  {materialRecovery.status === 'replacement_proposed'
                    ? 'A sufficient compatible replacement is preselected below. Review and confirm it before re-slicing.'
                    : materialRecovery.status === 'load_required'
                      ? 'No loaded compatible spool is sufficient. Load or replace a spool, then refresh H2D & AMS.'
                      : 'Three recovery rounds were exhausted. Manual material intervention is required.'}
                </p>
              </div>
            )}
            {!data.material_assignment ? (
              <>
                {data.material_assignment_stale && (
                  <p className="part-warning">
                    The previous assignment uses an outdated filament mapping. Refresh H2D &amp;
                    AMS, then recommend materials again.
                  </p>
                )}
                {prepareMaterials.isPending ? (
                  <div className="preparation-progress">
                    <strong>Preparing slicing materials…</strong>
                    <span>Reading H2D &amp; AMS</span>
                    <span>Resolving filament profiles</span>
                    <span>Recommending compatible slots</span>
                  </div>
                ) : prepareMaterials.error ? (
                  <>
                    <p className="error-copy">{prepareMaterials.error.message}</p>
                    <button
                      className="primary-action"
                      disabled={refreshCloud.isPending}
                      onClick={() => prepareMaterials.mutate()}
                    >
                      Retry preparation
                    </button>
                  </>
                ) : (
                  <small>Preparation starts automatically when this workspace opens.</small>
                )}
              </>
            ) : (
              <>
                <div className="slice-material-table">
                  {data.material_assignment.assignments.map((assignment) => {
                    const request = data.material_assignment!.requests.find(
                      (item) => item.part_id === assignment.part_id,
                    )
                    return (
                      <div key={assignment.part_id}>
                        <span
                          className="part-color"
                          style={{
                            background: request?.requested_color ?? '#b7c4d4',
                          }}
                        />
                        <strong>{request?.part_name ?? assignment.part_id}</strong>
                        <span>
                          {assignment.slot_id} · {assignment.material_id} ·{' '}
                          {assignment.toolhead_id}
                        </span>
                        <select
                          value={
                            materialOverrides[assignment.part_id] ??
                            assignment.spool_id
                          }
                          onChange={(event) =>
                            setMaterialOverrides((current) => ({
                              ...current,
                              [assignment.part_id]: event.target.value,
                            }))
                          }
                        >
                          {(
                            data.material_assignment!.candidate_options[
                              assignment.part_id
                            ] ?? []
                          ).map((candidate) => (
                            <option
                              key={candidate.spool_id}
                              value={candidate.spool_id}
                            >
                              {candidate.slot_id} · {candidate.material_id} · ~
                              {formatNumber(candidate.remaining_weight_g, 0)} g · ΔE{' '}
                              {candidate.color_distance.toFixed(2)}
                            </option>
                          ))}
                        </select>
                        <small>
                          {assignment.rationale} · estimated part usage{' '}
                          {formatNumber(request?.estimated_weight_g ?? 0, 1)} g +{' '}
                          {data.printer_snapshot?.overrides
                            .material_safety_margin_percent ?? 15}% margin
                        </small>
                      </div>
                    )
                  })}
                </div>
                {data.material_assignment.confirmed_at ? (
                  <span className="verified-label">
                    Materials and approximate quantity confirmed
                  </span>
                ) : (
                  <button
                    className="primary-action"
                    disabled={
                      confirmAndSlice.isPending ||
                      refreshCloud.isPending ||
                      !recoveryCanConfirm
                    }
                    onClick={() => confirmAndSlice.mutate()}
                  >
                    {confirmAndSlice.isPending
                      ? 'Confirming & starting slice…'
                      : materialRecovery
                        ? 'Confirm replacement & re-slice'
                        : 'Confirm materials & start slicing'}
                  </button>
                )}
              </>
            )}
          </section>

          <section className="slice-step-card">
            <span className="section-label">4 · Slice in Bambu Studio</span>
            <dl>
              <dt>Plate</dt>
              <dd>{data.printer_snapshot?.overrides.plate_id ?? 'Profile default'}</dd>
              <dt>Layer</dt>
              <dd>{data.printer_snapshot?.overrides.layer_height_mm ?? 'Profile default'} mm</dd>
              <dt>Infill</dt>
              <dd>{data.printer_snapshot?.overrides.infill_percent ?? 'Profile default'}%</dd>
              <dt>Supports</dt>
              <dd>{data.printer_snapshot?.overrides.supports ? 'Enabled' : 'Disabled'}</dd>
            </dl>
            <details>
              <summary>Advanced pinned settings</summary>
              <pre>{JSON.stringify(data.printer_snapshot?.overrides, null, 2)}</pre>
            </details>
            {data.material_assignment?.confirmed_at &&
              ['slice_failed', 'awaiting_material_review'].includes(workflow.state) &&
              !materialRecovery && (
                <button
                  className="primary-action"
                  disabled={retrySlice.isPending || refreshCloud.isPending}
                  onClick={() => retrySlice.mutate()}
                >
                  {retrySlice.isPending ? 'Requesting retry…' : 'Retry slicing'}
                </button>
              )}
            {data.slice_job && (
              <p>
                {formatState(data.slice_job.status)}
                {data.slice_job.message ? ` · ${data.slice_job.message}` : ''}
              </p>
            )}
          </section>

          {data.sliced_artifact && data.slice_job?.status === 'ready' && (
            <section className="slice-step-card slice-final-review">
              <span className="section-label">5 · Review &amp; download</span>
              <div className="slice-review-preview">
                {data.sliced_artifact.thumbnail_available && (
                  <img
                    alt="Bambu Studio plate preview"
                    src={`${API}/workflows/${workflowId}/slices/${data.slice_job.id}/thumbnail`}
                  />
                )}
                <dl>
                  <dt>Slicer</dt>
                  <dd>{data.sliced_artifact.slicer_version}</dd>
                  <dt>Machine</dt>
                  <dd>{data.sliced_artifact.machine_profile_id}</dd>
                  <dt>Process</dt>
                  <dd>{data.sliced_artifact.process_profile_id}</dd>
                  <dt>Size</dt>
                  <dd>{formatNumber(data.sliced_artifact.size_bytes / 1024, 0)} KB</dd>
                  <dt>SHA-256</dt>
                  <dd><code>{data.sliced_artifact.digest}</code></dd>
                </dl>
              </div>
              <div className="slice-download-actions">
                <a
                  className="primary-action"
                  download
                  href={`${API}/workflows/${workflowId}/slices/${data.slice_job.id}/download`}
                >
                  Download printer-ready .gcode.3mf
                </a>
                <a
                  className="secondary-action"
                  download
                  href={`${API}/workflows/${workflowId}/slices/${data.slice_job.id}/manifest`}
                >
                  Download slice manifest
                </a>
                <button
                  className="secondary-action"
                  onClick={() => setSubmissionNotice(true)}
                >
                  Submit to printer
                </button>
              </div>
              {submissionNotice && (
                <div className="part-warning">
                  Printer submission is a future feature. No file was uploaded and no printer
                  command was sent. Download the validated .gcode.3mf for manual handoff.
                </div>
              )}
              <div className="boundary-note">
                Printer-ready artifact created. No file was uploaded and no job was submitted
                to a printer.
              </div>
            </section>
          )}

          {(approve.error ||
            refreshCloud.error ||
            confirmAndSlice.error ||
            retrySlice.error ||
            cancel.error) && (
            <p className="error-copy">
              {approve.error?.message ??
                refreshCloud.error?.message ??
                confirmAndSlice.error?.message ??
                retrySlice.error?.message ??
                cancel.error?.message}
            </p>
          )}
        </div>
      </section>

      <footer className="slicing-footer">
        <button className="secondary-action" onClick={onBack}>Back to models</button>
        {!['cancelled', 'completed'].includes(workflow.state) && (
          <button
            className="danger-action"
            disabled={cancel.isPending}
            onClick={() => cancel.mutate()}
          >
            {cancel.isPending ? 'Cancelling…' : 'Cancel slicing workflow'}
          </button>
        )}
      </footer>
    </main>
  )
}

function WorkflowPanel({
  workflowId,
  onReset,
}: {
  workflowId: string
  onReset: () => void
}) {
  const queryClient = useQueryClient()
  const [feedback, setFeedback] = useState('')
  const [sourceOpen, setSourceOpen] = useState(false)
  const [scopedPartIds, setScopedPartIds] = useState<string[]>([])
  const workflowQuery = useQuery({
    queryKey: ['workflow', workflowId],
    queryFn: () => api<WorkflowResponse>(`/workflows/${workflowId}`),
    refetchInterval: (query) =>
      query.state.data &&
      (query.state.data.workflow.archived_at ||
        TERMINAL_STATES.has(query.state.data.workflow.state))
        ? 10000
        : 2500,
  })
  const events = useWorkflowEvents(workflowId)
  const data = workflowQuery.data
  const artifact = data?.artifact
  const workflow = data?.workflow
  const revisionFailure = data?.revision_failure
  const revisionVerification = data?.revision_verification
  const requiresSlicing =
    Boolean(data?.printer_snapshot) &&
    data?.printer_snapshot?.profile.slicer.driver_id !== 'simulator_passthrough'
  useEffect(() => {
    const partIds = new Set(artifact?.project?.parts.map((part) => part.id) ?? [])
    setScopedPartIds((current) => current.filter((partId) => partIds.has(partId)))
  }, [artifact?.version, artifact?.project])
  const toggleScopedPart = useCallback((partId: string) => {
    setScopedPartIds((current) =>
      current.includes(partId)
        ? current.filter((candidate) => candidate !== partId)
        : [...current, partId],
    )
  }, [])
  const canInspect = Boolean(workflow && artifact && INSPECTABLE_STATE.has(workflow.state))
  const isMultipart = Boolean(artifact?.project && artifact.project.parts.length > 1)
  const modelUrl = artifact
    ? artifactPreviewUrl(workflowId, artifact)
    : ''
  const sourceQuery = useQuery({
    queryKey: ['source', workflowId, artifact?.version],
    queryFn: async () => {
      const response = await fetch(
        `${API}/workflows/${workflowId}/artifacts/${artifact!.version}/source.scad`,
      )
      return response.ok ? response.text() : null
    },
    enabled: sourceOpen && Boolean(artifact?.source_available),
  })
  const approve = useMutation({
    mutationFn: () =>
      api(`/workflows/${workflowId}/approval`, {
        method: 'POST',
        body: JSON.stringify({
          artifact_version: artifact!.version,
          manifest_digest: artifact!.manifest_digest,
          approved_by: 'local-web',
        }),
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['workflow', workflowId] })
      void queryClient.invalidateQueries({ queryKey: ['workflows'] })
    },
  })
  const revise = useMutation({
    mutationFn: (mode: 'refine_current' | 'search_new_base') =>
      api(`/workflows/${workflowId}/revisions`, {
        method: 'POST',
        body: JSON.stringify({
          mode,
          feedback,
          allowed_part_ids:
            mode === 'refine_current' && isMultipart && scopedPartIds.length > 0
              ? scopedPartIds
              : null,
        }),
      }),
    onSuccess: () => {
      setFeedback('')
      setScopedPartIds([])
      void queryClient.invalidateQueries({ queryKey: ['workflow', workflowId] })
      void queryClient.invalidateQueries({ queryKey: ['workflows'] })
    },
  })
  const print = useMutation({
    mutationFn: () => api(`/workflows/${workflowId}/print`, { method: 'POST' }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['workflow', workflowId] })
      void queryClient.invalidateQueries({ queryKey: ['workflows'] })
    },
  })
  const copy = useMutation({
    mutationFn: () =>
      api<WorkflowResponse>(`/workflows/${workflowId}/copies`, { method: 'POST' }),
    onSuccess: (copied) => {
      void queryClient.invalidateQueries({ queryKey: ['workflows'] })
      window.location.hash = `/workflows/${copied.workflow.id}`
    },
  })
  const cancel = useMutation({
    mutationFn: () => api(`/workflows/${workflowId}/cancel`, { method: 'POST' }),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ['workflow', workflowId] }),
  })
  const archive = useMutation({
    mutationFn: (restore: boolean) =>
      api<WorkflowResponse>(`/workflows/${workflowId}/${restore ? 'restore' : 'archive'}`, {
        method: 'POST',
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['workflow', workflowId] })
      void queryClient.invalidateQueries({ queryKey: ['workflows'] })
    },
  })

  const archiveModel = () => {
    if (
      window.confirm(
        'Archive this model? It will be hidden from normal views, but its artifact and history will be preserved.',
      )
    ) {
      archive.mutate(false)
    }
  }

  if (workflowQuery.isLoading) return <div className="center-message">Loading workflow…</div>
  if (workflowQuery.error || !workflow)
    return (
      <div className="center-message error-copy">
        {workflowQuery.error?.message ?? 'Workflow not found'}
      </div>
    )

  return (
    <main className="workflow-layout">
      <header className="workflow-header">
        <div>
          <button className="text-button" onClick={onReset}>
            ← Model dashboard
          </button>
          <div className="eyebrow">Workflow {workflow.id.slice(0, 8)}</div>
          <h1>{canInspect ? 'Inspect final model' : 'Preparing your model'}</h1>
        </div>
        <div className="workflow-header-actions">
          <div className={`state-pill state-${workflow.state}`}>
            <span />
            {formatState(workflow.state)}
          </div>
          {workflow.archived_at ? (
            <>
              <span className="archive-pill">Archived</span>
              <button
                className="primary-action"
                disabled={archive.isPending}
                onClick={() => archive.mutate(true)}
              >
                Restore model <span>↺</span>
              </button>
            </>
          ) : (
            ARCHIVABLE_STATES.has(workflow.state) && (
              <button
                className="danger-action"
                disabled={archive.isPending}
                onClick={archiveModel}
              >
                Archive model
              </button>
            )
          )}
        </div>
      </header>

      {workflow.archived_at && (
        <section className="archive-notice">
          <strong>This model is archived.</strong>
          <p>
            Its artifact and full workflow history are preserved. Restore it before making any
            changes.
          </p>
        </section>
      )}

      {revisionFailure && (
        <section className="revision-failure-notice">
          <div>
            <strong>
              Latest edit was not applied · model v{revisionFailure.base_artifact_version}{' '}
              restored
            </strong>
            <p>{revisionFailure.feedback}</p>
          </div>
          <ul>
            {revisionFailure.checks
              .filter((check) => !check.passed)
              .map((check) => (
                <li key={check.id}>{check.message}</li>
              ))}
          </ul>
        </section>
      )}

      {!canInspect && (
        <section className="progress-grid">
          <div className="progress-card">
            <div className="agent-mark">D</div>
            <div>
              <strong>Discovery agent</strong>
              <p>Searches model pages, introductions, file lists, and creator galleries.</p>
            </div>
          </div>
          <div className="handoff-line">validated handoff →</div>
          <div className="progress-card">
            <div className="agent-mark modeling">M</div>
            <div>
              <strong>Modeling agent</strong>
              <p>Creates or modifies OpenSCAD, then repairs validation failures.</p>
            </div>
          </div>
          <section className="event-log">
            <h2>Live activity</h2>
            {events.length === 0 && <p className="muted">Waiting for the worker…</p>}
            {[...events].reverse().slice(0, 8).map((event) => (
              <div className="event-row" key={event.id}>
                <span className="event-dot" />
                <div>
                  <strong>{formatState(event.kind.replaceAll('.', ' '))}</strong>
                  <time>{new Date(event.created_at).toLocaleTimeString()}</time>
                </div>
              </div>
            ))}
          </section>
        </section>
      )}

      {canInspect && artifact && (
        <>
          <section className="inspection-grid">
            {isMultipart && artifact.project ? (
              <MultipartPartsViewer
                parts={artifact.project.parts.map((part) => {
                  const download = artifact.downloads.find(
                    (item) => item.role === 'part_stl' && item.part_id === part.id,
                  )
                  const material = artifact.project!.materials.find(
                    (item) => item.id === part.material_id,
                  )
                  return {
                    id: part.id,
                    name: part.name,
                    color: material?.color ?? '#79e2ca',
                    url: download
                      ? `${API}/workflows/${workflowId}/artifacts/${artifact.version}/${download.path}`
                      : '',
                  }
                })}
                instances={artifact.project.instances.map((instance) => ({
                  id: instance.id,
                  partId: instance.part_id,
                  transform: instance.transform,
                }))}
                scopedPartIds={scopedPartIds}
                onTogglePart={toggleScopedPart}
              />
            ) : (
              <ModelViewer url={modelUrl} dimensions={artifact.mesh.dimensions} />
            )}
            <aside className="inspection-panel">
              <div className="panel-section">
                <span className="section-label">Original request</span>
                <p>{workflow.requirement}</p>
              </div>
              <div className="metric-grid">
                <div>
                  <span>Triangles</span>
                  <strong>{formatNumber(artifact.mesh.triangle_count, 0)}</strong>
                </div>
                <div>
                  <span>Volume</span>
                  <strong>{formatNumber(artifact.mesh.volume_mm3)} mm³</strong>
                </div>
                <div>
                  <span>Watertight</span>
                  <strong>{artifact.mesh.watertight ? 'Yes' : 'No'}</strong>
                </div>
                <div>
                  <span>Components</span>
                  <strong>{artifact.mesh.connected_components}</strong>
                </div>
              </div>
              <div className="panel-section provenance">
                <span className="section-label">Provenance</span>
                <strong>
                  {artifact.provenance.kind === 'generated'
                    ? 'Generated from scratch'
                    : `Adapted from Thingiverse · ${artifact.provenance.creator}`}
                </strong>
                {artifact.provenance.license && <span>{artifact.provenance.license}</span>}
                {artifact.provenance.source_url && (
                  <a href={artifact.provenance.source_url} target="_blank" rel="noreferrer">
                    View original ↗
                  </a>
                )}
              </div>
              {artifact.project && (
                <div className="panel-section">
                  <span className="section-label">Editable parts</span>
                  {isMultipart && (
                    <div className={`edit-scope ${scopedPartIds.length > 0 ? 'restricted' : ''}`}>
                      <div>
                        <strong>
                          {scopedPartIds.length > 0
                            ? `Editing limited to ${scopedPartIds.length} selected ${
                                scopedPartIds.length === 1 ? 'part' : 'parts'
                              }`
                            : 'Agent decides affected parts'}
                        </strong>
                        <small>
                          {scopedPartIds.length > 0
                            ? 'The agent cannot edit parts outside this scope.'
                            : 'Select parts only when you want to restrict the agent.'}
                        </small>
                      </div>
                      {scopedPartIds.length > 0 && (
                        <button className="text-button" onClick={() => setScopedPartIds([])}>
                          Clear scope
                        </button>
                      )}
                      {revisionVerification?.verdict === 'passed' &&
                        revisionVerification.candidate_artifact_version === artifact.version && (
                          <div className="panel-section revision-summary">
                            <span className="section-label">Verified revision</span>
                            <strong>{revisionVerification.rationale}</strong>
                          </div>
                        )}
                    </div>
                  )}
                  <div className="part-list">
                    {artifact.project.parts.map((part) => {
                      const instanceCount = artifact.project!.instances.filter(
                        (instance) => instance.part_id === part.id,
                      ).length
                      const material = artifact.project!.materials.find(
                        (item) => item.id === part.material_id,
                      )
                      return (
                        <button
                          className={`part-row ${isMultipart ? '' : 'read-only'} ${
                            scopedPartIds.includes(part.id) ? 'selected' : ''
                          }`}
                          key={part.id}
                          onClick={() => toggleScopedPart(part.id)}
                          aria-pressed={isMultipart ? scopedPartIds.includes(part.id) : undefined}
                          disabled={!isMultipart}
                        >
                          {isMultipart && (
                            <span className="part-scope-check">
                              {scopedPartIds.includes(part.id) ? '✓' : ''}
                            </span>
                          )}
                          <span
                            className="part-color"
                            style={{ background: material?.color ?? '#b7c4d4' }}
                          />
                          <span>
                            <strong>{part.name}</strong>
                            <small>
                              {part.geometry_kind.replaceAll('_', ' ')} · {instanceCount}{' '}
                              {instanceCount === 1 ? 'instance' : 'instances'} ·{' '}
                              {Math.round(part.confidence * 100)}% confidence
                            </small>
                            <small>{part.annotation_origin.replaceAll('_', ' ')}</small>
                            {scopedPartIds.includes(part.id) && (
                              <small className="scope-label">Included in edit scope</small>
                            )}
                          </span>
                        </button>
                      )
                    })}
                  </div>
                  {artifact.project.warnings.map((warning) => (
                    <p className="part-warning" key={warning}>
                      {warning}
                    </p>
                  ))}
                  {artifact.project.interfaces.map((item) => (
                    <p className="part-warning" key={item.id}>
                      <strong>{item.interface_type.replaceAll('_', ' ')}</strong> ·{' '}
                      {item.fit_verified ? 'fit verified' : 'physical fit not verified'} ·{' '}
                      {item.rationale}
                    </p>
                  ))}
                </div>
              )}
              {artifact.revision && artifact.project && (
                <div className="panel-section revision-summary">
                  <span className="section-label">Changed in artifact v{artifact.version}</span>
                  <strong>
                    {artifact.revision.affected_part_ids
                      .map(
                        (partId) =>
                          artifact.project!.parts.find((part) => part.id === partId)?.name ??
                          partId,
                      )
                      .join(', ')}
                  </strong>
                  <p>{artifact.revision.rationale}</p>
                </div>
              )}
              {artifact.downloads.length > 0 && (
                <div className="panel-section">
                  <span className="section-label">Downloads</span>
                  <div className="download-list">
                    {artifact.downloads.map((download) => (
                      <a
                        key={download.path}
                        href={`${API}/workflows/${workflowId}/artifacts/${artifact.version}/${download.path}`}
                        download
                      >
                        {download.role.replaceAll('_', ' ')}
                        {download.part_id ? ` · ${download.part_id}` : ''}
                      </a>
                    ))}
                  </div>
                  <a
                    className="secondary-action package-download"
                    href={artifact.package.url}
                    download={artifact.package.filename}
                  >
                    Download all artifacts (.zip) ·{' '}
                    {formatNumber(artifact.package.size_bytes / 1024, 0)} KB
                  </a>
                </div>
              )}
              <div className="digest">
                <span>Immutable manifest</span>
                <code>{artifact.manifest_digest}</code>
              </div>
              {artifact.source_available && (
                <button className="secondary-action" onClick={() => setSourceOpen(!sourceOpen)}>
                  {sourceOpen ? 'Hide OpenSCAD source' : 'Inspect OpenSCAD source'}
                </button>
              )}
            </aside>
          </section>

          {sourceOpen && sourceQuery.data && (
            <section className="source-panel">
              <div>
                <span className="section-label">Adopted source · artifact v{artifact.version}</span>
                <button className="text-button" onClick={() => navigator.clipboard.writeText(sourceQuery.data!)}>
                  Copy source
                </button>
              </div>
              <pre>{sourceQuery.data}</pre>
            </section>
          )}

          {!workflow.archived_at &&
            ['awaiting_approval', 'approved'].includes(workflow.state) && (
            <section className="approval-panel">
              <div>
                <span className="section-label">
                  {workflow.state === 'approved' ? 'Artifact approved' : 'Human approval required'}
                </span>
                <h2>
                  {workflow.state === 'approved'
                    ? `Artifact v${artifact.version} is approved`
                    : 'Does this exact model look ready?'}
                </h2>
                <p>
                  Approval is bound to artifact v{artifact.version} and its manifest digest.
                  Any revision creates a new artifact that must be inspected again.
                </p>
              </div>
              <div className="approval-actions">
                <textarea
                  value={feedback}
                  onChange={(event) => setFeedback(event.target.value)}
                  placeholder="Describe what should change…"
                  rows={3}
                />
                {isMultipart && artifact.project && (
                  <p className="revision-target">
                    {scopedPartIds.length > 0
                      ? `Edit scope: ${scopedPartIds
                          .map(
                            (partId) =>
                              artifact.project!.parts.find((part) => part.id === partId)?.name ??
                              partId,
                          )
                          .join(', ')}`
                      : 'Edit scope: agent decides affected parts'}
                  </p>
                )}
                <div>
                  <button
                    className="secondary-action"
                    disabled={!feedback.trim() || revise.isPending || !artifact.source_available}
                    onClick={() => revise.mutate('refine_current')}
                    title={
                      artifact.source_available
                        ? 'Edit this artifact from its exact OpenSCAD source'
                        : 'This STL-only artifact has no editable OpenSCAD source'
                    }
                  >
                    Edit existing
                  </button>
                  <button
                    className="secondary-action"
                    disabled={!feedback.trim() || revise.isPending}
                    onClick={() => revise.mutate('search_new_base')}
                  >
                    Search new base
                  </button>
                  <button className="secondary-action" disabled={copy.isPending} onClick={() => copy.mutate()}>
                    Make a copy
                  </button>
                  {workflow.state === 'awaiting_approval' ? (
                    <button
                      className="primary-action"
                      disabled={approve.isPending}
                      onClick={() => approve.mutate()}
                    >
                      {approve.isPending ? 'Approving…' : 'Approve model'}
                      <span>✓</span>
                    </button>
                  ) : !requiresSlicing ? (
                    <button
                      className="primary-action"
                      disabled={print.isPending}
                      onClick={() => print.mutate()}
                    >
                      {print.isPending ? 'Queuing…' : 'Send to printer'}
                      <span>→</span>
                    </button>
                  ) : (
                    <span className="muted">Continue with material assignment below.</span>
                  )}
                </div>
                {(approve.error || revise.error || print.error || copy.error) && (
                  <p className="error-copy">
                    {approve.error?.message ??
                      revise.error?.message ??
                      print.error?.message ??
                      copy.error?.message}
                  </p>
                )}
              </div>
            </section>
          )}

          {!workflow.archived_at &&
            requiresSlicing &&
            [
              'awaiting_approval',
              'approved',
              'slice_setup',
              'awaiting_material_review',
              'slice_requested',
              'slicing',
              'slice_validating',
              'awaiting_slice_review',
              'slice_failed',
            ].includes(workflow.state) && (
              <section className="fabrication-panel">
                <div>
                  <span className="section-label">Dedicated H2D slicing workspace</span>
                  <h2>{data.printer_snapshot?.profile.display_name}</h2>
                  <p>
                    Continue in the guided workspace to refresh cloud inventory, review
                    materials, slice, and download the printer-ready artifact.
                  </p>
                </div>
                <a className="primary-action" href={`#/slices/${workflowId}`}>
                  Open slicing workspace <span>→</span>
                </a>
              </section>
            )}

          {data.job && (
            <section className="print-status">
              <div className="printer-glyph">▦</div>
              <div>
                <span className="section-label">Printer adapter · {workflow.printer_name}</span>
                <h2>{formatState(data.job.status)}</h2>
                <p>{data.job.message}</p>
                <code>{data.job.external_id}</code>
              </div>
              {!workflow.archived_at && ['queued', 'printing'].includes(data.job.status) && (
                <button className="danger-action" onClick={() => cancel.mutate()}>
                  Cancel print
                </button>
              )}
            </section>
          )}
        </>
      )}

      {workflow.failure_message && (
        <section className="failure-panel">
          <strong>Workflow stopped</strong>
          <p>{workflow.failure_message}</p>
        </section>
      )}
      {(archive.error || cancel.error) && (
        <p className="error-copy">
          {archive.error?.message ?? cancel.error?.message}
        </p>
      )}
    </main>
  )
}

type AppRoute =
  | { page: 'create' }
  | { page: 'models' }
  | { page: 'fabrication' }
  | { page: 'slice'; workflowId: string }
  | { page: 'workflow'; workflowId: string }

function readRoute(): AppRoute {
  const slice = window.location.hash.match(/^#\/slices\/(.+)$/)
  if (slice) return { page: 'slice', workflowId: slice[1] }
  const workflow = window.location.hash.match(/^#\/workflows\/(.+)$/)
  if (workflow) return { page: 'workflow', workflowId: workflow[1] }
  if (window.location.hash === '#/models') return { page: 'models' }
  if (window.location.hash === '#/fabrication') return { page: 'fabrication' }
  return { page: 'create' }
}

function App() {
  const [route, setRoute] = useState<AppRoute>(readRoute)

  const navigate = (
    path:
      | '/'
      | '/models'
      | '/fabrication'
      | `/workflows/${string}`
      | `/slices/${string}`,
  ) => {
    window.location.hash = path
    setRoute(readRoute())
  }

  useEffect(() => {
    const syncRoute = () => setRoute(readRoute())
    window.addEventListener('hashchange', syncRoute)
    return () => window.removeEventListener('hashchange', syncRoute)
  }, [])

  return (
    <>
      <nav className="topbar">
        <button className="brand" onClick={() => navigate('/')}>
          <span className="brand-cube">⬡</span>
          Form &amp; Function
        </button>
        <div className="nav-tabs" aria-label="Primary navigation">
          <button
            className={route.page === 'create' ? 'active' : ''}
            aria-current={route.page === 'create' ? 'page' : undefined}
            onClick={() => navigate('/')}
          >
            Create
          </button>
          <button
            className={
              route.page === 'models' ||
              route.page === 'workflow' ||
              route.page === 'slice'
                ? 'active'
                : ''
            }
            aria-current={
              route.page === 'models' ||
              route.page === 'workflow' ||
              route.page === 'slice'
                ? 'page'
                : undefined
            }
            onClick={() => navigate('/models')}
          >
            Models
          </button>
          <button
            className={route.page === 'fabrication' ? 'active' : ''}
            aria-current={route.page === 'fabrication' ? 'page' : undefined}
            onClick={() => navigate('/fabrication')}
          >
            Slicing profiles &amp; cloud
          </button>
        </div>
      </nav>
      {route.page === 'workflow' ? (
        <WorkflowPanel workflowId={route.workflowId} onReset={() => navigate('/models')} />
      ) : route.page === 'slice' ? (
        <SlicingWorkspace
          workflowId={route.workflowId}
          onBack={() => navigate('/models')}
        />
      ) : route.page === 'models' ? (
        <DashboardPanel
          onOpen={(id) => navigate(`/workflows/${id}`)}
          onSlice={(id) => navigate(`/slices/${id}`)}
        />
      ) : route.page === 'fabrication' ? (
        <FabricationSettings />
      ) : (
        <StartPanel onCreated={(id) => navigate(`/workflows/${id}`)} />
      )}
    </>
  )
}

export default App
