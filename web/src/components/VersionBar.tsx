import { useEffect, useState } from 'react'
import { Link } from 'react-router'
import { api } from '../api'
import { useResource } from '../hooks/useResource'
import { shortId, type Commit, type HeadState, type Preset } from '../types'

interface Props {
  preset: Preset
  /** 工作區儲存狀態；變成 saved 時重新讀取是否有未提交的變更 */
  saveState: string
  /** 版本切換（checkout）後需要重新載入工作區 */
  onCommitted?: () => void
}

export default function VersionBar({ preset, saveState, onCommitted }: Props) {
  const { data: head, reload, setData } = useResource<HeadState>('/vcs/state')
  const [open, setOpen] = useState(false)
  const [message, setMessage] = useState('')
  const [branchName, setBranchName] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    if (saveState === 'saved') reload()
  }, [saveState, reload])

  const commit = async () => {
    setBusy(true)
    setError(null)
    try {
      await api<Commit>('/vcs/commit', {
        method: 'POST',
        body: JSON.stringify({ message, preset, branch: head?.branch ? null : branchName }),
      })
      setMessage('')
      setBranchName('')
      setOpen(false)
      setData(await api<HeadState>('/vcs/state'))
      onCommitted?.()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  if (!head) return null
  const dirty = head.dirty || saveState === 'dirty' || saveState === 'saving'
  const base = head.base_commit

  return (
    <div className="version-bar">
      <span className="branch-chip" title={head.branch ? '目前分支' : '不在任何分支上（detached）'}>
        ⑂ {head.branch ?? `detached @${shortId(base?.id)}`}
      </span>
      {base ? (
        <span className="muted small base-line" title={base.message}>
          基於 <code>{shortId(base.id)}</code> {base.message}
        </span>
      ) : (
        <span className="muted small">尚未提交</span>
      )}
      <span className={`badge ${dirty ? 'warn' : ''}`}>
        <span className={`dot ${dirty ? 'err' : 'ok'}`} />
        {dirty ? '有未提交的變更' : '與版本一致'}
      </span>
      <button className="btn btn-sm" disabled={!dirty} onClick={() => setOpen((v) => !v)}>
        提交…
      </button>
      <Link className="small" to="/versions">
        版本樹 →
      </Link>
      {open && (
        <div className="commit-form">
          {!head.branch && (
            <input
              placeholder="新分支名稱（目前不在分支上）"
              value={branchName}
              onChange={(e) => setBranchName(e.target.value)}
            />
          )}
          <input
            autoFocus
            placeholder="提交說明，例如：文風改用 succubus改、關閉喔齁素材"
            value={message}
            onChange={(e) => setMessage(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && message.trim() && void commit()}
          />
          <button className="btn btn-sm btn-primary" disabled={!message.trim() || busy} onClick={commit}>
            提交
          </button>
          {error && <span className="field-error">{error}</span>}
        </div>
      )}
    </div>
  )
}
