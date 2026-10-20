import { useCallback, useEffect, useState, type ChangeEvent } from 'react'
import { api } from '../api'
import BlockList from '../components/BlockList'
import PreviewPane from '../components/PreviewPane'
import ScenarioPicker from '../components/ScenarioPicker'
import SettingsPanel from '../components/SettingsPanel'
import TryRunPanel from '../components/TryRunPanel'
import VersionBar from '../components/VersionBar'
import { useResource } from '../hooks/useResource'
import { useBuild, useWorkspace, type SaveState } from '../hooks/useWorkspace'
import type { Block, ImportResponse, Scenario } from '../types'

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
  const [importNotes, setImportNotes] = useState<{ title: string; lines: string[] } | null>(null)
  const [tab, setTab] = useState<'preview' | 'run'>('preview')
  const scenarios = useResource<Scenario[]>('/scenarios')
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

  const onImportFile = async (e: ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0]
    e.target.value = ''
    if (!file) return
    try {
      const data = JSON.parse(await file.text())
      const res = await api<ImportResponse>('/import/st-preset', {
        method: 'POST',
        body: JSON.stringify({ data, filename: file.name }),
      })
      if (!window.confirm(`以「${res.preset.name}」（${res.preset.blocks.length} 個區塊）取代目前的預設？`)) return
      setPreset(() => res.preset)
      setImportNotes({ title: `已匯入「${res.preset.name}」`, lines: res.notes })
    } catch (err) {
      setImportNotes({ title: '匯入失敗', lines: [err instanceof Error ? err.message : String(err)] })
    }
  }

  const onExport = async () => {
    if (!ws) return
    const data = await api<object>('/export/st-preset', { method: 'POST', body: JSON.stringify(ws.preset) })
    const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 4)], { type: 'application/json' }))
    const a = document.createElement('a')
    a.href = url
    a.download = `${ws.preset.name || 'preset'}.json`
    a.click()
    URL.revokeObjectURL(url)
  }

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
          <div className="header-actions">
            <label className="btn btn-sm" title="匯入 SillyTavern Chat Completion 預設（.json）">
              匯入 ST 預設
              <input type="file" accept=".json,application/json" hidden onChange={onImportFile} />
            </label>
            <button className="btn btn-sm" onClick={onExport} title="匯出成 SillyTavern 可匯入的 JSON">
              匯出 ST JSON
            </button>
          </div>
        </header>
        {importNotes && (
          <div className="alert notes" role="status">
            <strong>{importNotes.title}</strong>
            <ul>
              {importNotes.lines.map((n) => (
                <li key={n}>{n}</li>
              ))}
            </ul>
            <button className="icon-btn" aria-label="關閉" onClick={() => setImportNotes(null)}>
              ✕
            </button>
          </div>
        )}
        <VersionBar preset={preset} saveState={saveState} />
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
        <ScenarioPicker context={context} scenarios={scenarios.data ?? []} onChange={setContext} />
        <div className="tabs" role="tablist" aria-label="右側面板">
          <button role="tab" aria-selected={tab === 'preview'} onClick={() => setTab('preview')}>
            messages[] 預覽
          </button>
          <button role="tab" aria-selected={tab === 'run'} onClick={() => setTab('run')}>
            試跑
          </button>
        </div>
        {tab === 'preview' ? (
          <PreviewPane
            result={result}
            error={error}
            busy={busy}
            expandMacros={expandMacros}
            onExpandMacros={setExpandMacros}
            onSourceClick={jumpTo}
          />
        ) : (
          <TryRunPanel
            preset={preset}
            context={context}
            scenario={scenarios.data?.find((s) => s.id === context.scenario_id) ?? null}
          />
        )}
      </aside>
    </div>
  )
}
