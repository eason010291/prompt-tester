import { useState } from 'react'
import { api } from '../api'
import { formatCost, useResource } from '../hooks/useResource'
import FeedbackBox from './FeedbackBox'
import { MessageList, withDepth } from './RunCompare'
import type { ArchivedMessage, Feedback, Preset, Run, RunMessage } from '../types'

/** 實驗裡的重試設定（由實驗頁的工具列決定） */
export interface RetryContext {
  experimentId: number
  /** 這條分支所屬的階段、該階段的分支數（批次用） */
  stage: number
  stageSize: number
  /** llm：使用者發言由 LLM 扮演；manual：我來輸入（使用者泡泡可直接編輯） */
  inputMode: 'llm' | 'manual'
  laterUsers: 'keep' | 'regenerate'
  /** 不能重試的原因（實驗執行中、已有下一階段分支…） */
  locked: string | null
  onStarted: () => void
}

/** 單一分支：先前的對話（含父分支）摺疊、本段訊息、重試 / 編輯、整體與逐則評語、舊版本。 */
export default function RunView({
  runId,
  refreshKey,
  retry,
  onFeedback,
  onClose,
}: {
  runId: number
  /** 分支狀態改變時（例如重試中）重新讀取 */
  refreshKey?: string
  retry?: RetryContext
  onFeedback: () => void
  onClose: () => void
}) {
  const run = useResource<Run & { preset: Preset | null }>(`/runs/${runId}?preset=true&v=${encodeURIComponent(refreshKey ?? '')}`)
  const feedback = useResource<Feedback[]>(`/feedback?run_id=${runId}&v=${encodeURIComponent(refreshKey ?? '')}`)
  const archive = useResource<ArchivedMessage[]>(`/runs/${runId}/archive?v=${encodeURIComponent(refreshKey ?? '')}`)
  const [error, setError] = useState<string | null>(null)
  const changed = () => {
    feedback.reload()
    onFeedback()
  }
  const r = run.data

  const send = async (body: Record<string, unknown>, confirmText?: string) => {
    if (!retry) return
    if (confirmText && !window.confirm(confirmText)) return
    setError(null)
    try {
      const res = await api<{ started: number[]; skipped: { run_id: number; reason: string }[] }>(
        `/experiments/${retry.experimentId}/retry`,
        { method: 'POST', body: JSON.stringify({ later_users: retry.laterUsers, ...body }) },
      )
      if (res.skipped.length) setError(`已開始 ${res.started.length} 條；略過：` + res.skipped.map((s) => `#${s.run_id} ${s.reason}`).join('；'))
      retry.onStarted()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }

  const later = retry?.laterUsers === 'keep' ? '之後的使用者發言沿用原本的' : '之後的使用者發言重新產生'
  const single = (m: RunMessage, userText?: string) =>
    send(
      { items: [{ run_id: runId, turn: m.turn, role: m.role }], ...(userText !== undefined ? { user_text: userText } : {}) },
      `重新產生第 ${m.turn} 回合的${m.role === 'user' ? '使用者發言' : '角色回覆'}，並續跑到原本的回合數？\n（這則之後的訊息會封存；${later}）`,
    )
  const batch = (m: RunMessage, userText?: string) =>
    send(
      { batch: { stage: retry!.stage, turn: m.turn, role: m.role }, ...(userText !== undefined ? { user_text: userText } : {}) },
      `本階段 ${retry!.stageSize} 條分支的第 ${m.turn} 回合${m.role === 'user' ? '使用者發言' : '角色回覆'}全部${userText !== undefined ? '改成這段輸入並' : ''}重試？\n（會產生費用；${later}；已有下一階段分支的會略過）`,
    )

  const busy = r?.status === 'queued' || r?.status === 'running'
  const locked = retry?.locked ?? (busy ? '這條分支正在執行' : null)

  const actions = (m: RunMessage) => {
    if (!retry || (m.role === 'user' && retry.inputMode === 'manual')) return null
    return (
      <div className="bubble-actions">
        <button className="btn btn-sm" disabled={!!locked} title={locked ?? '只重試這條分支'} onClick={() => single(m)}>
          ↻ 重試
        </button>
        <button className="btn btn-sm" disabled={!!locked} title={locked ?? `本階段所有分支的第 ${m.turn} 回合`} onClick={() => batch(m)}>
          ↻ 批次（{retry.stageSize} 條）
        </button>
      </div>
    )
  }

  const userBody = (m: RunMessage) =>
    retry?.inputMode === 'manual' ? (
      <UserEditor key={`${m.id}:${m.content}`} m={m} locked={locked} stageSize={retry.stageSize} onSingle={(t) => single(m, t)} onBatch={(t) => batch(m, t)} />
    ) : null

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
            {r.temperature != null && ` · 溫度 ${r.temperature}`} · {busy ? '執行中…' : r.status} · {formatCost(r.cost)}
            {r.parent_run_id && ` · 接續 #${r.parent_run_id}`}
          </p>
          {retry?.locked && <p className="muted small">重試已停用：{retry.locked}</p>}
          {error && <p className="alert">{error}</p>}
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
            <MessageList
              run={r}
              preset={r.preset}
              messages={withDepth(r)}
              feedback={feedback.data ?? null}
              onFeedback={changed}
              renderActions={actions}
              renderUserBody={userBody}
            />
            {busy && <p className="muted small">生成中…（完成後自動更新）</p>}
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
          {(archive.data?.length ?? 0) > 0 && (
            <details className="history-fold">
              <summary>重試前的舊訊息（{archive.data!.length} 則）</summary>
              {archive.data!.map((a) => (
                <div key={a.id} className="archived">
                  <span className="muted small">
                    {a.reason} · 第 {a.turn} 回合 {a.role === 'user' ? r.scenario.user : r.scenario.char} ·{' '}
                    {new Date(a.archived_at).toLocaleString('zh-TW', { dateStyle: 'short', timeStyle: 'short' })}
                  </span>
                  <pre className="msg-content">
                    {a.content.slice(0, 1500)}
                    {a.content.length > 1500 && '…'}
                  </pre>
                </div>
              ))}
            </details>
          )}
        </>
      )}
    </aside>
  )
}

function UserEditor({
  m,
  locked,
  stageSize,
  onSingle,
  onBatch,
}: {
  m: RunMessage
  locked: string | null
  stageSize: number
  onSingle: (text: string) => void
  onBatch: (text: string) => void
}) {
  const [text, setText] = useState(m.content)
  const empty = !text.trim()
  return (
    <div className="user-editor">
      <textarea value={text} onChange={(e) => setText(e.target.value)} aria-label={`第 ${m.turn} 回合使用者發言`} disabled={!!locked} />
      <div className="bubble-actions">
        <button className="btn btn-sm btn-primary" disabled={!!locked || empty} title={locked ?? '改成這段輸入，重新產生之後的回覆'} onClick={() => onSingle(text)}>
          儲存並重試
        </button>
        <button className="btn btn-sm" disabled={!!locked || empty} title={locked ?? `本階段所有分支的第 ${m.turn} 回合都改成這段`} onClick={() => onBatch(text)}>
          套用到本階段 {stageSize} 條
        </button>
        {text !== m.content && (
          <button className="link-btn" onClick={() => setText(m.content)}>
            還原
          </button>
        )}
      </div>
    </div>
  )
}
