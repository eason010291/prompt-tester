import { lazy, Suspense, useMemo, useState } from 'react'
import DiffView from './DiffView'
import FeedbackBox, { scoreLevel } from './FeedbackBox'
import { formatCost, useResource } from '../hooks/useResource'
import { collectScriptSets } from '../lib/st/scripts'
import { makeMacro } from '../lib/st/regex'
import { formatScore, shortId, type ComparedRun, type Preset, type Run, type RunCompare, type RunMessage } from '../types'

const StMessage = lazy(() => import('./StMessage'))

/** 兩次試跑並排：分數差、逐回合（全域回合編號）對齊、各自的評語，可盲評。 */
export default function RunCompare({ a, b, onFeedback }: { a: number; b: number; onFeedback: () => void }) {
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

/** 依回合對齊兩邊的訊息；depth 是 ST 正則用的「距離最新一則」。接續的分支回合從中間開始。 */
function alignTurns(runs: ComparedRun[]): { turn: number; cells: Cell[] }[] {
  const all = runs.flatMap((r) => r.messages.map((m) => m.turn))
  if (!all.length) return []
  const [lo, hi] = [Math.min(...all), Math.max(...all)]
  return Array.from({ length: hi - lo + 1 }, (_, i) => lo + i).map((turn) => ({
    turn,
    cells: runs.map((r) => ({ messages: withDepth(r).filter((x) => x.m.turn === turn) })),
  }))
}

export function withDepth(r: Run): { m: RunMessage; depth: number }[] {
  const total = r.scenario.history.length + r.messages.length
  return r.messages.map((m, idx) => ({ m, depth: total - (r.scenario.history.length + idx) - 1 }))
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
  if (!cell.messages.length) return <div className="compare-cell muted small">（沒有這個回合）</div>
  return (
    <div className="compare-cell">
      <MessageList run={run} preset={run.preset} messages={cell.messages} feedback={run.feedback} onFeedback={onFeedback} />
    </div>
  )
}

/** 一串試跑訊息：使用者泡泡 + ST 渲染的角色回覆 + 逐則評語 */
export function MessageList({
  run,
  preset,
  messages,
  feedback,
  onFeedback,
}: {
  run: Run
  preset: Preset | null
  messages: { m: RunMessage; depth: number }[]
  feedback: ComparedRun['feedback'] | null
  onFeedback: () => void
}) {
  const scenario = run.scenario
  const scriptSets = useMemo(
    () => collectScriptSets((preset?.extra?.extensions as { regex_scripts?: unknown } | undefined)?.regex_scripts, scenario.card?.regex_scripts, { preset: true, card: true }),
    [preset, scenario.card],
  )
  const macro = useMemo(() => makeMacro({ ...scenario.vars, char: scenario.char, user: scenario.user }), [scenario])
  return (
    <>
      {messages.map(({ m, depth }) =>
        m.role === 'user' ? (
          <div key={m.id} className="bubble bubble-user">
            <span className="bubble-role">
              {scenario.user} · 第 {m.turn} 回合
            </span>
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
            {feedback && (
              <FeedbackBox
                targetType="run_message"
                targetId={m.id}
                items={feedback.filter((f) => f.target_type === 'run_message' && f.target_id === m.id)}
                onChanged={onFeedback}
                compact
              />
            )}
          </div>
        ),
      )}
    </>
  )
}
