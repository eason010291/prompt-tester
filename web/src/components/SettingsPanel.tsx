import type { Options, Params, PostProcessing } from '../types'

interface Props {
  params: Params
  options: Options
  onParams: (p: Partial<Params>) => void
  onOptions: (o: Partial<Options>) => void
}

const POST: { value: PostProcessing; label: string }[] = [
  { value: '', label: '無' },
  { value: 'merge', label: 'merge：合併連續同角色' },
  { value: 'semi', label: 'semi：中段 system 轉 user' },
  { value: 'strict', label: 'strict：semi + 補位 user' },
  { value: 'single', label: 'single：全部併成一則 user' },
]

const NUMBERS: { key: keyof Params; label: string; step: number; min?: number; max?: number }[] = [
  { key: 'temperature', label: 'temperature', step: 0.05, min: 0, max: 2 },
  { key: 'top_p', label: 'top_p', step: 0.05, min: 0, max: 1 },
  { key: 'frequency_penalty', label: 'frequency_penalty', step: 0.05, min: -2, max: 2 },
  { key: 'presence_penalty', label: 'presence_penalty', step: 0.05, min: -2, max: 2 },
  { key: 'max_tokens', label: '回覆上限（max_tokens）', step: 100, min: 1 },
  { key: 'max_context', label: '情境上限（max_context）', step: 1000, min: 1 },
]

export default function SettingsPanel({ params, options, onParams, onOptions }: Props) {
  return (
    <details className="panel">
      <summary>參數與組裝選項</summary>
      <div className="form-grid">
        {NUMBERS.map((n) => (
          <label key={n.key}>
            {n.label}
            <input
              type="number"
              step={n.step}
              min={n.min}
              max={n.max}
              value={params[n.key]}
              onChange={(e) => onParams({ [n.key]: Number(e.target.value) })}
            />
          </label>
        ))}
        <label>
          後處理
          <select
            value={options.post_processing}
            onChange={(e) => onOptions({ post_processing: e.target.value as PostProcessing })}
          >
            {POST.map((p) => (
              <option key={p.value} value={p.value}>
                {p.label}
              </option>
            ))}
          </select>
        </label>
        <label className="check">
          <input
            type="checkbox"
            checked={options.squash_system_messages}
            onChange={(e) => onOptions({ squash_system_messages: e.target.checked })}
          />
          合併連續 system 訊息（squash）
        </label>
        <label className="span-2">
          聊天開頭分隔（new_chat_prompt，留空不加）
          <input value={options.new_chat_prompt} onChange={(e) => onOptions({ new_chat_prompt: e.target.value })} />
        </label>
        <label className="check">
          <input
            type="checkbox"
            checked={options.prefer_character_prompt}
            onChange={(e) => onOptions({ prefer_character_prompt: e.target.checked })}
          />
          角色卡 system prompt 覆寫 main
        </label>
        <label className="check">
          <input
            type="checkbox"
            checked={options.prefer_character_jailbreak}
            onChange={(e) => onOptions({ prefer_character_jailbreak: e.target.checked })}
          />
          角色卡 post-history 覆寫 jailbreak
        </label>
        <span className="span-2 label">世界書（預設值同 ST 全新安裝）</span>
        <label>
          掃描深度（最新幾則訊息）
          <input type="number" min={0} value={options.wi_scan_depth} onChange={(e) => onOptions({ wi_scan_depth: Number(e.target.value) })} />
        </label>
        <label>
          預算（情境上限的 %）
          <input type="number" min={1} max={100} value={options.wi_budget_percent} onChange={(e) => onOptions({ wi_budget_percent: Number(e.target.value) })} />
        </label>
        <label>
          預算上限 tokens（0 = 不限）
          <input type="number" min={0} value={options.wi_budget_cap} onChange={(e) => onOptions({ wi_budget_cap: Number(e.target.value) })} />
        </label>
        <label className="check">
          <input type="checkbox" checked={options.wi_recursive} onChange={(e) => onOptions({ wi_recursive: e.target.checked })} />
          遞迴掃描
        </label>
        <label className="check">
          <input type="checkbox" checked={options.wi_match_whole_words} onChange={(e) => onOptions({ wi_match_whole_words: e.target.checked })} />
          全字比對（英文）
        </label>
        <label className="check">
          <input type="checkbox" checked={options.wi_case_sensitive} onChange={(e) => onOptions({ wi_case_sensitive: e.target.checked })} />
          區分大小寫
        </label>
        <label className="check">
          <input type="checkbox" checked={options.wi_include_names} onChange={(e) => onOptions({ wi_include_names: e.target.checked })} />
          掃描時包含名字
        </label>
        <label>
          世界書格式（wi_format，{'{0}'} = 內容）
          <input value={options.wi_format} onChange={(e) => onOptions({ wi_format: e.target.value })} />
        </label>
        <label>
          範例對話分隔
          <input value={options.new_example_chat_prompt} onChange={(e) => onOptions({ new_example_chat_prompt: e.target.value })} />
        </label>
        {options.post_processing === 'strict' && (
          <label className="span-2">
            strict 補位文字
            <input
              value={options.prompt_placeholder}
              onChange={(e) => onOptions({ prompt_placeholder: e.target.value })}
            />
          </label>
        )}
      </div>
    </details>
  )
}
