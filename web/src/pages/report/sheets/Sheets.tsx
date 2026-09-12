/**
 * Measure blocks, then lay them onto A4 sheets.
 *
 * `Measurer` renders every measurable block once, off-screen at the sheet's
 * content width, and reads its height (tables: header and every row). Blocks
 * with a fixed millimetre height - the figures - are not rendered here at
 * all, so a figure mounts exactly once, on its sheet. `SheetView` renders
 * the paginated result: one `.rp-sheet` per page with its own header and
 * footer, chapter fragments wrapped so anchors and status classes land on
 * the first fragment only.
 */
import { useLayoutEffect, useRef } from 'react'

import type { Block, Measured, Placed, Sheet, TableSpec } from './paginate'

export const PX_PER_MM = 96 / 25.4
export const A4 = { w: 210, h: 297, top: 16, right: 14, bottom: 18, left: 14 }
export const CONTENT_H_MM = A4.h - A4.top - A4.bottom     // 263
export const CONTENT_W_MM = A4.w - A4.left - A4.right     // 182

function TableFragment({ spec, range }: { spec: TableSpec; range?: [number, number] }) {
  const rows = range ? spec.rows.slice(range[0], range[1]) : spec.rows
  return (
    <table className={spec.className ?? 'rp-table'}>
      <thead>{spec.head}</thead>
      <tbody>{rows}</tbody>
    </table>
  )
}

export function Measurer({ blocks, onMeasured }: {
  blocks: Block[]
  onMeasured: (m: Record<string, Measured>) => void
}) {
  const root = useRef<HTMLDivElement>(null)
  useLayoutEffect(() => {
    let cancelled = false
    const measure = () => {
      if (cancelled || !root.current) return
      const out: Record<string, Measured> = {}
      for (const el of root.current.querySelectorAll<HTMLElement>('[data-block]')) {
        const id = el.dataset.block!
        const m: Measured = { height: el.getBoundingClientRect().height / PX_PER_MM }
        const table = el.querySelector('table')
        if (table) {
          m.headHeight = (table.tHead?.getBoundingClientRect().height ?? 0) / PX_PER_MM
          m.rowHeights = [...table.tBodies[0]?.rows ?? []].map((r) => r.getBoundingClientRect().height / PX_PER_MM)
        }
        out[id] = m
      }
      onMeasured(out)
    }
    const fonts = (document as Document & { fonts?: { ready: Promise<unknown> } }).fonts
    if (fonts?.ready) fonts.ready.then(() => requestAnimationFrame(measure))
    else requestAnimationFrame(measure)
    return () => { cancelled = true }
  }, [blocks, onMeasured])

  return (
    <div ref={root} className="rp-measure" aria-hidden="true">
      {blocks.filter((b) => b.fixedMm == null).map((b) => (
        <div key={b.id} data-block={b.id} className="rp-measure-block">
          {b.kind === 'table' && b.table ? <TableFragment spec={b.table} /> : b.node}
        </div>
      ))}
    </div>
  )
}

function renderPlaced(p: Placed) {
  const b = p.block
  if (b.kind === 'table' && b.table) {
    return <TableFragment key={`${b.id}#${p.fragment}`} spec={b.table} range={p.rowRange} />
  }
  return <div key={b.id} className="rp-block" data-block-id={b.id}>{b.node}</div>
}

/** Consecutive blocks of one chapter become one section fragment. */
function groupByChapter(placed: Placed[]): Array<{ chapter: string | null; starts: Block['startsChapter'] | undefined; items: Placed[] }> {
  const groups: Array<{ chapter: string | null; starts: Block['startsChapter'] | undefined; items: Placed[] }> = []
  for (const p of placed) {
    const last = groups[groups.length - 1]
    if (last && last.chapter === p.block.chapter && p.block.chapter != null && !p.block.startsChapter) {
      last.items.push(p)
    } else {
      groups.push({ chapter: p.block.chapter, starts: p.block.startsChapter, items: [p] })
    }
  }
  return groups
}

export function SheetView({ sheets, view, header, footer }: {
  sheets: Sheet[]
  view: string
  header: (i: number, n: number) => React.ReactNode
  footer: React.ReactNode
}) {
  const root = useRef<HTMLDivElement>(null)
  // Overflow guard: a sheet whose content is taller than its box is a bug in
  // the page plan, not something to hide. Print view only - on screen the
  // sheets grow.
  useLayoutEffect(() => {
    if (!root.current) return
    for (const el of root.current.querySelectorAll<HTMLElement>('.rp-sheet')) {
      const over = view === 'print' && el.scrollHeight > el.clientHeight + 1
      if (over) {
        el.dataset.overflow = '1'
        console.warn(`[report] sheet ${el.dataset.sheet} overflows by ${el.scrollHeight - el.clientHeight}px`)
      } else {
        delete el.dataset.overflow
      }
    }
  }, [sheets, view])

  return (
    <div ref={root} className="rp-sheets">
      {sheets.map((s) => (
        <section key={s.index} className="rp-sheet" data-sheet={s.index + 1}>
          <div className="rp-sheet-head">{header(s.index + 1, sheets.length)}</div>
          <div className="rp-sheet-body">
            {groupByChapter(s.placed).map((g, gi) => {
              const nodes = g.items.map(renderPlaced)
              if (g.chapter == null) return <div key={gi} className="rp-flow">{nodes}</div>
              if (g.starts) {
                return (
                  <section key={gi} id={'ch-' + g.chapter} data-chapter={g.chapter}
                    className={`rp-chapter rp-chapter-${g.starts.status}${g.starts.className ? ' ' + g.starts.className : ''}`}>
                    {nodes}
                  </section>
                )
              }
              return <section key={gi} className="rp-chapter-cont" data-chapter={g.chapter}>{nodes}</section>
            })}
          </div>
          <div className="rp-sheet-foot">{footer}</div>
        </section>
      ))}
    </div>
  )
}
