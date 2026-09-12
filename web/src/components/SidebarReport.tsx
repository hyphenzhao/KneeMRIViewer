/**
 * 诊断报告 - the AI report, in the reading sidebar under the label list.
 *
 * Visually this deliberately imitates the existing 影像报告 block: the same
 * `.section-title` + `dl.report` markup the radiologist's report uses, so a
 * clinician reads it as the same kind of object rather than as a new widget.
 * The dashboard's `.mx-card` styling is not reused - it is scaled for a 1180px
 * column and looks wrong in 300px.
 *
 * The report is cached server-side, one row per segmentation. Opening a case
 * that already has one costs nothing; only 重新生成 spends tokens, which is why
 * that is a separate, explicitly labelled button.
 */
import { useEffect, useState } from 'react'

import { api, type AiReport } from '../api'

const SOURCE: Record<string, { word: string; color: string; hint: string }> = {
  ok: { word: '模型生成', color: '#0ca30c', hint: '由语言模型撰写，已通过数值回溯校验' },
  fallback: {
    word: '内置模板', color: '#fab219',
    hint: '未调用语言模型（未启用或不可达），由固定模板按测量值确定性生成',
  },
  rejected_by_guardrail: {
    word: '模型被拒', color: '#ec835a',
    hint: '模型输出出现了测量结果中不存在的数字或结构，已整份拒收并回退到内置模板',
  },
  failed: { word: '生成失败', color: '#d03b3b', hint: '生成过程出错' },
}

const SECTIONS: Array<[keyof AiReport, string]> = [
  ['findings', '影像所见'],
  ['quant', '定量测量'],
  ['impression', '印象'],
  ['advice', '建议'],
]

/** Turn the engine's exception text into something a clinician can act on. */
function explainFailure(error: string): string {
  if (/swapped|medial\/lateral/i.test(error)) {
    return '该病例的内侧/外侧软骨标签与影像中膝关节的实际位置不符，疑似标注时左右侧弄反了。'
      + '请先核对分割标签（4/6 应为内侧，5/7 应为外侧）后重新生成。'
  }
  if (/not materialised/i.test(error)) return '该分割尚未生成标签体数据。'
  return error
}

export default function SidebarReport({ segId }: { segId: number }) {
  const [report, setReport] = useState<AiReport | null>(null)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')

  useEffect(() => {
    let cancelled = false
    setReport(null)
    setErr('')
    ;(async () => {
      try {
        // Ask whether one exists before fetching it, so the normal "not
        // generated yet" case is not a 404 in the console.
        const state = await api.aiReportState(segId)
        if (cancelled || !state.exists) return
        const r = await api.aiReport(segId)
        if (!cancelled) setReport(r)
      } catch (e) {
        if (!cancelled) setErr(String(e))
      }
    })()
    return () => { cancelled = true }
  }, [segId])

  const generate = async () => {
    setBusy(true)
    setErr('')
    try {
      setReport(await api.buildAiReport(segId))
    } catch (e) {
      setErr(String(e))
    } finally {
      setBusy(false)
    }
  }

  const openDetail = () => {
    // Must be the hash URL: the backend serves the bundle through StaticFiles
    // with no SPA fallback, so /metrics/8 would 404 on a fresh load.
    // The window name makes repeated clicks reuse one popup.
    const url = `${location.origin}${location.pathname}#/metrics/${segId}`
    window.open(url, `mriv-metrics-${segId}`, 'width=1280,height=900')
  }

  const src = report ? (SOURCE[report.status] ?? SOURCE.failed) : null

  return (
    <div className="sr">
      <div className="section-title">
        诊断报告
        {src && (
          <span className="sr-badge" style={{ color: src.color }} title={src.hint}>
            ● {src.word}
          </span>
        )}
      </div>

      {err && <div className="err">{explainFailure(err)}</div>}

      {!report && !busy && !err && (
        <div className="sr-intro">
          <button className="sr-primary" onClick={generate}>生成软骨诊断报告</button>
          <div className="sr-hint">
            测量 22 个亚区的厚度、体积与内外侧对称性并撰写报告，首次约 30 秒。
            生成后会缓存，再次打开该病例不再消耗 token。
          </div>
        </div>
      )}

      {busy && <div className="sr-intro"><div className="sr-hint">正在测量并撰写报告…</div></div>}

      {report && (
        <>
          <dl className="report">
            {SECTIONS.map(([key, title]) => {
              const text = String(report[key] ?? '').trim()
              if (!text) return null
              return (
                <div key={key} className="sr-row">
                  <dt>{title}</dt>
                  <dd>{text}</dd>
                </div>
              )
            })}
          </dl>
          <div className="sr-foot">
            <div className="sr-buttons">
              <button onClick={generate} disabled={busy}
                title="会重新调用模型并消耗 token，旧报告将被覆盖">
                重新生成报告
              </button>
              <button onClick={openDetail}>查看详细报告</button>
            </div>
            <div className="sr-hint">
              {report.createdAt}
              {report.version > 1 ? ` · 第 ${report.version} 次生成` : ''}
              {' · '}本报告由算法生成，非诊断结论，须由医师复核
            </div>
          </div>
        </>
      )}
    </div>
  )
}
