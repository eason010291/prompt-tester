import { useCallback, useEffect, useState } from 'react'
import BlockList from '../components/BlockList'
import ContextPanel from '../components/ContextPanel'
import PreviewPane from '../components/PreviewPane'
import SettingsPanel from '../components/SettingsPanel'
import { useBuild, useWorkspace, type SaveState } from '../hooks/useWorkspace'
import type { Block } from '../types'

const SAVE_LABEL: Record<SaveState, string> = {
  loading: '載入中…',
  saved: '已儲存',
  dirty: '未儲存的變更',
  saving: '儲存中…',
  error: '儲存失敗，將於下次變更時重試',
}

export default function PromptPage() {
  const { ws, saveState, loadError, setPreset, setContext } = useWorkspace()
  const [expandMacros, setExpandMacros] = useState(true)
  const [highlightId, setHighlightId] = useState<string | null>(null)
  const { result, error, busy } = useBuild(ws?.preset, ws?.context, expandMacros)

  const setBlocks = useCallback((blocks: Block[]) => setPreset((p) => ({ ...p, blocks })), [setPreset])

  const jumpTo = useCallback(
    (blockId: string) => {
      setPreset((p) => ({ ...p, blocks: p.blocks.map((b) => (b.id === blockId ? { ...b, collapsed: false } : b)) }))
      setHighlightId(blockId)
      requestAnimationFrame(() =>
        document.getElementById(`block-${blockId}`)?.scrollIntoView({ behavior: 'smooth', block: 'center' }),
      )
    },
    [setPreset],
  )

  useEffect(() => {
    if (!highlightId) return
    const t = window.setTimeout(() => setHighlightId(null), 1600)
    return () => window.clearTimeout(t)
  }, [highlightId])

  if (loadError) return <p className="alert alert-error">無法載入工作區：{loadError}</p>
  if (!ws) return <p className="muted">載入中…</p>

  const { preset, context } = ws
  const enabled = preset.blocks.filter((b) => b.enabled && b.kind === 'content').length

  return (
    <div className="prompt-page">
      <div className="editor-col">
        <header className="page-header row">
          <input
            className="title-input"
            value={preset.name}
            aria-label="預設名稱"
            onChange={(e) => setPreset((p) => ({ ...p, name: e.target.value }))}
          />
          <span className={`save-state save-${saveState}`}>{SAVE_LABEL[saveState]}</span>
        </header>
        <p className="muted small">
          {preset.blocks.length} 個區塊，{enabled} 個內容區塊啟用 · 拖曳 ⠿ 排序；變體群組相同的區塊只會有一個啟用
        </p>
        <SettingsPanel
          params={preset.params}
          options={preset.options}
          onParams={(p) => setPreset((x) => ({ ...x, params: { ...x.params, ...p } }))}
          onOptions={(o) => setPreset((x) => ({ ...x, options: { ...x.options, ...o } }))}
        />
        <BlockList
          blocks={preset.blocks}
          blockTokens={result?.block_tokens ?? {}}
          highlightId={highlightId}
          onChange={setBlocks}
        />
      </div>
      <aside className="preview-col">
        <ContextPanel context={context} onChange={setContext} />
        <PreviewPane
          result={result}
          error={error}
          busy={busy}
          expandMacros={expandMacros}
          onExpandMacros={setExpandMacros}
          onSourceClick={jumpTo}
        />
      </aside>
    </div>
  )
}
