import { describe, expect, it } from 'vitest'
import {
  MAX_FILE_BYTES,
  MAX_REQUEST_BYTES,
  MB,
  checkFile,
  extensionOf,
  fileKey,
  planBatches,
  requestBytes,
} from './upload'

const f = (name: string, mb: number) => ({ name, size: Math.round(mb * MB) })

describe('extensionOf', () => {
  it('берёт расширение в нижнем регистре', () => {
    expect(extensionOf('Акт АОСР №1.PDF')).toBe('pdf')
    expect(extensionOf('папка/вложенная/лист.dwg')).toBe('dwg')
  })
  it('нет расширения — пусто', () => {
    expect(extensionOf('README')).toBe('')
    expect(extensionOf('.hidden')).toBe('')
    expect(extensionOf('file.')).toBe('')
  })
})

describe('checkFile', () => {
  it('принимает допустимый формат до 50 МБ включительно', () => {
    expect(checkFile(f('КР1.pdf', 12))).toBeNull()
    expect(checkFile({ name: 'x.zip', size: MAX_FILE_BYTES })).toBeNull()
    expect(checkFile(f('манифест.jsonl', 0.01))).toBeNull()
  })
  it('отклоняет файл больше 50 МБ', () => {
    expect(checkFile({ name: 'big.pdf', size: MAX_FILE_BYTES + 1 })).toBe('FILE_TOO_LARGE')
  })
  it('отклоняет неподдерживаемый формат раньше размера', () => {
    expect(checkFile(f('setup.exe', 80))).toBe('UNSUPPORTED_FORMAT')
    expect(checkFile(f('чертёж.dwg', 1))).toBe('UNSUPPORTED_FORMAT')
  })
  it('отклоняет пустой файл', () => {
    expect(checkFile({ name: 'empty.pdf', size: 0 })).toBe('EMPTY_FILE')
  })
})

describe('planBatches', () => {
  it('маленький пакет — один запрос', () => {
    const files = [f('a.pdf', 10), f('b.pdf', 20)]
    const { batches, oversize } = planBatches(files)
    expect(batches).toEqual([files])
    expect(oversize).toEqual([])
  })

  it('5 файлов по 45 МБ → два запроса, каждый ≤ 200 МБ с запасом', () => {
    const files = Array.from({ length: 5 }, (_, i) => f(`${i}.pdf`, 45))
    const { batches } = planBatches(files)
    expect(batches.map((b) => b.length)).toEqual([4, 1])
    for (const b of batches) expect(requestBytes(b)).toBeLessThanOrEqual(MAX_REQUEST_BYTES)
  })

  it('сохраняет порядок и не теряет файлы', () => {
    const files = [30, 49, 49, 49, 49, 5, 1].map((mb, i) => f(`${i}.pdf`, mb))
    const { batches } = planBatches(files)
    expect(batches.flat().map((x) => x.name)).toEqual(files.map((x) => x.name))
    for (const b of batches) expect(requestBytes(b)).toBeLessThanOrEqual(MAX_REQUEST_BYTES)
  })

  it('пустой список — ни одного запроса', () => {
    expect(planBatches([])).toEqual({ batches: [], oversize: [] })
  })

  it('файл, который один не влезает в запрос, не отправляется', () => {
    const huge = { name: 'huge.zip', size: MAX_REQUEST_BYTES }
    const { batches, oversize } = planBatches([f('a.pdf', 1), huge])
    expect(oversize).toEqual([huge])
    expect(batches.flat()).toHaveLength(1)
  })

  it('уважает меньший лимит запроса', () => {
    const files = [f('a.pdf', 3), f('b.pdf', 3), f('c.pdf', 3)]
    const { batches } = planBatches(files, 8 * MB)
    expect(batches.map((b) => b.length)).toEqual([2, 1])
  })
})

describe('fileKey', () => {
  it('различает файлы по имени, размеру и дате изменения', () => {
    expect(fileKey({ name: 'a.pdf', size: 1, lastModified: 1 })).not.toBe(fileKey({ name: 'a.pdf', size: 1, lastModified: 2 }))
    expect(fileKey({ name: 'a.pdf', size: 1 })).toBe(fileKey({ name: 'a.pdf', size: 1 }))
  })
})
