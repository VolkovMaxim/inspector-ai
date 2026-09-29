// Правка параметра матрицы: черновик формы → только изменённые поля (сервер пишет в журнал старое и новое).
import type { Param, ParamPatch } from '../api/types'

export type Draft = {
  trigger_logic: string
  min_value: string
  max_value: string
  is_active: boolean
  review_priority: string
  sp_reference: string
  gost_reference: string
  fz_reference: string
  other_normative: string
}

export function toDraft(p: Param): Draft {
  return {
    trigger_logic: p.trigger_logic ?? '',
    min_value: p.min_value === null || p.min_value === undefined ? '' : String(p.min_value),
    max_value: p.max_value === null || p.max_value === undefined ? '' : String(p.max_value),
    is_active: p.is_active,
    review_priority: String(p.review_priority || 'MEDIUM'),
    sp_reference: p.sp_reference ?? '',
    gost_reference: p.gost_reference ?? '',
    fz_reference: p.fz_reference ?? '',
    other_normative: p.other_normative ?? '',
  }
}

export function parseNum(s: string): number | null | 'bad' {
  const t = s.trim().replace(',', '.')
  if (!t) return null
  const n = Number(t)
  return Number.isFinite(n) ? n : 'bad'
}

/** Только изменённые поля — сервер пишет в журнал старое и новое значение. */
export function diffParam(p: Param, d: Draft): ParamPatch | string {
  const patch: ParamPatch = {}
  const min = parseNum(d.min_value)
  const max = parseNum(d.max_value)
  if (min === 'bad' || max === 'bad') return 'Пороги — числа (можно с запятой) или пусто'
  if (min !== null && max !== null && min > max) return 'Минимум больше максимума'
  if (d.trigger_logic !== (p.trigger_logic ?? '')) patch.trigger_logic = d.trigger_logic
  if (min !== (p.min_value ?? null)) patch.min_value = min
  if (max !== (p.max_value ?? null)) patch.max_value = max
  if (d.is_active !== p.is_active) patch.is_active = d.is_active
  if (d.review_priority !== String(p.review_priority)) patch.review_priority = d.review_priority
  for (const k of ['sp_reference', 'gost_reference', 'fz_reference', 'other_normative'] as const) {
    if (d[k] !== (p[k] ?? '')) patch[k] = d[k]
  }
  return patch
}
