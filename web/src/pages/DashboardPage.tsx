import { useMemo, useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { api } from '../api/endpoints'
import type { Indicator, ProcessOut } from '../api/types'
import { useAuth } from '../auth/AuthContext'
import { IndicatorDot, ProcessStatusBadge } from '../components/Badges'
import { Empty, ErrorBox, Loading } from '../components/Feedback'
import { Icon } from '../components/Icon'
import { formatAgo, formatDateTime, formatInt } from '../lib/format'
import { useAsync, useDebounced, useDocumentTitle, useInterval } from '../lib/hooks'
import { INDICATOR_LABEL, PIPELINE_STAGES, PROCESS_STATUSES, PROCESS_STATUS_LABEL, SCENARIO_LABEL, label } from '../lib/labels'

const PAGE = 50
const INDICATORS: Indicator[] = ['red', 'yellow', 'green', 'gray']
const IND_SHORT: Record<Indicator, string> = { red: 'Красные', yellow: 'Жёлтые', green: 'Зелёные', gray: 'Без протокола' }

function StageCell({ p }: { p: ProcessOut }) {
  if (p.status !== 'PARSING') return <span className="muted">—</span>
  const key = p.stage ?? ''
  const stage = PIPELINE_STAGES.find((s) => s.key === key)
  const prog = p.progress?.[key]
  const pct = prog?.total ? Math.round(((prog.done ?? 0) / prog.total) * 100) : null
  return (
    <div className="proc-stage">
      <span>
        {stage?.label ?? (key || 'В очереди')}
        {prog?.total ? (
          <span className="muted num">
            {' '}
            {formatInt(prog.done ?? 0)} из {formatInt(prog.total)}
          </span>
        ) : null}
      </span>
      <div className="progress" aria-hidden>
        <div className="progress__fill" style={{ width: `${pct ?? 0}%` }} />
      </div>
    </div>
  )
}

export function DashboardPage() {
  useDocumentTitle('Проверки')
  const { can } = useAuth()
  const navigate = useNavigate()
  const [params, setParams] = useSearchParams()
  const status = params.get('status') ?? ''
  const dateFrom = params.get('from') ?? ''
  const dateTo = params.get('to') ?? ''
  const [query, setQuery] = useState(params.get('q') ?? '')
  const q = useDebounced(query.trim(), 300)
  const [limit, setLimit] = useState(PAGE)
  const [ind, setInd] = useState<Indicator | null>(null)

  const list = useAsync(
    (signal) => api.listProcesses({ status, q, date_from: dateFrom, date_to: dateTo, limit }, signal),
    [status, q, dateFrom, dateTo, limit],
  )
  const items = useMemo(() => list.data?.items ?? [], [list.data])
  const parsing = items.some((p) => p.status === 'PARSING')
  useInterval(() => list.reload(true), 5000, parsing)

  const byInd = useMemo(() => {
    const m: Record<string, number> = {}
    for (const p of items) m[p.indicator] = (m[p.indicator] ?? 0) + 1
    return m
  }, [items])
  const shown = ind ? items.filter((p) => p.indicator === ind) : items
  const total = list.data?.total ?? 0

  const setParam = (key: string, v: string) => {
    const next = new URLSearchParams(params)
    if (v) next.set(key, v)
    else next.delete(key)
    setParams(next, { replace: true })
    setLimit(PAGE)
  }
  const setStatus = (v: string) => setParam('status', v)

  const filtered = !!status || !!q || !!ind || !!dateFrom || !!dateTo

  return (
    <div className="page">
      <div className="pagehead">
        <div>
          <h1>Проверки</h1>
          <p>Объекты на сверке проектной, рабочей и исполнительной документации. Цвет — итог по протоколу.</p>
        </div>
        {can('upload') ? (
          <Link to="/processes/new" className="btn btn--primary">
            <Icon name="plus" /> Новая проверка
          </Link>
        ) : null}
      </div>

      <div className="toolbar">
        <select className="select" value={status} onChange={(e) => setStatus(e.target.value)} aria-label="Статус проверки">
          <option value="">Все статусы</option>
          {PROCESS_STATUSES.map((s) => (
            <option key={s} value={s}>
              {PROCESS_STATUS_LABEL[s]}
            </option>
          ))}
        </select>
        <input
          className="input"
          type="search"
          placeholder="Объект, код объекта или адрес"
          value={query}
          onChange={(e) => {
            setQuery(e.target.value)
            setLimit(PAGE)
          }}
          aria-label="Поиск проверки"
        />
        <span className="datefilter" role="group" aria-label="Дата создания проверки">
          <span className="muted">Создана</span>
          <input className="input input--small" type="date" value={dateFrom} max={dateTo || undefined} onChange={(e) => setParam('from', e.target.value)} aria-label="Создана с" />
          <span className="muted">—</span>
          <input className="input input--small" type="date" value={dateTo} min={dateFrom || undefined} onChange={(e) => setParam('to', e.target.value)} aria-label="Создана по" />
          {dateFrom || dateTo ? (
            <button
              type="button"
              className="iconbtn iconbtn--small"
              onClick={() => {
                const next = new URLSearchParams(params)
                next.delete('from')
                next.delete('to')
                setParams(next, { replace: true })
              }}
              aria-label="Сбросить даты"
              title="Сбросить даты"
            >
              <Icon name="close" size={13} />
            </button>
          ) : null}
        </span>
        <div className="indfilter" role="group" aria-label="Фильтр по цвету">
          {INDICATORS.map((i) => (
            <button
              key={i}
              type="button"
              aria-pressed={ind === i}
              title={INDICATOR_LABEL[i]}
              onClick={() => setInd(ind === i ? null : i)}
            >
              <IndicatorDot value={i} />
              <span className="sr-only">{IND_SHORT[i]}</span>
              {byInd[i] ?? 0}
            </button>
          ))}
        </div>
        <span className="toolbar__spacer" />
        <span className="muted num">
          {ind ? `${shown.length} из ${items.length} загруженных` : `Всего: ${formatInt(total)}`}
        </span>
      </div>

      {list.error && !list.data ? <ErrorBox error={list.error} onRetry={() => list.reload()} /> : null}
      {list.loading && !list.data ? <Loading /> : null}

      {list.data && shown.length === 0 ? (
        filtered ? (
          <Empty title="По фильтру ничего не найдено">Измените статус, даты, цвет или строку поиска.</Empty>
        ) : (
          <Empty title="Проверок пока нет">
            Создайте первую: укажите объект и загрузите его ПД, РД и ИД.
            {can('upload') ? (
              <div>
                <Link to="/processes/new" className="btn btn--primary">
                  Новая проверка
                </Link>
              </div>
            ) : null}
          </Empty>
        )
      ) : null}

      {shown.length > 0 ? (
        <div className="tablewrap">
          <table className="table table--hover">
            <thead>
              <tr>
                <th className="col-tight" aria-label="Цвет" />
                <th>Объект</th>
                <th>Статус</th>
                <th>Обработка</th>
                <th>Сценарий</th>
                <th className="col-num" title="Ждут решения / всего кандидатов">
                  Кандидаты
                </th>
                <th className="col-num">Подтверждено</th>
                <th className="col-num">Протокол</th>
                <th>Обновлено</th>
              </tr>
            </thead>
            <tbody>
              {shown.map((p) => {
                const c = p.counts ?? {}
                return (
                  <tr key={p.id} onClick={() => navigate(`/processes/${p.id}`)}>
                    <td className="col-tight">
                      <IndicatorDot value={p.indicator} />
                    </td>
                    <td>
                      <Link to={`/processes/${p.id}`} className="proc-name" onClick={(e) => e.stopPropagation()}>
                        {p.object.name || p.object.object_id}
                      </Link>
                      <span className="cell-sub">
                        <span className="mono">{p.object.object_id}</span>
                        {p.object.address ? `, ${p.object.address}` : ''}
                      </span>
                    </td>
                    <td>
                      <ProcessStatusBadge status={p.status} />
                      {p.status === 'FAILED' && p.error ? (
                        <span className="cell-sub cell-clip" title={p.error}>
                          {p.error}
                        </span>
                      ) : null}
                    </td>
                    <td>
                      <StageCell p={p} />
                    </td>
                    <td>{label(SCENARIO_LABEL, p.scenario)}</td>
                    <td className="col-num">
                      {c.candidates ? (
                        <span className="cands">
                          {c.candidates_pending ? <span className="cands__pending">{c.candidates_pending}</span> : '0'}
                          <span className="muted"> / {c.candidates}</span>
                        </span>
                      ) : (
                        <span className="muted">0</span>
                      )}
                    </td>
                    <td className="col-num">{c.confirmed ? <strong>{c.confirmed}</strong> : <span className="muted">0</span>}</td>
                    <td className="col-num">{p.protocol_version ? `v${p.protocol_version}` : <span className="muted">—</span>}</td>
                    <td className="nowrap" title={formatDateTime(p.updated_at)}>
                      {formatAgo(p.updated_at)}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      ) : null}

      {items.length < total && !ind ? (
        <div className="btnrow" style={{ marginTop: 12 }}>
          <button type="button" className="btn" onClick={() => setLimit((l) => l + PAGE)} disabled={list.loading}>
            Показать ещё {Math.min(PAGE, total - items.length)}
          </button>
        </div>
      ) : null}
    </div>
  )
}
