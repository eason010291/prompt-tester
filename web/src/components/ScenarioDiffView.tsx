import { diffChars } from 'diff'
import type { ScenarioDiff } from '../types'

const GROUP_LABEL: Record<string, string> = { card: '角色卡', fixture: '測試條件', meta: '其他' }
const LORE_STATUS: Record<string, string> = { added: '新增', removed: '刪除', modified: '修改' }

function TextDiff({ a, b }: { a: string; b: string }) {
  return (
    <pre className="content-diff">
      {diffChars(a, b).map((p, i) =>
        p.added ? <ins key={i}>{p.value}</ins> : p.removed ? <del key={i}>{p.value}</del> : <span key={i}>{p.value}</span>,
      )}
    </pre>
  )
}

/** 情境（角色卡）兩個版本之間的差異：欄位逐字標示、世界書條目。 */
export default function ScenarioDiffView({ diff, labels }: { diff: ScenarioDiff; labels: [string, string] }) {
  if (!diff.fields.length && !diff.lore.length) return <p className="muted small">兩個版本內容相同。</p>
  return (
    <div className="scenario-diff">
      <p className="muted small">
        {labels[0]} → {labels[1]}
      </p>
      {diff.fields.map((f) => (
        <details key={f.path} className="diff-entry" open={f.old.length + f.new.length < 4000}>
          <summary>
            {f.label} <span className={`chip ${f.group === 'card' ? 'st-modified' : 'chip-warn'}`}>{GROUP_LABEL[f.group] ?? f.group}</span>
          </summary>
          <TextDiff a={f.old} b={f.new} />
        </details>
      ))}
      {diff.lore.map((e) => (
        <details key={e.uid} className="diff-entry" open={e.status !== 'removed'}>
          <summary>
            世界書 #{e.uid} {e.comment} <span className={`chip st-${e.status}`}>{LORE_STATUS[e.status]}</span>
            {e.changes.length > 0 && <span className="muted small"> · {e.changes.join('、')}</span>}
          </summary>
          {e.old !== e.new && <TextDiff a={e.old} b={e.new} />}
        </details>
      ))}
    </div>
  )
}
