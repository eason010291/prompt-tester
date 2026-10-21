import { useEffect, useMemo, useState } from 'react'
import { api } from '../api'
import { formatPrice, useAutosave, useResource } from '../hooks/useResource'
import { parseScores, setCachedTemplate } from '../lib/feedback'
import {
  newConnection,
  type Connection,
  type EndpointInfo,
  type ModelInfo,
  type ProviderInfo,
  type ReasoningEffort,
  type UserSimSettings,
} from '../types'

const SAVE_LABEL = { saved: '已儲存', dirty: '未儲存', saving: '儲存中…', error: '儲存失敗' }

// 排除低精度量化（fp4 系列 / int4），其餘保留。設計文件 §8.3：迴歸比較需固定供應商與量化。
const NO_FP4 = ['int8', 'fp6', 'fp8', 'mxfp8', 'fp16', 'bf16', 'fp32', 'unknown']

const QUICK_ADD: { label: string; conn: Partial<Connection> }[] = [
  { label: 'GLM-5.3-Flash', conn: { name: 'GLM-5.3-Flash（Z.AI）', model: 'z-ai/glm-5.3-flash' } },
  { label: 'DeepSeek V4.1 Flash', conn: { name: 'DeepSeek V4.1 Flash', model: 'deepseek/deepseek-v4.1-flash-1017' } },
  { label: 'DeepSeek V4 Pro', conn: { name: 'DeepSeek V4 Pro', model: 'deepseek/deepseek-v4-pro' } },
  { label: 'Gemini 3.8 Flash', conn: { name: 'Gemini 3.8 Flash', model: 'google/gemini-3.8-flash' } },
]

export default function SettingsPage() {
  const providers = useResource<ProviderInfo[]>('/providers')
  const connections = useResource<Connection[]>('/connections')
  const [openId, setOpenId] = useState<number | null>(null)

  const add = async (partial: Partial<Connection> = {}, open = true) => {
    const created = await api<Connection>('/connections', {
      method: 'POST',
      body: JSON.stringify(newConnection({ routing: { ...newConnection().routing, quantizations: NO_FP4 }, ...partial })),
    })
    connections.setData((cur) => [...(cur ?? []), created])
    if (open) setOpenId(created.id)
    return created
  }

  return (
    <div className="settings-page">
      <header className="page-header">
        <h1>設定</h1>
        <p>金鑰只寫入本機 config.local.toml（或讀取環境變數），不進資料庫、不入版控。</p>
      </header>

      <section className="card">
        <h2>供應商</h2>
        <p className="muted small">API 端點與金鑰（例如 OpenRouter）。設定好之後，到下方「連線」選擇要用的模型。</p>
        {providers.error && <p className="alert alert-error">{providers.error}</p>}
        {providers.data?.map((p) => (
          <ProviderRow key={p.name} provider={p} onSaved={providers.reload} />
        ))}
      </section>

      <section className="card">
        <div className="list-head">
          <h2>連線</h2>
          <button className="btn btn-sm" onClick={() => add()}>
            ＋ 新增
          </button>
        </div>
        <p className="muted small">
          連線 = 供應商 + 模型 + 路由設定。提示詞試跑、多回合試跑與模擬使用者，選的都是這裡的連線。
        </p>
        <p className="muted small">
          快速新增（預設排除 fp4 量化）：
          {QUICK_ADD.map((q) => (
            <button key={q.label} className="btn btn-sm quick" onClick={() => add(q.conn)}>
              {q.label}
            </button>
          ))}
        </p>
        {connections.data?.map((c) => (
          <ConnectionCard
            key={c.id}
            initial={c}
            open={openId === c.id}
            onToggle={() => setOpenId(openId === c.id ? null : c.id)}
            onSaved={(saved) => connections.setData((cur) => cur?.map((x) => (x.id === saved.id ? saved : x)) ?? cur)}
            onDeleted={() => connections.setData((cur) => cur?.filter((x) => x.id !== c.id) ?? cur)}
            providers={providers.data ?? []}
          />
        ))}
        {connections.data?.length === 0 && <p className="muted">還沒有連線，用上方按鈕新增。</p>}
      </section>

      <UserSimCard connections={connections.data ?? []} onQuickAdd={() => add(QUICK_ADD[0].conn, false)} />
      <FeedbackTemplateCard />
    </div>
  )
}

function ProviderRow({ provider, onSaved }: { provider: ProviderInfo; onSaved: () => void }) {
  const [baseUrl, setBaseUrl] = useState(provider.base_url)
  const [key, setKey] = useState('')
  const [msg, setMsg] = useState<string | null>(null)

  const save = async (apiKey: string | null) => {
    try {
      await api(`/providers/${provider.name}`, {
        method: 'PUT',
        body: JSON.stringify({ base_url: baseUrl, api_key: apiKey }),
      })
      setKey('')
      setMsg('已儲存')
      onSaved()
    } catch (e) {
      setMsg(e instanceof Error ? e.message : String(e))
    }
  }

  const status = provider.has_key
    ? `已設定 ${provider.key_hint}（${provider.key_source === 'env' ? '環境變數' : 'config.local.toml'}）`
    : '尚未設定金鑰'

  return (
    <div className="provider-row">
      <strong>{provider.name}</strong>
      <span className={`badge ${provider.has_key ? '' : 'warn'}`}>
        <span className={`dot ${provider.has_key ? 'ok' : 'err'}`} />
        {status}
      </span>
      <div className="form-grid">
        <label>
          base_url
          <input value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)} />
        </label>
        <label>
          API key（留空表示不變更）
          <input
            type="password"
            autoComplete="off"
            value={key}
            placeholder={provider.has_key ? '輸入新金鑰以取代' : 'sk-or-…'}
            onChange={(e) => setKey(e.target.value)}
          />
        </label>
      </div>
      <div className="add-bar">
        <button className="btn btn-sm" onClick={() => save(key || null)}>
          儲存
        </button>
        {provider.key_source === 'config' && (
          <button className="btn btn-sm danger" onClick={() => window.confirm('清除 config.local.toml 中的金鑰？') && save('')}>
            清除金鑰
          </button>
        )}
        {msg && <span className="muted small">{msg}</span>}
      </div>
    </div>
  )
}

interface CardProps {
  initial: Connection
  open: boolean
  onToggle: () => void
  onSaved: (c: Connection) => void
  onDeleted: () => void
  providers: ProviderInfo[]
}

function ConnectionCard({ initial, open, onToggle, onSaved, onDeleted, providers }: CardProps) {
  const [c, setC] = useState(initial)
  const [extraText, setExtraText] = useState(JSON.stringify(initial.extra_body, null, 2))
  const [extraError, setExtraError] = useState<string | null>(null)
  const state = useAutosave(c, async (v) => {
    onSaved(await api<Connection>(`/connections/${v.id}`, { method: 'PUT', body: JSON.stringify(v) }))
  })

  const set = (patch: Partial<Connection>) => setC((x) => ({ ...x, ...patch }))
  const setRouting = (patch: Partial<Connection['routing']>) => setC((x) => ({ ...x, routing: { ...x.routing, ...patch } }))

  const remove = async () => {
    if (!window.confirm(`刪除連線「${c.name}」？`)) return
    await api(`/connections/${c.id}`, { method: 'DELETE' })
    onDeleted()
  }

  const r = c.routing
  const summary = [
    c.model || '（未選模型）',
    r.order.length ? `固定 ${r.order.join(' → ')}${r.allow_fallbacks ? '' : '（不備援）'}` : '自動路由',
    r.quantizations.length ? `量化 ${r.quantizations.length} 種` : '',
    c.reasoning_effort ? `推理 ${c.reasoning_effort}` : '',
  ].filter(Boolean)

  return (
    <article className={`conn-card ${open ? 'open' : ''}`}>
      <header className="conn-head" onClick={onToggle}>
        <span className="icon-btn" aria-hidden>
          {open ? '▾' : '▸'}
        </span>
        <strong>{c.name}</strong>
        <span className="muted small">{summary.join(' · ')}</span>
        <span className={`save-state save-${state}`}>{SAVE_LABEL[state]}</span>
      </header>

      {open && (
        <div className="conn-body">
          <div className="form-grid">
            <label>
              名稱
              <input value={c.name} onChange={(e) => set({ name: e.target.value })} />
            </label>
            <label>
              供應商
              <select value={c.provider} onChange={(e) => set({ provider: e.target.value })}>
                {providers.map((p) => (
                  <option key={p.name} value={p.name}>
                    {p.name}
                  </option>
                ))}
              </select>
            </label>
            <label>
              推理強度
              <select
                value={c.reasoning_effort}
                onChange={(e) => set({ reasoning_effort: e.target.value as ReasoningEffort })}
              >
                <option value="">不送（模型預設）</option>
                {['none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'].map((v) => (
                  <option key={v} value={v}>
                    {v}
                  </option>
                ))}
              </select>
            </label>
            <label className="check">
              <input type="checkbox" checked={c.send_seed} onChange={(e) => set({ send_seed: e.target.checked })} />
              送出 seed（供應商不一定遵守）
            </label>
          </div>

          <ModelPicker provider={c.provider} value={c.model} onChange={(model) => set({ model, routing: { ...r, order: [] } })} />

          {c.model && c.provider === 'openrouter' && (
            <EndpointTable model={c.model} routing={r} onChange={setRouting} />
          )}

          <label className="stack">
            額外 request 欄位（JSON，直接合併進 body；進階）
            <textarea
              className="mono"
              value={extraText}
              onChange={(e) => {
                setExtraText(e.target.value)
                try {
                  const v = e.target.value.trim() ? JSON.parse(e.target.value) : {}
                  if (typeof v !== 'object' || Array.isArray(v) || v === null) throw new Error('必須是物件')
                  setExtraError(null)
                  set({ extra_body: v })
                } catch (err) {
                  setExtraError(err instanceof Error ? err.message : String(err))
                }
              }}
            />
            {extraError && <span className="field-error">JSON 錯誤：{extraError}（不會儲存）</span>}
          </label>

          <div className="add-bar">
            <button className="btn btn-sm danger" onClick={remove}>
              刪除連線
            </button>
          </div>
        </div>
      )}
    </article>
  )
}

const FAMILIES = ['deepseek/', 'z-ai/', 'google/gemini']

function ModelPicker({ provider, value, onChange }: { provider: string; value: string; onChange: (id: string) => void }) {
  const { data, error, loading, reload } = useResource<ModelInfo[]>(`/providers/${provider}/models`)
  const [q, setQ] = useState('')
  const [family, setFamily] = useState<string>('')

  const shown = useMemo(() => {
    const words = q.toLowerCase().split(/\s+/).filter(Boolean)
    return (data ?? [])
      .filter((m) => !family || m.id.startsWith(family))
      .filter((m) => words.every((w) => m.id.toLowerCase().includes(w) || m.name.toLowerCase().includes(w)))
      .slice(0, 60)
  }, [data, q, family])

  const current = data?.find((m) => m.id === value)

  return (
    <div className="model-picker">
      <div className="picker-head">
        <span className="label">模型</span>
        <strong>{value || '（未選）'}</strong>
        {current && (
          <span className="muted small">
            列表價 in {formatPrice(current.pricing.prompt)} / out {formatPrice(current.pricing.completion)} 每百萬 tokens ·{' '}
            {current.context_length?.toLocaleString()} ctx
          </span>
        )}
      </div>
      <div className="picker-filter">
        <input placeholder="搜尋模型 id，例如 glm flash" value={q} onChange={(e) => setQ(e.target.value)} />
        <div className="segmented" role="group" aria-label="家族">
          {[['', '全部'], ['deepseek/', 'DeepSeek'], ['z-ai/', 'GLM'], ['google/gemini', 'Gemini']].map(([v, label]) => (
            <button key={v} aria-pressed={family === v} onClick={() => setFamily(v)}>
              {label}
            </button>
          ))}
        </div>
        <button className="btn btn-sm" onClick={() => reload()} disabled={loading} title="重新抓取模型與價格">
          {loading ? '讀取中…' : '重新整理'}
        </button>
      </div>
      {error && <p className="alert alert-error">{error}</p>}
      <div className="model-list" role="listbox" aria-label="模型列表">
        {shown.map((m) => (
          <button
            key={m.id}
            role="option"
            aria-selected={m.id === value}
            className={`model-row ${m.id === value ? 'active' : ''} ${FAMILIES.some((f) => m.id.startsWith(f)) ? 'fav' : ''}`}
            onClick={() => onChange(m.id)}
          >
            <span className="model-id">{m.id}</span>
            <span className="num">{formatPrice(m.pricing.prompt)}</span>
            <span className="num">{formatPrice(m.pricing.completion)}</span>
            <span className="num muted">{m.created ? new Date(m.created * 1000).toISOString().slice(0, 10) : ''}</span>
          </button>
        ))}
      </div>
    </div>
  )
}

function EndpointTable({
  model,
  routing,
  onChange,
}: {
  model: string
  routing: Connection['routing']
  onChange: (patch: Partial<Connection['routing']>) => void
}) {
  const { data, error, loading } = useResource<EndpointInfo[]>(`/providers/openrouter/endpoints?model=${encodeURIComponent(model)}`)
  const [onlyAllowed, setOnlyAllowed] = useState(false)

  const quants = useMemo(() => [...new Set((data ?? []).map((e) => e.quantization || 'unknown'))].sort(), [data])
  const allowed = (e: EndpointInfo) =>
    !routing.quantizations.length || routing.quantizations.includes(e.quantization || 'unknown')

  const pin = (name: string) => {
    const order = routing.order.includes(name) ? routing.order.filter((x) => x !== name) : [...routing.order, name]
    onChange({ order })
  }

  // 模型換了之後，清掉已不存在的固定供應商
  useEffect(() => {
    if (!data) return
    const names = new Set(data.map((e) => e.provider))
    const kept = routing.order.filter((n) => names.has(n))
    if (kept.length !== routing.order.length) onChange({ order: kept })
  }, [data, routing.order, onChange])

  return (
    <div className="endpoints">
      <div className="picker-head">
        <span className="label">供應商（OpenRouter）</span>
        <span className="muted small">
          {routing.order.length ? `依序使用：${routing.order.join(' → ')}` : '未固定：由 OpenRouter 依價格與可用度選擇'}
        </span>
      </div>
      <div className="form-row-inline">
        <label className="check">
          <input
            type="checkbox"
            checked={!routing.allow_fallbacks}
            onChange={(e) => onChange({ allow_fallbacks: !e.target.checked })}
          />
          不允許備援（迴歸比較建議勾選）
        </label>
        <span className="label">允許的量化：</span>
        {quants.map((q) => (
          <label key={q} className="check">
            <input
              type="checkbox"
              checked={!routing.quantizations.length || routing.quantizations.includes(q)}
              onChange={(e) => {
                const base = routing.quantizations.length ? routing.quantizations : quants
                const next = e.target.checked ? [...new Set([...base, q])] : base.filter((x) => x !== q)
                onChange({ quantizations: next.length === quants.length ? [] : next })
              }}
            />
            {q}
          </label>
        ))}
        <label className="check">
          <input type="checkbox" checked={onlyAllowed} onChange={(e) => setOnlyAllowed(e.target.checked)} />
          只顯示允許的
        </label>
      </div>
      {loading && <p className="muted small">讀取供應商…</p>}
      {error && <p className="alert alert-error">{error}</p>}
      {data && (
        <table className="ep-table">
          <thead>
            <tr>
              <th>固定</th>
              <th>供應商</th>
              <th>量化</th>
              <th className="num">輸入</th>
              <th className="num">輸出</th>
              <th className="num">快取讀</th>
              <th className="num">可用度</th>
            </tr>
          </thead>
          <tbody>
            {data
              .filter((e) => !onlyAllowed || allowed(e))
              .map((e, i) => {
                const idx = routing.order.indexOf(e.provider)
                return (
                  <tr key={`${e.provider}-${e.tag}-${i}`} className={allowed(e) ? '' : 'dim'}>
                    <td>
                      <button className={`pin ${idx >= 0 ? 'on' : ''}`} onClick={() => pin(e.provider)} title="加入 / 移出固定順序">
                        {idx >= 0 ? idx + 1 : '＋'}
                      </button>
                    </td>
                    <td>{e.provider}</td>
                    <td>{e.quantization || 'unknown'}</td>
                    <td className="num">{formatPrice(e.pricing.prompt)}</td>
                    <td className="num">{formatPrice(e.pricing.completion)}</td>
                    <td className="num">{formatPrice(e.pricing.cache_read)}</td>
                    <td className="num">{e.uptime != null ? `${Math.round(e.uptime)}%` : '—'}</td>
                  </tr>
                )
              })}
          </tbody>
        </table>
      )}
      <p className="muted small">價格單位：USD / 百萬 tokens。實際費用以 API 回傳為準。</p>
    </div>
  )
}

interface UserSimProps {
  connections: Connection[]
  /** 建立一個便宜的連線（GLM-5.3-Flash）並回傳 */
  onQuickAdd: () => Promise<Connection>
}

function UserSimCard({ connections, onQuickAdd }: UserSimProps) {
  const { data } = useResource<UserSimSettings>('/user-sim')
  return data ? <UserSimForm initial={data} connections={connections} onQuickAdd={onQuickAdd} /> : null
}

function UserSimForm({ initial, connections, onQuickAdd }: UserSimProps & { initial: UserSimSettings }) {
  const [s, setS] = useState(initial)
  const state = useAutosave(s, (v) => api('/user-sim', { method: 'PUT', body: JSON.stringify(v) }))
  const set = (patch: Partial<UserSimSettings>) => setS((x) => ({ ...x, ...patch }))

  const resetTemplate = async () => {
    if (!window.confirm('把模板還原成預設內容？')) return
    const { template: _omit, ...rest } = s
    setS(await api<UserSimSettings>('/user-sim', { method: 'PUT', body: JSON.stringify(rest) }))
  }

  return (
    <section className="card">
      <div className="list-head">
        <h2>模擬使用者</h2>
        <span className={`save-state save-${state}`}>{SAVE_LABEL[state]}</span>
      </div>
      <p className="muted small">
        多回合試跑時由 LLM 扮演 {'{{user}}'}。人設取自情境的 persona、目標取自情境的「使用者目標」。建議用便宜的模型（例如 GLM-5.3-Flash）。
      </p>
      {connections.length === 0 && (
        <p className="alert">
          還沒有任何連線，下拉選單是空的。可以到上方「連線」新增，或直接
          <button
            className="btn btn-sm quick"
            onClick={async () => {
              const created = await onQuickAdd()
              set({ connection_id: created.id })
            }}
          >
            建立 GLM-5.3-Flash 連線並使用
          </button>
        </p>
      )}
      <div className="form-grid">
        <label>
          連線
          <select value={s.connection_id ?? ''} onChange={(e) => set({ connection_id: Number(e.target.value) || null })}>
            <option value="">（未設定）</option>
            {connections.map((c) => (
              <option key={c.id} value={c.id!}>
                {c.name}（{c.model || '未選模型'}）
              </option>
            ))}
          </select>
        </label>
        <label>
          temperature
          <input type="number" step={0.05} min={0} max={2} value={s.temperature} onChange={(e) => set({ temperature: Number(e.target.value) })} />
        </label>
        <label>
          回覆上限（含推理）
          <input type="number" step={100} min={50} value={s.max_tokens} onChange={(e) => set({ max_tokens: Number(e.target.value) })} />
        </label>
        <label>
          參考最近幾則訊息
          <input type="number" min={1} max={40} value={s.context_messages} onChange={(e) => set({ context_messages: Number(e.target.value) })} />
        </label>
        <label>
          推理強度
          <select
            value={s.reasoning_effort ?? 'inherit'}
            onChange={(e) => set({ reasoning_effort: e.target.value === 'inherit' ? null : (e.target.value as ReasoningEffort) })}
          >
            <option value="inherit">沿用連線設定</option>
            <option value="">不送（模型預設）</option>
            {['none', 'minimal', 'low', 'medium', 'high'].map((v) => (
              <option key={v} value={v}>
                {v}
              </option>
            ))}
          </select>
        </label>
        <label className="check">
          <input type="checkbox" checked={s.strip_markup} onChange={(e) => set({ strip_markup: e.target.checked })} />
          剝除角色回覆的思考鏈、狀態欄 HTML
        </label>
      </div>
      <label className="stack">
        系統指示模板（可用 {'{{user}}'}、{'{{char}}'}、{'{{persona}}'}、{'{{user_goal}}'}）
        <textarea className="tall" value={s.template} onChange={(e) => set({ template: e.target.value })} />
      </label>
      <div className="add-bar">
        <button className="btn btn-sm" onClick={resetTemplate}>
          還原預設模板
        </button>
      </div>
    </section>
  )
}

function FeedbackTemplateCard() {
  const { data } = useResource<{ text: string }>('/feedback-template')
  return data ? <FeedbackTemplateForm initial={data.text} /> : null
}

function FeedbackTemplateForm({ initial }: { initial: string }) {
  const [text, setText] = useState(initial)
  const state = useAutosave(text, async (v) => {
    await api('/feedback-template', { method: 'PUT', body: JSON.stringify({ text: v }) })
    setCachedTemplate(v)
  })
  const names = Object.keys(parseScores(text.replace(/_\/10/g, '5/10')))

  const reset = async () => {
    if (!window.confirm('把評語模板還原成預設內容？')) return
    const t = await api<{ text: string }>('/feedback-template', { method: 'PUT', body: '{}' })
    setCachedTemplate(t.text)
    setText(t.text)
  }

  return (
    <section className="card">
      <div className="list-head">
        <h2>評語模板</h2>
        <span className={`save-state save-${state}`}>{SAVE_LABEL[state]}</span>
      </div>
      <p className="muted small">
        新增評語時預先填入的文字，只是佔位，評的時候可以任意增刪。任何「名稱：數字/10」的行都會被解析成分數（「_/10」視為未填）。
      </p>
      <label className="stack">
        模板內容
        <textarea className="tall" value={text} onChange={(e) => setText(e.target.value)} />
      </label>
      <div className="add-bar">
        <span className="muted small">{names.length ? `模板裡的評分屬性：${names.join('、')}` : '模板裡沒有評分行'}</span>
        <button className="btn btn-sm" onClick={reset}>
          還原預設模板
        </button>
      </div>
    </section>
  )
}
