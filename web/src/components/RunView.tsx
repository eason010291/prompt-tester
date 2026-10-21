import { useResource } from '../hooks/useResource'
import { formatCost } from '../hooks/useResource'
import FeedbackBox from './FeedbackBox'
import { MessageList, withDepth } from './RunCompare'
import type { Feedback, Preset, Run } from '../types'

/** 單一分支：先前的對話（含父分支）摺疊、本段訊息、整體與逐則評語。 */
export default function RunView({ runId, onFeedback, onClose }: { runId: number; onFeedback: () => void; onClose: () => void }) {
  const run = useResource<Run & { preset: Preset | null }>(`/runs/${runId}?preset=true`)
  const feedback = useResource<Feedback[]>(`/feedback?run_id=${runId}`)
  const changed = () => {
    feedback.reload()
    onFeedback()
  }
  const r = run.data

  return (
    <aside className="card run-view" aria-label={`分支 #${runId}`}>
      <div className="list-head">
        <h2>分支 #{runId}</h2>
        <button className="icon-btn" aria-label="關閉" onClick={onClose}>
          ✕
        </button>
      </div>
      {run.error && <p className="alert alert-error">{run.error}</p>}
      {r && (
        <>
          <p className="muted small">
            seed {r.seed}
            {r.temperature != null && ` · 溫度 ${r.temperature}`} · {r.status} · {formatCost(r.cost)}
            {r.parent_run_id && ` · 接續 #${r.parent_run_id}`}
          </p>
          {r.scenario.history.length > 0 && (
            <details className="history-fold">
              <summary>先前的對話（開場、既有歷史與父分支，共 {r.scenario.history.length} 則）</summary>
              {r.scenario.history.map((h, i) => (
                <pre key={i} className="msg-content">
                  {h.role === 'user' ? r.scenario.user : r.scenario.char}：{h.content.slice(0, 800)}
                  {h.content.length > 800 && '…'}
                </pre>
              ))}
            </details>
          )}
          <div className="transcript">
            <MessageList run={r} preset={r.preset} messages={withDepth(r)} feedback={feedback.data ?? null} onFeedback={changed} />
          </div>
          {r.error && <p className="alert alert-error">{r.error}</p>}
          {feedback.data && (
            <FeedbackBox
              targetType="run"
              targetId={r.id}
              items={feedback.data.filter((f) => f.target_type === 'run')}
              onChanged={changed}
              label="整體評語"
            />
          )}
        </>
      )}
    </aside>
  )
}
