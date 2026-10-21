import type { Run, RunMessage, ScriptData } from '../types'

/** 分數等級（顏色用）：≥ 7.5 高、≥ 5 中、其餘低 */
export function scoreLevel(v: number | null | undefined): 'hi' | 'mid' | 'lo' | 'none' {
  if (v == null) return 'none'
  return v >= 7.5 ? 'hi' : v >= 5 ? 'mid' : 'lo'
}

/** 每則訊息附上 ST 正則用的 depth（0 = 最新一則，含情境歷史一起計算） */
export function withDepth(r: Run): { m: RunMessage; depth: number }[] {
  const total = r.scenario.history.length + r.messages.length
  return r.messages.map((m, idx) => ({ m, depth: total - (r.scenario.history.length + idx) - 1 }))
}

export interface EstimateRow {
  i: number
  branches: number
  turns: number
  calls: number
  note: string
}

/** 估算腳本各階段的分支數、新增回合與 LLM 呼叫上限（人工挑選假設挑 K 條） */
export function estimateScript(data: ScriptData, scenarios: number): { rows: EstimateRow[]; total: number } {
  const rows: EstimateRow[] = []
  let starts = scenarios
  let prevTurns = 0
  let total = 0
  data.stages.forEach((st, i) => {
    const restart = i === 0 || data.stages[i - 1].promote.mode === 'restart'
    if (restart) {
      starts = scenarios
      prevTurns = 0
    }
    const branches = starts * st.seeds * Math.max(1, st.temperatures.length)
    const turns = Math.max(0, st.turns - prevTurns)
    const calls = branches * turns * 2
    total += calls
    rows.push({ i, branches, turns, calls, note: restart && i > 0 ? '從開場' : '' })
    // 下一階段的起點數
    const m = st.promote.mode
    starts = m === 'all' ? branches : m === 'restart' ? scenarios : Math.min(st.promote.k, branches / scenarios) * scenarios
    prevTurns = st.turns
  })
  return { rows, total }
}
