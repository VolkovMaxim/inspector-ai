// Общие ячейки и фильтр для таблиц находок.
import { useMemo, useState, type ReactNode } from 'react'
import type { FindingCard } from '../../api/types'

export function Value({ v }: { v: string | null | undefined }) {
  if (v === null || v === undefined || v === '') return <span className="valuecell valuecell--empty">—</span>
  return (
    <span className="valuecell" title={v}>
      {v}
    </span>
  )
}

export function CodeCell({ c }: { c: FindingCard }) {
  return (
    <>
      <span className="mono">{c.code}</span>
      <span className="cell-sub">{c.section}</span>
    </>
  )
}

export function ParamCell({ c }: { c: FindingCard }) {
  return (
    <>
      <span>{c.parameter_name}</span>
      {c.location ? <span className="cell-sub">{c.location}</span> : null}
    </>
  )
}

export interface FindingFilter {
  q: string
  section: string
}

export function applyFilter(cards: FindingCard[], f: FindingFilter): FindingCard[] {
  const q = f.q.trim().toLocaleLowerCase('ru')
  return cards.filter((c) => {
    if (f.section && c.section !== f.section) return false
    if (!q) return true
    const hay = [c.code, c.parameter_name, c.location, c.expected_value, c.actual_values.RD, c.actual_values.ID, c.finding_id]
    return hay.some((v) => v && v.toLocaleLowerCase('ru').includes(q))
  })
}

export function useFindingFilter(cards: FindingCard[]) {
  const [filter, setFilter] = useState<FindingFilter>({ q: '', section: '' })
  const sections = useMemo(() => Array.from(new Set(cards.map((c) => c.section).filter(Boolean))).sort((a, b) => a.localeCompare(b, 'ru')), [cards])
  const shown = useMemo(() => applyFilter(cards, filter), [cards, filter])
  return { filter, setFilter, sections, shown }
}

export function FilterBar({
  filter,
  setFilter,
  sections,
  shown,
  total,
  children,
}: {
  filter: FindingFilter
  setFilter: (f: FindingFilter) => void
  sections: string[]
  shown: number
  total: number
  children?: ReactNode
}) {
  return (
    <div className="toolbar">
      <input
        className="input"
        type="search"
        placeholder="Код, параметр, локация или значение"
        value={filter.q}
        onChange={(e) => setFilter({ ...filter, q: e.target.value })}
        aria-label="Поиск по карточкам"
      />
      {sections.length > 1 ? (
        <select className="select" value={filter.section} onChange={(e) => setFilter({ ...filter, section: e.target.value })} aria-label="Раздел">
          <option value="">Все разделы</option>
          {sections.map((s) => (
            <option key={s} value={s}>
              {s}
            </option>
          ))}
        </select>
      ) : null}
      {children}
      <span className="toolbar__spacer" />
      <span className="muted num">{shown === total ? `Всего: ${total}` : `Показано ${shown} из ${total}`}</span>
    </div>
  )
}
