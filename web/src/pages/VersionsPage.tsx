import { useMemo, useState } from 'react'
import { useNavigate } from 'react-router'
import { api } from '../api'
import DiffView from '../components/DiffView'
import ModifyPanel from '../components/ModifyPanel'
import { scoreLevel } from '../components/FeedbackBox'
import { useResource } from '../hooks/useResource'
import { useWorkspace } from '../hooks/useWorkspace'
import { layoutGraph, type GraphRow } from '../lib/graph'
import {
  authorLabel,
  formatScore,
  shortId,
  type BlockDiff,
  type Commit,
  type HeadState,
  type PresetDiff,
  type Ref,
  type VcsLog,
} from '../types'

const ROW_H = 44
const LANE_W = 16
const COLORS = ['#cc785c', '#5db8a6', '#e8a55a', '#8b7fd6', '#d16b8f', '#6aa0d8']

interface Compare {
  a: string
  b: string
  labels: [string, string]
  /** 左邊是 commit、右邊是工作區時可以套用區塊 */
  cherryPick: boolean
}

function timeAgo(iso: string): string {
  const s = (Date.now() - new Date(iso).getTime()) / 1000
  if (s < 60) return '剛剛'
  if (s < 3600) return `${Math.floor(s / 60)} 分鐘前`
  if (s < 86400) return `${Math.floor(s / 3600)} 小時前`
  return new Date(iso).toLocaleString('zh-TW', { dateStyle: 'short', timeStyle: 'short' })
}

export default function VersionsPage() {
  const navigate = useNavigate()
  const { ws, setPreset, saveState } = useWorkspace()
  const log = useResource<VcsLog>('/vcs/log')
  const head = useResource<HeadState>(`/vcs/state?v=${encodeURIComponent(ws?.updated_at ?? '')}`)
  const [selected, setSelected] = useState<string | null>(null)
  const [compare, setCompare] = useState<Compare | null>(null)
  const [msg, setMsg] = useState<string | null>(null)
  const [modify, setModify] = useState(false)

  const commits = useMemo(() => log.data?.commits ?? [], [log.data])
  const rows = useMemo(() => layoutGraph(commits), [commits])
  const refsByCommit = useMemo(() => {
    const m = new Map<string, Ref[]>()
    for (const r of log.data?.refs ?? []) m.set(r.commit_id, [...(m.get(r.commit_id) ?? []), r])
    return m
  }, [log.data])

  const base = head.data?.base_commit?.id ?? null
  const current = commits.find((c) => c.id === (selected ?? base)) ?? commits[0] ?? null

  const defaultCompare: Compare | null = current
    ? {
        a: current.parents[0] ?? 'EMPTY',
        b: current.id,
        labels: [current.parents[0] ? shortId(current.parents[0]) : '（空）', shortId(current.id)],
        cherryPick: false,
      }
    : null
  const cmp = compare ?? defaultCompare
  const diffPath = cmp
    ? `/vcs/diff?a=${encodeURIComponent(cmp.a)}&b=${encodeURIComponent(cmp.b)}&v=${encodeURIComponent(ws?.updated_at ?? '')}`
    : null
  const diff = useResource<PresetDiff>(diffPath)

  const select = (c: Commit, shift: boolean) => {
    if (shift && current && current.id !== c.id) {
      const [older, newer] = commits.indexOf(c) > commits.indexOf(current) ? [c, current] : [current, c]
      setCompare({ a: older.id, b: newer.id, labels: [shortId(older.id), shortId(newer.id)], cherryPick: false })
      return
    }
    setSelected(c.id)
    setCompare(null)
  }

  const run = async (fn: () => Promise<unknown>, okMsg: string) => {
    try {
      await fn()
      setMsg(okMsg)
      log.reload()
      head.reload()
    } catch (e) {
      setMsg(e instanceof Error ? e.message : String(e))
    }
  }

  const checkout = async (target: string) => {
    const dirty = head.data?.dirty && head.data.base_commit
    if (dirty && !window.confirm('工作區有未提交的變更，切換會捨棄這些變更。確定要切換？')) return
    try {
      await api('/vcs/checkout', { method: 'POST', body: JSON.stringify({ target, force: !!dirty }) })
      navigate('/prompt')
    } catch (e) {
      setMsg(e instanceof Error ? e.message : String(e))
    }
  }

  const createRef = (kind: 'branch' | 'tag', commitId: string) => {
    const name = window.prompt(kind === 'branch' ? '新分支名稱' : '標籤名稱（例如 聿V1.2.4-1020）')
    if (!name) return
    const checkoutNow = kind === 'branch' && window.confirm(`建立分支「${name}」後切換過去？`)
    void run(
      () =>
        api('/vcs/refs', { method: 'POST', body: JSON.stringify({ name, kind, commit_id: commitId, checkout: checkoutNow }) }),
      `已建立${kind === 'branch' ? '分支' : '標籤'}「${name}」`,
    ).then(() => checkoutNow && navigate('/prompt'))
  }

  const deleteRef = (r: Ref) => {
    if (!window.confirm(`刪除${r.kind === 'branch' ? '分支' : '標籤'}「${r.name}」？commit 本身會保留。`)) return
    void run(() => api(`/vcs/refs/${encodeURIComponent(r.name)}`, { method: 'DELETE' }), `已刪除「${r.name}」`)
  }

  const applyOld = (entry: BlockDiff) => {
    const old = entry.old
    if (!old) return
    setPreset((p) => {
      const blocks = [...p.blocks]
      const idx = blocks.findIndex((b) => b.id === old.id)
      if (idx >= 0) blocks[idx] = { ...old, collapsed: blocks[idx].collapsed }
      else blocks.splice(Math.min(entry.old_index ?? blocks.length, blocks.length), 0, old)
      return { ...p, blocks }
    })
    setMsg(`已把「${old.name}」套用到工作區（自動儲存）`)
  }

  if (log.error) return <p className="alert alert-error">{log.error}</p>

  return (
    <div className="versions-page">
      <header className="page-header row">
        <h1>版本樹</h1>
        {head.data && (
          <span className="muted small">
            ⑂ {head.data.branch ?? 'detached'} · 基於 <code>{shortId(base) || '（尚未提交）'}</code> ·{' '}
            {head.data.dirty || saveState === 'dirty' ? '有未提交的變更' : '與版本一致'}
          </span>
        )}
        <button className="btn btn-sm" onClick={() => setModify((v) => !v)} title="讓 LLM 依你的要求修改工作區並提交到新分支">
          請 LLM 修改…
        </button>
        {base && (
          <button
            className="btn btn-sm"
            onClick={() => setCompare({ a: base, b: 'WORKING', labels: [shortId(base), '工作區'], cherryPick: true })}
          >
            比較基準與工作區
          </button>
        )}
      </header>
      {modify && <ModifyPanel experimentId={null} onClose={() => (setModify(false), log.reload(), head.reload())} />}
      {msg && (
        <p className="alert" role="status">
          {msg}
        </p>
      )}

      {commits.length === 0 ? (
        <p className="muted">還沒有任何提交。到提示詞頁上方按「提交…」建立第一個版本。</p>
      ) : (
        <div className="versions-grid">
          <ol className="commit-list" aria-label="提交紀錄">
            {commits.map((c, i) => (
              <li
                key={c.id}
                className={`commit-row ${c.id === current?.id ? 'active' : ''} ${c.id === base ? 'is-base' : ''}`}
                onClick={(e) => select(c, e.shiftKey)}
                title="點擊查看；Shift + 點擊與目前選取的版本比較"
              >
                <GraphCell row={rows[i]} />
                <div className="commit-main">
                  <div className="commit-title">
                    {c.message}
                    {(refsByCommit.get(c.id) ?? []).map((r) => (
                      <span key={r.name} className={`chip ref-${r.kind}`}>
                        {r.kind === 'branch' ? '⑂' : '🏷'} {r.name}
                      </span>
                    ))}
                    {c.id === base && <span className="chip chip-marker">工作區基準</span>}
                  </div>
                  <div className="muted small">
                    <code>{shortId(c.id)}</code> · {authorLabel(c.author)} · {timeAgo(c.created_at)}
                    {c.generations ? ` · ${c.generations} 次生成` : ''}
                    {c.runs ? ` · ${c.runs} 次試跑` : ''}
                    {c.score && (
                      <span className="chip score-chip" data-level={scoreLevel(c.score.overall)} title={`${c.score.scored} 個分數的平均`}>
                        ★ {formatScore(c.score.overall)}
                      </span>
                    )}
                  </div>
                </div>
              </li>
            ))}
          </ol>

          <section className="commit-detail">
            {current && !compare && (
              <div className="card">
                <h2>{current.message}</h2>
                <p className="muted small">
                  <code>{current.id}</code>
                  <br />
                  {authorLabel(current.author)} · {new Date(current.created_at).toLocaleString('zh-TW')}
                  {current.parents.length > 0 && (
                    <>
                      {' '}
                      · 父版本 <code>{shortId(current.parents[0])}</code>
                    </>
                  )}
                </p>
                <div className="add-bar">
                  {(refsByCommit.get(current.id) ?? [])
                    .filter((r) => r.kind === 'branch' && r.name !== head.data?.branch)
                    .map((r) => (
                      <button key={r.name} className="btn btn-sm btn-primary" onClick={() => checkout(r.name)}>
                        切換到分支 {r.name}
                      </button>
                    ))}
                  {current.id !== base && (
                    <button className="btn btn-sm" onClick={() => checkout(current.id)} title="切換到這個版本（不在任何分支上）">
                      切換到此版本
                    </button>
                  )}
                  <button className="btn btn-sm" onClick={() => createRef('branch', current.id)}>
                    從這裡建立分支
                  </button>
                  <button className="btn btn-sm" onClick={() => createRef('tag', current.id)}>
                    加標籤
                  </button>
                  <button
                    className="btn btn-sm"
                    onClick={() =>
                      setCompare({ a: current.id, b: 'WORKING', labels: [shortId(current.id), '工作區'], cherryPick: true })
                    }
                  >
                    與工作區比較
                  </button>
                </div>
                {(refsByCommit.get(current.id) ?? []).length > 0 && (
                  <div className="ref-list">
                    {(refsByCommit.get(current.id) ?? []).map((r) => (
                      <span key={r.name} className="chip">
                        {r.kind === 'branch' ? '⑂' : '🏷'} {r.name}
                        {r.protected ? '（受保護）' : (
                          <button className="icon-btn" aria-label={`刪除 ${r.name}`} onClick={() => deleteRef(r)}>
                            ✕
                          </button>
                        )}
                      </span>
                    ))}
                  </div>
                )}
              </div>
            )}

            {compare && (
              <div className="add-bar">
                <button className="btn btn-sm" onClick={() => setCompare(null)}>
                  ← 回到版本詳情
                </button>
              </div>
            )}

            {diff.error && <p className="alert alert-error">{diff.error}</p>}
            {diff.data && cmp && (
              <DiffView diff={diff.data} labels={cmp.labels} onApplyOld={cmp.cherryPick ? applyOld : undefined} />
            )}
          </section>
        </div>
      )}
    </div>
  )
}

function GraphCell({ row }: { row: GraphRow }) {
  const w = Math.max(1, row.width) * LANE_W
  const mid = ROW_H / 2
  const x = (i: number) => i * LANE_W + LANE_W / 2
  const color = (i: number) => COLORS[i % COLORS.length]
  return (
    <svg className="graph-cell" width={w} height={ROW_H} aria-hidden>
      {row.top.map((to, i) => to !== null && <line key={`t${i}`} x1={x(i)} y1={0} x2={x(to)} y2={mid} stroke={color(i)} strokeWidth={2} />)}
      {row.bottom.map((from, i) => from !== null && <line key={`b${i}`} x1={x(from)} y1={mid} x2={x(i)} y2={ROW_H} stroke={color(i)} strokeWidth={2} />)}
      <circle cx={x(row.col)} cy={mid} r={5} fill={color(row.col)} stroke="var(--bg-card)" strokeWidth={2} />
    </svg>
  )
}
