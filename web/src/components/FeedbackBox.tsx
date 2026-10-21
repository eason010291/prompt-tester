import { useState } from 'react'
import { api } from '../api'
import { feedbackTemplate, parseScores } from '../lib/feedback'
import { formatScore, type Feedback, type FeedbackTarget } from '../types'

interface Props {
  targetType: FeedbackTarget
  targetId: number
  /** 已存在的評語（呼叫端一次讀取整個試跑的評語後依目標篩選） */
  items: Feedback[]
  onChanged: () => void
  /** 訊息上的小型版本：沒有評語時只顯示一個按鈕 */
  compact?: boolean
  label?: string
}

export default function FeedbackBox({ targetType, targetId, items, onChanged, compact, label = '評語' }: Props) {
  const [draft, setDraft] = useState<{ id: number | null; text: string } | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const startNew = async () => setDraft({ id: null, text: await feedbackTemplate() })

  const save = async () => {
    if (!draft) return
    setBusy(true)
    setError(null)
    try {
      if (draft.id === null) {
        await api('/feedback', { method: 'POST', body: JSON.stringify({ target_type: targetType, target_id: targetId, text: draft.text }) })
      } else {
        await api(`/feedback/${draft.id}`, { method: 'PUT', body: JSON.stringify({ text: draft.text }) })
      }
      setDraft(null)
      onChanged()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  const remove = async (f: Feedback) => {
    if (!window.confirm('刪除這則評語？')) return
    await api(`/feedback/${f.id}`, { method: 'DELETE' })
    onChanged()
  }

  if (compact && items.length === 0 && !draft) {
    return (
      <button className="btn btn-sm feedback-add" onClick={startNew}>
        ✎ {label}
      </button>
    )
  }

  const preview = draft ? parseScores(draft.text) : {}
  return (
    <div className={`feedback-box ${compact ? 'is-compact' : ''}`}>
      {!compact && <h3>{label}</h3>}
      {items.map((f) =>
        draft?.id === f.id ? null : (
          <div key={f.id} className="feedback-item">
            <ScoreChips scores={f.scores} />
            <pre className="feedback-text">{f.text}</pre>
            <div className="feedback-actions">
              <span className="muted small">{new Date(f.updated_at).toLocaleString('zh-TW', { dateStyle: 'short', timeStyle: 'short' })}</span>
              <button className="btn btn-sm" onClick={() => setDraft({ id: f.id, text: f.text })} disabled={!!draft}>
                編輯
              </button>
              <button className="icon-btn" aria-label="刪除評語" onClick={() => remove(f)}>
                ✕
              </button>
            </div>
          </div>
        ),
      )}
      {draft ? (
        <div className="feedback-edit">
          <textarea
            autoFocus
            value={draft.text}
            onChange={(e) => setDraft({ ...draft, text: e.target.value })}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) void save()
            }}
            aria-label="評語內容"
          />
          <div className="feedback-actions">
            {Object.keys(preview).length ? <ScoreChips scores={preview} /> : <span className="muted small">寫成「屬性：7/10」的行會解析成分數</span>}
            <button className="btn btn-sm" onClick={() => setDraft(null)} disabled={busy}>
              取消
            </button>
            <button className="btn btn-sm btn-primary" onClick={save} disabled={busy || !draft.text.trim()} title="Ctrl + Enter">
              儲存
            </button>
          </div>
          {error && <p className="alert alert-error">{error}</p>}
        </div>
      ) : (
        <button className="btn btn-sm feedback-add" onClick={startNew}>
          ＋ 新增{label}
        </button>
      )}
    </div>
  )
}

export function ScoreChips({ scores }: { scores: Record<string, number> }) {
  const entries = Object.entries(scores)
  if (!entries.length) return null
  return (
    <span className="score-chips">
      {entries.map(([k, v]) => (
        <span key={k} className="chip score-chip" data-level={scoreLevel(v)}>
          {k} <b>{formatScore(v)}</b>
        </span>
      ))}
    </span>
  )
}

export function scoreLevel(v: number | null | undefined): 'hi' | 'mid' | 'lo' | 'none' {
  if (v == null) return 'none'
  return v >= 7.5 ? 'hi' : v >= 5 ? 'mid' : 'lo'
}
