import { lazy, Suspense, useMemo, useRef, useState } from 'react'
import { useNavigate } from 'react-router'
import { api, streamApi } from '../api'
import { formatCost, useResource } from '../hooks/useResource'
import { collectScriptSets } from '../lib/st/scripts'
import { makeMacro } from '../lib/st/regex'
import { shortId, type ExperimentDetail, type Preset, type Probe, type Run, type RunMessage, type WorkspaceDiff } from '../types'
import DiffView from './DiffView'
import MetricChips from './MetricChips'
import Modal from './Modal'

const StMessage = lazy(() => import('./StMessage'))

type ProbeEvent =
  | { type: 'start'; warnings: string[] }
  | { type: 'content' | 'reasoning'; text: string }
  | { type: 'meta'; provider: string | null }
  | { type: 'done'; probe: Probe }

/**
 * 迴歸試算：顯示「實驗版本 → 目前工作區」的提示詞差異，以相同的歷史與使用者發言、用新版本重新產生這一回合，
 * 可反覆試；滿意後開啟驗證輪次（只重跑這條或本階段，沿用原對話與使用者發言）。試算結果不寫入實驗。
 */
export default function ProbeDialog({
  exp,
  run,
  message,
  stageSize,
  onClose,
}: {
  exp: ExperimentDetail
  run: Run & { preset: Preset | null }
  message: RunMessage
  stageSize: number
  onClose: () => void
}) {
  const navigate = useNavigate()
  const diff = useResource<WorkspaceDiff>(`/experiments/${exp.id}/diff-workspace`)
  const ws = useResource<{ preset: Preset }>('/workspace')
  const probes = useResource<Probe[]>(`/experiments/${exp.id}/probes?run_id=${run.id}&turn=${message.turn}`)
  const [streaming, setStreaming] = useState<string | null>(null)
  const [selected, setSelected] = useState<number | null>(null)
  const [scope, setScope] = useState<'branch' | 'stage'>('branch')
  const [error, setError] = useState<string | null>(null)
  const abort = useRef<AbortController | null>(null)

  const scenario = run.scenario
  const macro = useMemo(() => makeMacro({ ...scenario.vars, char: scenario.char, user: scenario.user }), [scenario])
  const oldSets = useMemo(
    () => collectScriptSets((run.preset?.extra?.extensions as { regex_scripts?: unknown } | undefined)?.regex_scripts, scenario.card?.regex_scripts, { preset: true, card: true }),
    [run.preset, scenario.card],
  )
  const newSets = useMemo(
    () => collectScriptSets((ws.data?.preset.extra?.extensions as { regex_scripts?: unknown } | undefined)?.regex_scripts, scenario.card?.regex_scripts, { preset: true, card: true }),
    [ws.data, scenario.card],
  )
  const userMsg = run.messages.find((m) => m.turn === message.turn && m.role === 'user')
  const shown = probes.data?.find((p) => p.id === selected) ?? probes.data?.[0] ?? null

  const tryOnce = async () => {
    abort.current?.abort()
    const ctrl = new AbortController()
    abort.current = ctrl
    setError(null)
    setStreaming('')
    try {
      await streamApi<ProbeEvent>(
        `/experiments/${exp.id}/probe`,
        { run_id: run.id, turn: message.turn },
        (ev) => {
          if (ev.type === 'content') setStreaming((s) => (s ?? '') + ev.text)
          if (ev.type === 'done') setSelected(ev.probe.id)
        },
        ctrl.signal,
      )
    } catch (e) {
      if (!(e instanceof DOMException && e.name === 'AbortError')) setError(e instanceof Error ? e.message : String(e))
    } finally {
      setStreaming(null)
      probes.reload()
    }
  }

  const verify = async () => {
    const n = scope === 'branch' ? 1 : stageSize
    const state = await api<{ dirty: boolean }>('/vcs/state').catch(() => ({ dirty: false }))
    const note = state.dirty
      ? '\n\n提醒：工作區有未提交的修改。先提交的話，版本樹才會連到這輪驗證（之後也能從版本號找回這次的提示詞）。'
      : ''
    if (!window.confirm(`開啟驗證輪次：用目前工作區重跑階段 ${run.stage! + 1} 的 ${n} 條分支（沿用原對話與使用者發言），會產生費用。${note}`)) return
    try {
      const created = await api<ExperimentDetail>(`/experiments/${exp.id}/verify`, {
        method: 'POST',
        body: JSON.stringify({ from_stage: run.stage, run_ids: scope === 'branch' ? [run.id] : [] }),
      })
      navigate(`/experiments?exp=${created.id}`)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }

  return (
    <Modal
      wide
      title={`迴歸試算：分支 #${run.id} 第 ${message.turn} 回合`}
      onClose={onClose}
      footer={
        <>
          {error && <span className="alert alert-error">{error}</span>}
          <span className="small">驗證範圍</span>
          <div className="segmented" role="group" aria-label="驗證範圍">
            <button aria-pressed={scope === 'branch'} onClick={() => setScope('branch')}>
              這條分支
            </button>
            <button aria-pressed={scope === 'stage'} onClick={() => setScope('stage')}>
              本階段 {stageSize} 條
            </button>
          </div>
          <button className="btn btn-primary" disabled={!probes.data?.length || diff.data?.same} onClick={verify} title="試算滿意後，用新版本重跑選定範圍">
            開啟驗證輪次
          </button>
        </>
      }
    >
      <details className="probe-diff" open={!diff.data?.same}>
        <summary>
          提示詞變更：實驗版本 {exp.commit_id ? <code>{shortId(exp.commit_id)}</code> : '（未提交）'} → 目前工作區
          {diff.data && (diff.data.same ? '（沒有差異）' : `（${Object.entries(diff.data.diff.summary).filter(([, v]) => v).map(([k, v]) => `${k} ${v}`).join('、')}）`)}
        </summary>
        {diff.data?.same && <p className="alert">工作區和實驗用的版本相同。先到提示詞頁修改（或用「依評語修改」），再回來試算。</p>}
        {diff.data && !diff.data.same && <DiffView diff={diff.data.diff} labels={['實驗版本', '工作區']} />}
      </details>

      {userMsg && (
        <div className="bubble bubble-user">
          <span className="bubble-role">{scenario.user} · 第 {message.turn} 回合（兩邊相同）</span>
          <div className="bubble-text">{userMsg.content}</div>
        </div>
      )}

      <div className="probe-grid">
        <div className="compare-cell">
          <div className="compare-head">
            <strong className="side-label">原本</strong>
            <span className="muted small">實驗版本</span>
            <MetricChips defs={run.metric_defs} values={run.metrics?.messages[String(message.id)]} showZero />
          </div>
          <Suspense fallback={<pre className="msg-content">{message.content}</pre>}>
            <StMessage text={message.content} theme="st-dark" depth={0} charName={scenario.char} scriptSets={oldSets} macro={macro} />
          </Suspense>
        </div>
        <div className="compare-cell">
          <div className="compare-head">
            <strong className="side-label">新版本</strong>
            <span className="muted small">
              {shown && streaming === null ? `${formatCost(shown.cost)} · ${shown.served_by ?? ''}` : '工作區'}
            </span>
            {shown && streaming === null && <MetricChips defs={run.metric_defs} values={shown.metrics} showZero />}
          </div>
          {streaming !== null ? (
            <pre className="msg-content">{streaming || '生成中…'}</pre>
          ) : shown ? (
            <>
              {shown.error && <p className="alert alert-error">{shown.error}</p>}
              <Suspense fallback={<pre className="msg-content">{shown.content}</pre>}>
                <StMessage text={shown.content} theme="st-dark" depth={0} charName={scenario.char} scriptSets={newSets} macro={macro} />
              </Suspense>
            </>
          ) : (
            <p className="muted">還沒有試算。按下方按鈕，用目前工作區重新產生這一回合。</p>
          )}
          <div className="bubble-actions">
            <button className="btn btn-sm btn-primary" onClick={tryOnce} disabled={streaming !== null || diff.data?.same}>
              {streaming !== null ? '生成中…' : probes.data?.length ? '↻ 再試一次' : '▶ 用新版本試這回合'}
            </button>
            {(probes.data?.length ?? 0) > 1 && (
              <select value={shown?.id ?? ''} onChange={(e) => setSelected(Number(e.target.value))} aria-label="試算紀錄">
                {probes.data!.map((p, i) => (
                  <option key={p.id} value={p.id}>
                    第 {probes.data!.length - i} 次 · {new Date(p.created_at).toLocaleTimeString('zh-TW')} · {formatCost(p.cost)}
                  </option>
                ))}
              </select>
            )}
          </div>
        </div>
      </div>
      <p className="muted small">試算只產生這一回合，結果不寫入實驗；開啟驗證輪次才會建立新的實驗輪次（之前的階段不重跑）。</p>
    </Modal>
  )
}
