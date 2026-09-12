/** Thin typed client for the mriviewer API. Same-origin only. */

export interface DatasetSummary {
  id: number
  key: string
  name: string
  adapter: string
  viewer: string
  n_patients: number
  n_series: number
  n_segmentations: number
}

export interface PatientRow {
  id: number
  external_id: string
  name: string | null
  sex: string | null
  age: string | null
  group_name: string | null
  laterality: string | null
  dataset: string
  viewer: string
  n_studies: number
  n_series: number
  seg_count: number
}

export interface SegmentationRow {
  id: number
  display_name: string | null
  origin: string
  kind: string
  seg_state: string
  seg_error?: string | null
  model_name: string | null
  model_version: string | null
  version_int: number
  association_rule: string | null
  presentValues?: number[]
}

export interface SeriesRow {
  id: number
  series_uid: string | null
  series_number: number | null
  modality: string | null
  description: string | null
  path_rel: string
  rows: number
  cols: number
  n_slices: number | null
  n_instances: number | null
  slice_spacing: number | null
  acquisition_plane: string | null
  anisotropy_ratio: number | null
  volume_state: string
  geometry_warning: string | null
  dtype: string | null
  segmentations: SegmentationRow[]
}

export interface StudyRow {
  id: number
  study_uid: string | null
  study_date: string | null
  description: string | null
  series: SeriesRow[]
}

export interface PatientDetail extends PatientRow {
  extra: Record<string, unknown>
  studies: StudyRow[]
  report: Record<string, string> | null
}

export interface LabelDef {
  value: number
  name: string
  nameZh: string | null
  color: string
  opacity: number
  group: string | null
}

export interface LabelSet {
  key: string
  name: string
  nameEn: string | null
  labels: LabelDef[]
}

export interface VolumeMeta {
  volumeId: string
  seriesId: number
  variant: string
  url: string
  dimensions: [number, number, number]
  spacing: [number, number, number]
  origin: [number, number, number]
  direction: number[]
  dataType: 'Uint8Array' | 'Int16Array' | 'Uint16Array' | 'Float32Array'
  numpyDtype: string
  byteLength: number
  gzipBytes: number
  range: { min: number; max: number }
  plane: string
  anisotropy: number
  warning: string | null
  variants?: string[]
  defaultVariant?: string
  metadata: Record<string, unknown>
}

export interface LabelStat {
  value: number
  voxels: number
  volumeMm3: number
  volumeCm3: number
  centroidLps: [number, number, number]
  extentMm: [number, number, number]
  sliceRange: [number, number]
  intensity?: { mean: number; median: number; p10: number; p90: number }
}

export interface LabelmapMeta {
  segmentationId: string
  segmentationDbId: number
  url: string
  dimensions: [number, number, number]
  spacing: [number, number, number]
  origin: [number, number, number]
  direction: number[]
  presentValues: number[]
  stats: { labels: LabelStat[]; voxelVolumeMm3: number }
  labelSet: LabelSet | null
}

export interface ReportHit {
  external_id: string
  age: string | null
  sex: string | null
  body_part: string | null
  diagnosis: string | null
  excerpt: string
  excerptField: string
}

export interface MeshEntry {
  label_value: number
  n_points: number
  n_tris: number
  bytes: number
  url: string
}

const BASE = '/api/v1'

async function json<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(BASE + path, init)
  if (!res.ok) {
    const detail = await res.text().catch(() => '')
    throw new Error(`${res.status} ${res.statusText} - ${detail.slice(0, 300)}`)
  }
  return (await res.json()) as T
}

/** One cartilage plate's metrics. Field names mirror the server's JSON exactly. */
export interface SubregionMetric {
  moaks: string
  labelZh: string
  thicknessMm: { mean: number | null; max: number | null; p10: number | null
    p50: number | null; p90: number | null; n: number; areaMm2: number }
  sliceSupport: number
  reliableAreaFraction: number
  effectiveResolutionMm: number
  confidence: 'high' | 'medium' | 'low'
}

export interface PlateMetric {
  label: number
  name: string
  volume_mm3: number
  interface_area_mm2: number
  thickness: SubregionMetric['thicknessMm']
  histogram: { binMm: number; edges: number[]; areaMm2: number[] }
  subregions: Record<string, SubregionMetric>
  qc: {
    escapedFraction: number
    rayVsNnMedianRatio: number | null
    reliableAreaFraction: number
    effectiveResolutionMm: number
    isoMm: number
    notes: string[]
  }
}

export interface CompartmentMetric {
  femoralThicknessMm: number | null
  tibialThicknessMm: number | null
  sumThicknessMm: number | null
  volumeMm3: number
}

export interface Morphometry {
  id: number
  segmentationId: number
  algoVersion: string
  paramsHash: string
  params: Record<string, unknown>
  frame: {
    laterality: string
    medial_lateral_consistent: boolean
    notes: string[]
  } | null
  metrics: {
    plates: PlateMetric[]
    compartments: Record<string, CompartmentMetric | number> & {
      medialLateralRatio?: number
      asymmetryPct?: number
    }
  } | null
  qc: {
    plateCount: number
    reliableAreaFraction: number
    effectiveResolutionMm: number
    escapedFraction: number
    medialLateralConsistent: boolean
    frameNotes: string[]
    subregionConfidence: Record<string, number>
    systematicBiasNote: string
  } | null
  state: string
  error: string | null
  durationMs: number | null
  computedAt: string
}

export interface ReferenceSet {
  key: string
  version: number
  updated: string
  measurement_caveat: { zh: string; implications_zh: string[] }
  thresholds: Record<string, unknown>
  metrics: Record<string, {
    label_zh: string; unit: string; direction: string; explanation_zh: string }>
  reference_source: {
    name: string; n: number; sequence: string; measure: string
    citation: string; applicability: string; applicability_note_zh: string
  }
  subregions: Record<string, {
    label_zh: string; male?: [number, number]; female?: [number, number] }>
  observed_deviation_note_zh: string
}

export interface AiReport {
  id: number
  segmentationId: number
  version: number
  provider: string | null
  model: string | null
  findings: string | null
  quant: string | null
  impression: string | null
  advice: string | null
  /** ok | fallback | rejected_by_guardrail | failed */
  status: string
  error: string | null
  guardrail: {
    ok: boolean; violations: string[]
    unmatchedNumbers: number[]; bannedHits: string[]
  } | null
  /** The exact de-identified payload that was transmitted, for audit. */
  input: Record<string, unknown> | null
  latencyMs: number | null
  reviewState: string
  reviewedBy: string | null
  reviewedAt: string | null
  reviewedText: string | null
  createdAt: string
  disclaimer: string
}

export interface AiSettings {
  enabled: boolean
  allowEgress: boolean
  baseUrl: string
  model: string
  timeoutS: number
  /** The key itself is never returned - only whether one is set. */
  keyPresent: boolean
  patientContext: string[]
  /** Which fields the database overrides, as opposed to the config file. */
  overridden: string[]
}

export interface AiReportState {
  exists: boolean
  status?: string
  reviewState?: string
  createdAt?: string
  generation?: number
}

export interface AiStatus {
  enabled: boolean
  allowEgress: boolean
  baseUrl: string
  model: string
  keyPresent: boolean
  reachable: boolean
  modelPresent: boolean | null
  detail: string | null
}

export const api = {
  datasets: () => json<DatasetSummary[]>('/datasets'),

  patients: (params: {
    dataset?: string
    q?: string
    report_q?: string
    has_segmentation?: boolean
    limit?: number
    offset?: number
  }) => {
    const sp = new URLSearchParams()
    for (const [k, v] of Object.entries(params)) {
      if (v !== undefined && v !== '' && v !== false) sp.set(k, String(v))
    }
    return json<{ total: number; items: PatientRow[] }>(`/patients?${sp}`)
  },

  patient: (id: number) => json<PatientDetail>(`/patients/${id}`),
  series: (id: number) => json<SeriesRow>(`/series/${id}`),
  volumeMeta: (id: number, variant = 'native') =>
    json<VolumeMeta>(`/series/${id}/volume.json?variant=${variant}`),
  labelmapMeta: (id: number) => json<LabelmapMeta>(`/segmentations/${id}/labelmap.json`),
  meshes: (id: number) =>
    json<{ segmentationId: number; state: string; meshes: MeshEntry[] }>(
      `/segmentations/${id}/meshes`,
    ),
  morphometry: (id: number) => json<Morphometry>(`/segmentations/${id}/morphometry`),
  buildMorphometry: (id: number, force = false) =>
    json<Morphometry>(
      `/segmentations/${id}/morphometry/build${force ? '?force=true' : ''}`,
      { method: 'POST' },
    ),
  references: (key: string) => json<ReferenceSet>(`/references/${key}`),
  aiStatus: () => json<AiStatus>('/ai/status'),
  aiReport: (id: number) => json<AiReport>(`/segmentations/${id}/ai-report`),
  /** Absence is `exists: false`, not a 404 - "no report yet" is not an error. */
  aiReportState: (id: number) =>
    json<AiReportState>(`/segmentations/${id}/ai-report/state`),
  aiSettings: () => json<AiSettings>('/ai/settings'),
  saveAiSettings: (body: Partial<{
    enabled: boolean; allowEgress: boolean; baseUrl: string
    model: string; timeoutS: number; apiKey: string; updatedBy: string
  }>) => json<AiSettings>('/ai/settings', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }),
  probeAi: (body: Partial<{
    baseUrl: string; model: string; apiKey: string; allowEgress: boolean
  }>) => json<{
    ok: boolean; models: string[]; modelPresent?: boolean
    detail: string | null; egressBlocked?: boolean
  }>('/ai/probe', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }),
  buildAiReport: (id: number) =>
    json<AiReport>(`/segmentations/${id}/ai-report`, { method: 'POST' }),
  reviewAiReport: (reportId: number, body: {
    reviewState: string; reviewedBy?: string; reviewedText?: string
  }) => json<AiReport>(`/ai-reports/${reportId}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }),

  buildMeshes: (id: number) =>
    json<{ status: string; built: number[] }>(`/segmentations/${id}/meshes/build`, {
      method: 'POST',
    }),
  searchReports: (q: string, dataset?: string) => {
    const sp = new URLSearchParams({ q })
    if (dataset) sp.set('dataset', dataset)
    return json<{ query: string; total: number; items: ReportHit[] }>(`/reports/search?${sp}`)
  },
  labelsets: () => json<LabelSet[]>('/labelsets'),
  patchLabel: (key: string, value: number, body: Partial<LabelDef>) =>
    json<LabelSet>(`/labelsets/${key}/labels/${value}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }),

  /**
   * Raw binary fetch, optionally reporting progress.
   *
   * The payload is stored gzipped and decoded by the browser, so
   * `Content-Length` is the *compressed* size while the stream we read is the
   * decompressed one - using the header for a percentage would overshoot
   * wildly. The caller passes `expectedBytes` from the descriptor instead,
   * which is the true decompressed length.
   */
  async binary(
    url: string,
    opts: {
      signal?: AbortSignal
      expectedBytes?: number
      onProgress?: (received: number, total: number) => void
    } = {},
  ): Promise<ArrayBuffer> {
    const { signal, expectedBytes, onProgress } = opts
    const res = await fetch(url, { signal })
    if (!res.ok) throw new Error(`${res.status} fetching ${url}`)
    if (!onProgress || !res.body) return res.arrayBuffer()

    const total = expectedBytes ?? Number(res.headers.get('content-length') ?? 0)
    const reader = res.body.getReader()
    const chunks: Uint8Array[] = []
    let received = 0
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      chunks.push(value)
      received += value.length
      onProgress(received, total)
    }
    const out = new Uint8Array(received)
    let at = 0
    for (const c of chunks) {
      out.set(c, at)
      at += c.length
    }
    onProgress(received, received)
    return out.buffer
  },
}

export function hexToRgb(hex: string): [number, number, number] {
  const m = /^#?([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(hex.trim())
  if (!m) return [255, 0, 0]
  return [parseInt(m[1], 16), parseInt(m[2], 16), parseInt(m[3], 16)]
}
