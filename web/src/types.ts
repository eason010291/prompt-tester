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
  char: string
  user: string
  seed: number
  history: HistoryMessage[]
}

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
