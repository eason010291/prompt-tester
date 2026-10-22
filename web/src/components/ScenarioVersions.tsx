import { useState } from 'react'
import { api } from '../api'
import { useResource } from '../hooks/useResource'
import { authorLabel, type Scenario, type ScenarioDiff, type ScenarioRevisionState } from '../types'
import ScenarioDiffView from './ScenarioDiffView'

/** 情境的線性版本：目前狀態、存成版本、版本紀錄（diff 與還原）。 */
export default function ScenarioVersions({
  scenarioId,
  refreshKey,
  onRestored,
}: {
  scenarioId: number
  /** 情境內容儲存後改變，用來重新讀取狀態 */
  refreshKey: string
  onRestored: (s: Scenario) => void
}) {
  const state = useResource<ScenarioRevisionState>(`/scenarios/${scenarioId}/revisions?v=${encodeURIComponent(refreshKey)}`)
  const [message, setMessage] = useState('')
  const [saving, setSaving] = useState(false)
  const [selected, setSelected] = useState<number | null>(null)
  const [error, setError] = useState<string | null>(null)
  const diff = useResource<ScenarioDiff>(selected ? `/scenarios/${scenarioId}/diff?a=${selected}&v=${encodeURIComponent(refreshKey)}` : null)
  const s = state.data
  const revs = s?.revisions ?? []
  const current = revs[0]

  const save = async () => {
    setSaving(true)
    setError(null)
    try {
      await api(`/scenarios/${scenarioId}/revisions`, { method: 'POST', body: JSON.stringify({ message }) })
      setMessage('')
      state.reload()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setSaving(false)
    }
  }

  const restore = async (rid: number, number: number) => {
    if (!window.confirm(`把情境內容換成版本 #${number}？目前內容${s?.dirty ? '（有尚未存成版本的修改）' : ''}會被取代；要保留請先存成版本。`)) return
    const restored = await api<Scenario>(`/scenarios/${scenarioId}/revisions/${rid}/restore`, { method: 'POST' })
    setSelected(null)
    onRestored(restored)
  }

  if (!s) return null
  return (
    <div className="card scenario-versions">
      <div className="version-line">
        <strong className="small">版本</strong>
        <span className="small">
          {current ? `#${revs.length}（${authorLabel(current.author)}：${current.message}）` : '還沒有版本'}
          {' · '}
          {s.dirty ? <span className="warn-text">有尚未存成版本的修改</span> : <span className="muted">與版本一致</span>}
        </span>
        {s.dirty && (
          <>
            <input className="version-msg" value={message} placeholder="版本說明（選填）" onChange={(e) => setMessage(e.target.value)} />
            <button className="btn btn-sm btn-primary" onClick={save} disabled={saving}>
              存成版本
            </button>
          </>
        )}
        {error && <span className="alert alert-error">{error}</span>}
      </div>
      {revs.length > 0 && (
        <details className="version-history">
          <summary className="small">版本紀錄（{revs.length}）· 建立實驗、LLM 修改角色卡時也會自動存一版</summary>
          <ul>
            {revs.map((r) => (
              <li key={r.id} className={selected === r.id ? 'active' : ''}>
                <button className="link-btn" onClick={() => setSelected(selected === r.id ? null : r.id)}>
                  #{r.number}
                </button>{' '}
                <span className="small">
                  {authorLabel(r.author)} · {r.message} · {new Date(r.created_at).toLocaleString('zh-TW', { dateStyle: 'short', timeStyle: 'short' })}
                </span>
                {selected === r.id && (
                  <div className="version-detail">
                    {diff.data && <ScenarioDiffView diff={diff.data} labels={[`版本 #${r.number}`, '目前內容']} />}
                    <button className="btn btn-sm" onClick={() => restore(r.id, r.number)}>
                      還原到這版
                    </button>
                  </div>
                )}
              </li>
            ))}
          </ul>
        </details>
      )}
    </div>
  )
}
