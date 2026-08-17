import { useCallback, useEffect, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import * as THREE from 'three'
import { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js'
import { STLLoader } from 'three/examples/jsm/loaders/STLLoader.js'
import './App.css'
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

type WorkflowResponse = {
  workflow: Workflow
  artifact: Artifact | null
  job: PrintJob | null
}

type Printer = {
  name: string
  build_volume: Dimensions
  accepted_formats: string[]
  supported_materials: string[]
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
])
const INSPECTABLE_STATE = new Set([
  'awaiting_approval',
  'approved',
  'submitting',
  'queued',
  'printing',
  'completed',
  'print_failed',
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
    return ACTIVE_STATES.has(state) && !['submitting', 'queued', 'printing'].includes(state)
  if (filter === 'needs_approval') return state === 'awaiting_approval'
  if (filter === 'approved') return state === 'approved'
  if (filter === 'printing') return ['submitting', 'queued', 'printing'].includes(state)
  if (filter === 'completed') return state === 'completed'
  return ['preparation_failed', 'print_failed', 'cancelled'].includes(state)
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
  const printers = useQuery({ queryKey: ['printers'], queryFn: () => api<Printer[]>('/printers') })
  const create = useMutation({
    mutationFn: () =>
      api<Workflow>('/workflows', {
        method: 'POST',
        body: JSON.stringify({ requirement, printer_name: printer }),
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

function DashboardPanel({ onOpen }: { onOpen: (id: string) => void }) {
  const queryClient = useQueryClient()
  const [filter, setFilter] = useState<DashboardFilter>('all')
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
        {items.map(({ workflow, artifact, job }) => {
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
                  <button className="secondary-action" onClick={() => onOpen(workflow.id)}>
                    {workflow.state === 'awaiting_approval'
                      ? 'Inspect & approve'
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
                      {workflow.state === 'approved' && (
                        <button
                          className="primary-action"
                          disabled={print.isPending}
                          onClick={() => print.mutate(workflow.id)}
                        >
                          Send to printer <span>→</span>
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
                  ) : (
                    <button
                      className="primary-action"
                      disabled={print.isPending}
                      onClick={() => print.mutate()}
                    >
                      {print.isPending ? 'Queuing…' : 'Send to printer'}
                      <span>→</span>
                    </button>
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
      {archive.error && <p className="error-copy">{archive.error.message}</p>}
    </main>
  )
}

type AppRoute =
  | { page: 'create' }
  | { page: 'models' }
  | { page: 'workflow'; workflowId: string }

function readRoute(): AppRoute {
  const workflow = window.location.hash.match(/^#\/workflows\/(.+)$/)
  if (workflow) return { page: 'workflow', workflowId: workflow[1] }
  if (window.location.hash === '#/models') return { page: 'models' }
  return { page: 'create' }
}

function App() {
  const [route, setRoute] = useState<AppRoute>(readRoute)

  const navigate = (path: '/' | '/models' | `/workflows/${string}`) => {
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
            className={route.page !== 'create' ? 'active' : ''}
            aria-current={route.page !== 'create' ? 'page' : undefined}
            onClick={() => navigate('/models')}
          >
            Models
          </button>
        </div>
      </nav>
      {route.page === 'workflow' ? (
        <WorkflowPanel workflowId={route.workflowId} onReset={() => navigate('/models')} />
      ) : route.page === 'models' ? (
        <DashboardPanel onOpen={(id) => navigate(`/workflows/${id}`)} />
      ) : (
        <StartPanel onCreated={(id) => navigate(`/workflows/${id}`)} />
      )}
    </>
  )
}

export default App
