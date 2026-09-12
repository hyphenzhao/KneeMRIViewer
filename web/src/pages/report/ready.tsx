/**
 * "Every figure is on the page" - the contract the PDF renderer waits for.
 *
 * `networkidle` cannot see a canvas being painted or a data-URL image being
 * decoded, so each figure registers itself on mount and reports when it is
 * really drawn (or that it was skipped, or failed - both are final). The
 * root flips `data-report-ready` only when nothing is pending. A watchdog
 * resolves stragglers as timed out, so a stuck figure yields a PDF with a
 * visible gap instead of no PDF at all.
 */
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react'

export type FigureStatus = 'ok' | 'skipped' | 'error' | 'timeout'

interface Registry {
  register: (id: string) => void
  resolve: (id: string, status: FigureStatus) => void
  unregister: (id: string) => void
}

const Ctx = createContext<Registry | null>(null)

export interface ReadySummary { pending: number; total: number; skipped: number; failed: number }

export function FigureReadyProvider({ children, onChange, watchdogMs = 45000 }: {
  children: React.ReactNode
  onChange: (s: ReadySummary) => void
  watchdogMs?: number
}) {
  const state = useRef(new Map<string, FigureStatus | 'pending'>())
  const [, bump] = useState(0)
  const emit = useCallback(() => {
    let pending = 0, skipped = 0, failed = 0
    for (const s of state.current.values()) {
      if (s === 'pending') pending++
      else if (s === 'skipped') skipped++
      else if (s === 'error' || s === 'timeout') failed++
    }
    onChange({ pending, total: state.current.size, skipped, failed })
  }, [onChange])

  const registry = useMemo<Registry>(() => ({
    register: (id) => { if (!state.current.has(id)) { state.current.set(id, 'pending'); emit() } },
    resolve: (id, status) => { state.current.set(id, status); emit(); bump((n) => n + 1) },
    unregister: (id) => { state.current.delete(id); emit() },
  }), [emit])

  useEffect(() => {
    const t = setTimeout(() => {
      let changed = false
      for (const [id, s] of state.current) {
        if (s === 'pending') { state.current.set(id, 'timeout'); changed = true }
      }
      if (changed) { emit(); bump((n) => n + 1) }
    }, watchdogMs)
    return () => clearTimeout(t)
  }, [emit, watchdogMs])

  return <Ctx.Provider value={registry}>{children}</Ctx.Provider>
}

/** Register a figure for the lifetime of the component; call `done` once drawn. */
export function useFigureReady(id: string): (status: FigureStatus) => void {
  const reg = useContext(Ctx)
  useEffect(() => {
    reg?.register(id)
    return () => reg?.unregister(id)
  }, [reg, id])
  return useCallback((status: FigureStatus) => reg?.resolve(id, status), [reg, id])
}
