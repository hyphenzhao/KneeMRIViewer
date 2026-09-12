/**
 * 膝关节 MRI 结构化报告  #/report/<segId>?view=web|print|edit&to=<chapterId>&pdf=1
 *
 * One document, fixed chapters, three views of the same rendered data:
 *
 * - web:   dark, scrollable, with the interactive dashboard one click away
 * - print: white A4 column; Chromium's print engine paginates it (browser
 *          Ctrl+P today, the server-side PDF endpoint renders the same view)
 * - edit:  every prose block, number and grade can be overridden by a named
 *          doctor; the original stays visible next to the change
 *
 * Chapters whose source is `pending` never carry prose. They show the
 * placeholder and, when the hospital's own report exists, its sentences under
 * a 「放射科报告」 label - attributed, never blended in.
 */
import { useEffect, useMemo, useState } from 'react'

import { api, type ReportDocument, type RenderedChapter } from '../api'

type View = 'web' | 'print' | 'edit'

const GRADE_ZH: Record<string, string> = {
  '0': '0 级', II: 'II 级', III: 'III 级', IV: 'IV 级', 未评估: '未评估',
}
const CONF = {
  high: { glyph: '●', word: '可信' },
  medium: { glyph: '◐', word: '一般' },
  low: { glyph: '○', word: '存疑' },
} as const

function parseHash() {
  const m = /^#\/report\/(\d+)(?:\?(.*))?$/.exec(window.location.hash)
  const q = new URLSearchParams(m?.[2] ?? '')
  return {
    segId: m ? Number(m[1]) : null,
    view: ((q.get('view') as View) || 'web') as View,
    to: q.get('to') || '',
    pdf: q.get('pdf') === '1',
  }
}

export default function ReportPage() {
  const [route, setRoute] = useState(parseHash())
  const [doc, setDoc] = useState<ReportDocument | null>(null)
  const [busy, setBusy] = useState(true)
  const [err, setErr] = useState('')
  const [pdfOk, setPdfOk] = useState<{ available: boolean; detail: string | null } | null>(null)
  useEffect(() => { api.pdfStatus().then(setPdfOk).catch(() => setPdfOk({ available: false, detail: null })) }, [])
  const [editor, setEditor] = useState(() => {
    try { return localStorage.getItem('mriv.editor') ?? '' } catch { return '' }
  })

  useEffect(() => {
    const on = () => setRoute(parseHash())
    window.addEventListener('hashchange', on)
    return () => window.removeEventListener('hashchange', on)
  }, [])

  const { segId, view, to, pdf } = route

  useEffect(() => {
    if (segId == null) return
    let cancelled = false
    setBusy(true)
    setErr('')
    ;(async () => {
      try {
        const st = await api.reportDocumentState(segId)
        // Generate on first visit: a link that only works after someone pressed
        // a button elsewhere is a link that gets reported as broken.
        const d = st.exists ? await api.reportDocument(segId) : await api.buildReportDocument(segId)
        if (!cancelled) setDoc(d)
      } catch (e) {
        if (!cancelled) setErr(String(e))
      } finally {
        if (!cancelled) setBusy(false)
      }
    })()
    return () => { cancelled = true }
  }, [segId])

  useEffect(() => {
    if (doc && to) document.getElementById('ch-' + to)?.scrollIntoView({ block: 'start' })
  }, [doc, to])

  useEffect(() => {
    if (doc) document.title = `膝关节报告 ${doc.header.caseLabel ?? ''}`.trim()
  }, [doc])

  const setView = (v: View) => {
    const q = new URLSearchParams()
    if (v !== 'web') q.set('view', v)
    window.location.hash = `#/report/${segId}${q.toString() ? '?' + q : ''}`
  }

  const regenerate = async () => {
    if (segId == null) return
    setBusy(true)
    setErr('')
    try {
      setDoc(await api.buildReportDocument(segId, editor))
    } catch (e) {
      setErr(String(e))
    } finally {
      setBusy(false)
    }
  }

  const review = async (state: 'approved' | 'rejected') => {
    if (!doc) return
    if (!editor.trim()) { setErr('请先填写复核医师姓名'); return }
    setBusy(true)
    try {
      setDoc(await api.reviewReportDocument(doc.id, state, editor))
    } catch (e) {
      setErr(String(e))
    } finally {
      setBusy(false)
    }
  }

  const override = async (target: { chapter: string; kind: string; key: string }, value: unknown, reason = '') => {
    if (!doc) return
    if (!editor.trim()) { setErr('请先填写修改医师姓名'); return }
    setErr('')
    try {
      setDoc(await api.setOverride(doc.id, target, value, editor, reason))
    } catch (e) {
      setErr(String(e))
    }
  }

  const revoke = async (overrideId: string) => {
    if (!doc) return
    try {
      setDoc(await api.revokeOverride(doc.id, overrideId, editor))
    } catch (e) {
      setErr(String(e))
    }
  }

  const rendered = doc?.rendered
  const chapters = useMemo(() => rendered?.chapters ?? [], [rendered])
  const findings = chapters.filter((c) => c.section === 'findings')
  const impression = chapters.find((c) => c.id === 'impression')
  const advice = chapters.find((c) => c.id === 'advice')
  const appendix = chapters.find((c) => c.section === 'appendix')

  return (
    <div className={`rp-root rp-${view}`} data-view={view}
      data-report-ready={doc && !busy ? '1' : '0'}>
      {!pdf && (
        <header className="rp-toolbar">
          <a className="mx-back" href="#/">← 返回阅片</a>
          <h1>膝关节 MRI 结构化报告</h1>
          <div className="rp-views" role="tablist">
            {(['web', 'print', 'edit'] as View[]).map((v) => (
              <button key={v} role="tab" aria-selected={view === v}
                className={view === v ? 'active' : ''} onClick={() => setView(v)}>
                {{ web: '网页视图', print: '打印视图', edit: '编辑视图' }[v]}
              </button>
            ))}
          </div>
          <span className="spacer" />
          <input className="rp-editor" placeholder="医师姓名（修改/复核时需要）" value={editor}
            onChange={(e) => { setEditor(e.target.value); try { localStorage.setItem('mriv.editor', e.target.value) } catch { /* private mode */ } }} />
          <button onClick={regenerate} disabled={busy || segId == null}
            title="重新计算并重写；医生的修改会保留并标为需复核">重新生成</button>
          <button onClick={() => review('approved')} disabled={busy || !doc}>复核通过</button>
          <button onClick={() => review('rejected')} disabled={busy || !doc}>不通过</button>
          <button onClick={() => window.print()} disabled={!doc}>浏览器打印</button>
          {segId != null && (
            pdfOk?.available
              ? <a className="rp-btn" href={api.reportPdfUrl(segId)}>下载 PDF</a>
              : <button disabled title={pdfOk?.detail ?? '服务器端 PDF 渲染不可用'}>下载 PDF</button>
          )}
          {segId != null && <a className="rp-link" href={`#/metrics/${segId}`} target="_blank" rel="noopener">交互看板 ↗</a>}
        </header>
      )}

      <main className="rp-page">
        {busy && !doc && <p className="loading">正在生成章节报告，首次约 30 秒…</p>}
        {err && <p className="err">{err}</p>}
        {doc && rendered && (
          <>
            <ReportHeader doc={doc} />
            {rendered.staleOverrideCount > 0 && (
              <div className="rp-guard">
                有 {rendered.staleOverrideCount} 处医师修改早于本次生成，已标记「请复核」。
              </div>
            )}
            {doc.status === 'partial' && (
              <div className="rp-guard">部分章节生成失败：{doc.error}。失败章节按「未生成」显示，不会被当作正常。</div>
            )}

            <h2 className="rp-section">影像所见</h2>
            {findings.map((c) => (
              <Chapter key={c.id} c={c} view={view} onOverride={override} onRevoke={revoke} />
            ))}

            {impression && <ItemsChapter c={impression} view={view} onOverride={override} onRevoke={revoke} />}
            {advice && <ItemsChapter c={advice} view={view} onOverride={override} onRevoke={revoke} />}

            <div className="rp-sign">
              <div>复核状态：{reviewZh(rendered.reviewState)}{rendered.reviewedBy ? ` · ${rendered.reviewedBy}` : ''}{rendered.reviewedAt ? ` · ${rendered.reviewedAt}` : ''}</div>
              <div>复核医师签名：______________　　日期：______________</div>
            </div>

            {appendix && <Appendix c={appendix} view={view} onOverride={override} onRevoke={revoke} />}

            <footer className="rp-foot">{rendered.disclaimerZh}</footer>
          </>
        )}
      </main>
    </div>
  )
}

function reviewZh(s: string | null | undefined) {
  return { unreviewed: '未复核', edited: '已修改，待复核', approved: '已通过', rejected: '未通过' }[s ?? ''] ?? (s ?? '')
}

function ReportHeader({ doc }: { doc: ReportDocument }) {
  const h = doc.header
  return (
    <div className="rp-head">
      <div className="rp-title">{h.titleZh ?? '膝关节 MRI 结构化报告'}</div>
      <dl className="rp-meta">
        <dt>侧别</dt><dd>{h.lateralityZh ?? '—'}</dd>
        <dt>性别 / 年龄段</dt><dd>{h.sexZh ?? '—'} / {h.ageBand ?? '—'}</dd>
        <dt>病例序号</dt><dd>{h.caseLabel ?? '—'}</dd>
        <dt>序列</dt><dd>{h.seriesDescription ?? '—'}</dd>
        <dt>生成时间</dt><dd>{h.generatedAt ?? doc.generatedAt}</dd>
        <dt>算法 / 模板</dt><dd>{h.algoVersion} · {h.templateKey} v{h.templateVersion}{doc.generation > 1 ? ` · 第 ${doc.generation} 次生成` : ''}</dd>
      </dl>
    </div>
  )
}

type OverrideFn = (t: { chapter: string; kind: string; key: string }, v: unknown, reason?: string) => void

function Mark({ o }: { o: RenderedChapter['proseOverride'] }) {
  if (!o) return null
  return (
    <span className="rp-mark">
      医师修改（原值 {o.original == null ? '—' : String(o.original)}）· {o.editor} · {o.editedAt}
      {o.noteZh ? ` · ${o.noteZh}` : ''}
      {o.reason ? ` · 理由：${o.reason}` : ''}
    </span>
  )
}

function StatusBadge({ c }: { c: RenderedChapter }) {
  const zh: Record<string, string> = {
    ok: c.proseOrigin === 'llm' ? '模型解读' : c.proseOrigin === 'template' ? '算法生成' : '算法生成',
    pending: '未评估', failed: '生成失败',
  }
  return <span className={`rp-badge rp-badge-${c.status}`}>{zh[c.status] ?? c.status}</span>
}

function Chapter({ c, view, onOverride, onRevoke }: {
  c: RenderedChapter; view: View; onOverride: OverrideFn; onRevoke: (id: string) => void
}) {
  const editing = view === 'edit'
  return (
    <section className={`rp-chapter rp-chapter-${c.status}`} id={'ch-' + c.id}>
      <h3>{c.titleZh} <StatusBadge c={c} /></h3>

      {c.status === 'pending' && (
        <p className="rp-placeholder">{c.placeholderZh}</p>
      )}
      {c.status === 'failed' && (
        <p className="rp-placeholder rp-failed">本章生成失败，未生成任何内容。{c.caveatsZh[0]}</p>
      )}

      {(c.prose || c.proseEffective) && c.status === 'ok' && (
        <div className="rp-prose">
          {editing ? (
            <EditableText value={c.proseEffective ?? c.prose ?? ''} original={c.prose ?? ''}
              onSave={(v) => onOverride({ chapter: c.id, kind: 'prose', key: '' }, v)} />
          ) : (
            (c.proseEffective ?? '').split('\n').filter(Boolean).map((l, i) => <p key={i}>{l}</p>)
          )}
          <Mark o={c.proseOverride} />
          {editing && c.proseOverride && (
            <button className="rp-mini" onClick={() => onRevoke(c.proseOverride!.id)}>撤销修改</button>
          )}
        </div>
      )}

      {c.subitemsZh.length > 0 && c.status === 'pending' && (
        <ul className="rp-subitems">
          {c.subitemsZh.map((s) => <li key={s}>{s}：未评估</li>)}
        </ul>
      )}

      {c.grades.length > 0 && <GradeTable c={c} editing={editing} onOverride={onOverride} onRevoke={onRevoke} />}
      {c.facts.length > 0 && c.section === 'findings' && c.id !== 'quant_appendix' && (
        <FactsTable c={c} editing={editing} onOverride={onOverride} onRevoke={onRevoke} compact />
      )}

      {c.radiologist && (
        <div className="rp-radiologist">
          <div className="rp-radiologist-label">{c.radiologist.labelZh}</div>
          {c.radiologist.sentences.map((s, i) => <p key={i}>{s}</p>)}
          <div className="rp-note">{c.radiologist.note}</div>
        </div>
      )}

      {c.caveatsZh.length > 0 && c.status === 'ok' && (
        <ul className="rp-caveats">
          {c.caveatsZh.map((t) => <li key={t}>{t}</li>)}
        </ul>
      )}
    </section>
  )
}

function ItemsChapter({ c, view, onOverride, onRevoke }: {
  c: RenderedChapter; view: View; onOverride: OverrideFn; onRevoke: (id: string) => void
}) {
  const editing = view === 'edit'
  return (
    <section className="rp-chapter" id={'ch-' + c.id}>
      <h2 className="rp-section">{c.titleZh}</h2>
      <ol className="rp-items">
        {c.items.map((it) => (
          <li key={it.id} className={`rp-item rp-origin-${it.origin}`}>
            {editing && it.origin !== 'template' ? (
              <EditableText value={it.textEffective ?? it.text} original={it.text}
                onSave={(v) => onOverride({ chapter: c.id, kind: 'item', key: it.id }, v)} inline />
            ) : (
              <span>{it.textEffective ?? it.text}</span>
            )}
            {it.origin === 'radiologist' && <span className="rp-origin">（放射科报告）</span>}
            <Mark o={it.override} />
            {editing && it.override && (
              <button className="rp-mini" onClick={() => onRevoke(it.override!.id)}>撤销</button>
            )}
          </li>
        ))}
      </ol>
    </section>
  )
}

function fmtValue(v: unknown, fmt: string): string {
  if (v == null || typeof v !== 'number') return v == null ? '—' : String(v)
  const m = /^([+]?)\.?(\d)?f$/.exec(fmt)
  const digits = m?.[2] != null ? Number(m[2]) : 2
  const s = v.toFixed(digits)
  return m?.[1] === '+' && v > 0 ? '+' + s : s
}

function FactsTable({ c, editing, onOverride, onRevoke, compact }: {
  c: RenderedChapter; editing: boolean; onOverride: OverrideFn; onRevoke: (id: string) => void; compact?: boolean
}) {
  const facts = compact ? c.facts.filter((f) => !f.key.startsWith('sub.')) : c.facts
  if (!facts.length) return null
  return (
    <table className="rp-table">
      <thead><tr><th>指标</th><th>值</th><th>单位</th>{editing && <th>修改</th>}</tr></thead>
      <tbody>
        {facts.map((f) => (
          <tr key={f.key}>
            <td>{f.labelZh}</td>
            <td className="rp-num">
              {fmtValue(f.valueEffective, f.fmt)}
              {f.override && <div className="rp-mark-cell"><Mark o={f.override} /></div>}
            </td>
            <td>{f.unit}</td>
            {editing && (
              <td>
                <NumberEdit value={f.valueEffective as number | null}
                  onSave={(v) => onOverride({ chapter: c.id, kind: 'fact', key: f.key }, v)} />
                {f.override && <button className="rp-mini" onClick={() => onRevoke(f.override!.id)}>撤销</button>}
              </td>
            )}
          </tr>
        ))}
      </tbody>
    </table>
  )
}

function GradeTable({ c, editing, onOverride, onRevoke }: {
  c: RenderedChapter; editing: boolean; onOverride: OverrideFn; onRevoke: (id: string) => void
}) {
  return (
    <table className="rp-table">
      <thead><tr><th>亚区</th><th>Outerbridge（厚度推导）</th><th>可信度</th>{editing && <th>修改</th>}</tr></thead>
      <tbody>
        {c.grades.map((g) => {
          const conf = CONF[(g.confidence ?? 'low') as keyof typeof CONF] ?? CONF.low
          const eff = g.gradeEffective ?? g.grade
          return (
            <tr key={g.key}>
              <td>{g.labelZh ?? g.key}</td>
              <td className="rp-num">
                {GRADE_ZH[eff ?? ''] ?? eff}
                {g.override && <div className="rp-mark-cell"><Mark o={g.override} /></div>}
              </td>
              <td>{conf.glyph} {conf.word}</td>
              {editing && (
                <td>
                  <select value={eff ?? ''} onChange={(e) => onOverride({ chapter: c.id, kind: 'grade', key: g.key }, e.target.value)}>
                    {['0', 'II', 'III', 'IV', '未评估'].map((v) => <option key={v} value={v}>{GRADE_ZH[v]}</option>)}
                  </select>
                  {g.override && <button className="rp-mini" onClick={() => onRevoke(g.override!.id)}>撤销</button>}
                </td>
              )}
            </tr>
          )
        })}
      </tbody>
    </table>
  )
}

function Appendix({ c, view, onOverride, onRevoke }: {
  c: RenderedChapter; view: View; onOverride: OverrideFn; onRevoke: (id: string) => void
}) {
  const editing = view === 'edit'
  return (
    <section className="rp-chapter rp-appendix" id={'ch-' + c.id}>
      <h2 className="rp-section">{c.titleZh}</h2>
      {c.grades.length > 0 && <GradeTable c={c} editing={editing} onOverride={onOverride} onRevoke={onRevoke} />}
      <FactsTable c={c} editing={editing} onOverride={onOverride} onRevoke={onRevoke} />
    </section>
  )
}

function EditableText({ value, original, onSave, inline }: {
  value: string; original: string; onSave: (v: string) => void; inline?: boolean
}) {
  const [v, setV] = useState(value)
  useEffect(() => setV(value), [value])
  const commit = () => { if (v.trim() && v !== value) onSave(v) }
  return inline
    ? <input className="rp-edit-inline" value={v} onChange={(e) => setV(e.target.value)} onBlur={commit} />
    : <textarea className="rp-edit" value={v} rows={Math.max(3, v.split('\n').length + 1)}
        onChange={(e) => setV(e.target.value)} onBlur={commit}
        title={original ? '系统生成的原文：\n' + original : ''} />
}

function NumberEdit({ value, onSave }: { value: number | null; onSave: (v: number) => void }) {
  const [v, setV] = useState(value == null ? '' : String(value))
  useEffect(() => setV(value == null ? '' : String(value)), [value])
  return (
    <input className="rp-edit-num" value={v} onChange={(e) => setV(e.target.value)}
      onBlur={() => { const n = Number(v); if (v !== '' && Number.isFinite(n) && n !== value) onSave(n) }} />
  )
}
