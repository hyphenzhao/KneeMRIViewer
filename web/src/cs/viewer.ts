/**
 * The four-up viewer: axial / coronal / sagittal MPR plus a 3D window.
 *
 * On anisotropy: these knee scans are 0.29 x 0.29 x 3.0 mm. Cornerstone builds
 * a real 3-D texture with the true world spacing, so the reformatted coronal
 * and axial planes are geometrically correct - they are simply blurred along
 * the acquisition normal, because that is all the data there is. We surface
 * that honestly in the viewport corner rather than pretending otherwise, and
 * default the layout so the acquisition plane is the one you look at first.
 *
 * The 3D window renders **precomputed label surfaces**, not a volume rendering
 * of a 30-slice fat-sat knee (which looks like a banded blob). Surfaces come
 * from the server already smoothed and decimated.
 */
import {
  Enums,
  RenderingEngine,
  cache,
  geometryLoader,
  setVolumesForViewports,
  type Types,
} from '@cornerstonejs/core'
import {
  utilities as csToolsUtilities,
  Enums as csToolsEnums,
  PanTool,
  StackScrollTool,
  ToolGroupManager,
  TrackballRotateTool,
  WindowLevelTool,
  ZoomTool,
  segmentation,
} from '@cornerstonejs/tools'

import { api, hexToRgb, type LabelSet, type LabelStat, type MeshEntry } from '../api'
import { initCornerstone } from './init'
import { decodeMesh } from './mesh'
import {
  ensureVolume, getLabelmapMeta, getVolumeMeta, reportStage, segVolumeId,
  seriesVolumeId, setSegFrameOfReference,
} from './volumeLoader'

const { ViewportType, OrientationAxis } = Enums
const { MouseBindings } = csToolsEnums
const SegRep = csToolsEnums.SegmentationRepresentations

export const ENGINE_ID = 'mriv-engine'
export const TOOL_GROUP_2D = 'mriv-2d'
export const TOOL_GROUP_3D = 'mriv-3d'

export const VP = {
  axial: 'vp-axial',
  coronal: 'vp-coronal',
  sagittal: 'vp-sagittal',
  volume3d: 'vp-3d',
} as const

export type PlaneId = 'axial' | 'coronal' | 'sagittal'
export const PLANES: PlaneId[] = ['axial', 'coronal', 'sagittal']

const ORIENTATION: Record<PlaneId, Enums.OrientationAxis> = {
  axial: OrientationAxis.AXIAL,
  coronal: OrientationAxis.CORONAL,
  sagittal: OrientationAxis.SAGITTAL,
}

export interface LoadedSeries {
  seriesId: number
  volumeId: string
  dimensions: [number, number, number]
  spacing: [number, number, number]
  anisotropy: number
  plane: string
  gzipBytes: number
  byteLength: number
  variant: string
  warning: string | null
}

export interface LoadedSegmentation {
  segId: number
  segmentationId: string
  presentValues: number[]
  labelSet: LabelSet | null
  meshes: MeshEntry[]
  /** Per-label geometry/intensity summary, from the same descriptor fetch. */
  stats: LabelStat[]
}

let engine: RenderingEngine | null = null

const nextFrame = () =>
  new Promise<void>((resolve) => requestAnimationFrame(() => resolve()))


export async function ensureEngine(): Promise<RenderingEngine> {
  await initCornerstone()
  if (!engine) engine = new RenderingEngine(ENGINE_ID)
  return engine
}

export function getEngine(): RenderingEngine | null {
  return engine
}

/** Attach the four viewports to their DOM elements. Safe to call repeatedly. */
export async function setupViewports(elements: Record<string, HTMLDivElement>) {
  const eng = await ensureEngine()
  const tools = await import('@cornerstonejs/tools')

  const addOnce = (T: { toolName: string }) => {
    try {
      tools.addTool(T as never)
    } catch {
      /* already registered */
    }
  }
  addOnce(WindowLevelTool)
  addOnce(PanTool)
  addOnce(ZoomTool)
  addOnce(StackScrollTool)
  addOnce(TrackballRotateTool)

  eng.setViewports([
    ...PLANES.map((p) => ({
      viewportId: VP[p],
      type: ViewportType.ORTHOGRAPHIC,
      element: elements[p],
      defaultOptions: { orientation: ORIENTATION[p], background: [0, 0, 0] as Types.Point3 },
    })),
    {
      viewportId: VP.volume3d,
      type: ViewportType.VOLUME_3D,
      element: elements.volume3d,
      defaultOptions: { background: [0.05, 0.06, 0.09] as Types.Point3 },
    },
  ])

  let g2 = ToolGroupManager.getToolGroup(TOOL_GROUP_2D)
  if (!g2) {
    g2 = ToolGroupManager.createToolGroup(TOOL_GROUP_2D)!
    g2.addTool(WindowLevelTool.toolName)
    g2.addTool(PanTool.toolName)
    g2.addTool(ZoomTool.toolName)
    g2.addTool(StackScrollTool.toolName)
    g2.setToolActive(WindowLevelTool.toolName, {
      bindings: [{ mouseButton: MouseBindings.Primary }],
    })
    g2.setToolActive(PanTool.toolName, {
      bindings: [{ mouseButton: MouseBindings.Auxiliary }],
    })
    g2.setToolActive(ZoomTool.toolName, {
      bindings: [{ mouseButton: MouseBindings.Secondary }],
    })
    g2.setToolActive(StackScrollTool.toolName, {
      bindings: [{ mouseButton: MouseBindings.Wheel }],
    })
  }
  for (const p of PLANES) g2.addViewport(VP[p], ENGINE_ID)

  let g3 = ToolGroupManager.getToolGroup(TOOL_GROUP_3D)
  if (!g3) {
    g3 = ToolGroupManager.createToolGroup(TOOL_GROUP_3D)!
    g3.addTool(TrackballRotateTool.toolName)
    g3.addTool(ZoomTool.toolName)
    g3.addTool(PanTool.toolName)
    g3.setToolActive(TrackballRotateTool.toolName, {
      bindings: [{ mouseButton: MouseBindings.Primary }],
    })
    g3.setToolActive(ZoomTool.toolName, {
      bindings: [{ mouseButton: MouseBindings.Secondary }],
    })
    g3.setToolActive(PanTool.toolName, {
      bindings: [{ mouseButton: MouseBindings.Auxiliary }],
    })
  }
  g3.addViewport(VP.volume3d, ENGINE_ID)

  return eng
}


/**
 * Ask Cornerstone to render segmentations into a viewport, and make sure it
 * actually happened.
 *
 * Its rendering engine coalesces requests into a single requestAnimationFrame
 * and pushes anything asked for while a frame is already pending onto a queue
 * that drains one entry per frame. Our four viewports get their representations
 * across several async steps, so a single fire-and-forget trigger routinely
 * lands in that queue and quietly does nothing - the viewport stays black with
 * no error anywhere. So: trigger, check for the actors, try again for a few
 * frames, and say so if it never takes.
 */
async function renderSegmentationInto(
  viewportId: string,
  minActors: number,
  attempts = 30,
): Promise<boolean> {
  for (let i = 0; i < attempts; i++) {
    // Draw the viewport first. A VOLUME_3D viewport that was never given a
    // volume has not built its vtk renderer yet, and the segmentation render
    // pass silently produces no actors until it has.
    engine?.renderViewports([viewportId])
    await nextFrame()
    csToolsUtilities.segmentation.triggerSegmentationRender(viewportId)
    await nextFrame()
    await new Promise((r) => setTimeout(r, 40))
    const n = engine?.getViewport(viewportId)?.getActors()?.length ?? 0
    if (n >= minActors) {
      engine?.renderViewports([viewportId])
      return true
    }
  }
  console.warn(
    `[mriv] segmentation never rendered into ${viewportId} after ${attempts} attempts`,
  )
  return false
}

/**
 * Detach every segmentation from every viewport and drop them from Cornerstone's
 * state.
 *
 * This has to happen on *every* series change, not only when the new series has
 * a segmentation of its own. Otherwise opening an unannotated knee leaves the
 * previous patient's cartilage painted over it - which in a viewer used to
 * check segmentation results is not a cosmetic bug, it is a wrong answer.
 *
 * The cached volumes, labelmaps and surface geometry are deliberately left in
 * the cache, so flipping back to a case you already opened costs no download.
 */
export function clearSegmentations(): void {
  const ids = segmentation.state.getSegmentations().map((s) => s.segmentationId)
  for (const viewportId of Object.values(VP)) {
    try {
      segmentation.removeSegmentationRepresentations(viewportId, {}, true)
    } catch {
      /* viewport may not be enabled yet */
    }
  }
  for (const id of ids) {
    try {
      segmentation.removeSegmentation(id)
    } catch {
      /* already gone */
    }
  }
  surfaceLabels.clear()
  engine?.renderViewports(Object.values(VP))
}

export async function loadSeries(seriesId: number, variant = 'native'): Promise<LoadedSeries> {
  const eng = await ensureEngine()
  const volumeId = seriesVolumeId(seriesId, variant)
  await ensureVolume(volumeId)

  await setVolumesForViewports(eng, [{ volumeId }], PLANES.map((p) => VP[p]))
  eng.renderViewports(PLANES.map((p) => VP[p]))

  // Frame the cameras only once the volume actors are actually in the scene.
  // resetCamera() immediately after setVolumesForViewports can run before the
  // actor bounds exist, which leaves a viewport pointing at nothing - the
  // reformat is blank even though the data is there. It bit the axial plane in
  // particular, and only on series with no segmentation: on annotated ones the
  // extra render passes that attach the labelmap happened to fix the camera by
  // accident, which is exactly the kind of thing that hides a bug.
  await nextFrame()
  await nextFrame()
  for (const p of PLANES) eng.getViewport(VP[p])?.resetCamera()
  eng.renderViewports(PLANES.map((p) => VP[p]))

  const meta = getVolumeMeta(volumeId)!
  return {
    seriesId,
    volumeId,
    dimensions: meta.dimensions,
    spacing: meta.spacing,
    anisotropy: meta.anisotropy,
    plane: meta.plane,
    gzipBytes: meta.gzipBytes,
    byteLength: meta.byteLength,
    variant: meta.variant,
    warning: meta.warning,
  }
}

function colorLutFrom(labelSet: LabelSet | null, present: number[]): Types.ColorLUT {
  const max = Math.max(0, ...present)
  const lut: number[][] = [[0, 0, 0, 0]]
  for (let v = 1; v <= max; v++) {
    const def = labelSet?.labels.find((l) => l.value === v)
    const [r, g, b] = def ? hexToRgb(def.color) : [200, 200, 200]
    lut[v] = [r, g, b, Math.round(255 * (def?.opacity ?? 0.7))]
  }
  return lut as unknown as Types.ColorLUT
}

export async function loadSegmentation(
  segId: number,
  seriesVolumeIdStr: string,
): Promise<LoadedSegmentation> {
  const eng = await ensureEngine()
  const volumeId = segVolumeId(segId)
  // Must be set before the labelmap volume is built: it decides whether
  // Cornerstone considers the overlay compatible with the viewport at all.
  const seriesMeta = getVolumeMeta(seriesVolumeIdStr)
  const forUID = seriesMeta?.metadata?.FrameOfReferenceUID as string | undefined
  if (forUID) setSegFrameOfReference(volumeId, forUID)
  await ensureVolume(volumeId)
  const meta = getLabelmapMeta(volumeId)!
  const segmentationId = volumeId

  // Every present label must be declared here. Cornerstone's normaliser, given
  // a labelmap with no `config.segments`, registers exactly ONE segment (index
  // 1) - so segments 2..8 would not exist in its state and per-segment colour
  // and visibility would silently do nothing.
  const segments: Record<number, { label: string; active: boolean }> = {}
  for (const v of meta.presentValues) {
    const def = meta.labelSet?.labels.find((l) => l.value === v)
    segments[v] = { label: def?.nameZh || def?.name || `Label ${v}`, active: false }
  }

  // Cornerstone throws "Segmentation with id ... already exists" if the same id
  // is added twice, which is exactly what happens when you reopen a case you
  // looked at earlier. clearSegmentations() normally removes it first; this is
  // the belt to that braces.
  if (!segmentation.state.getSegmentation(segmentationId)) {
    segmentation.addSegmentations([
      {
        segmentationId,
        representation: {
          type: SegRep.Labelmap,
          data: { volumeId, referencedVolumeId: seriesVolumeIdStr },
        },
        config: {
          segments,
          segmentOrder: [...meta.presentValues],
          label: meta.labelSet?.name ?? 'segmentation',
        },
      },
    ])
  }

  const lutIndex = segmentation.config.color.addColorLUT(
    colorLutFrom(meta.labelSet, meta.presentValues),
  )
  for (const p of PLANES) {
    segmentation.addLabelmapRepresentationToViewport(VP[p], [{ segmentationId }])
    segmentation.config.color.setColorLUT(VP[p], segmentationId, lutIndex)
    // Mark it active for this viewport. `renderInactiveSegmentations` defaults
    // to false, so a labelmap that is attached but not active is simply not
    // drawn - the overlay silently never appears, with the representation and
    // its actors both present and correct.
    segmentation.activeSegmentation.setActiveSegmentation(VP[p], segmentationId)
  }
  // 2 actors = the image volume plus the labelmap overlay.
  await Promise.all(PLANES.map((p) => renderSegmentationInto(VP[p], 2)))
  eng.renderViewports(PLANES.map((p) => VP[p]))

  let meshes: MeshEntry[] = []
  try {
    const res = await api.meshes(segId)
    meshes = res.meshes
    if (!meshes.length) meshes = (await api.buildMeshes(segId), (await api.meshes(segId)).meshes)
  } catch {
    meshes = []
  }

  return {
    segId,
    segmentationId,
    presentValues: meta.presentValues,
    labelSet: meta.labelSet,
    meshes,
    stats: meta.stats.labels,
  }
}

/** Load the precomputed surfaces and show them in the 3D viewport. */
export async function loadSurfaces(
  seg: LoadedSegmentation,
  visible: Set<number>,
  frameOfReferenceUID = 'mriv',
): Promise<void> {
  const eng = await ensureEngine()
  if (!seg.meshes.length) return

  const geometryIds = new Map<number, string>()
  // Meshes are many small files, so count them rather than bytes.
  let done = 0
  reportStage({ stage: 'mesh', received: 0, total: seg.meshes.length, detail: '3D 表面' })
  await Promise.all(
    seg.meshes.map(async (m) => {
      const geometryId = `${seg.segmentationId}:mesh:${m.label_value}`
      const buf = await api.binary(m.url)
      const { points, polys } = decodeMesh(buf)
      const def = seg.labelSet?.labels.find((l) => l.value === m.label_value)
      const [r, g, b] = def ? hexToRgb(def.color) : [200, 200, 200]
      if (!cache.getGeometry(geometryId)) {
        // Do NOT wrap this in a bare catch: a failure here leaves geometryIds
        // populated but the cache empty, and the 3D viewport then silently
        // renders nothing at all.
        geometryLoader.createAndCacheGeometry(geometryId, {
          type: Enums.GeometryType.SURFACE,
          geometryData: {
            id: geometryId,
            points: points as unknown as number[],
            polys: polys as unknown as number[],
            frameOfReferenceUID,
            color: [r, g, b] as Types.Point3,
            segmentIndex: m.label_value,
          },
        })
      }
      geometryIds.set(m.label_value, geometryId)
      done += 1
      reportStage({ stage: 'mesh', received: done, total: seg.meshes.length,
                    detail: '3D 表面' })
    }),
  )

  const surfaceSegId = `${seg.segmentationId}:surface`
  surfaceLabels.set(surfaceSegId, new Set(geometryIds.keys()))
  if (!segmentation.state.getSegmentation(surfaceSegId)) {
    segmentation.addSegmentations([
      {
        segmentationId: surfaceSegId,
        representation: { type: SegRep.Surface, data: { geometryIds } },
      },
    ])
  }
  segmentation.addSurfaceRepresentationToViewport(VP.volume3d, [
    { segmentationId: surfaceSegId },
  ])
  const ok = await renderSegmentationInto(VP.volume3d, geometryIds.size)
  applySurfaceVisibility(surfaceSegId, seg.presentValues, visible)
  if (ok) {
    // Only frame the camera once the surface actors exist - resetting an empty
    // 3D viewport just leaves the camera on a unit cube at the origin, and the
    // surfaces then sit far off-screen when they do arrive.
    const vp = eng.getViewport(VP.volume3d)
    vp?.resetCamera()
    vp?.render()
  }
  eng.renderViewports([VP.volume3d])
}

export function applyLabelVisibility(
  segmentationId: string,
  allValues: number[],
  visible: Set<number>,
) {
  for (const p of PLANES) {
    for (const v of allValues) {
      segmentation.config.visibility.setSegmentIndexVisibility(
        VP[p],
        { segmentationId, type: SegRep.Labelmap },
        v,
        visible.has(v),
      )
    }
  }
  engine?.renderViewports(PLANES.map((p) => VP[p]))
}

/** Labels that actually produced a mesh, per surface segmentation id. */
const surfaceLabels = new Map<string, Set<number>>()

export function applySurfaceVisibility(
  surfaceSegId: string,
  allValues: number[],
  visible: Set<number>,
) {
  // Only touch labels that have a surface. A structure too small to mesh has no
  // actor, and asking Cornerstone to toggle it throws
  // "Cannot read properties of null (reading 'updateSurfaceData')" from inside
  // an event handler, where a try/catch here would not catch it.
  const have = surfaceLabels.get(surfaceSegId)
  if (!have) return
  for (const v of allValues) {
    if (!have.has(v)) continue
    try {
      segmentation.config.visibility.setSegmentIndexVisibility(
        VP.volume3d,
        { segmentationId: surfaceSegId, type: SegRep.Surface },
        v,
        visible.has(v),
      )
    } catch {
      /* representation not attached yet */
    }
  }
  engine?.renderViewports([VP.volume3d])
}

export function setLabelColor(
  segmentationId: string,
  value: number,
  hex: string,
  opacity: number,
) {
  const [r, g, b] = hexToRgb(hex)
  const color = [r, g, b, Math.round(255 * opacity)] as Types.Color
  for (const p of PLANES) {
    try {
      segmentation.config.color.setSegmentIndexColor(VP[p], segmentationId, value, color)
    } catch {
      /* representation not present */
    }
  }
  engine?.renderViewports(PLANES.map((p) => VP[p]))
}

export function resetCameras() {
  const eng = engine
  if (!eng) return
  for (const id of Object.values(VP)) {
    const vp = eng.getViewport(id)
    vp?.resetCamera()
  }
  eng.render()
}

/**
 * Dev-only introspection hook. Cornerstone keeps its state in module scope, so
 * without this there is no way to ask a running page what it actually rendered.
 */
if (import.meta.env.DEV) {
  ;(window as unknown as Record<string, unknown>).__mriv = {
    getEngine,
    VP,
    cache,
    segmentation,
    csToolsUtilities,
    getSegmentationState: () => segmentation.state.getSegmentations(),
    getActors: (id: string) =>
      engine
        ?.getViewport(id)
        ?.getActors()
        ?.map((a) => ({
          uid: a.uid,
          visible: (a.actor as unknown as { getVisibility?: () => boolean })
            ?.getVisibility?.(),
        })) ?? [],
  }
}

export function teardown() {
  for (const id of [TOOL_GROUP_2D, TOOL_GROUP_3D]) {
    try {
      ToolGroupManager.destroyToolGroup(id)
    } catch {
      /* not created */
    }
  }
  engine?.destroy()
  engine = null
}
