// Полоса решения: «Подтвердить» (1), «Отклонить» (2 → причина → комментарий → Enter), «Требует уточнения» (3),
// ←/→ — соседние карточки. Цель — не больше трёх осознанных действий до сохранённого решения.
import { useEffect, useRef, useState, type KeyboardEvent } from 'react'
import type { Decision, FindingCard } from '../../api/types'
import { DecisionBadge } from '../../components/Badges'
import { Icon } from '../../components/Icon'
import type { Neighbors } from '../../lib/findings'
import { formatDateTime } from '../../lib/format'
import { isTyping } from '../../lib/hooks'
import { FINDING_STATUS_LABEL, REASON_CODES, REASON_LABEL, label } from '../../lib/labels'

export type FormMode = null | 'reject' | 'clarify' | 'confirm'

interface Props {
  card: FindingCard
  mode: FormMode
  setMode: (m: FormMode) => void
  /** решение возможно: роль, статус проверки, актуальность карточки */
  canDecide: boolean
  blockedReason: string | null
  busy: boolean
  onDecide: (decision: Decision, reasonCode?: string, comment?: string) => Promise<boolean>
  onPromote: () => Promise<boolean>
  nav: Neighbors
  onNav: (id: number) => void
  pendingLeft: number
}

export function DecisionBar({ card, mode, setMode, canDecide, blockedReason, busy, onDecide, onPromote, nav, onNav, pendingLeft }: Props) {
  const [reason, setReason] = useState<string>('')
  const [comment, setComment] = useState('')
  const [error, setError] = useState<string | null>(null)
  const reasonsRef = useRef<HTMLFieldSetElement>(null)
  const commentRef = useRef<HTMLTextAreaElement>(null)
  const isCandidate = card.machine_status === 'CANDIDATE'
  const isSuspicion = card.status === 'SUSPICION'
  const hasPages = card.sources.some((s) => s.page)

  // новая карточка — форма чистая
  useEffect(() => {
    setReason('')
    setComment('')
    setError(null)
  }, [card.id])

  // фокус при открытии формы: причины (цифрами 1–7) или комментарий
  useEffect(() => {
    setError(null)
    if (mode === 'reject') {
      if (reason) commentRef.current?.focus()
      else reasonsRef.current?.focus()
    } else if (mode === 'clarify' || mode === 'confirm') commentRef.current?.focus()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mode])

  const submit = async () => {
    if (mode === 'reject') {
      if (!reason || !comment.trim()) {
        setError(!reason ? 'Выберите причину отклонения' : 'Напишите комментарий: что именно не так')
        ;(!reason ? reasonsRef.current : commentRef.current)?.focus()
        return
      }
      if (await onDecide('NEGATIVE_VERIFIED', reason, comment.trim())) setMode(null)
    } else if (mode === 'clarify') {
      if (await onDecide('CLARIFICATION_REQUIRED', undefined, comment.trim() || undefined)) setMode(null)
    } else if (mode === 'confirm') {
      if (await onDecide('CONFIRMED_VIOLATION', undefined, comment.trim() || undefined)) setMode(null)
    }
  }

  const onFormKey = (e: KeyboardEvent<HTMLDivElement>) => {
    if (e.key === 'Escape') {
      e.preventDefault()
      setMode(null)
      return
    }
    if (mode === 'reject' && !isTyping(e.target) && /^[1-7]$/.test(e.key)) {
      e.preventDefault()
      setReason(REASON_CODES[Number(e.key) - 1].code)
      setError(null)
      commentRef.current?.focus()
      return
    }
    if (e.key === 'Enter' && !e.shiftKey && e.target === commentRef.current) {
      e.preventDefault()
      void submit()
    }
  }

  const decisionState = () => {
    if (blockedReason) return <span>{blockedReason}</span>
    if (isCandidate) {
      const st = card.inspector.status
      if (!st || st === 'PENDING') return <DecisionBadge status="PENDING" />
      return (
        <span>
          <DecisionBadge status={st} /> {card.inspector.user}, {formatDateTime(card.inspector.decided_at)}
          {card.inspector.reason_code ? `; ${label(REASON_LABEL, card.inspector.reason_code)}` : ''}. Можно изменить до
          финализации.
        </span>
      )
    }
    if (isSuspicion) return <span>Гипотеза свободного поиска — не входит в число нарушений.</span>
    return <span>Решение не требуется: {label(FINDING_STATUS_LABEL, card.status).toLowerCase()}.</span>
  }

  return (
    <div className="dbar" onKeyDown={mode ? onFormKey : undefined}>
      {mode ? (
        <div className={`dbar__form${mode === 'reject' ? '' : ' dbar__form--single'}`}>
          {mode === 'reject' ? (
            <fieldset className="reasons" ref={reasonsRef} tabIndex={-1}>
              <legend>Причина отклонения — клавиши 1–7</legend>
              {REASON_CODES.map((r, i) => (
                <label key={r.code} className="reason" title={r.code}>
                  <input
                    type="radio"
                    name="reason"
                    value={r.code}
                    checked={reason === r.code}
                    onChange={() => {
                      setReason(r.code)
                      setError(null)
                      commentRef.current?.focus()
                    }}
                  />
                  <kbd>{i + 1}</kbd>
                  {r.label}
                </label>
              ))}
            </fieldset>
          ) : null}
          <div className="dbar__comment">
            <label className="field">
              <span className="field__label">
                {mode === 'reject'
                  ? 'Комментарий — обязателен'
                  : mode === 'clarify'
                    ? 'Что уточнить (необязательно)'
                    : 'Комментарий к подтверждению (необязательно)'}
              </span>
              <textarea
                ref={commentRef}
                className="textarea"
                value={comment}
                onChange={(e) => {
                  setComment(e.target.value)
                  setError(null)
                }}
                placeholder={mode === 'reject' ? 'Например: в РД на листе 5 указана другая секция; значение относится к корпусу 2' : ''}
              />
            </label>
            <div className="dbar__formfoot">
              {error ? <span className="dbar__error" role="alert">{error}</span> : <span>Enter — сохранить, Shift+Enter — новая строка, Esc — отмена</span>}
              <span className="grow" />
              <button type="button" className="btn btn--small" onClick={() => setMode(null)}>
                Отмена
              </button>
              <button type="button" className={`btn btn--small${mode === 'confirm' ? ' btn--primary' : ''}`} onClick={() => void submit()} disabled={busy}>
                {mode === 'reject' ? 'Отклонить с причиной' : mode === 'clarify' ? 'Отправить на уточнение' : 'Подтвердить нарушение'}
              </button>
            </div>
          </div>
        </div>
      ) : null}

      <div className="dbar__main">
        {isCandidate && canDecide ? (
          <>
            <button type="button" className="dbtn dbtn--confirm" disabled={busy} onClick={() => void onDecide('CONFIRMED_VIOLATION')} title="Подтвердить нарушение (клавиша 1)">
              <kbd>1</kbd> Подтвердить нарушение
            </button>
            <button type="button" className="iconbtn" disabled={busy} onClick={() => setMode(mode === 'confirm' ? null : 'confirm')} aria-label="Подтвердить с комментарием" title="Подтвердить с комментарием">
              <Icon name="text" />
            </button>
            <button type="button" className={`dbtn dbtn--reject${mode === 'reject' ? ' is-open' : ''}`} disabled={busy} onClick={() => setMode(mode === 'reject' ? null : 'reject')} title="Отклонить: причина и комментарий (клавиша 2)">
              <kbd>2</kbd> Отклонить
            </button>
            <button type="button" className={`dbtn dbtn--clarify${mode === 'clarify' ? ' is-open' : ''}`} disabled={busy} onClick={() => setMode(mode === 'clarify' ? null : 'clarify')} title="Требует уточнения (клавиша 3)">
              <kbd>3</kbd> Требует уточнения
            </button>
          </>
        ) : null}
        {isSuspicion && canDecide ? (
          <button
            type="button"
            className="dbtn dbtn--promote"
            disabled={busy || !hasPages}
            onClick={() => void onPromote()}
            title={hasPages ? 'Перевести гипотезу в кандидаты на нарушение' : 'Нет страниц-доказательств — привязать нельзя'}
          >
            Привязать как кандидата
          </button>
        ) : null}
        <div className="dbar__state">{decisionState()}</div>
        <div className="dbar__nav">
          {isCandidate ? <span className="muted num" style={{ fontSize: 12 }}>ждут решения: {pendingLeft}</span> : null}
          <button type="button" className="btn btn--small" onClick={() => nav.prev !== null && onNav(nav.prev)} disabled={nav.prev === null} title="Предыдущая карточка (←)">
            <Icon name="chevronLeft" size={14} /> <kbd>←</kbd>
          </button>
          <span className="num wb__pos">{nav.index >= 0 ? `${nav.index + 1} из ${nav.total}` : '—'}</span>
          <button type="button" className="btn btn--small" onClick={() => nav.next !== null && onNav(nav.next)} disabled={nav.next === null} title="Следующая карточка (→)">
            <kbd>→</kbd> <Icon name="chevronRight" size={14} />
          </button>
        </div>
      </div>
    </div>
  )
}
