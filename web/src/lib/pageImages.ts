// PNG страниц для панелей ПД/РД/ИД. Картинка приходит с Bearer-токеном (в адрес токен не кладём),
// поэтому храним blob-URL в небольшом LRU-кэше; ошибки не кэшируются — повтор загрузит заново.
import { fetchBlob } from '../api/client'
import { api } from '../api/endpoints'

const MAX_ENTRIES = 80
const cache = new Map<string, Promise<string>>()

function keyOf(filePk: number, page: number, width: number): string {
  return `${filePk}/${page}/${width}`
}

function touch(key: string, value: Promise<string>): void {
  cache.delete(key)
  cache.set(key, value)
}

function evict(): void {
  while (cache.size > MAX_ENTRIES) {
    const oldest = cache.keys().next().value as string
    const p = cache.get(oldest)
    cache.delete(oldest)
    p?.then((url) => URL.revokeObjectURL(url)).catch(() => undefined)
  }
}

export function pageImageUrl(filePk: number, page: number, width: number): Promise<string> {
  const key = keyOf(filePk, page, width)
  const hit = cache.get(key)
  if (hit) {
    touch(key, hit)
    return hit
  }
  const p = fetchBlob(api.pageImagePath(filePk, page), { query: { width }, timeoutMs: 90_000, retries: 1 }).then(({ blob }) =>
    URL.createObjectURL(blob),
  )
  cache.set(key, p)
  p.catch(() => cache.delete(key))
  evict()
  return p
}

/** Подгрузить заранее (следующая карточка) — без ожидания и без шума при ошибке. */
export function prefetchPage(filePk: number, page: number, width = 1400): void {
  pageImageUrl(filePk, page, width).catch(() => undefined)
}

// Миниатюры для сетки страниц: свой кэш побольше, чтобы листание сетки не вытесняло крупные страницы просмотра.
const THUMB_MAX = 1200
const thumbs = new Map<string, Promise<string>>()

export function pageThumbUrl(filePk: number, page: number, width: number): Promise<string> {
  const key = keyOf(filePk, page, width)
  const hit = thumbs.get(key)
  if (hit) {
    thumbs.delete(key)
    thumbs.set(key, hit)
    return hit
  }
  const p = fetchBlob(api.pageImagePath(filePk, page), { query: { width }, timeoutMs: 90_000, retries: 1 }).then(({ blob }) =>
    URL.createObjectURL(blob),
  )
  thumbs.set(key, p)
  p.catch(() => thumbs.delete(key))
  while (thumbs.size > THUMB_MAX) {
    const oldest = thumbs.keys().next().value as string
    const old = thumbs.get(oldest)
    thumbs.delete(oldest)
    old?.then((url) => URL.revokeObjectURL(url)).catch(() => undefined)
  }
  return p
}
