/**
 * The whole disk-to-browser bridge, client side.
 *
 * A volume id looks like `mriv:series:<n>:<variant>` or `mriv:seg:<n>`. We
 * fetch one JSON descriptor plus one binary blob (stored gzipped on the server,
 * decompressed natively by the browser) and hand Cornerstone a local volume.
 * No DICOMweb, no per-slice requests.
 *
 * Note on `createLocalVolume`: it already splits the buffer into per-slice
 * images, puts each one in the image cache, and caches the volume itself. So we
 * must NOT then call `volume.load()` - that is the streaming-volume path, and
 * it tries to pull our synthetic `mriv:..._slice_N` image ids through the
 * *image* loader registry, failing with "No image loader found for scheme
 * 'mriv'". Everything is in cache by the time createLocalVolume returns.
 */
import { cache, imageLoader, volumeLoader } from '@cornerstonejs/core'
import type { Types } from '@cornerstonejs/core'

import { api, type LabelmapMeta, type VolumeMeta } from '../api'

/** What the UI is waiting for, and how far along it is. */
export interface LoadPhase {
  stage: 'volume' | 'labelmap' | 'mesh' | 'render' | 'idle'
  received: number
  total: number
  detail?: string
}

export type ProgressFn = (p: LoadPhase) => void

let reportProgress: ProgressFn = () => {}

export function setProgressReporter(fn: ProgressFn): void {
  reportProgress = fn
}

export function reportStage(p: LoadPhase): void {
  reportProgress(p)
}

export const MRIV_SCHEME = 'mriv'

const TYPED: Record<string, new (b: ArrayBuffer) => Types.PixelDataTypedArray> = {
  Uint8Array,
  Int8Array: Int8Array as never,
  Uint16Array,
  Int16Array,
  Float32Array,
}

export function seriesVolumeId(seriesId: number, variant = 'native'): string {
  return `${MRIV_SCHEME}:series:${seriesId}:${variant}`
}

export function segVolumeId(segId: number): string {
  return `${MRIV_SCHEME}:seg:${segId}`
}

/** Descriptors are kept so the loader and the UI agree on geometry. */
const volumeMetaCache = new Map<string, VolumeMeta>()
const labelmapMetaCache = new Map<string, LabelmapMeta>()
const inFlight = new Map<string, Promise<Types.IImageVolume>>()

export function getVolumeMeta(volumeId: string): VolumeMeta | undefined {
  return volumeMetaCache.get(volumeId)
}

export function getLabelmapMeta(volumeId: string): LabelmapMeta | undefined {
  return labelmapMetaCache.get(volumeId)
}

function toMat3(d: number[]): Types.Mat3 {
  return d as unknown as Types.Mat3
}

/**
 * Frame of reference to stamp on a labelmap, keyed by its volume id.
 *
 * Cornerstone refuses to attach a labelmap to a volume viewport whose
 * FrameOfReferenceUID differs - and it only `console.warn`s, so the overlay
 * just silently never appears. The labelmap must therefore inherit the exact
 * UID of the image volume it was drawn on.
 */
const segFrameOfReference = new Map<string, string>()

export function setSegFrameOfReference(volumeId: string, uid: string): void {
  segFrameOfReference.set(volumeId, uid)
}

async function buildVolume(volumeId: string): Promise<Types.IImageVolume> {
  const [, kind, idStr, variant = 'native'] = volumeId.split(':')
  const id = Number(idStr)

  if (kind === 'series') {
    const meta = await api.volumeMeta(id, variant)
    volumeMetaCache.set(volumeId, meta)
    const buf = await api.binary(meta.url, {
      expectedBytes: meta.byteLength,
      onProgress: (received, total) =>
        reportProgress({ stage: 'volume', received, total, detail: '影像体数据' }),
    })
    const Ctor = TYPED[meta.dataType]
    if (!Ctor) throw new Error(`unsupported volume dataType ${meta.dataType}`)
    const scalarData = new Ctor(buf)
    const expected = meta.dimensions[0] * meta.dimensions[1] * meta.dimensions[2]
    const got = (scalarData as unknown as { length: number }).length
    if (got !== expected) {
      throw new Error(`volume payload is ${got} voxels, descriptor says ${expected}`)
    }
    return volumeLoader.createLocalVolume(volumeId, {
      metadata: meta.metadata as unknown as Types.Metadata,
      dimensions: meta.dimensions as Types.Point3,
      spacing: meta.spacing as Types.Point3,
      origin: meta.origin as Types.Point3,
      direction: toMat3(meta.direction),
      scalarData,
      targetBuffer: { type: meta.dataType as Types.PixelDataTypedArrayString },
    })
  }

  if (kind === 'seg') {
    const meta = await api.labelmapMeta(id)
    labelmapMetaCache.set(volumeId, meta)
    const buf = await api.binary(meta.url, {
      expectedBytes: meta.dimensions[0] * meta.dimensions[1] * meta.dimensions[2],
      onProgress: (received, total) =>
        reportProgress({ stage: 'labelmap', received, total, detail: '分割标注' }),
    })
    const scalarData = new Uint8Array(buf)
    const expected = meta.dimensions[0] * meta.dimensions[1] * meta.dimensions[2]
    if (scalarData.length !== expected) {
      throw new Error(`labelmap is ${scalarData.length} voxels, descriptor says ${expected}`)
    }
    return volumeLoader.createLocalVolume(volumeId, {
      metadata: {
        BitsAllocated: 8,
        BitsStored: 8,
        HighBit: 7,
        SamplesPerPixel: 1,
        PixelRepresentation: 0,
        PhotometricInterpretation: 'MONOCHROME2',
        Modality: 'SEG',
        Rows: meta.dimensions[1],
        Columns: meta.dimensions[0],
        PixelSpacing: [meta.spacing[1], meta.spacing[0]],
        FrameOfReferenceUID: segFrameOfReference.get(volumeId) ?? 'mriv',
        voiLut: [{ windowCenter: 128, windowWidth: 255 }],
        VOILUTFunction: 'LINEAR',
      } as unknown as Types.Metadata,
      dimensions: meta.dimensions as Types.Point3,
      spacing: meta.spacing as Types.Point3,
      origin: meta.origin as Types.Point3,
      direction: toMat3(meta.direction),
      scalarData,
      targetBuffer: { type: 'Uint8Array' },
    })
  }

  throw new Error(`unknown volume id ${volumeId}`)
}

/**
 * Get a volume, fetching it at most once. Safe to call concurrently for the
 * same id - the in-flight promise is shared, so a volume is never downloaded
 * twice (which for a chest CT would be 47 MB of waste).
 */
export function ensureVolume(volumeId: string): Promise<Types.IImageVolume> {
  const cached = cache.getVolume(volumeId)
  if (cached) return Promise.resolve(cached)
  const pending = inFlight.get(volumeId)
  if (pending) return pending
  const p = buildVolume(volumeId).finally(() => inFlight.delete(volumeId))
  inFlight.set(volumeId, p)
  return p
}

/**
 * Registered so anything inside Cornerstone that resolves a volume by id
 * (rather than going through ensureVolume) still works.
 */
export function mrivVolumeLoader(volumeId: string) {
  return { promise: ensureVolume(volumeId) }
}

/**
 * Image loader for the same scheme.
 *
 * createLocalVolume() names its per-slice images `<volumeId>_slice_<n>` and
 * puts them straight into the image cache, but parts of the render path still
 * resolve them through the image *loader* registry - and a scheme with no
 * registered loader throws rather than falling back to the cache. So serve them
 * from the cache; there is never anything to fetch.
 */
export function mrivImageLoader(imageId: string) {
  const image = cache.getImage(imageId)
  if (image) return { promise: Promise.resolve(image) }
  return {
    promise: Promise.reject(
      new Error(`mriv image ${imageId} is not in the cache; its volume was never built`),
    ),
  }
}
