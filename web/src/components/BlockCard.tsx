import { useSortable } from '@dnd-kit/sortable'
import { CSS } from '@dnd-kit/utilities'
import { useState } from 'react'
import { POSITION_IN_CHAT, POSITION_RELATIVE, type Block, type Role } from '../types'

interface Props {
  block: Block
  tokens: number | undefined
  highlighted: boolean
  onChange: (patch: Partial<Block>) => void
  onToggle: (enabled: boolean) => void
  onDuplicate: () => void
  onDelete: () => void
  onMove: (delta: -1 | 1) => void
  onToggleSection?: () => void
  sectionCollapsed?: boolean
}

const ROLES: Role[] = ['system', 'user', 'assistant']

export default function BlockCard(props: Props) {
  const { block, tokens, highlighted, onChange } = props
  const { attributes, listeners, setNodeRef, setActivatorNodeRef, transform, transition, isDragging } = useSortable({
    id: block.id,
  })
  const [showAdvanced, setShowAdvanced] = useState(false)

  const style = { transform: CSS.Translate.toString(transform), transition }
  const classes = [
    'block',
    `kind-${block.kind}`,
    block.enabled ? '' : 'is-disabled',
    isDragging ? 'is-dragging' : '',
    highlighted ? 'is-highlighted' : '',
  ].join(' ')

  const handle = (
    <button
      ref={setActivatorNodeRef}
      className="icon-btn drag-handle"
      aria-label="拖曳排序"
      title="拖曳排序（也可用鍵盤：空白鍵抓取、方向鍵移動）"
      {...attributes}
      {...listeners}
    >
      ⠿
    </button>
  )

  if (block.kind === 'divider') {
    return (
      <div ref={setNodeRef} style={style} className={classes} id={`block-${block.id}`}>
        {handle}
        <button
          className="icon-btn"
          aria-label={props.sectionCollapsed ? '展開此段' : '摺疊此段'}
          title={props.sectionCollapsed ? '展開此段所有區塊' : '摺疊此段所有區塊'}
          onClick={props.onToggleSection}
        >
          {props.sectionCollapsed ? '▸' : '▾'}
        </button>
        <input
          className="divider-name"
          value={block.name}
          aria-label="分隔標題"
          onChange={(e) => onChange({ name: e.target.value })}
        />
        <BlockMenu {...props} />
      </div>
    )
  }

  const isMarker = block.kind === 'marker'
  const inChat = block.injection_position === POSITION_IN_CHAT

  return (
    <article ref={setNodeRef} style={style} className={classes} id={`block-${block.id}`}>
      <header className="block-head">
        {handle}
        <label className="switch" title={block.enabled ? '停用' : '啟用'}>
          <input type="checkbox" checked={block.enabled} onChange={(e) => props.onToggle(e.target.checked)} />
          <span aria-hidden />
          <span className="sr-only">啟用</span>
        </label>
        <input
          className="block-name"
          value={block.name}
          aria-label="區塊名稱"
          onChange={(e) => onChange({ name: e.target.value })}
        />
        {isMarker ? (
          <span className="chip chip-marker">聊天歷史插入點</span>
        ) : (
          <>
            {inChat && <span className="chip chip-inject">深度 {block.injection_depth}</span>}
            {block.group && <span className="chip chip-group">⇄ {block.group}</span>}
            <select
              className={`role-select role-${block.role}`}
              value={block.role}
              aria-label="角色"
              onChange={(e) => onChange({ role: e.target.value as Role })}
            >
              {ROLES.map((r) => (
                <option key={r} value={r}>
                  {r}
                </option>
              ))}
            </select>
          </>
        )}
        <span className="token-count" title="展開巨集後的 token 估算">
          {block.enabled && tokens !== undefined ? `${tokens} tok` : ''}
        </span>
        {!isMarker && (
          <button
            className="icon-btn"
            aria-label="進階設定"
            aria-pressed={showAdvanced}
            title="位置、深度、變體群組"
            onClick={() => setShowAdvanced((v) => !v)}
          >
            ⚙
          </button>
        )}
        {!isMarker && (
          <button
            className="icon-btn"
            aria-label={block.collapsed ? '展開' : '摺疊'}
            onClick={() => onChange({ collapsed: !block.collapsed })}
          >
            {block.collapsed ? '▸' : '▾'}
          </button>
        )}
        <BlockMenu {...props} />
      </header>

      {showAdvanced && !isMarker && (
        <div className="block-advanced">
          <label>
            位置
            <select
              value={block.injection_position}
              onChange={(e) => onChange({ injection_position: Number(e.target.value) })}
            >
              <option value={POSITION_RELATIVE}>相對（依列表位置）</option>
              <option value={POSITION_IN_CHAT}>深度注入（插入聊天歷史）</option>
            </select>
          </label>
          {inChat && (
            <>
              <label>
                深度
                <input
                  type="number"
                  min={0}
                  value={block.injection_depth}
                  onChange={(e) => onChange({ injection_depth: Math.max(0, Number(e.target.value)) })}
                />
              </label>
              <label>
                順序
                <input
                  type="number"
                  value={block.injection_order}
                  onChange={(e) => onChange({ injection_order: Number(e.target.value) })}
                />
              </label>
            </>
          )}
          <label>
            變體群組
            <input
              value={block.group}
              placeholder="同群組只能開一個"
              onChange={(e) => onChange({ group: e.target.value })}
            />
          </label>
        </div>
      )}

      {!isMarker && !block.collapsed && (
        <textarea
          className="block-text"
          value={block.content}
          spellCheck={false}
          placeholder="區塊內容，可使用 {{char}}、{{user}}、{{random::a::b}} 等巨集"
          aria-label={`${block.name} 內容`}
          onChange={(e) => onChange({ content: e.target.value })}
        />
      )}
      {!isMarker && block.collapsed && block.content && (
        <p className="block-excerpt" onClick={() => onChange({ collapsed: false })}>
          {block.content.slice(0, 120)}
        </p>
      )}
    </article>
  )
}

function BlockMenu({ onDuplicate, onDelete, onMove, block }: Props) {
  return (
    <details className="menu">
      <summary className="icon-btn" aria-label="更多操作">
        ⋯
      </summary>
      <div className="menu-pop" role="menu">
        <button role="menuitem" onClick={() => onMove(-1)}>
          上移
        </button>
        <button role="menuitem" onClick={() => onMove(1)}>
          下移
        </button>
        {block.kind !== 'marker' && (
          <button role="menuitem" onClick={onDuplicate}>
            複製
          </button>
        )}
        <button role="menuitem" className="danger" onClick={onDelete}>
          刪除
        </button>
      </div>
    </details>
  )
}
