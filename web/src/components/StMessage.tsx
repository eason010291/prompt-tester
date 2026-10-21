import { useEffect, useMemo, useRef, useState } from 'react'
import { messageFormatting, type FormatOptions } from '../lib/st/format'
import type { Applied } from '../lib/st/regex'

export type StTheme = 'st-dark' | 'light'

/*
 * 以 sandbox iframe 顯示 ST 格式化後的訊息：
 * - 不允許腳本（DOMPurify 也已移除），允許同源以便量測高度，允許從連結開新分頁
 * - 預設 / 角色卡的 CSS 只作用在 iframe 內的 .mes_text，不影響工具介面
 * - 基本樣式取自 ST 1.18.0 style.css 的預設主題變數與 .mes_text 規則
 */
const BASE_CSS = `
:root {
  --SmartThemeBodyColor: rgb(220, 220, 210);
  --SmartThemeEmColor: rgb(145, 145, 145);
  --SmartThemeUnderlineColor: rgb(188, 231, 207);
  --SmartThemeQuoteColor: rgb(225, 138, 36);
  --SmartThemeBorderColor: rgba(0, 0, 0, 0.5);
  --SmartThemeBlurTintColor: rgba(23, 23, 23, 1);
  --SmartThemeChatTintColor: rgba(23, 23, 23, 1);
  --black70a: rgba(0, 0, 0, 0.7);
  --mainFontSize: 15px;
  --mainFontFamily: "Noto Sans", "Noto Sans TC", "Microsoft JhengHei", sans-serif;
  --monoFontFamily: "Noto Sans Mono", "Courier New", Consolas, monospace;
}
:root.light {
  --SmartThemeBodyColor: rgb(40, 40, 36);
  --SmartThemeEmColor: rgb(110, 110, 110);
  --SmartThemeUnderlineColor: rgb(30, 120, 80);
  --SmartThemeQuoteColor: rgb(190, 100, 10);
  --SmartThemeChatTintColor: rgb(250, 249, 245);
  --black70a: rgba(0, 0, 0, 0.06);
}
html, body { margin: 0; background: var(--SmartThemeChatTintColor); color: var(--SmartThemeBodyColor);
  font-family: var(--mainFontFamily); font-size: var(--mainFontSize); line-height: 1.6; }
.mes_text { padding: 8px 12px; overflow-wrap: anywhere; }
.mes_text p { margin-top: 0; margin-bottom: 10px; }
.mes_text p:last-child { margin-bottom: 0; }
.mes_text table { border-spacing: 0; border-collapse: collapse; margin-bottom: 10px; }
.mes_text td, .mes_text th { border: 1px solid; border-collapse: collapse; padding: 0.25em; }
.mes_text ol, .mes_text ul { margin-top: 5px; margin-bottom: 5px; }
.mes_text i, .mes_text em { color: var(--SmartThemeEmColor); }
.mes_text q i, .mes_text q em { color: inherit; }
.mes_text u { color: var(--SmartThemeUnderlineColor); }
.mes_text q { color: var(--SmartThemeQuoteColor); }
.mes_text q:before, .mes_text q:after { content: ''; }
.mes_text font[color] em, .mes_text font[color] i, .mes_text font[color] u, .mes_text font[color] q { color: inherit; }
.mes_text hr { opacity: 0.4; }
.mes_text code { font-family: var(--monoFontFamily); white-space: pre-wrap; border: 1px solid var(--SmartThemeBorderColor);
  border-radius: 5px; background-color: var(--black70a); padding: 0 3px; }
.mes_text pre code { position: relative; display: block; overflow-x: auto; padding: 1em; }
.mes_text img { max-width: 100%; }
`

interface Props extends Omit<FormatOptions, 'applied'> {
  text: string
  theme: StTheme
  onApplied?: (applied: Applied[]) => void
}

export default function StMessage({ text, theme, onApplied, ...opts }: Props) {
  const frame = useRef<HTMLIFrameElement>(null)
  const [height, setHeight] = useState(40)

  const { html, applied } = useMemo(() => {
    const applied: Applied[] = []
    return { html: messageFormatting(text, { ...opts, applied }), applied }
    // opts 是每次 render 的新物件，以其內容判斷是否需要重算
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [text, opts.charName, opts.isUser, opts.isReasoning, opts.depth, opts.scriptSets, opts.macro, opts.externalMedia, opts.autoFixMarkdown])

  useEffect(() => {
    onApplied?.(applied)
  }, [applied, onApplied])

  const srcDoc = useMemo(
    () =>
      `<!doctype html><html class="${theme === 'light' ? 'light' : ''}"><head><meta charset="utf-8"><style>${BASE_CSS}</style></head>` +
      `<body><div class="mes_text">${html}</div></body></html>`,
    [html, theme],
  )

  useEffect(() => {
    const el = frame.current
    if (!el) return
    let observer: ResizeObserver | null = null
    const measure = () => {
      const body = el.contentDocument?.body
      if (body) setHeight(Math.ceil(body.scrollHeight) + 2)
    }
    const onLoad = () => {
      measure()
      const body = el.contentDocument?.body
      if (body) {
        observer = new ResizeObserver(measure)
        observer.observe(body)
      }
    }
    el.addEventListener('load', onLoad)
    return () => {
      el.removeEventListener('load', onLoad)
      observer?.disconnect()
    }
  }, [srcDoc])

  return (
    <iframe
      ref={frame}
      className="st-frame"
      title="訊息渲染"
      sandbox="allow-same-origin allow-popups allow-popups-to-escape-sandbox"
      srcDoc={srcDoc}
      style={{ height }}
    />
  )
}
