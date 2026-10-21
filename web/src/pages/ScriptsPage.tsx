import { useMemo, useState } from 'react'
import { useSearchParams } from 'react-router'
import { api } from '../api'
import { useAutosave, useResource } from '../hooks/useResource'
import { estimateScript } from '../lib/runs'
import {
  PROMOTE_LABEL,
  USER_SOURCE_LABEL,
  type PromoteMode,
  type Script,
  type ScriptData,
  type Stage,
  type UserSource,
} from '../types'

const SAVE_LABEL = { saved: '已儲存', dirty: '未儲存的變更', saving: '儲存中…', error: '儲存失敗' }

/** 依父子關係排成樹（深度優先），回傳 [腳本, 深度] */
function treeOrder(list: Script[]): [Script, number][] {
  const children = new Map<number | null, Script[]>()
  const ids = new Set(list.map((s) => s.id))
  for (const s of list) {
    const p = s.parent_id !== null && ids.has(s.parent_id) ? s.parent_id : null
    children.set(p, [...(children.get(p) ?? []), s])
  }
  const out: [Script, number][] = []
  const walk = (p: number | null, depth: number) => {
    for (const s of children.get(p) ?? []) {
      out.push([s, depth])
      walk(s.id, depth + 1)
    }
  }
  walk(null, 0)
  return out
}

function descendants(list: Script[], id: number): Set<number> {
  const out = new Set<number>([id])
  let grew = true
  while (grew) {
    grew = false
    for (const s of list) {
      if (s.parent_id !== null && out.has(s.parent_id) && !out.has(s.id)) {
        out.add(s.id)
        grew = true
      }
    }
  }
  return out
}

export default function ScriptsPage() {
  const scripts = useResource<Script[]>('/scripts')
  const [params, setParams] = useSearchParams()
  const list = useMemo(() => scripts.data ?? [], [scripts.data])
  const selectedId = Number(params.get('id')) || list[0]?.id || null
  const selected = list.find((s) => s.id === selectedId) ?? null
  const ordered = useMemo(() => treeOrder(list), [list])

  const select = (id: number) => setParams({ id: String(id) })

  const create = async (parent: Script | null) => {
    const created = await api<Script>('/scripts', {
      method: 'POST',
      body: JSON.stringify(parent ? { name: `${parent.name}-子腳本`, parent_id: parent.id } : { name: '新腳本' }),
    })
    scripts.reload()
    select(created.id)
  }

  const remove = async (s: Script) => {
    if (!window.confirm(`刪除腳本「${s.name}」？已建立的實驗保留各自的腳本快照。`)) return
    try {
      await api(`/scripts/${s.id}`, { method: 'DELETE' })
      setParams({})
      scripts.reload()
    } catch (e) {
      window.alert(e instanceof Error ? e.message : String(e))
    }
  }

  if (scripts.error) return <p className="alert alert-error">無法載入腳本：{scripts.error}</p>

  return (
    <div className="two-pane">
      <nav className="list-pane" aria-label="腳本列表">
        <div className="list-head">
          <h1>腳本</h1>
          <button className="btn btn-sm" onClick={() => create(null)}>
            ＋ 新增
          </button>
        </div>
        <p className="muted small">腳本決定金字塔各階段的回合數、分支數與晉級方式。子腳本只存與父腳本不同的地方，父腳本修改會傳下去。</p>
        {ordered.map(([s, depth]) => (
          <button
            key={s.id}
            className={`list-item ${s.id === selectedId ? 'active' : ''}`}
            style={{ marginLeft: depth * 16 }}
            onClick={() => select(s.id)}
          >
            <strong>
              {depth > 0 && <span className="muted">└ </span>}
              {s.name}
            </strong>
            <small>
              {s.resolved.stages.map((st) => `${st.turns}回×${st.seeds}${st.temperatures.length > 1 ? `×${st.temperatures.length}溫` : ''}`).join(' → ')}
              {s.parent_id !== null && ` · 覆寫 ${Object.keys(s.overrides).length} 項`}
            </small>
          </button>
        ))}
      </nav>

      {selected && (
        <ScriptEditor
          key={selected.id}
          script={selected}
          all={list}
          onSaved={scripts.reload}
          onChild={() => create(selected)}
          onDelete={() => remove(selected)}
        />
      )}
    </div>
  )
}

interface Draft {
  name: string
  notes: string
  data: ScriptData
}

function ScriptEditor({
  script,
  all,
  onSaved,
  onChild,
  onDelete,
}: {
  script: Script
  all: Script[]
  onSaved: () => void
  onChild: () => void
  onDelete: () => void
}) {
  const [draft, setDraft] = useState<Draft>({ name: script.name, notes: script.notes, data: script.resolved })
  const [current, setCurrent] = useState(script)
  const [error, setError] = useState<string | null>(null)

  const put = async (body: Record<string, unknown>) => {
    try {
      const saved = await api<Script>(`/scripts/${script.id}`, { method: 'PUT', body: JSON.stringify(body) })
      setCurrent(saved)
      setError(null)
      return saved
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
      throw e
    }
  }

  // 名稱、備註、數值：自動儲存，伺服器算出覆寫
  const state = useAutosave(draft, async (d) => {
    await put({ name: d.name, notes: d.notes, parent_id: current.parent_id, data: d.data })
    onSaved()
  })
  // 換父腳本、還原繼承：立即儲存，編輯中的數值改用伺服器解析的結果
  const immediate = async (body: Record<string, unknown>) => {
    const saved = await put({ name: draft.name, notes: draft.notes, parent_id: current.parent_id, ...body })
    setDraft((d) => ({ ...d, data: saved.resolved }))
    onSaved()
  }

  const isChild = current.parent_id !== null
  const ov = current.overrides
  const wholeStages = isChild && 'stages' in ov
  const overridden = (path: string) => isChild && path in ov
  const setData = (patch: Partial<ScriptData>) => setDraft((d) => ({ ...d, data: { ...d.data, ...patch } }))
  const setStage = (i: number, patch: Partial<Stage>) =>
    setData({ stages: draft.data.stages.map((s, j) => (j === i ? { ...s, ...patch } : s)) })
  const excluded = descendants(all, script.id)

  return (
    <section className="detail-pane script-editor">
      <header className="page-header row">
        <input className="title-input" value={draft.name} aria-label="腳本名稱" onChange={(e) => setDraft({ ...draft, name: e.target.value })} />
        <span className={`save-state save-${state}`}>{SAVE_LABEL[state]}</span>
        <div className="header-actions">
          <button className="btn btn-sm" onClick={onChild} title="建立繼承這個腳本的子腳本">
            建立子腳本
          </button>
          <button className="btn btn-sm danger" onClick={onDelete}>
            刪除
          </button>
        </div>
      </header>
      {error && <p className="alert alert-error">{error}</p>}
      {current.warnings.map((w) => (
        <p key={w} className="alert">
          {w}
        </p>
      ))}

      <div className="card">
        <div className="form-grid">
          <label>
            父腳本
            <select value={current.parent_id ?? ''} onChange={(e) => immediate({ parent_id: Number(e.target.value) || null })}>
              <option value="">（無，根腳本）</option>
              {all
                .filter((s) => !excluded.has(s.id))
                .map((s) => (
                  <option key={s.id} value={s.id}>
                    {s.name}
                  </option>
                ))}
            </select>
          </label>
          <label>
            <span>
              使用者發言
              <OverrideMark on={overridden('user_source')} onReset={() => immediate({ reset: ['user_source'] })} />
            </span>
            <select value={draft.data.user_source} onChange={(e) => setData({ user_source: e.target.value as UserSource })}>
              {Object.entries(USER_SOURCE_LABEL).map(([k, v]) => (
                <option key={k} value={k}>
                  {v}
                </option>
              ))}
            </select>
          </label>
          <label>
            <span>
              同時執行的分支數
              <OverrideMark on={overridden('concurrency')} onReset={() => immediate({ reset: ['concurrency'] })} />
            </span>
            <input type="number" min={1} max={16} value={draft.data.concurrency} onChange={(e) => setData({ concurrency: Number(e.target.value) })} />
          </label>
        </div>
        <label className="stack">
          備註
          <textarea value={draft.notes} onChange={(e) => setDraft({ ...draft, notes: e.target.value })} />
        </label>
      </div>

      <div className="card">
        <div className="list-head">
          <h2>
            階段
            <OverrideMark on={wholeStages} label="整個階段表已覆寫" onReset={() => immediate({ reset: ['stages'] })} />
          </h2>
          <span className="muted small">回合數是「累計」：本階段結束時對話的總回合數。分支數是每個起點（情境或晉級的分支）長出幾條。</span>
        </div>
        <table className="ep-table stage-table">
          <thead>
            <tr>
              <th>#</th>
              <th>名稱</th>
              <th>累計回合</th>
              <th>分支數</th>
              <th>溫度掃描</th>
              <th>結束後晉級</th>
              <th>K</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {draft.data.stages.map((st, i) => {
              const last = i === draft.data.stages.length - 1
              const p = (f: string) => `stages[${i}].${f}`
              const mark = (f: string) => <OverrideMark on={overridden(p(f))} onReset={() => immediate({ reset: [p(f)] })} />
              return (
                <tr key={i}>
                  <td>{i + 1}</td>
                  <td>
                    <input value={st.name} onChange={(e) => setStage(i, { name: e.target.value })} aria-label="階段名稱" />
                    {mark('name')}
                  </td>
                  <td>
                    <input type="number" min={1} max={30} value={st.turns} onChange={(e) => setStage(i, { turns: Number(e.target.value) })} aria-label="累計回合" />
                    {mark('turns')}
                  </td>
                  <td>
                    <input type="number" min={1} max={50} value={st.seeds} onChange={(e) => setStage(i, { seeds: Number(e.target.value) })} aria-label="分支數" />
                    {mark('seeds')}
                  </td>
                  <td>
                    <TempInput value={st.temperatures} onChange={(temperatures) => setStage(i, { temperatures })} />
                    {mark('temperatures')}
                  </td>
                  <td>
                    {last ? (
                      <span className="muted">—（最後一階段）</span>
                    ) : (
                      <>
                        <select value={st.promote.mode} onChange={(e) => setStage(i, { promote: { ...st.promote, mode: e.target.value as PromoteMode } })}>
                          {Object.entries(PROMOTE_LABEL).map(([k, v]) => (
                            <option key={k} value={k}>
                              {v}
                            </option>
                          ))}
                        </select>
                        {mark('promote.mode')}
                      </>
                    )}
                  </td>
                  <td>
                    {!last && (st.promote.mode === 'top_k' || st.promote.mode === 'llm_assist' || st.promote.mode === 'manual') && (
                      <>
                        <input type="number" min={1} max={50} value={st.promote.k} onChange={(e) => setStage(i, { promote: { ...st.promote, k: Number(e.target.value) } })} aria-label="K" />
                        {mark('promote.k')}
                      </>
                    )}
                  </td>
                  <td>
                    {draft.data.stages.length > 1 && (
                      <button className="icon-btn" aria-label={`刪除第 ${i + 1} 階段`} onClick={() => setData({ stages: draft.data.stages.filter((_, j) => j !== i) })}>
                        ✕
                      </button>
                    )}
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
        <div className="add-bar">
          <button
            className="btn btn-sm"
            disabled={draft.data.stages.length >= 8}
            onClick={() => {
              const prev = draft.data.stages[draft.data.stages.length - 1]
              setData({ stages: [...draft.data.stages, { name: '', turns: Math.min(30, prev.turns + 3), seeds: 1, temperatures: [], promote: { mode: 'manual', k: 3 } }] })
            }}
          >
            ＋ 階段
          </button>
          <span className="muted small">
            「人工挑選」的 K 只用於下方估算。晉級時一律由你確認勾選；「分數前 K」與「LLM 評審預選」會先幫你勾好。
          </span>
        </div>
      </div>

      <Estimate data={draft.data} />
    </section>
  )
}

function OverrideMark({ on, label = '覆寫', onReset }: { on: boolean; label?: string; onReset: () => void }) {
  if (!on) return null
  return (
    <span className="chip chip-override" title="這個欄位與父腳本不同">
      {label}
      <button className="link-btn" onClick={(e) => (e.preventDefault(), onReset())}>
        還原繼承
      </button>
    </span>
  )
}

function TempInput({ value, onChange }: { value: number[]; onChange: (v: number[]) => void }) {
  const [text, setText] = useState(value.join(', '))
  return (
    <input
      value={text}
      placeholder="預設溫度"
      aria-label="溫度掃描"
      title="以逗號分隔，例如 0.3, 1.0, 1.4；留空使用預設的溫度"
      onChange={(e) => {
        setText(e.target.value)
        const nums = e.target.value
          .split(/[,，\s]+/)
          .filter(Boolean)
          .map(Number)
        if (nums.every((n) => Number.isFinite(n) && n >= 0 && n <= 2)) onChange(nums)
      }}
    />
  )
}

/** 估算每階段的分支數與呼叫次數（人工挑選假設挑 K 條） */
function Estimate({ data }: { data: ScriptData }) {
  const [scenarios, setScenarios] = useState(1)
  const { rows, total } = estimateScript(data, scenarios)
  return (
    <div className="card">
      <div className="list-head">
        <h2>估算</h2>
        <label className="inline small">
          情境數
          <input type="number" min={1} max={20} value={scenarios} onChange={(e) => setScenarios(Math.max(1, Number(e.target.value)))} />
        </label>
      </div>
      <table className="ep-table">
        <thead>
          <tr>
            <th>階段</th>
            <th>分支</th>
            <th>本階段新增回合</th>
            <th>LLM 呼叫（角色 + 模擬使用者，上限）</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.i}>
              <td>
                {r.i + 1} {r.note && <span className="muted small">（{r.note}）</span>}
              </td>
              <td>{r.branches}</td>
              <td>{r.turns}</td>
              <td>{r.calls}</td>
            </tr>
          ))}
          <tr className="total">
            <td>合計</td>
            <td />
            <td />
            <td>{total}</td>
          </tr>
        </tbody>
      </table>
      <p className="muted small">第一回合使用情境的發言時，模擬使用者的呼叫會少一些。費用依模型而定，可參考試跑紀錄的單次花費。</p>
    </div>
  )
}
