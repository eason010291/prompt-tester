import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import { api } from '../api'

/**
 * 讀取一個 API 資源，提供 reload。path 為 null 時不讀取。
 * path 改變後、新資料到之前回傳 null，避免畫面顯示屬於上一個 path 的舊資料
 * （例如 diff 切換比較對象時，舊 diff 配上新的「套用」按鈕）。reload 同一 path 時保留舊資料。
 */
export function useResource<T>(path: string | null) {
  const [result, setResult] = useState<{ path: string | null; data: T | null }>({ path: null, data: null })
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(path !== null)
  const [version, setVersion] = useState(0)

  const data = result.path === path ? result.data : null
  const setData = useCallback(
    (next: T | null | ((cur: T | null) => T | null)) =>
      setResult((r) => ({
        path: r.path,
        data: typeof next === 'function' ? (next as (cur: T | null) => T | null)(r.data) : next,
      })),
    [],
  )

  useEffect(() => {
    if (!path) return
    let cancelled = false
    api<T>(path)
      .then((d) => {
        if (cancelled) return
        setResult({ path, data: d })
        setError(null)
      })
      .catch((e) => {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e))
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [path, version])

  const reload = useCallback(() => {
    setLoading(true)
    setVersion((v) => v + 1)
  }, [])

  return { data, setData, error, loading, reload }
}

/**
 * 防抖自動儲存：value 改變後 delay 毫秒呼叫 save。第一次的值視為已儲存。
 * 元件卸載時（例如切換到另一個情境）若還有沒送出的變更，立刻送出，避免遺失。
 */
export function useAutosave<T>(value: T, save: (v: T) => Promise<unknown>, delay = 700) {
  const [state, setState] = useState<'saved' | 'dirty' | 'saving' | 'error'>('saved')
  const initial = useRef(value)
  const saveRef = useRef(save)
  const pending = useRef<{ value: T } | null>(null)

  useLayoutEffect(() => {
    saveRef.current = save
  })

  useEffect(
    () => () => {
      if (pending.current) void saveRef.current(pending.current.value)
    },
    [],
  )

  useEffect(() => {
    if (value === initial.current) return
    pending.current = { value }
    const dirty = window.setTimeout(() => setState('dirty'), 0)
    const t = window.setTimeout(async () => {
      pending.current = null
      setState('saving')
      try {
        await saveRef.current(value)
        setState('saved')
      } catch {
        setState('error')
      }
    }, delay)
    return () => {
      window.clearTimeout(dirty)
      window.clearTimeout(t)
    }
  }, [value, delay])

  return state
}

export function formatCost(cost: number | null | undefined): string {
  if (cost == null) return '—'
  if (cost === 0) return '$0'
  return cost < 0.01 ? `$${cost.toFixed(5)}` : `$${cost.toFixed(4)}`
}

export function formatPrice(v: number | null | undefined): string {
  return v == null ? '—' : `$${v < 1 ? v.toFixed(3) : v.toFixed(2)}`
}
