/**
 * Plate raster (`mriv-plate-raster/1`) - the server's flattened cartilage map.
 *
 * Data, not a picture: four base64 layers over one pixel grid (thickness in
 * µm, pixel class, subregion index, lesion index) plus the analytic overlay
 * (boundaries, label anchors) in the same pixel coordinates. The browser
 * paints it, so the colours follow the report's palette and the text stays
 * vector on paper. See server/src/mriviewer/morph/figures.py.
 */

export const CLS = { outside: 0, measured: 1, unreliable: 2, filled: 3, denuded: 4 } as const

export interface PlateRaster {
  schema: string
  label: number
  name: string
  kind: 'femur' | 'tibia' | 'patella'
  medial: boolean | null
  pxMm: number
  width: number
  height: number
  originMm: [number, number]
  axes: { x?: { labelZh: string; unit: string }; y?: { labelZh: string; unit: string } }
  frame2d: Record<string, unknown>
  layers: { thicknessUm: string; cls: string; code: string; lesion: string }
  codes: string[]
  labels: Array<{ code: string; labelZh: string; px: [number, number] }>
  boundaries: Array<
    | { type: 'line'; a: [number, number]; b: [number, number] }
    | { type: 'ellipse'; c: [number, number]; r: [number, number] }
  >
  lesions: Array<{ index: number; code: string; grade: string; areaMm2: number; kind: 'deficit' | 'denuded' }>
  scale: { minMm: number; maxMm: number }
  stats: { reliableAreaFraction: number; meanMm: number | null; measuredPixels: number; platePixels: number }
}

export interface DecodedRaster extends PlateRaster {
  thicknessUm: Uint16Array
  cls: Uint8Array
  code: Uint8Array
  lesion: Uint8Array
}

function bytes(b64: string): Uint8Array {
  const bin = atob(b64)
  const out = new Uint8Array(bin.length)
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i)
  return out
}

const LITTLE_ENDIAN = new Uint8Array(new Uint16Array([1]).buffer)[0] === 1

function u16le(b: Uint8Array): Uint16Array {
  if (LITTLE_ENDIAN && b.byteOffset % 2 === 0) return new Uint16Array(b.buffer, b.byteOffset, b.length / 2)
  const dv = new DataView(b.buffer, b.byteOffset, b.length)
  const out = new Uint16Array(b.length / 2)
  for (let i = 0; i < out.length; i++) out[i] = dv.getUint16(i * 2, true)
  return out
}

export function decodePlateRaster(r: PlateRaster): DecodedRaster {
  const n = r.width * r.height
  const th = u16le(bytes(r.layers.thicknessUm))
  const cls = bytes(r.layers.cls)
  const code = bytes(r.layers.code)
  const lesion = bytes(r.layers.lesion)
  if (th.length !== n || cls.length !== n || code.length !== n || lesion.length !== n) {
    throw new Error(`plate raster ${r.label}: layer size mismatch`)
  }
  return { ...r, thicknessUm: th, cls, code, lesion }
}

/** Subregion code of a pixel, or '' outside the plate. */
export function codeAt(r: DecodedRaster, x: number, y: number): string {
  const i = r.code[y * r.width + x]
  return i > 0 ? r.codes[i - 1] ?? '' : ''
}
