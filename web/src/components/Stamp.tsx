// «Основная надпись»: паспорт документа в виде штампа чертежа — толстая рамка, тонкие внутренние линии,
// мелкие подписи граф. Используется для паспорта доказательства, источников и трассируемости протокола.
import type { CSSProperties, ReactNode } from 'react'

export interface StampCell {
  label: string
  value: ReactNode
  mono?: boolean
  /** сколько колонок сетки занимает графа */
  span?: number
  title?: string
  muted?: boolean
}

export function Stamp({
  cells,
  columns,
  className,
  dense,
}: {
  cells: StampCell[]
  /** grid-template-columns, по умолчанию — равные колонки */
  columns?: string
  className?: string
  dense?: boolean
}) {
  const style: CSSProperties = { gridTemplateColumns: columns ?? `repeat(${Math.min(cells.length, 4)}, minmax(0, 1fr))` }
  return (
    <dl className={`stamp${dense ? ' stamp--dense' : ''}${className ? ` ${className}` : ''}`} style={style}>
      {cells.map((c, i) => (
        <div
          key={`${c.label}-${i}`}
          className={`stamp__cell${c.muted ? ' stamp__cell--muted' : ''}`}
          style={c.span ? { gridColumn: `span ${c.span}` } : undefined}
          title={c.title}
        >
          <dt>{c.label}</dt>
          <dd className={c.mono ? 'mono' : undefined}>{c.value === null || c.value === undefined || c.value === '' ? '—' : c.value}</dd>
        </div>
      ))}
    </dl>
  )
}
