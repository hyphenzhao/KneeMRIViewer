/**
 * 膝关节 MRI 结构化报告  #/report/<segId>?view=web|print|edit&to=<chapterId>&pdf=1
 *
 * One illustrated document on explicit A4 sheets, three views of it:
 *
 * - web:   dark tokens, the same sheets, tooltips on the figures
 * - print: white A4 sheets, exactly what the browser's print dialog and the
 *          server-side PDF produce (both use this view, margins zero)
 * - edit:  every prose block, number and grade can be overridden by a named
 *          doctor; the original stays visible next to the change
 *
 * The figures live inside the chapters they belong to - the cartilage
 * chapter carries the key slices, the 3D snapshot, the flattened thickness
 * maps, the grade maps, the reference dot plot and the histograms - and the
 * QC panel sits in the overview chapter, before any number. Pagination is
 * ours (sheets/paginate.ts): blocks are measured, then placed; figures have
 * fixed millimetre boxes so the page plan never waits for pixels.
 */
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'

import { api, type AiReport, type LabelSet, type Morphometry, type PlateMetric, type ReferenceSet, type ReportDocument, type RenderedChapter } from '../api'
import {
  AiAudit, CompartmentsBlock, EditableText, Mark, QcBlock, StatusBadge, dataTable, explainFailure,
  factsTable, glossaryTable, gradeTable, type OverrideFn, type Sex, type View,
} from './report/blocks'
import { Histograms } from './report/charts/Histograms'
import { SubregionDotPlot } from './report/charts/SubregionDotPlot'
import { ColourBar, GradeMap, MapLegend, PlateGrid, ThicknessMap, type TileBox } from './report/figures/PlateMaps'
import { Recon3D } from './report/figures/Recon3D'
import { PLANES, SliceTile, sliceCaption, type SliceMeta } from './report/figures/SliceSnapshots'
import { decodePlateRaster, type DecodedRaster, type PlateRaster } from './report/raster'
import { FigureReadyProvider, type ReadySummary } from './report/ready'
import { paginate, type Block, type Measured } from './report/sheets/paginate'
import { CONTENT_H_MM, Measurer, SheetView } from './report/sheets/Sheets'

const REF_KEY = 'knee_cartilage_reference_v1'
const PLATE_ORDER = [4, 5, 6, 7, 8]

// The page plan, in millimetres. Tile boxes are what the figures are fitted
// into; the block heights are what the paginator budgets for them.
// Figures 1-3 are sized to share one sheet: 62 + 112 + 84 = 258 of 263 mm.
const FIG1 = { tile: { wMm: 43, hMm: 46 } as TileBox, mm: 62 }
const FIG2 = { a: { wMm: 88, hMm: 36 } as TileBox, b: { wMm: 57, hMm: 46 } as TileBox, mm: 112 }
const FIG3 = { a: { wMm: 88, hMm: 24 } as TileBox, b: { wMm: 57, hMm: 32 } as TileBox, mm: 84 }

interface Figures {
  loaded: boolean
  rasters: Partial<Record<number, DecodedRaster | null>>
  slices: Record<string, SliceMeta>
  error: string | null
}

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

function sexOf(doc: ReportDocument | null): Sex | null {
  const s = doc?.header.sexZh ?? ''
  return /男/.test(s) ? 'male' : /女/.test(s) ? 'female' : null
}

export default function ReportPage() {
  const [route, setRoute] = useState(parseHash())
  const [doc, setDoc] = useState<ReportDocument | null>(null)
  const [busy, setBusy] = useState(true)
  const [err, setErr] = useState('')
  const [fail, setFail] = useState<string | null>(null)
  const [morph, setMorph] = useState<Morphometry | null>(null)
  const [refs, setRefs] = useState<ReferenceSet | null>(null)
  const [labelSet, setLabelSet] = useState<LabelSet | null>(null)
  const [aiReport, setAiReport] = useState<AiReport | null>(null)
  const [figures, setFigures] = useState<Figures | null>(null)
  const [pdfOk, setPdfOk] = useState<{ available: boolean; detail: string | null } | null>(null)
  const [ready, setReady] = useState<ReadySummary>({ pending: 0, total: 0, skipped: 0, failed: 0 })
  const [paper, setPaper] = useState('#ffffff')
  const rootRef = useRef<HTMLDivElement>(null)
  const [editor, setEditor] = useState(() => {
    try { return localStorage.getItem('mriv.editor') ?? '' } catch { return '' }
  })
  // Read through a ref by the override callbacks, so typing a name does not
  // rebuild (and re-paginate) the whole document on every keystroke.
  const editorRef = useRef(editor)
  editorRef.current = editor

  useEffect(() => { api.pdfStatus().then(setPdfOk).catch(() => setPdfOk({ available: false, detail: null })) }, [])
  useEffect(() => {
    const on = () => setRoute(parseHash())
    window.addEventListener('hashchange', on)
    return () => window.removeEventListener('hashchange', on)
  }, [])

  const { segId, view, to, pdf } = route

  useLayoutEffect(() => {
    if (rootRef.current) {
      const p = getComputedStyle(rootRef.current).getPropertyValue('--rp-paper').trim()
      if (p) setPaper(p)
    }
  }, [view, doc])

  /** Everything the page needs, in the order a reader needs it: text first. */
  const load = useCallback(async (id: number, regenerate: boolean, actor = '') => {
    setBusy(true); setErr(''); setFail(null)
    let d: ReportDocument
    try {
      if (regenerate) {
        d = await api.buildReportDocument(id, actor)
      } else {
        const st = await api.reportDocumentState(id)
        // Generate on first visit: a link that only works after someone
        // pressed a button elsewhere is a link that gets reported as broken.
        d = st.exists ? await api.reportDocument(id) : await api.buildReportDocument(id)
      }
    } catch (e) {
      const msg = String(e)
      if (/swapped|medial\/lateral/i.test(msg)) setFail(msg)
      else setErr(msg)
      setBusy(false)
      return
    }
    setDoc(d)
    setBusy(false)

    const [r, seg, ai] = await Promise.all([
      api.references(REF_KEY).catch(() => null),
      api.segmentation(id).catch(() => null),
      api.aiReportState(id).then((s) => (s.exists ? api.aiReport(id) : null)).catch(() => null),
    ])
    setRefs(r); setLabelSet((seg?.labelSet as LabelSet | null) ?? null); setAiReport(ai)

    let m: Morphometry | null = null
    try {
      m = await api.morphometry(id)
    } catch {
      try { m = await api.buildMorphometry(id) } catch (e) { setErr(String(e)) }
    }
    setMorph(m)

    const fig: Figures = { loaded: true, rasters: {}, slices: {}, error: null }
    const manifest = m?.figures
    if (!manifest) fig.error = m ? '本次测量未生成图形' : '形态学测量不可用'
    else if (manifest.error) fig.error = manifest.error
    else {
      fig.slices = (manifest.slices ?? {}) as Record<string, SliceMeta>
      await Promise.all(Object.entries(manifest.plates ?? {}).map(async ([label, p]) => {
        try {
          const res = await fetch(p.url)
          if (!res.ok) throw new Error(`${res.status}`)
          fig.rasters[Number(label)] = decodePlateRaster((await res.json()) as PlateRaster)
        } catch (e) {
          console.warn('[report] plate raster failed', label, e)
          fig.rasters[Number(label)] = null
        }
      }))
    }
    setFigures(fig)
  }, [])

  useEffect(() => {
    if (segId == null) return
    setDoc(null); setMorph(null); setFigures(null); setAiReport(null)
    load(segId, false)
  }, [segId, load])

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

  const regenerate = () => { if (segId != null) { setFigures(null); load(segId, true, editor) } }

  const review = async (state: 'approved' | 'rejected') => {
    if (!doc) return
    if (!editor.trim()) { setErr('请先填写复核医师姓名'); return }
    setBusy(true)
    try { setDoc(await api.reviewReportDocument(doc.id, state, editor)) } catch (e) { setErr(String(e)) } finally { setBusy(false) }
  }

  const docId = doc?.id
  const override = useCallback<OverrideFn>(async (target, value, reason = '') => {
    if (docId == null) return
    const who = editorRef.current
    if (!who.trim()) { setErr('请先填写修改医师姓名'); return }
    setErr('')
    try { setDoc(await api.setOverride(docId, target, value, who, reason)) } catch (e) { setErr(String(e)) }
  }, [docId])
  const revoke = useCallback(async (overrideId: string) => {
    if (docId == null) return
    try { setDoc(await api.revokeOverride(docId, overrideId, editorRef.current)) } catch (e) { setErr(String(e)) }
  }, [docId])

  const blocks = useMemo<Block[]>(() => {
    if (!doc || !figures?.loaded) return []
    return buildBlocks({ doc, morph, refs, figures, view, aiReport, labelSet, paper, override, revoke })
  }, [doc, morph, refs, figures, view, aiReport, labelSet, paper, override, revoke])

  const [measured, setMeasured] = useState<{ blocks: Block[]; m: Record<string, Measured> } | null>(null)
  const onMeasured = useCallback((m: Record<string, Measured>) => setMeasured({ blocks, m }), [blocks])
  // Pagination is fresh when it was computed for the current blocks. While a
  // re-measure is in flight the previous sheets stay up (no blank flash on an
  // edit); readiness for the PDF renderer requires the fresh ones.
  const fresh = !!measured && measured.blocks === blocks && blocks.length > 0
  const lastSheets = useRef<ReturnType<typeof paginate> | null>(null)
  const sheets = useMemo(() => {
    if (fresh) lastSheets.current = paginate(blocks, measured!.m, CONTENT_H_MM)
    return blocks.length ? lastSheets.current : null
  }, [fresh, measured, blocks])

  // Figures expected on the page: two maps per raster, one image per slice,
  // and the 3D tile. Ready only once all of them have registered and none is
  // pending - a bare "pending === 0" is true for one frame before they mount.
  const expectedFigures = figures?.loaded
    ? Object.values(figures.rasters).filter(Boolean).length * 2 + Object.keys(figures.slices).length + 1
    : 0
  const isReady = !!doc && !busy && fresh && !!sheets && !!figures?.loaded && ready.total >= expectedFigures && ready.pending === 0

  const h = doc?.header
  const headerLine = [h?.titleZh ?? '膝关节 MRI 结构化报告', h?.lateralityZh, h?.sexZh, h?.ageBand,
    h?.caseLabel ? `病例 ${h.caseLabel}` : null, (h?.generatedAt ?? doc?.generatedAt ?? '').slice(0, 10)]
    .filter(Boolean).join(' · ')

  return (
    <FigureReadyProvider onChange={setReady}>
      <div ref={rootRef} className={`rp-root rp-${view}`} data-view={view}
        data-report-ready={isReady ? '1' : '0'}
        data-figures-total={ready.total} data-figures-skipped={ready.skipped} data-figures-failed={ready.failed}>
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
          </header>
        )}

        <main className="rp-page">
          {busy && !doc && <p className="loading">正在生成报告，首次约 40 秒…</p>}
          {err && <p className="err">{err}</p>}
          {fail && (
            <div className="rp-fail">
              <h2>无法生成报告</h2>
              <p>{explainFailure(fail)}</p>
              <p className="rp-raw">{fail}</p>
              <p className="rp-note">
                这是刻意的硬失败。内外侧标签疑似互换时，所有亚区指标与图形都会镜像，
                而镜像后的报告看起来完全合理——宁可不出，也不能出错的。
              </p>
            </div>
          )}
          {doc && !figures?.loaded && !fail && <p className="loading">正在载入测量图形…</p>}
          {blocks.length > 0 && (
            <>
              <Measurer blocks={blocks} onMeasured={onMeasured} />
              {sheets
                ? <SheetView sheets={sheets} view={view}
                    header={(i, n) => <><span>{headerLine}</span><span>第 {i} / {n} 页</span></>}
                    footer={<><span>{doc?.rendered.disclaimerZh}</span><span>{h?.algoVersion}</span></>} />
                : <p className="loading">正在排版…</p>}
            </>
          )}
        </main>
      </div>
    </FigureReadyProvider>
  )
}

// ------------------------------------------------------------- the document

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

function Missing({ text }: { text: string }) {
  return <div className="rp-fig-missing">{text}</div>
}

interface BuildArgs {
  doc: ReportDocument
  morph: Morphometry | null
  refs: ReferenceSet | null
  figures: Figures
  view: View
  aiReport: AiReport | null
  labelSet: LabelSet | null
  paper: string
  override: OverrideFn
  revoke: (id: string) => void
}

function buildBlocks(a: BuildArgs): Block[] {
  const { doc, morph, refs, figures, view, override, revoke } = a
  const editing = view === 'edit'
  const rendered = doc.rendered
  const chapters = rendered.chapters
  const findings = chapters.filter((c) => c.section === 'findings')
  const impression = chapters.find((c) => c.id === 'impression')
  const advice = chapters.find((c) => c.id === 'advice')
  const appendix = chapters.find((c) => c.section === 'appendix')
  const cartilage = chapters.find((c) => c.id === 'cartilage')
  const sex = sexOf(doc)
  const plates = [...(morph?.metrics?.plates ?? [])].sort((x, y) => PLATE_ORDER.indexOf(x.label) - PLATE_ORDER.indexOf(y.label))
  const comp = morph?.metrics?.compartments
  const figError = figures.error
  const out: Block[] = []
  const atomic = (id: string, chapter: string | null, node: React.ReactNode, extra: Partial<Block> = {}) =>
    out.push({ id, chapter, kind: 'atomic', node, ...extra })
  const table = (id: string, chapter: string | null, spec: ReturnType<typeof factsTable>) => {
    if (spec) out.push({ id, chapter, kind: 'table', table: spec })
  }

  atomic('header', null, <ReportHeader doc={doc} />)
  if (rendered.staleOverrideCount > 0) {
    atomic('guard.stale', null, <div className="rp-guard">有 {rendered.staleOverrideCount} 处医师修改早于本次生成，已标记「请复核」。</div>)
  }
  if (doc.status === 'partial') {
    atomic('guard.partial', null, <div className="rp-guard">部分章节生成失败：{doc.error}。失败章节按「未生成」显示，不会被当作正常。</div>)
  }
  if (morph && doc.morphometryId != null && doc.morphometryId !== morph.id) {
    atomic('guard.morph', null, <div className="rp-guard">图形与数据表基于最新一次测量（{morph.algoVersion}），正文基于生成时的测量，建议重新生成以保持一致。</div>)
  }
  out.push({ id: 'section.findings', chapter: null, kind: 'heading', node: <h2 className="rp-section">影像所见</h2> })

  for (const c of findings) {
    out.push({
      id: `${c.id}.h`, chapter: c.id, kind: 'heading', startsChapter: { status: c.status },
      node: <h3>{c.titleZh} <StatusBadge c={c} /></h3>,
    })
    if (c.status === 'pending') {
      atomic(`${c.id}.placeholder`, c.id, <p className="rp-placeholder">{c.placeholderZh}</p>)
      if (c.subitemsZh.length) {
        atomic(`${c.id}.sub`, c.id, <ul className="rp-subitems">{c.subitemsZh.map((s) => <li key={s}>{s}：未评估</li>)}</ul>)
      }
    }
    if (c.status === 'failed') {
      atomic(`${c.id}.failed`, c.id, <p className="rp-placeholder rp-failed">本章生成失败，未生成任何内容。{c.caveatsZh[0]}</p>)
    }
    if ((c.prose || c.proseEffective) && c.status === 'ok') {
      atomic(`${c.id}.prose`, c.id, (
        <div className="rp-prose">
          {editing ? (
            <EditableText value={c.proseEffective ?? c.prose ?? ''} original={c.prose ?? ''}
              onSave={(v) => override({ chapter: c.id, kind: 'prose', key: '' }, v)} />
          ) : (
            (c.proseEffective ?? '').split('\n').filter(Boolean).map((l, i) => <p key={i}>{l}</p>)
          )}
          <Mark o={c.proseOverride} />
          {editing && c.proseOverride && <button className="rp-mini" onClick={() => revoke(c.proseOverride!.id)}>撤销修改</button>}
        </div>
      ))
    }

    if (c.id === 'overview' && c.status === 'ok' && morph?.qc) {
      atomic('overview.qc', c.id, <QcBlock qc={morph.qc} refs={refs} />)
    }

    if (c.id === 'cartilage' && c.status === 'ok') {
      cartilageFigures(a, c, plates, comp, sex, out)
      table('cartilage.grades', c.id, gradeTable(c, editing, override, revoke, true))
    } else if (c.grades.length) {
      table(`${c.id}.grades`, c.id, gradeTable(c, editing, override, revoke, false))
    }
    if (c.facts.length && c.id !== 'quant_appendix') {
      table(`${c.id}.facts`, c.id, factsTable(c, editing, override, revoke, true))
    }
    if (c.radiologist) {
      atomic(`${c.id}.radiologist`, c.id, (
        <div className="rp-radiologist">
          <div className="rp-radiologist-label">{c.radiologist.labelZh}</div>
          {c.radiologist.sentences.map((s, i) => <p key={i}>{s}</p>)}
          <div className="rp-note">{c.radiologist.note}</div>
        </div>
      ))
    }
    if (c.caveatsZh.length && c.status === 'ok') {
      atomic(`${c.id}.caveats`, c.id, <ul className="rp-caveats">{c.caveatsZh.map((t) => <li key={t}>{t}</li>)}</ul>)
    }
  }

  for (const c of [impression, advice]) {
    if (!c) continue
    out.push({
      id: `${c.id}.h`, chapter: c.id, kind: 'heading', startsChapter: { status: c.status },
      node: <h2 className="rp-section">{c.titleZh}</h2>,
    })
    atomic(`${c.id}.items`, c.id, (
      <ol className="rp-items">
        {c.items.map((it) => (
          <li key={it.id} className={`rp-item rp-origin-${it.origin}`}>
            {editing && it.origin !== 'template' ? (
              <EditableText value={it.textEffective ?? it.text} original={it.text}
                onSave={(v) => override({ chapter: c.id, kind: 'item', key: it.id }, v)} inline />
            ) : (
              <span>{it.textEffective ?? it.text}</span>
            )}
            {it.origin === 'radiologist' && <span className="rp-origin">（放射科报告）</span>}
            <Mark o={it.override} />
            {editing && it.override && <button className="rp-mini" onClick={() => revoke(it.override!.id)}>撤销</button>}
          </li>
        ))}
      </ol>
    ))
  }

  atomic('sign', null, (
    <div className="rp-sign">
      <div>复核状态：{reviewZh(rendered.reviewState)}{rendered.reviewedBy ? ` · ${rendered.reviewedBy}` : ''}{rendered.reviewedAt ? ` · ${rendered.reviewedAt}` : ''}</div>
      <div>复核医师签名：______________　　日期：______________</div>
    </div>
  ))

  if (appendix) {
    out.push({
      id: 'appendix.h', chapter: appendix.id, kind: 'heading', breakBefore: true,
      startsChapter: { status: appendix.status, className: 'rp-appendix' },
      node: <h2 className="rp-section">{appendix.titleZh}</h2>,
    })
    table('appendix.grades', appendix.id, gradeTable(appendix, editing, override, revoke, false))
    table('appendix.facts', appendix.id, factsTable(appendix, editing, override, revoke, false))
    if (plates.length) {
      atomic('appendix.data.h', appendix.id, <h4 className="rp-block-title">全部亚区数据</h4>, { kind: 'heading' })
      table('appendix.data', appendix.id, dataTable(plates, refs, sex))
    }
    if (refs) {
      atomic('appendix.gloss.h', appendix.id, <h4 className="rp-block-title">指标说明</h4>, { kind: 'heading' })
      table('appendix.gloss', appendix.id, glossaryTable(refs))
    }
    atomic('appendix.audit', appendix.id, <AiAudit report={a.aiReport} view={view} />)
    if (figError) atomic('appendix.figerr', appendix.id, <p className="rp-note">图形未生成：{figError}</p>)
  }
  atomic('foot', null, <footer className="rp-foot">{rendered.disclaimerZh}</footer>)
  return out
}

function cartilageFigures(a: BuildArgs, c: RenderedChapter, plates: PlateMetric[],
  comp: Record<string, any> | undefined, sex: Sex | null, out: Block[]) {
  const { figures, view, refs, labelSet, morph, paper, doc } = a
  const interactive = view !== 'print'
  const missing = (why: string) => <Missing text={`图未生成：${why}`} />
  const reason = figures.error ?? '缺少该软骨板的展开图'
  const grades: Record<string, { grade: string; overridden: boolean }> = {}
  for (const g of c.grades) grades[g.key] = { grade: g.gradeEffective ?? g.grade ?? '未评估', overridden: !!g.override }

  // 图 1 影像概览
  out.push({
    id: 'fig.overview', chapter: c.id, kind: 'atomic', fixedMm: FIG1.mm,
    node: (
      <figure className="rp-fig rp-fig-1" style={{ height: `${FIG1.mm}mm` }}>
        <h4 className="rp-fig-title">图 1 · 影像概览：三平面关键层（标签轮廓）与三维重建</h4>
        <div className="rp-fig-row">
          {PLANES.map((plane) => {
            const meta = figures.slices[plane]
            return (
              <div key={plane} className="rp-fig-tile" style={{ width: `${FIG1.tile.wMm}mm` }}>
                <div className="rp-fig-box" style={{ height: `${FIG1.tile.hMm}mm` }}>
                  {meta ? <SliceTile plane={plane} meta={meta} box={FIG1.tile} /> : missing(figures.error ?? '未选出关键层')}
                </div>
                <figcaption>{meta ? sliceCaption(plane, meta) : plane}</figcaption>
              </div>
            )
          })}
          <div className="rp-fig-tile" style={{ width: `${FIG1.tile.wMm}mm` }}>
            <div className="rp-fig-box" style={{ height: `${FIG1.tile.hMm}mm` }}>
              <Recon3D segId={doc.segmentationId} labelSet={labelSet} frame={morph?.frame ?? null} box={FIG1.tile} background={paper} />
            </div>
            <figcaption>三维重建 · 前内侧观（显示网格经形状插值，仅供观察）</figcaption>
          </div>
        </div>
      </figure>
    ),
  })

  // 图 2 厚度展开图
  out.push({
    id: 'fig.thickness', chapter: c.id, kind: 'atomic', fixedMm: FIG2.mm,
    node: (
      <figure className="rp-fig rp-fig-2" style={{ height: `${FIG2.mm}mm` }}>
        <h4 className="rp-fig-title">图 2 · 软骨厚度展开图（骨-软骨界面，按亚区划分；Chondrometrics 展示惯例）</h4>
        <PlateGrid rasters={figures.rasters} boxA={FIG2.a} boxB={FIG2.b}
          render={(r, box) => <ThicknessMap raster={r} box={box} interactive={interactive} />}
          missing={() => missing(reason)} />
        <div className="rp-fig-foot">
          <ColourBar maxMm={4} />
          <MapLegend mode="thickness" />
        </div>
      </figure>
    ),
  })

  // 图 3 分级图
  out.push({
    id: 'fig.grade', chapter: c.id, kind: 'atomic', fixedMm: FIG3.mm,
    node: (
      <figure className="rp-fig rp-fig-3" style={{ height: `${FIG3.mm}mm` }}>
        <h4 className="rp-fig-title">图 3 · Outerbridge 分级图（MRI 改良、厚度推导；I 级不可评估；须医师复核）</h4>
        <PlateGrid rasters={figures.rasters} boxA={FIG3.a} boxB={FIG3.b}
          render={(r, box) => <GradeMap raster={r} box={box} grades={grades} interactive={interactive} />}
          missing={() => missing(reason)} />
        <div className="rp-fig-foot"><MapLegend mode="grade" /></div>
      </figure>
    ),
  })

  if (comp) out.push({ id: 'cartilage.compartments', chapter: c.id, kind: 'atomic', node: <CompartmentsBlock comp={comp} /> })

  if (plates.length) {
    out.push({
      id: 'fig.dotplot', chapter: c.id, kind: 'atomic',
      node: (
        <figure className="rp-fig rp-fig-4">
          <h4 className="rp-fig-title">图 4 · 各亚区平均软骨厚度与参考范围</h4>
          <p className="rp-sub">
            采集条件：矢状位 2D、层厚 3 mm。体模实测本平台在此条件下系统性低估 7-18%，
            参考带（斜纹）来自近各向同性序列的 Framingham 队列，仅作量级参照，<b>不是诊断阈值</b>。
            {sex ? `参考值按${sex === 'male' ? '男' : '女'}性取值。` : '本例性别未知，未绘制参考带。'}
          </p>
          <div className="rp-key">
            <span><i className="rp-key-dot" /> 本例测量值</span>
            <span><i className="rp-key-band" /> 参考均值 ±1SD（深）/ ±2SD（浅）</span>
            <span className="rp-key-muted">括号内为该亚区跨越的原始层数</span>
          </div>
          <SubregionDotPlot plates={plates} refs={refs} sex={sex} />
        </figure>
      ),
    })
    out.push({
      id: 'fig.hist', chapter: c.id, kind: 'atomic',
      node: (
        <figure className="rp-fig rp-fig-5">
          <h4 className="rp-fig-title">图 5 · 各软骨板厚度分布（按界面面积加权，0.1 mm 分箱；左移或左侧长尾提示局部变薄）</h4>
          <Histograms plates={plates} />
        </figure>
      ),
    })
  }
}
