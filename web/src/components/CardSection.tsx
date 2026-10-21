import { useState } from 'react'
import { WI_POSITION, type CharacterCard, type LoreEntry } from '../types'

interface Props {
  card: CharacterCard
  opening: string
  onChange: (card: CharacterCard) => void
  onOpening: (text: string) => void
  onRemove: () => void
}

const FIELDS: { key: keyof CharacterCard; label: string; marker: string }[] = [
  { key: 'description', label: '角色描述', marker: 'charDescription' },
  { key: 'personality', label: '個性', marker: 'charPersonality' },
  { key: 'scenario', label: '情境', marker: 'scenario' },
  { key: 'mes_example', label: '範例對話（<START> 分段）', marker: 'dialogueExamples' },
  { key: 'system_prompt', label: '角色 system prompt（覆寫 main）', marker: 'main' },
  { key: 'post_history_instructions', label: '角色 post-history（覆寫 jailbreak）', marker: 'jailbreak' },
  { key: 'creator_notes', label: '作者備註（不送出）', marker: '' },
]

const ROLE = ['system', 'user', 'assistant']

export default function CardSection({ card, opening, onChange, onOpening, onRemove }: Props) {
  const set = (patch: Partial<CharacterCard>) => onChange({ ...card, ...patch })
  const greetings = [card.first_mes, ...card.alternate_greetings]
  const book = card.book

  const setEntry = (uid: number, patch: Partial<LoreEntry>) =>
    book && set({ book: { ...book, entries: book.entries.map((e) => (e.uid === uid ? { ...e, ...patch } : e)) } })

  return (
    <div className="card card-section">
      <div className="list-head">
        <h2>角色卡：{card.name}</h2>
        <span className="muted small">{card.spec}</span>
        <button className="btn btn-sm danger" onClick={() => window.confirm('移除這個情境的角色卡資料？') && onRemove()}>
          移除角色卡
        </button>
      </div>

      {greetings.length > 1 && (
        <div className="greetings">
          <span className="label">開場白（共 {greetings.length} 個）：</span>
          {greetings.map((g, i) => (
            <button
              key={i}
              className={`chip chip-source ${g === opening ? 'active' : ''}`}
              title={g.slice(0, 200)}
              onClick={() => onOpening(g)}
            >
              {i === 0 ? '主要' : `替代 ${i}`}
            </button>
          ))}
        </div>
      )}

      {FIELDS.map((f) => {
        const value = card[f.key] as string
        return (
          <details key={f.key} className="field-fold">
            <summary>
              {f.label}
              {f.marker && <code className="muted"> → {f.marker}</code>}
              <span className="muted small"> · {value.length.toLocaleString()} 字</span>
            </summary>
            <textarea value={value} onChange={(e) => set({ [f.key]: e.target.value } as Partial<CharacterCard>)} />
          </details>
        )
      })}

      <details className="field-fold">
        <summary>
          深度提示（Character's Note）
          <span className="muted small">
            {' '}
            · {card.depth_prompt.prompt ? `深度 ${card.depth_prompt.depth} · ${card.depth_prompt.role}` : '未設定'}
          </span>
        </summary>
        <div className="form-row-inline">
          <label>
            深度{' '}
            <input
              type="number"
              min={0}
              value={card.depth_prompt.depth}
              onChange={(e) => set({ depth_prompt: { ...card.depth_prompt, depth: Number(e.target.value) } })}
            />
          </label>
          <label>
            角色{' '}
            <select
              value={card.depth_prompt.role}
              onChange={(e) =>
                set({ depth_prompt: { ...card.depth_prompt, role: e.target.value as CharacterCard['depth_prompt']['role'] } })
              }
            >
              {ROLE.map((r) => (
                <option key={r}>{r}</option>
              ))}
            </select>
          </label>
        </div>
        <textarea
          value={card.depth_prompt.prompt}
          onChange={(e) => set({ depth_prompt: { ...card.depth_prompt, prompt: e.target.value } })}
        />
      </details>

      {book && <LorebookTable entries={book.entries} name={book.name} onEntry={setEntry} />}
    </div>
  )
}

function LorebookTable({
  entries,
  name,
  onEntry,
}: {
  entries: LoreEntry[]
  name: string
  onEntry: (uid: number, patch: Partial<LoreEntry>) => void
}) {
  const [open, setOpen] = useState<number | null>(null)
  const enabled = entries.filter((e) => e.enabled).length
  const constant = entries.filter((e) => e.enabled && e.constant).length

  return (
    <details className="field-fold" open>
      <summary>
        世界書：{name || '（未命名）'}
        <span className="muted small">
          {' '}
          · {entries.length} 條，啟用 {enabled}，常駐 {constant}
        </span>
      </summary>
      <table className="ep-table lore-table">
        <thead>
          <tr>
            <th>啟用</th>
            <th>名稱</th>
            <th>觸發</th>
            <th>位置</th>
            <th className="num">順序</th>
            <th className="num">字數</th>
          </tr>
        </thead>
        <tbody>
          {entries.map((e) => (
            <LoreRow
              key={e.uid}
              entry={e}
              open={open === e.uid}
              onToggleOpen={() => setOpen(open === e.uid ? null : e.uid)}
              onChange={(p) => onEntry(e.uid, p)}
            />
          ))}
        </tbody>
      </table>
    </details>
  )
}

function LoreRow({
  entry: e,
  open,
  onToggleOpen,
  onChange,
}: {
  entry: LoreEntry
  open: boolean
  onToggleOpen: () => void
  onChange: (p: Partial<LoreEntry>) => void
}) {
  const trigger = e.constant ? '常駐' : e.keys.length ? e.keys.join('、') : '（無關鍵字）'
  const pos = WI_POSITION[e.position] ?? String(e.position)
  return (
    <>
      <tr className={e.enabled ? '' : 'dim'}>
        <td>
          <input type="checkbox" checked={e.enabled} aria-label={`啟用 ${e.comment}`} onChange={(ev) => onChange({ enabled: ev.target.checked })} />
        </td>
        <td>
          <button className="link-btn" onClick={onToggleOpen}>
            {open ? '▾' : '▸'} {e.comment || `#${e.uid}`}
          </button>
        </td>
        <td className="lore-keys" title={trigger}>
          {e.constant ? <span className="chip chip-marker">常駐</span> : trigger}
          {e.secondary_keys.length > 0 && <span className="muted"> ＋ {e.secondary_keys.join('、')}</span>}
        </td>
        <td>
          {pos}
          {e.position === 4 && ` ${e.depth}`}
        </td>
        <td className="num">{e.order}</td>
        <td className="num">{e.content.length.toLocaleString()}</td>
      </tr>
      {open && (
        <tr>
          <td colSpan={6}>
            <label className="stack">
              關鍵字（逗號分隔）
              <input
                value={e.keys.join(', ')}
                onChange={(ev) => onChange({ keys: ev.target.value.split(',').map((k) => k.trim()).filter(Boolean) })}
              />
            </label>
            <label className="stack">
              內容
              <textarea value={e.content} onChange={(ev) => onChange({ content: ev.target.value })} />
            </label>
          </td>
        </tr>
      )}
    </>
  )
}
