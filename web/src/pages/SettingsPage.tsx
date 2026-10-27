import { useEffect, useMemo, useState } from 'react'
import { api } from '../api'
import { formatPrice, useAutosave, useResource } from '../hooks/useResource'
import { parseScores, setCachedTemplate } from '../lib/feedback'
import {
  newConnection,
  type Connection,
  type EndpointInfo,
  type AgentSettings,
  type JudgeSettings,
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
      <ModifierCard connections={connections.data ?? []} />
      <AgentAccessCard />
      <JudgeCard connections={connections.data ?? []} />
      <FeedbackTemplateCard />
      <RetryReasonsCard />
      <MetricsCard />
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

function JudgeCard({ connections }: { connections: Connection[] }) {
  const { data } = useResource<JudgeSettings>('/judge')
  return data ? <JudgeForm initial={data} connections={connections} /> : null
}

function JudgeForm({ initial, connections }: { initial: JudgeSettings; connections: Connection[] }) {
  const [s, setS] = useState(initial)
  const state = useAutosave(s, (v) => api('/judge', { method: 'PUT', body: JSON.stringify(v) }))
  const set = (patch: Partial<JudgeSettings>) => setS((x) => ({ ...x, ...patch }))

  return (
    <section className="card">
      <div className="list-head">
        <h2>評審（LLM 預選晉級）</h2>
        <span className={`save-state save-${state}`}>{SAVE_LABEL[state]}</span>
      </div>
      <p className="muted small">
        腳本階段設為「LLM 評審預選」時，階段結束後由評審依下方標準排序同一情境的分支並預先勾選，你確認後才晉級。評審會看到回覆原文（含格式）與你寫的評語。
      </p>
      <div className="form-grid">
        <label>
          連線
          <select value={s.connection_id ?? ''} onChange={(e) => set({ connection_id: Number(e.target.value) || null })}>
            <option value="">沿用實驗的角色連線</option>
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
          <input type="number" step={500} min={200} value={s.max_tokens} onChange={(e) => set({ max_tokens: Number(e.target.value) })} />
        </label>
        <label>
          每則回覆最多給評審看幾字
          <input type="number" step={500} min={200} value={s.max_chars} onChange={(e) => set({ max_chars: Number(e.target.value) })} />
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
      </div>
      <label className="stack">
        挑選標準
        <textarea className="tall" value={s.criteria} onChange={(e) => set({ criteria: e.target.value })} />
      </label>
    </section>
  )
}

function ModifierCard({ connections }: { connections: Connection[] }) {
  const { data, setData } = useResource<AgentSettings>('/agent/settings')
  return data ? <ModifierForm key={data.prompt_history.length} initial={data} connections={connections} onSaved={setData} /> : null
}

function ModifierForm({ initial, connections, onSaved }: { initial: AgentSettings; connections: Connection[]; onSaved: (s: AgentSettings) => void }) {
  const [s, setS] = useState(initial)
  const state = useAutosave(s, async (v) => onSaved(await api<AgentSettings>('/agent/settings', { method: 'PUT', body: JSON.stringify(v) })))
  const set = (patch: Partial<AgentSettings>) => setS((x) => ({ ...x, ...patch }))

  const restoreDefault = async () => {
    if (!window.confirm('把修改者的系統提示詞還原成預設內容？目前的版本會留在歷史中。')) return
    set({ system_prompt: (await api<{ text: string }>('/agent/default-prompt')).text })
  }

  return (
    <section className="card">
      <div className="list-head">
        <h2>修改者 LLM</h2>
        <span className={`save-state save-${state}`}>{SAVE_LABEL[state]}</span>
      </div>
      <p className="muted small">
        「依評語修改」時，由這個模型以工具呼叫讀評語、修改工作區的區塊並提交（作者記為 llm:模型名稱）。建議用推理較強的模型；呼叫次數少，品質比單價重要。
      </p>
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
          每次回覆上限（含推理）
          <input type="number" step={1000} min={500} value={s.max_tokens} onChange={(e) => set({ max_tokens: Number(e.target.value) })} />
        </label>
        <label>
          LLM 呼叫上限
          <input type="number" min={1} max={100} value={s.max_steps} onChange={(e) => set({ max_steps: Number(e.target.value) })} />
        </label>
        <label>
          工具呼叫上限
          <input type="number" min={1} max={300} value={s.max_tool_calls} onChange={(e) => set({ max_tool_calls: Number(e.target.value) })} />
        </label>
        <label>
          每輪花費上限（USD）
          <input type="number" step={0.1} min={0.01} value={s.budget_usd} onChange={(e) => set({ budget_usd: Number(e.target.value) })} />
        </label>
        <label className="check">
          <input type="checkbox" checked={s.allow_params} onChange={(e) => set({ allow_params: e.target.checked })} />
          允許修改取樣參數（temperature 等）
        </label>
        <label className="check">
          <input type="checkbox" checked={s.allow_run_experiment} onChange={(e) => set({ allow_run_experiment: e.target.checked })} />
          允許外部 agent 執行實驗（會產生 LLM 費用）
        </label>
        <label className="check">
          <input type="checkbox" checked={s.allow_fixtures} onChange={(e) => set({ allow_fixtures: e.target.checked })} />
          允許修改情境的測試條件（既有歷史、使用者輸入、人設、目標；改了會在版本紀錄標示，之後的實驗不能和之前直接比較）
        </label>
      </div>
      <label className="stack">
        系統提示詞（修改後舊版會保留，可還原）
        <textarea className="tall" value={s.system_prompt} onChange={(e) => set({ system_prompt: e.target.value })} />
      </label>
      <div className="add-bar">
        <button className="btn btn-sm" onClick={restoreDefault}>
          還原預設
        </button>
        {s.prompt_history.length > 0 && (
          <select
            value=""
            aria-label="還原舊版"
            onChange={(e) => {
              const h = s.prompt_history[Number(e.target.value)]
              if (h && window.confirm('還原成這個版本？目前的版本會留在歷史中。')) set({ system_prompt: h.text })
            }}
          >
            <option value="">還原舊版（{s.prompt_history.length}）…</option>
            {s.prompt_history.map((h, i) => (
              <option key={i} value={i}>
                {new Date(h.saved_at).toLocaleString('zh-TW', { dateStyle: 'short', timeStyle: 'short' })} · {h.text.slice(0, 30)}
              </option>
            ))}
          </select>
        )}
      </div>
    </section>
  )
}

function AgentAccessCard() {
  const base = `${window.location.protocol}//${window.location.host}`
  const tools = useResource<
    { name: string; summary: string; enabled: boolean; actions: { name: string; doc: string; builtin: boolean; enabled: boolean }[] }[]
  >('/agent/tools')
  return (
    <section className="card">
      <h2>外部 agent 接入（MCP / REST）</h2>
      <p className="muted small">
        用你自己的 agent（Claude Code、Claude Desktop、Cursor…）對話式修改預設與角色卡，與網頁內建的修改迴圈共用同一組工具與限制：一個分頁一支工具，用 <code>action</code> 指定動作、<code>path</code> 定位欄位；預設只能改工作區，提交到 main 時會自動改建 <code>agent/…</code> 分支；情境的修改存成版本，可 diff 與還原。只接受本機連線；要開放給區網請在 config.local.toml 設定 <code>[agent] token</code>。
      </p>
      <dl className="access-list">
        <dt>MCP（streamable HTTP）</dt>
        <dd>
          <code>{base}/mcp</code>
          <br />
          Claude Code：<code>claude mcp add --transport http prompt-tester {base}/mcp</code>
        </dd>
        <dt>MCP（stdio，轉發到執行中的伺服器）</dt>
        <dd>
          <code>uv run --directory &lt;專案資料夾&gt; python -m prompt_tester.mcp_stdio</code>（環境變數 <code>PROMPT_TESTER_URL={base}</code>）
        </dd>
        <dt>REST</dt>
        <dd>
          <code>POST {base}/api/agent/tools/&lt;工具名稱&gt;</code>（JSON 參數，含 <code>action</code>；可帶 <code>X-Agent-Name</code> 標頭作為作者名稱）· <a href="/docs#/agent%20tools" target="_blank" rel="noreferrer">OpenAPI 文件</a>
        </dd>
      </dl>
      {tools.data && (
        <details>
          <summary className="small">工具清單（{tools.data.length}）</summary>
          <ul className="tool-list">
            {tools.data.map((t) => (
              <li key={t.name}>
                <code>{t.name}</code> {t.summary}
                <ul>
                  {t.actions.map((a) => (
                    <li key={a.name} className={a.enabled ? '' : 'muted'}>
                      <code>{a.name}</code> {a.doc}
                      {!a.builtin && <em>（只給外部 agent）</em>}
                      {!a.enabled && <em>（未開啟）</em>}
                    </li>
                  ))}
                </ul>
              </li>
            ))}
          </ul>
        </details>
      )}
    </section>
  )
}

function RetryReasonsCard() {
  const { data } = useResource<{ reasons: string[] }>('/retry-reasons')
  return data ? <RetryReasonsForm initial={data.reasons.join('\n')} /> : null
}

function RetryReasonsForm({ initial }: { initial: string }) {
  const [text, setText] = useState(initial)
  const state = useAutosave(text, (v) => api('/retry-reasons', { method: 'PUT', body: JSON.stringify({ reasons: v.split('\n') }) }))
  return (
    <section className="card">
      <div className="list-head">
        <h2>重試原因</h2>
        <span className={`save-state save-${state}`}>{SAVE_LABEL[state]}</span>
      </div>
      <p className="muted small">重試時可勾選的原因標籤（一行一個），另外永遠有「其他」可自行輸入。實驗頁的重試統計會依模型列出各原因的次數。</p>
      <label className="stack">
        標籤
        <textarea className="tall" value={text} onChange={(e) => setText(e.target.value)} />
      </label>
    </section>
  )
}

function MetricsCard() {
  const { data } = useResource<{ id: string; name: string; pattern: string; scope: string; warn_at: number | null }[]>('/metrics')
  if (!data) return null
  return (
    <section className="card">
      <h2>自動指標</h2>
      <p className="muted small">
        每則角色回覆生成後自動計數，不用 LLM、不花錢；結果顯示在分支、階段、比較頁與迴歸試算，修改者讀評語時也看得到。目前是簡版，只有內建的一條，之後再開放自訂。
      </p>
      <table className="ep-table">
        <thead>
          <tr>
            <th>名稱</th>
            <th>規則（正則）</th>
            <th>範圍</th>
            <th>每則達幾次標示</th>
          </tr>
        </thead>
        <tbody>
          {data.map((m) => (
            <tr key={m.id}>
              <td>{m.name}</td>
              <td>
                <code>{m.pattern}</code>
              </td>
              <td>{m.scope === 'body' ? '只數正文（不含思考鏈、狀態欄等 HTML）' : '全文'}</td>
              <td>{m.warn_at ?? '—'}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  )
}
