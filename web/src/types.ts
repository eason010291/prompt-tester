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
  new_example_chat_prompt: string
  wi_format: string
  scenario_format: string
  personality_format: string
  prefer_character_prompt: boolean
  prefer_character_jailbreak: boolean
  wi_scan_depth: number
  wi_budget_percent: number
  wi_budget_cap: number
  wi_recursive: boolean
  wi_case_sensitive: boolean
  wi_match_whole_words: boolean
  wi_include_names: boolean
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

export interface LoreEntry {
  uid: number
  comment: string
  keys: string[]
  secondary_keys: string[]
  content: string
  constant: boolean
  selective: boolean
  selective_logic: number
  enabled: boolean
  order: number
  position: number
  depth: number
  role: number
  probability: number
  use_probability: boolean
  scan_depth: number | null
  case_sensitive: boolean | null
  match_whole_words: boolean | null
  exclude_recursion: boolean
  prevent_recursion: boolean
  delay_until_recursion: number
  group: string
  group_override: boolean
  group_weight: number
  use_group_scoring: boolean | null
  ignore_budget: boolean
  outlet_name: string
  triggers: string[]
  match_sources: string[]
}

export interface Lorebook {
  name: string
  entries: LoreEntry[]
  scan_depth: number | null
  token_budget: number | null
  recursive_scanning: boolean | null
}

export interface CharacterCard {
  spec: string
  name: string
  description: string
  personality: string
  scenario: string
  first_mes: string
  mes_example: string
  creator_notes: string
  system_prompt: string
  post_history_instructions: string
  alternate_greetings: string[]
  depth_prompt: { prompt: string; depth: number; role: Role }
  regex_scripts: Record<string, unknown>[]
  book: Lorebook | null
  raw: Record<string, unknown>
}

/** ST world_info_position */
export const WI_POSITION: Record<number, string> = {
  0: '角色前',
  1: '角色後',
  2: 'AN 上',
  3: 'AN 下',
  4: '@深度',
  5: '範例上',
  6: '範例下',
  7: 'outlet',
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
  persona: string
  user_goal: string
  card: CharacterCard | null
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
  branch: string | null
  base_commit: string | null
}

export type Origin = 'block' | 'history' | 'injection' | 'separator' | 'placeholder' | 'card' | 'worldinfo' | 'example'

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
  worldinfo: {
    budget: number
    used_tokens: number
    total_entries: number
    activated: { uid: number; comment: string; position: number; depth: number; order: number; reason: string; tokens: number }[]
  } | null
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
    persona: '',
    user_goal: '',
    card: null,
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

// ---- 版本樹（M3） ----

export interface Commit {
  id: string
  tree_id: string
  parents: string[]
  author: string
  message: string
  created_at: string
  meta: Record<string, unknown>
  generations?: number
  runs?: number
  /** 這個版本所有試跑 / 生成評語的分數平均 */
  score?: { overall: number; scored: number } | null
}

export interface Ref {
  name: string
  kind: 'branch' | 'tag'
  commit_id: string
  protected: boolean
  updated_at: string
}

export interface HeadState {
  branch: string | null
  base_commit: Commit | null
  dirty: boolean
  tree_id: string
}

export interface VcsLog {
  commits: Commit[]
  refs: Ref[]
  head: { branch: string | null; base_commit: string | null }
}

export type DiffStatus = 'added' | 'removed' | 'modified' | 'toggled' | 'moved' | 'unchanged'

export interface BlockDiff {
  id: string
  name: string
  status: DiffStatus[]
  changes: Record<string, [unknown, unknown]>
  old_index: number | null
  new_index: number | null
  old: Block | null
  new: Block | null
}

export interface PresetDiff {
  name: [string, string] | null
  params: Record<string, [unknown, unknown]>
  options: Record<string, [unknown, unknown]>
  extra_changed: boolean
  summary: Record<'added' | 'removed' | 'modified' | 'toggled' | 'moved', number>
  blocks: BlockDiff[]
}

export const shortId = (id: string | null | undefined) => (id ? id.slice(0, 8) : '')

export function authorLabel(author: string): string {
  if (author === 'human') return '人工'
  if (author.startsWith('llm:')) return `LLM · ${author.slice(4)}`
  if (author.startsWith('agent:')) return `外部 · ${author.slice(6)}`
  return author
}

// ---- 多回合試跑（LLM 扮演 {{user}}） ----

export interface UserSimSettings {
  connection_id: number | null
  template: string
  temperature: number
  max_tokens: number
  context_messages: number
  strip_markup: boolean
  reasoning_effort: ReasoningEffort | null
}

export type RunSource = 'scenario' | 'llm_user' | 'replay' | 'replay_fallback' | 'manual' | 'char'

export const SOURCE_LABEL: Record<RunSource, string> = {
  scenario: '情境',
  llm_user: 'LLM 扮演',
  replay: '回放',
  replay_fallback: 'LLM 補位',
  manual: '手動輸入',
  char: '角色',
}

export interface ArchivedMessage {
  id: number
  run_id: number
  idx: number
  turn: number
  role: 'user' | 'assistant'
  source: RunSource
  content: string
  cost: number | null
  model: string | null
  archived_at: string
  reason: string
}

export interface RunMessage {
  id: number
  run_id: number
  idx: number
  turn: number
  role: 'user' | 'assistant'
  source: RunSource
  content: string
  reasoning: string
  usage: Usage | null
  cost: number | null
  served_by: string | null
  model: string | null
  latency_ms: number | null
  finish_reason: string | null
  error: string | null
}

export interface RunChecks {
  build_warnings: string[]
  issues: { turn: number; role: 'user' | 'assistant'; message: string }[]
}

export interface RunSummary {
  id: number
  created_at: string
  status: 'queued' | 'running' | 'done' | 'error' | 'cancelled'
  mode: 'live' | 'replay'
  replay_of: number | null
  turns: number
  seed: number
  preset_name: string
  preset_hash: string
  commit_id: string | null
  scenario_id: number | null
  scenario_name: string | null
  cost: number | null
  checks: RunChecks | null
  error: string | null
  connection_name: string | null
  user_connection_name: string | null
  replies: number
  /** 評語分數的整體平均 */
  score: number | null
  /** 屬於金字塔實驗時的位置 */
  experiment_id: number | null
  stage: number | null
  parent_run_id: number | null
  temperature: number | null
  promoted: number
  /** 最後完成的全域回合 */
  last_turn: number | null
  /** 部分迴歸：對照的原分支 */
  origin_run_id?: number | null
}

export interface Run
  extends Omit<RunSummary, 'scenario_name' | 'connection_name' | 'user_connection_name' | 'replies' | 'score' | 'last_turn'> {
  scenario: Scenario
  messages: RunMessage[]
}

export type RunEvent =
  | { type: 'start'; run_id: number; turns: number; history: HistoryMessage[] }
  | { type: 'message_start'; turn: number; role: 'user' | 'assistant' }
  | { type: 'content' | 'reasoning'; text: string }
  | { type: 'meta'; provider: string | null }
  | { type: 'message_done'; message: RunMessage }
  | { type: 'done'; run: Run }

// ---- 評語與比較（M5） ----

export type FeedbackTarget = 'run' | 'run_message' | 'generation'

export interface Feedback {
  id: number
  target_type: FeedbackTarget
  target_id: number
  run_id: number | null
  text: string
  scores: Record<string, number>
  created_at: string
  updated_at: string
}

export interface ScoreSummary {
  attributes: Record<string, number>
  counts: Record<string, number>
  overall: number | null
  feedback_count: number
}

export type ComparedRun = Run & { connection: Connection; preset: Preset | null; feedback: Feedback[]; scores: ScoreSummary }

export interface RunCompare {
  a: ComparedRun
  b: ComparedRun
  same_preset: boolean
  diff: PresetDiff | null
}

export const formatScore = (v: number | null | undefined) => (v == null ? '—' : Number.isInteger(v) ? String(v) : v.toFixed(1))

// ---- 腳本與金字塔實驗（M4） ----

export type PromoteMode = 'manual' | 'llm_assist' | 'top_k' | 'all' | 'restart'
export type UserSource = 'scenario_first' | 'scenario_all' | 'llm'

export interface Stage {
  name: string
  /** 累計回合數：本階段結束時對話的總回合數 */
  turns: number
  /** 每個起點長出幾條分支 */
  seeds: number
  temperatures: number[]
  promote: { mode: PromoteMode; k: number }
}

export interface ScriptData {
  stages: Stage[]
  user_source: UserSource
  concurrency: number
}

export interface Script {
  id: number
  name: string
  parent_id: number | null
  overrides: Record<string, unknown>
  notes: string
  updated_at: string | null
  resolved: ScriptData
  warnings: string[]
  error: string | null
}

export interface JudgeSettings {
  connection_id: number | null
  criteria: string
  temperature: number
  max_tokens: number
  reasoning_effort: ReasoningEffort | null
  max_chars: number
}

export type ExperimentStatus = 'running' | 'waiting' | 'done' | 'cancelled'

export interface ExperimentSummary {
  id: number
  created_at: string
  name: string
  status: ExperimentStatus
  current_stage: number
  stage_count: number
  preset_name: string
  commit_id: string | null
  script_name: string
  rerun_of: number | null
  runs: number
  cost: number
  score: number | null
}

export interface ExpRun extends RunSummary {
  experiment_id: number
  stage: number
  excerpt: string
  counterpart: number | null
}

export interface JudgeResult {
  picks: Record<string, { pick: boolean; rank: number; reason: string }>
  criteria?: string
  model?: string
  cost?: number
  error: string | null
  at?: string
}

export interface ExperimentDetail {
  id: number
  created_at: string
  name: string
  status: ExperimentStatus
  current_stage: number
  preset_name: string
  preset_hash: string
  commit_id: string | null
  script_id: number | null
  script_name: string
  script_snapshot: ScriptData
  script_hash: string
  scenario_ids: number[]
  connection_id: number
  rerun_of: number | null
  state: { judge?: Record<string, JudgeResult>; judge_cost?: number; partial_from?: number }
  error: string | null
  runs: ExpRun[]
  cost: number
  scenarios: Record<string, string>
  origin_promoted?: number[]
}

export const PROMOTE_LABEL: Record<PromoteMode, string> = {
  manual: '人工挑選',
  llm_assist: 'LLM 評審預選',
  top_k: '分數前 K',
  all: '全部晉級',
  restart: '從開場重跑',
}

export const USER_SOURCE_LABEL: Record<UserSource, string> = {
  scenario_first: '情境第一句，之後 LLM 扮演',
  scenario_all: '情境逐回合輸入，不足由 LLM 補',
  llm: '全部由 LLM 扮演',
}

// ---- LLM 修改（M6） ----

export interface AgentSettings {
  connection_id: number | null
  system_prompt: string
  prompt_history: { text: string; saved_at: string }[]
  temperature: number
  max_tokens: number
  max_steps: number
  max_tool_calls: number
  budget_usd: number
  allow_params: boolean
  allow_run_experiment: boolean
}

export interface AgentCall {
  name: string
  args: unknown
  ok: boolean
  result: string
}

export interface AgentStep {
  step: number
  text: string
  reasoning: string
  calls: AgentCall[]
  cost: number
  finish_reason: string | null
  tokens: [number | null, number | null]
}

export interface AgentRound {
  id: number
  created_at: string
  status: 'running' | 'done' | 'uncommitted' | 'error' | 'cancelled'
  author: string
  connection: Connection
  instructions: string
  experiment_id: number | null
  auto_rerun: boolean
  base_commit: string | null
  branch: string | null
  result_commit: string | null
  rerun_experiment_id: number | null
  steps?: AgentStep[]
  step_count?: number
  final_message: string | null
  tool_calls: number
  cost: number
  error: string | null
}

// ---- 分層重試與迴歸試算 ----

export type RetryKind = 'message' | 'branch' | 'resume'

export interface RetryResult {
  started: number[]
  cascade: number[]
  skipped: { run_id: number; reason: string }[]
}

export interface RetryStats {
  models: {
    model: string
    replies: number
    manual: number
    error: number
    cascade: number
    manual_rate: number | null
    tags: Record<string, number>
  }[]
  providers: { provider: string; calls: number; errors: number; error_rate: number | null }[]
}

export interface Probe {
  id: number
  created_at: string
  run_id: number
  turn: number
  preset_hash: string
  commit_id: string | null
  content: string
  reasoning: string
  cost: number | null
  model: string | null
  served_by: string | null
  finish_reason: string | null
  error: string | null
}

export interface WorkspaceDiff {
  same: boolean
  diff: PresetDiff
  experiment_commit: string | null
  preset_name: string
}
