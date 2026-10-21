import { useEffect, useMemo, useState } from 'react'
import { Link, useNavigate } from 'react-router'
import { api } from '../api'
import { formatCost, useResource } from '../hooks/useResource'
import { scoreLevel } from '../lib/runs'
import ModifyPanel from './ModifyPanel'
import RetryDialog, { type RetryRequest } from './RetryDialog'
import RunView from './RunView'
import {
  PROMOTE_LABEL,
  USER_SOURCE_LABEL,
  formatScore,
  shortId,
  type ExpRun,
  type ExperimentDetail,
  type JudgeResult,
  type RetryStats,
  type Stage,
  type Workspace,
} from '../types'

const STATUS_LABEL = { running: '執行中', waiting: '等待晉級', done: '完成', cancelled: '已中止' }

export default function ExperimentView({ id }: { id: number }) {
  const navigate = useNavigate()
  const exp = useResource<ExperimentDetail>(`/experiments/${id}`)
  const [open, setOpen] = useState<number | null>(null)
  const [pendingA, setPendingA] = useState<number | null>(null)
  const [msg, setMsg] = useState<string | null>(null)
  const [modify, setModify] = useState(false)
  // 使用者輸入：LLM 扮演，或我來輸入（使用者泡泡可編輯、晉級時可預先寫下一階段的發言）
  const [inputMode, setInputMode] = useState<'llm' | 'manual'>(() => {
    try {
      return localStorage.getItem('exp.inputMode') === 'manual' ? 'manual' : 'llm'
    } catch {
      return 'llm'
    }
  })
  const [laterUsers, setLaterUsers] = useState<'keep' | 'regenerate' | null>(null)
  const later = laterUsers ?? (inputMode === 'manual' ? 'keep' : 'regenerate')
  const switchMode = (m: 'llm' | 'manual') => {
    setInputMode(m)
    try {
      localStorage.setItem('exp.inputMode', m)
    } catch {
      // 無法儲存偏好時忽略
    }
  }
  const d = exp.data

  // 執行中每 1.5 秒更新進度
  const { reload } = exp
  useEffect(() => {
    if (d?.status !== 'running') return
    const t = window.setInterval(reload, 1500)
    return () => window.clearInterval(t)
  }, [d?.status, reload])

  if (exp.error) return <p className="alert alert-error">{exp.error}</p>
  if (!d) return <p className="muted">載入中…</p>

  const script = d.script_snapshot
  const byStage = (i: number) => d.runs.filter((r) => r.stage === i)

  const act = async (fn: () => Promise<unknown>) => {
    setMsg(null)
    try {
      await fn()
    } catch (e) {
      setMsg(e instanceof Error ? e.message : String(e))
    }
    exp.reload()
  }

  const rerun = () =>
    act(async () => {
      if (
        !window.confirm(
          `實驗重啟：用目前工作區從第 1 階段整張重跑，所有分支重新產生，成本最高（原實驗花費 ${formatCost(d.cost)}）。\n\n` +
            '只想驗證某個問題的話，請改用回覆上的「🔍 迴歸試算」，再開啟只重跑一條分支或一個階段的驗證輪次。\n\n確定要整張重跑？',
        )
      )
        return
      const ws = await api<Workspace>('/workspace')
      const created = await api<ExperimentDetail>('/experiments', {
        method: 'POST',
        body: JSON.stringify({ preset: ws.preset, rerun_of: d.id }),
      })
      navigate(`/experiments?exp=${created.id}`)
    })

  const remove = () =>
    act(async () => {
      if (!window.confirm(`刪除實驗「${d.name}」與它的 ${d.runs.length} 條分支（含評語）？`)) return
      await api(`/experiments/${d.id}`, { method: 'DELETE' })
      navigate('/experiments')
    })

  const compare = (r: ExpRun) => {
    if (pendingA === null) setPendingA(r.id)
    else if (pendingA === r.id) setPendingA(null)
    else navigate(`/experiments?a=${pendingA}&b=${r.id}&exp=${d.id}`)
  }

  return (
    <div className="experiment">
      <div className="card exp-head">
        <div className="list-head">
          <h2>
            {d.name} <span className={`chip exp-status exp-${d.status}`}>{STATUS_LABEL[d.status]}</span>
          </h2>
          <div className="header-actions">
            {d.status === 'running' ? (
              <button className="btn btn-sm danger" onClick={() => act(() => api(`/experiments/${d.id}/cancel`, { method: 'POST' }))}>
                ■ 中止
              </button>
            ) : (
              <>
                <button className="btn btn-sm btn-primary" onClick={() => setModify((v) => !v)} title="讓 LLM 讀這個實驗的評語並修改預設">
                  依評語修改…
                </button>
                <button className="btn btn-sm" onClick={rerun} title="實驗重啟：用目前工作區從第 1 階段整張重跑（成本最高）">
                  用目前工作區重跑（實驗重啟）
                </button>
                <button className="btn btn-sm danger" onClick={remove}>
                  刪除
                </button>
              </>
            )}
          </div>
        </div>
        <p className="muted small">
          預設 {d.preset_name}
          {d.commit_id ? ` @${shortId(d.commit_id)}` : '（未提交的版本）'} · 腳本 {d.script_name} · {USER_SOURCE_LABEL[script.user_source]} · 同時 {script.concurrency} 條 ·
          花費 {formatCost(d.cost)}
          {d.rerun_of && (
            <>
              {' '}
              · 重跑自 <Link to={`/experiments?exp=${d.rerun_of}`}>實驗 #{d.rerun_of}</Link>（分支旁的「對照」可並排比較）
            </>
          )}
        </p>
        <RetryStatsLine expId={d.id} refreshKey={`${d.status}:${d.runs.length}:${d.cost}`} />
        {msg && <p className="alert alert-error">{msg}</p>}
        {pendingA !== null && (
          <p className="alert" role="status">
            已選 #{pendingA} 為 A，再點另一條分支的「比較」。
            <button className="link-btn" onClick={() => setPendingA(null)}>
              取消
            </button>
          </p>
        )}
      </div>

      {modify && <ModifyPanel experimentId={d.id} onClose={() => setModify(false)} />}

      <div className="input-mode-bar">
        <span className="small">使用者輸入</span>
        <div className="segmented" role="group" aria-label="使用者輸入模式">
          <button aria-pressed={inputMode === 'llm'} onClick={() => switchMode('llm')}>
            LLM 扮演
          </button>
          <button aria-pressed={inputMode === 'manual'} onClick={() => switchMode('manual')}>
            我來輸入
          </button>
        </div>
        <label className="inline small">
          重試後，之後的使用者發言
          <select value={later} onChange={(e) => setLaterUsers(e.target.value as 'keep' | 'regenerate')}>
            <option value="keep">沿用原本的</option>
            <option value="regenerate">重新產生</option>
          </select>
        </label>
        <span className="muted small">
          {inputMode === 'manual'
            ? '打開分支後可直接改寫使用者發言並重試；晉級時可預先寫下一階段的使用者發言。'
            : '打開分支後可單則重試或「🔍 迴歸試算」；整條分支在分支面板、整個階段在階段卡片的「重試本階段」。'}
        </span>
      </div>

      <div className={`exp-body ${open ? 'has-panel' : ''}`}>
        <div className="stages">
          {script.stages.map((st, i) =>
            i < (d.state.partial_from ?? 0) ? (
              <div key={i} className="card stage-card is-future">
                <StageTitle st={st} i={i} />
                <p className="muted small">
                  沿用原實驗 <Link to={`/experiments?exp=${d.rerun_of}`}>#{d.rerun_of}</Link> 的對話（驗證輪次不重跑這個階段）
                </p>
              </div>
            ) : i > d.current_stage ? (
              <div key={i} className="card stage-card is-future">
                <StageTitle st={st} i={i} />
                <p className="muted small">尚未開始</p>
              </div>
            ) : (
              <StageCard
                key={`${i}:${d.current_stage}`}
                d={d}
                st={st}
                i={i}
                runs={byStage(i)}
                judge={d.state.judge?.[String(i)]}
                active={i === d.current_stage && d.status !== 'running' && i < script.stages.length - 1}
                open={open}
                pendingA={pendingA}
                onOpen={setOpen}
                onCompare={compare}
                act={act}
                inputMode={inputMode}
                laterUsers={later}
              />
            ),
          )}
        </div>
        {open !== null && (() => {
          const r = d.runs.find((x) => x.id === open)
          return (
            <RunView
              key={open}
              runId={open}
              refreshKey={r ? `${r.status}:${r.last_turn}:${r.cost}:${r.replies}` : ''}
              retry={
                r
                  ? {
                      exp: d,
                      stageSize: byStage(r.stage).length,
                      cascade: descendants(d.runs, [open]).length,
                      inputMode,
                      laterUsers: later,
                      onStarted: exp.reload,
                    }
                  : undefined
              }
              onFeedback={exp.reload}
              onClose={() => setOpen(null)}
            />
          )
        })()}
      </div>
    </div>
  )
}

function StageTitle({ st, i }: { st: Stage; i: number }) {
  return (
    <h3>
      階段 {i + 1}
      {st.name && `：${st.name}`}
      <span className="muted small">
        {' '}
        累計 {st.turns} 回合 · 每個起點 {st.seeds} 條
        {st.temperatures.length > 0 && ` × 溫度 ${st.temperatures.join(' / ')}`}
      </span>
    </h3>
  )
}

interface StageProps {
  d: ExperimentDetail
  st: Stage
  i: number
  runs: ExpRun[]
  judge: JudgeResult | undefined
  active: boolean
  open: number | null
  pendingA: number | null
  onOpen: (id: number) => void
  onCompare: (r: ExpRun) => void
  act: (fn: () => Promise<unknown>) => Promise<void>
  inputMode: 'llm' | 'manual'
  laterUsers: 'keep' | 'regenerate'
}

/** 接在這些分支後面的所有下游分支 */
function descendants(runs: ExpRun[], roots: number[]): number[] {
  const out: number[] = []
  let frontier = roots
  while (frontier.length) {
    const kids = runs.filter((r) => r.parent_run_id != null && frontier.includes(r.parent_run_id)).map((r) => r.id)
    out.push(...kids)
    frontier = kids
  }
  return out
}

function RetryStatsLine({ expId, refreshKey }: { expId: number; refreshKey: string }) {
  const stats = useResource<RetryStats>(`/experiments/${expId}/retry-stats?v=${encodeURIComponent(refreshKey)}`)
  const s = stats.data
  if (!s) return null
  const manual = s.models.filter((m) => m.manual > 0)
  const errors = s.providers.filter((p) => p.errors > 0)
  if (!manual.length && !errors.length) return null
  const pct = (v: number | null) => (v == null ? '—' : `${(v * 100).toFixed(1)}%`)
  return (
    <details className="retry-stats">
      <summary className="small">
        重試統計：
        {manual.map((m) => `${m.model} 手動重試 ${m.manual}/${m.replies}（${pct(m.manual_rate)}）`).join('、') || '沒有手動重試'}
        {errors.length > 0 && ` · 供應商錯誤：${errors.map((p) => `${p.provider} ${p.errors}/${p.calls}`).join('、')}`}
      </summary>
      <table className="ep-table">
        <thead>
          <tr>
            <th>角色模型</th>
            <th>產生的回覆</th>
            <th>手動重試</th>
            <th>重試率</th>
            <th>出錯續跑</th>
            <th>原因</th>
          </tr>
        </thead>
        <tbody>
          {s.models.map((m) => (
            <tr key={m.model}>
              <td>{m.model}</td>
              <td>{m.replies}</td>
              <td>{m.manual}</td>
              <td className={(m.manual_rate ?? 0) >= 0.2 ? 'delta-down' : ''}>{pct(m.manual_rate)}</td>
              <td>{m.error}</td>
              <td className="small">{Object.entries(m.tags).map(([t, n]) => `${t} ×${n}`).join('、') || '—'}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <table className="ep-table">
        <thead>
          <tr>
            <th>實際回應的供應商</th>
            <th>呼叫</th>
            <th>錯誤</th>
            <th>錯誤率</th>
          </tr>
        </thead>
        <tbody>
          {s.providers.map((p) => (
            <tr key={p.provider}>
              <td>{p.provider}</td>
              <td>{p.calls}</td>
              <td>{p.errors}</td>
              <td className={(p.error_rate ?? 0) >= 0.1 ? 'delta-down' : ''}>{pct(p.error_rate)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="muted small">手動重試率依角色模型計算（你不滿意而重試）；錯誤率依實際回應的供應商計算，偏高時可在連線設定排除該供應商。</p>
    </details>
  )
}

function topK(runs: ExpRun[], k: number): number[] {
  const groups = new Map<number | null, ExpRun[]>()
  for (const r of runs) if (r.status === 'done') groups.set(r.scenario_id, [...(groups.get(r.scenario_id) ?? []), r])
  return [...groups.values()].flatMap((g) =>
    g
      .filter((r) => r.score != null)
      .sort((a, b) => b.score! - a.score!)
      .slice(0, k)
      .map((r) => r.id),
  )
}

function StageCard({ d, st, i, runs, judge, active, open, pendingA, onOpen, onCompare, act, inputMode, laterUsers }: StageProps) {
  const script = d.script_snapshot
  const mode = st.promote.mode
  const isLast = i === script.stages.length - 1
  const next = script.stages[i + 1] as Stage | undefined
  const done = runs.filter((r) => r.status === 'done')
  // 出錯 / 中止、且還沒有子分支接續的分支可以續跑
  const unfinished = runs.filter((r) => (r.status === 'error' || r.status === 'cancelled') && !d.runs.some((x) => x.parent_run_id === r.id))
  const judgePicks = useMemo(
    () => Object.entries(judge?.picks ?? {}).filter(([, v]) => v.pick).map(([k]) => Number(k)),
    [judge],
  )

  const fromOrigin = useMemo(() => {
    const origin = new Set(d.origin_promoted ?? [])
    return runs.filter((r) => r.counterpart != null && origin.has(r.counterpart)).map((r) => r.id)
  }, [d.origin_promoted, runs])

  const preset = (): number[] => {
    if (fromOrigin.length) return fromOrigin
    if (mode === 'all') return done.map((r) => r.id)
    if (mode === 'top_k') return topK(runs, st.promote.k)
    if (mode === 'llm_assist') return judgePicks
    return []
  }
  const [selected, setSelected] = useState<Set<number> | null>(null)
  const sel = selected ?? new Set(active ? preset() : runs.filter((r) => r.promoted).map((r) => r.id))
  const toggle = (id: number) => {
    const n = new Set(sel)
    if (n.has(id)) n.delete(id)
    else n.add(id)
    setSelected(n)
  }

  const scores = done.map((r) => r.score).filter((v): v is number => v != null)
  const avg = scores.length ? scores.reduce((a, b) => a + b, 0) / scores.length : null
  const scenarios = [...new Set(runs.map((r) => r.scenario_id))]
  const nextBranches = next ? (mode === 'restart' ? d.scenario_ids.length : sel.size) * next.seeds * Math.max(1, next.temperatures.length) : 0

  // 我來輸入：下一階段新回合的使用者發言（空白的仍由 LLM 扮演）
  const [nextInputs, setNextInputs] = useState<Record<number, string>>({})
  const firstNew = mode === 'restart' ? 1 : st.turns + 1
  const newTurns = next ? Array.from({ length: Math.max(0, next.turns - firstNew + 1) }, (_, k) => firstNew + k) : []
  const advance = () =>
    act(() =>
      api(`/experiments/${d.id}/advance`, {
        method: 'POST',
        body: JSON.stringify({ run_ids: [...sel], user_turns: inputMode === 'manual' ? nextInputs : {} }),
      }),
    )
  const runJudge = () => act(() => api(`/experiments/${d.id}/judge`, { method: 'POST', body: JSON.stringify({ stage: i }) }))
  const [judging, setJudging] = useState(false)

  return (
    <div className={`card stage-card ${active ? 'is-active' : ''}`}>
      <div className="list-head">
        <StageTitle st={st} i={i} />
        <span className="muted small">
          {done.length}/{runs.length} 完成
          {avg != null && (
            <>
              {' '}
              · 平均{' '}
              <span className="chip score-chip" data-level={scoreLevel(avg)}>
                {formatScore(Math.round(avg * 100) / 100)}
              </span>
            </>
          )}
          {!isLast && ` · 結束後：${PROMOTE_LABEL[mode]}${mode === 'top_k' || mode === 'llm_assist' ? `（K = ${st.promote.k}）` : ''}`}
        </span>
      </div>

      {unfinished.length > 0 && (
        <div className="resume-bar">
          <span className="small">
            {unfinished.length} 條分支沒有跑完（出錯或中止）：{unfinished.map((r) => `#${r.id}`).join('、')}
          </span>
          <button
            className="btn btn-sm"
            onClick={() =>
              act(() => api(`/experiments/${d.id}/retry`, { method: 'POST', body: JSON.stringify({ stage: { stage: i, kind: 'resume' } }) }))
            }
            title="從中斷處接著跑（最後一則出錯就重試它），已完成的部分保留"
          >
            ↻ 續跑未完成的分支
          </button>
        </div>
      )}
      <StageRetry d={d} st={st} i={i} runs={runs} inputMode={inputMode} laterUsers={laterUsers} onStarted={() => act(async () => {})} />
      {judge && (judge.error || judge.model) && (
        <p className={`small ${judge.error ? 'alert alert-error' : 'muted'}`}>
          {judge.error ? `評審失敗：${judge.error}` : `評審 ${judge.model} 已預選 ${judgePicks.length} 條 · ${formatCost(judge.cost)}`}
        </p>
      )}

      {scenarios.map((sid) => (
        <div key={sid ?? 'none'} className="branch-group">
          {scenarios.length > 1 && <h4>{d.scenarios[String(sid)] ?? `情境 #${sid}`}</h4>}
          <ul className="branch-list">
            {runs
              .filter((r) => r.scenario_id === sid)
              .map((r) => {
                const jp = judge?.picks[String(r.id)]
                const issues = (r.checks?.issues.length ?? 0) + (r.checks?.build_warnings.length ?? 0)
                return (
                  <li key={r.id} className={`branch ${open === r.id ? 'is-open' : ''} ${r.promoted ? 'is-promoted' : ''}`}>
                    {active && mode !== 'restart' ? (
                      <input type="checkbox" checked={sel.has(r.id)} disabled={r.status !== 'done'} onChange={() => toggle(r.id)} aria-label={`晉級 #${r.id}`} />
                    ) : (
                      <span className="promoted-mark" title={r.promoted ? '已晉級' : ''}>
                        {r.promoted ? '↑' : ''}
                      </span>
                    )}
                    <span className={`status-dot status-${r.status}`} aria-label={r.status} />
                    <button className="branch-main" onClick={() => onOpen(r.id)}>
                      <span className="branch-meta">
                        #{r.id} · seed {r.seed}
                        {r.temperature != null && ` · 溫度 ${r.temperature}`}
                        {r.parent_run_id && ` · ← #${r.parent_run_id}`}
                        {r.status === 'done' && ` · ${r.last_turn ?? 0} 回合`}
                        {issues > 0 && <em className="warn"> · {issues} 個提醒</em>}
                        {r.score != null && (
                          <span className="chip score-chip" data-level={scoreLevel(r.score)}>
                            {formatScore(r.score)}
                          </span>
                        )}
                        {jp && <span className={`chip judge-chip ${jp.pick ? 'is-pick' : ''}`}>評審 #{jp.rank}</span>}
                      </span>
                      <span className="branch-excerpt">{r.error ?? (r.excerpt || (r.status === 'queued' ? '排隊中…' : r.status === 'running' ? '生成中…' : ''))}</span>
                      {jp?.reason && <span className="branch-reason">評審：{jp.reason}</span>}
                    </button>
                    <span className="branch-actions">
                      {(r.status === 'running' || r.status === 'queued') && (
                        <button
                          className="btn btn-sm danger"
                          title="只中止這條分支（卡住時用），其他分支繼續"
                          onClick={() => act(() => api(`/experiments/${d.id}/runs/${r.id}/cancel`, { method: 'POST' }))}
                        >
                          中止
                        </button>
                      )}
                      {r.counterpart && (
                        <Link className="btn btn-sm" to={`/experiments?a=${r.counterpart}&b=${r.id}&exp=${d.id}`} title="與原實驗同位置的分支並排比較">
                          對照
                        </Link>
                      )}
                      <button className="btn btn-sm" aria-pressed={pendingA === r.id} onClick={() => onCompare(r)}>
                        比較
                      </button>
                    </span>
                  </li>
                )
              })}
          </ul>
        </div>
      ))}

      {active && next && inputMode === 'manual' && newTurns.length > 0 && (
        <div className="next-inputs">
          <span className="small">下一階段的使用者發言（套用到所有新分支；留空的回合由 LLM 扮演）</span>
          {newTurns.map((t) => (
            <label key={t} className="stack">
              第 {t} 回合
              <textarea value={nextInputs[t] ?? ''} onChange={(e) => setNextInputs({ ...nextInputs, [t]: e.target.value })} />
            </label>
          ))}
        </div>
      )}
      {active && next && (
        <div className="advance-bar">
          {mode === 'restart' ? (
            <span className="small">下一階段不沿用本階段結果，從開場重跑（{nextBranches} 條分支）。</span>
          ) : (
            <>
              <span className="small">
                已勾選 <b>{sel.size}</b> 條 → 下一階段 {nextBranches} 條分支
              </span>
              <span className="presets">
                <button className="link-btn" onClick={() => setSelected(new Set(topK(runs, st.promote.k)))} title="每個情境依評語分數取前 K 條">
                  分數前 {st.promote.k}
                </button>
                {judgePicks.length > 0 && (
                  <button className="link-btn" onClick={() => setSelected(new Set(judgePicks))}>
                    評審預選
                  </button>
                )}
                {fromOrigin.length > 0 && (
                  <button className="link-btn" onClick={() => setSelected(new Set(fromOrigin))} title="勾選與原實驗晉級的分支同位置的分支">
                    照原實驗
                  </button>
                )}
                <button className="link-btn" onClick={() => setSelected(new Set(done.map((r) => r.id)))}>
                  全選
                </button>
                <button className="link-btn" onClick={() => setSelected(new Set())}>
                  清除
                </button>
                <button
                  className="link-btn"
                  disabled={judging}
                  onClick={async () => {
                    setJudging(true)
                    await runJudge()
                    setJudging(false)
                    setSelected(null)
                  }}
                  title="請評審 LLM 依設定頁的挑選標準排序（會產生少量費用）"
                >
                  {judging ? '評審中…' : judge ? '請評審重新挑選' : '請評審挑選'}
                </button>
              </span>
            </>
          )}
          <button className="btn btn-primary" disabled={mode !== 'restart' && sel.size === 0} onClick={advance}>
            開始階段 {i + 2}
          </button>
        </div>
      )}
    </div>
  )
}

/** 階段層級的重試：本階段所有分支「整條重新產生」或「指定回合 / 角色」；我來輸入模式可直接給該回合的使用者發言。 */
function StageRetry({
  d,
  st,
  i,
  runs,
  inputMode,
  laterUsers,
  onStarted,
}: {
  d: ExperimentDetail
  st: Stage
  i: number
  runs: ExpRun[]
  inputMode: 'llm' | 'manual'
  laterUsers: 'keep' | 'regenerate'
  onStarted: () => void
}) {
  const stages = d.script_snapshot.stages
  const prev = i === 0 || stages[i - 1].promote.mode === 'restart' ? 0 : stages[i - 1].turns
  const turns = Array.from({ length: Math.max(0, st.turns - prev) }, (_, k) => prev + k + 1)
  const [mode, setMode] = useState<'branch' | 'message'>('message')
  const [turn, setTurn] = useState(turns[turns.length - 1] ?? 1)
  const [role, setRole] = useState<'assistant' | 'user'>('assistant')
  const [text, setText] = useState('')
  const [req, setReq] = useState<RetryRequest | null>(null)
  const [result, setResult] = useState<string | null>(null)
  const settled = runs.filter((r) => r.status !== 'running' && r.status !== 'queued')
  const cascade = descendants(d.runs, settled.map((r) => r.id)).length
  const manualText = mode === 'message' && role === 'user' && inputMode === 'manual'

  const open = () =>
    setReq({
      title: `重試階段 ${i + 1}（${settled.length} 條分支）`,
      description: (
        <p className="small">
          {mode === 'branch'
            ? '本階段每條分支的所有回合重新產生（上層的對話保留）。'
            : `本階段每條分支的第 ${turn} 回合${role === 'user' ? '使用者發言' : '角色回覆'}${manualText && text.trim() ? '改成你輸入的內容並' : ''}重新產生，之後的訊息封存、續跑到原本的回合數。`}
          執行中的分支會略過。
        </p>
      ),
      body: {
        stage: { stage: i, kind: mode, ...(mode === 'message' ? { turn, role } : {}) },
        ...(manualText && text.trim() ? { user_text: text } : {}),
      },
      cascade,
      askLater: true,
    })

  if (!runs.length) return null
  return (
    <details className="stage-retry">
      <summary className="small">↻ 重試本階段…</summary>
      <div className="stage-retry-body">
        <div className="segmented" role="group" aria-label="重試範圍">
          <button aria-pressed={mode === 'message'} onClick={() => setMode('message')}>
            指定回合
          </button>
          <button aria-pressed={mode === 'branch'} onClick={() => setMode('branch')}>
            整條分支
          </button>
        </div>
        {mode === 'message' && (
          <>
            <label className="inline small">
              回合
              <select value={turn} onChange={(e) => setTurn(Number(e.target.value))}>
                {turns.map((t) => (
                  <option key={t} value={t}>
                    第 {t} 回合
                  </option>
                ))}
              </select>
            </label>
            <label className="inline small">
              角色
              <select value={role} onChange={(e) => setRole(e.target.value as 'assistant' | 'user')}>
                <option value="assistant">角色回覆</option>
                <option value="user">使用者發言</option>
              </select>
            </label>
          </>
        )}
        {manualText && (
          <textarea
            className="stage-retry-text"
            value={text}
            placeholder="這一回合所有分支的使用者發言（留空則由 LLM 重新扮演）"
            onChange={(e) => setText(e.target.value)}
          />
        )}
        <button className="btn btn-sm btn-primary" disabled={!settled.length} onClick={open}>
          重試 {settled.length} 條…
        </button>
        {result && <span className="muted small">{result}</span>}
      </div>
      {req && (
        <RetryDialog
          experimentId={d.id}
          req={req}
          defaultLater={laterUsers}
          onDone={(r) => {
            setReq(null)
            setResult(`已開始 ${r.started.length} 條${r.cascade.length ? `，連帶 ${r.cascade.length} 條` : ''}${r.skipped.length ? `，略過 ${r.skipped.length} 條` : ''}`)
            onStarted()
          }}
          onClose={() => setReq(null)}
        />
      )}
    </details>
  )
}
