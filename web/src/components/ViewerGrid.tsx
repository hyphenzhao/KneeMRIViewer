import { useEffect, useRef } from 'react'

import { PLANES, VP, getEngine, setupViewports, type LoadedSeries } from '../cs/viewer'

const TITLE: Record<string, string> = {
  axial: '横断面 Axial',
  coronal: '冠状面 Coronal',
  sagittal: '矢状面 Sagittal',
  volume3d: '3D 重建',
}

interface Props {
  series: LoadedSeries | null
  onReady: () => void
}

export default function ViewerGrid({ series, onReady }: Props) {
  const refs = useRef<Record<string, HTMLDivElement | null>>({})
  const done = useRef(false)

  useEffect(() => {
    if (done.current) return
    const els: Record<string, HTMLDivElement> = {}
    for (const k of [...PLANES, 'volume3d']) {
      const el = refs.current[k]
      if (!el) return
      els[k] = el
    }
    done.current = true
    setupViewports(els).then(onReady).catch(console.error)
  }, [onReady])

  useEffect(() => {
    let raf = 0
    const onResize = () => {
      cancelAnimationFrame(raf)
      // keepCamera=true. Passing false makes Cornerstone drop the camera on
      // every resize, and any layout change - a progress bar appearing, the
      // window being dragged - then leaves the 2D viewports blank until
      // something resets them by hand.
      raf = requestAnimationFrame(() => getEngine()?.resize(true, true))
    }
    window.addEventListener('resize', onResize)
    // The grid also changes size when the browser window does not, e.g. when a
    // sidebar section expands, so watch the element too.
    const ro = new ResizeObserver(onResize)
    for (const el of Object.values(refs.current)) if (el) ro.observe(el)
    return () => {
      cancelAnimationFrame(raf)
      window.removeEventListener('resize', onResize)
      ro.disconnect()
    }
  }, [])

  // The acquisition plane is the only one with real through-plane resolution.
  const acq = series?.plane
  const note = (k: string) => {
    if (!series) return null
    if (k === 'volume3d') return null
    if (k === acq) return null
    const th = series.spacing[2]
    if (series.anisotropy < 3) return null
    return `重建视图 · 层厚 ${th.toFixed(1)} mm`
  }

  return (
    <div className="viewgrid">
      {[...PLANES, 'volume3d'].map((k) => (
        <div className="vp" key={k}>
          <div
            className="cs"
            ref={(el) => {
              refs.current[k] = el
            }}
            onContextMenu={(e) => e.preventDefault()}
          />
          <span className="tag">
            {TITLE[k]}
            {k === acq ? ' · 采集平面' : ''}
          </span>
          {note(k) && <span className="note">{note(k)}</span>}
        </div>
      ))}
    </div>
  )
}

export { VP }
