import { useState } from 'react'

import { api, type LabelSet, type LabelStat } from '../api'

interface Props {
  labelSet: LabelSet | null
  stats: LabelStat[]
  present: number[]
  visible: Set<number>
  onToggle: (value: number) => void
  onToggleAll: (on: boolean) => void
  onSolo: (value: number | null) => void
  soloed: number | null
  onLabelChanged: (ls: LabelSet) => void
}

/**
 * The label panel doubles as the "what is this label?" tool.
 *
 * When a segmentation arrives with names nobody has confirmed, volume,
 * centroid, through-plane extent and mean image intensity are usually enough to
 * tell a femur from a patella - so they are shown inline, and every name is
 * editable in place and persisted back to the label set.
 */
export default function LabelPanel(props: Props) {
  const { labelSet, stats, present, visible, soloed } = props
  const [expanded, setExpanded] = useState<number | null>(null)
  const [saving, setSaving] = useState(false)

  if (!labelSet && !present.length) {
    return <div className="loading">该序列没有分割标注</div>
  }

  const statFor = (v: number) => stats.find((s) => s.value === v)
  const defFor = (v: number) => labelSet?.labels.find((l) => l.value === v)

  const rename = async (value: number, name: string) => {
    if (!labelSet) return
    setSaving(true)
    try {
      const updated = await api.patchLabel(labelSet.key, value, { name })
      props.onLabelChanged(updated)
    } finally {
      setSaving(false)
    }
  }

  const allOn = present.every((v) => visible.has(v))

  return (
    <div>
      <div className="section-title">
        分割标签 {saving && '· 保存中…'}
      </div>
      <div className="row wrap" style={{ padding: '0 10px 8px' }}>
        <button onClick={() => props.onToggleAll(!allOn)}>{allOn ? '全部隐藏' : '全部显示'}</button>
        {soloed !== null && <button onClick={() => props.onSolo(null)}>取消单独显示</button>}
      </div>

      <ul className="labels">
        {present.map((v) => {
          const def = defFor(v)
          const st = statFor(v)
          const dim = !visible.has(v)
          return (
            <li key={v} className={dim ? 'dim' : ''}>
              <input
                type="checkbox"
                checked={visible.has(v)}
                onChange={() => props.onToggle(v)}
                title="显示 / 隐藏"
              />
              <span
                className="swatch"
                style={{ background: def?.color ?? '#888' }}
                onClick={() => props.onSolo(soloed === v ? null : v)}
                title="点击单独高亮这一个标签"
              />
              <span className="nm">
                <input
                  defaultValue={def?.nameZh || def?.name || `Label ${v}`}
                  onBlur={(e) => {
                    const val = e.target.value.trim()
                    if (val && val !== (def?.nameZh || def?.name)) rename(v, val)
                  }}
                  title="可直接改名，会存回标签配置"
                />
              </span>
              <span
                className="vol"
                onClick={() => setExpanded(expanded === v ? null : v)}
                style={{ cursor: 'pointer' }}
                title="展开统计"
              >
                {st ? `${st.volumeCm3.toFixed(2)} cm³` : '—'}
              </span>
              {expanded === v && st && (
                <span className="stats">
                  体素 {st.voxels.toLocaleString()} · 层范围 {st.sliceRange[0]}–{st.sliceRange[1]}
                  <br />
                  质心 LPS [{st.centroidLps.map((x) => x.toFixed(0)).join(', ')}] · 尺寸{' '}
                  {st.extentMm.map((x) => x.toFixed(0)).join(' × ')} mm
                  {st.intensity && (
                    <>
                      <br />
                      信号强度 中位数 {st.intensity.median} (p10 {st.intensity.p10} – p90{' '}
                      {st.intensity.p90})
                    </>
                  )}
                </span>
              )}
            </li>
          )
        })}
      </ul>
    </div>
  )
}
