// Короткие уведомления о результате действия (aria-live, исчезают сами).
import { createContext, useCallback, useContext, useMemo, useRef, useState, type ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { Icon } from './Icon'

type Kind = 'ok' | 'error' | 'info'

/** Ссылка в уведомлении (например, «Открыть» готовую проверку); такие уведомления живут дольше. */
export interface ToastLink {
  label: string
  to: string
}

interface Toast {
  id: number
  kind: Kind
  text: string
  link?: ToastLink
}

interface ToastApi {
  ok: (text: string, link?: ToastLink) => void
  error: (text: string, link?: ToastLink) => void
  info: (text: string, link?: ToastLink) => void
}

const ToastContext = createContext<ToastApi | null>(null)

export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<Toast[]>([])
  const seq = useRef(0)

  const dismiss = useCallback((id: number) => setItems((xs) => xs.filter((t) => t.id !== id)), [])

  const push = useCallback(
    (kind: Kind, text: string, link?: ToastLink) => {
      const id = ++seq.current
      setItems((xs) => [...xs.slice(-3), { id, kind, text, link }])
      window.setTimeout(() => dismiss(id), link ? 15000 : kind === 'error' ? 9000 : 4500)
    },
    [dismiss],
  )

  const api = useMemo<ToastApi>(
    () => ({ ok: (t, l) => push('ok', t, l), error: (t, l) => push('error', t, l), info: (t, l) => push('info', t, l) }),
    [push],
  )

  return (
    <ToastContext.Provider value={api}>
      {children}
      <div className="toasts" aria-live="polite" aria-atomic="false">
        {items.map((t) => (
          <div key={t.id} className={`toast toast--${t.kind}`} role={t.kind === 'error' ? 'alert' : 'status'}>
            <Icon name={t.kind === 'error' ? 'alert' : t.kind === 'ok' ? 'check' : 'info'} />
            <span>
              {t.text}
              {t.link ? (
                <>
                  {' '}
                  <Link to={t.link.to} className="toast__link" onClick={() => dismiss(t.id)}>
                    {t.link.label}
                  </Link>
                </>
              ) : null}
            </span>
            <button type="button" className="iconbtn iconbtn--small" onClick={() => dismiss(t.id)} aria-label="Скрыть">
              <Icon name="close" size={14} />
            </button>
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  )
}

export function useToast(): ToastApi {
  const ctx = useContext(ToastContext)
  if (!ctx) throw new Error('useToast вне ToastProvider')
  return ctx
}
