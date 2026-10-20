// 與後端 src/prompt_tester/preset.py、routes/prompt.py 對應

export type Role = 'system' | 'user' | 'assistant'
export type BlockKind = 'content' | 'marker' | 'divider'
export type PostProcessing = '' | 'merge' | 'semi' | 'strict' | 'single'

export const POSITION_RELATIVE = 0
export const POSITION_IN_CHAT = 1

export type Marker =
  | 'chatHistory'
  | 'dialogueExamples'
  | 'worldInfoBefore'
  | 'worldInfoAfter'
  | 'charDescription'
  | 'charPersonality'
  | 'scenario'
  | 'personaDescription'

export const MARKER_LABEL: Record<Marker, string> = {
  chatHistory: '聊天歷史插入點',
  dialogueExamples: '範例對話',
  worldInfoBefore: '世界書（前）',
  worldInfoAfter: '世界書（後）',
  charDescription: '角色描述',
  charPersonality: '角色個性',
  scenario: '情境',
  personaDescription: 'Persona 描述',
}

export interface Block {
  id: string
  name: string
  kind: BlockKind
  marker: Marker | null
  role: Role
  content: string
  enabled: boolean
  injection_position: number
  injection_depth: number
  injection_order: number
  group: string
  collapsed: boolean
  /** ST 專屬欄位（system_prompt、injection_trigger…），匯出時寫回 */
  st: Record<string, unknown>
}

export interface Params {
  temperature: number
  top_p: number
  frequency_penalty: number
  presence_penalty: number
  max_tokens: number
  max_context: number
}

export interface Options {
  squash_system_messages: boolean
  post_processing: PostProcessing
  new_chat_prompt: string
  prompt_placeholder: string
}

export interface Preset {
  name: string
  blocks: Block[]
  params: Params
  options: Options
  extra: Record<string, unknown>
}

export interface HistoryMessage {
  role: Role
  content: string
  name?: string | null
}

export interface PreviewContext {
  scenario_id: number | null
  seed: number
  /** 本次的使用者輸入，附加在情境歷史最後；空字串表示不附加 */
  user_input: string
}

export interface Scenario {
  id: number | null
  name: string
  char: string
  user: string
  vars: Record<string, string>
  opening: string
  history: HistoryMessage[]
  user_turns: string[]
  notes: string
  updated_at: string | null
}

export type ReasoningEffort = '' | 'none' | 'minimal' | 'low' | 'medium' | 'high' | 'xhigh' | 'max'

export interface Routing {
  order: string[]
  allow_fallbacks: boolean
  only: string[]
  ignore: string[]
  quantizations: string[]
}

export interface Connection {
  id: number | null
  name: string
  provider: string
  model: string
  routing: Routing
  reasoning_effort: ReasoningEffort
  send_seed: boolean
  extra_body: Record<string, unknown>
}

export interface ProviderInfo {
  name: string
  base_url: string
  has_key: boolean
  key_source: '' | 'config' | 'env'
  key_hint: string
}

export interface Pricing {
  prompt: number | null
  completion: number | null
  cache_read: number | null
}

export interface ModelInfo {
  id: string
  name: string
  created: number | null
  context_length: number | null
  pricing: Pricing
  supported_parameters: string[]
}

export interface EndpointInfo {
  provider: string
  tag: string | null
  quantization: string | null
  context_length: number | null
  max_completion_tokens: number | null
  pricing: Pricing
  uptime: number | null
  status: number | null
  supported_parameters: string[]
}

export interface Usage {
  prompt_tokens?: number
  completion_tokens?: number
  total_tokens?: number
  cost?: number
  prompt_tokens_details?: { cached_tokens?: number }
  completion_tokens_details?: { reasoning_tokens?: number }
}

export interface GenerationSummary {
  id: number
  created_at: string
  status: 'running' | 'done' | 'error' | 'cancelled'
  preset_name: string
  scenario_id: number | null
  scenario_name: string | null
  connection_name: string | null
  model: string | null
  served_by: string | null
  cost: number | null
  cost_source: 'api' | 'generation' | 'estimate' | null
  latency_ms: number | null
  first_token_ms: number | null
  finish_reason: string | null
  error: string | null
  excerpt: string
}

export interface Generation extends Omit<GenerationSummary, 'excerpt' | 'scenario_name' | 'connection_name'> {
  output: string
  reasoning: string
  usage: Usage | null
  request: { messages: { role: Role; content: string }[]; [k: string]: unknown }
  scenario: Scenario
  connection: Connection
}

export type StreamEvent =
  | { type: 'start'; id: number; message_count: number; warnings: string[] }
  | { type: 'meta'; id: string | null; model: string | null; provider: string | null }
  | { type: 'content' | 'reasoning'; text: string }
  | { type: 'error'; message: string }
  | { type: 'done'; generation: Generation }

export interface Workspace {
  preset: Preset
  context: PreviewContext
  updated_at: string | null
}

export type Origin = 'block' | 'history' | 'injection' | 'separator' | 'placeholder'

export interface BuiltMessage {
  role: Role
  content: string
  name: string | null
  tokens: number
  sources: { origin: Origin; block_id: string | null; name: string }[]
}

export interface BuildResponse {
  messages: BuiltMessage[]
  total_tokens: number
  budget: number
  history_total: number
  history_included: number
  block_tokens: Record<string, number>
  variables: Record<string, string>
  warnings: string[]
}

export function newBlock(partial: Partial<Block> = {}): Block {
  return {
    id: crypto.randomUUID(),
    name: '新區塊',
    kind: 'content',
    marker: null,
    role: 'system',
    content: '',
    enabled: true,
    injection_position: POSITION_RELATIVE,
    injection_depth: 4,
    injection_order: 100,
    group: '',
    collapsed: false,
    st: {},
    ...partial,
  }
}

export interface ImportResponse {
  preset: Preset
  notes: string[]
}

export function newScenario(partial: Partial<Scenario> = {}): Scenario {
  return {
    id: null,
    name: '新情境',
    char: '角色',
    user: '使用者',
    vars: {},
    opening: '',
    history: [],
    user_turns: [''],
    notes: '',
    updated_at: null,
    ...partial,
  }
}

export function newConnection(partial: Partial<Connection> = {}): Connection {
  return {
    id: null,
    name: '新連線',
    provider: 'openrouter',
    model: '',
    routing: { order: [], allow_fallbacks: true, only: [], ignore: [], quantizations: [] },
    reasoning_effort: '',
    send_seed: true,
    extra_body: {},
    ...partial,
  }
}
