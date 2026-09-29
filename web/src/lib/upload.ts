// Проверки загрузки до отправки (ТЗ 9.1): 50 МБ на файл, 200 МБ на запрос, допустимые форматы.
// Большой пакет режется на несколько запросов по ≤ 200 МБ, они уходят последовательно.

export const MB = 1024 * 1024
export const MAX_FILE_BYTES = 50 * MB
export const MAX_REQUEST_BYTES = 200 * MB
/** Запас на заголовки multipart и служебные поля запроса. */
export const REQUEST_ENVELOPE_BYTES = 1 * MB
export const PER_FILE_ENVELOPE_BYTES = 1024

export const ALLOWED_EXTENSIONS = [
  'pdf', 'docx', 'doc', 'xml', 'xls', 'xlsx', 'rtf', 'odt',
  'jpg', 'jpeg', 'png', 'tif', 'tiff', 'zip', '7z', 'rar', 'sig', 'jsonl',
]

export const ACCEPT_ATTR = ALLOWED_EXTENSIONS.map((e) => `.${e}`).join(',')

export type LocalRejectCode = 'FILE_TOO_LARGE' | 'UNSUPPORTED_FORMAT' | 'EMPTY_FILE'

export interface FileLike {
  name: string
  size: number
}

export function extensionOf(name: string): string {
  const base = name.split(/[\\/]/).pop() ?? name
  const dot = base.lastIndexOf('.')
  return dot > 0 && dot < base.length - 1 ? base.slice(dot + 1).toLowerCase() : ''
}

/** Причина отказа до отправки или null, если файл можно отправлять. */
export function checkFile(f: FileLike, maxFileBytes = MAX_FILE_BYTES): LocalRejectCode | null {
  if (!ALLOWED_EXTENSIONS.includes(extensionOf(f.name))) return 'UNSUPPORTED_FORMAT'
  if (f.size <= 0) return 'EMPTY_FILE'
  if (f.size > maxFileBytes) return 'FILE_TOO_LARGE'
  return null
}

export function requestBytes(files: FileLike[]): number {
  return REQUEST_ENVELOPE_BYTES + files.reduce((s, f) => s + f.size + PER_FILE_ENVELOPE_BYTES, 0)
}

/**
 * Делит файлы на запросы так, чтобы каждый запрос с запасом на multipart был ≤ limit.
 * Порядок файлов сохраняется (жадно: пока влезает — в текущий запрос). Файл, который один
 * не влезает в запрос, не отправляется — такие файлы должны быть отсеяны checkFile заранее.
 */
export function planBatches<T extends FileLike>(files: T[], limit = MAX_REQUEST_BYTES): { batches: T[][]; oversize: T[] } {
  const batches: T[][] = []
  const oversize: T[] = []
  let current: T[] = []
  let used = REQUEST_ENVELOPE_BYTES
  for (const f of files) {
    const cost = f.size + PER_FILE_ENVELOPE_BYTES
    if (REQUEST_ENVELOPE_BYTES + cost > limit) {
      oversize.push(f)
      continue
    }
    if (used + cost > limit && current.length) {
      batches.push(current)
      current = []
      used = REQUEST_ENVELOPE_BYTES
    }
    current.push(f)
    used += cost
  }
  if (current.length) batches.push(current)
  return { batches, oversize }
}

/** Ключ для отсева повторно добавленного файла. */
export function fileKey(f: { name: string; size: number; lastModified?: number }): string {
  return `${f.name}\u0000${f.size}\u0000${f.lastModified ?? 0}`
}
