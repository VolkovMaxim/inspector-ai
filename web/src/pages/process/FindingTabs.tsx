import { useMemo, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { errorText } from '../../api/client'
import { api } from '../../api/endpoints'
import type { FindingCard, ParamRow } from '../../api/types'
import { DecisionBadge, PriorityMark, StatusBadge } from '../../components/Badges'
import { Empty } from '../../components/Feedback'
import { Modal } from '../../components/Modal'
import { useToast } from '../../components/Toasts'
import { COMPLETENESS_STATUSES, isPending, listFor, type ListKey } from '../../lib/findings'
import { formatDateTime } from '../../lib/format'
import { DISCOVERY_LABEL, FINDING_STATUS_LABEL, REASON_LABEL, label } from '../../lib/labels'
import { CodeCell, FilterBar, ParamCell, Value, useFindingFilter } from './common'

interface TabProps {
  processId: string
  findings: FindingCard[]
  canDecide: boolean
  onChanged: () => void
}

function openCard(navigate: ReturnType<typeof useNavigate>, processId: string, id: number, list: ListKey) {
  navigate(`/processes/${processId}/findings/${id}?list=${list}`)
}

// ---------------------------------------------------------------- кандидаты

export function CandidatesTab({ processId, findings, canDecide, onChanged }: TabProps) {
  const navigate = useNavigate()
  const toast = useToast()
  const all = useMemo(() => listFor(findings, 'candidates'), [findings])
  const { filter, setFilter, sections, shown } = useFindingFilter(all)
  const [selected, setSelected] = useState<Set<number>>(new Set())
  const [confirmOpen, setConfirmOpen] = useState(false)
  const [comment, setComment] = useState('')
  const [busy, setBusy] = useState(false)

  const selectable = shown.filter(isPending)
  const chosen = all.filter((c) => selected.has(c.id) && isPending(c))
  const allChecked = selectable.length > 0 && selectable.every((c) => selected.has(c.id))

  const toggle = (id: number) =>
    setSelected((s) => {
      const n = new Set(s)
      if (n.has(id)) n.delete(id)
      else n.add(id)
      return n
    })

  const bulk = async () => {
    setBusy(true)
    try {
      const res = await api.bulkConfirm(
        chosen.map((c) => c.id),
        comment.trim() || undefined,
      )
      const skipped = res.skipped?.length ?? 0
      toast.ok(`Подтверждено: ${res.updated?.length ?? 0}${skipped ? `, пропущено: ${skipped}` : ''}`)
      if (skipped) toast.info(res.skipped.map((s) => `${s.id}: ${s.reason}`).slice(0, 3).join('; '))
      setSelected(new Set())
      setComment('')
      setConfirmOpen(false)
      onChanged()
    } catch (e) {
      toast.error(`Не подтверждено: ${errorText(e)}`)
    } finally {
      setBusy(false)
    }
  }

  if (!all.length) return <Empty title="Кандидатов в нарушения нет">Судья и верификатор не нашли расхождений, требующих решения инспектора.</Empty>

  const pendingTotal = all.filter(isPending).length

  return (
    <>
      <FilterBar filter={filter} setFilter={setFilter} sections={sections} shown={shown.length} total={all.length}>
        <span className="muted">Ждут решения: {pendingTotal}</span>
      </FilterBar>
      {canDecide && chosen.length ? (
        <div className="bulkbar">
          <strong>Выбрано: {chosen.length}</strong>
          <button type="button" className="btn btn--small btn--primary" onClick={() => setConfirmOpen(true)}>
            Подтвердить выбранные
          </button>
          <button type="button" className="btn btn--small btn--ghost" onClick={() => setSelected(new Set())}>
            Снять выбор
          </button>
          <span className="muted">Отклонить можно только по одной карточке — с причиной и комментарием.</span>
        </div>
      ) : null}
      <div className="tablewrap">
        <table className="table table--hover">
          <thead>
            <tr>
              {canDecide ? (
                <th className="col-check">
                  <input
                    type="checkbox"
                    aria-label="Выбрать всех нерешённых"
                    checked={allChecked}
                    disabled={!selectable.length}
                    onChange={() =>
                      setSelected(allChecked ? new Set() : new Set(selectable.map((c) => c.id)))
                    }
                  />
                </th>
              ) : null}
              <th>Приоритет</th>
              <th>Код</th>
              <th>Параметр и локация</th>
              <th>ПД</th>
              <th>РД</th>
              <th>ИД</th>
              <th>Отклонение</th>
              <th>Решение инспектора</th>
            </tr>
          </thead>
          <tbody>
            {shown.map((c) => {
              const pending = isPending(c)
              return (
                <tr
                  key={c.id}
                  className={selected.has(c.id) ? 'is-selected' : pending ? undefined : 'is-dim'}
                  onClick={() => openCard(navigate, processId, c.id, 'candidates')}
                >
                  {canDecide ? (
                    <td className="col-check" onClick={(e) => e.stopPropagation()}>
                      <input
                        type="checkbox"
                        aria-label={`Выбрать ${c.code} ${c.location}`}
                        checked={selected.has(c.id)}
                        disabled={!pending}
                        onChange={() => toggle(c.id)}
                      />
                    </td>
                  ) : null}
                  <td>
                    <PriorityMark value={c.review_priority} />
                  </td>
                  <td>
                    <CodeCell c={c} />
                  </td>
                  <td>
                    <ParamCell c={c} />
                  </td>
                  <td>
                    <Value v={c.expected_value} />
                  </td>
                  <td>
                    <Value v={c.actual_values.RD} />
                  </td>
                  <td>
                    <Value v={c.actual_values.ID} />
                  </td>
                  <td className="cell-clip" title={c.delta ?? ''}>
                    {c.delta ?? <span className="muted">—</span>}
                  </td>
                  <td>
                    <DecisionBadge status={c.inspector.status} />
                    {c.inspector.user ? (
                      <span className="cell-sub">
                        {c.inspector.user}, {formatDateTime(c.inspector.decided_at)}
                      </span>
                    ) : null}
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>

      <Modal
        open={confirmOpen}
        title={`Подтвердить нарушения: ${chosen.length}`}
        onClose={() => setConfirmOpen(false)}
        footer={
          <>
            <button type="button" className="btn" onClick={() => setConfirmOpen(false)}>
              Отмена
            </button>
            <button type="button" className="btn btn--primary" disabled={busy || !chosen.length} onClick={bulk}>
              {busy ? 'Сохраняем…' : `Подтвердить ${chosen.length}`}
            </button>
          </>
        }
      >
        <p>Каждая карточка получит решение «Подтверждённое нарушение». Решение можно изменить до финализации протокола.</p>
        <ul style={{ margin: 0, paddingLeft: 18, maxHeight: 180, overflow: 'auto' }}>
          {chosen.map((c) => (
            <li key={c.id}>
              <span className="mono">{c.code}</span> {c.location || c.parameter_name}
            </li>
          ))}
        </ul>
        <label className="field">
          <span className="field__label">Комментарий ко всем (необязательно)</span>
          <textarea className="textarea" value={comment} onChange={(e) => setComment(e.target.value)} />
        </label>
      </Modal>
    </>
  )
}

// ---------------------------------------------------------------- подтверждённые и отрицательные

export function ConfirmedTab({ processId, findings }: TabProps) {
  const navigate = useNavigate()
  const all = useMemo(() => listFor(findings, 'confirmed'), [findings])
  const { filter, setFilter, sections, shown } = useFindingFilter(all)
  if (!all.length) return <Empty title="Подтверждённых нарушений нет">Они появятся, когда инспектор подтвердит кандидатов.</Empty>
  return (
    <>
      <FilterBar filter={filter} setFilter={setFilter} sections={sections} shown={shown.length} total={all.length} />
      <div className="tablewrap">
        <table className="table table--hover">
          <thead>
            <tr>
              <th>Код</th>
              <th>Параметр и локация</th>
              <th>ПД</th>
              <th>РД</th>
              <th>ИД</th>
              <th>Критичность</th>
              <th>Подтвердил</th>
              <th>Комментарий</th>
            </tr>
          </thead>
          <tbody>
            {shown.map((c) => (
              <tr key={c.id} onClick={() => openCard(navigate, processId, c.id, 'confirmed')}>
                <td>
                  <CodeCell c={c} />
                </td>
                <td>
                  <ParamCell c={c} />
                </td>
                <td>
                  <Value v={c.expected_value} />
                </td>
                <td>
                  <Value v={c.actual_values.RD} />
                </td>
                <td>
                  <Value v={c.actual_values.ID} />
                </td>
                <td>{c.criticality ?? '—'}</td>
                <td className="nowrap">
                  {c.inspector.user ?? '—'}
                  <span className="cell-sub">{formatDateTime(c.inspector.decided_at)}</span>
                </td>
                <td className="cell-clip" title={c.inspector.comment ?? ''}>
                  {c.inspector.comment || <span className="muted">—</span>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  )
}

export function NegativesTab({ processId, findings }: TabProps) {
  const navigate = useNavigate()
  const all = useMemo(() => listFor(findings, 'negatives'), [findings])
  const { filter, setFilter, sections, shown } = useFindingFilter(all)
  if (!all.length) return <Empty title="Проверенных отрицательных нет">Сюда попадают совпавшие значения и отклонённые инспектором кандидаты.</Empty>
  return (
    <>
      <FilterBar filter={filter} setFilter={setFilter} sections={sections} shown={shown.length} total={all.length} />
      <div className="tablewrap">
        <table className="table table--hover">
          <thead>
            <tr>
              <th>Код</th>
              <th>Параметр и локация</th>
              <th>ПД</th>
              <th>РД</th>
              <th>ИД</th>
              <th>Кто решил</th>
              <th>Причина отклонения</th>
              <th>Комментарий</th>
            </tr>
          </thead>
          <tbody>
            {shown.map((c) => {
              const byInspector = c.machine_status === 'CANDIDATE'
              return (
                <tr key={c.id} onClick={() => openCard(navigate, processId, c.id, 'negatives')}>
                  <td>
                    <CodeCell c={c} />
                  </td>
                  <td>
                    <ParamCell c={c} />
                  </td>
                  <td>
                    <Value v={c.expected_value} />
                  </td>
                  <td>
                    <Value v={c.actual_values.RD} />
                  </td>
                  <td>
                    <Value v={c.actual_values.ID} />
                  </td>
                  <td className="nowrap">
                    {byInspector ? (
                      <>
                        Инспектор {c.inspector.user ?? ''}
                        <span className="cell-sub">{formatDateTime(c.inspector.decided_at)}</span>
                      </>
                    ) : (
                      <span className="muted">Сверка: значения совпали</span>
                    )}
                  </td>
                  <td title={c.inspector.reason_code ?? ''}>{byInspector ? label(REASON_LABEL, c.inspector.reason_code) : '—'}</td>
                  <td className="cell-clip" title={c.inspector.comment ?? c.rationale}>
                    {byInspector ? c.inspector.comment : c.rationale}
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
    </>
  )
}

// ---------------------------------------------------------------- гипотезы

export function HypothesesTab({ processId, findings, canDecide, onChanged }: TabProps) {
  const navigate = useNavigate()
  const toast = useToast()
  const all = useMemo(() => listFor(findings, 'hypotheses'), [findings])
  const { filter, setFilter, sections, shown } = useFindingFilter(all)
  const [busy, setBusy] = useState<number | null>(null)

  const promote = async (c: FindingCard) => {
    setBusy(c.id)
    try {
      await api.promote(c.id)
      toast.ok(`Гипотеза ${c.code} ${c.location} переведена в кандидаты`)
      onChanged()
    } catch (e) {
      toast.error(errorText(e))
    } finally {
      setBusy(null)
    }
  }

  return (
    <>
      <div className="hypo-note" style={{ marginTop: 0, marginBottom: 12 }}>
        <StatusBadge status="SUSPICION" />
        <span>
          Гипотезы свободного поиска (экспликации помещений, квартирография) не входят в число нарушений. Привязать гипотезу к
          кандидатам можно, только если у неё есть страницы-доказательства.
        </span>
      </div>
      {!all.length ? (
        <Empty title="Гипотез нет">Свободный поиск не нашёл расхождений в таблицах помещений.</Empty>
      ) : (
        <>
          <FilterBar filter={filter} setFilter={setFilter} sections={sections} shown={shown.length} total={all.length} />
          <div className="tablewrap">
            <table className="table table--hover">
              <thead>
                <tr>
                  <th>Код</th>
                  <th>Что сравнивали</th>
                  <th>ПД</th>
                  <th>РД</th>
                  <th>ИД</th>
                  <th>Как найдено</th>
                  <th>Обоснование</th>
                  <th>Страницы</th>
                  {canDecide ? <th aria-label="Действие" /> : null}
                </tr>
              </thead>
              <tbody>
                {shown.map((c) => {
                  const pages = c.sources.filter((s) => s.page)
                  return (
                    <tr key={c.id} onClick={() => openCard(navigate, processId, c.id, 'hypotheses')}>
                      <td>
                        <CodeCell c={c} />
                      </td>
                      <td>
                        <ParamCell c={c} />
                      </td>
                      <td>
                        <Value v={c.expected_value} />
                      </td>
                      <td>
                        <Value v={c.actual_values.RD} />
                      </td>
                      <td>
                        <Value v={c.actual_values.ID} />
                      </td>
                      <td>{label(DISCOVERY_LABEL, c.discovery_method)}</td>
                      <td className="cell-clip" title={c.rationale}>
                        {c.rationale}
                      </td>
                      <td className="nowrap">
                        {pages.length
                          ? pages.map((s) => `${s.stage === 'PD' ? 'ПД' : s.stage === 'RD' ? 'РД' : 'ИД'} с. ${s.page}`).join(', ')
                          : '—'}
                      </td>
                      {canDecide ? (
                        <td className="col-tight" onClick={(e) => e.stopPropagation()}>
                          <button
                            type="button"
                            className="btn btn--small"
                            disabled={!pages.length || busy === c.id}
                            title={pages.length ? 'Перевести гипотезу в кандидаты на нарушение' : 'Нет страниц-доказательств'}
                            onClick={() => promote(c)}
                          >
                            Привязать
                          </button>
                        </td>
                      ) : null}
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        </>
      )}
    </>
  )
}

// ---------------------------------------------------------------- комплектность

export function CompletenessTab({ processId, params, findings }: { processId: string; params: ParamRow[]; findings: FindingCard[] }) {
  const [status, setStatus] = useState<string>('')
  const [q, setQ] = useState('')
  const rows = useMemo(() => params.filter((p) => COMPLETENESS_STATUSES.includes(p.status)), [params])
  const counts = useMemo(() => {
    const m: Record<string, number> = {}
    for (const r of rows) m[r.status] = (m[r.status] ?? 0) + 1
    return m
  }, [rows])
  const idByFindingId = useMemo(() => new Map(findings.map((f) => [f.finding_id, f])), [findings])
  const needle = q.trim().toLocaleLowerCase('ru')
  const shown = rows.filter(
    (r) =>
      (!status || r.status === status) &&
      (!needle || r.code.toLocaleLowerCase('ru').includes(needle) || r.name.toLocaleLowerCase('ru').includes(needle) || r.section.toLocaleLowerCase('ru').includes(needle)),
  )

  if (!rows.length) return <Empty title="Все параметры сопоставлены">Нет параметров без доказательств, неприменимых или несопоставимых.</Empty>

  return (
    <>
      <p className="note" style={{ marginBottom: 12 }}>
        Комплектность — состояние параметров матрицы, по которым вывода о нарушении нет: доказательств не нашли, параметр
        неприменим к объекту, значения несопоставимы или нужна уточнённая редакция. Это не кандидаты в нарушения.
      </p>
      <div className="toolbar">
        <div className="chips" role="group" aria-label="Статус комплектности">
          <button type="button" className="chip" aria-pressed={!status} onClick={() => setStatus('')}>
            Все <span className="chip__n">{rows.length}</span>
          </button>
          {COMPLETENESS_STATUSES.map((s) => (
            <button key={s} type="button" className="chip" aria-pressed={status === s} onClick={() => setStatus(status === s ? '' : s)} title={s}>
              {FINDING_STATUS_LABEL[s]} <span className="chip__n">{counts[s] ?? 0}</span>
            </button>
          ))}
        </div>
        <input className="input" type="search" placeholder="Код, раздел или параметр" value={q} onChange={(e) => setQ(e.target.value)} aria-label="Поиск параметра" />
      </div>
      <div className="tablewrap">
        <table className="table">
          <thead>
            <tr>
              <th>Код</th>
              <th>Раздел</th>
              <th>Параметр</th>
              <th>Статус</th>
              <th>Пояснение</th>
              <th>Карточки</th>
            </tr>
          </thead>
          <tbody>
            {shown.map((r) => (
              <tr key={r.code}>
                <td className="mono nowrap">{r.code}</td>
                <td>{r.section}</td>
                <td>{r.name}</td>
                <td>
                  <StatusBadge status={r.status} />
                </td>
                <td className="muted">{r.comment || '—'}</td>
                <td>
                  <div className="findlinks">
                    {r.finding_ids.length
                      ? r.finding_ids.map((fid) => {
                          const f = idByFindingId.get(fid)
                          return f ? (
                            <Link key={fid} to={`/processes/${processId}/findings/${f.id}?list=completeness`}>
                              {f.location || f.code}
                            </Link>
                          ) : (
                            <span key={fid} className="mono muted">
                              {fid}
                            </span>
                          )
                        })
                      : '—'}
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  )
}
