import type { MetricDef } from '../types'

/**
 * 自動指標的次數。replies 有值時以「每則平均」判斷是否超過警戒值（分支合計用）；
 * 省略時以次數本身判斷（單則回覆用）。次數為 0 的不顯示，除非 showZero。
 */
export default function MetricChips({
  defs,
  values,
  replies,
  showZero,
}: {
  defs: MetricDef[] | undefined
  values: Record<string, number> | undefined
  replies?: number
  showZero?: boolean
}) {
  if (!defs?.length || !values) return null
  const items = defs
    .map((d) => ({ d, v: values[d.id] ?? 0 }))
    .filter(({ v }) => showZero || v > 0)
  if (!items.length) return null
  return (
    <>
      {items.map(({ d, v }) => {
        const basis = replies ? v / replies : v
        const warn = d.warn_at != null && (d.direction === 'lower' ? basis >= d.warn_at : basis <= d.warn_at)
        return (
          <span
            key={d.id}
            className={`chip metric-chip ${warn ? 'is-warn' : ''}`}
            title={`${d.name}：${replies ? `共 ${v} 次，平均每則 ${(v / replies).toFixed(1)}` : `${v} 次`}${d.warn_at != null ? `（每則達 ${d.warn_at} 次標示）` : ''}`}
          >
            {d.name} ×{v}
          </span>
        )
      })}
    </>
  )
}
