import { describe, expect, it } from 'vitest'
import type { PageRow } from '../api/types'
import {
  EMPTY_FILTER,
  applyCorrection,
  applyPageFilter,
  confidenceTone,
  correctionsFor,
  moveFocus,
  orderRows,
  rangeSelect,
} from './pages'

function row(over: Partial<PageRow>): PageRow {
  return {
    page_pk: 1,
    file_pk: 10,
    file_id: 'ИД-001',
    file_name: 'a.pdf',
    stage: 'ID',
    page_no: 1,
    page_class: 'АОСР',
    other_classes: [],
    is_first_page: true,
    section: 'НЕТ',
    quality_label: 'хорошее',
    title: null,
    text_source: 'layer',
    label_source: 'model',
    confidence: 0.95,
    label_model: 'm@1',
    model_class: 'АОСР',
    model_confidence: 0.95,
    corrected: false,
    document_pk: null,
    ...over,
  }
}

const rows = [
  row({ page_pk: 1, page_no: 1 }),
  row({ page_pk: 2, page_no: 2, page_class: 'ПЛАН', is_first_page: false, label_source: 'qwen', model_confidence: 0.4 }),
  row({ page_pk: 3, page_no: 3, page_class: 'ПЛАН', is_first_page: false, label_source: 'inspector', corrected: true, model_confidence: 0.8 }),
  row({ page_pk: 4, file_pk: 11, stage: 'RD', page_no: 1, label_source: null, model_confidence: null, model_class: null }),
]

describe('applyPageFilter', () => {
  it('без фильтров — все', () => {
    expect(applyPageFilter(rows, EMPTY_FILTER)).toHaveLength(4)
  })
  it('стадия, тип, источник «не записан»', () => {
    expect(applyPageFilter(rows, { ...EMPTY_FILTER, stage: 'ID' }).map((r) => r.page_pk)).toEqual([1, 2, 3])
    expect(applyPageFilter(rows, { ...EMPTY_FILTER, cls: 'ПЛАН' }).map((r) => r.page_pk)).toEqual([2, 3])
    expect(applyPageFilter(rows, { ...EMPTY_FILTER, source: 'none' }).map((r) => r.page_pk)).toEqual([4])
  })
  it('неуверенные: строго ниже порога, без уверенности модели — не попадают', () => {
    expect(applyPageFilter(rows, { ...EMPTY_FILTER, confBelow: 0.9 }).map((r) => r.page_pk)).toEqual([2, 3])
    expect(applyPageFilter(rows, { ...EMPTY_FILTER, confBelow: 0.4 })).toEqual([])
  })
  it('исправленные и начальные', () => {
    expect(applyPageFilter(rows, { ...EMPTY_FILTER, correctedOnly: true }).map((r) => r.page_pk)).toEqual([3])
    expect(applyPageFilter(rows, { ...EMPTY_FILTER, firstOnly: true }).map((r) => r.page_pk)).toEqual([1, 4])
  })
})

describe('orderRows', () => {
  it('сначала неуверенные, без уверенности — в конце', () => {
    expect(orderRows(rows, 'confidence').map((r) => r.page_pk)).toEqual([2, 3, 1, 4])
    expect(orderRows(rows, 'file')).toBe(rows)
  })
})

describe('rangeSelect и moveFocus', () => {
  it('диапазон в обе стороны', () => {
    expect(rangeSelect(rows, 1, 3)).toEqual([2, 3, 4])
    expect(rangeSelect(rows, 3, 1)).toEqual([2, 3, 4])
  })
  it('стрелки в сетке 3 колонки, края', () => {
    expect(moveFocus(-1, 'ArrowRight', 3, 7)).toBe(0)
    expect(moveFocus(0, 'ArrowDown', 3, 7)).toBe(3)
    expect(moveFocus(5, 'ArrowDown', 3, 7)).toBe(5)
    expect(moveFocus(1, 'ArrowUp', 3, 7)).toBe(1)
    expect(moveFocus(6, 'ArrowRight', 3, 7)).toBe(6)
    expect(moveFocus(0, 'ArrowLeft', 3, 7)).toBe(0)
    expect(moveFocus(0, 'ArrowRight', 3, 0)).toBe(-1)
  })
})

describe('correctionsFor и applyCorrection', () => {
  it('только реальные изменения', () => {
    const out = correctionsFor(rows.slice(0, 3), { page_class: 'ПЛАН', is_first_page: false })
    expect(out).toEqual([{ page_pk: 1, page_class: 'ПЛАН', is_first_page: false }])
  })
  it('пустая правка — пусто', () => {
    expect(correctionsFor(rows, {})).toEqual([])
  })
  it('после правки: метка человека, модельная уверенность сохранена', () => {
    const r = applyCorrection(rows[1], { page_pk: 2, page_class: 'СХЕМА' })
    expect(r).toMatchObject({ page_class: 'СХЕМА', label_source: 'inspector', corrected: true, model_confidence: 0.4 })
  })
})

describe('confidenceTone', () => {
  it('пороги', () => {
    expect(confidenceTone(0.95)).toBe('ok')
    expect(confidenceTone(0.75)).toBe('warn')
    expect(confidenceTone(0.2)).toBe('bad')
    expect(confidenceTone(null)).toBeNull()
  })
})
