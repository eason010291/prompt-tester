import { lazy, Suspense, useEffect, useState } from 'react'
import { NavLink, Navigate, Route, Routes } from 'react-router'
import { api, type Health } from './api'
import { useTheme, type Theme } from './theme'
import Placeholder from './pages/Placeholder'
import PromptPage from './pages/PromptPage'

// 次要頁面按需載入（diff、模型列表等套件只在用到時下載）
const ScenariosPage = lazy(() => import('./pages/ScenariosPage'))
const SettingsPage = lazy(() => import('./pages/SettingsPage'))
const VersionsPage = lazy(() => import('./pages/VersionsPage'))
const ExperimentsPage = lazy(() => import('./pages/ExperimentsPage'))
const ScriptsPage = lazy(() => import('./pages/ScriptsPage'))

interface Section {
  path: string
  label: string
  hint: string
  milestone: string
  plans: string[]
}

const SECTIONS: Section[] = [
  {
    path: '/prompt',
    label: '提示詞',
    hint: '區塊組裝與預覽',
    milestone: 'M1',
    plans: [
      '區塊卡片：名稱 / role / 開關 / 拖曳排序 / 文字方塊 / token 估算',
      '分隔區塊與互斥變體群組',
      'chatHistory marker、深度注入、巨集',
      'messages[] 預覽，標示每段的來源區塊',
    ],
  },
  {
    path: '/scenarios',
    label: '情境',
    hint: '測試輸入與歷史',
    milestone: 'M2',
    plans: [],
  },
  {
    path: '/versions',
    label: '版本樹',
    hint: '提交、分支、diff',
    milestone: 'M3',
    plans: ['工作區提交與切換', '區塊級 diff 與 cherry-pick', 'commit 圖與各版本平均分'],
  },
  {
    path: '/scripts',
    label: '腳本',
    hint: '金字塔階段與 seed',
    milestone: 'M4',
    plans: ['階段：回合數 / seed 數 / 晉級方式', '父腳本 + 覆寫的繼承樹', '溫度掃描'],
  },
  {
    path: '/experiments',
    label: '實驗',
    hint: '金字塔批次、評語、比較',
    milestone: 'M4–M6',
    plans: ['對話樹與晉級挑選', '自由評語與分數解析', '父子版本並排比較', '依評語由 LLM 修改'],
  },
  {
    path: '/settings',
    label: '設定',
    hint: '金鑰、連線、模型',
    milestone: 'M2',
    plans: ['OpenRouter 連線與供應商固定', '模型價格與支援參數', '評語佔位模板'],
  },
]

const BUILT = new Set(['/prompt', '/scenarios', '/settings', '/versions', '/experiments', '/scripts'])

const THEMES: { value: Theme; label: string }[] = [
  { value: 'system', label: '系統' },
  { value: 'light', label: '淺' },
  { value: 'dark', label: '深' },
]

export default function App() {
  const [theme, setTheme] = useTheme()
  const health = useHealth()

  return (
    <div className="shell">
      <nav className="sidebar" aria-label="主選單">
        <div className="brand">
          <img src="/favicon.svg" alt="" />
          Prompt Tester
        </div>
        {SECTIONS.map((s) => (
          <NavLink key={s.path} to={s.path} className="nav-link">
            {s.label}
            <small>{s.hint}</small>
          </NavLink>
        ))}
        <div className="sidebar-footer">
          <span className="badge">
            <span className={`dot ${health.state}`} />
            {health.label}
          </span>
          <div className="segmented" role="group" aria-label="主題">
            {THEMES.map((t) => (
              <button key={t.value} aria-pressed={theme === t.value} onClick={() => setTheme(t.value)}>
                {t.label}
              </button>
            ))}
          </div>
        </div>
      </nav>
      <main className="main">
        <Suspense fallback={<p className="muted">載入中…</p>}>
          <Routes>
            <Route path="/" element={<Navigate to="/prompt" replace />} />
            <Route path="/prompt" element={<PromptPage />} />
            <Route path="/scenarios" element={<ScenariosPage />} />
            <Route path="/settings" element={<SettingsPage />} />
            <Route path="/versions" element={<VersionsPage />} />
            <Route path="/experiments" element={<ExperimentsPage />} />
            <Route path="/scripts" element={<ScriptsPage />} />
            {SECTIONS.filter((s) => !BUILT.has(s.path)).map((s) => (
              <Route
                key={s.path}
                path={s.path}
                element={<Placeholder title={s.label} milestone={s.milestone} plans={s.plans} />}
              />
            ))}
            <Route path="*" element={<Navigate to="/prompt" replace />} />
          </Routes>
        </Suspense>
      </main>
    </div>
  )
}

function useHealth(): { state: 'ok' | 'err' | ''; label: string } {
  const [result, setResult] = useState<{ state: 'ok' | 'err' | ''; label: string }>({
    state: '',
    label: '連線中…',
  })

  useEffect(() => {
    api<Health>('/health')
      .then((h) => setResult({ state: 'ok', label: `後端 v${h.version} · schema ${h.schema_version}` }))
      .catch(() => setResult({ state: 'err', label: '後端未連線' }))
  }, [])

  return result
}
