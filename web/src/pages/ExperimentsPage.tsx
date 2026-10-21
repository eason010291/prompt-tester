import { lazy, Suspense, useMemo, useState } from 'react'
import { useSearchParams } from 'react-router'
import DiffView from '../components/DiffView'
import FeedbackBox, { scoreLevel } from '../components/FeedbackBox'
import { formatCost, useResource } from '../hooks/useResource'
import { collectScriptSets } from '../lib/st/scripts'
import { makeMacro } from '../lib/st/regex'
import { formatScore, shortId, type ComparedRun, type RunCompare, type RunMessage, type RunSummary } from '../types'

const StMessage = lazy(() => import('../components/StMessage'))

/** 試跑列表 + 兩次試跑並排比較（評語、分數、預設 diff），可盲評。 */
export default function ExperimentsPage() {
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
    <div className="experiments-page">
      <header className="page-header row">
        <h1>實驗</h1>
        <span className="muted small">挑兩次試跑並排比較：逐回合回覆、評語分數與預設差異。批次與金字塔晉級在 M4 後續加入。</span>
      </header>

      <details className="card run-picker" open={!(a && b)}>
        <summary>
          <h2>試跑紀錄</h2>
          <span className="muted small">
            A：{a ? `#${a}` : '—'} · B：{b ? `#${b}` : '—'}
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
        {runs.data && runs.data.length === 0 && <p className="muted">還沒有任何試跑。到「情境」頁的「多回合試跑」跑幾次再回來比較。</p>}
        {shown.length > 0 && (
          <table className="ep-table run-table">
            <thead>
              <tr>
                <th>比較</th>
                <th>#</th>
                <th>情境</th>
                <th>預設</th>
                <th>模式</th>
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
                  <td>{r.mode === 'live' ? '即時' : `回放 #${r.replay_of}`}</td>
                  <td>
                    {r.replies}/{r.turns}
                  </td>
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

      {a && b ? <Compare a={a} b={b} onFeedback={runs.reload} /> : <p className="muted">在上方各選一個 A 與 B。</p>}
    </div>
  )
}

function Compare({ a, b, onFeedback }: { a: number; b: number; onFeedback: () => void }) {
  const cmp = useResource<RunCompare>(`/compare/runs?a=${a}&b=${b}`)
  const [blind, setBlind] = useState(false)
  const [swap, setSwap] = useState(false)
  const [revealed, setRevealed] = useState<string | null>(null)

  if (cmp.error) return <p className="alert alert-error">{cmp.error}</p>
  if (!cmp.data) return <p className="muted">載入中…</p>
  const data = cmp.data

  const toggleBlind = () => {
    if (blind) {
      // 揭曉：記下盲評時的 X / Y 各是哪一次
      const [x, y] = swap ? [data.b, data.a] : [data.a, data.b]
      setRevealed(`X = #${x.id}（${x.preset_name}），Y = #${y.id}（${y.preset_name}）`)
      setBlind(false)
    } else {
      setSwap(Math.random() < 0.5)
      setRevealed(null)
      setBlind(true)
    }
  }

  const sides: { key: string; label: string; run: ComparedRun }[] = blind
    ? (swap ? [data.b, data.a] : [data.a, data.b]).map((run, i) => ({ key: String(run.id), label: i ? 'Y' : 'X', run }))
    : [
        { key: String(data.a.id), label: 'A', run: data.a },
        { key: String(data.b.id), label: 'B', run: data.b },
      ]
  const reload = () => {
    cmp.reload()
    onFeedback()
  }

  return (
    <div className="compare">
      <div className="add-bar">
        <button className="btn btn-sm" aria-pressed={blind} onClick={toggleBlind}>
          {blind ? '揭曉' : '盲評模式'}
        </button>
        {blind ? (
          <span className="muted small">已隨機左右對調並隱藏預設、模型與花費；評完再按「揭曉」。</span>
        ) : (
          <span className="muted small">
            {data.same_preset ? '兩次使用完全相同的預設（差異來自 seed、模型或使用者發言）。' : '兩次的預設不同，差異見下方。'}
          </span>
        )}
      </div>
      {revealed && (
        <p className="alert" role="status">
          {revealed}
        </p>
      )}

      <ScoreTable sides={sides} />

      <div className="compare-grid">
        {sides.map((s) => (
          <RunHead key={s.key} label={s.label} run={s.run} blind={blind} />
        ))}
        {alignTurns(sides.map((s) => s.run)).map((row) => (
          <TurnRow key={row.turn} turn={row.turn} cells={row.cells} sides={sides} onFeedback={reload} />
        ))}
        {sides.map((s) => (
          <div key={s.key} className="compare-cell">
            <FeedbackBox
              targetType="run"
              targetId={s.run.id}
              items={s.run.feedback.filter((f) => f.target_type === 'run')}
              onChanged={reload}
              label={`${s.label} 整體評語`}
            />
          </div>
        ))}
      </div>

      {!blind && data.diff && !data.same_preset && (
        <details className="card" open>
          <summary>
            <h2>預設差異（A → B）</h2>
          </summary>
          <DiffView diff={data.diff} labels={[`A #${data.a.id}`, `B #${data.b.id}`]} />
        </details>
      )}
    </div>
  )
}

function RunHead({ label, run, blind }: { label: string; run: ComparedRun; blind: boolean }) {
  return (
    <div className="compare-cell compare-head">
      <strong className="side-label">{label}</strong>
      {blind ? (
        <span className="muted small">{run.scenario.name}</span>
      ) : (
        <span className="muted small">
          #{run.id} · {run.preset_name}
          {run.commit_id && ` @${shortId(run.commit_id)}`} · {run.connection?.model} · seed {run.seed} · {run.mode === 'live' ? '即時' : `回放 #${run.replay_of}`} ·{' '}
          {formatCost(run.cost)}
        </span>
      )}
    </div>
  )
}

function ScoreTable({ sides }: { sides: { label: string; run: ComparedRun }[] }) {
  const names = [...new Set(sides.flatMap((s) => Object.keys(s.run.scores.attributes)))]
  if (!names.length) return <p className="muted small">還沒有分數。在下方評語寫「屬性：7/10」這樣的行就會統計。</p>
  const [x, y] = sides
  const row = (name: string, vx: number | null | undefined, vy: number | null | undefined, strong = false) => {
    const delta = vx != null && vy != null ? vy - vx : null
    return (
      <tr key={name} className={strong ? 'total' : ''}>
        <td>{name}</td>
        <td>
          <span className="chip score-chip" data-level={scoreLevel(vx)}>
            {formatScore(vx)}
          </span>
        </td>
        <td>
          <span className="chip score-chip" data-level={scoreLevel(vy)}>
            {formatScore(vy)}
          </span>
        </td>
        <td className={delta == null || delta === 0 ? 'muted' : delta > 0 ? 'delta-up' : 'delta-down'}>
          {delta == null ? '—' : `${delta > 0 ? '+' : ''}${formatScore(Math.round(delta * 100) / 100)}`}
        </td>
      </tr>
    )
  }
  return (
    <table className="ep-table score-table">
      <thead>
        <tr>
          <th>屬性</th>
          <th>{x.label}</th>
          <th>{y.label}</th>
          <th>
            {y.label} − {x.label}
          </th>
        </tr>
      </thead>
      <tbody>
        {names.map((n) => row(n, x.run.scores.attributes[n], y.run.scores.attributes[n]))}
        {row('整體平均', x.run.scores.overall, y.run.scores.overall, true)}
      </tbody>
    </table>
  )
}

interface Cell {
  messages: { m: RunMessage; depth: number }[]
}

/** 依回合對齊兩邊的訊息；depth 是 ST 正則用的「距離最新一則」 */
function alignTurns(runs: ComparedRun[]): { turn: number; cells: Cell[] }[] {
  const maxTurn = Math.max(0, ...runs.flatMap((r) => r.messages.map((m) => m.turn)))
  return Array.from({ length: maxTurn }, (_, i) => ({
    turn: i + 1,
    cells: runs.map((r) => {
      const total = r.scenario.history.length + r.messages.length
      return {
        messages: r.messages
          .map((m, idx) => ({ m, depth: total - (r.scenario.history.length + idx) - 1 }))
          .filter((x) => x.m.turn === i + 1),
      }
    }),
  }))
}

function TurnRow({
  turn,
  cells,
  sides,
  onFeedback,
}: {
  turn: number
  cells: Cell[]
  sides: { key: string; run: ComparedRun }[]
  onFeedback: () => void
}) {
  return (
    <>
      <div className="turn-label">第 {turn} 回合</div>
      {cells.map((cell, i) => (
        <SideTurn key={sides[i].key} cell={cell} run={sides[i].run} onFeedback={onFeedback} />
      ))}
    </>
  )
}

function SideTurn({ cell, run, onFeedback }: { cell: Cell; run: ComparedRun; onFeedback: () => void }) {
  const scenario = run.scenario
  const scriptSets = useMemo(
    () => collectScriptSets((run.preset?.extra?.extensions as { regex_scripts?: unknown } | undefined)?.regex_scripts, scenario.card?.regex_scripts, { preset: true, card: true }),
    [run.preset, scenario.card],
  )
  const macro = useMemo(() => makeMacro({ ...scenario.vars, char: scenario.char, user: scenario.user }), [scenario])
  if (!cell.messages.length) return <div className="compare-cell muted small">（沒有這個回合）</div>
  return (
    <div className="compare-cell">
      {cell.messages.map(({ m, depth }) =>
        m.role === 'user' ? (
          <div key={m.id} className="bubble bubble-user">
            <span className="bubble-role">{scenario.user}</span>
            <div className="bubble-text">{m.content}</div>
          </div>
        ) : (
          <div key={m.id} className="bubble bubble-assistant">
            <span className="bubble-role">
              {scenario.char}
              {m.finish_reason === 'length' && <em> · 被截斷</em>}
            </span>
            <Suspense fallback={<pre className="msg-content">{m.content}</pre>}>
              <StMessage text={m.content} theme="st-dark" depth={depth} charName={scenario.char} scriptSets={scriptSets} macro={macro} />
            </Suspense>
            {m.error && <p className="alert alert-error">{m.error}</p>}
            <FeedbackBox
              targetType="run_message"
              targetId={m.id}
              items={run.feedback.filter((f) => f.target_type === 'run_message' && f.target_id === m.id)}
              onChanged={onFeedback}
              compact
            />
          </div>
        ),
      )}
    </div>
  )
}
