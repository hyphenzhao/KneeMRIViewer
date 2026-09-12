/**
 * 诊断报告 - one card, one button, one document.
 *
 * The cartilage report, the knee chapters and the detailed document are not
 * three things a reader has to assemble: they are one generation, run in
 * order, and the card shows which step is running. There are exactly two
 * buttons, and only once the report exists: 重新生成 and 查看详细报告.
 *
 * Visually this imitates the 影像报告 block above it - the same
 * `.section-title` + `dl.report` markup the radiologist's report uses - so a
 * clinician reads it as the same kind of object rather than a new widget.
 */
import { useEffect, useRef, useState } from 'react'

import { api, type ReportDocument, type ReportDocumentState } from '../api'

export function openReportWindow(segId: number, anchor?: string) {
  // Hash URL on purpose: the backend serves the bundle with no SPA fallback,
  // so /report/8 would 404 on a fresh load. One named window per case.
  const url = `${location.origin}${location.pathname}#/report/${segId}${anchor ? '?to=' + anchor : ''}`
  window.open(url, `mriv-report-${segId}`, 'width=1280,height=900')
}

const SOURCE: Record<string, { word: string; color: string; hint: string }> = {
  ok: { word: '模型生成', color: '#0ca30c', hint: '软骨章由语言模型撰写，已通过数值与分级回溯校验' },
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

/** Turn an engine exception into something a clinician can act on. */
function explain(error: string): string {
  if (/swapped|medial\/lateral/i.test(error)) {
    return '该病例的内侧/外侧软骨标签与影像中膝关节的实际位置不符，疑似标注时左右侧弄反了。'
      + '请先核对分割标签（4/6 应为内侧，5/7 应为外侧）后重新生成。'
  }
  if (/not materialised/i.test(error)) return '该分割尚未生成标签体数据。'
  return error
}

/**
 * One line per item, with the hospital sentences marked as such.
 *
 * The detailed report labels them as 放射科报告; this summary must too, or a
 * reader here cannot tell a radiologist observation from our computation.
 */
function itemLines(items: Array<{ text: string; textEffective?: string; origin: string }>): string {
  return items
    .map((i) => (i.textEffective ?? i.text) + (i.origin === 'radiologist' ? '（放射科报告）' : ''))
    .join('\n')
}


export default function SidebarReport({ segId }: { segId: number }) {
  const [state, setState] = useState<ReportDocumentState | null>(null)
  const [doc, setDoc] = useState<ReportDocument | null>(null)
  const [step, setStep] = useState<{ label: string; done: number; total: number } | null>(null)
  const [err, setErr] = useState('')
  const alive = useRef(true)

  useEffect(() => {
    alive.current = true
    setDoc(null); setState(null); setErr(''); setStep(null)
    ;(async () => {
      try {
        const st = await api.reportDocumentState(segId)
        if (!alive.current) return
        setState(st)
        if (st.exists) {
          const d = await api.reportDocument(segId)
          if (alive.current) setDoc(d)
        }
      } catch (e) {
        if (alive.current) setErr(String(e))
      }
    })()
    return () => { alive.current = false }
  }, [segId])

  const generate = async () => {
    setErr('')
    setStep({ label: '准备中', done: 0, total: 1 })
    try {
      const d = await api.streamReportDocument(segId, '', (ev) => {
        if (alive.current) setStep({ label: ev.labelZh, done: ev.done, total: ev.total })
      })
      if (!alive.current) return
      setDoc(d)
      setState(await api.reportDocumentState(segId))
    } catch (e) {
      if (alive.current) setErr(String(e))
    } finally {
      if (alive.current) setStep(null)
    }
  }

  const chapters = doc?.rendered.chapters ?? []
  const byId = (id: string) => chapters.find((c) => c.id === id)
  const cartilage = byId('cartilage')
  const impression = byId('impression')
  const advice = byId('advice')
  const src = cartilage?.sourceRef?.status
    ? (SOURCE[cartilage.sourceRef.status] ?? SOURCE.failed)
    : null
  const withData = state?.findingsChaptersWithData ?? 0
  const totalFindings = state?.findingsChapters ?? 0

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

      {err && <div className="err">{explain(err)}</div>}

      {!doc && !step && !err && (
        <div className="sr-intro">
          <button className="sr-primary" onClick={generate}>生成诊断报告</button>
          <div className="sr-hint">
            依次完成：软骨形态学测量 → 软骨报告 → 膝关节各章节。首次约 40 秒，
            生成后会缓存，再次打开该病例不再消耗 token。
          </div>
        </div>
      )}

      {step && (
        <div className="sr-intro">
          <div className="sr-progress">
            <div className="sr-progress-bar"
              style={{ width: `${Math.round((100 * step.done) / Math.max(step.total, 1))}%` }} />
          </div>
          <div className="sr-hint">
            正在生成：{step.label}（{step.done}/{step.total}）
          </div>
        </div>
      )}

      {doc && !step && (
        <>
          <dl className="report">
            {cartilage?.proseEffective && (
              <div className="sr-row">
                <dt>关节软骨</dt>
                <dd>{cartilage.proseEffective}</dd>
              </div>
            )}
            {impression && impression.items.length > 0 && (
              <div className="sr-row">
                <dt>印象</dt>
                <dd>{itemLines(impression.items)}</dd>
              </div>
            )}
            {advice && advice.items.length > 0 && (
              <div className="sr-row">
                <dt>建议</dt>
                <dd>{itemLines(advice.items)}</dd>
              </div>
            )}
            <div className="sr-row">
              <dt>章节覆盖</dt>
              <dd>
                {withData} / {totalFindings} 个所见章节有数据来源，其余标注「未评估」，
                等相应模型接入后自动填充。
              </dd>
            </div>
          </dl>

          {doc.status === 'partial' && (
            <div className="err">部分章节生成失败：{doc.error}</div>
          )}
          {state?.cartilageStale && (
            <div className="sr-hint sr-warn">
              软骨报告在本文档生成后被单独重新生成过，建议重新生成以保持一致。
            </div>
          )}

          <div className="sr-foot">
            <div className="sr-buttons">
              <button onClick={generate}
                title="会重新测量并重新调用模型，消耗 token；医师的修改会保留并标为需复核">
                重新生成
              </button>
              <button onClick={() => openReportWindow(segId)}>查看详细报告</button>
            </div>
            <div className="sr-hint">
              {doc.generatedAt}
              {doc.generation > 1 ? ` · 第 ${doc.generation} 次生成` : ''}
              {' · '}本报告由算法生成，非诊断结论，须由医师复核
            </div>
          </div>
        </>
      )}
    </div>
  )
}
