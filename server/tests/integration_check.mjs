/**
 * End-to-end check of every request the viewer makes, run from Node against a
 * live server. Verifies payload sizes, geometry agreement between image and
 * labelmap, and that the MRIVMESH binaries decode to sane geometry.
 *
 * This covers everything except the WebGL draw itself.
 *
 *   node tests/integration_check.mjs [http://127.0.0.1:8080]
 */
const BASE = (process.argv[2] || 'http://127.0.0.1:8080') + '/api/v1'

let pass = 0
let fail = 0
const problems = []

function check(name, cond, detail = '') {
  if (cond) {
    pass++
  } else {
    fail++
    problems.push(`${name}${detail ? ' - ' + detail : ''}`)
  }
}

async function json(p) {
  const r = await fetch(BASE + p)
  if (!r.ok) throw new Error(`${r.status} ${p}`)
  return r.json()
}

/**
 * Like json(), but a 422 means the source bytes on disk are unreadable rather
 * than the server being broken. The drive really does hold one corrupt file
 * (a COPD lung image.nii.gz with a good gzip header and a bad body); that is a
 * data defect to report, not a reason to abort the whole run.
 */
async function jsonOrUnreadable(p) {
  const r = await fetch(BASE + p)
  if (r.status === 422) return { unreadable: (await r.json()).detail ?? '422' }
  if (!r.ok) throw new Error(`${r.status} ${p}`)
  return r.json()
}

async function bin(url) {
  const r = await fetch(url.startsWith('http') ? url : new URL(url, BASE).origin + url)
  if (!r.ok) throw new Error(`${r.status} ${url}`)
  const buf = Buffer.from(await r.arrayBuffer())
  return { buf, encoding: r.headers.get('content-encoding'), wire: r.headers.get('content-length') }
}

function decodeMesh(buf) {
  const magic = buf.subarray(0, 8).toString('latin1')
  if (magic !== 'MRIVMESH') throw new Error('bad magic ' + JSON.stringify(magic))
  const version = buf.readUInt32LE(8)
  const nPoints = buf.readUInt32LE(12)
  const nTris = buf.readUInt32LE(16)
  const bounds = []
  for (let i = 0; i < 6; i++) bounds.push(buf.readFloatLE(20 + i * 4))
  const HEAD = 8 + 12 + 24
  const pointsEnd = HEAD + nPoints * 12
  const expected = pointsEnd + nTris * 12
  return { version, nPoints, nTris, bounds, expected, actual: buf.length }
}

const TYPE_BYTES = { Uint8Array: 1, Int16Array: 2, Uint16Array: 2, Float32Array: 4 }

const t0 = Date.now()
console.log('checking', BASE)

const datasets = await json('/datasets')
check('datasets non-empty', datasets.length > 0)
console.log(
  `  datasets: ${datasets.map((d) => `${d.key}(${d.n_patients}p/${d.n_series}s/${d.n_segmentations}seg)`).join(', ')}`,
)

const { items: patients, total } = await json('/patients?limit=500')
check('patient list', patients.length > 0, `total=${total}`)

// Ask the server for annotated patients PER DATASET. A single global query
// cannot work: the list is id-ordered and COPD alone has 523 annotated cases,
// so ds0826's 24 knee cases - the ones this platform exists for - fall outside
// any reasonable limit. Sampling a few from each dataset also keeps the run
// bounded, since a COPD volume is a 207 MB gzipped NIfTI that takes minutes to
// materialise on first touch.
const PER_DATASET = Number(process.env.MRIV_PER_DATASET || 8)
const wanted = process.env.MRIV_DATASET
  ? [process.env.MRIV_DATASET]
  : datasets.map((d) => d.key)

const annotated = []
let annotatedTotal = 0
for (const key of wanted) {
  const r = await json(
    `/patients?has_segmentation=true&limit=${PER_DATASET}&dataset=${key}`)
  annotatedTotal += r.total
  annotated.push(...r.items)
}
check('annotated patients present', annotated.length > 0, `total=${annotatedTotal}`)
console.log(`  patients: ${total} indexed, ${annotatedTotal} with a segmentation`)
console.log(`  sampling ${annotated.length} of them, <=${PER_DATASET} per dataset`)

let checkedSeries = 0
let checkedSegs = 0
let totalWire = 0
let totalMeshBytes = 0
const unreadable = []

for (const p of annotated) {
  const detail = await json(`/patients/${p.id}`)
  const series = detail.studies.flatMap((s) => s.series)
  check(`patient ${p.external_id} has series`, series.length > 0)

  for (const s of series) {
    const meta = await jsonOrUnreadable(`/series/${s.id}/volume.json`)
    if (meta.unreadable) {
      unreadable.push(`series ${s.id}: ${meta.unreadable}`)
      continue
    }
    const [cols, rows, slices] = meta.dimensions
    const itemBytes = TYPE_BYTES[meta.dataType]
    check(`series ${s.id} dataType known`, !!itemBytes, meta.dataType)
    check(
      `series ${s.id} byteLength matches dims`,
      meta.byteLength === cols * rows * slices * itemBytes,
      `${meta.byteLength} vs ${cols * rows * slices * itemBytes}`,
    )
    check(`series ${s.id} rows/cols agree with metadata`,
      meta.metadata.Rows === rows && meta.metadata.Columns === cols)
    check(`series ${s.id} has a window`, Array.isArray(meta.metadata.voiLut) &&
      meta.metadata.voiLut[0].windowWidth > 0)

    const v = await bin(meta.url)
    check(`series ${s.id} volume gzipped`, v.encoding === 'gzip', String(v.encoding))
    check(
      `series ${s.id} volume decodes to byteLength`,
      v.buf.length === meta.byteLength,
      `${v.buf.length} vs ${meta.byteLength}`,
    )
    totalWire += Number(v.wire || 0)
    checkedSeries++

    for (const g of s.segmentations) {
      const lm = await json(`/segmentations/${g.id}/labelmap.json`)
      check(
        `seg ${g.id} dims match series`,
        JSON.stringify(lm.dimensions) === JSON.stringify(meta.dimensions),
        `${lm.dimensions} vs ${meta.dimensions}`,
      )
      check(
        `seg ${g.id} spacing matches series`,
        lm.spacing.every((x, i) => Math.abs(x - meta.spacing[i]) < 1e-6),
      )
      check(
        `seg ${g.id} origin matches series`,
        lm.origin.every((x, i) => Math.abs(x - meta.origin[i]) < 1e-6),
        `${lm.origin} vs ${meta.origin}`,
      )
      check(`seg ${g.id} has labels`, lm.presentValues.length > 0)
      check(`seg ${g.id} has a label set`, !!lm.labelSet, 'labelSet missing')
      check(
        `seg ${g.id} every present value is named`,
        lm.presentValues.every((v) => lm.labelSet?.labels.some((l) => l.value === v)),
      )
      check(`seg ${g.id} stats cover every label`,
        lm.presentValues.every((v) => lm.stats.labels.some((l) => l.value === v)))

      const lb = await bin(lm.url)
      check(`seg ${g.id} labelmap gzipped`, lb.encoding === 'gzip')
      check(
        `seg ${g.id} labelmap size`,
        lb.buf.length === cols * rows * slices,
        `${lb.buf.length} vs ${cols * rows * slices}`,
      )
      // Every byte in the labelmap must be a value the descriptor declared.
      const seen = new Set(lb.buf)
      const declared = new Set([0, ...lm.presentValues])
      check(
        `seg ${g.id} labelmap contains only declared values`,
        [...seen].every((v) => declared.has(v)),
        `${[...seen].join(',')} vs ${[...declared].join(',')}`,
      )
      totalWire += Number(lb.wire || 0)

      // Meshes are built lazily. "pending" means nobody has opened this
      // segmentation in 3D yet, which is a legitimate state - a 768x768x320
      // COPD lung mask should not be surfaced just because a test walked past
      // it. The contract is that "ready" implies meshes exist, and that the
      // knee dataset this platform exists for actually has them.
      const ms = await json(`/segmentations/${g.id}/meshes`)
      const meshes = ms.meshes
      check(
        `seg ${g.id} mesh state is consistent`,
        ms.state !== 'ready' || meshes.length > 0,
        `state=${ms.state} meshes=${meshes.length}`,
      )
      if (p.dataset === 'ds0826') {
        check(`seg ${g.id} (knee) has meshes`, meshes.length > 0,
          `state=${ms.state}`)
      }
      if (!meshes.length) continue
      for (const m of meshes) {
        const mb = await bin(m.url)
        const d = decodeMesh(mb.buf)
        check(`mesh ${g.id}/${m.label_value} version`, d.version === 1)
        check(
          `mesh ${g.id}/${m.label_value} length`,
          d.actual === d.expected,
          `${d.actual} vs ${d.expected}`,
        )
        check(
          `mesh ${g.id}/${m.label_value} counts match db`,
          d.nPoints === m.n_points && d.nTris === m.n_tris,
        )
        check(`mesh ${g.id}/${m.label_value} finite bounds`,
          d.bounds.every(Number.isFinite))
        // The mesh must sit inside the volume it came from, with a little slack.
        const st = lm.stats.labels.find((l) => l.value === m.label_value)
        if (st) {
          const cx = (d.bounds[0] + d.bounds[3]) / 2
          const cy = (d.bounds[1] + d.bounds[4]) / 2
          const cz = (d.bounds[2] + d.bounds[5]) / 2
          const dist = Math.hypot(
            cx - st.centroidLps[0], cy - st.centroidLps[1], cz - st.centroidLps[2],
          )
          check(
            `mesh ${g.id}/${m.label_value} sits on its label`,
            dist < 25,
            `bbox centre ${dist.toFixed(1)} mm from label centroid`,
          )
        }
        totalMeshBytes += mb.buf.length
      }
      checkedSegs++
    }
  }
}

const labelsets = await json('/labelsets')
check('label sets present', labelsets.length > 0)

console.log(`  checked ${checkedSeries} series, ${checkedSegs} segmentations`)
if (unreadable.length) {
  console.log(`  ${unreadable.length} series unreadable on disk (data defect, not a server fault):`)
  for (const u of unreadable) console.log('    ' + u)
}
console.log(`  transferred ${(totalWire / 1e6).toFixed(1)} MB on the wire`)
console.log(`  meshes ${(totalMeshBytes / 1e6).toFixed(1)} MB decompressed`)
console.log(`\n${pass} passed, ${fail} failed in ${((Date.now() - t0) / 1000).toFixed(1)}s`)
if (fail) {
  console.log('\nfailures:')
  for (const p of problems.slice(0, 40)) console.log('  ' + p)
  process.exit(1)
}
