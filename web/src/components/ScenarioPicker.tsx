import { Link } from 'react-router'
import type { PreviewContext, Scenario } from '../types'

interface Props {
  context: PreviewContext
  scenarios: Scenario[]
  onChange: (fn: (c: PreviewContext) => PreviewContext) => void
}

export default function ScenarioPicker({ context, scenarios, onChange }: Props) {
  const current = scenarios.find((s) => s.id === context.scenario_id)
  const turns = current?.user_turns.filter((t) => t.trim()) ?? []

  const pick = (id: number) => {
    const s = scenarios.find((x) => x.id === id)
    onChange((c) => ({ ...c, scenario_id: id, user_input: s?.user_turns.find((t) => t.trim()) ?? '' }))
  }

  return (
    <details className="panel scenario-panel" open>
      <summary>情境與本次輸入</summary>
      <div className="form-row">
        <label>
          情境
          <select value={context.scenario_id ?? ''} onChange={(e) => pick(Number(e.target.value))}>
            {!current && <option value="">（請選擇）</option>}
            {scenarios.map((s) => (
              <option key={s.id} value={s.id!}>
                {s.name}
              </option>
            ))}
          </select>
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
              title="換一個 seed（重擲 random / roll / pick，並作為 API seed）"
              onClick={() => onChange((c) => ({ ...c, seed: Math.floor(Math.random() * 1e6) }))}
            >
              🎲
            </button>
          </span>
        </label>
        <span className="picker-link">
          <Link to={current ? `/scenarios?id=${current.id}` : '/scenarios'}>編輯情境 →</Link>
        </span>
      </div>
      {current && (
        <p className="muted small scenario-summary">
          {current.char} × {current.user} · 開場{current.opening ? '有' : '無'} · 既有歷史 {current.history.length} 則
        </p>
      )}
      <label className="stack">
        <span>
          本次使用者輸入（接在歷史最後；留空則不加）
          {turns.length > 1 &&
            turns.map((t, i) => (
              <button
                key={i}
                className={`chip chip-source ${t === context.user_input ? 'active' : ''}`}
                title={t}
                onClick={() => onChange((c) => ({ ...c, user_input: t }))}
              >
                回合 {i + 1}
              </button>
            ))}
        </span>
        <textarea value={context.user_input} onChange={(e) => onChange((c) => ({ ...c, user_input: e.target.value }))} />
      </label>
    </details>
  )
}
