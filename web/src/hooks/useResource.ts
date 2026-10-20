import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import { api } from '../api'

/** 讀取一個 API 資源，提供 reload。path 為 null 時不讀取。 */
export function useResource<T>(path: string | null) {
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(path !== null)
  const [version, setVersion] = useState(0)

  useEffect(() => {
    if (!path) return
    let cancelled = false
    api<T>(path)
      .then((d) => {
        if (cancelled) return
        setData(d)
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

/** 防抖自動儲存：value 改變後 delay 毫秒呼叫 save。第一次的值視為已儲存。 */
export function useAutosave<T>(value: T, save: (v: T) => Promise<unknown>, delay = 700) {
  const [state, setState] = useState<'saved' | 'dirty' | 'saving' | 'error'>('saved')
  const initial = useRef(value)
  const saveRef = useRef(save)

  useLayoutEffect(() => {
    saveRef.current = save
  })

  useEffect(() => {
    if (value === initial.current) return
    const dirty = window.setTimeout(() => setState('dirty'), 0)
    const t = window.setTimeout(async () => {
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
