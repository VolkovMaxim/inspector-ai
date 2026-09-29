import { describe, expect, it } from 'vitest'
import type { Param } from '../api/types'
import { diffParam, parseNum, toDraft } from './params'

const base: Param = {
  code: 'KR-058',
  section: 'КР',
  parameter_name: 'Толщина фундаментной плиты',
  unit: 'мм',
  source_pd: '',
  source_rd: '',
  source_id: '',
  trigger_logic: 'Уменьшение толщины',
  criticality: 'Критическое',
  review_priority: 'HIGH',
  data_type: 'number',
  min_value: null,
  max_value: 900,
  sp_reference: 'СП 63.13330',
  gost_reference: '',
  fz_reference: '',
  other_normative: '',
  is_active: true,
}

describe('parseNum', () => {
  it('число с запятой, пусто и мусор', () => {
    expect(parseNum('12,5')).toBe(12.5)
    expect(parseNum('  ')).toBeNull()
    expect(parseNum('abc')).toBe('bad')
  })
})

describe('diffParam', () => {
  it('без изменений — пустой патч', () => {
    expect(diffParam(base, toDraft(base))).toEqual({})
  })

  it('только изменённые поля, пороги — числами', () => {
    const d = { ...toDraft(base), min_value: '400', trigger_logic: 'Любое отличие', is_active: false }
    expect(diffParam(base, d)).toEqual({ min_value: 400, trigger_logic: 'Любое отличие', is_active: false })
  })

  it('очистка порога → null', () => {
    expect(diffParam(base, { ...toDraft(base), max_value: '' })).toEqual({ max_value: null })
  })

  it('отказ: минимум больше максимума или не число', () => {
    expect(diffParam(base, { ...toDraft(base), min_value: '1000' })).toBe('Минимум больше максимума')
    expect(typeof diffParam(base, { ...toDraft(base), max_value: 'много' })).toBe('string')
  })
})
