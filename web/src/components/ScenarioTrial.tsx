import { lazy, Suspense, useMemo, useRef, useState } from 'react'
import { Link } from 'react-router'
import { api, streamApi } from '../api'
import { formatCost, useResource } from '../hooks/useResource'
import { collectScriptSets } from '../lib/st/scripts'
import { makeMacro } from '../lib/st/regex'
import FeedbackBox from './FeedbackBox'
import {
  formatScore,
  shortId,
  type Connection,
  type Feedback,
  type HistoryMessage,
  type Run,
  type RunEvent,
  type RunMessage,
  type RunSource,
  type RunSummary,
  type Scenario,
  type UserSimSettings,
  type Workspace,
} from '../types'

const StMessage = lazy(() => import('./StMessage'))

const SOURCE_LABEL: Record<RunSource, string> = {
  scenario: '情境',
  llm_user: 'LLM 扮演',
  replay: '回放',
  replay_fallback: 'LLM 補位',
  char: '角色',
}

interface Props {
  scenario: Scenario
  /** 情境有未儲存的變更時不能試跑（試跑使用伺服器上的版本） */
  saved: boolean
  /** 剛匯入角色卡，提示先預檢 */
  suggestPreflight?: boolean
}

interface Live {
  runId: number | null
  history: HistoryMessage[]
  messages: RunMessage[]
  streaming: { role: 'user' | 'assistant'; turn: number; text: string } | null
  status: 'idle' | 'running' | 'done' | 'error' | 'cancelled'
  run: Run | null
  error: string | null
}

const IDLE: Live = { runId: null, history: [], messages: [], streaming: null, status: 'idle', run: null, error: null }

export default function ScenarioTrial({ scenario, saved, suggestPreflight }: Props) {
  const ws = useResource<Workspace>('/workspace')
  const connections = useResource<Connection[]>('/connections')
  const userSim = useResource<UserSimSettings>('/user-sim')
  const runs = useResource<RunSummary[]>(`/runs?scenario_id=${scenario.id}&standalone=true`)
  const [connId, setConnId] = useState<number | null>(null)
  const [turns, setTurns] = useState(3)
  const [mode, setMode] = useState<'live' | 'replay'>('live')
  const [replayOf, setReplayOf] = useState<number | null>(null)
  const [seed, setSeed] = useState(1)
  const [firstFromScenario, setFirstFromScenario] = useState(true)
  const [live, setLive] = useState<Live>(IDLE)
  const abort = useRef<AbortController | null>(null)
  // 試跑結束（或打開舊紀錄）後才能寫評語
  const feedback = useResource<Feedback[]>(live.run ? `/feedback?run_id=${live.run.id}` : null)
  const onFeedback = () => {
    feedback.reload()
    runs.reload()
  }

  const preset = ws.data?.preset
  const conn = connections.data?.find((c) => c.id === connId) ?? connections.data?.[0] ?? null
  const userConn = connections.data?.find((c) => c.id === userSim.data?.connection_id) ?? null
  const scriptSets = useMemo(
    () => collectScriptSets((preset?.extra?.extensions as { regex_scripts?: unknown } | undefined)?.regex_scripts, scenario.card?.regex_scripts, { preset: true, card: true }),
    [preset, scenario.card],
  )
  const macro = useMemo(() => makeMacro({ ...scenario.vars, char: scenario.char, user: scenario.user }), [scenario])
  const running = live.status === 'running'
  const hasReplayable = (runs.data ?? []).some((r) => r.replies > 0)

  const start = async (opts: { turns: number; mode: 'live' | 'replay' }) => {
    if (!preset || !conn?.id) return
    abort.current?.abort()
    const ctrl = new AbortController()
    abort.current = ctrl
    setLive({ ...IDLE, status: 'running' })
    try {
      await streamApi<RunEvent>(
        '/runs',
        {
          preset,
          scenario_id: scenario.id,
          connection_id: conn.id,
          turns: opts.turns,
          seed,
          mode: opts.mode,
          replay_of: opts.mode === 'replay' ? replayOf : null,
          first_from_scenario: firstFromScenario,
        },
        (ev) =>
          setLive((l) => {
            switch (ev.type) {
              case 'start':
                return { ...l, runId: ev.run_id, history: ev.history }
              case 'message_start':
                return { ...l, streaming: { role: ev.role, turn: ev.turn, text: '' } }
              case 'content':
                return l.streaming ? { ...l, streaming: { ...l.streaming, text: l.streaming.text + ev.text } } : l
              case 'message_done':
                return { ...l, streaming: null, messages: [...l.messages, ev.message] }
              case 'done':
                return { ...l, status: ev.run.status === 'running' || ev.run.status === 'queued' ? 'done' : ev.run.status, run: ev.run, streaming: null }
              default:
                return l
            }
          }),
        ctrl.signal,
      )
    } catch (e) {
      const aborted = e instanceof DOMException && e.name === 'AbortError'
      setLive((l) => ({ ...l, status: aborted ? 'cancelled' : 'error', error: aborted ? null : String(e instanceof Error ? e.message : e), streaming: null }))
    } finally {
      runs.reload()
    }
  }

  const open = async (id: number) => {
    const run = await api<Run>(`/runs/${id}`)
    setLive({ ...IDLE, runId: id, history: run.scenario.history, messages: run.messages, status: run.status === 'running' || run.status === 'queued' ? 'cancelled' : run.status, run, error: run.error })
  }

  const remove = async (id: number) => {
    await api(`/runs/${id}`, { method: 'DELETE' })
    if (live.runId === id) setLive(IDLE)
    runs.reload()
  }

  const baseBlocker = !saved
    ? '情境有未儲存的變更，請稍候自動儲存完成'
    : !preset
      ? '讀取工作區預設中…'
      : !conn
        ? '還沒有連線'
        : null
  // 預檢一律即時扮演，需要模擬使用者連線
  const liveBlocker = baseBlocker ?? (!userConn ? '尚未設定模擬使用者的連線' : null)
  const blocker = mode === 'live' ? liveBlocker : (baseBlocker ?? (!replayOf ? '請選擇要回放的試跑' : null))
  // ST 的正則深度：0 = 最新一則（含開場與既有歷史一起計算）
  const total = live.history.length + live.messages.length

  const checks = live.run?.checks
  const issues = checks ? [...checks.build_warnings.map((w) => ({ turn: 0, role: 'build', message: w })), ...checks.issues] : []

  return (
    <div className="card trial">
      <div className="list-head">
        <h2>多回合試跑</h2>
        <span className="muted small">
          預設：{preset?.name ?? '…'}
          {ws.data?.base_commit && ` @${shortId(ws.data.base_commit)}`} · 模擬使用者：
          {userConn ? userConn.name : <Link to="/settings">未設定</Link>}
        </span>
      </div>

      {suggestPreflight && live.status === 'idle' && (
        <p className="alert">剛匯入角色卡。建議先執行「預檢」，用 2 回合確認組裝、世界書預算與模型回應是否正常（約幾分錢）。</p>
      )}

      <div className="trial-opts">
        <label>
          角色連線
          <select value={conn?.id ?? ''} onChange={(e) => setConnId(Number(e.target.value))} disabled={running}>
            {connections.data?.map((c) => (
              <option key={c.id} value={c.id!}>
                {c.name}
              </option>
            ))}
          </select>
        </label>
        <label>
          回合數
          <input type="number" min={1} max={12} value={turns} onChange={(e) => setTurns(Math.min(12, Math.max(1, Number(e.target.value))))} disabled={running} />
        </label>
        <label>
          seed
          <input type="number" value={seed} onChange={(e) => setSeed(Number(e.target.value))} disabled={running} />
        </label>
        <div className="segmented" role="group" aria-label="使用者發言">
          <button aria-pressed={mode === 'live'} onClick={() => setMode('live')} disabled={running}>
            即時扮演
          </button>
          <button aria-pressed={mode === 'replay'} onClick={() => setMode('replay')} disabled={running || !hasReplayable} title={hasReplayable ? '' : '還沒有可回放的試跑'}>
            回放
          </button>
        </div>
        {mode === 'replay' && (
          <select value={replayOf ?? ''} onChange={(e) => setReplayOf(Number(e.target.value) || null)} disabled={running} aria-label="回放哪一次">
            <option value="">選擇要沿用使用者發言的試跑</option>
            {runs.data
              ?.filter((r) => r.replies > 0)
              .map((r) => (
                <option key={r.id} value={r.id}>
                  #{r.id} · {r.preset_name} · {r.replies} 回合
                </option>
              ))}
          </select>
        )}
        <label className="check">
          <input type="checkbox" checked={firstFromScenario} onChange={(e) => setFirstFromScenario(e.target.checked)} disabled={running} />
          第 1 回合用情境的第一句
        </label>
      </div>

      <div className="add-bar">
        {running ? (
          <button className="btn btn-primary" onClick={() => abort.current?.abort()}>
            ■ 停止
          </button>
        ) : (
          <>
            <button className="btn btn-primary" disabled={!!blocker} onClick={() => start({ turns, mode })}>
              ▶ 試跑 {turns} 回合
            </button>
            <button
              className="btn"
              disabled={!!liveBlocker}
              onClick={() => start({ turns: 2, mode: 'live' })}
              title="以即時扮演跑 2 回合，檢查組裝警告、出戲 / 拒答、截斷與錯誤"
            >
              預檢（2 回合）
            </button>
          </>
        )}
        {blocker && !running && <span className="muted small">{blocker}</span>}
      </div>

      {live.status !== 'idle' && (
        <div className="transcript">
          {live.history.length > 0 && (
            <details className="history-fold">
              <summary>開場與既有歷史（{live.history.length} 則）</summary>
              {live.history.map((h, i) => (
                <pre key={i} className="msg-content">
                  {h.role}：{h.content.slice(0, 600)}
                </pre>
              ))}
            </details>
          )}
          {live.messages.map((m, i) => (
            <Bubble
              key={m.id}
              m={m}
              depth={total - (live.history.length + i) - 1}
              scenario={scenario}
              scriptSets={scriptSets}
              macro={macro}
              feedback={live.run ? (feedback.data ?? []).filter((f) => f.target_type === 'run_message' && f.target_id === m.id) : null}
              onFeedback={onFeedback}
            />
          ))}
          {live.streaming && (
            <div className={`bubble bubble-${live.streaming.role} is-streaming`}>
              <span className="bubble-role">
                第 {live.streaming.turn} 回合 · {live.streaming.role === 'user' ? `${scenario.user}（生成中）` : `${scenario.char}（生成中）`}
              </span>
              <pre className="msg-content">{live.streaming.text || '…'}</pre>
            </div>
          )}
          {live.error && <p className="alert alert-error">{live.error}</p>}
          {live.run && (
            <div className={`preflight ${issues.length ? 'has-issues' : 'ok'}`}>
              <strong>
                {live.run.status === 'done' ? '試跑完成' : live.run.status === 'cancelled' ? '已中止' : '試跑失敗'} · #{live.run.id} · 花費{' '}
                {formatCost(live.run.cost)}
              </strong>
              {issues.length === 0 ? (
                <p className="muted small">預檢：沒有發現問題（組裝、出戲 / 拒答、截斷、錯誤）。</p>
              ) : (
                <ul>
                  {issues.map((it, i) => (
                    <li key={i}>
                      {it.turn ? `第 ${it.turn} 回合 ${it.role === 'user' ? '使用者' : '角色'}：` : '組裝：'}
                      {it.message}
                    </li>
                  ))}
                </ul>
              )}
            </div>
          )}
          {live.run && feedback.data && (
            <FeedbackBox
              targetType="run"
              targetId={live.run.id}
              items={feedback.data.filter((f) => f.target_type === 'run')}
              onChanged={onFeedback}
              label="整體評語"
            />
          )}
          {live.run && (
            <p className="muted small">
              <Link to={`/experiments?a=${live.run.id}`}>到「實驗」頁與其他試跑並排比較 →</Link>
            </p>
          )}
        </div>
      )}

      {(runs.data?.length ?? 0) > 0 && (
        <details className="gen-history" open={live.status === 'idle'}>
          <summary>這個情境的試跑紀錄</summary>
          <ul>
            {runs.data?.map((r) => (
              <li key={r.id} className={live.runId === r.id ? 'active' : ''}>
                <button className="gen-item" onClick={() => open(r.id)}>
                  <span className={`status-dot status-${r.status}`} aria-label={r.status} />
                  <span className="gen-meta">
                    #{r.id} · {r.mode === 'live' ? '即時' : `回放 #${r.replay_of}`} · {r.replies}/{r.turns} 回合 · {r.preset_name}
                    {r.commit_id && ` @${shortId(r.commit_id)}`} · {formatCost(r.cost)}
                    {r.score != null && <b className="run-score"> · ★ {formatScore(r.score)}</b>}
                  </span>
                  <span className="gen-excerpt">
                    {r.error ??
                      ((r.checks?.issues.length ?? 0) + (r.checks?.build_warnings.length ?? 0) > 0
                        ? `${(r.checks?.issues.length ?? 0) + (r.checks?.build_warnings.length ?? 0)} 個提醒`
                        : '沒有提醒')}
                  </span>
                </button>
                <button className="icon-btn" aria-label={`刪除 #${r.id}`} onClick={() => remove(r.id)}>
                  ✕
                </button>
              </li>
            ))}
          </ul>
        </details>
      )}
    </div>
  )
}

function Bubble({
  m,
  depth,
  scenario,
  scriptSets,
  macro,
  feedback,
  onFeedback,
}: {
  m: RunMessage
  depth: number
  scenario: Scenario
  scriptSets: ReturnType<typeof collectScriptSets>
  macro: ReturnType<typeof makeMacro>
  /** null：試跑還在進行，不能評 */
  feedback: Feedback[] | null
  onFeedback: () => void
}) {
  const isUser = m.role === 'user'
  return (
    <div className={`bubble bubble-${m.role}`}>
      <span className="bubble-role">
        第 {m.turn} 回合 · {isUser ? scenario.user : scenario.char}
        <em> · {SOURCE_LABEL[m.source]}</em>
        {m.served_by && <em> · {m.served_by}</em>}
        {m.cost != null && <em> · {formatCost(m.cost)}</em>}
      </span>
      {m.reasoning && (
        <details className="reasoning">
          <summary>推理內容（{m.reasoning.length.toLocaleString()} 字）</summary>
          <pre>{m.reasoning}</pre>
        </details>
      )}
      {isUser ? (
        <div className="bubble-text">{m.content}</div>
      ) : (
        <Suspense fallback={<pre className="msg-content">{m.content}</pre>}>
          <StMessage text={m.content} theme="st-dark" depth={depth} charName={scenario.char} scriptSets={scriptSets} macro={macro} />
        </Suspense>
      )}
      {m.error && <p className="alert alert-error">{m.error}</p>}
      {m.finish_reason === 'length' && <p className="alert">被 max_tokens 截斷</p>}
      {feedback && !isUser && <FeedbackBox targetType="run_message" targetId={m.id} items={feedback} onChanged={onFeedback} compact />}
    </div>
  )
}
