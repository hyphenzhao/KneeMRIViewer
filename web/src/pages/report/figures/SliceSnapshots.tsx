/**
 * Three key slices (server PNGs with label contours) plus captions.
 *
 * The server chose the slices and says why (the worst lesion, or the central
 * medial condyle); the caption repeats that, so a reader knows the picture
 * was picked by a rule, not by a person. Scale bar and plane letter are drawn
 * here, in vector, from the PNG's mm-per-pixel.
 */
import { useFigureReady } from '../ready'
import type { TileBox } from './PlateMaps'

export interface SliceMeta {
  name: string; url: string; axis: number; index: number; why: string
  widthPx: number; heightPx: number; mmPerPx: number; labels: number[]; nativeSliceMm?: number
}

const PLANE_ZH: Record<string, { zh: string; letter: string }> = {
  sagittal: { zh: '矢状位', letter: 'S' }, coronal: { zh: '冠状位', letter: 'C' }, axial: { zh: '轴位', letter: 'A' },
}
export const PLANES = ['sagittal', 'coronal', 'axial'] as const

export function SliceTile({ plane, meta, box }: { plane: string; meta: SliceMeta; box: TileBox }) {
  const done = useFigureReady(`slice-${plane}`)
  const s = Math.min(box.wMm / meta.widthPx, box.hMm / meta.heightPx)
  const wMm = meta.widthPx * s, hMm = meta.heightPx * s
  const barPx = 10 / meta.mmPerPx
  const fu = 2.4 / s
  const p = PLANE_ZH[plane] ?? { zh: plane, letter: '?' }
  return (
    <div className="rp-slice" style={{ width: `${wMm}mm`, height: `${hMm}mm` }}>
      <img className="rp-slice-img" data-figure={`slice-${plane}`} src={meta.url} alt={`${p.zh}关键层`}
        width={meta.widthPx} height={meta.heightPx}
        onLoad={(e) => { const img = e.currentTarget; (img.decode ? img.decode() : Promise.resolve()).then(() => done('ok'), () => done('ok')) }}
        onError={() => done('error')} />
      <svg viewBox={`0 0 ${meta.widthPx} ${meta.heightPx}`} preserveAspectRatio="none" aria-hidden="true">
        <text x={fu * 0.5} y={fu * 1.2} fontSize={fu * 1.15} className="rp-slice-text" fontWeight="700">{p.letter}</text>
        <line x1={fu * 0.6} y1={meta.heightPx - fu * 0.9} x2={fu * 0.6 + barPx} y2={meta.heightPx - fu * 0.9}
          className="rp-slice-bar" strokeWidth={fu * 0.18} />
        <text x={fu * 0.6 + barPx / 2} y={meta.heightPx - fu * 1.3} fontSize={fu * 0.8} textAnchor="middle" className="rp-slice-text">10 mm</text>
      </svg>
    </div>
  )
}

export function sliceCaption(plane: string, meta: SliceMeta): string {
  const p = PLANE_ZH[plane]?.zh ?? plane
  const native = meta.nativeSliceMm != null && meta.nativeSliceMm >= 1.0
  return `${p} · 第 ${meta.index + 1} 层${native ? '' : '（重建）'} · ${meta.why}`
}
