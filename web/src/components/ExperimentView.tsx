import { useEffect, useMemo, useState } from 'react'
import { Link, useNavigate } from 'react-router'
import { api } from '../api'
import { formatCost, useResource } from '../hooks/useResource'
import { scoreLevel } from './FeedbackBox'
import ModifyPanel from './ModifyPanel'
import RunView from './RunView'
import {
  PROMOTE_LABEL,
  USER_SOURCE_LABEL,
  formatScore,
  shortId,
  type ExpRun,
  type ExperimentDetail,
  type JudgeResult,
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
                <button className="btn btn-sm" onClick={rerun} title="用同一份腳本快照與情境，以目前工作區的預設重跑（迴歸比較）">
                  用目前工作區重跑
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
            : '打開分支後，每則泡泡都有「重試」與「批次」（本階段所有分支的同一回合）。'}
        </span>
      </div>

      <div className={`exp-body ${open ? 'has-panel' : ''}`}>
        <div className="stages">
          {script.stages.map((st, i) =>
            i > d.current_stage ? (
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
              />
            ),
          )}
        </div>
        {open !== null && (() => {
          const r = d.runs.find((x) => x.id === open)
          const hasChildren = d.runs.some((x) => x.parent_run_id === open)
          return (
            <RunView
              key={open}
              runId={open}
              refreshKey={r ? `${r.status}:${r.last_turn}:${r.cost}:${r.replies}` : ''}
              retry={
                r
                  ? {
                      experimentId: d.id,
                      stage: r.stage,
                      stageSize: byStage(r.stage).length,
                      inputMode,
                      laterUsers: later,
                      locked:
                        d.status === 'running'
                          ? '實驗正在執行，請等目前的階段或重試結束'
                          : hasChildren
                            ? '已有下一階段的分支接續它'
                            : null,
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

function StageCard({ d, st, i, runs, judge, active, open, pendingA, onOpen, onCompare, act, inputMode }: StageProps) {
  const script = d.script_snapshot
  const mode = st.promote.mode
  const isLast = i === script.stages.length - 1
  const next = script.stages[i + 1] as Stage | undefined
  const done = runs.filter((r) => r.status === 'done')
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
