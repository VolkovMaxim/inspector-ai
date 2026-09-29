// Рабочее место по карточке: слева карточка доказательства, справа синхронные панели ПД | РД | ИД,
// внизу решение. Горячие клавиши: 1 — подтвердить, 2 — отклонить, 3 — требует уточнения, ←/→ — соседние карточки.
import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { errorText } from '../api/client'
import { api } from '../api/endpoints'
import type { Decision, DecisionResult, FindingCard, Stage } from '../api/types'
import { useAuth } from '../auth/AuthContext'
import { decisionsAllowed } from '../auth/permissions'
import { ErrorBox, Loading } from '../components/Feedback'
import { Icon } from '../components/Icon'
import { useToast } from '../components/Toasts'
import { LIST_TITLE, defaultListFor, isListKey, isPending, listFor, neighbors, nextPendingId, type ListKey } from '../lib/findings'
import { useAsync, useDocumentTitle, useHotkeys } from '../lib/hooks'
import { DECISION_LABEL, STAGES } from '../lib/labels'
import { prefetchPage } from '../lib/pageImages'
import { DecisionBar, type FormMode } from './workbench/DecisionBar'
import { EvidencePanel, type FocusRequest } from './workbench/EvidencePanel'
import { FindingCardView } from './workbench/FindingCardView'

const LIST_TAB: Record<ListKey, string> = {
  candidates: 'candidates',
  confirmed: 'confirmed',
  negatives: 'negatives',
  completeness: 'completeness',
  hypotheses: 'hypotheses',
  all: 'summary',
}

export function WorkbenchPage() {
  const { id = '', fid = '' } = useParams()
  const fidNum = Number(fid)
  const [params] = useSearchParams()
  const navigate = useNavigate()
  const toast = useToast()
  const { can } = useAuth()

  const protocol = useAsync((signal) => api.protocol(id, undefined, signal), [id])
  const cardQ = useAsync((signal) => api.finding(fidNum, signal), [fidNum])
  // Карточка из протокола показывается сразу (без мигания при переходе к следующей), запрос /findings/{id}
  // дополняет её историей решений. id всегда текущий — решение не может уйти на предыдущую карточку.
  const listCard = useMemo(() => protocol.data?.findings.find((c) => c.id === fidNum), [protocol.data, fidNum])
  const card = cardQ.data && cardQ.data.id === fidNum ? cardQ.data : listCard
  useDocumentTitle(card ? `${card.code} ${card.location || card.parameter_name}` : 'Карточка')

  const [mode, setMode] = useState<FormMode>(null)
  const [busy, setBusy] = useState(false)
  const [showCard, setShowCard] = useState(true)
  const [linked, setLinked] = useState(false)
  const [sharedZoom, setSharedZoom] = useState(1)
  const [expanded, setExpanded] = useState<Stage | null>(null)
  const [focusReq, setFocusReq] = useState<FocusRequest>({ seq: 0, stage: 'all' })

  useEffect(() => {
    setMode(null)
  }, [fidNum])

  const findings = useMemo(() => protocol.data?.findings ?? [], [protocol.data])
  const listParam = params.get('list')
  const listKey: ListKey = isListKey(listParam) ? listParam : card ? defaultListFor(card) : 'all'
  const list = useMemo(() => {
    const l = listFor(findings, listKey)
    return l.some((c) => c.id === fidNum) ? l : listFor(findings, 'all')
  }, [findings, listKey, fidNum])
  const ids = useMemo(() => list.map((c) => c.id), [list])
  const nav = neighbors(ids, fidNum)
  const pendingIds = useMemo(() => new Set(list.filter(isPending).map((c) => c.id)), [list])
  const pendingLeft = useMemo(() => findings.filter(isPending).length, [findings])

  const files = useMemo(() => new Map((protocol.data?.files ?? []).map((f) => [f.file_pk, f])), [protocol.data])
  const stagePresent = useMemo(() => {
    const s = new Set((protocol.data?.files ?? []).filter((f) => f.status === 'ok' || f.status === 'pending').map((f) => f.stage))
    return (st: Stage) => s.has(st)
  }, [protocol.data])

  const status = protocol.data?.process_status
  const roleCan = can('decide')
  const open = !!status && decisionsAllowed(status)
  const active = card?.active !== false
  const canDecide = roleCan && open && active
  const blockedReason = !card
    ? null
    : !active
      ? 'Карточка устарела после повторной обработки — откройте актуальную из протокола.'
      : status === 'FINALIZED'
        ? 'Протокол финализирован — решения закрыты. Отмена финализации — у супервизора.'
        : !open
          ? 'Решения принимаются, когда протокол готов.'
          : !roleCan && (card.machine_status === 'CANDIDATE' || card.status === 'SUSPICION')
            ? 'Ваша роль — только просмотр.'
            : null

  const go = useCallback(
    (target: number) => navigate(`/processes/${id}/findings/${target}?list=${listKey}`),
    [navigate, id, listKey],
  )

  // заранее подгрузить страницы следующей карточки
  useEffect(() => {
    if (nav.next === null) return
    const next = findings.find((c) => c.id === nav.next)
    next?.sources.forEach((s) => s.page && prefetchPage(s.file_pk, s.page))
  }, [nav.next, findings])

  const applyResult = (res: DecisionResult) => {
    const { process_status, candidates_pending: _pending, ...cardPart } = res
    void _pending
    cardQ.setData(cardPart as FindingCard)
    protocol.setData((prev) =>
      prev
        ? {
            ...prev,
            process_status: process_status ?? prev.process_status,
            findings: prev.findings.map((f) => (f.id === res.id ? { ...f, ...(cardPart as FindingCard) } : f)),
          }
        : prev,
    )
  }

  const decide = async (decision: Decision, reasonCode?: string, comment?: string): Promise<boolean> => {
    if (!card) return false
    setBusy(true)
    try {
      const res = await api.decide(card.id, { decision, reason_code: reasonCode, comment })
      applyResult(res)
      toast.ok(`${DECISION_LABEL[decision]}: ${card.code}${card.location ? `, ${card.location}` : ''}`)
      if (listKey === 'candidates') {
        const rest = new Set(pendingIds)
        rest.delete(card.id)
        const nextId = nextPendingId(ids, rest, card.id)
        if (nextId !== null) go(nextId)
        else toast.info('Все кандидаты рассмотрены — протокол можно финализировать')
      }
      return true
    } catch (e) {
      toast.error(`Решение не сохранено: ${errorText(e)}`)
      return false
    } finally {
      setBusy(false)
    }
  }

  const promote = async (): Promise<boolean> => {
    if (!card) return false
    setBusy(true)
    try {
      const res = await api.promote(card.id)
      applyResult(res)
      toast.ok(`Гипотеза ${card.code} переведена в кандидаты`)
      navigate(`/processes/${id}/findings/${card.id}?list=candidates`, { replace: true })
      return true
    } catch (e) {
      toast.error(`Не привязано: ${errorText(e)}`)
      return false
    } finally {
      setBusy(false)
    }
  }

  useHotkeys(
    (e) => {
      if (!card) return
      if (mode === null && e.key === 'ArrowLeft' && nav.prev !== null) {
        e.preventDefault()
        go(nav.prev)
      } else if (mode === null && e.key === 'ArrowRight' && nav.next !== null) {
        e.preventDefault()
        go(nav.next)
      } else if (mode === null && canDecide && !busy && card.machine_status === 'CANDIDATE') {
        if (e.key === '1') {
          e.preventDefault()
          void decide('CONFIRMED_VIOLATION')
        } else if (e.key === '2') {
          e.preventDefault()
          setMode('reject')
        } else if (e.key === '3') {
          e.preventDefault()
          setMode('clarify')
        }
      }
    },
    !!card,
  )

  const focusStage = (stage: Stage, sourceIndex?: number) => {
    if (expanded && expanded !== stage) setExpanded(stage)
    setFocusReq((f) => ({ seq: f.seq + 1, stage, sourceIndex }))
  }

  if (protocol.loading && !protocol.data) return <div className="page"><Loading text="Открываем карточку…" /></div>
  if (cardQ.loading && !card) return <div className="page"><Loading text="Открываем карточку…" /></div>
  if (cardQ.error && !card) return <div className="page"><ErrorBox error={cardQ.error} onRetry={() => cardQ.reload()} title="Карточка не открылась" /></div>
  if (protocol.error && !protocol.data) return <div className="page"><ErrorBox error={protocol.error} onRetry={() => protocol.reload()} title="Протокол не загрузился" /></div>
  if (!card || !protocol.data) return null

  const present = (st: Stage) => card.sources.some((s) => s.stage === st && s.page)
  const shownStages = expanded ? [expanded] : STAGES
  const columns = expanded ? 'minmax(0, 1fr)' : STAGES.map((st) => (present(st) ? 'minmax(0, 1fr)' : 'minmax(150px, 0.4fr)')).join(' ')

  return (
    <div className={`wb${showCard ? '' : ' wb--nocard'}`}>
      <div className="wb__head">
        <nav className="wb__crumbs" aria-label="Путь">
          <Link to="/">Проверки</Link>
          <span className="muted">/</span>
          <Link to={`/processes/${id}`} title={protocol.data.object.name}>
            {protocol.data.object.name || protocol.data.object.object_id}
          </Link>
          <span className="muted">/</span>
          <Link to={`/processes/${id}?tab=${LIST_TAB[listKey]}`}>{LIST_TITLE[listKey]}</Link>
          <span className="muted">/</span>
          <span className="mono">{card.code}</span>
        </nav>
        <div className="wb__headright">
          <label className="check" title="Одинаковый масштаб во всех панелях">
            <input
              type="checkbox"
              checked={linked}
              onChange={(e) => {
                setLinked(e.target.checked)
                setSharedZoom(1)
              }}
            />
            Связать масштаб
          </label>
          <button type="button" className={`btn btn--small${showCard ? '' : ' btn--primary'}`} onClick={() => setShowCard((v) => !v)} aria-pressed={!showCard}>
            <Icon name="sidebar" size={14} /> {showCard ? 'Скрыть карточку' : 'Показать карточку'}
          </button>
        </div>
      </div>

      <aside className="wb__card" aria-label="Карточка доказательства">
        <FindingCardView card={card} onFocus={focusStage} />
      </aside>

      <section className="wb__panels" style={{ gridTemplateColumns: columns }} aria-label="Страницы ПД, РД и ИД">
        {shownStages.map((st) => (
          <EvidencePanel
            key={st}
            stage={st}
            sources={card.sources.filter((s) => s.stage === st)}
            files={files}
            stagePresent={stagePresent(st)}
            cardValue={st === 'PD' ? card.expected_value : (card.actual_values[st as 'RD' | 'ID'] ?? null)}
            cardKey={String(card.id)}
            focusReq={focusReq}
            linkedZoom={{ linked, zoom: sharedZoom, setZoom: setSharedZoom }}
            expanded={expanded === st}
            onToggleExpand={() => setExpanded(expanded === st ? null : st)}
            layoutKey={`${showCard ? 'card' : 'nocard'}-${expanded ?? 'all'}`}
          />
        ))}
      </section>

      <DecisionBar
        card={card}
        mode={mode}
        setMode={setMode}
        canDecide={canDecide}
        blockedReason={blockedReason}
        busy={busy}
        onDecide={decide}
        onPromote={promote}
        nav={nav}
        onNav={go}
        pendingLeft={pendingLeft}
      />
    </div>
  )
}
