/**
 * The report's figure palette.
 *
 * Sequential magnitude (thickness) is viridis - one perceptual ramp, legible
 * on white paper and on the dark screen alike, and the one readers of
 * cartilage maps already know. Everything categorical (grades, lesions,
 * confidence) goes through `--rp-*` CSS tokens so the print view can flip it
 * in one place; canvases read the tokens at paint time via `resolvePalette`.
 */

// viridis, 9 stops, 0 -> 1
const VIRIDIS: Array<[number, number, number]> = [
  [68, 1, 84], [72, 36, 117], [65, 68, 135], [53, 95, 141], [42, 120, 142],
  [33, 145, 140], [34, 168, 132], [68, 191, 112], [122, 209, 81], [189, 223, 38], [253, 231, 37],
]

export function thicknessRgb(t: number): [number, number, number] {
  const u = Math.min(1, Math.max(0, t)) * (VIRIDIS.length - 1)
  const i = Math.min(VIRIDIS.length - 2, Math.floor(u))
  const f = u - i
  const a = VIRIDIS[i], b = VIRIDIS[i + 1]
  return [a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f, a[2] + (b[2] - a[2]) * f]
}

/** CSS gradient stops for the colour bar, matching thicknessRgb. */
export function viridisStops(): Array<{ offset: number; color: string }> {
  return VIRIDIS.map((c, i) => ({ offset: i / (VIRIDIS.length - 1), color: `rgb(${c[0]},${c[1]},${c[2]})` }))
}

export const GRADE_TOKEN: Record<string, string> = {
  '0': '--rp-grade-0', II: '--rp-grade-2', III: '--rp-grade-3', IV: '--rp-grade-4', 未评估: '--rp-grade-na',
}
export const LESION_TOKEN: Record<string, string> = {
  II: '--rp-lesion-ii', III: '--rp-lesion-iii', IV: '--rp-lesion-iv',
}

export interface Palette {
  paper: string; text: string; muted: string
  grade: Record<string, string>
  lesion: Record<string, string>
  unmeasured: string
  denuded: string
}

export function resolvePalette(el: Element): Palette {
  const cs = getComputedStyle(el)
  const v = (name: string, fallback: string) => cs.getPropertyValue(name).trim() || fallback
  return {
    paper: v('--rp-paper', '#fff'),
    text: v('--rp-text', '#111'),
    muted: v('--rp-muted', '#666'),
    grade: Object.fromEntries(Object.entries(GRADE_TOKEN).map(([g, t]) => [g, v(t, '#999')])),
    lesion: Object.fromEntries(Object.entries(LESION_TOKEN).map(([g, t]) => [g, v(t, '#c00')])),
    unmeasured: v('--rp-map-unmeasured', '#bbb'),
    denuded: v('--rp-lesion-iv', '#c00'),
  }
}

export function parseCssColor(c: string): [number, number, number] {
  const m = /^#([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(c.trim())
  if (m) return [parseInt(m[1], 16), parseInt(m[2], 16), parseInt(m[3], 16)]
  const r = /rgba?\(\s*(\d+)[,\s]+(\d+)[,\s]+(\d+)/.exec(c)
  if (r) return [Number(r[1]), Number(r[2]), Number(r[3])]
  return [153, 153, 153]
}
