import { useCallback, useEffect, useRef, useState, type DependencyList } from 'react'
import { isAbort } from '../api/client'

export interface AsyncState<T> {
  data: T | undefined
  error: unknown
  loading: boolean
  /** Перезагрузить; silent — не сбрасывать данные и не показывать загрузку (для опроса). */
  reload: (silent?: boolean) => void
  setData: (updater: T | ((prev: T | undefined) => T | undefined)) => void
}

/** Загрузка данных с отменой устаревших запросов. Данные не пропадают при повторной загрузке. */
export function useAsync<T>(fn: (signal: AbortSignal) => Promise<T>, deps: DependencyList, enabled = true): AsyncState<T> {
  const [data, setDataState] = useState<T | undefined>(undefined)
  const [error, setError] = useState<unknown>(null)
  const [loading, setLoading] = useState<boolean>(enabled)
  const [tick, setTick] = useState(0)
  const silentRef = useRef(false)
  const fnRef = useRef(fn)
  fnRef.current = fn

  useEffect(() => {
    if (!enabled) {
      setLoading(false)
      return
    }
    const ctrl = new AbortController()
    if (!silentRef.current) setLoading(true)
    silentRef.current = false
    fnRef
      .current(ctrl.signal)
      .then((value) => {
        if (ctrl.signal.aborted) return
        setDataState(value)
        setError(null)
      })
      .catch((e) => {
        if (ctrl.signal.aborted || isAbort(e)) return
        setError(e)
      })
      .finally(() => {
        if (!ctrl.signal.aborted) setLoading(false)
      })
    return () => ctrl.abort()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, tick, enabled])

  const reload = useCallback((silent = false) => {
    silentRef.current = silent
    setTick((t) => t + 1)
  }, [])

  const setData = useCallback((updater: T | ((prev: T | undefined) => T | undefined)) => {
    setDataState((prev) =>
      typeof updater === 'function' ? (updater as (p: T | undefined) => T | undefined)(prev) : updater,
    )
  }, [])

  return { data, error, loading, reload, setData }
}

/** Периодический вызов, пока enabled; на скрытой вкладке — пауза. */
export function useInterval(callback: () => void, ms: number, enabled: boolean): void {
  const cb = useRef(callback)
  cb.current = callback
  useEffect(() => {
    if (!enabled) return
    const id = window.setInterval(() => {
      if (document.visibilityState === 'visible') cb.current()
    }, ms)
    return () => window.clearInterval(id)
  }, [ms, enabled])
}

export function useDocumentTitle(title: string): void {
  useEffect(() => {
    document.title = title ? `${title} — Инспектор ИИ` : 'Инспектор ИИ'
  }, [title])
}

/** Горячие клавиши: не срабатывают при вводе текста и с модификаторами. Пока открыт модальный диалог, клавиши
 *  страницы молчат (иначе «1» в окне правки подтвердит нарушение под ним); inDialog — обработчик самого диалога. */
export function useHotkeys(handler: (e: KeyboardEvent) => void, enabled = true, opts: { inDialog?: boolean } = {}): void {
  const h = useRef(handler)
  h.current = handler
  const inDialog = !!opts.inDialog
  useEffect(() => {
    if (!enabled) return
    const onKey = (e: KeyboardEvent) => {
      if (e.defaultPrevented || e.ctrlKey || e.metaKey || e.altKey) return
      if (isTyping(e.target)) return
      if (!inDialog && modalOpen()) return
      h.current(e)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [enabled, inDialog])
}

/** Enter и Пробел на кнопке или ссылке — это её нажатие, а не горячая клавиша. */
export function onControl(e: KeyboardEvent): boolean {
  return (e.key === 'Enter' || e.key === ' ') && e.target instanceof HTMLElement && !!e.target.closest('button, a, summary')
}

export function modalOpen(): boolean {
  return typeof document !== 'undefined' && !!document.querySelector('dialog[open]')
}

export function isTyping(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false
  if (target.isContentEditable) return true
  const tag = target.tagName
  if (tag === 'TEXTAREA' || tag === 'SELECT') return true
  if (tag === 'INPUT') {
    const type = (target as HTMLInputElement).type
    return !['checkbox', 'radio', 'button', 'submit', 'reset', 'range'].includes(type)
  }
  return false
}

/** Отложенное значение (поиск без запроса на каждую букву). */
export function useDebounced<T>(value: T, ms = 300): T {
  const [v, setV] = useState(value)
  useEffect(() => {
    const t = window.setTimeout(() => setV(value), ms)
    return () => window.clearTimeout(t)
  }, [value, ms])
  return v
}
