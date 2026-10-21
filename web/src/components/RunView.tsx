import { useState } from 'react'
import { api } from '../api'
import { formatCost, useResource } from '../hooks/useResource'
import FeedbackBox from './FeedbackBox'
import ProbeDialog from './ProbeDialog'
import RetryDialog, { type RetryRequest } from './RetryDialog'
import { MessageList, withDepth } from './RunCompare'
import type { ArchivedMessage, ExperimentDetail, Feedback, Preset, RetryResult, Run, RunMessage } from '../types'

/** 實驗裡的重試設定（由實驗頁決定） */
export interface RetryContext {
  exp: ExperimentDetail
  /** 這條分支所屬階段的分支數（驗證輪次的範圍選項用） */
  stageSize: number
  /** 接在這條分支後面的下一階段分支數（重試會連帶重跑） */
  cascade: number
  /** llm：使用者發言由 LLM 扮演；manual：我來輸入（使用者泡泡可直接編輯） */
  inputMode: 'llm' | 'manual'
  laterUsers: 'keep' | 'regenerate'
  onStarted: () => void
}

/** 單一分支：先前的對話摺疊、本段訊息（單則重試 / 編輯 / 迴歸試算）、整條重試、續跑、評語、舊版本。 */
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
  const v = encodeURIComponent(refreshKey ?? '')
  const run = useResource<Run & { preset: Preset | null }>(`/runs/${runId}?preset=true&v=${v}`)
  const feedback = useResource<Feedback[]>(`/feedback?run_id=${runId}&v=${v}`)
  const archive = useResource<ArchivedMessage[]>(`/runs/${runId}/archive?v=${v}`)
  const [error, setError] = useState<string | null>(null)
  const [dialog, setDialog] = useState<RetryRequest | null>(null)
  const [probe, setProbe] = useState<RunMessage | null>(null)
  const changed = () => {
    feedback.reload()
    onFeedback()
  }
  const r = run.data
  const busy = r?.status === 'queued' || r?.status === 'running'
  const incomplete = !busy && (r?.status === 'error' || r?.status === 'cancelled')
  const lockedReason = busy ? '這條分支正在執行（可以先中止）' : null

  const done = (res: RetryResult) => {
    setDialog(null)
    setError(res.skipped.length ? '略過：' + res.skipped.map((s) => `#${s.run_id} ${s.reason}`).join('；') : null)
    retry?.onStarted()
  }

  const retryMessage = (m: RunMessage, userText?: string) =>
    setDialog({
      title: userText !== undefined ? `改寫第 ${m.turn} 回合使用者發言並重試` : `重試第 ${m.turn} 回合的${m.role === 'user' ? '使用者發言' : '角色回覆'}`,
      description: (
        <p className="small">
          只影響這條分支：這則{m.role === 'assistant' ? '' : '與之後的回覆'}會重新產生，之後的訊息封存，續跑到原本的回合數。
          {userText !== undefined && (
            <>
              <br />
              新的使用者發言：「{userText.slice(0, 80)}」
            </>
          )}
        </p>
      ),
      body: { items: [{ run_id: runId, kind: 'message', turn: m.turn, role: m.role }], ...(userText !== undefined ? { user_text: userText } : {}) },
      cascade: retry?.cascade ?? 0,
      askLater: true,
    })

  const retryBranch = () =>
    setDialog({
      title: `重試整條分支 #${runId}`,
      description: <p className="small">這條分支本階段的所有回合重新產生（上層的對話保留）。</p>,
      body: { items: [{ run_id: runId, kind: 'branch' }] },
      cascade: retry?.cascade ?? 0,
      askLater: true,
    })

  const cancelThis = async () => {
    if (!retry) return
    try {
      await api(`/experiments/${retry.exp.id}/runs/${runId}/cancel`, { method: 'POST' })
      retry.onStarted()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }

  const resume = async () => {
    if (!retry || !window.confirm('從中斷處接著跑完這條分支？（已完成的部分保留；最後一則出錯就重試它）')) return
    try {
      done(await api<RetryResult>(`/experiments/${retry.exp.id}/retry`, { method: 'POST', body: JSON.stringify({ items: [{ run_id: runId, kind: 'resume' }] }) }))
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }

  const actions = (m: RunMessage) => {
    if (!retry || (m.role === 'user' && retry.inputMode === 'manual')) return null
    return (
      <div className="bubble-actions">
        <button className="btn btn-sm" disabled={!!lockedReason} title={lockedReason ?? '只重試這一則'} onClick={() => retryMessage(m)}>
          ↻ 重試
        </button>
        {m.role === 'assistant' && (
          <button className="btn btn-sm" onClick={() => setProbe(m)} title="用修改後的工作區重新產生這一回合，和原本的回覆比較">
            🔍 迴歸試算
          </button>
        )}
      </div>
    )
  }

  const userBody = (m: RunMessage) =>
    retry?.inputMode === 'manual' ? (
      <UserEditor key={`${m.id}:${m.content}`} m={m} locked={lockedReason} onSave={(t) => retryMessage(m, t)} />
    ) : null

  return (
    <aside className="card run-view" aria-label={`分支 #${runId}`}>
      <div className="list-head">
        <h2>分支 #{runId}</h2>
        {retry && r && !busy && (
          <button className="btn btn-sm" onClick={retryBranch} title="本階段的所有回合重新產生">
            ↻ 重試整條分支
          </button>
        )}
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
            {retry && retry.cascade > 0 && ` · 後面接了 ${retry.cascade} 條分支（重試會連帶重跑）`}
          </p>
          {error && <p className="alert">{error}</p>}
          {retry && busy && (
            <div className="resume-bar">
              <span className="small">這條分支{r.status === 'queued' ? '排隊中' : '正在生成'}。卡住的話可以單獨中止，其他分支不受影響。</span>
              <button className="btn btn-sm danger" onClick={cancelThis}>
                ■ 中止這條
              </button>
            </div>
          )}
          {retry && incomplete && (
            <div className="resume-bar">
              <span className="small">這條分支沒有跑完（{r.status === 'error' ? '出錯' : '已中止'}）。</span>
              <button className="btn btn-sm btn-primary" onClick={resume}>
                ↻ 從中斷處續跑
              </button>
            </div>
          )}
          {r.scenario.history.length > 0 && (
            <details className="history-fold">
              <summary>先前的對話（開場、既有歷史與上層分支，共 {r.scenario.history.length} 則）</summary>
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
      {dialog && retry && (
        <RetryDialog experimentId={retry.exp.id} req={dialog} defaultLater={retry.laterUsers} onDone={done} onClose={() => setDialog(null)} />
      )}
      {probe && retry && r && <ProbeDialog exp={retry.exp} run={r} message={probe} stageSize={retry.stageSize} onClose={() => setProbe(null)} />}
    </aside>
  )
}

function UserEditor({ m, locked, onSave }: { m: RunMessage; locked: string | null; onSave: (text: string) => void }) {
  const [text, setText] = useState(m.content)
  const empty = !text.trim()
  return (
    <div className="user-editor">
      <textarea value={text} onChange={(e) => setText(e.target.value)} aria-label={`第 ${m.turn} 回合使用者發言`} disabled={!!locked} />
      <div className="bubble-actions">
        <button className="btn btn-sm btn-primary" disabled={!!locked || empty} title={locked ?? '改成這段輸入，重新產生之後的回覆'} onClick={() => onSave(text)}>
          儲存並重試
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
