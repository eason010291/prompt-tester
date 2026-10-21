import {
  DndContext,
  KeyboardSensor,
  PointerSensor,
  closestCenter,
  useSensor,
  useSensors,
  type DragEndEvent,
} from '@dnd-kit/core'
import { SortableContext, arrayMove, sortableKeyboardCoordinates, verticalListSortingStrategy } from '@dnd-kit/sortable'
import { newBlock, type Block } from '../types'
import BlockCard from './BlockCard'

interface Props {
  blocks: Block[]
  blockTokens: Record<string, number>
  highlightId: string | null
  onChange: (blocks: Block[]) => void
}

export default function BlockList({ blocks, blockTokens, highlightId, onChange }: Props) {
  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 4 } }),
    useSensor(KeyboardSensor, { coordinateGetter: sortableKeyboardCoordinates }),
  )

  const patch = (id: string, p: Partial<Block>) => onChange(blocks.map((b) => (b.id === id ? { ...b, ...p } : b)))

  // 互斥變體群組：開啟一個時關掉同群組的其他區塊
  const toggle = (block: Block, enabled: boolean) =>
    onChange(
      blocks.map((b) => {
        if (b.id === block.id) return { ...b, enabled }
        if (enabled && block.group && b.group === block.group) return { ...b, enabled: false }
        return b
      }),
    )

  const move = (id: string, delta: -1 | 1) => {
    const i = blocks.findIndex((b) => b.id === id)
    const j = i + delta
    if (j < 0 || j >= blocks.length) return
    onChange(arrayMove(blocks, i, j))
  }

  const duplicate = (id: string) => {
    const i = blocks.findIndex((b) => b.id === id)
    const copy = { ...blocks[i], id: crypto.randomUUID(), name: `${blocks[i].name}（複製）` }
    onChange([...blocks.slice(0, i + 1), copy, ...blocks.slice(i + 1)])
  }

  const remove = (id: string) => {
    const b = blocks.find((x) => x.id === id)
    if (b && b.content.trim() && !window.confirm(`刪除區塊「${b.name}」？`)) return
    onChange(blocks.filter((x) => x.id !== id))
  }

  // 分隔區塊到下一個分隔之間的內容區塊
  const sectionOf = (dividerId: string) => {
    const i = blocks.findIndex((b) => b.id === dividerId)
    const ids: string[] = []
    for (let j = i + 1; j < blocks.length && blocks[j].kind !== 'divider'; j++) {
      if (blocks[j].kind === 'content') ids.push(blocks[j].id)
    }
    return ids
  }

  const isSectionCollapsed = (dividerId: string) => {
    const ids = sectionOf(dividerId)
    return ids.length > 0 && ids.every((id) => blocks.find((b) => b.id === id)?.collapsed)
  }

  const toggleSection = (dividerId: string) => {
    const ids = new Set(sectionOf(dividerId))
    const collapse = !isSectionCollapsed(dividerId)
    onChange(blocks.map((b) => (ids.has(b.id) ? { ...b, collapsed: collapse } : b)))
  }

  const onDragEnd = ({ active, over }: DragEndEvent) => {
    if (!over || active.id === over.id) return
    const from = blocks.findIndex((b) => b.id === active.id)
    const to = blocks.findIndex((b) => b.id === over.id)
    onChange(arrayMove(blocks, from, to))
  }

  const hasHistory = blocks.some((b) => b.kind === 'marker' && b.marker === 'chatHistory')
  const add = (b: Block) => onChange([...blocks, b])

  return (
    <div className="block-list">
      <DndContext sensors={sensors} collisionDetection={closestCenter} onDragEnd={onDragEnd}>
        <SortableContext items={blocks.map((b) => b.id)} strategy={verticalListSortingStrategy}>
          {blocks.map((b) => (
            <BlockCard
              key={b.id}
              block={b}
              tokens={blockTokens[b.id]}
              highlighted={highlightId === b.id}
              onChange={(p) => patch(b.id, p)}
              onToggle={(enabled) => toggle(b, enabled)}
              onDuplicate={() => duplicate(b.id)}
              onDelete={() => remove(b.id)}
              onMove={(d) => move(b.id, d)}
              onToggleSection={b.kind === 'divider' ? () => toggleSection(b.id) : undefined}
              sectionCollapsed={b.kind === 'divider' ? isSectionCollapsed(b.id) : undefined}
            />
          ))}
        </SortableContext>
      </DndContext>

      <div className="add-bar">
        <button className="btn" onClick={() => add(newBlock())}>
          ＋ 內容區塊
        </button>
        <button className="btn" onClick={() => add(newBlock({ kind: 'divider', name: '===新段落===', enabled: false }))}>
          ＋ 分隔
        </button>
        <button
          className="btn"
          disabled={hasHistory}
          title={hasHistory ? '已有聊天歷史 marker' : undefined}
          onClick={() => add(newBlock({ kind: 'marker', marker: 'chatHistory', name: '聊天歷史' }))}
        >
          ＋ 聊天歷史
        </button>
      </div>
    </div>
  )
}
