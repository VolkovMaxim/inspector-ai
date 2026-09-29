// Статусы: русская подпись, код — в подсказке. Цвет несёт только статус.
import type { Indicator } from '../api/types'
import {
  DECISION_LABEL,
  FINDING_STATUS_LABEL,
  INDICATOR_LABEL,
  PRIORITY_LABEL,
  PROCESS_STATUS_LABEL,
  SYNC_LABEL,
  UPLOAD_STATUS_LABEL,
  label,
} from '../lib/labels'

export function StatusBadge({ status, short }: { status: string; short?: boolean }) {
  let text = label(FINDING_STATUS_LABEL, status)
  if (short && status === 'CONFIRMED_VIOLATION') text = 'Нарушение'
  return (
    <span className={`badge badge--${status}`} title={status}>
      {text}
    </span>
  )
}

export function ProcessStatusBadge({ status }: { status: string }) {
  return (
    <span className={`pstatus pstatus--${status}`} title={status}>
      {status === 'PARSING' ? <span className="pstatus__pulse" aria-hidden /> : null}
      {label(PROCESS_STATUS_LABEL, status)}
    </span>
  )
}

export function DecisionBadge({ status }: { status: string | null | undefined }) {
  const s = status || 'PENDING'
  return (
    <span className={`decision decision--${s}`} title={s}>
      {label(DECISION_LABEL, s)}
    </span>
  )
}

export function UploadStatusBadge({ status }: { status: string }) {
  const kind = status.endsWith('UPLOADED') ? 'ok' : status.endsWith('PARTIAL') ? 'partial' : 'missing'
  return (
    <span className={`ustatus ustatus--${kind}`} title={status}>
      {label(UPLOAD_STATUS_LABEL, status)}
    </span>
  )
}

export function IndicatorDot({ value }: { value: Indicator | string | null | undefined }) {
  const v = (value || 'gray') as string
  const text = INDICATOR_LABEL[v] ?? v
  return <span className={`ind ind--${v}`} title={text} aria-label={text} role="img" />
}

export function PriorityMark({ value }: { value: string | null | undefined }) {
  const v = value || 'LOW'
  return (
    <span className={`prio prio--${v}`} title={`Приоритет рассмотрения: ${v}`}>
      {label(PRIORITY_LABEL, v)}
    </span>
  )
}

export function SyncBadge({ status }: { status: string | null | undefined }) {
  const s = status || 'NONE'
  return (
    <span className={`sync sync--${s}`} title={s}>
      {label(SYNC_LABEL, s)}
    </span>
  )
}

export function StageTag({ stage }: { stage: string | null | undefined }) {
  const s = stage || '—'
  const text = s === 'PD' ? 'ПД' : s === 'RD' ? 'РД' : s === 'ID' ? 'ИД' : s === 'AUTO' ? 'Авто' : s
  return (
    <span className={`stagetag stagetag--${s}`} title={s}>
      {text}
    </span>
  )
}
