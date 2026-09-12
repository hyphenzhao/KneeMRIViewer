/**
 * The report's building blocks: tables, editable text, and the dashboard
 * panels that used to live on their own page (QC, compartments, data table,
 * glossary, AI audit). Tables are returned as `TableSpec`s - head and rows
 * apart - so the paginator can cut them between rows.
 */
import { useEffect, useState } from 'react'

import type { AiReport, Morphometry, PlateMetric, ReferenceSet, RenderedChapter } from '../../api'
import type { TableSpec } from './sheets/paginate'

export type View = 'web' | 'print' | 'edit'
export type OverrideFn = (t: { chapter: string; kind: string; key: string }, v: unknown, reason?: string) => void
export type Sex = 'male' | 'female'

export const GRADE_ZH: Record<string, string> = {
  '0': '0 级', II: 'II 级', III: 'III 级', IV: 'IV 级', 未评估: '未评估',
}
export const CONF = {
  high: { glyph: '●', word: '可信' },
  medium: { glyph: '◐', word: '一般' },
  low: { glyph: '○', word: '存疑' },
} as const

export function fmt(v: number | null | undefined): string {
  return v == null ? '—' : v.toFixed(2)
}

export function fmtValue(v: unknown, fmtSpec: string): string {
  if (v == null || typeof v !== 'number') return v == null ? '—' : String(v)
  const m = /^([+]?)\.?(\d)?f$/.exec(fmtSpec)
  const digits = m?.[2] != null ? Number(m[2]) : 2
  const s = v.toFixed(digits)
  return m?.[1] === '+' && v > 0 ? '+' + s : s
}

/** Turn the engine's exception text into something a clinician can act on. */
export function explainFailure(error: string | null): string {
  if (error && /swapped|medial\/lateral/i.test(error)) {
    return '该病例的内侧/外侧软骨标签与影像中膝关节的实际位置不符，疑似标注时左右侧弄反了。'
      + '请先核对分割标签（标签 4/6 应为内侧，5/7 应为外侧）后重新生成。'
  }
  if (error && /not materialised/i.test(error)) {
    return '该分割尚未生成标签体数据，请先在阅片页打开一次该病例。'
  }
  return error ?? '未知错误'
}

// ------------------------------------------------------------- marks & edits

export function Mark({ o }: { o: RenderedChapter['proseOverride'] }) {
  if (!o) return null
  return (
    <span className="rp-mark">
      医师修改（原值 {o.original == null ? '—' : String(o.original)}）· {o.editor} · {o.editedAt}
      {o.noteZh ? ` · ${o.noteZh}` : ''}
      {o.reason ? ` · 理由：${o.reason}` : ''}
    </span>
  )
}

export function StatusBadge({ c }: { c: RenderedChapter }) {
  const zh: Record<string, string> = {
    ok: c.proseOrigin === 'llm' ? '模型解读' : '算法生成',
    pending: '未评估', failed: '生成失败',
  }
  return <span className={`rp-badge rp-badge-${c.status}`}>{zh[c.status] ?? c.status}</span>
}

export function EditableText({ value, original, onSave, inline }: {
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

export function NumberEdit({ value, onSave }: { value: number | null; onSave: (v: number) => void }) {
  const [v, setV] = useState(value == null ? '' : String(value))
  useEffect(() => setV(value == null ? '' : String(value)), [value])
  return (
    <input className="rp-edit-num" value={v} onChange={(e) => setV(e.target.value)}
      onBlur={() => { const n = Number(v); if (v !== '' && Number.isFinite(n) && n !== value) onSave(n) }} />
  )
}

// ------------------------------------------------------------------ tables

export function factsTable(c: RenderedChapter, editing: boolean, onOverride: OverrideFn,
  onRevoke: (id: string) => void, compact: boolean): TableSpec | null {
  const facts = compact ? c.facts.filter((f) => !f.key.startsWith('sub.')) : c.facts
  if (!facts.length) return null
  return {
    head: <tr><th>指标</th><th>值</th><th>单位</th>{editing && <th>修改</th>}</tr>,
    rows: facts.map((f) => (
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
    )),
  }
}

/**
 * Outerbridge per subregion. `compact` lists only the graded subregions and
 * says how many are grade 0 - the grade map already shows them all - but the
 * edit view always gets every row, because that is where grades are changed.
 */
export function gradeTable(c: RenderedChapter, editing: boolean, onOverride: OverrideFn,
  onRevoke: (id: string) => void, compact: boolean): TableSpec | null {
  if (!c.grades.length) return null
  const eff = (g: RenderedChapter['grades'][number]) => g.gradeEffective ?? g.grade ?? ''
  const shown = compact && !editing ? c.grades.filter((g) => eff(g) !== '0') : c.grades
  const zeros = c.grades.length - shown.length
  const cols = 3 + (editing ? 1 : 0)
  const rows = shown.map((g) => {
    const conf = CONF[(g.confidence ?? 'low') as keyof typeof CONF] ?? CONF.low
    const e = eff(g)
    return (
      <tr key={g.key}>
        <td>{g.labelZh ?? g.key} <span className="rp-code">{g.key}</span></td>
        <td className="rp-num">
          {GRADE_ZH[e] ?? e}
          {g.override && <div className="rp-mark-cell"><Mark o={g.override} /></div>}
        </td>
        <td>{conf.glyph} {conf.word}</td>
        {editing && (
          <td>
            <select value={e} onChange={(ev) => onOverride({ chapter: c.id, kind: 'grade', key: g.key }, ev.target.value)}>
              {['0', 'II', 'III', 'IV', '未评估'].map((v) => <option key={v} value={v}>{GRADE_ZH[v]}</option>)}
            </select>
            {g.override && <button className="rp-mini" onClick={() => onRevoke(g.override!.id)}>撤销</button>}
          </td>
        )}
      </tr>
    )
  })
  if (compact && !editing) {
    rows.push(
      <tr key="__zeros" className="rp-row-note">
        <td colSpan={cols}>{shown.length ? `其余 ${zeros} 个亚区为 0 级（无局灶变薄）。` : `全部 ${c.grades.length} 个亚区均为 0 级（无局灶变薄）。`}</td>
      </tr>,
    )
  }
  return {
    head: <tr><th>亚区</th><th>Outerbridge（厚度推导）</th><th>可信度</th>{editing && <th>修改</th>}</tr>,
    rows, minRows: 1,
  }
}

export function dataTable(plates: PlateMetric[], refs: ReferenceSet | null, sex: Sex | null): TableSpec {
  return {
    className: 'rp-table rp-table-data',
    head: (
      <tr>
        <th>软骨板</th><th>亚区</th><th>MOAKS</th>
        <th>平均 mm</th><th>p10</th><th>p90</th>
        <th>参考 mm</th><th>层数</th><th>可靠区</th><th>可信度</th>
      </tr>
    ),
    rows: plates.flatMap((p) =>
      Object.entries(p.subregions).map(([code, s]) => {
        const r = sex && refs ? refs.subregions[code]?.[sex] : undefined
        const c = CONF[s.confidence]
        return (
          <tr key={`${p.label}-${code}`}>
            <td>{p.name}</td>
            <td>{s.labelZh}</td>
            <td className="rp-num">{s.moaks}</td>
            <td className="rp-num"><b>{fmt(s.thicknessMm.mean)}</b></td>
            <td className="rp-num">{fmt(s.thicknessMm.p10)}</td>
            <td className="rp-num">{fmt(s.thicknessMm.p90)}</td>
            <td className="rp-num">{r ? `${r[0].toFixed(2)} ± ${r[1].toFixed(2)}` : '—'}</td>
            <td className="rp-num">{s.sliceSupport}</td>
            <td className="rp-num">{(s.reliableAreaFraction * 100).toFixed(0)}%</td>
            <td className={`rp-conf-${s.confidence}`}>{c.glyph} {c.word}</td>
          </tr>
        )
      })),
  }
}

export function glossaryTable(refs: ReferenceSet): TableSpec {
  const rows = Object.entries(refs.metrics).map(([k, m]) => (
    <tr key={k}>
      <td><b>{m.label_zh}</b> <span className="rp-code">{m.unit}</span></td>
      <td>{m.explanation_zh}</td>
    </tr>
  ))
  rows.push(
    <tr key="__source">
      <td><b>参考值来源</b></td>
      <td>{refs.reference_source.name}（n={refs.reference_source.n}，{refs.reference_source.sequence}，
        {refs.reference_source.measure} 口径）。适用性标记：<b>{refs.reference_source.applicability}</b>。
        {refs.reference_source.applicability_note_zh}</td>
    </tr>,
  )
  return { className: 'rp-table rp-table-gloss', head: <tr><th>指标</th><th>说明</th></tr>, rows }
}

// ------------------------------------------------------------ dashboard panels

/** Caveats first: a reader must meet them before the millimetres. */
export function QcBlock({ qc, refs }: { qc: NonNullable<Morphometry['qc']>; refs: ReferenceSet | null }) {
  const conf = qc.subregionConfidence ?? {}
  return (
    <div className="rp-qc">
      <h4 className="rp-block-title">测量质量与适用范围</h4>
      <p className="rp-caveat">{refs?.measurement_caveat.zh ?? qc.systematicBiasNote}</p>
      {refs && (
        <ul className="rp-implications">
          {refs.measurement_caveat.implications_zh.map((t) => <li key={t}>{t}</li>)}
        </ul>
      )}
      <div className="rp-qc-grid">
        <Stat label="可靠区面积占比" value={`${(qc.reliableAreaFraction * 100).toFixed(0)}%`} hint="有效分辨率 ≤ 0.6 mm 的骨-软骨界面面积比例" />
        <Stat label="平均有效分辨率" value={`${qc.effectiveResolutionMm.toFixed(2)} mm`} hint="测厚方向上原始数据的采样间隔" />
        <Stat label="射线逃逸比例" value={`${(qc.escapedFraction * 100).toFixed(1)}%`} hint="未能在 9 mm 内找到软骨外表面的顶点比例" />
        <Stat label="内外侧一致性" value={qc.medialLateralConsistent ? '通过' : '不通过'} hint="标签几何推出的侧别与影像位置是否吻合" />
      </div>
      <div className="rp-conf-row">
        <span className="rp-kv">亚区可信度分布</span>
        {(['high', 'medium', 'low'] as const).map((k) => (
          <span key={k} className={`rp-conf-badge rp-conf-${k}`}>{CONF[k].glyph} {CONF[k].word} {conf[k] ?? 0}</span>
        ))}
      </div>
    </div>
  )
}

function Stat({ label, value, hint }: { label: string; value: string; hint: string }) {
  return (
    <div className="rp-stat" title={hint}>
      <div className="rp-stat-v rp-num">{value}</div>
      <div className="rp-stat-l">{label}</div>
    </div>
  )
}

export function CompartmentsBlock({ comp }: { comp: Record<string, any> }) {
  const asym = comp.asymmetryPct as number | undefined
  return (
    <div className="rp-compartments">
      <h4 className="rp-block-title">间室汇总</h4>
      <p className="rp-sub">内外侧对比是本平台在 3 mm 层厚数据上最可信的定量指标——层厚导致的系统性低估在两个间室间大体抵消。</p>
      <div className="rp-comp-grid">
        {(['medial', 'lateral'] as const).map((side) => {
          const c = comp[side]
          if (!c) return null
          return (
            <div key={side} className="rp-comp">
              <div className="rp-comp-title">{side === 'medial' ? '内侧间室' : '外侧间室'}</div>
              <dl>
                <dt>股骨软骨平均厚度</dt><dd className="rp-num">{fmt(c.femoralThicknessMm)} mm</dd>
                <dt>胫骨软骨平均厚度</dt><dd className="rp-num">{fmt(c.tibialThicknessMm)} mm</dd>
                <dt>股胫合计 (MFTC/LFTC)</dt><dd className="rp-num">{fmt(c.sumThicknessMm)} mm</dd>
                <dt>软骨体积</dt><dd className="rp-num">{(c.volumeMm3 / 1000).toFixed(2)} cm³</dd>
              </dl>
            </div>
          )
        })}
        {asym !== undefined && (
          <div className="rp-comp rp-hero">
            <div className="rp-comp-title">内外侧不对称度</div>
            <div className={`rp-hero-n rp-num ${Math.abs(asym) > 10 ? 'rp-bad' : 'rp-good'}`}>
              {asym > 0 ? '+' : ''}{asym.toFixed(1)}%
            </div>
            <p className="rp-note">
              {Math.abs(asym) <= 10
                ? '在 ±10% 之内，内外侧大致对称。'
                : `${asym < 0 ? '内侧' : '外侧'}间室相对偏薄，建议结合力线与半月板情况判读。`}
            </p>
          </div>
        )}
      </div>
    </div>
  )
}

const SOURCE_ZH: Record<string, string> = {
  ok: '模型生成，已通过数值与分级回溯校验',
  fallback: '未调用语言模型，由内置模板按测量值确定性生成',
  rejected_by_guardrail: '模型输出出现了测量结果中不存在的数字或结构，整份拒收并回退到内置模板',
  failed: '生成过程出错',
}

async function sha256Hex(text: string): Promise<string> {
  const buf = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(text))
  return [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, '0')).join('')
}

/** What was sent to the model, and what came back - the privacy control made inspectable. */
export function AiAudit({ report, view }: { report: AiReport | null; view: View }) {
  const payload = report?.input ? JSON.stringify(report.input, null, 1) : ''
  const [hash, setHash] = useState('')
  useEffect(() => {
    let alive = true
    if (payload) sha256Hex(payload).then((h) => { if (alive) setHash(h) }).catch(() => undefined)
    return () => { alive = false }
  }, [payload])
  if (!report) return <p className="rp-note">本次未生成软骨模型解读记录。</p>
  return (
    <div className="rp-audit">
      <h4 className="rp-block-title">模型调用审计</h4>
      <dl className="rp-audit-dl">
        <dt>来源</dt><dd>{SOURCE_ZH[report.status] ?? report.status}</dd>
        <dt>提供方 / 模型</dt><dd>{report.provider ?? '—'} / {report.model ?? '—'}</dd>
        <dt>耗时</dt><dd>{report.latencyMs != null ? `${(report.latencyMs / 1000).toFixed(1)} s` : '—'}</dd>
        <dt>护栏</dt><dd>{report.guardrail ? (report.guardrail.ok ? '通过' : `未通过：${report.guardrail.violations.slice(0, 3).join('；')}`) : '—'}</dd>
        <dt>发送载荷</dt>
        <dd>{payload ? `${new TextEncoder().encode(payload).length} 字节 · SHA-256 ${hash ? hash.slice(0, 16) + '…' : '计算中'}` : '（无）'}</dd>
      </dl>
      <p className="rp-note">
        载荷由白名单逐字段构造，并在建立连接之前经过去标识化断言：不含姓名、病历号、任何 ID、检查号、UID、日期、
        文件路径、数据集名称、放射科原始报告或任何像素数据；年龄只保留十年段。
      </p>
      {view !== 'print' && payload && (
        <details className="rp-explain">
          <summary>查看实际发送给模型的内容</summary>
          <pre className="rp-payload">{payload}</pre>
        </details>
      )}
    </div>
  )
}
