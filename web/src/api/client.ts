// HTTP-клиент: Bearer-токен из localStorage, ошибки в формате {"error": {code, message, details}},
// таймауты, повторы только для идемпотентных GET, выход на страницу входа по 401.

export const API_BASE = '/api/v1'
const TOKEN_KEY = 'inspector.token'
const RETRYABLE_STATUS = new Set([502, 503, 504])

export class ApiError extends Error {
  readonly status: number
  readonly code: string
  readonly details: unknown

  constructor(status: number, code: string, message: string, details?: unknown) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
    this.details = details
  }
}

export function isAbort(e: unknown): boolean {
  return e instanceof DOMException && e.name === 'AbortError'
}

export function errorText(e: unknown): string {
  if (e instanceof ApiError) return e.message
  if (e instanceof Error) return e.message
  return String(e)
}

// --- токен ---------------------------------------------------------------

export function getToken(): string | null {
  try {
    return localStorage.getItem(TOKEN_KEY)
  } catch {
    return null
  }
}

export function setToken(token: string): void {
  try {
    localStorage.setItem(TOKEN_KEY, token)
  } catch {
    /* хранилище недоступно (приватный режим) — токен живёт до перезагрузки вкладки */
    memoryToken = token
  }
}

export function clearToken(): void {
  memoryToken = null
  try {
    localStorage.removeItem(TOKEN_KEY)
  } catch {
    /* нет хранилища */
  }
}

let memoryToken: string | null = null

function currentToken(): string | null {
  return getToken() ?? memoryToken
}

let unauthorizedHandler: (() => void) | null = null

/** Что делать при 401 (кроме самого входа): очистить сессию и увести на страницу входа. */
export function setUnauthorizedHandler(fn: (() => void) | null): void {
  unauthorizedHandler = fn
}

function handleUnauthorized(): ApiError {
  clearToken()
  unauthorizedHandler?.()
  return new ApiError(401, 'UNAUTHORIZED', 'Сессия истекла — войдите снова')
}

// --- URL -----------------------------------------------------------------

export type Query = Record<string, string | number | boolean | null | undefined>

export function buildUrl(path: string, query?: Query): string {
  const base = path.startsWith('/api/') || path === '/health' || path === '/metrics' ? path : API_BASE + path
  if (!query) return base
  const qs = new URLSearchParams()
  for (const [key, value] of Object.entries(query)) {
    if (value === undefined || value === null || value === '') continue
    qs.set(key, String(value))
  }
  const s = qs.toString()
  return s ? `${base}?${s}` : base
}

// --- ошибки --------------------------------------------------------------

export function errorFromBody(status: number, data: unknown, statusText = ''): ApiError {
  let code = `HTTP_${status}`
  let message = statusText || defaultMessage(status)
  let details: unknown
  if (data && typeof data === 'object') {
    const obj = data as Record<string, unknown>
    const err = obj.error
    if (err && typeof err === 'object') {
      const e = err as Record<string, unknown>
      if (typeof e.code === 'string') code = e.code
      if (typeof e.message === 'string' && e.message) message = e.message
      details = e.details
    } else if ('detail' in obj) {
      // запасной путь: стандартная ошибка FastAPI
      details = obj.detail
      if (typeof obj.detail === 'string') message = obj.detail
      else if (status === 422) {
        code = 'VALIDATION_ERROR'
        message = 'Проверьте заполнение полей'
      }
    }
  }
  return new ApiError(status, code, message, details)
}

function defaultMessage(status: number): string {
  if (status === 403) return 'Недостаточно прав для этого действия'
  if (status === 404) return 'Не найдено'
  if (status === 409) return 'Действие недоступно в текущем состоянии проверки'
  if (status === 413) return 'Слишком большой запрос'
  if (status >= 500) return 'Ошибка сервера'
  return 'Ошибка запроса'
}

async function readError(res: Response): Promise<ApiError> {
  let data: unknown = null
  try {
    data = await res.json()
  } catch {
    /* не JSON */
  }
  return errorFromBody(res.status, data, '')
}

// --- таймауты и повторы -----------------------------------------------------

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

function backoff(attempt: number): number {
  return 400 * 2 ** attempt
}

interface Deadline {
  signal: AbortSignal
  timedOut: () => boolean
  clear: () => void
}

function deadline(ms: number, outer?: AbortSignal): Deadline {
  const ctrl = new AbortController()
  let timed = false
  const timer = setTimeout(() => {
    timed = true
    ctrl.abort()
  }, ms)
  const onAbort = () => ctrl.abort()
  if (outer) {
    if (outer.aborted) ctrl.abort()
    else outer.addEventListener('abort', onAbort, { once: true })
  }
  return {
    signal: ctrl.signal,
    timedOut: () => timed,
    clear: () => {
      clearTimeout(timer)
      outer?.removeEventListener('abort', onAbort)
    },
  }
}

export interface RequestOptions {
  query?: Query
  body?: unknown
  signal?: AbortSignal
  timeoutMs?: number
  /** По умолчанию: 2 повтора для GET, 0 для изменяющих запросов. */
  retries?: number
}

async function send(method: string, path: string, opts: RequestOptions, accept: string): Promise<Response> {
  const { query, body, signal, timeoutMs = 30_000 } = opts
  const retries = opts.retries ?? (method === 'GET' ? 2 : 0)
  const headers: Record<string, string> = { Accept: accept }
  const token = currentToken()
  if (token) headers.Authorization = `Bearer ${token}`
  let payload: BodyInit | undefined
  if (body instanceof FormData) payload = body
  else if (body !== undefined) {
    headers['Content-Type'] = 'application/json'
    payload = JSON.stringify(body)
  }
  const url = buildUrl(path, query)
  for (let attempt = 0; ; attempt++) {
    const d = deadline(timeoutMs, signal)
    let res: Response
    try {
      res = await fetch(url, { method, headers, body: payload, signal: d.signal })
    } catch (e) {
      d.clear()
      if (signal?.aborted) throw e
      if (attempt < retries) {
        await sleep(backoff(attempt))
        continue
      }
      throw d.timedOut()
        ? new ApiError(0, 'TIMEOUT', 'Сервер не ответил вовремя — повторите попытку')
        : new ApiError(0, 'NETWORK', 'Нет связи с сервером — проверьте, что сервис запущен')
    }
    d.clear()
    if (res.status === 401 && path !== '/auth/login') throw handleUnauthorized()
    if (RETRYABLE_STATUS.has(res.status) && attempt < retries) {
      await sleep(backoff(attempt))
      continue
    }
    if (!res.ok) throw await readError(res)
    return res
  }
}

export async function request<T>(method: string, path: string, opts: RequestOptions = {}): Promise<T> {
  const res = await send(method, path, opts, 'application/json')
  if (res.status === 204) return undefined as T
  const type = res.headers.get('content-type') ?? ''
  if (type.includes('json')) return (await res.json()) as T
  const text = await res.text()
  return (text ? (text as unknown) : undefined) as T
}

export interface BlobResult {
  blob: Blob
  filename: string | null
}

export async function fetchBlob(path: string, opts: RequestOptions = {}): Promise<BlobResult> {
  const res = await send('GET', path, { timeoutMs: 120_000, ...opts }, '*/*')
  return { blob: await res.blob(), filename: filenameFromDisposition(res.headers.get('content-disposition')) }
}

export function filenameFromDisposition(header: string | null): string | null {
  if (!header) return null
  const star = /filename\*\s*=\s*(?:UTF-8|utf-8)''([^;]+)/.exec(header)
  if (star) {
    try {
      return decodeURIComponent(star[1].trim().replace(/^"|"$/g, ''))
    } catch {
      /* кривое кодирование — берём простое имя */
    }
  }
  const plain = /filename\s*=\s*"?([^";]+)"?/.exec(header)
  return plain ? plain[1].trim() : null
}

/** Скачать файл с авторизацией (ссылка <a href> не передаёт Bearer-токен). */
export async function downloadFile(path: string, fallbackName: string, query?: Query): Promise<void> {
  const { blob, filename } = await fetchBlob(path, { query })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename ?? fallbackName
  document.body.appendChild(a)
  a.click()
  a.remove()
  setTimeout(() => URL.revokeObjectURL(url), 60_000)
}

/** Загрузка multipart через XHR — ради прогресса отправки (fetch его не отдаёт). */
export function uploadForm<T>(
  path: string,
  form: FormData,
  onProgress?: (loaded: number, total: number) => void,
  signal?: AbortSignal,
): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const xhr = new XMLHttpRequest()
    xhr.open('POST', buildUrl(path))
    const token = currentToken()
    if (token) xhr.setRequestHeader('Authorization', `Bearer ${token}`)
    xhr.setRequestHeader('Accept', 'application/json')
    xhr.timeout = 60 * 60 * 1000
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable) onProgress?.(e.loaded, e.total)
    }
    xhr.onload = () => {
      let data: unknown = null
      try {
        data = xhr.responseText ? JSON.parse(xhr.responseText) : null
      } catch {
        data = null
      }
      if (xhr.status === 401) {
        reject(handleUnauthorized())
        return
      }
      if (xhr.status >= 200 && xhr.status < 300) resolve(data as T)
      else reject(errorFromBody(xhr.status, data, ''))
    }
    xhr.onerror = () => reject(new ApiError(0, 'NETWORK', 'Связь с сервером прервалась во время отправки'))
    xhr.ontimeout = () => reject(new ApiError(0, 'TIMEOUT', 'Отправка заняла больше часа и прервана'))
    xhr.onabort = () => reject(new DOMException('Отправка отменена', 'AbortError'))
    if (signal) {
      if (signal.aborted) {
        reject(new DOMException('Отправка отменена', 'AbortError'))
        return
      }
      signal.addEventListener('abort', () => xhr.abort(), { once: true })
    }
    xhr.send(form)
  })
}

/** Ответ-список: API может вернуть массив или {items: [...]}. */
export function asItems<T>(data: T[] | { items?: T[] } | null | undefined): T[] {
  if (!data) return []
  if (Array.isArray(data)) return data
  return Array.isArray(data.items) ? data.items : []
}
