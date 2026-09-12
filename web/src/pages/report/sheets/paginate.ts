/**
 * Deterministic A4 pagination of a flat block list.
 *
 * The report is a sequence of blocks with measured heights (mm). Sheets are
 * filled greedily: a block that does not fit starts the next sheet, a heading
 * is never left alone at the bottom, and a table is cut between rows with its
 * header repeated. Pure function, so the same document paginates the same way
 * on screen, in the browser's print dialog and in the server's PDF - one
 * source of truth for where the pages break.
 */
import type { ReactNode } from 'react'

export interface TableSpec {
  head: ReactNode
  rows: ReactNode[]
  className?: string
  /** A fragment on a non-empty sheet must carry at least this many rows. */
  minRows?: number
}

export interface Block {
  id: string
  /** Chapter this block belongs to, for wrapping fragments in a section. */
  chapter: string | null
  /** Set on the first block of a chapter: the wrapper gets the id and status class. */
  startsChapter?: { status: string; className?: string }
  kind: 'atomic' | 'heading' | 'table'
  breakBefore?: boolean
  /** Known height; the block is not measured (figures with fixed boxes). */
  fixedMm?: number
  node?: ReactNode
  table?: TableSpec
}

export interface Measured {
  height: number
  headHeight?: number
  rowHeights?: number[]
}

export interface Placed {
  block: Block
  fragment: number
  rowRange?: [number, number]
}

export interface Sheet {
  index: number
  placed: Placed[]
  usedMm: number
  overflow: boolean
}

const HEAD_ROOM = 2.5   // mm of slack per sheet so rounding never overflows a page

function blockHeight(b: Block, m: Record<string, Measured>): number {
  if (b.fixedMm != null) return b.fixedMm
  return m[b.id]?.height ?? 0
}

/** Height of what must come with a heading so it is not orphaned. */
function firstChunk(b: Block | undefined, m: Record<string, Measured>): number {
  if (!b) return 0
  if (b.kind === 'table') {
    const mm = m[b.id]
    const rows = mm?.rowHeights ?? []
    return (mm?.headHeight ?? 0) + rows.slice(0, Math.min(2, rows.length)).reduce((a, x) => a + x, 0)
  }
  return blockHeight(b, m)
}

export function paginate(blocks: Block[], m: Record<string, Measured>, pageMm = 263): Sheet[] {
  const page = pageMm - HEAD_ROOM
  const sheets: Sheet[] = []
  let cur: Sheet = { index: 0, placed: [], usedMm: 0, overflow: false }
  const flush = () => {
    if (cur.placed.length) sheets.push(cur)
    cur = { index: sheets.length, placed: [], usedMm: 0, overflow: false }
  }

  for (let i = 0; i < blocks.length; i++) {
    const b = blocks[i]
    if (b.breakBefore && cur.placed.length) flush()

    if (b.kind === 'table') {
      const mm = m[b.id]
      const head = mm?.headHeight ?? 0
      const rows = mm?.rowHeights ?? []
      const minRows = Math.min(b.table?.minRows ?? 2, rows.length)
      let at = 0
      let fragment = 0
      if (!rows.length) {
        if (cur.usedMm + head > page && cur.placed.length) flush()
        cur.placed.push({ block: b, fragment: 0, rowRange: [0, 0] })
        cur.usedMm += head
        continue
      }
      while (at < rows.length) {
        let avail = page - cur.usedMm - head
        let n = 0, used = 0
        while (at + n < rows.length && used + rows[at + n] <= avail) { used += rows[at + n]; n++ }
        if (n < minRows && cur.placed.length) {
          flush()
          continue
        }
        if (n === 0) {                      // a single row taller than a page: place it and flag
          n = 1; used = rows[at]
          cur.overflow = true
        }
        cur.placed.push({ block: b, fragment, rowRange: [at, at + n] })
        cur.usedMm += head + used
        at += n
        fragment++
        if (at < rows.length) flush()
      }
      continue
    }

    const h = blockHeight(b, m)
    const need = b.kind === 'heading' ? h + firstChunk(blocks[i + 1], m) : h
    if (cur.usedMm + need > page && cur.placed.length) flush()
    cur.placed.push({ block: b, fragment: 0 })
    cur.usedMm += h
    if (h > page) cur.overflow = true
  }
  flush()
  return sheets
}
