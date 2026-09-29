// Журнал аудита (admin, supervisor, ml): изменяющие действия, доступ к протоколам и файлам, регламент — кто, когда, что, откуда.
import { useState } from 'react'
import { api } from '../api/endpoints'
import { Empty, ErrorBox, Loading } from '../components/Feedback'
import { useAsync, useDebounced, useDocumentTitle } from '../lib/hooks'
import { AuditTable } from './process/JournalTab'

const ACTIONS: { value: string; label: string }[] = [
  { value: '', label: 'Все действия' },
  { value: 'login,login_failed,login_locked', label: 'Входы' },
  { value: 'process_create,upload,import_local,start,protocol_ready,PIPELINE_FAILED', label: 'Загрузки, запуски, обработка' },
  { value: 'protocol_view,protocol_download,file_download', label: 'Просмотр и выгрузки' },
  { value: 'decision,bulk_confirm,promote', label: 'Решения' },
  { value: 'finalize,unfinalize', label: 'Финализация' },
  { value: 'sync,RIN_SYNC_FAILED', label: 'ИАИС РиН' },
  { value: 'params_patch', label: 'Нормативная база' },
  { value: 'dataset_publish', label: 'GOLD' },
  { value: 'page_correction,retrain_requested,model_approved,model_rollback', label: 'Классификатор страниц' },
  { value: 'STORE_INTEGRITY_OK,STORE_INTEGRITY_FAILED,RETRAINING_REPORT,MAINTENANCE_FAILED', label: 'Регламентные задачи' },
]

export function AuditPage() {
  useDocumentTitle('Журнал')
  const [user, setUser] = useState('')
  const [processId, setProcessId] = useState('')
  const [action, setAction] = useState('')
  const [limit, setLimit] = useState(200)
  const u = useDebounced(user.trim(), 400)
  const pid = useDebounced(processId.trim(), 400)
  const rows = useAsync((signal) => api.audit({ user: u || undefined, process_id: pid || undefined, action: action || undefined, limit }, signal), [u, pid, action, limit])

  return (
    <div className="page">
      <div className="pagehead">
        <div>
          <h1>Журнал действий</h1>
          <p>Загрузки, запуски, просмотры и выгрузки, решения, финализация, правки матрицы и типов страниц, GOLD, регламентные проверки — с пользователем, временем и адресом</p>
        </div>
      </div>
      <div className="toolbar">
        <select className="select" value={action} onChange={(e) => setAction(e.target.value)} aria-label="Действие">
          {ACTIONS.map((a) => (
            <option key={a.value} value={a.value}>
              {a.label}
            </option>
          ))}
        </select>
        <input className="input" style={{ width: 180 }} placeholder="Логин" value={user} onChange={(e) => setUser(e.target.value)} aria-label="Пользователь" />
        <input className="input mono" style={{ width: 320 }} placeholder="Идентификатор проверки" value={processId} onChange={(e) => setProcessId(e.target.value)} aria-label="Проверка" />
        <select className="select" value={limit} onChange={(e) => setLimit(Number(e.target.value))} aria-label="Сколько записей">
          <option value={100}>100 записей</option>
          <option value={200}>200 записей</option>
          <option value={500}>500 записей</option>
          <option value={1000}>1000 записей</option>
        </select>
      </div>
      {rows.error && !rows.data ? <ErrorBox error={rows.error} onRetry={() => rows.reload()} /> : null}
      {rows.loading && !rows.data ? <Loading /> : null}
      {rows.data && !rows.data.length ? <Empty title="Записей нет">Измените фильтр.</Empty> : null}
      {rows.data?.length ? <AuditTable rows={rows.data} showProcess /> : null}
    </div>
  )
}
