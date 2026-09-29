// Состояния загрузки, ошибки, пустоты; копирование кодов; вкладки.
import { useState, type ReactNode } from 'react'
import { ApiError, errorText } from '../api/client'
import { Icon } from './Icon'

export function Loading({ text = 'Загрузка…' }: { text?: string }) {
  return (
    <div className="loading" role="status">
      <span className="loading__bar" aria-hidden />
      {text}
    </div>
  )
}

export function ErrorBox({ error, onRetry, title }: { error: unknown; onRetry?: () => void; title?: string }) {
  const code = error instanceof ApiError ? error.code : null
  return (
    <div className="errorbox" role="alert">
      <Icon name="alert" />
      <div>
        <strong>{title ?? 'Не удалось загрузить данные'}</strong>
        <p>
          {errorText(error)}
          {code && !code.startsWith('HTTP_') ? <span className="errorbox__code mono"> {code}</span> : null}
        </p>
      </div>
      {onRetry ? (
        <button type="button" className="btn btn--ghost" onClick={onRetry}>
          Повторить
        </button>
      ) : null}
    </div>
  )
}

export function Empty({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="empty">
      <strong>{title}</strong>
      {children ? <div className="empty__body">{children}</div> : null}
    </div>
  )
}

export function Copyable({ value, display, className }: { value: string; display?: string; className?: string }) {
  const [copied, setCopied] = useState(false)
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(value)
      setCopied(true)
      window.setTimeout(() => setCopied(false), 1500)
    } catch {
      /* буфер обмена недоступен (http без localhost) — значение видно в подсказке */
    }
  }
  return (
    <span className={`copyable mono${className ? ` ${className}` : ''}`} title={value}>
      <span className="copyable__text">{display ?? value}</span>
      <button type="button" className="copyable__btn" onClick={copy} aria-label={`Скопировать ${value}`}>
        <Icon name={copied ? 'check' : 'copy'} size={13} />
      </button>
    </span>
  )
}

export interface TabDef<K extends string> {
  key: K
  label: string
  count?: number | null
  tone?: 'alert' | 'muted'
}

export function Tabs<K extends string>({
  tabs,
  active,
  onChange,
  label,
}: {
  tabs: TabDef<K>[]
  active: K
  onChange: (k: K) => void
  label: string
}) {
  return (
    <div className="tabs" role="tablist" aria-label={label}>
      {tabs.map((t) => (
        <button
          key={t.key}
          type="button"
          role="tab"
          aria-selected={t.key === active}
          className={`tabs__tab${t.key === active ? ' tabs__tab--active' : ''}`}
          onClick={() => onChange(t.key)}
        >
          {t.label}
          {t.count !== undefined && t.count !== null ? (
            <span className={`tabs__count${t.tone ? ` tabs__count--${t.tone}` : ''}`}>{t.count}</span>
          ) : null}
        </button>
      ))}
    </div>
  )
}

export function Field({ label, hint, children, required }: { label: string; hint?: ReactNode; children: ReactNode; required?: boolean }) {
  return (
    <label className="field">
      <span className="field__label">
        {label}
        {required ? <span className="field__req" aria-hidden> *</span> : null}
      </span>
      {children}
      {hint ? <span className="field__hint">{hint}</span> : null}
    </label>
  )
}
