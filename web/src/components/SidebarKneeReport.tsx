/**
 * 膝关节报告 - the whole-knee, chaptered document, in the reading sidebar.
 *
 * Sits under 软骨报告. Shows how many findings chapters actually have a data
 * source (today: two of eight - the rest say 未评估 and wait for their models),
 * and opens the unified detailed report in its own named window.
 */
import { useEffect, useState } from 'react'

import { api, type ReportDocumentState } from '../api'

export function openReportWindow(segId: number, anchor?: string) {
  // Hash URL on purpose: the backend serves the bundle with no SPA fallback,
  // so /report/8 would 404 on a fresh load. One named window per case.
  const url = `${location.origin}${location.pathname}#/report/${segId}${anchor ? '?to=' + anchor : ''}`
  window.open(url, `mriv-report-${segId}`, 'width=1280,height=900')
}

export default function SidebarKneeReport({ segId }: { segId: number }) {
  const [state, setState] = useState<ReportDocumentState | null>(null)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')

  const refresh = async () => {
    try {
      setState(await api.reportDocumentState(segId))
    } catch (e) {
      setErr(String(e))
    }
  }

  useEffect(() => {
    let cancelled = false
    setState(null)
    setErr('')
    api.reportDocumentState(segId)
      .then((s) => { if (!cancelled) setState(s) })
      .catch((e) => { if (!cancelled) setErr(String(e)) })
    return () => { cancelled = true }
  }, [segId])

  const generate = async () => {
    setBusy(true)
    setErr('')
    try {
      await api.buildReportDocument(segId)
      await refresh()
    } catch (e) {
      setErr(String(e))
    } finally {
      setBusy(false)
    }
  }

  const reviewZh: Record<string, string> = {
    unreviewed: '未复核', edited: '已修改', approved: '已通过', rejected: '未通过',
  }

  return (
    <div className="sr sr-knee">
      <div className="section-title">膝关节报告</div>
      {err && <div className="err">{err}</div>}

      {state && !state.exists && !busy && (
        <div className="sr-intro">
          <button className="sr-primary" onClick={generate}>生成膝关节报告</button>
          <div className="sr-hint">
            按固定章节框架生成：骨 → 软骨 → 积液/滑膜 → 半月板 → 韧带肌腱 → 囊肿 → 软组织。
            目前只有软骨章有测量数据，其余章节标注「未评估」，等相应模型接入后自动填充。
          </div>
        </div>
      )}

      {busy && <div className="sr-intro"><div className="sr-hint">正在生成章节报告…</div></div>}

      {state && state.exists && !busy && (
        <div className="sr-intro">
          <dl className="report sr-state">
            <div className="sr-row">
              <dt>章节覆盖</dt>
              <dd>{state.findingsChaptersWithData} / {state.findingsChapters} 个所见章节有数据来源，其余标注「未评估」</dd>
            </div>
            <div className="sr-row">
              <dt>状态</dt>
              <dd>
                {state.status === 'partial' ? '部分章节生成失败' : '已生成'}
                {' · '}{reviewZh[state.reviewState ?? ''] ?? state.reviewState}
                {state.generation && state.generation > 1 ? ` · 第 ${state.generation} 次生成` : ''}
              </dd>
            </div>
            {state.cartilageStale && (
              <div className="sr-row">
                <dt>提示</dt>
                <dd className="sr-warn">软骨报告在本文档生成后被重新生成过，文档中的软骨章可能已过时，建议重新生成。</dd>
              </div>
            )}
          </dl>
          <div className="sr-buttons">
            <button onClick={generate} disabled={busy}
              title="重新生成会覆盖旧文档；医生的修改会保留并标记为需复核">
              重新生成报告
            </button>
            <button onClick={() => openReportWindow(segId)}>查看详细报告</button>
          </div>
          <div className="sr-hint">{state.generatedAt} · 算法自动生成，非诊断结论，须由医师复核</div>
        </div>
      )}
    </div>
  )
}
