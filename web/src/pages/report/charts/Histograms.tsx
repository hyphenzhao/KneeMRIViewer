/**
 * Small multiples: area-weighted thickness distribution per plate, one row.
 */
import type { PlateMetric } from '../../../api'

function fmt(v: number | null | undefined): string {
  return v == null ? '—' : v.toFixed(2)
}

export function Histogram({ plate }: { plate: PlateMetric }) {
  const { edges, areaMm2 } = plate.histogram
  const W = 250, H = 120, PAD_B = 22, PAD_L = 8, PAD_T = 6
  const maxA = Math.max(1e-9, ...areaMm2)
  const last = areaMm2.reduce((acc, v, i) => (v > 0 ? i : acc), 0)
  const upper = Math.max(2, Math.ceil(edges[Math.min(last + 2, edges.length - 1)]))
  const x = (mm: number) => PAD_L + (mm / upper) * (W - PAD_L - 6)
  const y = (a: number) => H - PAD_B - (a / maxA) * (H - PAD_B - PAD_T)
  return (
    <figure className="rp-hist">
      <figcaption>{plate.name}</figcaption>
      <svg className="rp-chart" viewBox={`0 0 ${W} ${H}`} role="img"
        aria-label={`${plate.name} 厚度分布，平均 ${fmt(plate.thickness.mean)} 毫米`}>
        {[0, 1, 2, 3, 4, 5].filter((t) => t <= upper).map((t) => (
          <g key={t}>
            <line x1={x(t)} y1={PAD_T} x2={x(t)} y2={H - PAD_B} className="rp-chart-grid" />
            <text x={x(t)} y={H - 7} fontSize="11" textAnchor="middle" className="rp-chart-muted">{t}</text>
          </g>
        ))}
        {areaMm2.map((a, i) => {
          if (a <= 0 || edges[i] > upper) return null
          const x0 = x(edges[i]), x1 = x(edges[i + 1])
          const w = Math.max(1, x1 - x0 - 1)      // a 1-unit gap keeps bins from reading as one mass
          return <rect key={i} x={x0} y={y(a)} width={w} height={H - PAD_B - y(a)} className="rp-chart-value" rx="1.5" />
        })}
        {plate.thickness.mean != null && (
          <line x1={x(plate.thickness.mean)} y1={PAD_T} x2={x(plate.thickness.mean)} y2={H - PAD_B}
            className="rp-chart-mean" strokeWidth="1.5" strokeDasharray="3 3" />
        )}
      </svg>
      <div className="rp-hist-meta">
        平均 <b>{fmt(plate.thickness.mean)}</b> mm · 体积 <b>{(plate.volume_mm3 / 1000).toFixed(2)}</b> cm³ · 可靠区 <b>{(plate.qc.reliableAreaFraction * 100).toFixed(0)}%</b>
      </div>
    </figure>
  )
}

export function Histograms({ plates }: { plates: PlateMetric[] }) {
  return (
    <div className="rp-hist-row">
      {plates.map((p) => <Histogram key={p.label} plate={p} />)}
    </div>
  )
}
