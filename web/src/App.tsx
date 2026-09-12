import { useCallback, useEffect, useRef, useState } from 'react'

import {
  api,
  type LabelSet,
  type LabelStat,
  type PatientDetail,
  type PatientRow,
  type SeriesRow,
} from './api'
import AiSettings from './components/AiSettings'
import LabelPanel from './components/LabelPanel'
import SidebarReport from './components/SidebarReport'
import PatientList from './components/PatientList'
import ViewerGrid from './components/ViewerGrid'
import {
  applyLabelVisibility,
  applySurfaceVisibility,
  clearSegmentations,
  loadSegmentation,
  loadSeries,
  loadSurfaces,
  resetCameras,
  type LoadedSegmentation,
  type LoadedSeries,
} from './cs/viewer'
import { setProgressReporter, type LoadPhase } from './cs/volumeLoader'

/**
 * What is shown when a segmentation opens: the cartilage plates only. The
 * bones are the reference the cartilage sits on, and drawn by default they
 * hide exactly the surfaces a reader opened the case to look at; one click
 * in the label panel brings them back.
 */
function defaultVisible(s: LoadedSegmentation): Set<number> {
  const bone = new Set(
    (s.labelSet?.labels ?? []).filter((l) => l.group === 'bone').map((l) => l.value))
  return new Set(s.presentValues.filter((v) => !bone.has(v)))
}

export default function App() {
  const [patient, setPatient] = useState<PatientDetail | null>(null)
  const [seriesRow, setSeriesRow] = useState<SeriesRow | null>(null)
  const [loaded, setLoaded] = useState<LoadedSeries | null>(null)
  const [aiOpen, setAiOpen] = useState(false)
  const [seg, setSeg] = useState<LoadedSegmentation | null>(null)
  const [segId, setSegId] = useState<number | null>(null)
  const [stats, setStats] = useState<LabelStat[]>([])
  const [visible, setVisible] = useState<Set<number>>(new Set())
  const [soloed, setSoloed] = useState<number | null>(null)
  const [showSurfaces, setShowSurfaces] = useState(true)
  const [status, setStatus] = useState<string>('')
  const [progress, setProgress] = useState<LoadPhase | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [engineReady, setEngineReady] = useState(false)
  const surfaceLoaded = useRef<string | null>(null)

  const onEngineReady = useCallback(() => setEngineReady(true), [])

  // Loaders push byte/step counts here; the bar under the toolbar shows them.
  useEffect(() => {
    setProgressReporter((p) => setProgress(p.stage === 'idle' ? null : p))
    return () => setProgressReporter(() => {})
  }, [])

  const pickPatient = async (p: PatientRow) => {
    setError(null)
    setStatus('读取病例…')
    try {
      const detail = await api.patient(p.id)
      setPatient(detail)
      const first = detail.studies.flatMap((s) => s.series)[0] ?? null
      setSeriesRow(first)
    } catch (e) {
      setError(String(e))
    } finally {
      setStatus('')
    }
  }

  // Load the selected series (and its first segmentation) into the viewports.
  useEffect(() => {
    if (!engineReady || !seriesRow) return
    let cancelled = false
    ;(async () => {
      setError(null)
      // Drop the previous case's overlay and 3D surfaces *before* anything
      // else. If the incoming series has no segmentation, the viewer must show
      // no segmentation - not the last patient's.
      clearSegmentations()
      setSeg(null)
      setSegId(null)
      setStats([])
      setVisible(new Set())
      setSoloed(null)
      surfaceLoaded.current = null
      try {
        setStatus('加载影像体数据…')
        const ls = await loadSeries(seriesRow.id)
        if (cancelled) return
        setLoaded(ls)
        resetCameras()

        const first = seriesRow.segmentations?.[0]
        setSegId(first ? first.id : null)
        if (first) {
          setStatus('加载分割标注…')
          const s = await loadSegmentation(first.id, ls.volumeId)
          if (cancelled) return
          // Set these together. An await between setSeg and setVisible would
          // let the surface effect run against the previous (empty) visibility
          // set and hide every 3D surface the moment it loaded.
          const vis = defaultVisible(s)
          setSeg(s)
          setStats(s.stats)
          setVisible(vis)
          applyLabelVisibility(s.segmentationId, s.presentValues, vis)
        }
      } catch (e) {
        if (!cancelled) setError(String(e))
      } finally {
        if (!cancelled) {
          setStatus('')
          setProgress(null)
        }
      }
    })()
    return () => {
      cancelled = true
    }
  }, [engineReady, seriesRow])

  // 3D surfaces are loaded lazily so the 2D planes appear first.
  useEffect(() => {
    if (!seg || !showSurfaces) return
    if (surfaceLoaded.current === seg.segmentationId) return
    surfaceLoaded.current = seg.segmentationId
    setStatus('加载 3D 表面…')
    loadSurfaces(seg, visible)
      .catch((e) => setError(String(e)))
      .finally(() => {
        setStatus('')
        setProgress(null)
      })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [seg, showSurfaces])

  const applyVisibility = (next: Set<number>) => {
    setVisible(next)
    if (!seg) return
    applyLabelVisibility(seg.segmentationId, seg.presentValues, next)
    applySurfaceVisibility(`${seg.segmentationId}:surface`, seg.presentValues, next)
  }

  const toggle = (v: number) => {
    const next = new Set(visible)
    next.has(v) ? next.delete(v) : next.add(v)
    setSoloed(null)
    applyVisibility(next)
  }

  const toggleAll = (on: boolean) => {
    setSoloed(null)
    applyVisibility(on ? new Set(seg?.presentValues ?? []) : new Set())
  }

  const solo = (v: number | null) => {
    setSoloed(v)
    applyVisibility(v === null ? new Set(seg?.presentValues ?? []) : new Set([v]))
  }

  const onLabelChanged = (ls: LabelSet) => {
    setSeg((prev) => (prev ? { ...prev, labelSet: ls } : prev))
  }

  const allSeries = patient?.studies.flatMap((s) => s.series) ?? []

  return (
    <div className="app">
      <header className="topbar">
        <h1>MRI 软骨可视化平台</h1>
        {patient && (
          <span className="hint">
            {patient.external_id}
            {patient.name ? ` · ${patient.name}` : ''}
            {patient.age ? ` · ${patient.age}` : ''}
          </span>
        )}
        <span className="spacer" />
        <button onClick={() => setAiOpen(true)}
          title="配置报告使用的模型接口，可接入院内自有模型">
          AI 管理
        </button>
        {status && <span className="hint">{status}</span>}
        {loaded && (
          <span className="hint">
            {loaded.dimensions.join('×')} · {loaded.spacing.map((s) => s.toFixed(2)).join('/')} mm
            {loaded.anisotropy >= 3 && ` · 各向异性 ${loaded.anisotropy.toFixed(1)}:1`}
            {' · '}
            {(loaded.gzipBytes / 1e6).toFixed(1)} MB
          </span>
        )}
      </header>

      <div className="main">
        <PatientList selectedId={patient?.id ?? null} onSelect={pickPatient} />

        <section className="center">
          <div className="toolbar">
            <select
              value={seriesRow?.id ?? ''}
              onChange={(e) =>
                setSeriesRow(allSeries.find((s) => s.id === Number(e.target.value)) ?? null)
              }
              disabled={!allSeries.length}
            >
              {allSeries.map((s) => (
                <option key={s.id} value={s.id}>
                  {s.description ?? s.path_rel} · {s.n_slices ?? s.n_instances} 层 ·{' '}
                  {s.acquisition_plane}
                  {s.segmentations.length ? ` · ${s.segmentations.length} 标注` : ''}
                </option>
              ))}
              {!allSeries.length && <option>（未选择病例）</option>}
            </select>

            {seriesRow && seriesRow.segmentations.length > 1 && (
              <select
                value={segId ?? ''}
                onChange={async (e) => {
                  const id = Number(e.target.value)
                  setSegId(id)
                  if (!loaded) return
                  setStatus('切换分割版本…')
                  try {
                    const s = await loadSegmentation(id, loaded.volumeId)
                    const vis = defaultVisible(s)
                    setSeg(s)
                    setStats(s.stats)
                    setVisible(vis)
                    applyLabelVisibility(s.segmentationId, s.presentValues, vis)
                    surfaceLoaded.current = null
                  } catch (err) {
                    setError(String(err))
                  } finally {
                    setStatus('')
                  }
                }}
              >
                {seriesRow.segmentations.map((g) => (
                  <option key={g.id} value={g.id}>
                    {g.display_name ?? g.origin}
                    {g.model_name ? ` (${g.model_name} ${g.model_version ?? ''})` : ''}
                  </option>
                ))}
              </select>
            )}

            <span className="sep" />
            <button onClick={() => resetCameras()}>重置视图</button>
            <button
              className={showSurfaces ? 'active' : ''}
              onClick={() => setShowSurfaces((v) => !v)}
              disabled={!seg}
            >
              3D 表面
            </button>
            <span className="spacer" style={{ flex: 1 }} />
            <span className="hint">左键窗宽窗位 · 右键缩放 · 中键平移 · 滚轮翻层</span>
          </div>

          {error && <div className="err">{error}</div>}

          {!patient ? (
            <div className="empty">
              从左侧选择一个病例开始
              <br />
              <span style={{ fontSize: 12 }}>勾选「只看有分割标注的」可直接跳到已标注病例</span>
            </div>
          ) : (
            <div className="viewwrap">
              {progress && (
                <div className="progress" title={`${progress.received} / ${progress.total}`}>
              <div
                className="bar"
                style={{
                  width: `${progress.total ? Math.min(100, (100 * progress.received) / progress.total) : 0}%`,
                }}
              />
                  <span className="lbl">
                    {progress.detail}
                    {progress.stage === 'mesh'
                      ? ` ${progress.received}/${progress.total}`
                      : ` ${(progress.received / 1e6).toFixed(1)} / ${(progress.total / 1e6).toFixed(1)} MB`}
                  </span>
                </div>
              )}
              <ViewerGrid series={loaded} onReady={onEngineReady} />
            </div>
          )}
        </section>

        <aside className="sidebar right">
          <div className="sidebar-body">
            {seg ? (
              <LabelPanel
                labelSet={seg.labelSet}
                stats={stats}
                present={seg.presentValues}
                visible={visible}
                soloed={soloed}
                onToggle={toggle}
                onToggleAll={toggleAll}
                onSolo={solo}
                onLabelChanged={onLabelChanged}
              />
            ) : (
              <div className="loading">
                {seriesRow ? '该序列没有分割标注' : '未选择序列'}
              </div>
            )}

            {seg && segId != null && <SidebarReport key={segId} segId={segId} />}

            {seriesRow?.geometry_warning && (
              <div className="err">几何提示：{seriesRow.geometry_warning}</div>
            )}

            {patient?.report && (
              <>
                <div className="section-title">影像报告</div>
                <dl className="report">
                  {patient.report.findings && (
                    <>
                      <dt>影像所见</dt>
                      <dd>{patient.report.findings}</dd>
                    </>
                  )}
                  {patient.report.diagnosis && (
                    <>
                      <dt>诊断</dt>
                      <dd>{patient.report.diagnosis}</dd>
                    </>
                  )}
                </dl>
              </>
            )}
          </div>
        </aside>
      </div>
      {aiOpen && <AiSettings onClose={() => setAiOpen(false)} />}
    </div>
  )
}
