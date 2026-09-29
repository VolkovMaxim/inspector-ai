// GOLD и дообучение (ml, admin): черновик из решений финализированных протоколов, публикация версии,
// версии с хешами и разбиением по объектам, выгрузка JSONL, отчёт по отклонениям, классификатор страниц
// (дообучение на правках разметчиков), реестр моделей.
import { useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { downloadFile, errorText } from '../api/client'
import { api } from '../api/endpoints'
import type { RetrainingReport } from '../api/types'
import { Copyable, Empty, ErrorBox, Loading, Tabs, type TabDef } from '../components/Feedback'
import { Icon } from '../components/Icon'
import { Modal } from '../components/Modal'
import { useToast } from '../components/Toasts'
import { countLabel, formatDateTime, formatInt, percent, percentValue, shortHash } from '../lib/format'
import { useAsync, useDocumentTitle } from '../lib/hooks'
import { REASON_LABEL, label } from '../lib/labels'
import { PageClassifierTab } from './PageClassifierTab'
import { PageRouterTab } from './PageRouterTab'

type Tab = 'dataset' | 'report' | 'models' | 'pagecls' | 'router'
const SPLITS = ['train', 'validation', 'test']
const SPLIT_LABEL: Record<string, string> = { train: 'обучение', validation: 'валидация', test: 'тест' }

function GoldLabel({ value }: { value: string }) {
  return value === 'POSITIVE' ? (
    <span className="badge badge--CONFIRMED_VIOLATION" title="POSITIVE">
      Нарушение (+)
    </span>
  ) : value === 'NEGATIVE' ? (
    <span className="badge badge--NEGATIVE_VERIFIED" title="NEGATIVE">
      Нет нарушения (−)
    </span>
  ) : (
    <span className="mono">{value}</span>
  )
}

function DatasetTab() {
  const toast = useToast()
  const drafts = useAsync((signal) => api.datasetItems('draft', signal), [])
  const versions = useAsync((signal) => api.datasetVersions(signal), [])
  const [name, setName] = useState('')
  const [confirm, setConfirm] = useState(false)
  const [busy, setBusy] = useState(false)
  const n = drafts.data?.length ?? 0

  const publish = async () => {
    setBusy(true)
    try {
      const v = await api.publishDataset(name.trim() || undefined)
      toast.ok(`Опубликована версия ${v.version}: ${countLabel(v.items, ['элемент', 'элемента', 'элементов'])}`)
      setName('')
      setConfirm(false)
      drafts.reload(true)
      versions.reload(true)
    } catch (e) {
      toast.error(`Не опубликовано: ${errorText(e)}`)
    } finally {
      setBusy(false)
    }
  }

  const exportVersion = async (v: string) => {
    try {
      await downloadFile(api.datasetExportPath(v), `${v}.jsonl`)
    } catch (e) {
      toast.error(`Выгрузка не получена: ${errorText(e)}`)
    }
  }

  return (
    <>
      <section className="section">
        <div className="section__head">
          <h2>Черновик</h2>
          <p>Подтверждённые нарушения (+) и отклонённые кандидаты (−) из финализированных протоколов; в набор попадают после публикации</p>
        </div>
        <div className="toolbar">
          <input className="input mono" style={{ width: 220 }} placeholder="gold-vN (по умолчанию)" value={name} onChange={(e) => setName(e.target.value)} aria-label="Имя версии" />
          <button type="button" className="btn btn--primary" disabled={!n || busy} onClick={() => setConfirm(true)}>
            Опубликовать версию
          </button>
          <span className="toolbar__spacer" />
          <span className="muted num">В черновике: {formatInt(n)}</span>
        </div>
        {drafts.error && !drafts.data ? <ErrorBox error={drafts.error} onRetry={() => drafts.reload()} /> : null}
        {drafts.loading && !drafts.data ? <Loading /> : null}
        {drafts.data && !n ? <Empty title="Черновик пуст">Он пополняется при финализации протоколов с решениями инспекторов.</Empty> : null}
        {n ? (
          <div className="tablewrap" style={{ maxHeight: 420, overflow: 'auto' }}>
            <table className="table table--compact">
              <thead>
                <tr>
                  <th>Объект</th>
                  <th>Параметр</th>
                  <th>Метка</th>
                  <th>Причина отклонения</th>
                  <th>Эксперт</th>
                  <th>Карточка</th>
                  <th>Создан</th>
                </tr>
              </thead>
              <tbody>
                {drafts.data!.map((i) => (
                  <tr key={i.id}>
                    <td className="mono">{i.object_id ?? i.object_code}</td>
                    <td className="mono">{i.param_code}</td>
                    <td>
                      <GoldLabel value={i.gold_label} />
                    </td>
                    <td>{label(REASON_LABEL, i.reason_code)}</td>
                    <td>{i.expert ?? '—'}</td>
                    <td className="mono cell-clip" title={i.evidence_group_id}>
                      {i.evidence_group_id}
                    </td>
                    <td className="nowrap">{formatDateTime(i.created_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : null}
      </section>

      <section className="section">
        <div className="section__head">
          <h2>Версии</h2>
          <p>Разбиение по объектам детерминированно: все элементы объекта — в одном наборе</p>
        </div>
        {versions.error && !versions.data ? <ErrorBox error={versions.error} onRetry={() => versions.reload()} /> : null}
        {versions.loading && !versions.data ? <Loading /> : null}
        {versions.data && !versions.data.length ? <Empty title="Версий пока нет">Опубликуйте черновик — появится gold-v1.</Empty> : null}
        {versions.data?.length ? (
          <div className="tablewrap">
            <table className="table">
              <thead>
                <tr>
                  <th>Версия</th>
                  <th className="col-num">Элементов</th>
                  <th className="col-num">+ / −</th>
                  <th>Разбиение: элементы (объекты)</th>
                  <th>SHA-256 выгрузки</th>
                  <th>Хеши наборов</th>
                  <th>Опубликовал</th>
                  <th aria-label="Выгрузка" />
                </tr>
              </thead>
              <tbody>
                {versions.data.map((v) => (
                  <tr key={v.version}>
                    <td className="mono">{v.version}</td>
                    <td className="col-num">{formatInt(v.items)}</td>
                    <td className="col-num">
                      {v.positives} / {v.negatives}
                    </td>
                    <td>
                      <span className="split">
                        {SPLITS.map((s) => (
                          <span key={s} title={(v.split_objects?.[s] ?? []).join(', ')}>
                            {SPLIT_LABEL[s]}: {v.split_counts?.[s] ?? '—'} ({v.split_objects?.[s]?.length ?? 0})
                          </span>
                        ))}
                      </span>
                    </td>
                    <td>
                      <Copyable value={v.sha256} display={shortHash(v.sha256, 16)} />
                    </td>
                    <td>
                      <details>
                        <summary>3 набора</summary>
                        {SPLITS.map((s) => (
                          <div key={s} style={{ fontSize: 12 }}>
                            {SPLIT_LABEL[s]}: {v.split_hashes?.[s] ? <Copyable value={v.split_hashes[s]} display={shortHash(v.split_hashes[s], 16)} /> : '—'}
                          </div>
                        ))}
                      </details>
                    </td>
                    <td className="nowrap">
                      {v.created_by ?? '—'}
                      <span className="cell-sub">{formatDateTime(v.created_at)}</span>
                    </td>
                    <td className="col-tight">
                      <button type="button" className="btn btn--small" onClick={() => exportVersion(v.version)}>
                        <Icon name="download" size={14} /> JSONL
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : null}
      </section>

      <Modal
        open={confirm}
        title="Опубликовать версию GOLD"
        onClose={() => setConfirm(false)}
        footer={
          <>
            <button type="button" className="btn" onClick={() => setConfirm(false)}>
              Отмена
            </button>
            <button type="button" className="btn btn--primary" onClick={publish} disabled={busy}>
              {busy ? 'Публикуем…' : 'Опубликовать'}
            </button>
          </>
        }
      >
        <p>
          Все элементы черновика ({formatInt(n)}) войдут в версию {name.trim() ? <span className="mono">{name.trim()}</span> : 'со следующим номером'}.
          Версия неизменяема: для неё сохраняются SHA-256 выгрузки и каждого набора.
        </p>
      </Modal>
    </>
  )
}

function toPairs(v: RetrainingReport['rejection_by_reason'] | RetrainingReport['rejection_by_section'], key: 'reason_code' | 'section'): [string, number][] {
  if (Array.isArray(v)) return v.map((x) => [String((x as Record<string, unknown>)[key]), Number(x.count)])
  return Object.entries(v ?? {}).map(([k, n]) => [k, Number(n)])
}

function BarTable({ rows, total, labelOf, head }: { rows: [string, number][]; total: number; labelOf: (k: string) => string; head: string }) {
  if (!rows.length) return <p className="muted">Отклонений нет.</p>
  return (
    <div className="tablewrap">
      <table className="table stats">
        <thead>
          <tr>
            <th>{head}</th>
            <th className="col-num">Отклонений</th>
            <th className="col-num">Доля</th>
            <th aria-label="Диаграмма" />
          </tr>
        </thead>
        <tbody>
          {[...rows]
            .sort((a, b) => b[1] - a[1])
            .map(([k, n]) => (
              <tr key={k}>
                <td title={k}>{labelOf(k)}</td>
                <td className="col-num">{n}</td>
                <td className="col-num">{percent(n, total)}</td>
                <td className="col-bar">
                  <div className="bar" aria-hidden>
                    <div className="bar__fill" style={{ width: `${percentValue(n, total)}%` }} />
                  </div>
                </td>
              </tr>
            ))}
        </tbody>
      </table>
    </div>
  )
}

function ReportTab() {
  const [days, setDays] = useState(7)
  const rep = useAsync((signal) => api.retrainingReport(days, signal), [days])
  const r = rep.data
  const rejected = r?.decisions.rejected ?? 0
  return (
    <>
      <div className="toolbar">
        <label className="check">
          Период:
          <select className="select select--small" value={days} onChange={(e) => setDays(Number(e.target.value))}>
            <option value={7}>7 дней</option>
            <option value={30}>30 дней</option>
            <option value={90}>90 дней</option>
            <option value={365}>год</option>
          </select>
        </label>
      </div>
      {rep.error && !r ? <ErrorBox error={rep.error} onRetry={() => rep.reload()} /> : null}
      {rep.loading && !r ? <Loading /> : null}
      {r ? (
        <div className="twocol">
          <div>
            <section className="section">
              <div className="section__head">
                <h2>Решения инспекторов за {r.period_days} дн.</h2>
              </div>
              <dl className="kv">
                <dt>Подтверждено</dt>
                <dd className="num">{formatInt(r.decisions.confirmed ?? 0)}</dd>
                <dt>Отклонено</dt>
                <dd className="num">{formatInt(rejected)}</dd>
                <dt>На уточнение</dt>
                <dd className="num">{formatInt(r.decisions.clarification ?? 0)}</dd>
                <dt>Доля подтверждений</dt>
                <dd className="num">{r.confirm_rate === null ? '—' : `${Math.round(r.confirm_rate * 1000) / 10} %`}</dd>
                <dt>Черновик GOLD</dt>
                <dd className="num">{formatInt(r.dataset?.draft_items ?? 0)}</dd>
                <dt>Последняя версия</dt>
                <dd className="mono">{r.dataset?.latest_version ?? '—'}</dd>
              </dl>
            </section>
            <section className="section">
              <div className="section__head">
                <h2>Отклонения по причинам</h2>
              </div>
              <BarTable rows={toPairs(r.rejection_by_reason, 'reason_code')} total={rejected} labelOf={(k) => label(REASON_LABEL, k)} head="Причина" />
            </section>
            <section className="section">
              <div className="section__head">
                <h2>Отклонения по разделам</h2>
              </div>
              <BarTable rows={toPairs(r.rejection_by_section, 'section')} total={rejected} labelOf={(k) => k} head="Раздел" />
            </section>
          </div>
          <div>
            <section className="section">
              <div className="section__head">
                <h2>Рекомендации</h2>
              </div>
              {r.recommendations.length ? (
                <ul className="recs">
                  {r.recommendations.map((t, i) => (
                    <li key={i}>{t}</li>
                  ))}
                </ul>
              ) : (
                <p className="muted">Рекомендаций нет: ни одна причина не превысила порог.</p>
              )}
            </section>
            <section className="section">
              <div className="section__head">
                <h2>Параметры с отклонениями</h2>
              </div>
              {r.rejection_by_param.length ? (
                <div className="tablewrap">
                  <table className="table table--compact">
                    <thead>
                      <tr>
                        <th>Параметр</th>
                        <th className="col-num">Отклонений</th>
                      </tr>
                    </thead>
                    <tbody>
                      {r.rejection_by_param.map((x) => (
                        <tr key={x.code}>
                          <td className="mono">{x.code}</td>
                          <td className="col-num">{x.count}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              ) : (
                <p className="muted">Отклонений нет.</p>
              )}
            </section>
          </div>
        </div>
      ) : null}
    </>
  )
}

function ModelsTab() {
  const models = useAsync((signal) => api.models(signal), [])
  if (models.error && !models.data) return <ErrorBox error={models.error} onRetry={() => models.reload()} />
  if (models.loading && !models.data) return <Loading />
  const rows = models.data ?? []
  if (!rows.length) return <Empty title="Реестр пуст">Текущая версия моделей записывается при старте сервиса.</Empty>
  return (
    <div className="tablewrap">
      <table className="table">
        <thead>
          <tr>
            <th>Версия моделей и промптов</th>
            <th>Артефакт</th>
            <th>Набор GOLD</th>
            <th>Статус</th>
            <th>Утвердил</th>
            <th>Развёрнута</th>
            <th>Откат на</th>
            <th>Метрики</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((m) => (
            <tr key={m.model_version}>
              <td className="mono" style={{ maxWidth: 460, overflowWrap: 'anywhere' }}>
                {m.model_version}
                {m.is_current ? <span className="cell-sub" style={{ fontFamily: 'var(--font-ui)' }}>используется сейчас</span> : null}
              </td>
              <td>{m.artifact}</td>
              <td className="mono">{m.dataset_version}</td>
              <td>{m.approval_status}</td>
              <td>{m.approved_by || '—'}</td>
              <td className="nowrap">{formatDateTime(m.deployed_at)}</td>
              <td className="mono">{m.rollback_to ?? '—'}</td>
              <td>
                {m.metrics && Object.keys(m.metrics).length ? (
                  <details>
                    <summary>показать</summary>
                    <pre className="json">{JSON.stringify(m.metrics, null, 1)}</pre>
                  </details>
                ) : (
                  <span className="muted">—</span>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

export function GoldPage() {
  useDocumentTitle('GOLD и дообучение')
  const [params, setParams] = useSearchParams()
  const raw = params.get('tab')
  const tab: Tab = raw === 'report' || raw === 'models' || raw === 'pagecls' || raw === 'router' ? raw : 'dataset'
  const tabs: TabDef<Tab>[] = [
    { key: 'dataset', label: 'Набор GOLD' },
    { key: 'report', label: 'Отчёт по дообучению' },
    { key: 'pagecls', label: 'Классификатор страниц' },
    { key: 'router', label: 'Отбор страниц' },
    { key: 'models', label: 'Реестр моделей' },
  ]
  return (
    <div className="page">
      <div className="pagehead">
        <div>
          <h1>GOLD и дообучение</h1>
          <p>Эталон из решений инспекторов: что подтверждено, что отклонено и почему</p>
        </div>
      </div>
      <Tabs tabs={tabs} active={tab} onChange={(t) => setParams(t === 'dataset' ? {} : { tab: t }, { replace: true })} label="Разделы GOLD" />
      <div style={{ marginTop: 16 }}>
        {tab === 'dataset' ? <DatasetTab /> : null}
        {tab === 'report' ? <ReportTab /> : null}
        {tab === 'models' ? <ModelsTab /> : null}
        {tab === 'pagecls' ? <PageClassifierTab /> : null}
        {tab === 'router' ? <PageRouterTab /> : null}
      </div>
    </div>
  )
}
