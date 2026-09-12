/**
 * AI 管理 - endpoint, model and key, editable at runtime.
 *
 * A floating panel rather than a route: navigating away would unmount App,
 * tearing down the loaded volume and Cornerstone's WebGL contexts, and the
 * reader would have to load the case again just for having opened settings.
 *
 * The API key travels one way. It is sent here and written to a 0600 file on
 * the server; no endpoint returns it, so this panel can only ever show whether
 * one is set.
 */
import { useEffect, useState } from 'react'

import { api, type AiSettings as Settings } from '../api'

export default function AiSettings({ onClose }: { onClose: () => void }) {
  const [s, setS] = useState<Settings | null>(null)
  const [key, setKey] = useState('')
  const [models, setModels] = useState<string[]>([])
  const [msg, setMsg] = useState('')
  const [busy, setBusy] = useState(true)
  const [err, setErr] = useState('')

  useEffect(() => {
    ;(async () => {
      try {
        setS(await api.aiSettings())
      } catch (e) {
        setErr(String(e))
      } finally {
        setBusy(false)
      }
    })()
  }, [])

  // Escape closes, as it does in every dialog anyone has used.
  useEffect(() => {
    const on = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', on)
    return () => window.removeEventListener('keydown', on)
  }, [onClose])

  const set = (patch: Partial<Settings>) => setS((p) => (p ? { ...p, ...patch } : p))

  const probe = async () => {
    if (!s) return
    setBusy(true); setMsg(''); setErr('')
    try {
      const r = await api.probeAi({
        baseUrl: s.baseUrl, model: s.model, allowEgress: s.allowEgress,
        ...(key ? { apiKey: key } : {}),
      })
      setModels(r.models)
      if (!r.ok) {
        setErr(r.egressBlocked
          ? `外网访问被拒绝：${r.detail}。若要连接外部地址，请勾选「允许连接外部网络」。`
          : `连接失败：${r.detail}`)
      } else {
        setMsg(r.modelPresent
          ? `连接成功，模型 ${s.model} 可用（共 ${r.models.length} 个）。`
          : `连接成功，但服务端没有 ${s.model}。可用：${r.models.join('、')}`)
      }
    } catch (e) {
      setErr(String(e))
    } finally {
      setBusy(false)
    }
  }

  const save = async () => {
    if (!s) return
    setBusy(true); setMsg(''); setErr('')
    try {
      const saved = await api.saveAiSettings({
        enabled: s.enabled, allowEgress: s.allowEgress, baseUrl: s.baseUrl,
        model: s.model, timeoutS: s.timeoutS, ...(key ? { apiKey: key } : {}),
      })
      setS(saved)
      setKey('')
      setMsg('已保存，立即生效，无需重启服务。')
    } catch (e) {
      setErr(String(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}
        role="dialog" aria-label="AI 管理">
        <div className="modal-head">
          <h2>AI 管理</h2>
          <button onClick={onClose} aria-label="关闭">✕</button>
        </div>

        <div className="modal-warn">
          本平台在局域网内运行且<b>没有登录认证</b>：任何能打开本页的人都可以修改以下设置，
          包括把定量数据发往外部地址。请仅在受控网络内使用。
        </div>

        {!s ? (
          <div className="loading">{err || '读取中…'}</div>
        ) : (
          <>
            <div className="modal-body">
              <label className="fld fld-inline">
                <input type="checkbox" checked={s.enabled}
                  onChange={(e) => set({ enabled: e.target.checked })} />
                <span>启用 AI 报告</span>
                <i>关闭后仍可出报告，但由内置模板按测量值确定性生成，不联网</i>
              </label>

              <label className="fld">
                <span>接口地址</span>
                <input value={s.baseUrl} spellCheck={false}
                  onChange={(e) => set({ baseUrl: e.target.value })}
                  placeholder="https://api.deepseek.com/v1" />
                <i>OpenAI 兼容格式。院内自有模型可填 http://127.0.0.1:11434/v1（Ollama）
                  或本地 vLLM 地址</i>
              </label>

              <label className="fld">
                <span>模型</span>
                <input value={s.model} spellCheck={false} list="mriv-models"
                  onChange={(e) => set({ model: e.target.value })} />
                <datalist id="mriv-models">
                  {models.map((m) => <option key={m} value={m} />)}
                </datalist>
                <i>先点「测试连接」可列出服务端实际提供的模型，避免填了已下线的名字</i>
              </label>

              <label className="fld">
                <span>超时（秒）</span>
                <input type="number" min={1} max={600} value={s.timeoutS}
                  onChange={(e) => set({ timeoutS: Number(e.target.value) })} />
              </label>

              <label className="fld">
                <span>API Key</span>
                <input type="password" value={key} spellCheck={false}
                  onChange={(e) => setKey(e.target.value)}
                  placeholder={s.keyPresent ? '已设置（留空则保持不变）' : '未设置'} />
                <i>只写入不读出：保存后写进服务器上权限 0600 的文件，任何接口都不会回传。
                  要清除请填一个空格后保存</i>
              </label>

              <label className="fld fld-inline">
                <input type="checkbox" checked={s.allowEgress}
                  onChange={(e) => set({ allowEgress: e.target.checked })} />
                <span>允许连接外部网络</span>
                <i>关闭时只能连接本机或局域网地址，这是断网部署的默认状态</i>
              </label>

              {s.overridden.length > 0 && (
                <div className="sr-hint">
                  以下项已由本界面覆盖配置文件：{s.overridden.join('、')}
                </div>
              )}
            </div>

            {msg && <div className="modal-msg">{msg}</div>}
            {err && <div className="err">{err}</div>}

            <div className="modal-foot">
              <button onClick={probe} disabled={busy}>测试连接</button>
              <span className="spacer" />
              <button onClick={onClose}>取消</button>
              <button className="sr-primary" onClick={save} disabled={busy}>保存</button>
            </div>
          </>
        )}
      </div>
    </div>
  )
}
