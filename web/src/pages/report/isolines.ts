/**
 * Outlines of a binary pixel mask, as closed loops on pixel corners.
 *
 * Pixel-exact by design: the loop follows the raster's own staircase, so an
 * outline never claims a smoothness the 0.5 mm grid does not have. Every
 * boundary edge is emitted once, directed clockwise (screen coordinates), and
 * the edges are chained into loops; holes come out as their own loops.
 */
export type Loop = Array<[number, number]>

export function traceMask(inside: (i: number) => boolean, w: number, h: number): Loop[] {
  // Directed edges keyed by their start corner: key = y * (w + 1) + x.
  const W1 = w + 1
  const next = new Map<number, number[]>()
  const add = (x0: number, y0: number, x1: number, y1: number) => {
    const k = y0 * W1 + x0
    const list = next.get(k)
    if (list) list.push(y1 * W1 + x1)
    else next.set(k, [y1 * W1 + x1])
  }
  for (let y = 0; y < h; y++) {
    for (let x = 0; x < w; x++) {
      if (!inside(y * w + x)) continue
      if (y === 0 || !inside((y - 1) * w + x)) add(x, y, x + 1, y)             // top, rightwards
      if (x === w - 1 || !inside(y * w + x + 1)) add(x + 1, y, x + 1, y + 1)   // right, downwards
      if (y === h - 1 || !inside((y + 1) * w + x)) add(x + 1, y + 1, x, y + 1) // bottom, leftwards
      if (x === 0 || !inside(y * w + x - 1)) add(x, y + 1, x, y)               // left, upwards
    }
  }
  const loops: Loop[] = []
  for (const [startKey, outs] of next) {
    while (outs.length) {
      const loop: Loop = []
      let k = startKey
      let guard = 0
      for (;;) {
        loop.push([k % W1, Math.floor(k / W1)])
        const list = next.get(k)
        if (!list || !list.length) break
        k = list.pop()!
        if (k === startKey || ++guard > 4 * w * h) break
      }
      if (loop.length >= 4) loops.push(simplify(loop))
    }
  }
  return loops
}

/** Drop the middle point of every collinear run, so paths stay short. */
function simplify(loop: Loop): Loop {
  const out: Loop = []
  const n = loop.length
  for (let i = 0; i < n; i++) {
    const p = loop[(i - 1 + n) % n], c = loop[i], q = loop[(i + 1) % n]
    const collinear = (c[0] - p[0]) * (q[1] - c[1]) === (c[1] - p[1]) * (q[0] - c[0])
    if (!collinear) out.push(c)
  }
  return out.length >= 3 ? out : loop
}

export function loopsToPath(loops: Loop[]): string {
  let d = ''
  for (const loop of loops) {
    d += `M${loop[0][0]} ${loop[0][1]}`
    for (let i = 1; i < loop.length; i++) d += `L${loop[i][0]} ${loop[i][1]}`
    d += 'Z'
  }
  return d
}
