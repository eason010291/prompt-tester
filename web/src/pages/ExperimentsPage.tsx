import { useMemo, useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router'
import { api } from '../api'
import ExperimentView from '../components/ExperimentView'
import { scoreLevel } from '../lib/runs'
import RunCompare from '../components/RunCompare'
import { formatCost, useResource } from '../hooks/useResource'
import {
  PROMOTE_LABEL,
  formatScore,
  shortId,
  type Connection,
  type ExperimentDetail,
  type ExperimentSummary,
  type RunSummary,
  type Scenario,
  type Script,
  type Workspace,
} from '../types'

const STATUS_LABEL = { running: '執行中', waiting: '等待晉級', done: '完成', cancelled: '已中止' }

/**
 * 實驗頁：
 * - ?a=&b=：兩次試跑並排比較（可帶 exp= 返回實驗）
 * - ?exp=：金字塔實驗（各階段分支、晉級、評審預選）
 * - 其他：新實驗、實驗列表、單次試跑比較
 */
export default function ExperimentsPage() {
  const [params] = useSearchParams()
  const a = Number(params.get('a')) || null
  const b = Number(params.get('b')) || null
  const exp = Number(params.get('exp')) || null

  if (a && b) {
    return (
      <div className="experiments-page">
        <header className="page-header row">
          <h1>比較</h1>
          <Link to={exp ? `/experiments?exp=${exp}` : '/experiments'} className="btn btn-sm">
            ← {exp ? `回到實驗 #${exp}` : '回到實驗列表'}
          </Link>
        </header>
        <RunCompare a={a} b={b} onFeedback={() => {}} />
      </div>
    )
  }
  if (exp) {
    return (
      <div className="experiments-page">
        <header className="page-header row">
          <h1>實驗 #{exp}</h1>
          <Link to="/experiments" className="btn btn-sm">
            ← 實驗列表
          </Link>
        </header>
        <ExperimentView key={exp} id={exp} />
      </div>
    )
  }
  return <Overview />
}

function Overview() {
  const navigate = useNavigate()
  const list = useResource<ExperimentSummary[]>('/experiments')

  return (
    <div className="experiments-page">
      <header className="page-header row">
        <h1>實驗</h1>
        <span className="muted small">金字塔批次：單回合多分支廣篩 → 晉級 → 多回合少分支。每個階段結束後由你確認晉級。</span>
      </header>

      <NewExperiment onCreated={(id) => navigate(`/experiments?exp=${id}`)} />

      <section className="card">
        <h2>實驗列表</h2>
        {list.data?.length === 0 && <p className="muted">還沒有實驗。</p>}
        {(list.data?.length ?? 0) > 0 && (
          <table className="ep-table exp-table">
            <thead>
              <tr>
                <th>#</th>
                <th>名稱</th>
                <th>狀態</th>
                <th>階段</th>
                <th>預設</th>
                <th>腳本</th>
                <th>分支</th>
                <th>平均分</th>
                <th>花費</th>
                <th>時間</th>
              </tr>
            </thead>
            <tbody>
              {list.data?.map((e) => (
                <tr key={e.id} className="clickable" onClick={() => navigate(`/experiments?exp=${e.id}`)}>
                  <td>#{e.id}</td>
                  <td>
                    <Link to={`/experiments?exp=${e.id}`} onClick={(ev) => ev.stopPropagation()}>
                      {e.name}
                    </Link>
                    {e.rerun_of && <span className="muted small">（重跑 #{e.rerun_of}）</span>}
                  </td>
                  <td>
                    <span className={`chip exp-status exp-${e.status}`}>{STATUS_LABEL[e.status]}</span>
                  </td>
                  <td>
                    {e.current_stage + 1}/{e.stage_count}
                  </td>
                  <td>
                    {e.preset_name}
                    {e.commit_id && <code className="muted"> @{shortId(e.commit_id)}</code>}
                  </td>
                  <td>{e.script_name}</td>
                  <td>{e.runs}</td>
                  <td>
                    <span className="chip score-chip" data-level={scoreLevel(e.score)}>
                      {formatScore(e.score)}
                    </span>
                  </td>
                  <td>{formatCost(e.cost)}</td>
                  <td className="muted">{new Date(e.created_at).toLocaleString('zh-TW', { dateStyle: 'short', timeStyle: 'short' })}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

      <RunPicker />
    </div>
  )
}

function NewExperiment({ onCreated }: { onCreated: (id: number) => void }) {
  const ws = useResource<Workspace>('/workspace')
  const scripts = useResource<Script[]>('/scripts')
  const scenarios = useResource<Scenario[]>('/scenarios')
  const connections = useResource<Connection[]>('/connections')
  const [scriptId, setScriptId] = useState<number | null>(null)
  const [picked, setPicked] = useState<Set<number>>(new Set())
  const [connId, setConnId] = useState<number | null>(null)
  const [name, setName] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const script = scripts.data?.find((s) => s.id === scriptId) ?? scripts.data?.[0] ?? null
  const conn = connections.data?.find((c) => c.id === connId) ?? connections.data?.[0] ?? null
  const first = script?.resolved.stages[0]
  const firstBranches = first ? picked.size * first.seeds * Math.max(1, first.temperatures.length) : 0

  const start = async () => {
    if (!ws.data || !script || !conn?.id) return
    setBusy(true)
    setError(null)
    try {
      const created = await api<ExperimentDetail>('/experiments', {
        method: 'POST',
        body: JSON.stringify({ name, preset: ws.data.preset, script_id: script.id, scenario_ids: [...picked], connection_id: conn.id }),
      })
      onCreated(created.id)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <details className="card new-exp" open>
      <summary>
        <h2>新實驗</h2>
        <span className="muted small">
          預設：工作區的 {ws.data?.preset.name ?? '…'}
          {ws.data?.base_commit && ` @${shortId(ws.data.base_commit)}`}（提交後再跑，版本樹才會連到實驗）
        </span>
      </summary>
      <div className="form-grid">
        <label>
          名稱（選填）
          <input value={name} placeholder="預設：預設名稱 × 腳本名稱" onChange={(e) => setName(e.target.value)} />
        </label>
        <label>
          腳本
          <select value={script?.id ?? ''} onChange={(e) => setScriptId(Number(e.target.value))}>
            {scripts.data?.map((s) => (
              <option key={s.id} value={s.id}>
                {s.name}
              </option>
            ))}
          </select>
        </label>
        <label>
          角色連線
          <select value={conn?.id ?? ''} onChange={(e) => setConnId(Number(e.target.value))}>
            {connections.data?.map((c) => (
              <option key={c.id} value={c.id!}>
                {c.name}
              </option>
            ))}
          </select>
        </label>
      </div>
      {script && (
        <p className="muted small">
          {script.resolved.stages
            .map((st, i) => `${i + 1}. ${st.name || '階段'}：累計 ${st.turns} 回 × ${st.seeds} 條${st.temperatures.length > 1 ? ` × ${st.temperatures.length} 個溫度` : ''}${i < script.resolved.stages.length - 1 ? ` → ${PROMOTE_LABEL[st.promote.mode]}` : ''}`)
            .join('　')}{' '}
          <Link to={`/scripts?id=${script.id}`}>編輯腳本</Link>
        </p>
      )}
      <fieldset className="scenario-picks">
        <legend>情境</legend>
        {scenarios.data?.map((s) => (
          <label key={s.id} className="check">
            <input
              type="checkbox"
              checked={picked.has(s.id!)}
              onChange={() => {
                const n = new Set(picked)
                if (n.has(s.id!)) n.delete(s.id!)
                else n.add(s.id!)
                setPicked(n)
              }}
            />
            {s.name}
          </label>
        ))}
      </fieldset>
      <div className="add-bar">
        <button className="btn btn-primary" disabled={busy || !picked.size || !script || !conn || !ws.data} onClick={start}>
          ▶ 開始第 1 階段（{firstBranches} 條分支）
        </button>
        {!connections.data?.length && <Link to="/settings">先到設定頁建立連線</Link>}
        {error && <span className="alert alert-error">{error}</span>}
      </div>
    </details>
  )
}

/** 任選兩次試跑（單次試跑或實驗分支）並排比較 */
function RunPicker() {
  const [params, setParams] = useSearchParams()
  const a = Number(params.get('a')) || null
  const b = Number(params.get('b')) || null
  const runs = useResource<RunSummary[]>('/runs?limit=300')
  const [scenarioFilter, setScenarioFilter] = useState<number | 'all'>('all')

  const pick = (side: 'a' | 'b', id: number) => {
    const next = new URLSearchParams(params)
    const other = side === 'a' ? 'b' : 'a'
    if (Number(next.get(other)) === id) next.delete(other)
    if (Number(next.get(side)) === id) next.delete(side)
    else next.set(side, String(id))
    setParams(next, { replace: true })
  }

  const scenarios = useMemo(() => {
    const m = new Map<number, string>()
    for (const r of runs.data ?? []) if (r.scenario_id != null) m.set(r.scenario_id, r.scenario_name ?? `#${r.scenario_id}`)
    return [...m]
  }, [runs.data])
  // 選了 A 之後預設只看同一個情境的試跑
  const anchor = runs.data?.find((r) => r.id === a)
  const effectiveFilter = scenarioFilter === 'all' && anchor && !b ? (anchor.scenario_id ?? 'all') : scenarioFilter
  const shown = (runs.data ?? []).filter((r) => effectiveFilter === 'all' || r.scenario_id === effectiveFilter)

  return (
    <details className="card run-picker">
      <summary>
        <h2>試跑比較</h2>
        <span className="muted small">
          任選兩次試跑並排比較 · A：{a ? `#${a}` : '—'} · B：{b ? `#${b}` : '—'}
        </span>
      </summary>
      <div className="add-bar">
        <label className="inline">
          情境
          <select value={effectiveFilter} onChange={(e) => setScenarioFilter(e.target.value === 'all' ? 'all' : Number(e.target.value))}>
            <option value="all">全部</option>
            {scenarios.map(([id, name]) => (
              <option key={id} value={id}>
                {name}
              </option>
            ))}
          </select>
        </label>
      </div>
      {runs.error && <p className="alert alert-error">{runs.error}</p>}
      {runs.data && runs.data.length === 0 && <p className="muted">還沒有任何試跑。</p>}
      {shown.length > 0 && (
        <table className="ep-table run-table">
          <thead>
            <tr>
              <th>比較</th>
              <th>#</th>
              <th>情境</th>
              <th>預設</th>
              <th>來源</th>
              <th>回合</th>
              <th>連線</th>
              <th>分數</th>
              <th>花費</th>
              <th>時間</th>
            </tr>
          </thead>
          <tbody>
            {shown.map((r) => (
              <tr key={r.id} className={r.id === a || r.id === b ? 'picked' : r.status !== 'done' ? 'dim' : ''}>
                <td className="pick-cell">
                  {(['a', 'b'] as const).map((side) => (
                    <button
                      key={side}
                      className="pin"
                      aria-pressed={(side === 'a' ? a : b) === r.id}
                      onClick={() => pick(side, r.id)}
                      aria-label={`設為 ${side.toUpperCase()}`}
                    >
                      {side.toUpperCase()}
                    </button>
                  ))}
                </td>
                <td>#{r.id}</td>
                <td>{r.scenario_name ?? '—'}</td>
                <td>
                  {r.preset_name}
                  {r.commit_id && <code className="muted"> @{shortId(r.commit_id)}</code>}
                </td>
                <td>
                  {r.experiment_id ? (
                    <Link to={`/experiments?exp=${r.experiment_id}`}>
                      實驗 #{r.experiment_id} 階段 {(r.stage ?? 0) + 1}
                    </Link>
                  ) : r.mode === 'live' ? (
                    '試跑'
                  ) : (
                    `回放 #${r.replay_of}`
                  )}
                </td>
                <td>{r.last_turn ?? 0}</td>
                <td>{r.connection_name}</td>
                <td>
                  <span className="chip score-chip" data-level={scoreLevel(r.score)}>
                    {formatScore(r.score)}
                  </span>
                </td>
                <td>{formatCost(r.cost)}</td>
                <td className="muted">{new Date(r.created_at).toLocaleString('zh-TW', { dateStyle: 'short', timeStyle: 'short' })}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </details>
  )
}
