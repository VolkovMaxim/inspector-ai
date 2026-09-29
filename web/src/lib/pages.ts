// Вкладка «Страницы»: фильтры, порядок, выделение и навигация по сетке — без React, чтобы проверять тестами.
import type { PageCorrectionIn, PageRow } from '../api/types'

export interface PageFilter {
  stage: string
  filePk: number | null
  cls: string
  /** model | qwen | inspector | none | '' */
  source: string
  /** уверенность классификатора ниже порога (0..1); null — любая */
  confBelow: number | null
  correctedOnly: boolean
  firstOnly: boolean
}

export const EMPTY_FILTER: PageFilter = {
  stage: '',
  filePk: null,
  cls: '',
  source: '',
  confBelow: null,
  correctedOnly: false,
  firstOnly: false,
}

export type PageOrder = 'file' | 'confidence'

/** Фильтры без типа и источника — от них считаются числа в выпадающих списках. */
export function scopeRows(rows: PageRow[], f: PageFilter): PageRow[] {
  return rows.filter((r) => (!f.stage || r.stage === f.stage) && (f.filePk === null || r.file_pk === f.filePk))
}

export function applyPageFilter(rows: PageRow[], f: PageFilter): PageRow[] {
  return scopeRows(rows, f).filter((r) => {
    if (f.cls && (r.page_class ?? '') !== f.cls) return false
    if (f.source && (r.label_source ?? 'none') !== f.source) return false
    if (f.confBelow !== null && !(r.model_confidence !== null && r.model_confidence < f.confBelow)) return false
    if (f.correctedOnly && !r.corrected) return false
    if (f.firstOnly && !r.is_first_page) return false
    return true
  })
}

/** «Сначала неуверенные»: по уверенности классификатора, страницы без неё — в конце, дальше по файлу и номеру. */
export function orderRows(rows: PageRow[], order: PageOrder): PageRow[] {
  if (order === 'file') return rows
  return [...rows].sort((a, b) => {
    const ca = a.model_confidence ?? 2
    const cb = b.model_confidence ?? 2
    return ca - cb || a.file_pk - b.file_pk || a.page_no - b.page_no
  })
}

export function countBy<T>(rows: T[], key: (r: T) => string): Map<string, number> {
  const m = new Map<string, number>()
  for (const r of rows) m.set(key(r), (m.get(key(r)) ?? 0) + 1)
  return m
}

/** Диапазон выделения Shift: от якоря до цели включительно, в порядке показа. */
export function rangeSelect(rows: PageRow[], anchor: number, target: number): number[] {
  const [a, b] = anchor <= target ? [anchor, target] : [target, anchor]
  return rows.slice(Math.max(0, a), Math.min(rows.length, b + 1)).map((r) => r.page_pk)
}

/** Сдвиг фокуса в сетке из cols колонок; за край не выходит. */
export function moveFocus(index: number, key: string, cols: number, total: number): number {
  if (total <= 0) return -1
  const c = Math.max(1, cols)
  const step = key === 'ArrowLeft' ? -1 : key === 'ArrowRight' ? 1 : key === 'ArrowUp' ? -c : key === 'ArrowDown' ? c : 0
  if (index < 0) return 0
  const next = index + step
  if (next < 0) return key === 'ArrowUp' ? index : 0
  if (next >= total) return key === 'ArrowDown' ? index : total - 1
  return next
}

export interface CorrectionPatch {
  page_class?: string
  is_first_page?: boolean
  section?: string
}

export function isEmptyPatch(p: CorrectionPatch): boolean {
  return p.page_class === undefined && p.is_first_page === undefined && p.section === undefined
}

/** Правки только там, где значение действительно меняется: нет изменений — нет строки обучения. */
export function correctionsFor(rows: PageRow[], patch: CorrectionPatch): PageCorrectionIn[] {
  const out: PageCorrectionIn[] = []
  for (const r of rows) {
    const item: PageCorrectionIn = { page_pk: r.page_pk }
    if (patch.page_class !== undefined && patch.page_class !== r.page_class) item.page_class = patch.page_class
    if (patch.is_first_page !== undefined && patch.is_first_page !== !!r.is_first_page) item.is_first_page = patch.is_first_page
    if (patch.section !== undefined && patch.section !== r.section) item.section = patch.section
    if (Object.keys(item).length > 1) out.push(item)
  }
  return out
}

/** Строка после правки: метка человека, уверенность модели остаётся для сравнения. */
export function applyCorrection(r: PageRow, c: PageCorrectionIn): PageRow {
  return {
    ...r,
    page_class: c.page_class ?? r.page_class,
    is_first_page: c.is_first_page ?? r.is_first_page,
    section: c.section ?? r.section,
    label_source: 'inspector',
    confidence: null,
    corrected: true,
  }
}

/** Тон уверенности: ≥ 0,9 — спокойно, 0,7–0,9 — присмотреться, ниже — проверить. */
export function confidenceTone(p: number | null): 'ok' | 'warn' | 'bad' | null {
  if (p === null || p === undefined) return null
  if (p >= 0.9) return 'ok'
  if (p >= 0.7) return 'warn'
  return 'bad'
}

export function pct(p: number | null): string {
  return p === null || p === undefined ? '—' : `${Math.round(p * 100)} %`
}
