import { useState, type ReactNode } from 'react'
import { api } from '../api'
import { useResource } from '../hooks/useResource'
import type { RetryResult } from '../types'
import Modal from './Modal'

export interface RetryRequest {
  title: string
  /** 範圍與影響說明 */
  description: ReactNode
  /** 送給 /retry 的內容（items 或 stage、user_text…），對話框會補上原因與 later_users */
  body: Record<string, unknown>
  /** 會連帶重跑的下游分支數 */
  cascade: number
  /** 這次重試會影響之後回合的使用者發言時才顯示選項 */
  askLater: boolean
}

/** 重試前確認：勾選原因（固定標籤 + 其他）、之後的使用者發言沿用或重新產生。 */
export default function RetryDialog({
  experimentId,
  req,
  defaultLater,
  onDone,
  onClose,
}: {
  experimentId: number
  req: RetryRequest
  defaultLater: 'keep' | 'regenerate'
  onDone: (r: RetryResult) => void
  onClose: () => void
}) {
  const reasons = useResource<{ reasons: string[] }>('/retry-reasons')
  const [tags, setTags] = useState<string[]>([])
  const [other, setOther] = useState('')
  const [later, setLater] = useState(defaultLater)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const toggle = (t: string) => setTags((cur) => (cur.includes(t) ? cur.filter((x) => x !== t) : [...cur, t]))

  const submit = async () => {
    setBusy(true)
    setError(null)
    try {
      const r = await api<RetryResult>(`/experiments/${experimentId}/retry`, {
        method: 'POST',
        body: JSON.stringify({ ...req.body, later_users: later, tags: other.trim() ? [...tags, '其他'] : tags, note: other.trim() }),
      })
      onDone(r)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
      setBusy(false)
    }
  }

  return (
    <Modal
      title={req.title}
      onClose={onClose}
      footer={
        <>
          {error && <span className="alert alert-error">{error}</span>}
          <button className="btn" onClick={onClose} disabled={busy}>
            取消
          </button>
          <button className="btn btn-primary" onClick={submit} disabled={busy}>
            {busy ? '送出中…' : '重試'}
          </button>
        </>
      }
    >
      <div className="retry-desc">{req.description}</div>
      {req.cascade > 0 && (
        <p className="alert">
          接在後面的 <b>{req.cascade}</b> 條下一階段分支會一起封存，等上層跑完後以新的對話重建並重跑（會產生費用）。
        </p>
      )}
      <fieldset className="reason-tags">
        <legend>重試原因（選填，用於統計模型的重試率）</legend>
        {reasons.data?.reasons.map((t) => (
          <label key={t} className={`tag-check ${tags.includes(t) ? 'on' : ''}`}>
            <input type="checkbox" checked={tags.includes(t)} onChange={() => toggle(t)} />
            {t}
          </label>
        ))}
        <input className="reason-other" value={other} placeholder="其他（自行輸入）" onChange={(e) => setOther(e.target.value)} />
      </fieldset>
      {req.askLater && (
        <label className="inline small">
          之後回合的使用者發言
          <select value={later} onChange={(e) => setLater(e.target.value as 'keep' | 'regenerate')}>
            <option value="keep">沿用原本的</option>
            <option value="regenerate">重新產生</option>
          </select>
        </label>
      )}
    </Modal>
  )
}
