import { describe, expect, it } from 'vitest'
import { buildUrl, errorFromBody, filenameFromDisposition } from '../api/client'
import { formatAgo, formatBytes, formatSeconds, percent, plural, shortHash } from './format'

describe('formatBytes', () => {
  it('двоичные единицы по-русски', () => {
    expect(formatBytes(512)).toBe('512 Б')
    expect(formatBytes(1536)).toBe('1,5 КБ')
    expect(formatBytes(50 * 1024 * 1024)).toBe('50 МБ')
    expect(formatBytes(250 * 1024 * 1024)).toBe('250 МБ')
  })
  it('нет значения — прочерк', () => {
    expect(formatBytes(null)).toBe('—')
    expect(formatBytes(Number.NaN)).toBe('—')
  })
})

describe('percent', () => {
  it('доля от числа параметров', () => {
    expect(percent(2, 132)).toBe('1,5 %')
    expect(percent(132, 132)).toBe('100 %')
    expect(percent(0, 132)).toBe('0 %')
  })
  it('ноль в знаменателе и очень малые доли', () => {
    expect(percent(1, 0)).toBe('—')
    expect(percent(1, 5000)).toBe('< 0,1 %')
  })
})

describe('formatSeconds', () => {
  it('секунды, минуты, часы', () => {
    const nb = (x: string) => x.replace(/\u00a0/g, ' ')
    expect(nb(formatSeconds(12.4))).toBe('12 с')
    expect(nb(formatSeconds(200))).toBe('3 мин 20 с')
    expect(nb(formatSeconds(3900))).toBe('1 ч 05 мин')
    expect(formatSeconds(200)).toContain('3\u00a0мин')
    expect(formatSeconds(undefined)).toBe('')
  })
})

describe('plural', () => {
  it('склоняет существительное по числу', () => {
    const forms: [string, string, string] = ['файл', 'файла', 'файлов']
    expect(plural(1, forms)).toBe('файл')
    expect(plural(3, forms)).toBe('файла')
    expect(plural(5, forms)).toBe('файлов')
    expect(plural(11, forms)).toBe('файлов')
    expect(plural(21, forms)).toBe('файл')
    expect(plural(112, forms)).toBe('файлов')
  })
})

describe('shortHash и formatAgo', () => {
  it('укорачивает SHA-256', () => {
    expect(shortHash('a'.repeat(64))).toBe(`${'a'.repeat(12)}…`)
    expect(shortHash(null)).toBe('—')
  })
  it('относительное время', () => {
    const now = Date.parse('2026-09-25T03:10:00Z')
    expect(formatAgo('2026-09-25T03:09:50Z', now)).toBe('только что')
    expect(formatAgo('2026-09-25T02:40:00Z', now)).toBe('30 мин назад')
    expect(formatAgo('garbage', now)).toBe('—')
  })
})

describe('api client helpers', () => {
  it('buildUrl добавляет префикс /api/v1 и пропускает пустые параметры', () => {
    expect(buildUrl('/processes', { status: 'READY', q: '', limit: 50, offset: undefined })).toBe(
      '/api/v1/processes?status=READY&limit=50',
    )
    expect(buildUrl('/health')).toBe('/health')
  })

  it('errorFromBody понимает формат {"error": {...}}', () => {
    const e = errorFromBody(409, { error: { code: 'PENDING_CANDIDATES', message: 'Осталось 2', details: { pending: 2 } } })
    expect(e.status).toBe(409)
    expect(e.code).toBe('PENDING_CANDIDATES')
    expect(e.message).toBe('Осталось 2')
    expect(e.details).toEqual({ pending: 2 })
  })

  it('errorFromBody: запасной путь для ошибки FastAPI и пустого тела', () => {
    expect(errorFromBody(422, { detail: [{ loc: ['body'], msg: 'x' }] }).code).toBe('VALIDATION_ERROR')
    const e = errorFromBody(403, null)
    expect(e.code).toBe('HTTP_403')
    expect(e.message).toBe('Недостаточно прав для этого действия')
  })

  it('filenameFromDisposition: UTF-8 и простое имя', () => {
    expect(filenameFromDisposition("attachment; filename*=UTF-8''protocol_%D0%9E%D0%91%D0%AA_v1.pdf")).toBe('protocol_ОБЪ_v1.pdf')
    expect(filenameFromDisposition('attachment; filename="protocol_v1.xml"')).toBe('protocol_v1.xml')
    expect(filenameFromDisposition(null)).toBeNull()
  })
})
