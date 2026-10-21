/*
 * SillyTavern 訊息顯示格式化（public/script.js messageFormatting 1.18.0）的前端移植：
 * markdownOnly 正則 → fixMarkdown → 引號包 <q> → showdown → 去除「角色名:」→
 * <style> 編碼 → DOMPurify（class 加 custom- 前綴）→ <style> 解碼並把選擇器範圍化到 .mes_text。
 * 結果放進 sandbox iframe 顯示（StMessage），預設 CSS 不會影響工具介面。
 *
 * showdown 有未修正的 XSS / ReDoS 公告；輸出一律經 DOMPurify，並關閉 metadata、表頭 id。
 */
import css from '@adobe/css-tools'
import DOMPurify from 'dompurify'
import showdown from 'showdown'
import { getRegexedString, PLACEMENT, type Applied, type MacroFn, type ScriptSet } from './regex'

// ST showdown-underscore.js：單底線包住的文字轉 <em>（code / style 內不處理）
const underscoreExt: showdown.ShowdownExtension = {
  type: 'output',
  regex: new RegExp(
    '(<code(?:\\s+[^>]*)?>[\\s\\S]*?<\\/code>|<style(?:\\s+[^>]*)?>[\\s\\S]*?<\\/style>)|\\b(?<!_)_(?!_)(.*?)(?<!_)_(?!_)\\b',
    'gi',
  ),
  replace: (match: string, tagContent: string, italicContent: string) => {
    if (tagContent) return match
    if (italicContent) return '<em>' + italicContent + '</em>'
    return match
  },
}

// ST script.js reloadMarkdownProcessor
const converter = new showdown.Converter({
  emoji: true,
  literalMidWordUnderscores: true,
  parseImgDimensions: true,
  tables: true,
  underline: true,
  simpleLineBreaks: true,
  strikethrough: true,
  disableForced4SpacesIndentedSublists: true,
  metadata: false,
  tablesHeaderId: false,
  noHeaderId: true,
  extensions: [underscoreExt],
})

// 獨立的 DOMPurify 實例，hook 不影響工具其他地方
const purify = DOMPurify(window)
let allowExternalMedia = false

purify.addHook('afterSanitizeAttributes', (node) => {
  if ('target' in node) {
    ;(node as Element).setAttribute('target', '_blank')
    ;(node as Element).setAttribute('rel', 'noopener')
  }
})

// ST chats.js：class 一律加 custom- 前綴（fa- / note- / monospace 除外）
purify.addHook('uponSanitizeAttribute', (_node, data) => {
  if (data.attrName === 'class' && data.attrValue) {
    data.attrValue = data.attrValue
      .split(' ')
      .map((v) => (v.startsWith('fa-') || v.startsWith('note-') || v === 'monospace' ? v : 'custom-' + v))
      .join(' ')
  }
})

// ST chats.js：未知元素中的換行轉 <br>（<pre> 內除外）；預設禁止外部媒體
purify.addHook('uponSanitizeElement', (node) => {
  if (node instanceof HTMLUnknownElement) {
    node.innerHTML = node.innerHTML.trim()
    const walker = document.createTreeWalker(node, NodeFilter.SHOW_TEXT)
    const candidates: Text[] = []
    while (walker.nextNode()) {
      const t = walker.currentNode as Text
      if (!t.data.includes('\n')) continue
      if (t.parentElement?.closest('pre')) continue
      candidates.push(t)
    }
    for (const t of candidates) {
      const frag = document.createDocumentFragment()
      t.data.split('\n').forEach((part, i, arr) => {
        if (part.length) frag.appendChild(document.createTextNode(part))
        if (i < arr.length - 1) frag.appendChild(document.createElement('br'))
      })
      t.replaceWith(frag)
    }
  }
  if (allowExternalMedia || !(node instanceof Element)) return
  if (['AUDIO', 'VIDEO', 'SOURCE', 'TRACK', 'EMBED', 'OBJECT', 'IMG'].includes(node.tagName)) {
    const isExternal = (url: string | null) =>
      !!url && (url.indexOf('://') > 0 || url.indexOf('//') === 0) && !url.startsWith(window.location.origin)
    const srcset = node.getAttribute('srcset')
    if (
      isExternal(node.getAttribute('src')) ||
      isExternal(node.getAttribute('data')) ||
      (srcset && srcset.split(',').some((s) => isExternal(s.trim().split(' ')[0])))
    ) {
      node.remove()
    }
  }
})

/** ST power-user.js fixMarkdown(text, forDisplay=true) */
export function fixMarkdown(text: string): string {
  const format = /([*_]{1,2})([\s\S]*?)\1/gm
  const matches: RegExpExecArray[] = []
  let m: RegExpExecArray | null
  while ((m = format.exec(text)) !== null) matches.push(m)

  let out = text
  for (let i = matches.length - 1; i >= 0; i--) {
    const mt = matches[i][0]
    const rep = mt.replace(
      /(\*|_)([\t    -   　﻿]+)|([\t    -   　﻿]+)(\*|_)/g,
      '$1$4',
    )
    out = out.slice(0, matches[i].index) + rep + out.slice(matches[i].index + mt.length)
  }

  // 補上落單的 * 或 "。ST 兩個字元都以原始行判斷，兩者都落單時後寫入者（"）勝出
  return out
    .split('\n')
    .map((line) => {
      let result = line
      for (const ch of ['*', '"']) {
        if (line.includes(ch) && line.split(ch).length % 2 === 0) result = line.trimEnd() + ch
      }
      return result
    })
    .join('\n')
}

/** ST utils.js escapeHtml */
function escapeHtml(str: string): string {
  return String(str ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;')
}

function escapeRegex(s: string): string {
  return s.replace(/[/\-\\^$*+?.()|[\]{}]/g, '\\$&')
}

/** ST chats.js encodeStyleTags */
function encodeStyleTags(text: string): string {
  return text.replaceAll(/<style>(.+?)<\/style>/gims, (_m, css: string) => `<custom-style>${encodeURIComponent(css)}</custom-style>`)
}

function sanitizeSimpleSelector(selector: string): string {
  return selector
    .split(/\s+/)
    .map((part) => part.replace(/\.([\w-]+)/g, (match, cls: string) => (cls.startsWith('custom-') ? match : `.custom-${cls}`)))
    .join(' ')
}

function sanitizeSelector(selector: string): string {
  const pseudoRegex = new RegExp(`:(${['has', 'not', 'where', 'is', 'matches', 'any'].join('|')})\\(([^)]+)\\)`, 'g')
  selector = selector.replace(pseudoRegex, (_m, pseudo: string, content: string) => `:${pseudo}(${sanitizeSimpleSelector(content)})`)
  return sanitizeSimpleSelector(selector)
}

type CssNode = {
  type?: string
  selectors?: string[]
  declarations?: { value?: string }[]
  rules?: CssNode[]
}

/** ST chats.js decodeStyleTags（使用同版本 @adobe/css-tools），逐行對應。 */
function decodeStyleTags(text: string, prefix = '.mes_text '): string {
  const mediaAllowed = allowExternalMedia

  function sanitizeRule(rule: CssNode) {
    if (Array.isArray(rule.selectors)) {
      for (let i = 0; i < rule.selectors.length; i++) {
        const selector = rule.selectors[i]
        if (selector) rule.selectors[i] = prefix + sanitizeSelector(selector)
      }
    }
    if (!mediaAllowed && Array.isArray(rule.declarations) && rule.declarations.length > 0) {
      // 與 ST 相同：宣告區內的註解沒有 value，這裡會丟出錯誤並顯示 CSS ERROR
      rule.declarations = rule.declarations.filter((d) => !(d.value as string).includes('://'))
    }
  }

  function sanitizeRuleSet(ruleSet: CssNode) {
    if (Array.isArray(ruleSet.selectors) || Array.isArray(ruleSet.declarations)) sanitizeRule(ruleSet)
    if (Array.isArray(ruleSet.rules)) {
      ruleSet.rules = ruleSet.rules.filter((rule) => rule.type !== 'import')
      for (const mediaRule of ruleSet.rules) sanitizeRuleSet(mediaRule)
    }
  }

  return text.replaceAll(/<custom-style>(.+?)<\/custom-style>/gms, (_m, style: string) => {
    try {
      const styleCleaned = decodeURIComponent(style).replaceAll(/<br\/>/g, '')
      const ast = css.parse(styleCleaned)
      const sheet = ast?.stylesheet
      if (sheet) sanitizeRuleSet(sheet as unknown as CssNode)
      return `<style>${css.stringify(ast)}</style>`
    } catch (error) {
      return `CSS ERROR: ${error}`
    }
  })
}

export interface FormatOptions {
  charName: string
  isUser?: boolean
  isReasoning?: boolean
  /** 0 = 最新一則 */
  depth?: number
  scriptSets: ScriptSet[]
  macro: MacroFn
  externalMedia?: boolean
  /** ST power_user.auto_fix_generated_markdown；全新安裝預設為 false */
  autoFixMarkdown?: boolean
  /** ST power_user.reasoning.prefix / suffix */
  reasoningPrefix?: string
  reasoningSuffix?: string
  /** 收集套用了哪些正則 */
  applied?: Applied[]
}

/** ST script.js messageFormatting（不含 reasoning 前後綴、prompt bias、聊天第 0 則巨集等本工具用不到的分支）。 */
export function messageFormatting(mes: string, o: FormatOptions): string {
  if (!mes) return ''
  allowExternalMedia = !!o.externalMedia

  const placement = o.isReasoning ? PLACEMENT.REASONING : o.isUser ? PLACEMENT.USER_INPUT : PLACEMENT.AI_OUTPUT
  mes = getRegexedString(mes, placement, { isMarkdown: true, depth: o.depth }, o.scriptSets, o.macro, o.applied)
  if (o.autoFixMarkdown) mes = fixMarkdown(mes)

  // ST：推理前後綴（預設 <think> / </think>）只跳脫第一次出現，讓它以文字顯示
  for (const s of [o.reasoningPrefix ?? '<think>', o.reasoningSuffix ?? '</think>']) {
    if (s.trim().length && mes.includes(s)) mes = mes.replace(s, escapeHtml(s))
  }

  // 標籤內的雙引號先換成特殊字元，避免被包成 <q>
  mes = mes.replace(/<([^>]+)>/g, (_m, contents: string) => '<' + contents.replace(/"/g, '￾') + '>')
  mes = mes.replace(
    /<style>[\s\S]*?<\/style>|```[\s\S]*?```|~~~[\s\S]*?~~~|``[\s\S]*?``|`[\s\S]*?`|(".*?")|(“.*?”)|(«.*?»)|(「.*?」)|(『.*?』)|(＂.*?＂)/gim,
    (match, p1, p2, p3, p4, p5, p6) => {
      if (p1) return `<q>"${p1.slice(1, -1)}"</q>`
      if (p2) return `<q>“${p2.slice(1, -1)}”</q>`
      if (p3) return `<q>«${p3.slice(1, -1)}»</q>`
      if (p4) return `<q>「${p4.slice(1, -1)}」</q>`
      if (p5) return `<q>『${p5.slice(1, -1)}』</q>`
      if (p6) return `<q>＂${p6.slice(1, -1)}＂</q>`
      return match
    },
  )
  mes = mes.replace(/￾/g, '"')
  mes = mes.replaceAll('\\begin{align*}', '$$').replaceAll('\\end{align*}', '$$')
  mes = converter.makeHtml(mes)
  mes = mes.replace(/<code(.*)>[\s\S]*?<\/code>/g, (m) => m.replace(/\n/gm, '\u0000'))
  // ST 以 \u0000 暫存 code 區塊內的換行
  // oxlint-disable-next-line no-control-regex
  mes = mes.replace(/\u0000/g, '\n').trim()
  mes = mes.replace(/<code(.*)>[\s\S]*?<\/code>/g, (m) => m.replace(/&amp;/g, '&'))

  if (o.charName && !o.isUser) {
    mes = mes.replace(new RegExp(`(^|\n)${escapeRegex(o.charName)}:`, 'g'), '$1')
  }

  mes = encodeStyleTags(mes)
  mes = purify.sanitize(mes, { ADD_TAGS: ['custom-style'] }) as string
  return decodeStyleTags(mes)
}
