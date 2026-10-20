import { diffChars } from 'diff'
import { useState } from 'react'
import type { Block, BlockDiff, PresetDiff } from '../types'

const STATUS_LABEL: Record<string, string> = {
  added: '新增',
  removed: '刪除',
  modified: '修改',
  toggled: '開關',
  moved: '移動',
  unchanged: '未變',
}

const FIELD_LABEL: Record<string, string> = {
  name: '名稱',
  kind: '類型',
  marker: 'marker',
  role: '角色',
  content: '內容',
  injection_position: '位置',
  injection_depth: '深度',
  injection_order: '順序',
  group: '變體群組',
  enabled: '啟用',
}

interface Props {
  diff: PresetDiff
  /** 左右兩邊的標題，例如「a1b2c3d4」與「工作區」 */
  labels: [string, string]
  /** 提供時，每個區塊可把左邊（a）的版本套用到工作區 */
  onApplyOld?: (entry: BlockDiff) => void
}

export default function DiffView({ diff, labels, onApplyOld }: Props) {
  const [showUnchanged, setShowUnchanged] = useState(false)
  const changed = diff.blocks.filter((b) => !b.status.includes('unchanged'))
  const shown = showUnchanged ? diff.blocks : changed
  const s = diff.summary
  const total = s.added + s.removed + s.modified + s.toggled + s.moved

  return (
    <div className="diff-view">
      <div className="diff-summary">
        <span className="muted small">
          {labels[0]} → {labels[1]}
        </span>
        {total === 0 && Object.keys(diff.params).length === 0 && Object.keys(diff.options).length === 0 && !diff.name && !diff.extra_changed ? (
          <span className="chip">完全相同</span>
        ) : (
          (['added', 'removed', 'modified', 'toggled', 'moved'] as const).map(
            (k) =>
              s[k] > 0 && (
                <span key={k} className={`chip st-${k}`}>
                  {STATUS_LABEL[k]} {s[k]}
                </span>
              ),
          )
        )}
        <label className="check small">
          <input type="checkbox" checked={showUnchanged} onChange={(e) => setShowUnchanged(e.target.checked)} />
          顯示未變區塊
        </label>
      </div>

      {(diff.name || Object.keys(diff.params).length > 0 || Object.keys(diff.options).length > 0 || diff.extra_changed) && (
        <table className="ep-table diff-fields">
          <tbody>
            {diff.name && <FieldRow label="預設名稱" pair={diff.name} />}
            {Object.entries(diff.params).map(([k, v]) => (
              <FieldRow key={k} label={`參數 ${k}`} pair={v} />
            ))}
            {Object.entries(diff.options).map(([k, v]) => (
              <FieldRow key={k} label={`選項 ${k}`} pair={v} />
            ))}
            {diff.extra_changed && (
              <tr>
                <td>其他欄位</td>
                <td colSpan={2}>有變更（例如正則腳本或 ST 專屬設定）</td>
              </tr>
            )}
          </tbody>
        </table>
      )}

      <ol className="diff-blocks">
        {shown.map((b) => (
          <DiffBlock key={b.id + b.status.join()} entry={b} onApplyOld={onApplyOld} />
        ))}
      </ol>
    </div>
  )
}

function FieldRow({ label, pair }: { label: string; pair: [unknown, unknown] }) {
  return (
    <tr>
      <td>{label}</td>
      <td className="old">{String(pair[0])}</td>
      <td className="new">{String(pair[1])}</td>
    </tr>
  )
}

function DiffBlock({ entry, onApplyOld }: { entry: BlockDiff; onApplyOld?: (e: BlockDiff) => void }) {
  const [open, setOpen] = useState(entry.status.includes('modified') && !!entry.changes.content)
  const fields = Object.entries(entry.changes).filter(([k]) => k !== 'content')
  const block = (entry.new ?? entry.old) as Block

  return (
    <li className={`diff-block ${entry.status.map((s) => `st-${s}`).join(' ')}`}>
      <div className="diff-head">
        <button className="link-btn" onClick={() => setOpen((v) => !v)}>
          {open ? '▾' : '▸'} {entry.name || block.id}
        </button>
        {entry.status.map((s) => (
          <span key={s} className={`chip st-${s}`}>
            {STATUS_LABEL[s]}
          </span>
        ))}
        {entry.status.includes('moved') && (
          <span className="muted small">
            #{(entry.old_index ?? 0) + 1} → #{(entry.new_index ?? 0) + 1}
          </span>
        )}
        {fields.map(([k, [a, b]]) => (
          <span key={k} className="muted small">
            {FIELD_LABEL[k] ?? k}：{String(a)} → {String(b)}
          </span>
        ))}
        {onApplyOld && entry.old && !entry.status.includes('unchanged') && (
          <button className="btn btn-sm apply-old" onClick={() => onApplyOld(entry)} title="把左邊版本的這個區塊套用到工作區">
            套用此版本
          </button>
        )}
      </div>
      {open && <ContentDiff a={entry.old?.content ?? ''} b={entry.new?.content ?? ''} />}
    </li>
  )
}

function ContentDiff({ a, b }: { a: string; b: string }) {
  if (a === b) return <pre className="content-diff">{a}</pre>
  const parts = diffChars(a, b)
  return (
    <pre className="content-diff">
      {parts.map((p, i) =>
        p.added ? (
          <ins key={i}>{p.value}</ins>
        ) : p.removed ? (
          <del key={i}>{p.value}</del>
        ) : (
          <span key={i}>{p.value.length > 400 ? p.value.slice(0, 180) + ' … ' + p.value.slice(-180) : p.value}</span>
        ),
      )}
    </pre>
  )
}
