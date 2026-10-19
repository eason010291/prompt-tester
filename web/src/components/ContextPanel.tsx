import type { PreviewContext, Role } from '../types'

interface Props {
  context: PreviewContext
  onChange: (fn: (c: PreviewContext) => PreviewContext) => void
}

/** M1 預覽用的簡易情境；M2 起改由正式的情境管理取代。 */
export default function ContextPanel({ context, onChange }: Props) {
  const setHistory = (i: number, patch: Partial<PreviewContext['history'][number]>) =>
    onChange((c) => ({ ...c, history: c.history.map((h, j) => (j === i ? { ...h, ...patch } : h)) }))

  return (
    <details className="panel" open>
      <summary>預覽情境</summary>
      <div className="form-row">
        <label>
          {'{{char}}'}
          <input value={context.char} onChange={(e) => onChange((c) => ({ ...c, char: e.target.value }))} />
        </label>
        <label>
          {'{{user}}'}
          <input value={context.user} onChange={(e) => onChange((c) => ({ ...c, user: e.target.value }))} />
        </label>
        <label>
          隨機 seed
          <span className="input-group">
            <input
              type="number"
              value={context.seed}
              onChange={(e) => onChange((c) => ({ ...c, seed: Number(e.target.value) }))}
            />
            <button
              className="btn btn-sm"
              title="換一個 seed（重擲 random / roll / pick）"
              onClick={() => onChange((c) => ({ ...c, seed: Math.floor(Math.random() * 1e6) }))}
            >
              🎲
            </button>
          </span>
        </label>
      </div>

      <div className="history-editor">
        <span className="label">聊天歷史（由舊到新）</span>
        {context.history.map((h, i) => (
          <div key={i} className="history-row">
            <select
              className={`role-select role-${h.role}`}
              value={h.role}
              aria-label={`第 ${i + 1} 則角色`}
              onChange={(e) => setHistory(i, { role: e.target.value as Role })}
            >
              <option value="user">user</option>
              <option value="assistant">assistant</option>
              <option value="system">system</option>
            </select>
            <textarea
              value={h.content}
              aria-label={`第 ${i + 1} 則內容`}
              onChange={(e) => setHistory(i, { content: e.target.value })}
            />
            <button
              className="icon-btn"
              aria-label={`刪除第 ${i + 1} 則`}
              onClick={() => onChange((c) => ({ ...c, history: c.history.filter((_, j) => j !== i) }))}
            >
              ✕
            </button>
          </div>
        ))}
        <div className="add-bar">
          {(['user', 'assistant'] as Role[]).map((role) => (
            <button
              key={role}
              className="btn btn-sm"
              onClick={() => onChange((c) => ({ ...c, history: [...c.history, { role, content: '' }] }))}
            >
              ＋ {role}
            </button>
          ))}
        </div>
      </div>
    </details>
  )
}
