import { useEffect, useState } from 'react'

export type Theme = 'system' | 'light' | 'dark'

const KEY = 'prompt-tester.theme'

function readStored(): Theme {
  try {
    const v = localStorage.getItem(KEY)
    return v === 'light' || v === 'dark' ? v : 'system'
  } catch {
    return 'system'
  }
}

export function useTheme(): [Theme, (t: Theme) => void] {
  const [theme, setTheme] = useState<Theme>(readStored)

  useEffect(() => {
    const root = document.documentElement
    if (theme === 'system') root.removeAttribute('data-theme')
    else root.setAttribute('data-theme', theme)
    try {
      localStorage.setItem(KEY, theme)
    } catch {
      // 無痕模式等情況下 localStorage 不可用，主題只在本次有效
    }
  }, [theme])

  return [theme, setTheme]
}
