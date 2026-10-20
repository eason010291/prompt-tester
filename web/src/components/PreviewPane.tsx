import { useState } from 'react'
import type { BuildResponse } from '../types'

interface Props {
  result: BuildResponse | null
  error: string | null
  busy: boolean
  expandMacros: boolean
  onExpandMacros: (v: boolean) => void
  onSourceClick: (blockId: string) => void
}

const LONG_LINES = 10
const LONG_CHARS = 600
const isLong = (s: string) => s.length > LONG_CHARS || s.split('\n').length > LONG_LINES

const ORIGIN_LABEL = {
  history: '歷史',
  injection: '注入',
  separator: '分隔',
  placeholder: '補位',
  block: '',
} as const

export default function PreviewPane({ result, error, busy, expandMacros, onExpandMacros, onSourceClick }: Props) {
  const [view, setView] = useState<'messages' | 'json'>('messages')
  const [copied, setCopied] = useState(false)
  const [expanded, setExpanded] = useState<Set<number>>(new Set())
  const toggleExpanded = (i: number) =>
    setExpanded((s) => {
      const next = new Set(s)
      if (next.has(i)) next.delete(i)
      else next.add(i)
      return next
    })

  const json = result
    ? JSON.stringify(
        result.messages.map((m) => ({ role: m.role, content: m.content, ...(m.name ? { name: m.name } : {}) })),
        null,
        2,
      )
    : ''

  const copy = async () => {
    await navigator.clipboard.writeText(json)
    setCopied(true)
    window.setTimeout(() => setCopied(false), 1200)
  }

  const pct = result ? Math.min(100, (result.total_tokens / Math.max(1, result.budget)) * 100) : 0

  return (
    <section className="preview" aria-label="組裝預覽">
      <div className="preview-toolbar">
        <span className="toolbar-spacer" />
        <div className="segmented" role="group" aria-label="巨集">
          <button aria-pressed={expandMacros} onClick={() => onExpandMacros(true)}>
            展開巨集
          </button>
          <button aria-pressed={!expandMacros} onClick={() => onExpandMacros(false)}>
            原文
          </button>
        </div>
        <div className="segmented" role="group" aria-label="檢視">
          <button aria-pressed={view === 'messages'} onClick={() => setView('messages')}>
            訊息
          </button>
          <button aria-pressed={view === 'json'} onClick={() => setView('json')}>
            JSON
          </button>
        </div>
        <button className="btn btn-sm" onClick={copy} disabled={!result}>
          {copied ? '已複製' : '複製 JSON'}
        </button>
      </div>

      {result && (
        <div className="preview-stats">
          <span>
            {result.messages.length} 則 · <strong>{result.total_tokens.toLocaleString()}</strong> /{' '}
            {result.budget.toLocaleString()} tokens（估算）
          </span>
          <span>
            歷史 {result.history_included}/{result.history_total}
          </span>
          {busy && <span className="muted">更新中…</span>}
          <div className="meter" role="meter" aria-valuenow={Math.round(pct)} aria-valuemin={0} aria-valuemax={100}>
            <div style={{ width: `${pct}%` }} className={pct > 90 ? 'over' : ''} />
          </div>
        </div>
      )}

      {error && <p className="alert alert-error">組裝失敗：{error}</p>}
      {result?.warnings.map((w) => (
        <p key={w} className="alert">
          {w}
        </p>
      ))}

      {result && view === 'messages' && (
        <ol className="msg-list">
          {result.messages.map((m, i) => (
            <li key={i} className={`msg msg-${m.role}`}>
              <div className="msg-head">
                <span className={`role-badge role-${m.role}`}>{m.role}</span>
                <span className="msg-sources">
                  {m.sources.map((s, j) =>
                    s.block_id ? (
                      <button key={j} className="chip chip-source" onClick={() => onSourceClick(s.block_id!)}>
                        {ORIGIN_LABEL[s.origin] && <em>{ORIGIN_LABEL[s.origin]}</em>}
                        {s.name || '（未命名）'}
                      </button>
                    ) : (
                      <span key={j} className="chip">
                        {ORIGIN_LABEL[s.origin]} {s.name}
                      </span>
                    ),
                  )}
                </span>
                <span className="token-count">{m.tokens} tok</span>
              </div>
              <pre className={`msg-content ${isLong(m.content) && !expanded.has(i) ? 'clamped' : ''}`}>{m.content}</pre>
              {isLong(m.content) && (
                <button className="btn btn-sm msg-toggle" onClick={() => toggleExpanded(i)}>
                  {expanded.has(i) ? '收合' : `展開全文（${m.content.length.toLocaleString()} 字）`}
                </button>
              )}
            </li>
          ))}
        </ol>
      )}

      {result && view === 'json' && <pre className="json-view">{json}</pre>}

      {result && Object.keys(result.variables).length > 0 && (
        <details className="vars">
          <summary>變數（組裝後）</summary>
          <dl>
            {Object.entries(result.variables).map(([k, v]) => (
              <div key={k}>
                <dt>{k}</dt>
                <dd>{v}</dd>
              </div>
            ))}
          </dl>
        </details>
      )}
    </section>
  )
}
