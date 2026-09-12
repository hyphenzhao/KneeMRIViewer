/**
 * Measured thickness per subregion against the reference range.
 *
 * A dot plot, not a bar chart: what a clinician reads is position within a
 * normal range, the form a lab report already uses. The band is hatched
 * because the Framingham reference came from near-isotropic FLASH and ours is
 * 3 mm PD-FSE: texture says "magnitude reference, not diagnostic threshold".
 * Colours are CSS classes so the print palette applies; pattern ids are
 * per-instance so two plots on one page cannot share (or steal) a hatch.
 */
import { useId, useMemo } from 'react'

import type { PlateMetric, ReferenceSet } from '../../../api'

const CONF = {
  high: { glyph: '●', word: '可信' }, medium: { glyph: '◐', word: '一般' }, low: { glyph: '○', word: '存疑' },
} as const

export type Sex = 'male' | 'female'

type Row =
  | { kind: 'group'; plate: string }
  | { kind: 'row'; code: string; labelZh: string; value: number; conf: keyof typeof CONF; slices: number; ref?: [number, number] }

export const DOT_ROW_H = 24

export function dotPlotRows(plates: PlateMetric[], refs: ReferenceSet | null, sex: Sex | null): Row[] {
  const out: Row[] = []
  for (const p of plates) {
    const entries = Object.entries(p.subregions).filter(([, s]) => s.thicknessMm.mean != null)
    if (!entries.length) continue
    out.push({ kind: 'group', plate: p.name })
    for (const [code, s] of entries) {
      out.push({
        kind: 'row', code, labelZh: s.labelZh, value: s.thicknessMm.mean!,
        conf: s.confidence, slices: s.sliceSupport,
        ref: sex && refs ? refs.subregions[code]?.[sex] : undefined,
      })
    }
  }
  return out
}

export function SubregionDotPlot({ plates, refs, sex }: { plates: PlateMetric[]; refs: ReferenceSet | null; sex: Sex | null }) {
  const uid = useId().replace(/:/g, '')
  const rows = useMemo(() => dotPlotRows(plates, refs, sex), [plates, refs, sex])
  if (!rows.length) return null

  const ROW_H = DOT_ROW_H, PAD_L = 200, PAD_R = 104, PAD_T = 26, W = 940
  const H = PAD_T + rows.length * ROW_H + 30
  const xMax = Math.max(5, ...rows.map((r) =>
    r.kind === 'row' ? Math.max(r.value, (r.ref?.[0] ?? 0) + 2 * (r.ref?.[1] ?? 0)) : 0)) * 1.08
  const x = (v: number) => PAD_L + (v / xMax) * (W - PAD_L - PAD_R)
  const ticks = [0, 1, 2, 3, 4, 5].filter((t) => t <= xMax)

  return (
    <svg className="rp-chart rp-dotplot" viewBox={`0 0 ${W} ${H}`} role="img" aria-label="各亚区平均软骨厚度与参考范围">
      <defs>
        <pattern id={`hatch1-${uid}`} width="6" height="6" patternTransform="rotate(45)" patternUnits="userSpaceOnUse">
          <rect width="6" height="6" className="rp-band-fill" />
          <line x1="0" y1="0" x2="0" y2="6" className="rp-band-line" strokeWidth="1.6" />
        </pattern>
        <pattern id={`hatch2-${uid}`} width="6" height="6" patternTransform="rotate(45)" patternUnits="userSpaceOnUse">
          <rect width="6" height="6" className="rp-band-fill-2" />
          <line x1="0" y1="0" x2="0" y2="6" className="rp-band-line-2" strokeWidth="1" />
        </pattern>
      </defs>

      {ticks.map((t) => (
        <g key={t}>
          <line x1={x(t)} y1={PAD_T - 6} x2={x(t)} y2={H - 26} className="rp-chart-grid" />
          <text x={x(t)} y={H - 10} fontSize="11" textAnchor="middle" className="rp-chart-muted">{t}</text>
        </g>
      ))}
      <text x={W - PAD_R} y={H - 10} fontSize="11" textAnchor="end" className="rp-chart-muted">mm</text>

      {rows.map((r, i) => {
        const y = PAD_T + i * ROW_H + ROW_H / 2
        if (r.kind === 'group') {
          return (
            <g key={`g${i}`}>
              <text x={12} y={y + 4} fontSize="12" fontWeight="600" className="rp-chart-text">{r.plate}</text>
              <line x1={12} y1={y + 11} x2={W - PAD_R} y2={y + 11} className="rp-chart-grid" />
            </g>
          )
        }
        const c = CONF[r.conf]
        const inBand = !!r.ref && Math.abs(r.value - r.ref[0]) <= 2 * r.ref[1]
        return (
          <g key={r.code}>
            <text x={PAD_L - 10} y={y + 4} fontSize="12" textAnchor="end" className="rp-chart-text">
              {r.labelZh}<tspan fontSize="11" className="rp-chart-muted"> ({r.slices})</tspan>
            </text>
            {r.ref && (
              <>
                <rect x={x(Math.max(0, r.ref[0] - 2 * r.ref[1]))} y={y - 8}
                  width={x(r.ref[0] + 2 * r.ref[1]) - x(Math.max(0, r.ref[0] - 2 * r.ref[1]))}
                  height={16} fill={`url(#hatch2-${uid})`} rx="3" />
                <rect x={x(r.ref[0] - r.ref[1])} y={y - 8}
                  width={x(r.ref[0] + r.ref[1]) - x(r.ref[0] - r.ref[1])}
                  height={16} fill={`url(#hatch1-${uid})`} rx="3" />
                <line x1={x(r.ref[0])} y1={y - 8} x2={x(r.ref[0])} y2={y + 8} className="rp-band-mean" strokeWidth="2" />
              </>
            )}
            <circle cx={x(r.value)} cy={y} r="6" className="rp-chart-halo" />
            <circle cx={x(r.value)} cy={y} r="4.5" className="rp-chart-value" />
            <text x={x(r.value) + 11} y={y + 4} fontSize="11" className="rp-chart-text rp-chart-haloed"
              strokeWidth={inBand ? 3 : 0} paintOrder="stroke">{r.value.toFixed(2)}</text>
            <text x={W - 10} y={y + 4} fontSize="11" textAnchor="end" className={`rp-conf-${r.conf}`}>{c.glyph} {c.word}</text>
            <title>
              {`${r.labelZh}（${r.code}）\n测量 ${r.value.toFixed(2)} mm\n` +
                (r.ref
                  ? `参考 ${r.ref[0].toFixed(2)} ± ${r.ref[1].toFixed(2)} mm（${sex === 'male' ? '男' : '女'}，indirect）\n`
                  : '该亚区无适用参考值\n') +
                `跨越 ${r.slices} 个原始层 · 可信度 ${c.word}`}
            </title>
          </g>
        )
      })}
    </svg>
  )
}
