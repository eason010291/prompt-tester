import { api } from '../api'

// 與後端 feedback.parse_scores 相同的規則：「名稱：數字/10」，佔位的「_/10」與超出範圍的值略過
const SCORE =
  /^[ \t]*(?:[-*•·][ \t]*)?(?<name>[^\n:：/]{1,30}?)[ \t]*[:：][ \t]*(?<score>\d+(?:\.\d+)?)[ \t]*\/[ \t]*10(?!\d)/gm

export function parseScores(text: string): Record<string, number> {
  const out: Record<string, number> = {}
  for (const m of text.matchAll(SCORE)) {
    const value = Number(m.groups!.score)
    const name = m.groups!.name.trim().replace(/^[【】[\]「」]+|[【】[\]「」]+$/g, '')
    if (name && value >= 0 && value <= 10) out[name] = value
  }
  return out
}

let template: Promise<string> | null = null

/** 評語佔位模板（設定頁可改）；同一頁面只讀一次 */
export function feedbackTemplate(): Promise<string> {
  template ??= api<{ text: string }>('/feedback-template')
    .then((t) => t.text)
    .catch(() => {
      template = null
      return ''
    })
  return template
}

export function setCachedTemplate(text: string) {
  template = Promise.resolve(text)
}
