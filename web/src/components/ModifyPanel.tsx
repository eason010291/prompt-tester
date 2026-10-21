import { useEffect, useState } from 'react'
import { Link } from 'react-router'
import { api } from '../api'
import { formatCost, useResource } from '../hooks/useResource'
import { shortId, type AgentRound, type AgentSettings, type Connection } from '../types'

const STATUS_LABEL: Record<AgentRound['status'], string> = {
  running: '修改中',
  done: '完成',
  uncommitted: '改了但未提交',
  error: '失敗',
  cancelled: '已中止',
}

const TOOL_LABEL: Record<string, string> = {
  list_blocks: '列出區塊',
  get_block: '讀取區塊',
  edit_block: '改寫區塊',
  patch_block: '局部修改',
  add_block: '新增區塊',
  remove_block: '刪除區塊',
  move_block: '移動區塊',
  toggle_block: '開關區塊',
  set_params: '改參數',
  preview: '預覽組裝',
  diff_workspace: '檢查差異',
  commit: '提交',
  get_feedback: '讀評語',
  get_reply: '讀回覆',
  list_scenarios: '列出情境',
}

/**
 * ① 內建迴圈：「依評語修改 → 提交 → 重跑」。
 * experimentId 有值時讀那個實驗的評語，並可在提交後自動重跑。
 */
export default function ModifyPanel({ experimentId, onClose }: { experimentId: number | null; onClose: () => void }) {
  const settings = useResource<AgentSettings>('/agent/settings')
  const connections = useResource<Connection[]>('/connections')
  const history = useResource<AgentRound[]>(experimentId ? `/agent/rounds?experiment_id=${experimentId}` : '/agent/rounds')
  const [instructions, setInstructions] = useState('')
  const [autoRerun, setAutoRerun] = useState(experimentId !== null)
  const [current, setCurrent] = useState<number | null>(null)
  const [error, setError] = useState<string | null>(null)

  const conn = connections.data?.find((c) => c.id === settings.data?.connection_id) ?? null
  const latest = current ?? history.data?.[0]?.id ?? null

  const start = async () => {
    setError(null)
    try {
      const r = await api<AgentRound>('/agent/rounds', {
        method: 'POST',
        body: JSON.stringify({ instructions, experiment_id: experimentId, auto_rerun: autoRerun }),
      })
      setCurrent(r.id)
      history.reload()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }

  return (
    <div className="card modify-panel">
      <div className="list-head">
        <h2>{experimentId ? '依評語修改' : '請 LLM 修改預設'}</h2>
        <button className="icon-btn" aria-label="關閉" onClick={onClose}>
          ✕
        </button>
      </div>
      <p className="muted small">
        修改者：{conn ? `${conn.name}（${conn.model}）` : <Link to="/settings">尚未設定（設定頁 → 修改者 LLM）</Link>} · 上限 {settings.data?.max_steps} 次呼叫 / $
        {settings.data?.budget_usd}。LLM 只改工作區；在 main 上時會先建立 <code>llm/…</code> 分支，main 不會被移動。修改期間請不要編輯提示詞頁。
      </p>
      <label className="stack">
        要求（選填{experimentId ? '；不填就依這個實驗的評語修改' : ''}）
        <textarea
          value={instructions}
          placeholder={experimentId ? '例如：狀態欄格式不要動，只處理文風的套話問題' : '例如：減少替使用者說話的情況'}
          onChange={(e) => setInstructions(e.target.value)}
        />
      </label>
      <div className="add-bar">
        {experimentId && (
          <label className="check">
            <input type="checkbox" checked={autoRerun} onChange={(e) => setAutoRerun(e.target.checked)} />
            提交後自動用新版本重跑這個實驗（第一階段，會產生費用）
          </label>
        )}
        <button className="btn btn-primary" onClick={start} disabled={!conn}>
          開始修改
        </button>
        {error && <span className="alert alert-error">{error}</span>}
      </div>

      {latest !== null && <RoundView key={latest} id={latest} onChanged={history.reload} />}

      {(history.data?.length ?? 0) > 1 && (
        <details className="round-history">
          <summary>先前的修改（{(history.data?.length ?? 1) - 1}）</summary>
          <ul>
            {history.data
              ?.filter((r) => r.id !== latest)
              .map((r) => (
                <li key={r.id}>
                  <button className="link-btn" onClick={() => setCurrent(r.id)}>
                    #{r.id}
                  </button>{' '}
                  {STATUS_LABEL[r.status]} · {r.author} · {r.step_count} 步 · {formatCost(r.cost)}
                  {r.result_commit && <code> → {shortId(r.result_commit)}</code>}
                </li>
              ))}
          </ul>
        </details>
      )}
    </div>
  )
}

function RoundView({ id, onChanged }: { id: number; onChanged: () => void }) {
  const round = useResource<AgentRound>(`/agent/rounds/${id}`)
  const r = round.data
  const { reload } = round

  useEffect(() => {
    if (r?.status !== 'running') {
      if (r) onChanged()
      return
    }
    const t = window.setInterval(reload, 1500)
    return () => window.clearInterval(t)
  }, [r?.status, reload]) // onChanged 只在狀態改變時通知，不列入依賴

  if (!r) return <p className="muted">載入中…</p>
  const act = async (path: string) => {
    try {
      await api(`/agent/rounds/${id}/${path}`, { method: 'POST' })
    } catch (e) {
      window.alert(e instanceof Error ? e.message : String(e))
    }
    reload()
  }

  return (
    <div className={`round round-${r.status}`}>
      <div className="round-head">
        <strong>
          #{r.id} · {STATUS_LABEL[r.status]}
        </strong>
        <span className="muted small">
          {r.author} · 分支 {r.branch} · {r.steps?.length ?? 0} 步 / {r.tool_calls} 次工具 · {formatCost(r.cost)}
        </span>
        {r.status === 'running' && (
          <button className="btn btn-sm danger" onClick={() => act('cancel')}>
            ■ 中止
          </button>
        )}
        {(r.status === 'uncommitted' || r.status === 'error' || r.status === 'cancelled') && !r.result_commit && (
          <button className="btn btn-sm" onClick={() => window.confirm('把工作區還原成這輪開始前的版本？') && act('discard')}>
            捨棄這輪的變更
          </button>
        )}
      </div>
      {r.error && <p className={`alert ${r.result_commit ? '' : 'alert-error'}`}>{r.error}</p>}
      {r.status === 'uncommitted' && <p className="alert">修改留在工作區，可以到提示詞頁檢查後自己提交，或捨棄。</p>}

      <ol className="steps">
        {r.steps?.map((s) => (
          <li key={s.step}>
            {s.text && <p className="step-text">{s.text}</p>}
            {s.reasoning && (
              <details className="reasoning">
                <summary>推理（{s.reasoning.length} 字）</summary>
                <pre>{s.reasoning}</pre>
              </details>
            )}
            {s.calls.map((c, i) => (
              <details key={i} className={`call ${c.ok ? '' : 'is-error'}`}>
                <summary>
                  {c.ok ? '✓' : '✗'} {TOOL_LABEL[c.name] ?? c.name} <code>{c.name}</code> {summarizeArgs(c.args)}
                </summary>
                <pre>{JSON.stringify(c.args, null, 2)}</pre>
                <pre className="call-result">{c.result}</pre>
              </details>
            ))}
          </li>
        ))}
        {r.status === 'running' && <li className="muted small">等待模型回應…</li>}
      </ol>

      {r.result_commit && (
        <div className="round-result">
          已提交 <code>{shortId(r.result_commit)}</code>（基於 <code>{shortId(r.base_commit)}</code>，分支 {r.branch}）·{' '}
          <Link to="/versions">到版本樹查看 diff</Link>
          {r.rerun_experiment_id && (
            <>
              {' '}
              · <Link to={`/experiments?exp=${r.rerun_experiment_id}`}>重跑的實驗 #{r.rerun_experiment_id} →</Link>
            </>
          )}
        </div>
      )}
      {r.final_message && <p className="step-text">{r.final_message}</p>}
    </div>
  )
}

function summarizeArgs(args: unknown): string {
  if (!args || typeof args !== 'object') return ''
  const a = args as Record<string, unknown>
  if (typeof a.message === 'string') return `「${a.message.slice(0, 40)}」`
  if (typeof a.name === 'string') return `「${a.name}」`
  if (typeof a.find === 'string') return `「${a.find.slice(0, 20)}…」`
  if (typeof a.id === 'string') return a.id.slice(0, 8)
  if (typeof a.experiment_id === 'number') return `實驗 #${a.experiment_id}`
  return ''
}
