import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../api'
import type { BuildResponse, PreviewContext, Preset, Workspace } from '../types'

export type SaveState = 'loading' | 'saved' | 'dirty' | 'saving' | 'error'

const SAVE_DELAY = 800
const BUILD_DELAY = 250

/** 載入工作區、自動儲存（防抖）。 */
export function useWorkspace() {
  const [ws, setWs] = useState<Workspace | null>(null)
  const [saveState, setSaveState] = useState<SaveState>('loading')
  const [loadError, setLoadError] = useState<string | null>(null)
  const timer = useRef<number | undefined>(undefined)
  const pending = useRef<Workspace | null>(null)

  useEffect(() => {
    api<Workspace>('/workspace')
      .then((w) => {
        setWs(w)
        setSaveState('saved')
      })
      .catch((e: Error) => setLoadError(e.message))
  }, [])

  const flush = useCallback(async () => {
    const next = pending.current
    if (!next) return
    pending.current = null
    setSaveState('saving')
    try {
      const saved = await api<Workspace>('/workspace', { method: 'PUT', body: JSON.stringify(next) })
      if (!pending.current) {
        setWs((cur) => (cur ? { ...cur, updated_at: saved.updated_at } : cur))
        setSaveState('saved')
      }
    } catch {
      setSaveState('error')
    }
  }, [])

  const update = useCallback(
    (fn: (w: Workspace) => Workspace) => {
      setWs((cur) => {
        if (!cur) return cur
        const next = fn(cur)
        pending.current = next
        return next
      })
      setSaveState('dirty')
      window.clearTimeout(timer.current)
      timer.current = window.setTimeout(flush, SAVE_DELAY)
    },
    [flush],
  )

  // 離開頁面前把未儲存的變更送出（keepalive 讓請求在頁面關閉後仍能完成；上限約 64KB）
  useEffect(() => {
    const onHide = () => {
      const next = pending.current
      if (!next) return
      pending.current = null
      void fetch('/api/workspace', {
        method: 'PUT',
        keepalive: true,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(next),
      })
    }
    window.addEventListener('pagehide', onHide)
    return () => window.removeEventListener('pagehide', onHide)
  }, [])

  const setPreset = useCallback((fn: (p: Preset) => Preset) => update((w) => ({ ...w, preset: fn(w.preset) })), [update])
  const setContext = useCallback(
    (fn: (c: PreviewContext) => PreviewContext) => update((w) => ({ ...w, context: fn(w.context) })),
    [update],
  )

  return { ws, saveState, loadError, setPreset, setContext }
}

/** 預設或情境變動後，防抖重新組裝預覽。 */
export function useBuild(preset: Preset | undefined, context: PreviewContext | undefined, expandMacros: boolean) {
  const [result, setResult] = useState<BuildResponse | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    if (!preset || !context) return
    const ctrl = new AbortController()
    const t = window.setTimeout(() => {
      setBusy(true)
      api<BuildResponse>('/build', {
        method: 'POST',
        body: JSON.stringify({ preset, context, expand_macros: expandMacros }),
        signal: ctrl.signal,
      })
        .then((r) => {
          setResult(r)
          setError(null)
        })
        .catch((e: Error) => {
          if (e.name !== 'AbortError') setError(e.message)
        })
        .finally(() => {
          if (!ctrl.signal.aborted) setBusy(false)
        })
    }, BUILD_DELAY)
    return () => {
      window.clearTimeout(t)
      ctrl.abort()
    }
  }, [preset, context, expandMacros])

  return { result, error, busy }
}
