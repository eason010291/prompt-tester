import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link } from 'react-router'
import { api, streamApi } from '../api'
import { formatCost, useResource } from '../hooks/useResource'
import type {
  Connection,
  Generation,
  GenerationSummary,
  ModelInfo,
  Preset,
  PreviewContext,
  Scenario,
  StreamEvent,
} from '../types'
import { collectScriptSets } from '../lib/st/scripts'
import { makeMacro, type Applied } from '../lib/st/regex'
import type { StTheme } from './StMessage'

// ST 渲染器（showdown、DOMPurify、CSS 解析）按需載入
const StMessage = lazy(() => import('./StMessage'))

interface Props {
  preset: Preset
  context: PreviewContext
  scenario: Scenario | null
}

const THEME_KEY = 'prompt-tester.st-theme'

function readTheme(): StTheme {
  try {
    return localStorage.getItem(THEME_KEY) === 'light' ? 'light' : 'st-dark'
  } catch {
    return 'st-dark'
  }
}

interface Live {
  output: string
  reasoning: string
  status: 'idle' | 'running' | 'done' | 'error' | 'cancelled'
  error: string | null
  servedBy: string | null
  generation: Generation | null
}

const IDLE: Live = { output: '', reasoning: '', status: 'idle', error: null, servedBy: null, generation: null }
const CONN_KEY = 'prompt-tester.connection'

// 預設參數中會送出的欄位 → OpenRouter supported_parameters 名稱
const SENT_PARAMS = ['temperature', 'top_p', 'frequency_penalty', 'presence_penalty', 'max_tokens'] as const

function readStoredConnection(): number | null {
  try {
    return Number(localStorage.getItem(CONN_KEY)) || null
  } catch {
    return null
  }
}

export default function TryRunPanel({ preset, context, scenario }: Props) {
  const connections = useResource<Connection[]>('/connections')
  const history = useResource<GenerationSummary[]>('/generations?limit=15')
  const [connId, setConnId] = useState<number | null>(readStoredConnection)
  const [live, setLive] = useState<Live>(IDLE)
  const [raw, setRaw] = useState(false)
  const [theme, setTheme] = useState<StTheme>(readTheme)
  const [useRegex, setUseRegex] = useState({ preset: true, card: true })
  const [externalMedia, setExternalMedia] = useState(false)
  const [autoFix, setAutoFix] = useState(false)
  const [applied, setApplied] = useState<Applied[]>([])
  const abort = useRef<AbortController | null>(null)

  useEffect(() => {
    try {
      localStorage.setItem(THEME_KEY, theme)
    } catch {
      // 只是不記住主題
    }
  }, [theme])

  // 開啟舊紀錄時，以當時的情境快照（角色名、角色正則）渲染
  const shownScenario = live.generation?.scenario ?? scenario
  const presetScripts = (preset.extra?.extensions as { regex_scripts?: unknown } | undefined)?.regex_scripts
  const cardScripts = shownScenario?.card?.regex_scripts
  const scriptSets = useMemo(
    () => collectScriptSets(presetScripts, cardScripts, useRegex),
    [presetScripts, cardScripts, useRegex],
  )
  const macro = useMemo(
    () => makeMacro({ ...(shownScenario?.vars ?? {}), char: shownScenario?.char ?? '', user: shownScenario?.user ?? '' }),
    [shownScenario],
  )
  const onApplied = useCallback((a: Applied[]) => setApplied(a), [])
  const presetCount = Array.isArray(presetScripts) ? presetScripts.length : 0
  const cardCount = Array.isArray(cardScripts) ? cardScripts.length : 0

  const conn = connections.data?.find((c) => c.id === connId) ?? connections.data?.[0] ?? null
  const models = useResource<ModelInfo[]>(conn ? `/providers/${conn.provider}/models` : null)
  const modelInfo = models.data?.find((m) => m.id === conn?.model)

  useEffect(() => {
    try {
      if (conn?.id) localStorage.setItem(CONN_KEY, String(conn.id))
    } catch {
      // localStorage 不可用時只是不記住選擇
    }
  }, [conn?.id])

  const ignored = useMemo(() => {
    if (!modelInfo || !conn) return []
    const sp = new Set(modelInfo.supported_parameters)
    const list: string[] = SENT_PARAMS.filter((p) => !sp.has(p))
    if (conn.send_seed && !sp.has('seed')) list.push('seed')
    if (conn.reasoning_effort && !sp.has('reasoning')) list.push('reasoning')
    return list
  }, [modelInfo, conn])

  const run = async () => {
    if (!conn?.id) return
    abort.current?.abort()
    const ctrl = new AbortController()
    abort.current = ctrl
    setLive({ ...IDLE, status: 'running' })
    try {
      await streamApi<StreamEvent>(
        '/generate',
        { preset, context, connection_id: conn.id },
        (ev) => {
          setLive((l) => {
            switch (ev.type) {
              case 'content':
                return { ...l, output: l.output + ev.text }
              case 'reasoning':
                return { ...l, reasoning: l.reasoning + ev.text }
              case 'meta':
                return { ...l, servedBy: ev.provider ?? l.servedBy }
              case 'error':
                return { ...l, status: 'error', error: ev.message }
              case 'done':
                return { ...l, status: 'done', generation: ev.generation, servedBy: ev.generation.served_by }
              default:
                return l
            }
          })
        },
        ctrl.signal,
      )
    } catch (e) {
      const aborted = e instanceof DOMException && e.name === 'AbortError'
      setLive((l) => ({ ...l, status: aborted ? 'cancelled' : 'error', error: aborted ? null : String(e instanceof Error ? e.message : e) }))
    } finally {
      void history.reload()
    }
  }

  const stop = () => abort.current?.abort()

  const open = async (id: number) => {
    const g = await api<Generation>(`/generations/${id}`)
    setLive({
      output: g.output,
      reasoning: g.reasoning,
      status: g.status === 'running' ? 'cancelled' : g.status,
      error: g.error,
      servedBy: g.served_by,
      generation: g,
    })
  }

  const remove = async (id: number) => {
    await api(`/generations/${id}`, { method: 'DELETE' })
    if (live.generation?.id === id) setLive(IDLE)
    void history.reload()
  }

  if (connections.data && connections.data.length === 0) {
    return (
      <section className="preview">
        <p>
          還沒有連線。請先到 <Link to="/settings">設定</Link> 填入 OpenRouter 金鑰並新增連線。
        </p>
      </section>
    )
  }

  const g = live.generation
  const usage = g?.usage
  const running = live.status === 'running'

  return (
    <section className="preview try-run" aria-label="試跑">
      <div className="preview-toolbar">
        <select
          value={conn?.id ?? ''}
          aria-label="連線"
          onChange={(e) => setConnId(Number(e.target.value))}
          disabled={running}
        >
          {connections.data?.map((c) => (
            <option key={c.id} value={c.id!}>
              {c.name}
            </option>
          ))}
        </select>
        {running ? (
          <button className="btn btn-primary" onClick={stop}>
            ■ 停止
          </button>
        ) : (
          <button className="btn btn-primary" onClick={run} disabled={!conn}>
            ▶ 生成
          </button>
        )}
        <div className="segmented" role="group" aria-label="顯示">
          <button aria-pressed={!raw} onClick={() => setRaw(false)}>
            渲染
          </button>
          <button aria-pressed={raw} onClick={() => setRaw(true)}>
            原文
          </button>
        </div>
      </div>

      <div className="render-opts">
        <label className="check">
          <input
            type="checkbox"
            checked={useRegex.preset}
            disabled={!presetCount}
            onChange={(e) => setUseRegex((u) => ({ ...u, preset: e.target.checked }))}
          />
          預設正則（{presetCount}）
        </label>
        <label className="check">
          <input
            type="checkbox"
            checked={useRegex.card}
            disabled={!cardCount}
            onChange={(e) => setUseRegex((u) => ({ ...u, card: e.target.checked }))}
          />
          角色正則（{cardCount}）
        </label>
        <label className="check" title="ST 預設禁止外部圖片 / 媒體與 CSS 中的外部網址">
          <input type="checkbox" checked={externalMedia} onChange={(e) => setExternalMedia(e.target.checked)} />
          允許外部媒體
        </label>
        <label
          className="check"
          title={'ST「自動修正 Markdown」：補上落單的 * 與 "、移除格式符號旁的空白（全新安裝預設關閉）'}
        >
          <input type="checkbox" checked={autoFix} onChange={(e) => setAutoFix(e.target.checked)} />
          自動修正 Markdown
        </label>
        <div className="segmented" role="group" aria-label="渲染主題">
          <button aria-pressed={theme === 'st-dark'} onClick={() => setTheme('st-dark')}>
            ST 深色
          </button>
          <button aria-pressed={theme === 'light'} onClick={() => setTheme('light')}>
            淺色
          </button>
        </div>
      </div>

      {conn && (
        <p className="muted small conn-line">
          {conn.model}
          {conn.routing.order.length > 0 && ` · 固定 ${conn.routing.order.join(' → ')}`}
          {!conn.routing.allow_fallbacks && '（不備援）'}
          {conn.reasoning_effort && ` · 推理 ${conn.reasoning_effort}`}
        </p>
      )}
      {ignored.length > 0 && (
        <p className="alert">此模型不支援：{ignored.join('、')}（會被忽略）</p>
      )}

      {context.user_input && (
        <div className="bubble bubble-user">
          <span className="bubble-role">user</span>
          {raw ? (
            <div className="bubble-text">{context.user_input}</div>
          ) : (
            <Suspense fallback={<p className="muted small">渲染中…</p>}>
              <StMessage
                text={context.user_input}
                theme={theme}
                isUser
                depth={1}
                charName={shownScenario?.char ?? ''}
                scriptSets={scriptSets}
                macro={macro}
                externalMedia={externalMedia}
                autoFixMarkdown={autoFix}
              />
            </Suspense>
          )}
        </div>
      )}

      {live.status !== 'idle' && (
        <div className={`bubble bubble-assistant ${running ? 'is-streaming' : ''}`}>
          <span className="bubble-role">
            assistant
            {live.servedBy && <em> · {live.servedBy}</em>}
          </span>
          {live.reasoning && (
            <details className="reasoning" open={running && !live.output}>
              <summary>推理內容（{live.reasoning.length.toLocaleString()} 字）</summary>
              <pre>{live.reasoning}</pre>
            </details>
          )}
          {raw || running ? (
            <pre className="msg-content">{live.output}</pre>
          ) : (
            <Suspense fallback={<p className="muted small">渲染中…</p>}>
              <StMessage
                text={live.output}
                theme={theme}
                depth={0}
                charName={shownScenario?.char ?? ''}
                scriptSets={scriptSets}
                macro={macro}
                externalMedia={externalMedia}
                autoFixMarkdown={autoFix}
                onApplied={onApplied}
              />
            </Suspense>
          )}
          {!raw && !running && live.output && (
            <p className="muted small applied">
              {applied.length
                ? '套用的正則：' + applied.map((a) => `${a.name}${a.count > 1 ? ` ×${a.count}` : ''}`).join('、')
                : '沒有正則命中'}
            </p>
          )}
          {running && !live.output && !live.reasoning && <p className="muted small">等待回應…</p>}
          {live.error && <p className="alert alert-error">{live.error}</p>}
          {live.status === 'cancelled' && <p className="muted small">已中止</p>}
        </div>
      )}

      {g?.finish_reason === 'length' && (
        <p className="alert">
          {live.output ? '回覆被 max_tokens 截斷' : '推理用完了 max_tokens，沒有產生正文'}
          {usage?.completion_tokens_details?.reasoning_tokens
            ? `（輸出 ${usage.completion_tokens?.toLocaleString()} tokens 中推理佔 ${usage.completion_tokens_details.reasoning_tokens.toLocaleString()}）`
            : ''}
          。推理型模型的推理 token 也計入上限：可提高「回覆上限」，或在連線設定調低推理強度。
        </p>
      )}

      {g && (
        <dl className="gen-stats">
          <div>
            <dt>模型</dt>
            <dd>{g.model}</dd>
          </div>
          <div>
            <dt>供應商</dt>
            <dd>{g.served_by ?? '—'}</dd>
          </div>
          <div>
            <dt>tokens</dt>
            <dd>
              {usage?.prompt_tokens?.toLocaleString() ?? '—'} → {usage?.completion_tokens?.toLocaleString() ?? '—'}
              {usage?.completion_tokens_details?.reasoning_tokens
                ? `（推理 ${usage.completion_tokens_details.reasoning_tokens.toLocaleString()}）`
                : ''}
              {usage?.prompt_tokens_details?.cached_tokens
                ? ` · 快取 ${usage.prompt_tokens_details.cached_tokens.toLocaleString()}`
                : ''}
            </dd>
          </div>
          <div>
            <dt>費用</dt>
            <dd>
              {formatCost(g.cost)}
              {g.cost_source === 'estimate' && '（估算）'}
            </dd>
          </div>
          <div>
            <dt>耗時</dt>
            <dd>
              首字 {g.first_token_ms != null ? `${(g.first_token_ms / 1000).toFixed(1)}s` : '—'} · 總計{' '}
              {g.latency_ms != null ? `${(g.latency_ms / 1000).toFixed(1)}s` : '—'}
            </dd>
          </div>
          <div>
            <dt>結束</dt>
            <dd>{g.finish_reason ?? '—'}</dd>
          </div>
        </dl>
      )}

      <details className="gen-history" open>
        <summary>最近的生成</summary>
        <ul>
          {history.data?.map((h) => (
            <li key={h.id} className={live.generation?.id === h.id ? 'active' : ''}>
              <button className="gen-item" onClick={() => open(h.id)}>
                <span className={`status-dot status-${h.status}`} aria-label={h.status} />
                <span className="gen-meta">
                  #{h.id} · {h.connection_name} · {h.scenario_name} · {formatCost(h.cost)}
                </span>
                <span className="gen-excerpt">{h.error ?? h.excerpt}</span>
              </button>
              <button className="icon-btn" aria-label={`刪除 #${h.id}`} onClick={() => remove(h.id)}>
                ✕
              </button>
            </li>
          ))}
          {history.data?.length === 0 && <li className="muted small">還沒有生成紀錄。</li>}
        </ul>
      </details>
    </section>
  )
}
