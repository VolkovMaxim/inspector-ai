import { describe, expect, it } from 'vitest'
import type { FindingCard, ParamRow } from '../api/types'
import {
  completenessRows,
  defaultListFor,
  isPending,
  listFor,
  neighbors,
  nextPendingId,
  summaryRows,
} from './findings'

function card(over: Partial<FindingCard> & { id: number }): FindingCard {
  return {
    finding_id: `FND-${over.id}`,
    source: 'matrix',
    code: 'KR-057',
    rule_version: 'matrix-132-r1.1',
    section: 'КР',
    parameter_name: 'Параметр',
    location: 'Локация',
    expected_value: 'А400',
    actual_values: { RD: 'А240' },
    delta: null,
    status: 'CANDIDATE',
    machine_status: 'CANDIDATE',
    finding_status: 'CANDIDATE',
    completeness_status: 'COMPLETE',
    review_priority: 'HIGH',
    criticality: 'Критическое (приостановка работ)',
    rationale: '',
    verification: null,
    discovery_method: null,
    approved_change_ref: 'NONE',
    sources: [
      {
        role: 'expected', stage: 'PD', file_pk: 1, file_id: 'F1', file_name: 'a.pdf', sha256: 'a', document_code: null,
        revision: null, approval_status: 'UNKNOWN', sheet: null, page: 3, bbox: null, bbox_source: 'page', value: 'А400',
        snippet: null,
      },
    ],
    inspector: { status: 'PENDING', user: null, decided_at: null, reason_code: null, comment: null },
    submission: { violation_label: 'VIOLATION_PRESENT', protocol_status: 'CRITICAL', include: true },
    created_in_version: 1,
    ...over,
  }
}

const decided = (id: number, decision: 'CONFIRMED_VIOLATION' | 'NEGATIVE_VERIFIED', extra: Partial<FindingCard> = {}) =>
  card({
    id,
    status: decision,
    inspector: { status: decision, user: 'inspector', decided_at: '2026-09-25T00:00:00Z', reason_code: null, comment: null },
    ...extra,
  })

describe('списки вкладок', () => {
  const cards = [
    card({ id: 1, review_priority: 'LOW', code: 'AR-040' }),
    decided(2, 'CONFIRMED_VIOLATION', { code: 'KR-055' }),
    card({ id: 3, review_priority: 'HIGH', code: 'KR-059' }),
    card({ id: 4, review_priority: 'MEDIUM', code: 'KR-058' }),
    card({ id: 5, status: 'NEGATIVE_VERIFIED', machine_status: 'NEGATIVE_VERIFIED' }),
    decided(6, 'NEGATIVE_VERIFIED'),
    card({ id: 7, status: 'SUSPICION', machine_status: 'SUSPICION' }),
    card({ id: 8, status: 'NOT_COMPARABLE', machine_status: 'NOT_COMPARABLE' }),
    card({ id: 9, status: 'MISSING_EVIDENCE', machine_status: 'MISSING_EVIDENCE', sources: [], location: '' }),
  ]

  it('кандидаты: сначала нерешённые по приоритету, решённые — в конце', () => {
    expect(listFor(cards, 'candidates').map((c) => c.id)).toEqual([3, 4, 1, 2, 6])
  })

  it('подтверждённые и отрицательные — по эффективному статусу', () => {
    expect(listFor(cards, 'confirmed').map((c) => c.id)).toEqual([2])
    expect(listFor(cards, 'negatives').map((c) => c.id).sort()).toEqual([5, 6])
  })

  it('гипотезы не попадают в кандидаты и нарушения', () => {
    expect(listFor(cards, 'hypotheses').map((c) => c.id)).toEqual([7])
    expect(listFor(cards, 'candidates').some((c) => c.id === 7)).toBe(false)
    expect(listFor(cards, 'confirmed').some((c) => c.id === 7)).toBe(false)
  })

  it('комплектность: только карточки со страницами-доказательствами', () => {
    expect(listFor(cards, 'completeness').map((c) => c.id)).toEqual([8])
  })

  it('isPending: только машинный кандидат без решения', () => {
    expect(isPending(cards[0])).toBe(true)
    expect(isPending(cards[1])).toBe(false)
    expect(isPending(cards[6])).toBe(false)
  })

  it('список по умолчанию для карточки', () => {
    expect(defaultListFor(cards[1])).toBe('candidates')
    expect(defaultListFor(cards[4])).toBe('negatives')
    expect(defaultListFor(cards[6])).toBe('hypotheses')
    expect(defaultListFor(cards[7])).toBe('completeness')
  })
})

describe('навигация', () => {
  it('соседи в середине, на краях и вне списка', () => {
    expect(neighbors([10, 20, 30], 20)).toEqual({ index: 1, total: 3, prev: 10, next: 30 })
    expect(neighbors([10, 20, 30], 10)).toMatchObject({ prev: null, next: 20 })
    expect(neighbors([10, 20, 30], 30)).toMatchObject({ prev: 20, next: null })
    expect(neighbors([10, 20, 30], 99)).toEqual({ index: -1, total: 3, prev: null, next: null })
  })

  it('следующий нерешённый — вперёд по кругу, без текущего', () => {
    const ids = [1, 2, 3, 4]
    expect(nextPendingId(ids, new Set([3, 4]), 1)).toBe(3)
    expect(nextPendingId(ids, new Set([1]), 3)).toBe(1)
    expect(nextPendingId(ids, new Set([2]), 2)).toBeNull()
    expect(nextPendingId([], new Set([1]), 1)).toBeNull()
  })
})

describe('сводка', () => {
  it('строки результатов и комплектности отдельно, без гипотез', () => {
    const rows = summaryRows({ CANDIDATE: 2, NEGATIVE_VERIFIED: 1, MISSING_EVIDENCE: 127, SUSPICION: 3 }, {})
    expect(rows.filter((r) => r.group === 'result').map((r) => r.key)).toEqual([
      'CONFIRMED_VIOLATION',
      'CANDIDATE',
      'NEGATIVE_VERIFIED',
    ])
    expect(rows.find((r) => r.key === 'MISSING_EVIDENCE')?.count).toBe(127)
    expect(rows.some((r) => r.key === 'SUSPICION')).toBe(false)
  })

  it('параметры комплектности', () => {
    const params: ParamRow[] = [
      { code: 'A', section: 'КР', name: 'a', status: 'CANDIDATE', comment: '', finding_ids: [] },
      { code: 'B', section: 'КР', name: 'b', status: 'MISSING_EVIDENCE', comment: '', finding_ids: [] },
      { code: 'C', section: 'ПОД', name: 'c', status: 'NOT_APPLICABLE', comment: '', finding_ids: [] },
    ]
    expect(completenessRows(params).map((p) => p.code)).toEqual(['B', 'C'])
  })
})
