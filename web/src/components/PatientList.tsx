import { useEffect, useMemo, useState } from 'react'

import { api, type DatasetSummary, type PatientRow } from '../api'

interface Props {
  selectedId: number | null
  onSelect: (p: PatientRow) => void
}

export default function PatientList({ selectedId, onSelect }: Props) {
  const [datasets, setDatasets] = useState<DatasetSummary[]>([])
  const [dataset, setDataset] = useState<string>('')
  const [q, setQ] = useState('')
  const [reportQ, setReportQ] = useState('')
  const [onlyAnnotated, setOnlyAnnotated] = useState(false)
  const [rows, setRows] = useState<PatientRow[]>([])
  const [total, setTotal] = useState(0)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    api
      .datasets()
      .then((d) => {
        // The server returns them in config.toml order, which is the order the
        // operator cares about. Only skip past ones that have not been scanned
        // yet, so the app never opens on an empty list.
        setDatasets(d)
        if (d.length && !dataset) {
          setDataset((d.find((x) => x.n_patients > 0) ?? d[0]).key)
        }
      })
      .catch((e) => setError(String(e)))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  useEffect(() => {
    let cancelled = false
    const t = setTimeout(() => {
      setBusy(true)
      api
        .patients({
          dataset: dataset || undefined,
          q: q || undefined,
          report_q: reportQ || undefined,
          has_segmentation: onlyAnnotated || undefined,
          limit: 300,
        })
        .then((r) => {
          if (cancelled) return
          setRows(r.items)
          setTotal(r.total)
          setError(null)
        })
        .catch((e) => !cancelled && setError(String(e)))
        .finally(() => !cancelled && setBusy(false))
    }, 180)
    return () => {
      cancelled = true
      clearTimeout(t)
    }
  }, [dataset, q, reportQ, onlyAnnotated])

  const annotated = useMemo(() => rows.filter((r) => r.seg_count > 0).length, [rows])

  return (
    <aside className="sidebar">
      <div className="sidebar-head">
        <select value={dataset} onChange={(e) => setDataset(e.target.value)}>
          {datasets.map((d) => (
            <option key={d.key} value={d.key}>
              {d.name} · {d.n_patients} 例
              {d.n_segmentations > 0 ? ` · ${d.n_segmentations} 标注` : ''}
              {d.n_patients === 0 ? '（尚未扫描）' : ''}
            </option>
          ))}
        </select>
        <input
          placeholder="搜索编号或姓名…"
          value={q}
          onChange={(e) => setQ(e.target.value)}
        />
        <input
          placeholder="搜索报告内容，如「软骨」…"
          value={reportQ}
          onChange={(e) => setReportQ(e.target.value)}
          title="在影像所见 / 诊断 / 建议里做子串搜索"
        />
        <label className="chk">
          <input
            type="checkbox"
            checked={onlyAnnotated}
            onChange={(e) => setOnlyAnnotated(e.target.checked)}
          />
          只看有分割标注的
        </label>
        <div className="hint" style={{ color: 'var(--muted)', fontSize: 11.5 }}>
          {busy ? '加载中…' : `显示 ${rows.length} / ${total} 例，其中 ${annotated} 例有标注`}
        </div>
      </div>

      {error && <div className="err">{error}</div>}

      <div className="sidebar-body">
        <ul className="plist">
          {rows.map((p) => (
            <li
              key={p.id}
              className={p.id === selectedId ? 'sel' : ''}
              onClick={() => onSelect(p)}
            >
              <span className="pid">
                {p.external_id}
                {p.name ? ` · ${p.name}` : ''}
              </span>
              <span className={p.seg_count > 0 ? 'badge' : 'badge none'}>
                {p.seg_count > 0 ? `${p.seg_count} 标注` : '无标注'}
              </span>
              <span className="sub">
                {[p.age, p.sex, `${p.n_series} 个序列`].filter(Boolean).join(' · ')}
              </span>
            </li>
          ))}
        </ul>
        {!busy && !rows.length && <div className="loading">没有匹配的病例</div>}
      </div>
    </aside>
  )
}
