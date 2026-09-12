/**
 * 软骨定量指标看板  #/metrics/<segId>
 *
 * Design decisions worth knowing before editing:
 *
 * - **The QC panel comes first, above the numbers.** On 3 mm slices the caveats
 *   are not a footnote; a reader who meets them after the millimetres has
 *   already formed an impression.
 * - **The main chart is a dot plot with reference ranges, not a bar chart.**
 *   Bars encode magnitude from zero; what a clinician reads here is position
 *   within a normal range, which is the form a lab report already uses.
 * - **Reference bands are hatched.** The Framingham cohort used near-isotropic
 *   FLASH; ours is 3 mm PD-FSE. The texture says "magnitude reference, not
 *   diagnostic threshold" in a way a legend line cannot.
 * - **Confidence is never colour alone** - every badge carries a glyph and a
 *   word, because the status hues deliberately fail categorical CVD separation.
 */
import { useEffect, useMemo, useState } from 'react'

import { api, type Morphometry, type PlateMetric, type ReferenceSet } from '../api'
import ReportCard from './ReportCard'

const REF_KEY = 'knee_cartilage_reference_v1'

// Validated against this app's surface #0d1117 with the dataviz palette
// checker: 3 categorical slots, all-pairs, dark mode - all checks pass.
const C_VALUE = '#3987e5'      // the patient's measurement
const C_BAND = '#8b97a8'       // reference range: recessive, never a series
const C_GRID = '#2a3341'

const CONFIDENCE = {
  high: { glyph: '●', word: '可信', color: '#0ca30c' },
  medium: { glyph: '◐', word: '一般', color: '#fab219' },
  low: { glyph: '○', word: '存疑', color: '#ec835a' },
} as const

/** Plate display order: medial femur, lateral femur, medial tibia, lateral tibia, patella. */
const PLATE_ORDER = [4, 5, 6, 7, 8]

function useHashSegId(): number | null {
  const [id, setId] = useState<number | null>(parse())
  useEffect(() => {
    const on = () => setId(parse())
    window.addEventListener('hashchange', on)
    return () => window.removeEventListener('hashchange', on)
  }, [])
  return id
  function parse(): number | null {
    const m = /^#\/metrics\/(\d+)/.exec(window.location.hash)
    return m ? Number(m[1]) : null
  }
}

export default function MetricsPage() {
  const segId = useHashSegId()
  const [morph, setMorph] = useState<Morphometry | null>(null)
  const [refs, setRefs] = useState<ReferenceSet | null>(null)
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(true)
  const [sex, setSex] = useState<'male' | 'female'>('male')

  useEffect(() => {
    if (segId == null) return
    let cancelled = false
    setBusy(true)
    setErr('')
    ;(async () => {
      try {
        const r = await api.references(REF_KEY)
        if (!cancelled) setRefs(r)
        // Compute on demand: a metrics link that only works after someone has
        // pressed a button elsewhere is a link that will be reported as broken.
        let m: Morphometry
        try {
          m = await api.morphometry(segId)
        } catch {
          m = await api.buildMorphometry(segId)
        }
        if (!cancelled) setMorph(m)
      } catch (e) {
        if (!cancelled) setErr(String(e))
      } finally {
        if (!cancelled) setBusy(false)
      }
    })()
    return () => {
      cancelled = true
    }
  }, [segId])

  if (segId == null) return <Shell><p className="loading">链接缺少分割 ID</p></Shell>
  if (busy) return <Shell><p className="loading">正在计算形态学指标，约需 5-10 秒…</p></Shell>
  if (err) return <Shell><p className="err">{err}</p></Shell>
  if (!morph) return <Shell><p className="loading">没有数据</p></Shell>

  if (morph.state !== 'ready') {
    return (
      <Shell>
        <div className="mx-fail">
          <h2>无法生成指标</h2>
          <p>{explainFailure(morph.error)}</p>
          <p className="mx-raw">{morph.error}</p>
          <p className="mx-note">
            这是刻意的硬失败。内外侧标签疑似互换时，所有亚区指标都会镜像，
            而镜像后的报告看起来完全合理——宁可不出，也不能出错的。
          </p>
        </div>
      </Shell>
    )
  }

  const plates = [...(morph.metrics?.plates ?? [])].sort(
    (a, b) => PLATE_ORDER.indexOf(a.label) - PLATE_ORDER.indexOf(b.label),
  )
  const comp = morph.metrics?.compartments

  return (
    <Shell>
      <Banner morph={morph} sex={sex} onSex={setSex} />
      {refs && morph.qc && <QcPanel qc={morph.qc} refs={refs} />}
      <ReportCard segId={segId} />
      {comp && <Compartments comp={comp} />}
      {refs && <SubregionChart plates={plates} refs={refs} sex={sex} />}
      <Histograms plates={plates} />
      {refs && <MetricGlossary refs={refs} />}
      {refs && <DataTable plates={plates} refs={refs} sex={sex} />}
    </Shell>
  )
}

function Shell({ children }: { children: React.ReactNode }) {
  return (
    <div className="mx-root">
      <header className="mx-head">
        <a className="mx-back" href="#/">← 返回阅片</a>
        <h1>软骨定量指标</h1>
        <span className="mx-disclaim">算法自动生成，非诊断结论，须由医师复核</span>
      </header>
      <main className="mx-main">{children}</main>
    </div>
  )
}

function Banner({ morph, sex, onSex }: {
  morph: Morphometry
  sex: 'male' | 'female'
  onSex: (s: 'male' | 'female') => void
}) {
  return (
    <section className="mx-banner">
      <div>
        <span className="mx-kv">侧别</span>
        <b>{morph.frame?.laterality === 'L' ? '左膝' : '右膝'}</b>
      </div>
      <div>
        <span className="mx-kv">算法版本</span>
        <b>{morph.algoVersion}</b>
      </div>
      <div>
        <span className="mx-kv">计算耗时</span>
        <b>{((morph.durationMs ?? 0) / 1000).toFixed(1)} s</b>
      </div>
      <div className="mx-sex">
        <span className="mx-kv">参考值性别</span>
        {(['male', 'female'] as const).map((s) => (
          <button key={s} className={sex === s ? 'active' : ''} onClick={() => onSex(s)}>
            {s === 'male' ? '男' : '女'}
          </button>
        ))}
      </div>
    </section>
  )
}

/** Caveats first. See the file header for why this is not at the bottom. */
function QcPanel({ qc, refs }: { qc: NonNullable<Morphometry['qc']>; refs: ReferenceSet }) {
  const conf = qc.subregionConfidence ?? {}
  return (
    <section className="mx-card mx-qc">
      <h2>测量质量与适用范围</h2>
      <p className="mx-caveat">{refs.measurement_caveat.zh}</p>
      <ul className="mx-implications">
        {refs.measurement_caveat.implications_zh.map((t) => <li key={t}>{t}</li>)}
      </ul>
      <div className="mx-qc-grid">
        <Stat label="可靠区面积占比"
          value={`${(qc.reliableAreaFraction * 100).toFixed(0)}%`}
          hint="有效分辨率 ≤ 0.6 mm 的骨-软骨界面面积比例" />
        <Stat label="平均有效分辨率" value={`${qc.effectiveResolutionMm.toFixed(2)} mm`}
          hint="测厚方向上原始数据的采样间隔" />
        <Stat label="射线逃逸比例" value={`${(qc.escapedFraction * 100).toFixed(1)}%`}
          hint="未能在 9 mm 内找到软骨外表面的顶点比例" />
        <Stat label="内外侧一致性" value={qc.medialLateralConsistent ? '通过' : '不通过'}
          hint="标签几何推出的侧别与影像位置是否吻合" />
      </div>
      <div className="mx-conf-row">
        <span className="mx-kv">亚区可信度分布</span>
        {(['high', 'medium', 'low'] as const).map((k) => (
          <span key={k} className="mx-badge" style={{ color: CONFIDENCE[k].color }}>
            {CONFIDENCE[k].glyph} {CONFIDENCE[k].word} {conf[k] ?? 0}
          </span>
        ))}
      </div>
    </section>
  )
}

function Stat({ label, value, hint }: { label: string; value: string; hint: string }) {
  return (
    <div className="mx-stat" title={hint}>
      <div className="mx-stat-v">{value}</div>
      <div className="mx-stat-l">{label}</div>
    </div>
  )
}

function Compartments({ comp }: { comp: Record<string, any> }) {
  const asym = comp.asymmetryPct as number | undefined
  return (
    <section className="mx-card">
      <h2>间室汇总</h2>
      <p className="mx-sub">
        内外侧对比是本平台在 3 mm 层厚数据上最可信的定量指标——层厚导致的系统性低估在两个间室间大体抵消。
      </p>
      <div className="mx-comp-grid">
        {(['medial', 'lateral'] as const).map((side) => {
          const c = comp[side]
          if (!c) return null
          return (
            <div key={side} className="mx-comp">
              <h3>{side === 'medial' ? '内侧间室' : '外侧间室'}</h3>
              <dl>
                <dt>股骨软骨平均厚度</dt><dd>{fmt(c.femoralThicknessMm)} mm</dd>
                <dt>胫骨软骨平均厚度</dt><dd>{fmt(c.tibialThicknessMm)} mm</dd>
                <dt>股胫合计 (MFTC/LFTC)</dt><dd>{fmt(c.sumThicknessMm)} mm</dd>
                <dt>软骨体积</dt><dd>{(c.volumeMm3 / 1000).toFixed(2)} cm³</dd>
              </dl>
            </div>
          )
        })}
        {asym !== undefined && (
          <div className="mx-comp mx-hero">
            <h3>内外侧不对称度</h3>
            <div className="mx-hero-n" style={{ color: Math.abs(asym) > 10 ? '#ec835a' : '#0ca30c' }}>
              {asym > 0 ? '+' : ''}{asym.toFixed(1)}%
            </div>
            <p className="mx-note">
              {Math.abs(asym) <= 10
                ? '在 ±10% 之内，内外侧大致对称。'
                : `${asym < 0 ? '内侧' : '外侧'}间室相对偏薄，建议结合力线与半月板情况判读。`}
            </p>
          </div>
        )}
      </div>
    </section>
  )
}

/**
 * Main chart: measured thickness against the reference range, per subregion.
 *
 * One row per subregion, grouped by plate. The hatched band is mean +/- 1 SD
 * (and a lighter +/- 2 SD), the dot is this knee. No legend box is needed for a
 * single measured series - the title names it - but the band and the dot are
 * both labelled in the key line above the plot.
 */
function SubregionChart({ plates, refs, sex }: {
  plates: PlateMetric[]; refs: ReferenceSet; sex: 'male' | 'female'
}) {
  // Rows interleaved with a header per cartilage plate: without the grouping
  // the 22 subregions read as one undifferentiated list and you cannot see
  // where the medial femur ends and the lateral femur begins.
  type Row =
    | { kind: 'group'; plate: string }
    | {
        kind: 'row'; code: string; labelZh: string; value: number
        conf: keyof typeof CONFIDENCE; slices: number; ref?: [number, number]
      }

  const rows = useMemo(() => {
    const out: Row[] = []
    for (const p of plates) {
      const entries = Object.entries(p.subregions)
        .filter(([, s]) => s.thicknessMm.mean != null)
      if (!entries.length) continue
      out.push({ kind: 'group', plate: p.name })
      for (const [code, s] of entries) {
        out.push({
          kind: 'row', code, labelZh: s.labelZh, value: s.thicknessMm.mean!,
          conf: s.confidence, slices: s.sliceSupport,
          ref: refs.subregions[code]?.[sex],
        })
      }
    }
    return out
  }, [plates, refs, sex])

  if (!rows.length) return null

  const ROW_H = 26
  const PAD_L = 200
  const PAD_R = 104          // room for the confidence word, not just its glyph
  const PAD_T = 30
  const W = 940
  const H = PAD_T + rows.length * ROW_H + 34
  const xMax = Math.max(5, ...rows.map((r) =>
    r.kind === 'row' ? Math.max(r.value, (r.ref?.[0] ?? 0) + 2 * (r.ref?.[1] ?? 0)) : 0)) * 1.08
  const x = (v: number) => PAD_L + (v / xMax) * (W - PAD_L - PAD_R)
  const ticks = [0, 1, 2, 3, 4, 5].filter((t) => t <= xMax)

  return (
    <section className="mx-card">
      <h2>各亚区平均软骨厚度</h2>
      <p className="mx-sub">
        采集条件：矢状位 2D、层厚 3 mm。体模实测本平台在此条件下系统性低估 7-18%，
        参考带（斜纹）来自近各向同性序列的 Framingham 队列，仅作量级参照，<b>不是诊断阈值</b>。
      </p>
      <div className="mx-key">
        <span><i className="mx-key-dot" style={{ background: C_VALUE }} /> 本例测量值</span>
        <span><i className="mx-key-band" /> 参考均值 ±1SD（深）/ ±2SD（浅）</span>
        <span className="mx-key-muted">括号内为该亚区跨越的原始层数</span>
      </div>
      <div className="mx-scroll">
        <svg width={W} height={H} role="img" aria-label="各亚区平均软骨厚度与参考范围">
          <defs>
            <pattern id="mx-hatch" width="6" height="6" patternTransform="rotate(45)"
              patternUnits="userSpaceOnUse">
              <rect width="6" height="6" fill={C_BAND} opacity="0.16" />
              <line x1="0" y1="0" x2="0" y2="6" stroke={C_BAND} strokeWidth="1.6" opacity="0.5" />
            </pattern>
            <pattern id="mx-hatch-2" width="6" height="6" patternTransform="rotate(45)"
              patternUnits="userSpaceOnUse">
              <rect width="6" height="6" fill={C_BAND} opacity="0.07" />
              <line x1="0" y1="0" x2="0" y2="6" stroke={C_BAND} strokeWidth="1" opacity="0.22" />
            </pattern>
          </defs>

          {ticks.map((t) => (
            <g key={t}>
              <line x1={x(t)} y1={PAD_T - 6} x2={x(t)} y2={H - 30} stroke={C_GRID} />
              <text x={x(t)} y={H - 14} fill="#8b97a8" fontSize="11" textAnchor="middle">{t}</text>
            </g>
          ))}
          <text x={W - PAD_R} y={H - 14} fill="#8b97a8" fontSize="11" textAnchor="end">mm</text>

          {rows.map((r, i) => {
            const y = PAD_T + i * ROW_H + ROW_H / 2
            if (r.kind === 'group') {
              return (
                <g key={`g${i}`}>
                  <text x={12} y={y + 4} fill="#d8e0ea" fontSize="12" fontWeight="600">
                    {r.plate}
                  </text>
                  <line x1={12} y1={y + 12} x2={W - PAD_R} y2={y + 12} stroke={C_GRID} />
                </g>
              )
            }
            const c = CONFIDENCE[r.conf]
            const inBand = !!r.ref && Math.abs(r.value - r.ref[0]) <= 2 * r.ref[1]
            return (
              <g key={r.code}>
                <text x={PAD_L - 10} y={y + 4} fill="#d8e0ea" fontSize="12" textAnchor="end">
                  {r.labelZh}
                  <tspan fill="#8b97a8" fontSize="11"> ({r.slices})</tspan>
                </text>

                {r.ref && (
                  <>
                    <rect x={x(Math.max(0, r.ref[0] - 2 * r.ref[1]))} y={y - 9}
                      width={x(r.ref[0] + 2 * r.ref[1]) - x(Math.max(0, r.ref[0] - 2 * r.ref[1]))}
                      height={18} fill="url(#mx-hatch-2)" rx="3" />
                    <rect x={x(r.ref[0] - r.ref[1])} y={y - 9}
                      width={x(r.ref[0] + r.ref[1]) - x(r.ref[0] - r.ref[1])}
                      height={18} fill="url(#mx-hatch)" rx="3" />
                    <line x1={x(r.ref[0])} y1={y - 9} x2={x(r.ref[0])} y2={y + 9}
                      stroke={C_BAND} strokeWidth="2" opacity="0.85" />
                  </>
                )}

                {/* 2px surface ring so the dot stays legible on top of the band */}
                <circle cx={x(r.value)} cy={y} r="6" fill="#0d1117" />
                <circle cx={x(r.value)} cy={y} r="4.5" fill={C_VALUE} />
                {/* Halo: the value sits over hatching whenever it lands in the
                    reference range, which is most of the time. */}
                <text x={x(r.value) + 11} y={y + 4} fill="#d8e0ea" fontSize="11"
                  stroke="#0d1117" strokeWidth={inBand ? 3 : 0} paintOrder="stroke">
                  {r.value.toFixed(2)}
                </text>
                <text x={W - 10} y={y + 4} fill={c.color} fontSize="11" textAnchor="end">
                  {c.glyph} {c.word}
                </text>
                <title>
                  {`${r.labelZh}（${r.code}）\n测量 ${r.value.toFixed(2)} mm\n` +
                    (r.ref
                      ? `参考 ${r.ref[0].toFixed(2)} ± ${r.ref[1].toFixed(2)} mm（${sex === 'male' ? '男' : '女'}，indirect）\n`
                      : '该亚区无公开参考值\n') +
                    `跨越 ${r.slices} 个原始层 · 可信度 ${c.word}`}
                </title>
              </g>
            )
          })}
        </svg>
      </div>
      <details className="mx-explain">
        <summary>这张图怎么读</summary>
        <p>{refs.metrics.meanThicknessMm.explanation_zh}</p>
        <p>{refs.reference_source.applicability_note_zh}</p>
        <p className="mx-note">{refs.observed_deviation_note_zh}</p>
      </details>
    </section>
  )
}

/** Small multiples: area-weighted thickness distribution per plate. */
function Histograms({ plates }: { plates: PlateMetric[] }) {
  return (
    <section className="mx-card">
      <h2>各软骨板厚度分布</h2>
      <p className="mx-sub">
        按骨-软骨界面顶点面积加权，0.1 mm 分箱。分布左移或出现左侧长尾提示局部变薄。
      </p>
      <div className="mx-hist-grid">
        {plates.map((p) => <Histogram key={p.label} plate={p} />)}
      </div>
    </section>
  )
}

function Histogram({ plate }: { plate: PlateMetric }) {
  const { edges, areaMm2 } = plate.histogram
  const W = 250, H = 132, PAD_B = 24, PAD_L = 30, PAD_T = 8
  const maxA = Math.max(1e-9, ...areaMm2)
  const last = areaMm2.reduce((acc, v, i) => (v > 0 ? i : acc), 0)
  const upper = Math.max(2, Math.ceil(edges[Math.min(last + 2, edges.length - 1)]))
  const x = (mm: number) => PAD_L + (mm / upper) * (W - PAD_L - 6)
  const y = (a: number) => H - PAD_B - (a / maxA) * (H - PAD_B - PAD_T)

  return (
    <figure className="mx-hist">
      <figcaption>{plate.name}</figcaption>
      <svg width={W} height={H} role="img"
        aria-label={`${plate.name} 厚度分布，平均 ${fmt(plate.thickness.mean)} 毫米`}>
        {[0, 1, 2, 3, 4, 5].filter((t) => t <= upper).map((t) => (
          <g key={t}>
            <line x1={x(t)} y1={PAD_T} x2={x(t)} y2={H - PAD_B} stroke={C_GRID} />
            <text x={x(t)} y={H - 8} fill="#8b97a8" fontSize="10" textAnchor="middle">{t}</text>
          </g>
        ))}
        {areaMm2.map((a, i) => {
          if (a <= 0 || edges[i] > upper) return null
          const x0 = x(edges[i]), x1 = x(edges[i + 1])
          // 2px gap between bars keeps adjacent bins from reading as one mass.
          const w = Math.max(1, x1 - x0 - 1)
          return <rect key={i} x={x0} y={y(a)} width={w} height={H - PAD_B - y(a)}
            fill={C_VALUE} rx="1.5" />
        })}
        {plate.thickness.mean != null && (
          <line x1={x(plate.thickness.mean)} y1={PAD_T} x2={x(plate.thickness.mean)}
            y2={H - PAD_B} stroke="#d8e0ea" strokeWidth="1.5" strokeDasharray="3 3" />
        )}
      </svg>
      <div className="mx-hist-meta">
        平均 <b>{fmt(plate.thickness.mean)}</b> mm ·
        体积 <b>{(plate.volume_mm3 / 1000).toFixed(2)}</b> cm³ ·
        可靠区 <b>{(plate.qc.reliableAreaFraction * 100).toFixed(0)}%</b>
      </div>
    </figure>
  )
}

function MetricGlossary({ refs }: { refs: ReferenceSet }) {
  return (
    <section className="mx-card">
      <h2>指标说明</h2>
      <dl className="mx-gloss">
        {Object.entries(refs.metrics).map(([k, m]) => (
          <div key={k}>
            <dt>{m.label_zh} <span className="mx-kv">{m.unit}</span></dt>
            <dd>{m.explanation_zh}</dd>
          </div>
        ))}
      </dl>
      <p className="mx-note">
        参考值来源：{refs.reference_source.name}（n={refs.reference_source.n}，
        {refs.reference_source.sequence}，{refs.reference_source.measure} 口径）。
        适用性标记：<b>{refs.reference_source.applicability}</b>。
      </p>
    </section>
  )
}

/** Table view - identity and value never depend on colour alone. */
function DataTable({ plates, refs, sex }: {
  plates: PlateMetric[]; refs: ReferenceSet; sex: 'male' | 'female'
}) {
  return (
    <section className="mx-card">
      <h2>数据表</h2>
      <div className="mx-scroll">
        <table className="mx-table">
          <thead>
            <tr>
              <th>软骨板</th><th>亚区</th><th>MOAKS</th>
              <th>平均 mm</th><th>p10</th><th>p90</th>
              <th>参考 mm</th><th>层数</th><th>可靠区</th><th>可信度</th>
            </tr>
          </thead>
          <tbody>
            {plates.flatMap((p) =>
              Object.entries(p.subregions).map(([code, s]) => {
                const r = refs.subregions[code]?.[sex]
                const c = CONFIDENCE[s.confidence]
                return (
                  <tr key={`${p.label}-${code}`}>
                    <td>{p.name}</td>
                    <td>{s.labelZh}</td>
                    <td className="mx-mono">{s.moaks}</td>
                    <td className="mx-mono"><b>{fmt(s.thicknessMm.mean)}</b></td>
                    <td className="mx-mono">{fmt(s.thicknessMm.p10)}</td>
                    <td className="mx-mono">{fmt(s.thicknessMm.p90)}</td>
                    <td className="mx-mono">{r ? `${r[0].toFixed(2)} ± ${r[1].toFixed(2)}` : '—'}</td>
                    <td className="mx-mono">{s.sliceSupport}</td>
                    <td className="mx-mono">{(s.reliableAreaFraction * 100).toFixed(0)}%</td>
                    <td style={{ color: c.color }}>{c.glyph} {c.word}</td>
                  </tr>
                )
              }),
            )}
          </tbody>
        </table>
      </div>
    </section>
  )
}

/** Turn the engine's exception text into something a clinician can act on. */
function explainFailure(error: string | null): string {
  if (error && /swapped|medial\/lateral/i.test(error)) {
    return '该病例的内侧/外侧软骨标签与影像中膝关节的实际位置不符，疑似标注时左右侧弄反了。'
      + '请先核对分割标签（标签 4/6 应为内侧，5/7 应为外侧）后重新计算。'
  }
  if (error && /not materialised/i.test(error)) {
    return '该分割尚未生成标签体数据，请先在阅片页打开一次该病例。'
  }
  return error ?? '未知错误'
}

function fmt(v: number | null | undefined): string {
  return v == null ? '—' : v.toFixed(2)
}
