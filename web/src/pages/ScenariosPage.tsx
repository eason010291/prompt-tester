import { useState, type ChangeEvent } from 'react'
import { useSearchParams } from 'react-router'
import { api } from '../api'
import CardSection from '../components/CardSection'
import HistoryEditor from '../components/HistoryEditor'
import { useAutosave, useResource } from '../hooks/useResource'
import { newScenario, type Scenario } from '../types'

const SAVE_LABEL = { saved: '已儲存', dirty: '未儲存的變更', saving: '儲存中…', error: '儲存失敗' }

export default function ScenariosPage() {
  const { data: list, setData: setList, error } = useResource<Scenario[]>('/scenarios')
  const [params, setParams] = useSearchParams()
  const selectedId = Number(params.get('id')) || list?.[0]?.id || null
  const selected = list?.find((s) => s.id === selectedId) ?? null

  const onSaved = (saved: Scenario) => setList((cur) => cur?.map((x) => (x.id === saved.id ? saved : x)) ?? cur)

  const select = (id: number) => setParams({ id: String(id) })

  const create = async (base?: Scenario) => {
    const body = base ? { ...base, id: null, name: `${base.name}（複製）` } : newScenario()
    const created = await api<Scenario>('/scenarios', { method: 'POST', body: JSON.stringify(body) })
    setList((cur) => [...(cur ?? []), created])
    select(created.id!)
  }

  const [notes, setNotes] = useState<{ title: string; lines: string[] } | null>(null)

  const importCard = async (e: ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0]
    e.target.value = ''
    if (!file) return
    try {
      const bytes = new Uint8Array(await file.arrayBuffer())
      let binary = ''
      for (let i = 0; i < bytes.length; i += 0x8000) binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000))
      const res = await api<{ scenario: Scenario; notes: string[] }>('/scenarios/import-card', {
        method: 'POST',
        body: JSON.stringify({ filename: file.name, data_base64: btoa(binary) }),
      })
      setList((cur) => [...(cur ?? []), res.scenario])
      select(res.scenario.id!)
      setNotes({ title: `已匯入角色卡「${res.scenario.name}」`, lines: res.notes })
    } catch (err) {
      setNotes({ title: '匯入失敗', lines: [err instanceof Error ? err.message : String(err)] })
    }
  }

  const remove = async (s: Scenario) => {
    if (!window.confirm(`刪除情境「${s.name}」？已產生的生成紀錄會保留情境快照。`)) return
    await api(`/scenarios/${s.id}`, { method: 'DELETE' })
    const rest = (list ?? []).filter((x) => x.id !== s.id)
    setList(rest)
    setParams(rest[0] ? { id: String(rest[0].id) } : {})
  }

  if (error) return <p className="alert alert-error">無法載入情境：{error}</p>

  return (
    <div className="two-pane">
      <nav className="list-pane" aria-label="情境列表">
        <div className="list-head">
          <h1>情境</h1>
          <button className="btn btn-sm" onClick={() => create()}>
            ＋ 新增
          </button>
        </div>
        <label className="btn btn-sm import-card" title="SillyTavern 角色卡（V2 / V3 的 JSON 或 PNG）">
          匯入角色卡
          <input type="file" accept=".json,.png,application/json,image/png" hidden onChange={importCard} />
        </label>
        {notes && (
          <div className="alert notes" role="status">
            <strong>{notes.title}</strong>
            <ul>
              {notes.lines.map((n) => (
                <li key={n}>{n}</li>
              ))}
            </ul>
            <button className="icon-btn" aria-label="關閉" onClick={() => setNotes(null)}>
              ✕
            </button>
          </div>
        )}
        {list?.map((s) => (
          <button
            key={s.id}
            className={`list-item ${s.id === selectedId ? 'active' : ''}`}
            onClick={() => select(s.id!)}
          >
            <strong>{s.name}</strong>
            <small>
              {s.card ? '🪪 ' : ''}
              {s.char} × {s.user} · {s.history.length + (s.opening ? 1 : 0)} 則歷史 · {s.user_turns.filter(Boolean).length} 回合輸入
            </small>
          </button>
        ))}
        {list?.length === 0 && <p className="muted small">還沒有情境。</p>}
      </nav>

      {selected && (
        <ScenarioEditor
          key={selected.id}
          initial={selected}
          onSaved={onSaved}
          onDuplicate={create}
          onDelete={remove}
        />
      )}
    </div>
  )
}

function VarsEditor({ vars, onChange }: { vars: Record<string, string>; onChange: (v: Record<string, string>) => void }) {
  const entries = Object.entries(vars)
  const update = (i: number, key: string, value: string) =>
    onChange(Object.fromEntries(entries.map((e, j) => (j === i ? [key, value] : e))))

  return (
    <div className="vars-editor">
      <span className="label">其他變數（在區塊中以 {'{{名稱}}'} 引用）</span>
      {entries.map(([k, v], i) => (
        <div key={i} className="var-row">
          <input value={k} aria-label="變數名稱" placeholder="名稱" onChange={(e) => update(i, e.target.value, v)} />
          <input value={v} aria-label="變數值" placeholder="值" onChange={(e) => update(i, k, e.target.value)} />
          <button
            className="icon-btn"
            aria-label={`刪除變數 ${k}`}
            onClick={() => onChange(Object.fromEntries(entries.filter((_, j) => j !== i)))}
          >
            ✕
          </button>
        </div>
      ))}
      <div className="add-bar">
        <button
          className="btn btn-sm"
          onClick={() => onChange({ ...vars, [`var${entries.length + 1}`]: '' })}
        >
          ＋ 變數
        </button>
      </div>
    </div>
  )
}

interface EditorProps {
  initial: Scenario
  onSaved: (s: Scenario) => void
  onDuplicate: (s: Scenario) => void
  onDelete: (s: Scenario) => void
}

function ScenarioEditor({ initial, onSaved, onDuplicate, onDelete }: EditorProps) {
  const [draft, setDraft] = useState(initial)
  const state = useAutosave(draft, async (s) => {
    onSaved(await api<Scenario>(`/scenarios/${s.id}`, { method: 'PUT', body: JSON.stringify(s) }))
  })
  const set = (patch: Partial<Scenario>) => setDraft((d) => ({ ...d, ...patch }))

  return (
      <section className="detail-pane">
        <header className="page-header row">
          <input className="title-input" value={draft.name} aria-label="情境名稱" onChange={(e) => set({ name: e.target.value })} />
          <span className={`save-state save-${state}`}>{SAVE_LABEL[state]}</span>
          <div className="header-actions">
            <button className="btn btn-sm" onClick={() => onDuplicate(draft)}>
              複製
            </button>
            <button className="btn btn-sm danger" onClick={() => onDelete(draft)}>
              刪除
            </button>
          </div>
        </header>

        <div className="card">
          <div className="form-grid">
            <label>
              {'{{char}}'}
              <input value={draft.char} onChange={(e) => set({ char: e.target.value })} />
            </label>
            <label>
              {'{{user}}'}
              <input value={draft.user} onChange={(e) => set({ user: e.target.value })} />
            </label>
          </div>
          <VarsEditor vars={draft.vars} onChange={(vars) => set({ vars })} />
          <label className="stack">
            {'{{user}}'} 的 persona 描述（填入 personaDescription marker，可留空）
            <textarea value={draft.persona} onChange={(e) => set({ persona: e.target.value })} />
          </label>
        </div>

        {draft.card && (
          <CardSection
            card={draft.card}
            opening={draft.opening}
            onChange={(card) => set({ card })}
            onOpening={(opening) => set({ opening })}
            onRemove={() => set({ card: null })}
          />
        )}

        <div className="card">
          <label className="stack">
            開場白（第一則 assistant 訊息，可留空）
            <textarea className="tall" value={draft.opening} onChange={(e) => set({ opening: e.target.value })} />
          </label>
          <HistoryEditor history={draft.history} onChange={(history) => set({ history })} />
        </div>

        <div className="card">
          <span className="label">逐回合使用者輸入（單次生成帶入第 1 則；M4 多回合依序使用）</span>
          {draft.user_turns.map((t, i) => (
            <div key={i} className="history-row">
              <span className="turn-no">#{i + 1}</span>
              <textarea
                value={t}
                aria-label={`第 ${i + 1} 回合輸入`}
                onChange={(e) => set({ user_turns: draft.user_turns.map((x, j) => (j === i ? e.target.value : x)) })}
              />
              <button
                className="icon-btn"
                aria-label={`刪除第 ${i + 1} 回合`}
                onClick={() => set({ user_turns: draft.user_turns.filter((_, j) => j !== i) })}
              >
                ✕
              </button>
            </div>
          ))}
          <div className="add-bar">
            <button className="btn btn-sm" onClick={() => set({ user_turns: [...draft.user_turns, ''] })}>
              ＋ 回合
            </button>
          </div>
        </div>

        <div className="card">
          <label className="stack">
            備註
            <textarea value={draft.notes} onChange={(e) => set({ notes: e.target.value })} />
          </label>
        </div>
      </section>
  )
}
