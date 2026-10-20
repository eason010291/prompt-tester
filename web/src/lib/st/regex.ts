/*
 * SillyTavern 正則引擎（public/scripts/extensions/regex/engine.js 1.18.0）的前端移植。
 * 在瀏覽器執行，使用原生 JS RegExp，語意與 ST 相同。
 */

export interface RegexScript {
  id?: string
  scriptName?: string
  findRegex: string
  replaceString: string
  trimStrings?: string[]
  placement: number[]
  disabled?: boolean
  markdownOnly?: boolean
  promptOnly?: boolean
  runOnEdit?: boolean
  substituteRegex?: number | boolean
  minDepth?: number | null
  maxDepth?: number | null
}

export const PLACEMENT = { USER_INPUT: 1, AI_OUTPUT: 2, SLASH_COMMAND: 3, WORLD_INFO: 5, REASONING: 6 } as const

/** 巨集代入函式；第二個參數用於 substituteRegex=2 時跳脫巨集結果。 */
export type MacroFn = (text: string, escape?: (value: string) => string) => string

export interface Applied {
  name: string
  source: string
  count: number
}

/** ST utils.js regexFromString */
export function regexFromString(input: string): RegExp | undefined {
  try {
    const m = input.match(/(\/?)(.+)\1([a-z]*)/i)
    if (!m) return undefined
    if (m[3] && !/^(?!.*?(.).*?\1)[gmixXsuUAJ]+$/.test(m[3])) {
      return RegExp(input)
    }
    return new RegExp(m[2], m[3])
  } catch {
    return undefined
  }
}

/** ST engine.js sanitizeRegexMacro：substituteRegex=2 時跳脫巨集結果中的 regex 特殊字元 */
export function sanitizeRegexMacro(x: string): string {
  return x.replaceAll(/[\n\r\t\v\f\0.^$*+?{}[\]\\/|()]/gs, (s) => {
    switch (s) {
      case '\n':
        return '\\n'
      case '\r':
        return '\\r'
      case '\t':
        return '\\t'
      case '\v':
        return '\\v'
      case '\f':
        return '\\f'
      case '\0':
        return '\\0'
      default:
        return '\\' + s
    }
  })
}

function filterString(raw: string, trimStrings: string[] | undefined, macro: MacroFn): string {
  let out = raw
  for (const t of trimStrings ?? []) {
    out = out.replaceAll(macro(t), '')
  }
  return out
}

/** ST engine.js runRegexScript。回傳替換後字串與替換次數。 */
export function runRegexScript(script: RegexScript, raw: string, macro: MacroFn): [string, number] {
  if (!script || script.disabled || !script.findRegex || !raw) return [raw, 0]

  let pattern = script.findRegex
  switch (Number(script.substituteRegex ?? 0)) {
    case 1:
      pattern = macro(pattern)
      break
    case 2:
      pattern = macro(pattern, sanitizeRegexMacro)
      break
  }
  const find = regexFromString(pattern)
  if (!find) return [raw, 0]

  let count = 0
  const result = raw.replace(find, (...args: unknown[]) => {
    count++
    let match = args[0] as string | undefined
    const replaceString = (script.replaceString ?? '').replace(/{{match}}/gi, '$0')
    const withGroups = replaceString.replaceAll(/\$(\d+)|\$<([^>]+)>/g, (_m, num: string, groupName: string) => {
      if (num) {
        match = args[Number(num)] as string | undefined
      } else if (groupName) {
        const groups = args[args.length - 1]
        match =
          groups && typeof groups === 'object' ? ((groups as Record<string, string>)[groupName] as string) : undefined
      }
      if (!match) return ''
      return filterString(match, script.trimStrings, macro)
    })
    return macro(withGroups)
  })
  return [result, count]
}

export interface ScriptSet {
  source: string
  scripts: RegexScript[]
}

/**
 * ST engine.js getRegexedString。scriptSets 依序：全域 → 預設 → 角色。
 * isMarkdown：顯示用；isPrompt：送出用；兩者皆否：寫入儲存時。
 */
export function getRegexedString(
  raw: string,
  placement: number,
  opts: { isMarkdown?: boolean; isPrompt?: boolean; depth?: number },
  scriptSets: ScriptSet[],
  macro: MacroFn,
  applied?: Applied[],
): string {
  if (!raw) return raw
  let out = raw
  const { isMarkdown = false, isPrompt = false, depth } = opts
  for (const { source, scripts } of scriptSets) {
    for (const script of scripts) {
      const md = !!script.markdownOnly
      const pr = !!script.promptOnly
      if (!((md && isMarkdown) || (pr && isPrompt) || (!md && !pr && !isMarkdown && !isPrompt))) continue
      if (typeof depth === 'number') {
        const { minDepth, maxDepth } = script
        if (minDepth != null && !Number.isNaN(minDepth) && minDepth >= -1 && depth < minDepth) continue
        if (maxDepth != null && !Number.isNaN(maxDepth) && maxDepth >= 0 && depth > maxDepth) continue
      }
      if (!(script.placement ?? []).includes(placement)) continue
      const [next, count] = runRegexScript(script, out, macro)
      out = next
      if (applied && count) applied.push({ name: script.scriptName || script.id || '(未命名)', source, count })
    }
  }
  return out
}

/** 前端簡易巨集：{{char}}、{{user}} 與情境變數（不分大小寫）、{{// 註解}}、{{newline}}；其他保留原樣。 */
export function makeMacro(names: Record<string, string>): MacroFn {
  const lower = Object.fromEntries(Object.entries(names).map(([k, v]) => [k.toLowerCase(), v]))
  return (text, escape) => {
    if (!text || !text.includes('{{')) return text
    return text
      .replace(/{{\/\/[\s\S]*?}}/g, '')
      .replace(/{{\s*([A-Za-z_][\w-]*)\s*}}/g, (m, name: string) => {
        const key = name.toLowerCase()
        let value: string | undefined
        if (key === 'newline') value = '\n'
        else if (key === 'noop') value = ''
        else value = lower[key]
        if (value === undefined) return m
        return escape ? escape(value) : value
      })
  }
}
