import { useEffect, useRef, useState } from 'react'
import * as THREE from 'three'
import { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js'
import { STLLoader } from 'three/examples/jsm/loaders/STLLoader.js'

type ViewerPart = {
  id: string
  name: string
  color: string
  url: string
}

type ViewerInstance = {
  id: string
  partId: string
  transform: number[]
}

export function MultipartPartsViewer({
  parts,
  instances,
  selectedPartId,
  onSelectPart,
}: {
  parts: ViewerPart[]
  instances: ViewerInstance[]
  selectedPartId: string | null
  onSelectPart: (partId: string) => void
}) {
  const containerRef = useRef<HTMLDivElement>(null)
  const [mode, setMode] = useState<'unique' | 'quantities'>('unique')
  const [wireframe, setWireframe] = useState(false)
  const [errors, setErrors] = useState<string[]>([])

  useEffect(() => {
    const container = containerRef.current
    if (!container) return
    const scene = new THREE.Scene()
    scene.background = new THREE.Color('#111519')
    const camera = new THREE.PerspectiveCamera(
      42,
      container.clientWidth / container.clientHeight,
      0.1,
      5000,
    )
    const renderer = new THREE.WebGLRenderer({ antialias: true })
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2))
    renderer.setSize(container.clientWidth, container.clientHeight)
    renderer.outputColorSpace = THREE.SRGBColorSpace
    container.appendChild(renderer.domElement)
    const controls = new OrbitControls(camera, renderer.domElement)
    controls.enableDamping = true
    scene.add(new THREE.HemisphereLight('#eaf7ff', '#25301d', 2.1))
    const key = new THREE.DirectionalLight('#ffffff', 3.2)
    key.position.set(120, 180, 100)
    scene.add(key)
    scene.add(new THREE.GridHelper(300, 24, '#4e645f', '#25302e'))

    const loader = new STLLoader()
    const meshes: THREE.Mesh[] = []
    let disposed = false
    let frame = 0
    const load = async () => {
      const failures: string[] = []
      const geometries = new Map<string, THREE.BufferGeometry>()
      await Promise.all(
        parts.map(async (part) => {
          try {
            const geometry = await loader.loadAsync(part.url)
            geometry.computeVertexNormals()
            geometries.set(part.id, geometry)
          } catch {
            failures.push(`${part.name}: failed to load`)
          }
        }),
      )
      if (disposed) {
        geometries.forEach((geometry) => geometry.dispose())
        return
      }
      setErrors(failures)
      let cursor = 0
      const renderItems =
        mode === 'quantities'
          ? instances
          : parts.map((part) => ({
              id: `${part.id}_unique`,
              partId: part.id,
              transform: [],
            }))
      renderItems.forEach((item) => {
        const part = parts.find((candidate) => candidate.id === item.partId)
        const source = geometries.get(item.partId)
        if (!part || !source) return
        const geometry = source.clone()
        geometry.computeBoundingBox()
        const bounds = geometry.boundingBox!
        const mesh = new THREE.Mesh(
          geometry,
          new THREE.MeshStandardMaterial({
            color: selectedPartId === part.id ? '#f2b45e' : part.color,
            roughness: 0.42,
            metalness: 0.08,
            wireframe,
          }),
        )
        mesh.userData.partId = part.id
        if (mode === 'quantities' && item.transform.length === 16) {
          mesh.matrixAutoUpdate = false
          mesh.matrix.fromArray(item.transform).transpose()
        } else {
          mesh.position.set(cursor - bounds.min.x, -bounds.min.y, -bounds.min.z)
          cursor += bounds.max.x - bounds.min.x + 8
        }
        scene.add(mesh)
        meshes.push(mesh)
      })
      geometries.forEach((geometry) => geometry.dispose())
      const bounds = new THREE.Box3()
      meshes.forEach((mesh) => bounds.expandByObject(mesh))
      if (!bounds.isEmpty()) {
        const size = bounds.getSize(new THREE.Vector3())
        const center = bounds.getCenter(new THREE.Vector3())
        const span = Math.max(size.x, size.y, size.z, 20)
        camera.position.set(center.x + span * 1.5, center.y + span, center.z + span * 1.5)
        controls.target.copy(center)
      }
    }
    void load()

    const raycaster = new THREE.Raycaster()
    const pointer = new THREE.Vector2()
    const selectMesh = (event: PointerEvent) => {
      const rect = renderer.domElement.getBoundingClientRect()
      pointer.x = ((event.clientX - rect.left) / rect.width) * 2 - 1
      pointer.y = -((event.clientY - rect.top) / rect.height) * 2 + 1
      raycaster.setFromCamera(pointer, camera)
      const hit = raycaster.intersectObjects(meshes, false)[0]
      if (hit?.object.userData.partId) onSelectPart(hit.object.userData.partId)
    }
    renderer.domElement.addEventListener('pointerdown', selectMesh)
    const resize = new ResizeObserver(() => {
      renderer.setSize(container.clientWidth, container.clientHeight)
      camera.aspect = container.clientWidth / container.clientHeight
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
      disposed = true
      cancelAnimationFrame(frame)
      resize.disconnect()
      renderer.domElement.removeEventListener('pointerdown', selectMesh)
      controls.dispose()
      meshes.forEach((mesh) => {
        mesh.geometry.dispose()
        if (Array.isArray(mesh.material)) mesh.material.forEach((item) => item.dispose())
        else mesh.material.dispose()
      })
      renderer.dispose()
      container.removeChild(renderer.domElement)
    }
  }, [instances, mode, onSelectPart, parts, selectedPartId, wireframe])

  return (
    <div className="viewer-shell">
      <div className="viewer-toolbar">
        <button className={mode === 'unique' ? 'active' : ''} onClick={() => setMode('unique')}>
          Unique parts
        </button>
        <button
          className={mode === 'quantities' ? 'active' : ''}
          onClick={() => setMode('quantities')}
        >
          Print quantities
        </button>
        <button className={wireframe ? 'active' : ''} onClick={() => setWireframe(!wireframe)}>
          Wireframe
        </button>
      </div>
      <div className="model-viewer" ref={containerRef} aria-label="Separated multipart STL viewer" />
      <div className="viewer-notice">Separated parts / print layout—not assembled</div>
      {errors.length > 0 && <div className="viewer-errors">{errors.join(' · ')}</div>}
    </div>
  )
}
