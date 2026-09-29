import { useEffect, useMemo, useRef, useState } from 'react'
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { ApiError, downloadFile, errorText } from '../api/client'
import { api } from '../api/endpoints'
import type { ProcessOut } from '../api/types'
import { useAuth } from '../auth/AuthContext'
import { decisionsAllowed, uploadAllowed } from '../auth/permissions'
import { IndicatorDot, ProcessStatusBadge, SyncBadge } from '../components/Badges'
import { ErrorBox, Loading, Tabs, type TabDef } from '../components/Feedback'
import { Icon } from '../components/Icon'
import { Modal } from '../components/Modal'
import { StageProgress } from '../components/StageProgress'
import { useToast } from '../components/Toasts'
import { COMPLETENESS_STATUSES, isPending, listFor } from '../lib/findings'
import { formatDateTime } from '../lib/format'
import { useAsync, useDocumentTitle, useInterval } from '../lib/hooks'
import { SCENARIO_LABEL, label } from '../lib/labels'
import { DocumentsTab } from './process/DocumentsTab'
import { CandidatesTab, CompletenessTab, ConfirmedTab, HypothesesTab, NegativesTab } from './process/FindingTabs'
import { JournalTab } from './process/JournalTab'
import { PagesTab } from './process/PagesTab'
import { SummaryTab, UploadStatusTable } from './process/SummaryTab'

type TabKey = 'summary' | 'candidates' | 'confirmed' | 'negatives' | 'completeness' | 'hypotheses' | 'documents' | 'pages' | 'journal'
const TAB_KEYS: TabKey[] = ['summary', 'candidates', 'confirmed', 'negatives', 'completeness', 'hypotheses', 'documents', 'pages', 'journal']
const PROTOCOL_TABS: TabKey[] = ['candidates', 'confirmed', 'negatives', 'completeness', 'hypotheses']

type ExportKind = 'pdf' | 'docx' | 'xml' | 'submission'
const EXPORTS: { kind: ExportKind; label: string; ext: string }[] = [
  { kind: 'pdf', label: 'PDF', ext: 'pdf' },
  { kind: 'docx', label: 'DOCX', ext: 'docx' },
  { kind: 'xml', label: 'XML', ext: 'xml' },
  { kind: 'submission', label: 'Сабмит JSON', ext: 'json' },
]

function FinalizeModal({
  process,
  open,
  onClose,
  onDone,
  firstPendingId,
}: {
  process: ProcessOut
  open: boolean
  onClose: () => void
  onDone: () => void
  firstPendingId: number | null
}) {
  const toast = useToast()
  const navigate = useNavigate()
  const [comment, setComment] = useState('')
  const [busy, setBusy] = useState(false)
  const [pending, setPending] = useState<{ count: number; first: number | null } | null>(null)
  const known = process.counts?.candidates_pending ?? 0
  const blockCount = pending?.count ?? known
  const firstId = pending?.first ?? firstPendingId

  const submit = async () => {
    setBusy(true)
    try {
      await api.finalize(process.id, comment.trim() || undefined)
      toast.ok('Протокол финализирован')
      setComment('')
      onDone()
      onClose()
    } catch (e) {
      if (e instanceof ApiError && e.code === 'PENDING_CANDIDATES') {
        const d = (e.details ?? {}) as { pending?: number; finding_ids?: number[] }
        setPending({ count: d.pending ?? known, first: d.finding_ids?.[0] ?? null })
      } else toast.error(`Не финализирован: ${errorText(e)}`)
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal
      open={open}
      title="Финализировать протокол"
      onClose={onClose}
      footer={
        <>
          <button type="button" className="btn" onClick={onClose}>
            Отмена
          </button>
          <button type="button" className="btn btn--primary" onClick={submit} disabled={busy || blockCount > 0}>
            {busy ? 'Финализируем…' : 'Финализировать'}
          </button>
        </>
      }
    >
      {blockCount > 0 ? (
        <div className="note note--bad">
          Без решения осталось кандидатов: <strong>{blockCount}</strong>. Примите решение по каждому или перенесите в «Требует
          уточнения» — затем финализируйте.
          {firstId ? (
            <div style={{ marginTop: 8 }}>
              <button type="button" className="btn btn--small" onClick={() => navigate(`/processes/${process.id}/findings/${firstId}?list=candidates`)}>
                Открыть первого нерешённого
              </button>
            </div>
          ) : null}
        </div>
      ) : (
        <p>
          Будет сохранена итоговая версия протокола. После этого решения по карточкам и дозагрузка недоступны; отменить
          финализацию может супервизор с указанием причины. Решения этой проверки попадут в черновик GOLD.
        </p>
      )}
      <label className="field">
        <span className="field__label">Комментарий (необязательно)</span>
        <textarea className="textarea" value={comment} onChange={(e) => setComment(e.target.value)} disabled={blockCount > 0} />
      </label>
    </Modal>
  )
}

function UnfinalizeModal({ process, open, onClose, onDone }: { process: ProcessOut; open: boolean; onClose: () => void; onDone: () => void }) {
  const toast = useToast()
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const submit = async () => {
    if (!reason.trim()) {
      setError('Укажите причину отмены')
      return
    }
    setBusy(true)
    try {
      await api.unfinalize(process.id, reason.trim())
      toast.ok('Финализация отменена: протокол снова открыт для решений')
      setReason('')
      setError(null)
      onDone()
      onClose()
    } catch (e) {
      setError(errorText(e))
    } finally {
      setBusy(false)
    }
  }
  return (
    <Modal
      open={open}
      title="Отменить финализацию"
      onClose={onClose}
      footer={
        <>
          <button type="button" className="btn" onClick={onClose}>
            Отмена
          </button>
          <button type="button" className="btn btn--danger" onClick={submit} disabled={busy}>
            {busy ? 'Отменяем…' : 'Отменить финализацию'}
          </button>
        </>
      }
    >
      <p>
        Протокол вернётся в статус «Верификация завершена», черновик GOLD этой проверки будет снят, запланированные повторы
        отправки в РиН — отменены. Причина попадёт в журнал.
      </p>
      <label className="field">
        <span className="field__label">
          Причина <span className="field__req">*</span>
        </span>
        <textarea className="textarea" value={reason} onChange={(e) => setReason(e.target.value)} autoFocus />
      </label>
      {error ? <p className="dbar__error">{error}</p> : null}
    </Modal>
  )
}

function RinModal({ process, open, onClose, onDone, confirmed }: { process: ProcessOut; open: boolean; onClose: () => void; onDone: () => void; confirmed: number }) {
  const toast = useToast()
  const [busy, setBusy] = useState(false)
  const submit = async () => {
    setBusy(true)
    try {
      const res = await api.sendToRin(process.id)
      const st = res?.sync_status
      if (st === 'SYNCED') toast.ok('Протокол передан в ИАИС РиН')
      else toast.info(`РиН не принял с первого раза: ${label({ PENDING_SYNC: 'повтор запланирован', FAILED: 'передача не удалась' }, st ?? '')}`)
      onDone()
      onClose()
    } catch (e) {
      toast.error(`Не отправлено: ${errorText(e)}`)
    } finally {
      setBusy(false)
    }
  }
  return (
    <Modal
      open={open}
      title="Отправить в ИАИС РиН"
      onClose={onClose}
      footer={
        <>
          <button type="button" className="btn" onClick={onClose}>
            Отмена
          </button>
          <button type="button" className="btn btn--primary" onClick={submit} disabled={busy}>
            <Icon name="send" /> {busy ? 'Отправляем…' : 'Отправить'}
          </button>
        </>
      }
    >
      <p>
        Уйдёт итоговая версия протокола v{process.protocol_version}: подтверждённые нарушения ({confirmed}), хеши входных файлов,
        версии матрицы и моделей. При сбое система повторит отправку через 1, 5 и 15 минут.
      </p>
    </Modal>
  )
}

export function ProcessPage() {
  const { id = '' } = useParams()
  const toast = useToast()
  const { can } = useAuth()
  const [params, setParams] = useSearchParams()
  const rawTab = params.get('tab') as TabKey | null
  const tab: TabKey = rawTab && TAB_KEYS.includes(rawTab) ? rawTab : 'summary'

  const proc = useAsync((signal) => api.getProcess(id, signal), [id])
  const p = proc.data
  const hasProtocol = !!p && p.protocol_version >= 1
  const protocol = useAsync((signal) => api.protocol(id, undefined, signal), [id], hasProtocol)
  useDocumentTitle(p ? p.object.name || p.object.object_id : 'Проверка')

  useInterval(() => proc.reload(true), 3000, p?.status === 'PARSING')

  // статус или версия протокола изменились (конвейер закончил, решения) — перечитать живой протокол
  const sig = p ? `${p.status}|${p.protocol_version}|${p.batch}` : ''
  const prevSig = useRef(sig)
  useEffect(() => {
    if (prevSig.current && sig && sig !== prevSig.current && hasProtocol) protocol.reload(true)
    prevSig.current = sig
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sig])

  const [modal, setModal] = useState<'finalize' | 'unfinalize' | 'rin' | null>(null)
  const [busy, setBusy] = useState<string | null>(null)

  const findings = useMemo(() => protocol.data?.findings ?? [], [protocol.data])
  const params_ = protocol.data?.params ?? []
  const counts = useMemo(() => {
    const cands = listFor(findings, 'candidates')
    return {
      candidates: cands.length,
      pending: cands.filter(isPending).length,
      firstPending: cands.find(isPending)?.id ?? null,
      confirmed: listFor(findings, 'confirmed').length,
      negatives: listFor(findings, 'negatives').length,
      hypotheses: listFor(findings, 'hypotheses').length,
      completeness: params_.filter((x) => COMPLETENESS_STATUSES.includes(x.status)).length,
    }
  }, [findings, params_])

  const refresh = () => {
    proc.reload(true)
    if (hasProtocol) protocol.reload(true)
  }

  const setTab = (t: TabKey) => {
    const next = new URLSearchParams(params)
    if (t === 'summary') next.delete('tab')
    else next.set('tab', t)
    setParams(next, { replace: true })
  }

  if (proc.loading && !p) return <div className="page"><Loading /></div>
  if (proc.error && !p) return <div className="page"><ErrorBox error={proc.error} onRetry={() => proc.reload()} /></div>
  if (!p) return null

  const status = p.status
  const canDecide = can('decide') && decisionsAllowed(status)

  const doExport = async (kind: ExportKind, ext: string) => {
    setBusy(kind)
    try {
      await downloadFile(api.exportPath(p.id, kind), `${kind === 'submission' ? 'submission' : 'protocol'}_${p.object.object_id}_v${p.protocol_version}.${ext}`)
    } catch (e) {
      toast.error(e instanceof ApiError && e.code === 'RENDER_UNAVAILABLE' ? 'Выгрузка в этот формат пока недоступна на сервере' : `Не выгружено: ${errorText(e)}`)
    } finally {
      setBusy(null)
    }
  }

  const start = async () => {
    setBusy('start')
    try {
      await api.startProcess(p.id)
      toast.ok('Проверка поставлена в очередь')
      refresh()
    } catch (e) {
      toast.error(errorText(e))
    } finally {
      setBusy(null)
    }
  }

  const tabs: TabDef<TabKey>[] = [
    { key: 'summary', label: 'Сводка' },
    ...(hasProtocol
      ? ([
          { key: 'candidates', label: 'Кандидаты', count: counts.pending ? counts.pending : counts.candidates, tone: counts.pending ? 'alert' : 'muted' },
          { key: 'confirmed', label: 'Подтверждённые', count: counts.confirmed },
          { key: 'negatives', label: 'Проверенные отрицательные', count: counts.negatives, tone: 'muted' },
          { key: 'completeness', label: 'Комплектность', count: counts.completeness, tone: 'muted' },
          { key: 'hypotheses', label: 'Гипотезы', count: counts.hypotheses, tone: 'muted' },
        ] as TabDef<TabKey>[])
      : []),
    { key: 'documents', label: 'Документы', count: p.counts?.files ?? null, tone: 'muted' },
    { key: 'pages', label: 'Страницы', count: p.counts?.pages ?? null, tone: 'muted' },
    { key: 'journal', label: 'Журнал' },
  ]
  const activeTab: TabKey = !hasProtocol && PROTOCOL_TABS.includes(tab) ? 'summary' : tab
  const showStages = status === 'PARSING' || status === 'FAILED' || Object.keys(p.progress ?? {}).length > 0

  return (
    <>
      <div className="phead">
        <div className="phead__inner">
          <Link to="/" className="backlink">
            <Icon name="back" size={14} /> К списку проверок
          </Link>
          <div className="phead__top">
            <div>
              <div className="phead__title">
                <IndicatorDot value={p.indicator} />
                <h1>{p.object.name || p.object.object_id}</h1>
              </div>
              {p.object.address ? <p className="phead__addr">{p.object.address}</p> : null}
              <div className="phead__meta">
                <ProcessStatusBadge status={status} />
                <span>
                  <span className="muted">Код объекта</span>
                  <span className="mono">{p.object.object_id}</span>
                </span>
                <span>
                  <span className="muted">Сценарий</span>
                  {label(SCENARIO_LABEL, p.scenario, 'определится после обработки')}
                </span>
                <span>
                  <span className="muted">Протокол</span>
                  {p.protocol_version ? <span className="mono">v{p.protocol_version}</span> : 'ещё не сформирован'}
                </span>
                <span>
                  <span className="muted">Обновлено</span>
                  {formatDateTime(p.updated_at)}
                </span>
                {status === 'FINALIZED' || p.sync_status !== 'NONE' ? <SyncBadge status={p.sync_status} /> : null}
              </div>
            </div>
            <div className="phead__actions">
              {hasProtocol ? (
                <div className="btngroup" role="group" aria-label="Выгрузки протокола">
                  {EXPORTS.map((x) => (
                    <button key={x.kind} type="button" className="btn btn--small" disabled={busy === x.kind} onClick={() => doExport(x.kind, x.ext)} title={`Скачать протокол: ${x.label}`}>
                      {x.kind === 'pdf' ? <Icon name="download" size={14} /> : null}
                      {x.label}
                    </button>
                  ))}
                </div>
              ) : null}
              {can('upload') && uploadAllowed(status) ? (
                <Link to={`/processes/${p.id}/upload`} className="btn btn--small">
                  <Icon name="upload" size={14} /> Дозагрузить
                </Link>
              ) : null}
              {can('start') && (status === 'PENDING' || status === 'FAILED') && (p.counts?.files ?? 0) > 0 ? (
                <button type="button" className="btn btn--small btn--primary" onClick={start} disabled={busy === 'start'}>
                  {status === 'FAILED' ? 'Запустить повторно' : 'Запустить проверку'}
                </button>
              ) : null}
              {can('finalize') && decisionsAllowed(status) && hasProtocol ? (
                <button type="button" className="btn btn--small btn--primary" onClick={() => setModal('finalize')}>
                  <Icon name="lock" size={14} /> Финализировать
                </button>
              ) : null}
              {can('unfinalize') && status === 'FINALIZED' ? (
                <button type="button" className="btn btn--small btn--danger" onClick={() => setModal('unfinalize')}>
                  Отменить финализацию
                </button>
              ) : null}
              {can('sendRin') && status === 'FINALIZED' && p.sync_status !== 'SYNCED' ? (
                <button type="button" className="btn btn--small btn--primary" onClick={() => setModal('rin')}>
                  <Icon name="send" size={14} /> Отправить в РиН
                </button>
              ) : null}
            </div>
          </div>
          {showStages ? <StageProgress process={p} /> : null}
          {status === 'PARSING' && !p.stage ? (
            <p className="muted" style={{ marginTop: 6, fontSize: 12 }}>
              Задача в очереди: обработка начнётся, когда освободится обработчик (worker).
            </p>
          ) : null}
          {status === 'FAILED' && p.error ? (
            <div style={{ marginTop: 10 }}>
              <ErrorBox error={new Error(p.error)} title="Обработка остановлена с ошибкой" onRetry={can('start') ? start : undefined} />
            </div>
          ) : null}
          <Tabs tabs={tabs} active={activeTab} onChange={setTab} label="Разделы проверки" />
        </div>
      </div>

      <div className="pbody">
        {activeTab === 'summary' && !hasProtocol ? (
          <>
            <section className="section">
              <div className="section__head">
                <h2>Статус загрузки по стадиям</h2>
                <p>Протокол появится после обработки пакета</p>
              </div>
              {p.upload_status?.length ? <UploadStatusTable rows={p.upload_status} /> : null}
            </section>
            {status === 'PENDING' ? (
              <p className="note" style={{ marginTop: 16 }}>
                Проверка ещё не запущена.{' '}
                {(p.counts?.files ?? 0) > 0 ? 'Файлы загружены — нажмите «Запустить проверку».' : 'Загрузите файлы через «Дозагрузить».'}
              </p>
            ) : null}
          </>
        ) : null}

        {hasProtocol && protocol.error && !protocol.data ? <ErrorBox error={protocol.error} onRetry={() => protocol.reload()} title="Протокол не загрузился" /> : null}
        {hasProtocol && protocol.loading && !protocol.data && !['documents', 'pages', 'journal'].includes(activeTab) ? <Loading text="Загрузка протокола…" /> : null}

        {protocol.data && activeTab === 'summary' ? <SummaryTab protocol={protocol.data} /> : null}
        {protocol.data && activeTab === 'candidates' ? <CandidatesTab processId={p.id} findings={findings} canDecide={canDecide} onChanged={refresh} /> : null}
        {protocol.data && activeTab === 'confirmed' ? <ConfirmedTab processId={p.id} findings={findings} canDecide={canDecide} onChanged={refresh} /> : null}
        {protocol.data && activeTab === 'negatives' ? <NegativesTab processId={p.id} findings={findings} canDecide={canDecide} onChanged={refresh} /> : null}
        {protocol.data && activeTab === 'completeness' ? <CompletenessTab processId={p.id} params={params_} findings={findings} /> : null}
        {protocol.data && activeTab === 'hypotheses' ? <HypothesesTab processId={p.id} findings={findings} canDecide={canDecide} onChanged={refresh} /> : null}
        {activeTab === 'documents' ? <DocumentsTab processId={p.id} /> : null}
        {activeTab === 'pages' ? <PagesTab processId={p.id} /> : null}
        {activeTab === 'journal' ? <JournalTab process={p} /> : null}
      </div>

      <FinalizeModal process={p} open={modal === 'finalize'} onClose={() => setModal(null)} onDone={refresh} firstPendingId={counts.firstPending} />
      <UnfinalizeModal process={p} open={modal === 'unfinalize'} onClose={() => setModal(null)} onDone={refresh} />
      <RinModal process={p} open={modal === 'rin'} onClose={() => setModal(null)} onDone={refresh} confirmed={counts.confirmed} />
    </>
  )
}
