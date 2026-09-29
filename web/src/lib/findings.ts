// Списки карточек для вкладок и навигации рабочего места: чистые функции без I/O.
import type { FindingCard, FindingStatus, ParamRow } from '../api/types'

export type ListKey = 'candidates' | 'confirmed' | 'negatives' | 'completeness' | 'hypotheses' | 'all'

export const LIST_TITLE: Record<ListKey, string> = {
  candidates: 'Кандидаты',
  confirmed: 'Подтверждённые',
  negatives: 'Проверенные отрицательные',
  completeness: 'Комплектность',
  hypotheses: 'Гипотезы',
  all: 'Все карточки',
}

export const COMPLETENESS_STATUSES: FindingStatus[] = [
  'MISSING_EVIDENCE',
  'NOT_APPLICABLE',
  'NOT_COMPARABLE',
  'CLARIFICATION_REQUIRED',
]

const PRIORITY_RANK: Record<string, number> = { HIGH: 0, MEDIUM: 1, LOW: 2 }

export function isListKey(v: string | null | undefined): v is ListKey {
  return !!v && v in LIST_TITLE
}

/** Кандидат, по которому инспектор ещё не решил. */
export function isPending(c: FindingCard): boolean {
  return c.machine_status === 'CANDIDATE' && (c.inspector?.status ?? 'PENDING') === 'PENDING'
}

/** Машинный кандидат (в т.ч. уже решённый инспектором). */
export function isCandidate(c: FindingCard): boolean {
  return c.machine_status === 'CANDIDATE'
}

export function hasPageEvidence(c: FindingCard): boolean {
  return c.sources.some((s) => !!s.file_pk && !!s.page)
}

function byPriorityThenCode(a: FindingCard, b: FindingCard): number {
  return (
    (PRIORITY_RANK[a.review_priority] ?? 3) - (PRIORITY_RANK[b.review_priority] ?? 3) ||
    a.code.localeCompare(b.code, 'ru') ||
    a.location.localeCompare(b.location, 'ru') ||
    a.id - b.id
  )
}

export function sortCandidates(cards: FindingCard[]): FindingCard[] {
  return [...cards].sort((a, b) => Number(!isPending(a)) - Number(!isPending(b)) || byPriorityThenCode(a, b))
}

export function listFor(cards: FindingCard[], key: ListKey): FindingCard[] {
  switch (key) {
    case 'candidates':
      return sortCandidates(cards.filter(isCandidate))
    case 'confirmed':
      return cards.filter((c) => c.status === 'CONFIRMED_VIOLATION').sort(byPriorityThenCode)
    case 'negatives':
      return cards.filter((c) => c.status === 'NEGATIVE_VERIFIED').sort(byPriorityThenCode)
    case 'completeness':
      return cards
        .filter((c) => COMPLETENESS_STATUSES.includes(c.status) && hasPageEvidence(c))
        .sort(byPriorityThenCode)
    case 'hypotheses':
      return cards.filter((c) => c.status === 'SUSPICION').sort(byPriorityThenCode)
    case 'all':
      return cards.filter(hasPageEvidence).sort(byPriorityThenCode)
  }
}

/** Какой список подходит карточке, если в адресе его нет. */
export function defaultListFor(c: FindingCard): ListKey {
  if (c.machine_status === 'CANDIDATE') return 'candidates'
  if (c.status === 'SUSPICION') return 'hypotheses'
  if (c.status === 'NEGATIVE_VERIFIED') return 'negatives'
  if (c.status === 'CONFIRMED_VIOLATION') return 'confirmed'
  return 'completeness'
}

export interface Neighbors {
  index: number
  total: number
  prev: number | null
  next: number | null
}

export function neighbors(ids: number[], id: number): Neighbors {
  const index = ids.indexOf(id)
  if (index < 0) return { index: -1, total: ids.length, prev: null, next: null }
  return {
    index,
    total: ids.length,
    prev: index > 0 ? ids[index - 1] : null,
    next: index < ids.length - 1 ? ids[index + 1] : null,
  }
}

/** Следующий нерешённый после текущего (по кругу), иначе null. */
export function nextPendingId(ids: number[], pending: Set<number>, current: number): number | null {
  if (!ids.length) return null
  const start = ids.indexOf(current)
  for (let step = 1; step <= ids.length; step++) {
    const id = ids[(Math.max(start, 0) + step) % ids.length]
    if (id !== current && pending.has(id)) return id
  }
  return null
}

export function completenessRows(params: ParamRow[]): ParamRow[] {
  return params.filter((p) => COMPLETENESS_STATUSES.includes(p.status))
}

/** Строки сводной статистики (Приложение 2, раздел 2): статусы параметров матрицы. */
export interface SummaryRow {
  key: string
  label: string
  count: number
  group: 'result' | 'completeness'
}

export function summaryRows(summary: Record<string, number>, labels: Record<string, string>): SummaryRow[] {
  const result: FindingStatus[] = ['CONFIRMED_VIOLATION', 'CANDIDATE', 'NEGATIVE_VERIFIED']
  return [
    ...result.map((s) => ({ key: s, label: labels[s] ?? s, count: summary[s] ?? 0, group: 'result' as const })),
    ...COMPLETENESS_STATUSES.map((s) => ({
      key: s,
      label: labels[s] ?? s,
      count: summary[s] ?? 0,
      group: 'completeness' as const,
    })),
  ]
}
