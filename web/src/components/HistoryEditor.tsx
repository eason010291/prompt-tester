import type { HistoryMessage, Role } from '../types'

interface Props {
  history: HistoryMessage[]
  onChange: (history: HistoryMessage[]) => void
  label?: string
}

export default function HistoryEditor({ history, onChange, label = '既有歷史（由舊到新）' }: Props) {
  const set = (i: number, patch: Partial<HistoryMessage>) =>
    onChange(history.map((h, j) => (j === i ? { ...h, ...patch } : h)))

  return (
    <div className="history-editor">
      <span className="label">{label}</span>
      {history.map((h, i) => (
        <div key={i} className="history-row">
          <select
            className={`role-select role-${h.role}`}
            value={h.role}
            aria-label={`第 ${i + 1} 則角色`}
            onChange={(e) => set(i, { role: e.target.value as Role })}
          >
            <option value="user">user</option>
            <option value="assistant">assistant</option>
            <option value="system">system</option>
          </select>
          <textarea
            value={h.content}
            aria-label={`第 ${i + 1} 則內容`}
            onChange={(e) => set(i, { content: e.target.value })}
          />
          <button
            className="icon-btn"
            aria-label={`刪除第 ${i + 1} 則`}
            onClick={() => onChange(history.filter((_, j) => j !== i))}
          >
            ✕
          </button>
        </div>
      ))}
      <div className="add-bar">
        {(['user', 'assistant'] as Role[]).map((role) => (
          <button key={role} className="btn btn-sm" onClick={() => onChange([...history, { role, content: '' }])}>
            ＋ {role}
          </button>
        ))}
      </div>
    </div>
  )
}
