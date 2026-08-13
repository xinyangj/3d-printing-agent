import { useEffect, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import * as THREE from 'three'
import { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js'
import { STLLoader } from 'three/examples/jsm/loaders/STLLoader.js'
import './App.css'

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
      'modeling.handoff_ready',
      'modeling.started',
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
    <main className="start-layout">
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
  const workflowQuery = useQuery({
    queryKey: ['workflow', workflowId],
    queryFn: () => api<WorkflowResponse>(`/workflows/${workflowId}`),
    refetchInterval: (query) =>
      query.state.data && TERMINAL_STATES.has(query.state.data.workflow.state) ? false : 2500,
  })
  const events = useWorkflowEvents(workflowId)
  const data = workflowQuery.data
  const artifact = data?.artifact
  const workflow = data?.workflow
  const canInspect = Boolean(workflow && artifact && INSPECTABLE_STATE.has(workflow.state))
  const modelUrl = artifact
    ? `${API}/workflows/${workflowId}/artifacts/${artifact.version}/model.stl`
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
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ['workflow', workflowId] }),
  })
  const revise = useMutation({
    mutationFn: (mode: 'refine_current' | 'search_new_base') =>
      api(`/workflows/${workflowId}/revisions`, {
        method: 'POST',
        body: JSON.stringify({ mode, feedback }),
      }),
    onSuccess: () => {
      setFeedback('')
      void queryClient.invalidateQueries({ queryKey: ['workflow', workflowId] })
    },
  })
  const cancel = useMutation({
    mutationFn: () => api(`/workflows/${workflowId}/cancel`, { method: 'POST' }),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ['workflow', workflowId] }),
  })

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
            ← New request
          </button>
          <div className="eyebrow">Workflow {workflow.id.slice(0, 8)}</div>
          <h1>{canInspect ? 'Inspect final model' : 'Preparing your model'}</h1>
        </div>
        <div className={`state-pill state-${workflow.state}`}>
          <span />
          {formatState(workflow.state)}
        </div>
      </header>

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
            <ModelViewer url={modelUrl} dimensions={artifact.mesh.dimensions} />
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

          {workflow.state === 'awaiting_approval' && (
            <section className="approval-panel">
              <div>
                <span className="section-label">Human approval required</span>
                <h2>Does this exact model look ready to print?</h2>
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
                <div>
                  <button
                    className="secondary-action"
                    disabled={!feedback.trim() || revise.isPending}
                    onClick={() => revise.mutate('refine_current')}
                  >
                    Refine current
                  </button>
                  <button
                    className="secondary-action"
                    disabled={!feedback.trim() || revise.isPending}
                    onClick={() => revise.mutate('search_new_base')}
                  >
                    Search new base
                  </button>
                  <button
                    className="primary-action"
                    disabled={approve.isPending}
                    onClick={() => approve.mutate()}
                  >
                    {approve.isPending ? 'Approving…' : 'Approve & print'}
                    <span>→</span>
                  </button>
                </div>
                {(approve.error || revise.error) && (
                  <p className="error-copy">{approve.error?.message ?? revise.error?.message}</p>
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
              {['queued', 'printing'].includes(data.job.status) && (
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
    </main>
  )
}

function App() {
  const [workflowId, setWorkflowId] = useState<string | null>(() => {
    const value = window.location.hash.match(/^#\/workflows\/(.+)$/)
    return value?.[1] ?? null
  })

  const navigate = (id: string | null) => {
    window.location.hash = id ? `/workflows/${id}` : ''
    setWorkflowId(id)
  }

  return (
    <>
      <nav className="topbar">
        <button className="brand" onClick={() => navigate(null)}>
          <span className="brand-cube">⬡</span>
          Form &amp; Function
        </button>
        <div className="architecture-badge">
          <span>Discovery</span>
          <i />
          <span>Modeling</span>
          <i />
          <span>Printer adapter</span>
        </div>
      </nav>
      {workflowId ? (
        <WorkflowPanel workflowId={workflowId} onReset={() => navigate(null)} />
      ) : (
        <StartPanel onCreated={(id) => navigate(id)} />
      )}
    </>
  )
}

export default App
