/** git log --graph 風格的泳道配置。commits 須由新到舊排列。 */
export interface GraphRow {
  col: number
  /** 這一列上半段：每條泳道的線要接到哪一欄（null = 無線） */
  top: (number | null)[]
  /** 這一列下半段：從哪一欄出發往下（null = 無線） */
  bottom: (number | null)[]
  width: number
}

export function layoutGraph(commits: { id: string; parents: string[] }[]): GraphRow[] {
  const lanes: (string | null)[] = []
  const rows: GraphRow[] = []
  for (const c of commits) {
    let col = lanes.indexOf(c.id)
    if (col < 0) {
      col = lanes.indexOf(null)
      if (col < 0) col = lanes.push(null) - 1
    }
    const before = [...lanes]

    // 上半段：等待這個 commit 的泳道都匯入節點；其他泳道直行
    const top = before.map((id, i) => (id === null ? null : id === c.id ? col : i))
    if (before[col] !== c.id) top[col] = null

    // 更新泳道：節點所在欄接續第一個父節點，其他等待同一 commit 的泳道結束
    before.forEach((id, i) => {
      if (id === c.id && i !== col) lanes[i] = null
    })
    lanes[col] = c.parents[0] ?? null
    for (const p of c.parents.slice(1)) {
      if (lanes.includes(p)) continue
      const free = lanes.indexOf(null)
      if (free >= 0) lanes[free] = p
      else lanes.push(p)
    }

    // 下半段：節點往父節點；其他仍在進行的泳道直行
    const bottom = lanes.map((id, i) => {
      if (id === null) return null
      if (i === col) return col
      return before[i] !== null && before[i] !== c.id ? i : col
    })
    while (lanes.length && lanes[lanes.length - 1] === null) lanes.pop()
    rows.push({ col, top, bottom, width: Math.max(top.length, bottom.length, col + 1) })
  }
  return rows
}
