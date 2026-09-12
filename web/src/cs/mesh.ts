/**
 * MRIVMESH decoder - the server's per-label surface format.
 *
 * Deliberately not glTF/Draco: Cornerstone's SurfaceData is already just a flat
 * point array plus a flat triangle index array, so a container format would buy
 * nothing and a Draco decoder would put WASM back into an air-gapped bundle.
 *
 *   magic "MRIVMESH" | u32 version | u32 nPoints | u32 nTris | f32 bbox[6]
 *   f32 points[nPoints * 3]   (LPS mm)
 *   u32 polys[nTris * 3]
 */
const MAGIC = 'MRIVMESH'
const HEADER_BYTES = 8 + 4 * 3 + 4 * 6

export interface DecodedMesh {
  points: Float32Array
  polys: Uint32Array
  nPoints: number
  nTris: number
  bounds: [number, number, number, number, number, number]
}

export function decodeMesh(buf: ArrayBuffer): DecodedMesh {
  const magic = new TextDecoder().decode(new Uint8Array(buf, 0, 8))
  if (magic !== MAGIC) throw new Error(`not a MRIVMESH buffer (got ${JSON.stringify(magic)})`)

  const dv = new DataView(buf)
  const version = dv.getUint32(8, true)
  if (version !== 1) throw new Error(`unsupported MRIVMESH version ${version}`)
  const nPoints = dv.getUint32(12, true)
  const nTris = dv.getUint32(16, true)
  const bounds: number[] = []
  for (let i = 0; i < 6; i++) bounds.push(dv.getFloat32(20 + i * 4, true))

  const pointsOffset = HEADER_BYTES
  const polysOffset = pointsOffset + nPoints * 12
  const expected = polysOffset + nTris * 12
  if (buf.byteLength < expected) {
    throw new Error(`MRIVMESH truncated: ${buf.byteLength} bytes, expected ${expected}`)
  }

  const points = new Float32Array(buf.slice(pointsOffset, polysOffset))

  // Cornerstone/VTK want the cell array format: [3, a, b, c, 3, d, e, f, ...]
  const tri = new Uint32Array(buf, polysOffset, nTris * 3)
  const polys = new Uint32Array(nTris * 4)
  for (let t = 0; t < nTris; t++) {
    polys[t * 4] = 3
    polys[t * 4 + 1] = tri[t * 3]
    polys[t * 4 + 2] = tri[t * 3 + 1]
    polys[t * 4 + 3] = tri[t * 3 + 2]
  }

  return {
    points,
    polys,
    nPoints,
    nTris,
    bounds: bounds as [number, number, number, number, number, number],
  }
}
