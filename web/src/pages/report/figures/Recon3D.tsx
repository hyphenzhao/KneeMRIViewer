/**
 * 3D reconstruction snapshot for the report.
 *
 * Rendered in the browser with vtk.js from the cached display meshes, captured
 * once to a PNG, and shown as a plain image in every view. A static picture is
 * what a report is; rotating belongs to the viewer. It also prints reliably -
 * the print engine has no WebGL - and one capture per report keeps us far from
 * the browser's WebGL-context limit. The server cannot render this itself
 * (no display), which is why the PDF renderer runs a real browser.
 */
import { useEffect, useRef, useState } from 'react'

import '@kitware/vtk.js/Rendering/Profiles/Geometry'
import vtkActor from '@kitware/vtk.js/Rendering/Core/Actor'
import vtkMapper from '@kitware/vtk.js/Rendering/Core/Mapper'
import vtkGenericRenderWindow from '@kitware/vtk.js/Rendering/Misc/GenericRenderWindow'
import vtkPolyData from '@kitware/vtk.js/Common/DataModel/PolyData'

import { api, hexToRgb, type LabelSet, type Morphometry } from '../../../api'
import { decodeMesh } from '../../../cs/mesh'
import { useFigureReady } from '../ready'
import type { TileBox } from './PlateMaps'

// Fallback colours = the knee label set shipped with the platform.
const DEFAULT_COLORS: Record<number, string> = {
  1: '#D6C6A5', 2: '#B9A97E', 3: '#E8DCC0', 4: '#E6194B', 5: '#F58231', 6: '#3CB44B', 7: '#42D4F4', 8: '#911EB4',
}

const CAPTURE_W = 720, CAPTURE_H = 800

// One capture per (segmentation, background); the figure mounts again when
// the page re-paginates and must not render the scene twice.
const cache = new Map<string, Promise<string | null>>()

type Vec3 = [number, number, number]

function norm(v: Vec3): Vec3 {
  const n = Math.hypot(v[0], v[1], v[2]) || 1
  return [v[0] / n, v[1] / n, v[2] / n]
}

async function snapshot(segId: number, labelSet: LabelSet | null, frame: Morphometry['frame'],
  background: string): Promise<string | null> {
  const list = await api.meshes(segId)
  if (!list.meshes.length) return null
  const bg = hexToRgb(background).map((c) => c / 255) as Vec3

  const host = document.createElement('div')
  host.style.cssText = `position:fixed;left:-20000px;top:0;width:${CAPTURE_W}px;height:${CAPTURE_H}px;`
  document.body.appendChild(host)
  const grw = vtkGenericRenderWindow.newInstance({ background: bg, listenWindowResize: false })
  try {
    grw.setContainer(host)
    grw.resize()
    const renderer = grw.getRenderer()
    const renderWindow = grw.getRenderWindow()
    const colors: Record<number, string> = { ...DEFAULT_COLORS }
    for (const l of labelSet?.labels ?? []) colors[l.value] = l.color

    const buffers = await Promise.all(list.meshes.map((m) => api.binary(m.url)))
    let any = false
    list.meshes.forEach((m, i) => {
      const mesh = decodeMesh(buffers[i])
      if (!mesh.nTris) return
      const pd = vtkPolyData.newInstance()
      pd.getPoints().setData(mesh.points, 3)
      pd.getPolys().setData(mesh.polys)
      const mapper = vtkMapper.newInstance()
      mapper.setInputData(pd)
      const actor = vtkActor.newInstance()
      actor.setMapper(mapper)
      const [r, g, b] = hexToRgb(colors[m.label_value] ?? '#cccccc')
      actor.getProperty().setColor(r / 255, g / 255, b / 255)
      actor.getProperty().setSpecular(0.15)
      renderer.addActor(actor)
      any = true
    })
    if (!any) return null

    // Anterior view, turned 20 deg toward the medial side, superior up. The
    // anatomic frame is the morphometry's own, so left and right knees pose
    // the same way.
    const cam = renderer.getActiveCamera()
    cam.setParallelProjection(true)
    if (frame && 'e_AP' in frame) {
      const f = frame as unknown as { e_ML: Vec3; e_AP: Vec3; e_SI: Vec3; medial_sign: number }
      const c = Math.cos(Math.PI / 9), s = Math.sin(Math.PI / 9) * (f.medial_sign || 1)
      const dir = norm([c * f.e_AP[0] + s * f.e_ML[0], c * f.e_AP[1] + s * f.e_ML[1], c * f.e_AP[2] + s * f.e_ML[2]])
      cam.setFocalPoint(0, 0, 0)
      cam.setPosition(dir[0] * 400, dir[1] * 400, dir[2] * 400)
      cam.setViewUp(f.e_SI[0], f.e_SI[1], f.e_SI[2])
    } else {
      cam.setFocalPoint(0, 0, 0)
      cam.setPosition(0, -400, 0)     // LPS: anterior is -y
      cam.setViewUp(0, 0, 1)
    }
    renderer.resetCamera()
    cam.setParallelScale(cam.getParallelScale() * 0.9)
    renderer.resetCameraClippingRange()
    renderWindow.render()
    const images = await Promise.all(renderWindow.captureImages('image/png'))
    return images[0] ?? null
  } finally {
    try { grw.delete() } catch { /* context already gone */ }
    host.remove()
  }
}

export function Recon3D({ segId, labelSet, frame, box, background }: {
  segId: number; labelSet: LabelSet | null; frame: Morphometry['frame']; box: TileBox; background: string
}) {
  const done = useFigureReady('recon3d')
  const [src, setSrc] = useState<string | null>(null)
  const [state, setState] = useState<'loading' | 'ok' | 'skipped' | 'error'>('loading')
  const alive = useRef(true)

  useEffect(() => {
    alive.current = true
    const key = `${segId}|${background}`
    let p = cache.get(key)
    if (!p) {
      const canvas = document.createElement('canvas')
      const gl = canvas.getContext('webgl2') || canvas.getContext('webgl')
      if (!gl) {
        p = Promise.resolve(null)
      } else {
        p = snapshot(segId, labelSet, frame, background).catch((e) => { console.warn('[report] 3D snapshot failed', e); throw e })
      }
      cache.set(key, p)
    }
    p.then((url) => {
      if (!alive.current) return
      if (url) { setSrc(url); setState('ok') }
      else { setState('skipped'); done('skipped') }
    }, () => {
      if (!alive.current) return
      cache.delete(key)
      setState('error'); done('error')
    })
    return () => { alive.current = false }
  }, [segId, labelSet, frame, background, done])

  return (
    <div className="rp-recon" style={{ width: `${box.wMm}mm`, height: `${box.hMm}mm` }}
      data-figure-skipped={state === 'skipped' ? 'meshes' : state === 'error' ? 'error' : undefined}>
      {src && (
        <img data-figure="recon3d" src={src} alt="三维重建"
          onLoad={(e) => { const img = e.currentTarget; (img.decode ? img.decode() : Promise.resolve()).then(() => done('ok'), () => done('ok')) }}
          onError={() => done('error')} />
      )}
      {state === 'loading' && <div className="rp-fig-missing">三维重建渲染中…</div>}
      {state === 'skipped' && <div className="rp-fig-missing">三维重建未生成（无网格或无 WebGL）</div>}
      {state === 'error' && <div className="rp-fig-missing">三维重建失败</div>}
    </div>
  )
}
