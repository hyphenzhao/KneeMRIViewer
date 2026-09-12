/**
 * Flattened cartilage maps - thickness and Outerbridge grade - per plate.
 *
 * One raster from the server (see raster.ts), two ways of painting it:
 *
 * - **ThicknessMap**: viridis over 0-4 mm; unreliable pixels hatched, gaps
 *   that were bridged left plain, bare bone marked; subregion boundaries and
 *   codes and lesion outlines on an SVG overlay, so text and lines stay
 *   vector on paper.
 * - **GradeMap**: the same silhouette with every subregion filled by its
 *   *effective* grade (a doctor's override wins and is starred), lesion
 *   outlines in the lesion's own grade colour.
 *
 * Both are laid out from millimetre tile boxes chosen by the page plan, so a
 * figure's height is known before any pixel arrives and pagination never
 * waits for the network.
 */
import { useId, useLayoutEffect, useMemo, useRef, useState } from 'react'

import { parseCssColor, resolvePalette, thicknessRgb, viridisStops, type Palette } from '../colormap'
import { loopsToPath, traceMask } from '../isolines'
import { CLS, codeAt, type DecodedRaster } from '../raster'
import { useFigureReady } from '../ready'

export interface TileBox { wMm: number; hMm: number }

const GRADE_ROMAN: Record<string, string> = { '0': '0', II: 'Ⅱ', III: 'Ⅲ', IV: 'Ⅳ', 未评估: '?' }

export const PLATE_ORDER = [4, 5, 6, 7, 8]

function fit(r: DecodedRaster, box: TileBox) {
  const s = Math.min(box.wMm / r.width, box.hMm / r.height)
  return { wMm: r.width * s, hMm: r.height * s, unitMm: s }
}

/** Outline paths of the raster's classes and lesions, in pixel units. */
function useOutlines(r: DecodedRaster) {
  return useMemo(() => {
    const { width: w, height: h, cls, lesion } = r
    const silhouette = loopsToPath(traceMask((i) => cls[i] !== CLS.outside, w, h))
    const unreliable = loopsToPath(traceMask((i) => cls[i] === CLS.unreliable, w, h))
    const denuded = loopsToPath(traceMask((i) => cls[i] === CLS.denuded, w, h))
    const lesions = r.lesions.map((l) => ({
      ...l, path: loopsToPath(traceMask((i) => lesion[i] === l.index, w, h)),
    }))
    return { silhouette, unreliable, denuded, lesions }
  }, [r])
}

type Painter = (img: ImageData, r: DecodedRaster, pal: Palette) => void

const paintThickness: Painter = (img, r, pal) => {
  const d = img.data
  const max = r.scale.maxMm * 1000
  const grey = parseCssColor(pal.unmeasured)
  const bare = parseCssColor(pal.denuded)
  for (let i = 0; i < r.width * r.height; i++) {
    const c = r.cls[i]
    const o = i * 4
    if (c === CLS.outside) { d[o + 3] = 0; continue }
    let rgb: [number, number, number]
    if (c === CLS.denuded) rgb = [bare[0], bare[1], bare[2]]
    else if (c === CLS.unreliable && r.thicknessUm[i] === 0) rgb = grey
    else rgb = thicknessRgb(r.thicknessUm[i] / max)
    d[o] = rgb[0]; d[o + 1] = rgb[1]; d[o + 2] = rgb[2]
    d[o + 3] = c === CLS.unreliable ? 150 : 255
  }
}

function gradePainter(grades: Record<string, string>): Painter {
  return (img, r, pal) => {
    const d = img.data
    const byIndex = r.codes.map((code) => parseCssColor(pal.grade[grades[code] ?? '未评估'] ?? pal.grade['未评估']))
    const bare = parseCssColor(pal.denuded)
    for (let i = 0; i < r.width * r.height; i++) {
      const c = r.cls[i]
      const o = i * 4
      if (c === CLS.outside) { d[o + 3] = 0; continue }
      const rgb = c === CLS.denuded ? bare : (byIndex[r.code[i] - 1] ?? parseCssColor(pal.grade['未评估']))
      d[o] = rgb[0]; d[o + 1] = rgb[1]; d[o + 2] = rgb[2]; d[o + 3] = 255
    }
  }
}

function MapTile({ raster: r, box, figureId, paint, labels, interactive, className, tooltip }: {
  raster: DecodedRaster
  box: TileBox
  figureId: string
  paint: Painter
  labels: Array<{ x: number; y: number; text: string }>
  interactive: boolean
  className: string
  tooltip?: (x: number, y: number) => string
}) {
  const uid = useId().replace(/:/g, '')
  const done = useFigureReady(figureId)
  const canvas = useRef<HTMLCanvasElement>(null)
  const wrap = useRef<HTMLDivElement>(null)
  const [tip, setTip] = useState<{ x: number; y: number; text: string } | null>(null)
  const { wMm, hMm, unitMm } = fit(r, box)
  const outlines = useOutlines(r)

  useLayoutEffect(() => {
    const cv = canvas.current
    if (!cv || !wrap.current) return
    try {
      const ctx = cv.getContext('2d')!
      const img = ctx.createImageData(r.width, r.height)
      paint(img, r, resolvePalette(wrap.current))
      ctx.putImageData(img, 0, 0)
      done('ok')
    } catch {
      done('error')
    }
  }, [r, paint, done])

  const fu = 2.4 / unitMm            // 2.4 mm text, in pixel units
  const sw = 0.22 / unitMm           // 0.22 mm lines
  const hatch = 1.1 / unitMm         // hatch pitch ~1.1 mm

  return (
    <div ref={wrap} className={`rp-map ${className}`} style={{ width: `${wMm}mm`, height: `${hMm}mm` }}
      onMouseMove={interactive && tooltip ? (e) => {
        const rect = wrap.current!.getBoundingClientRect()
        const x = Math.floor(((e.clientX - rect.left) / rect.width) * r.width)
        const y = Math.floor(((e.clientY - rect.top) / rect.height) * r.height)
        if (x < 0 || y < 0 || x >= r.width || y >= r.height || r.cls[y * r.width + x] === CLS.outside) { setTip(null); return }
        setTip({ x: e.clientX - rect.left, y: e.clientY - rect.top, text: tooltip(x, y) })
      } : undefined}
      onMouseLeave={() => setTip(null)}>
      <canvas ref={canvas} width={r.width} height={r.height} />
      <svg viewBox={`0 0 ${r.width} ${r.height}`} preserveAspectRatio="none" aria-hidden="true">
        <defs>
          <clipPath id={`sil-${uid}`}><path d={outlines.silhouette} /></clipPath>
          <pattern id={`hatch-${uid}`} width={hatch} height={hatch} patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
            <line x1="0" y1="0" x2="0" y2={hatch} className="rp-map-hatch" strokeWidth={sw * 1.3} />
          </pattern>
          <pattern id={`bare-${uid}`} width={hatch} height={hatch} patternUnits="userSpaceOnUse" patternTransform="rotate(-45)">
            <line x1="0" y1="0" x2="0" y2={hatch} className="rp-map-bare" strokeWidth={sw * 1.6} />
          </pattern>
        </defs>
        <path d={outlines.unreliable} fill={`url(#hatch-${uid})`} stroke="none" />
        <path d={outlines.denuded} fill={`url(#bare-${uid})`} stroke="none" />
        <g clipPath={`url(#sil-${uid})`} className="rp-map-boundary" fill="none" strokeWidth={sw}>
          {r.boundaries.map((b, i) => b.type === 'line'
            ? <line key={i} x1={b.a[0]} y1={b.a[1]} x2={b.b[0]} y2={b.b[1]} />
            : <ellipse key={i} cx={b.c[0]} cy={b.c[1]} rx={b.r[0]} ry={b.r[1]} />)}
        </g>
        <path d={outlines.silhouette} className="rp-map-outline" fill="none" strokeWidth={sw} />
        {outlines.lesions.map((l) => (
          <path key={l.index} d={l.path} className={`rp-map-lesion rp-map-lesion-${l.grade.toLowerCase()}`}
            fill="none" strokeWidth={sw * 1.8} strokeLinejoin="round" />
        ))}
        {labels.map((l, i) => (
          <text key={i} x={l.x} y={l.y} className="rp-map-label" fontSize={fu} textAnchor="middle"
            dominantBaseline="middle" strokeWidth={fu * 0.28} paintOrder="stroke">{l.text}</text>
        ))}
      </svg>
      {tip && <div className="rp-map-tip" style={{ left: tip.x + 10, top: tip.y + 10 }}>{tip.text}</div>}
    </div>
  )
}

const CLS_ZH: Record<number, string> = {
  1: '实测', 2: '不可靠（沿层厚方向采样）', 3: '邻近插补', 4: '骨面裸露',
}

export function ThicknessMap({ raster, box, interactive }: { raster: DecodedRaster; box: TileBox; interactive: boolean }) {
  const labels = raster.labels.map((l) => ({ x: l.px[0], y: l.px[1], text: l.code }))
  return (
    <MapTile raster={raster} box={box} figureId={`thickness-${raster.label}`} paint={paintThickness}
      labels={labels} interactive={interactive} className="rp-map-thickness"
      tooltip={(x, y) => {
        const i = y * raster.width + x
        const code = codeAt(raster, x, y)
        const name = raster.labels.find((l) => l.code === code)?.labelZh ?? code
        const th = raster.thicknessUm[i] / 1000
        return `${name}（${code}）· ${th > 0 ? th.toFixed(2) + ' mm' : '未测'} · ${CLS_ZH[raster.cls[i]] ?? ''}`
      }} />
  )
}

export function GradeMap({ raster, box, grades, interactive }: {
  raster: DecodedRaster; box: TileBox; interactive: boolean
  grades: Record<string, { grade: string; overridden: boolean }>
}) {
  const painter = useMemo(() => gradePainter(Object.fromEntries(
    Object.entries(grades).map(([k, v]) => [k, v.grade]))), [grades])
  const labels = raster.labels.map((l) => {
    const g = grades[l.code]
    return { x: l.px[0], y: l.px[1], text: `${l.code} ${GRADE_ROMAN[g?.grade ?? '未评估'] ?? g?.grade ?? '?'}${g?.overridden ? '*' : ''}` }
  })
  return (
    <MapTile raster={raster} box={box} figureId={`grade-${raster.label}`} paint={painter}
      labels={labels} interactive={interactive} className="rp-map-grade"
      tooltip={(x, y) => {
        const code = codeAt(raster, x, y)
        const g = grades[code]
        const name = raster.labels.find((l) => l.code === code)?.labelZh ?? code
        return `${name}（${code}）· Outerbridge ${g?.grade ?? '未评估'}${g?.overridden ? '（医师修改）' : ''}`
      }} />
  )
}

/** Two rows: femur ×2, then tibia ×2 + patella; each in a fixed mm box. */
export function PlateGrid({ rasters, boxA, boxB, render, missing }: {
  rasters: Partial<Record<number, DecodedRaster | null>>
  boxA: TileBox
  boxB: TileBox
  render: (r: DecodedRaster, box: TileBox) => React.ReactNode
  missing: (label: number) => React.ReactNode
}) {
  const row = (labels: number[], box: TileBox) => (
    <div className="rp-plate-row" style={{ height: `${box.hMm + 6}mm` }}>
      {labels.map((label) => {
        const r = rasters[label]
        return (
          <figure key={label} className="rp-plate-tile" style={{ width: `${box.wMm}mm` }}>
            <div className="rp-plate-box" style={{ height: `${box.hMm}mm` }}>
              {r ? render(r, box) : missing(label)}
            </div>
            <figcaption>{r ? r.name : `标签 ${label}`}{r?.axes?.x ? ` · 横：${r.axes.x.labelZh}，纵：${r.axes.y?.labelZh ?? ''}` : ''}</figcaption>
          </figure>
        )
      })}
    </div>
  )
  return (
    <div className="rp-plate-grid">
      {row([4, 5], boxA)}
      {row([6, 7, 8], boxB)}
    </div>
  )
}

export function ColourBar({ maxMm }: { maxMm: number }) {
  const uid = useId().replace(/:/g, '')
  const ticks = [0, 1, 2, 3, 4].filter((t) => t <= maxMm)
  return (
    <svg className="rp-colourbar" viewBox="0 0 400 22" preserveAspectRatio="none" role="img" aria-label="厚度色标">
      <defs>
        <linearGradient id={`vir-${uid}`} x1="0" x2="1" y1="0" y2="0">
          {viridisStops().map((s) => <stop key={s.offset} offset={s.offset} stopColor={s.color} />)}
        </linearGradient>
      </defs>
      <rect x="40" y="2" width="320" height="8" fill={`url(#vir-${uid})`} />
      {ticks.map((t) => (
        <g key={t}>
          <line x1={40 + (t / maxMm) * 320} y1="10" x2={40 + (t / maxMm) * 320} y2="13" className="rp-chart-axis" />
          <text x={40 + (t / maxMm) * 320} y="21" fontSize="8" textAnchor="middle" className="rp-chart-text">{t}</text>
        </g>
      ))}
      <text x="366" y="9" fontSize="8" className="rp-chart-text">mm</text>
      <text x="36" y="9" fontSize="8" textAnchor="end" className="rp-chart-text">厚度</text>
    </svg>
  )
}

export function MapLegend({ mode }: { mode: 'thickness' | 'grade' }) {
  return (
    <div className="rp-map-legend">
      {mode === 'grade' && ['0', 'II', 'III', 'IV', '未评估'].map((g) => (
        <span key={g}><i className={`rp-chip rp-chip-grade-${g === '未评估' ? 'na' : g.toLowerCase()}`} />{g === '未评估' ? '未评估' : `${g} 级`}</span>
      ))}
      {mode === 'thickness' && <span><i className="rp-chip rp-chip-hatch" />不可靠区（沿层厚方向采样）</span>}
      <span><i className="rp-chip rp-chip-lesion-ii" />病灶轮廓 II</span>
      <span><i className="rp-chip rp-chip-lesion-iii" />III</span>
      <span><i className="rp-chip rp-chip-lesion-iv" />IV（骨面裸露）</span>
      {mode === 'grade' && <span className="rp-map-legend-note">* 医师修改后的分级</span>}
    </div>
  )
}
