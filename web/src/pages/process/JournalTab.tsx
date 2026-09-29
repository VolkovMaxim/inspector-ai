import { useState } from 'react'
import { Link } from 'react-router-dom'
import { ApiError, errorText } from '../../api/client'
import { api } from '../../api/endpoints'
import type { AuditRow, ProcessOut } from '../../api/types'
import { SyncBadge } from '../../components/Badges'
import { Copyable, Empty, ErrorBox, Loading } from '../../components/Feedback'
import { Icon } from '../../components/Icon'
import { useToast } from '../../components/Toasts'
import { formatDateTime, shortHash } from '../../lib/format'
import { useAsync } from '../../lib/hooks'
import { AUDIT_ACTION_LABEL, PROCESS_STATUS_LABEL, SYNC_LABEL, VERSION_KIND_LABEL, label } from '../../lib/labels'

function saveJson(data: unknown, name: string) {
  const blob = new Blob([JSON.stringify(data, null, 1)], { type: 'application/json' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = name
  document.body.appendChild(a)
  a.click()
  a.remove()
  setTimeout(() => URL.revokeObjectURL(url), 30_000)
}

export function AuditTable({ rows, showProcess }: { rows: AuditRow[]; showProcess?: boolean }) {
  const hasIp = rows.some((r) => r.ip)
  return (
    <div className="tablewrap">
      <table className="table">
        <thead>
          <tr>
            <th>Время</th>
            <th>Пользователь</th>
            <th>Действие</th>
            {showProcess ? <th>Объект / проверка</th> : null}
            <th>Подробности</th>
            {hasIp ? <th>IP и браузер</th> : null}
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.id}>
              <td className="nowrap num">{formatDateTime(r.created_at)}</td>
              <td>{r.user ?? r.user_login ?? '—'}</td>
              <td title={r.action}>{label(AUDIT_ACTION_LABEL, r.action)}</td>
              {showProcess ? (
                <td>
                  {r.object_code ? <span className="mono">{r.object_code}</span> : null}
                  {r.process_id ? (
                    <span className="cell-sub">
                      <Link to={`/processes/${r.process_id}`}>{r.process_id.slice(0, 8)}</Link>
                    </span>
                  ) : null}
                </td>
              ) : null}
              <td style={{ maxWidth: 520 }}>
                {r.details && typeof r.details === 'object' && Object.keys(r.details as object).length ? (
                  <details>
                    <summary>{summarize(r.details as Record<string, unknown>)}</summary>
                    <pre className="json">{JSON.stringify(r.details, null, 1)}</pre>
                  </details>
                ) : (
                  <span className="muted">—</span>
                )}
              </td>
              {hasIp ? (
                <td title={r.user_agent ?? ''}>
                  <span className="mono">{r.ip ?? '—'}</span>
                  <span className="cell-sub ua">{r.user_agent ?? ''}</span>
                </td>
              ) : null}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function summarize(d: Record<string, unknown>): string {
  const parts: string[] = []
  for (const k of ['code', 'decision', 'reason_code', 'stage', 'accepted', 'rejected', 'protocol_version', 'sync_status', 'confirmed', 'version', 'format', 'file', 'n', 'log_id', 'items', 'kind', 'job_id', 'path', 'reason', 'comment']) {
    const v = d[k]
    if (v !== undefined && v !== null && v !== '') parts.push(`${k}: ${typeof v === 'object' ? JSON.stringify(v) : String(v)}`)
    if (parts.length >= 3) break
  }
  return parts.join(', ') || 'показать'
}

export function JournalTab({ process }: { process: ProcessOut }) {
  const toast = useToast()
  const versions = useAsync((signal) => api.protocolVersions(process.id, signal), [process.id, process.protocol_version, process.status])
  const audit = useAsync((signal) => api.auditOfProcess(process.id, signal), [process.id, process.updated_at])
  const showSync = process.status === 'FINALIZED' || (process.sync_status && process.sync_status !== 'NONE')
  const sync = useAsync((signal) => api.rinStatus(process.id, signal), [process.id, process.sync_status], !!showSync)
  const [busy, setBusy] = useState<number | null>(null)

  const downloadVersion = async (v: number) => {
    setBusy(v)
    try {
      const data = await api.protocol(process.id, v)
      saveJson(data, `protocol_${process.object.object_id}_v${v}.json`)
    } catch (e) {
      toast.error(`Снимок не получен: ${errorText(e)}`)
    } finally {
      setBusy(null)
    }
  }

  return (
    <>
      <section className="section">
        <div className="section__head">
          <h2>Версии протокола</h2>
          <p>Прежние версии не меняются: каждая обработка и финализация — новый снимок</p>
        </div>
        {versions.error && !versions.data ? <ErrorBox error={versions.error} onRetry={() => versions.reload()} /> : null}
        {versions.loading && !versions.data ? <Loading /> : null}
        {versions.data && !versions.data.length ? <Empty title="Снимков протокола пока нет">Первый появится после обработки пакета.</Empty> : null}
        {versions.data?.length ? (
          <div className="tablewrap">
            <table className="table">
              <thead>
                <tr>
                  <th>Версия</th>
                  <th>Вид</th>
                  <th>Статус проверки</th>
                  <th>Создан</th>
                  <th className="col-num">Кандидаты</th>
                  <th className="col-num">Подтверждено</th>
                  <th>Модели</th>
                  <th>SHA-256 манифеста</th>
                  <th aria-label="Скачать" />
                </tr>
              </thead>
              <tbody>
                {versions.data.map((v) => (
                  <tr key={v.version}>
                    <td className="mono">v{v.version}</td>
                    <td>{label(VERSION_KIND_LABEL, v.kind)}</td>
                    <td>{label(PROCESS_STATUS_LABEL, v.status)}</td>
                    <td className="nowrap">
                      {formatDateTime(v.created_at)}
                      {v.finalized_at ? <span className="cell-sub">финализирован {formatDateTime(v.finalized_at)}</span> : null}
                    </td>
                    <td className="col-num">{v.summary?.CANDIDATE ?? '—'}</td>
                    <td className="col-num">{v.summary?.CONFIRMED_VIOLATION ?? '—'}</td>
                    <td className="cell-clip mono" title={v.model_version}>
                      {v.model_version}
                    </td>
                    <td>{v.input_manifest_hash ? <Copyable value={v.input_manifest_hash} display={shortHash(v.input_manifest_hash, 12)} /> : '—'}</td>
                    <td className="col-tight">
                      <button type="button" className="btn btn--small" disabled={busy === v.version} onClick={() => downloadVersion(v.version)}>
                        <Icon name="download" size={14} /> JSON
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : null}
      </section>

      {showSync ? (
        <section className="section">
          <div className="section__head">
            <h2>Передача в ИАИС РиН</h2>
            <SyncBadge status={sync.data?.sync_status ?? process.sync_status} />
          </div>
          {sync.error && !sync.data ? (
            <ErrorBox error={sync.error} onRetry={() => sync.reload()} title="Журнал передачи недоступен" />
          ) : null}
          {sync.data?.attempts?.length ? (
            <div className="tablewrap">
              <table className="table table--compact">
                <thead>
                  <tr>
                    <th className="col-num">Попытка</th>
                    <th>Результат</th>
                    <th className="col-num">HTTP</th>
                    <th>Ошибка</th>
                    <th>Время</th>
                    <th>Следующая попытка</th>
                  </tr>
                </thead>
                <tbody>
                  {sync.data.attempts.map((a, i) => (
                    <tr key={`${a.attempt}-${i}`}>
                      <td className="col-num">{a.attempt}</td>
                      <td>{label(SYNC_LABEL, a.status)}</td>
                      <td className="col-num">{a.http_status ?? '—'}</td>
                      <td className="cell-clip" title={a.error ?? ''}>
                        {a.error ?? '—'}
                      </td>
                      <td className="nowrap">{formatDateTime(a.created_at)}</td>
                      <td className="nowrap">{a.next_try_at ? formatDateTime(a.next_try_at) : '—'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : sync.data ? (
            <p className="muted">Попыток передачи ещё не было.</p>
          ) : null}
        </section>
      ) : null}

      <section className="section">
        <div className="section__head">
          <h2>Журнал действий по проверке</h2>
          <p>Загрузки, запуски, решения, финализация — кто, когда, с какими данными</p>
        </div>
        {audit.error && !audit.data ? (
          audit.error instanceof ApiError && audit.error.status === 403 ? (
            <p className="muted">Журнал действий доступен администратору, супервизору и ML-инженеру.</p>
          ) : (
            <ErrorBox error={audit.error} onRetry={() => audit.reload()} />
          )
        ) : null}
        {audit.loading && !audit.data ? <Loading /> : null}
        {audit.data && !audit.data.length ? <p className="muted">Записей нет.</p> : null}
        {audit.data?.length ? <AuditTable rows={audit.data} /> : null}
      </section>
    </>
  )
}
