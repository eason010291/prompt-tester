import DOMPurify from 'dompurify'
import showdown from 'showdown'

/*
 * 回覆渲染（M2 基本版）。選項比照 SillyTavern messageFormatting 使用的 showdown 設定。
 * showdown 有未修正的 XSS / ReDoS 公告：輸出一律經 DOMPurify 消毒，並關閉 metadata 與表頭 id。
 * class 仿照 ST 加上 `custom-` 前綴，避免模型輸出的 class 撞到本工具的樣式；
 * <style> 一律移除（預設 CSS 的完整渲染在 M8 以 sandbox iframe 處理）。
 */
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
})

let hooked = false
function ensureHook() {
  if (hooked) return
  hooked = true
  DOMPurify.addHook('afterSanitizeAttributes', (node) => {
    if (node instanceof Element && node.hasAttribute('class')) {
      const classes = node.getAttribute('class')!.split(/\s+/).filter(Boolean)
      node.setAttribute('class', classes.map((c) => (c.startsWith('custom-') ? c : `custom-${c}`)).join(' '))
    }
    if (node instanceof HTMLAnchorElement) {
      node.setAttribute('target', '_blank')
      node.setAttribute('rel', 'noopener noreferrer')
    }
  })
}

export function renderMarkdown(text: string): string {
  ensureHook()
  return DOMPurify.sanitize(converter.makeHtml(text), {
    FORBID_TAGS: ['style', 'script', 'iframe', 'form', 'input', 'button', 'textarea', 'select'],
  })
}
