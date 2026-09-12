/**
 * 诊断报告正文 - the four-section clinical report, with its provenance visible.
 *
 * Two things are deliberately never hidden:
 *
 * - **Which path wrote it.** A report from the model and a report from the
 *   built-in template read almost the same; only the badge distinguishes them,
 *   so the badge is always shown rather than only on the unhappy path.
 * - **What was sent.** The exact de-identified payload is one click away. A
 *   privacy claim nobody can inspect is a promise, not a control.
 */
import { useEffect, useState } from 'react'

import { api, type AiReport, type AiStatus } from '../api'

const STATUS: Record<string, { word: string; color: string; hint: string }> = {
  ok: { word: '模型生成', color: '#0ca30c', hint: '由语言模型撰写，已通过数值回溯校验' },
  fallback: {
    word: '内置模板', color: '#fab219',
    hint: '未调用语言模型（未启用或不可达），由固定模板按测量值生成',
  },
  rejected_by_guardrail: {
    word: '模型被拒', color: '#ec835a',
    hint: '模型输出未通过校验（出现了数据中不存在的数字或结构），已回退到内置模板',
  },
  failed: { word: '生成失败', color: '#d03b3b', hint: '生成过程出错' },
}

const SECTIONS: Array<[keyof AiReport, string]> = [
  ['findings', '影像所见'],
  ['quant', '定量测量'],
  ['impression', '印象'],
  ['advice', '建议'],
]

export default function ReportCard({ segId }: { segId: number }) {
  const [report, setReport] = useState<AiReport | null>(null)
  const [status, setStatus] = useState<AiStatus | null>(null)
  const [busy, setBusy] = useState(true)
  const [err, setErr] = useState('')
  const [showPayload, setShowPayload] = useState(false)
  const [reviewer, setReviewer] = useState('')

  useEffect(() => {
    let cancelled = false
    setBusy(true)
    ;(async () => {
      try {
        const s = await api.aiStatus().catch(() => null)
        if (!cancelled && s) setStatus(s)
        // Ask whether a report exists before asking for it. Fetching a
        // not-yet-generated report would 404, and a 404 on the happy path is
        // noise in the console that trains people to ignore real errors.
        const state = await api.aiReportState(segId).catch(() => ({ exists: false }))
        const r = state.exists ? await api.aiReport(segId) : null
        if (!cancelled) setReport(r)
      } catch (e) {
        if (!cancelled) setErr(String(e))
      } finally {
        if (!cancelled) setBusy(false)
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

  const review = async (state: string) => {
    if (!report) return
    setBusy(true)
    try {
      setReport(await api.reviewAiReport(report.id, {
        reviewState: state, reviewedBy: reviewer,
      }))
    } catch (e) {
      setErr(String(e))
    } finally {
      setBusy(false)
    }
  }

  const st = report ? (STATUS[report.status] ?? STATUS.failed) : null

  return (
    <section className="mx-card mx-report">
      <div className="mx-report-head">
        <h2>诊断报告正文</h2>
        {st && (
          <span className="mx-badge" style={{ color: st.color }} title={st.hint}>
            ● {st.word}
          </span>
        )}
        {report && (
          <span className="mx-kv">
            {report.createdAt}
            {report.version > 1 ? ` · 第 ${report.version} 次生成` : ''}
          </span>
        )}
        <button onClick={generate} disabled={busy} style={{ marginLeft: 'auto' }}>
          {busy ? '生成中…' : report ? '重新生成' : '生成报告'}
        </button>
      </div>

      {status && !status.enabled && (
        <p className="mx-note">
          未启用语言模型（<code>ai.enabled = false</code>），报告由内置模板按测量值确定性生成。
          断网部署可将 <code>ai.base_url</code> 指向院内本地模型。
        </p>
      )}
      {status && status.enabled && status.modelPresent === false && (
        <p className="mx-warn">
          配置的模型 <code>{status.model}</code> 在服务端不存在。{status.detail}
        </p>
      )}
      {err && <p className="err">{err}</p>}

      {!report && !busy && (
        <p className="mx-note">尚未生成报告。点击「生成报告」按当前测量结果撰写。</p>
      )}

      {report && (
        <>
          {report.status === 'rejected_by_guardrail' && (
            <div className="mx-guard">
              <b>模型输出已被拒收</b>，下面显示的是内置模板生成的报告。
              <div className="mx-note">原因：{report.error}</div>
              <div className="mx-note">
                数值回溯校验要求正文中的每个数字都能在测量结果中找到。
                这条规则拦下的正是最危险的失败：一段读起来完全合理、但数字是编出来的文字。
              </div>
            </div>
          )}

          <div className="mx-sections">
            {SECTIONS.map(([key, title]) => (
              <div key={key} className="mx-section">
                <h3>{title}</h3>
                {String(report[key] ?? '').split('\n').filter(Boolean).map((line, i) => (
                  <p key={i}>{line}</p>
                ))}
              </div>
            ))}
          </div>

          <p className="mx-disclaim-block">{report.disclaimer}</p>

          <div className="mx-review">
            <span className="mx-kv">医师复核</span>
            <input placeholder="复核医师姓名" value={reviewer}
              onChange={(e) => setReviewer(e.target.value)} />
            <button onClick={() => review('approved')} disabled={busy || !reviewer}>
              通过
            </button>
            <button onClick={() => review('rejected')} disabled={busy || !reviewer}>
              不通过
            </button>
            <span className="mx-kv">
              当前状态：{{
                unreviewed: '未复核', approved: '已通过', rejected: '未通过',
                edited: '已修改',
              }[report.reviewState] ?? report.reviewState}
              {report.reviewedBy ? ` · ${report.reviewedBy}` : ''}
              {report.reviewedAt ? ` · ${report.reviewedAt}` : ''}
            </span>
          </div>

          <details className="mx-explain" open={showPayload}
            onToggle={(e) => setShowPayload((e.target as HTMLDetailsElement).open)}>
            <summary>
              查看实际发送给模型的内容（去标识化审计）
            </summary>
            <p className="mx-note">
              下面是本次生成时**真正传输出去的全部内容**。它由白名单逐字段构造，
              并在建立连接之前经过去标识化断言：不含姓名、病历号、任何 ID、
              检查号、UID、日期、文件路径、数据集名称、放射科原始报告或任何像素数据。
              年龄只保留十年段。
              {report.provider === 'builtin'
                && '（本次未联网，以下内容仅为模板输入。）'}
            </p>
            <pre className="mx-payload">
              {report.input ? JSON.stringify(report.input, null, 1) : '（无）'}
            </pre>
          </details>
        </>
      )}
    </section>
  )
}
