import { useEffect, useMemo, useRef, useState, type DragEvent } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { ApiError, errorText, isAbort } from '../api/client'
import { api } from '../api/endpoints'
import type { NewProcessBody, UploadAccepted, UploadRejected, UploadResult, UploadStage } from '../api/types'
import { useAuth } from '../auth/AuthContext'
import { uploadAllowed } from '../auth/permissions'
import { ProcessStatusBadge, StageTag } from '../components/Badges'
import { ErrorBox, Field, Loading } from '../components/Feedback'
import { Icon } from '../components/Icon'
import { Stamp } from '../components/Stamp'
import { useToast } from '../components/Toasts'
import { filesFromDrop, filesFromInput, type PickedFile } from '../lib/dropfiles'
import { countLabel, formatBytes } from '../lib/format'
import { useAsync, useDocumentTitle } from '../lib/hooks'
import { UPLOAD_REJECT_LABEL, label } from '../lib/labels'
import {
  ACCEPT_ATTR,
  MAX_FILE_BYTES,
  MAX_REQUEST_BYTES,
  checkFile,
  fileKey,
  planBatches,
  type LocalRejectCode,
} from '../lib/upload'

type Zone = UploadStage

const ZONES: { key: Zone; title: string; hint: string }[] = [
  { key: 'PD', title: 'Проектная документация', hint: 'Тома проекта, прошедшие экспертизу' },
  { key: 'RD', title: 'Рабочая документация', hint: 'Листы и спецификации в производство работ' },
  { key: 'ID', title: 'Исполнительная документация', hint: 'Акты АОСР, исполнительные схемы, сертификаты, журналы' },
  {
    key: 'AUTO',
    title: 'Определить автоматически',
    hint: 'Стадию определит конвейер по содержимому страниц. Имя папки используется только как подсказка и помечается в паспорте файла.',
  },
]

interface LocalFile {
  key: string
  file: File
  relPath: string
  reject: LocalRejectCode | null
}

type BatchStateKind = 'queued' | 'sending' | 'waiting' | 'done' | 'error' | 'cancelled'

interface BatchState {
  id: number
  zone: Zone
  files: LocalFile[]
  bytes: number
  state: BatchStateKind
  progress: number
  message?: string
  accepted?: number
  rejected?: number
}

interface ResultRow {
  zone: Zone
  accepted: UploadAccepted[]
  rejected: UploadRejected[]
}

const EMPTY_ZONES: Record<Zone, LocalFile[]> = { PD: [], RD: [], ID: [], AUTO: [] }
const LIST_LIMIT = 300

function sleep(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    const t = window.setTimeout(resolve, ms)
    signal.addEventListener(
      'abort',
      () => {
        window.clearTimeout(t)
        reject(new DOMException('Отправка отменена', 'AbortError'))
      },
      { once: true },
    )
  })
}

function ZoneBox({
  zone,
  files,
  disabled,
  onAdd,
  onRemove,
  onClear,
}: {
  zone: (typeof ZONES)[number]
  files: LocalFile[]
  disabled: boolean
  onAdd: (picked: PickedFile[]) => void
  onRemove: (key: string) => void
  onClear: () => void
}) {
  const [over, setOver] = useState(false)
  const fileInput = useRef<HTMLInputElement>(null)
  const dirInput = useRef<HTMLInputElement>(null)
  const ok = files.filter((f) => !f.reject)
  const bytes = ok.reduce((s, f) => s + f.file.size, 0)

  useEffect(() => {
    // выбор папки: нестандартный атрибут, React его не знает
    dirInput.current?.setAttribute('webkitdirectory', '')
    dirInput.current?.setAttribute('directory', '')
  }, [])

  const onDrop = (e: DragEvent<HTMLDivElement>) => {
    e.preventDefault()
    setOver(false)
    if (disabled) return
    filesFromDrop(e.dataTransfer).then(onAdd)
  }

  return (
    <section
      className={`zone${zone.key === 'AUTO' ? ' zone--auto' : ''}${over ? ' zone--over' : ''}`}
      onDragOver={(e) => {
        e.preventDefault()
        if (!disabled) setOver(true)
      }}
      onDragLeave={(e) => {
        if (!e.currentTarget.contains(e.relatedTarget as Node | null)) setOver(false)
      }}
      onDrop={onDrop}
      aria-label={zone.title}
    >
      <div className="zone__head">
        <div>
          <div className="zone__title">
            <StageTag stage={zone.key} />
            <strong>{zone.title}</strong>
          </div>
          <p className="zone__hint">{zone.hint}</p>
        </div>
        <div className="btnrow">
          <button type="button" className="btn btn--small" disabled={disabled} onClick={() => fileInput.current?.click()}>
            Файлы
          </button>
          <button type="button" className="btn btn--small" disabled={disabled} onClick={() => dirInput.current?.click()}>
            Папка
          </button>
        </div>
        <input
          ref={fileInput}
          type="file"
          multiple
          accept={ACCEPT_ATTR}
          hidden
          onChange={(e) => {
            onAdd(filesFromInput(e.target.files))
            e.target.value = ''
          }}
        />
        <input
          ref={dirInput}
          type="file"
          multiple
          hidden
          onChange={(e) => {
            onAdd(filesFromInput(e.target.files))
            e.target.value = ''
          }}
        />
      </div>
      {files.length === 0 ? (
        <div className="zone__drop">
          <Icon name="upload" size={22} />
          Перетащите сюда файлы или папку
        </div>
      ) : (
        <>
          <div className="zone__files">
            {files.slice(0, LIST_LIMIT).map((f) => (
              <div key={f.key} className={`filerow${f.reject ? ' filerow--bad' : ''}`}>
                <span className="filerow__name" title={f.relPath}>
                  {f.relPath}
                </span>
                {f.reject ? (
                  <span className="filerow__reason">{label(UPLOAD_REJECT_LABEL, f.reject)}</span>
                ) : (
                  <span className="muted num">{formatBytes(f.file.size)}</span>
                )}
                <button
                  type="button"
                  className="iconbtn iconbtn--small"
                  disabled={disabled}
                  onClick={() => onRemove(f.key)}
                  aria-label={`Убрать ${f.relPath}`}
                >
                  <Icon name="close" size={13} />
                </button>
              </div>
            ))}
            {files.length > LIST_LIMIT ? (
              <p className="muted" style={{ padding: '6px 0' }}>
                и ещё {files.length - LIST_LIMIT}
              </p>
            ) : null}
          </div>
          <div className="zone__foot">
            <span className="num">
              {countLabel(ok.length, ['файл', 'файла', 'файлов'])}, {formatBytes(bytes)}
              {files.length > ok.length ? `; не будет отправлено: ${files.length - ok.length}` : ''}
            </span>
            <button type="button" className="linkbtn" disabled={disabled} onClick={onClear}>
              Очистить
            </button>
          </div>
        </>
      )}
    </section>
  )
}

const BATCH_STATE_LABEL: Record<BatchStateKind, string> = {
  queued: 'в очереди',
  sending: 'отправляется',
  waiting: 'ждёт окончания обработки',
  done: 'принят сервером',
  error: 'ошибка',
  cancelled: 'отменён',
}

export function UploadPage() {
  const { id: processId } = useParams()
  const appendMode = !!processId
  useDocumentTitle(appendMode ? 'Дозагрузка' : 'Новая проверка')
  const { can } = useAuth()
  const toast = useToast()
  const navigate = useNavigate()

  const proc = useAsync((signal) => api.getProcess(processId as string, signal), [processId], appendMode)

  const [object, setObject] = useState<NewProcessBody>({ name: '', address: '', customer: '', contractor: '', permit_number: '', object_id: '' })
  const [zones, setZones] = useState<Record<Zone, LocalFile[]>>(EMPTY_ZONES)
  const [batches, setBatches] = useState<BatchState[]>([])
  const [results, setResults] = useState<ResultRow[]>([])
  const [phase, setPhase] = useState<'edit' | 'sending' | 'done'>('edit')
  const [targetId, setTargetId] = useState<string | null>(processId ?? null)
  const [formError, setFormError] = useState<string | null>(null)
  const [starting, setStarting] = useState(false)
  const [importPath, setImportPath] = useState('')
  const [importStage, setImportStage] = useState<Zone>('AUTO')
  const [importing, setImporting] = useState(false)
  const abortRef = useRef<AbortController | null>(null)

  // не терять отправку при случайном закрытии вкладки
  useEffect(() => {
    if (phase !== 'sending') return
    const warn = (e: BeforeUnloadEvent) => {
      e.preventDefault()
      e.returnValue = ''
    }
    window.addEventListener('beforeunload', warn)
    return () => window.removeEventListener('beforeunload', warn)
  }, [phase])

  useEffect(() => () => abortRef.current?.abort(), [])

  const all = useMemo(() => (Object.keys(zones) as Zone[]).flatMap((z) => zones[z].map((f) => ({ ...f, zone: z }))), [zones])
  const sendable = all.filter((f) => !f.reject)
  const localRejected = all.filter((f) => f.reject)
  const plan = useMemo(() => {
    const out: { zone: Zone; files: LocalFile[] }[] = []
    for (const z of Object.keys(zones) as Zone[]) {
      const { batches: bs } = planBatches(zones[z].filter((f) => !f.reject).map((f) => ({ ...f, name: f.relPath, size: f.file.size })))
      for (const b of bs) out.push({ zone: z, files: b })
    }
    return out
  }, [zones])
  const totalBytes = sendable.reduce((s, f) => s + f.file.size, 0)

  const addFiles = (zone: Zone, picked: PickedFile[]) => {
    if (!picked.length) return
    const known = new Map(all.map((f) => [f.key, f.zone]))
    const fresh: LocalFile[] = []
    let dup = 0
    for (const p of picked) {
      const key = fileKey({ name: p.relPath, size: p.file.size, lastModified: p.file.lastModified })
      if (known.has(key)) {
        dup++
        continue
      }
      known.set(key, zone)
      fresh.push({ key, file: p.file, relPath: p.relPath, reject: checkFile({ name: p.file.name, size: p.file.size }) })
    }
    if (dup) toast.info(`Уже добавлены раньше: ${dup}`)
    setZones((zs) => ({ ...zs, [zone]: [...zs[zone], ...fresh] }))
  }

  const proc0 = proc.data
  const blocked = appendMode && proc0 ? !uploadAllowed(proc0.status) : false
  const incremental = appendMode && !!proc0 && proc0.protocol_version >= 1

  const waitUntilFree = async (pid: string, batchId: number, signal: AbortSignal) => {
    for (;;) {
      await sleep(5000, signal)
      const p = await api.getProcess(pid, signal)
      if (p.status === 'FINALIZED') throw new ApiError(409, 'PROCESS_FINALIZED', 'Протокол финализирован — дозагрузка невозможна')
      if (p.status !== 'PARSING') return
      setBatches((bs) =>
        bs.map((b) => (b.id === batchId ? { ...b, message: `идёт обработка предыдущей партии${p.stage ? `: этап ${p.stage}` : ''}` } : b)),
      )
    }
  }

  const ensureProcess = async (): Promise<string> => {
    if (targetId) return targetId
    const body: NewProcessBody = { name: object.name.trim() }
    for (const k of ['address', 'customer', 'contractor', 'permit_number', 'object_id'] as const) {
      const v = (object[k] ?? '').trim()
      if (v) body[k] = v
    }
    const p = await api.createProcess(body)
    setTargetId(p.id)
    return p.id
  }

  const validateObject = (): boolean => {
    if (!appendMode && !object.name.trim() && !(object.object_id ?? '').trim()) {
      setFormError('Укажите название объекта (или код существующего объекта)')
      return false
    }
    setFormError(null)
    return true
  }

  const send = async () => {
    if (!validateObject()) return
    if (!plan.length) {
      setFormError('Добавьте хотя бы один файл допустимого формата')
      return
    }
    const ctrl = new AbortController()
    abortRef.current = ctrl
    const initial: BatchState[] = plan.map((b, i) => ({
      id: i + 1,
      zone: b.zone,
      files: b.files,
      bytes: b.files.reduce((s, f) => s + f.file.size, 0),
      state: 'queued',
      progress: 0,
    }))
    setBatches(initial)
    setPhase('sending')
    const set = (id: number, patch: Partial<BatchState>) => setBatches((bs) => bs.map((b) => (b.id === id ? { ...b, ...patch } : b)))
    let pid: string
    try {
      pid = await ensureProcess()
    } catch (e) {
      setPhase('edit')
      setBatches([])
      setFormError(`Проверку создать не удалось: ${errorText(e)}`)
      return
    }
    const collected: ResultRow[] = []
    for (const b of initial) {
      if (ctrl.signal.aborted) {
        set(b.id, { state: 'cancelled' })
        continue
      }
      set(b.id, { state: 'sending' })
      for (;;) {
        try {
          const form = new FormData()
          form.append('stage', b.zone)
          form.append('process_id', pid)
          for (const f of b.files) form.append('files', f.file, f.relPath)
          const res: UploadResult = await api.upload(form, (loaded, total) => set(b.id, { progress: total ? loaded / total : 0 }), ctrl.signal)
          collected.push({ zone: b.zone, accepted: res.accepted ?? [], rejected: res.rejected ?? [] })
          set(b.id, { state: 'done', progress: 1, accepted: res.accepted?.length ?? 0, rejected: res.rejected?.length ?? 0, message: undefined })
          break
        } catch (e) {
          if (isAbort(e)) {
            set(b.id, { state: 'cancelled' })
            break
          }
          if (e instanceof ApiError && e.code === 'PROCESS_BUSY') {
            // дозагрузка в готовый протокол: предыдущая порция уже запустила обработку — ждём её окончания
            set(b.id, { state: 'waiting', progress: 0, message: 'идёт обработка предыдущей партии' })
            try {
              await waitUntilFree(pid, b.id, ctrl.signal)
              set(b.id, { state: 'sending', message: undefined })
              continue
            } catch (waitErr) {
              set(b.id, { state: isAbort(waitErr) ? 'cancelled' : 'error', message: isAbort(waitErr) ? undefined : errorText(waitErr) })
              break
            }
          }
          set(b.id, { state: 'error', message: errorText(e) })
          collected.push({
            zone: b.zone,
            accepted: [],
            rejected: b.files.map((f) => ({ name: f.relPath, reason_code: e instanceof ApiError ? e.code : 'NOT_SENT', reason: errorText(e) })),
          })
          break
        }
      }
    }
    setResults(collected)
    setPhase('done')
    abortRef.current = null
    const acc = collected.reduce((s, r) => s + r.accepted.length, 0)
    if (acc) toast.ok(`Принято файлов: ${acc}`)
  }

  const runImport = async () => {
    if (!importPath.trim()) return
    if (!validateObject()) return
    setImporting(true)
    try {
      const pid = await ensureProcess()
      const res = await api.importLocal(pid, importPath.trim(), importStage)
      setResults((rs) => [...rs, { zone: importStage, accepted: res.accepted ?? [], rejected: res.rejected ?? [] }])
      setPhase('done')
      toast.ok(`Импорт: принято ${res.accepted?.length ?? 0}, отклонено ${res.rejected?.length ?? 0}`)
    } catch (e) {
      toast.error(`Импорт не выполнен: ${errorText(e)}`)
    } finally {
      setImporting(false)
    }
  }

  const start = async () => {
    if (!targetId) return
    setStarting(true)
    try {
      await api.startProcess(targetId)
      toast.ok('Проверка поставлена в очередь')
      navigate(`/processes/${targetId}`)
    } catch (e) {
      if (e instanceof ApiError && e.code === 'PROCESS_BUSY') {
        toast.info('Обработка уже идёт')
        navigate(`/processes/${targetId}`)
      } else toast.error(errorText(e))
    } finally {
      setStarting(false)
    }
  }

  const accepted = results.flatMap((r) => r.accepted.map((a) => ({ ...a, zone: r.zone })))
  const rejected = [
    ...results.flatMap((r) => r.rejected.map((x) => ({ ...x, zone: r.zone }))),
    ...localRejected.map((f) => ({ name: f.relPath, reason_code: f.reject as string, reason: 'не отправлен: ' + label(UPLOAD_REJECT_LABEL, f.reject), zone: f.zone })),
  ]
  const editing = phase === 'edit'

  if (appendMode && proc.loading && !proc.data) return <div className="page"><Loading /></div>
  if (appendMode && proc.error && !proc.data) return <div className="page"><ErrorBox error={proc.error} onRetry={() => proc.reload()} /></div>

  return (
    <div className="page">
      <Link to={appendMode ? `/processes/${processId}` : '/'} className="backlink">
        <Icon name="back" size={14} /> {appendMode ? 'К проверке' : 'К списку проверок'}
      </Link>
      <div className="pagehead">
        <div>
          <h1>{appendMode ? 'Дозагрузка файлов' : 'Новая проверка'}</h1>
          <p>
            Файл — до {formatBytes(MAX_FILE_BYTES)}, один запрос — до {formatBytes(MAX_REQUEST_BYTES)}: большой пакет уйдёт несколькими
            запросами по очереди. Форматы: PDF, DOC(X), XLS(X), RTF, ODT, XML, JPG, PNG, TIFF, архивы ZIP, 7Z, RAR, подписи SIG,
            манифест JSONL.
          </p>
        </div>
      </div>

      <div className="upload">
        <aside className="upload__object">
          {appendMode && proc0 ? (
            <>
              <h2>Объект</h2>
              <Stamp
                className="stamp--wrap"
                columns="1fr 1fr"
                cells={[
                  { label: 'Объект', value: proc0.object.name, span: 2 },
                  { label: 'Код объекта', value: proc0.object.object_id, mono: true, span: 2 },
                  { label: 'Статус', value: <ProcessStatusBadge status={proc0.status} /> },
                  { label: 'Протокол', value: proc0.protocol_version ? `v${proc0.protocol_version}` : 'ещё нет' },
                ]}
              />
              {blocked ? (
                <p className="note note--bad">
                  {proc0.status === 'FINALIZED'
                    ? 'Протокол финализирован: дозагрузка невозможна, новая проверка — отдельным процессом.'
                    : 'Идёт обработка пакета — дозагрузка станет доступна после её окончания.'}
                </p>
              ) : incremental ? (
                <p className="note note--warn">
                  У проверки уже есть протокол. Каждая отправленная порция сразу уходит в обработку новой партией; решения по
                  прежним находкам сохраняются. Следующая порция ждёт окончания обработки предыдущей.
                </p>
              ) : null}
            </>
          ) : (
            <>
              <h2>Объект</h2>
              <Field label="Название объекта" required>
                <input className="input" value={object.name} disabled={!editing || !!targetId} onChange={(e) => setObject({ ...object, name: e.target.value })} placeholder="Жилой дом, ул. Новослободская" />
              </Field>
              <Field label="Адрес">
                <input className="input" value={object.address} disabled={!editing || !!targetId} onChange={(e) => setObject({ ...object, address: e.target.value })} />
              </Field>
              <Field label="Заказчик">
                <input className="input" value={object.customer} disabled={!editing || !!targetId} onChange={(e) => setObject({ ...object, customer: e.target.value })} />
              </Field>
              <Field label="Подрядчик">
                <input className="input" value={object.contractor} disabled={!editing || !!targetId} onChange={(e) => setObject({ ...object, contractor: e.target.value })} />
              </Field>
              <Field label="Разрешение на строительство">
                <input className="input" value={object.permit_number} disabled={!editing || !!targetId} onChange={(e) => setObject({ ...object, permit_number: e.target.value })} placeholder="77-000000-000000-2025" />
              </Field>
              <Field label="Код объекта" hint="Если объект уже проверялся, укажите его код — новая проверка привяжется к нему.">
                <input className="input mono" value={object.object_id} disabled={!editing || !!targetId} onChange={(e) => setObject({ ...object, object_id: e.target.value })} placeholder="OBJ-…" />
              </Field>
            </>
          )}
          {formError ? <p className="login__error" role="alert">{formError}</p> : null}
        </aside>

        <div>
          <div className="zones">
            {ZONES.map((z) => (
              <ZoneBox
                key={z.key}
                zone={z}
                files={zones[z.key]}
                disabled={!editing || blocked}
                onAdd={(picked) => addFiles(z.key, picked)}
                onRemove={(key) => setZones((zs) => ({ ...zs, [z.key]: zs[z.key].filter((f) => f.key !== key) }))}
                onClear={() => setZones((zs) => ({ ...zs, [z.key]: [] }))}
              />
            ))}
          </div>

          {editing ? (
            <div className="upload__summary">
              <span className="grow num">
                {sendable.length ? (
                  <>
                    К отправке: <strong>{countLabel(sendable.length, ['файл', 'файла', 'файлов'])}</strong>, {formatBytes(totalBytes)} —{' '}
                    {countLabel(plan.length, ['запрос', 'запроса', 'запросов'])}
                  </>
                ) : (
                  'Добавьте файлы в зоны ПД, РД, ИД или в «Определить автоматически»'
                )}
                {localRejected.length ? <span className="muted">. Не будет отправлено: {localRejected.length}</span> : null}
              </span>
              <button type="button" className="btn btn--primary" disabled={!sendable.length || blocked} onClick={send}>
                <Icon name="upload" />
                {appendMode ? 'Загрузить' : 'Создать проверку и загрузить'}
              </button>
            </div>
          ) : null}

          {batches.length ? (
            <div className="section">
              <div className="section__head">
                <h2>Отправка</h2>
                {phase === 'sending' ? (
                  <button type="button" className="btn btn--small btn--danger" onClick={() => abortRef.current?.abort()}>
                    Остановить
                  </button>
                ) : null}
              </div>
              <div className="batchlist">
                {batches.map((b) => (
                  <div key={b.id} className={`batch batch--${b.state === 'done' ? 'done' : b.state === 'error' ? 'error' : b.state === 'waiting' ? 'wait' : 'idle'}`}>
                    <span>
                      <StageTag stage={b.zone} /> Запрос {b.id} из {batches.length}
                    </span>
                    <span className="num">
                      {countLabel(b.files.length, ['файл', 'файла', 'файлов'])}, {formatBytes(b.bytes)}
                      {b.state === 'sending' || b.state === 'waiting' ? (
                        <div className="progress" style={{ marginTop: 4 }}>
                          <div className="progress__fill" style={{ width: `${Math.round(b.progress * 100)}%` }} />
                        </div>
                      ) : null}
                    </span>
                    <span className={b.state === 'error' ? 'dbar__error' : 'muted'}>
                      {BATCH_STATE_LABEL[b.state]}
                      {b.state === 'done' ? `: принято ${b.accepted}, отклонено ${b.rejected}` : ''}
                      {b.message ? ` — ${b.message}` : ''}
                    </span>
                  </div>
                ))}
              </div>
            </div>
          ) : null}

          {phase === 'done' ? (
            <div className="section">
              <div className="section__head">
                <h2>Результат</h2>
                <div className="btnrow">
                  {targetId && !incremental ? (
                    <button type="button" className="btn btn--primary" onClick={start} disabled={starting || !accepted.length}>
                      {starting ? 'Запускаем…' : 'Запустить проверку'}
                    </button>
                  ) : null}
                  {targetId ? (
                    <Link to={`/processes/${targetId}`} className="btn">
                      Открыть проверку
                    </Link>
                  ) : null}
                  <button
                    type="button"
                    className="btn btn--ghost"
                    onClick={() => {
                      setZones(EMPTY_ZONES)
                      setBatches([])
                      setResults([])
                      setPhase('edit')
                    }}
                  >
                    Добавить ещё файлы
                  </button>
                </div>
              </div>
              {incremental && accepted.length ? (
                <p className="note note--ok" style={{ marginBottom: 12 }}>
                  Новые файлы приняты и поставлены в обработку автоматически — прогресс виден на странице проверки.
                </p>
              ) : null}
              <div className="twocol">
                <div>
                  <h3 style={{ marginBottom: 8 }}>Принято: {accepted.length}</h3>
                  {accepted.length ? (
                    <div className="tablewrap">
                      <table className="table table--compact">
                        <thead>
                          <tr>
                            <th>Файл</th>
                            <th>Стадия</th>
                            <th className="col-num">Размер</th>
                          </tr>
                        </thead>
                        <tbody>
                          {accepted.map((a) => (
                            <tr key={`${a.file_pk}-${a.name}`}>
                              <td className="cell-clip" title={a.rel_path || a.name}>
                                {a.rel_path || a.name}
                              </td>
                              <td>
                                <StageTag stage={a.stage ?? a.zone} />
                              </td>
                              <td className="col-num">{formatBytes(a.size)}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  ) : (
                    <p className="muted">Ни один файл не принят.</p>
                  )}
                </div>
                <div>
                  <h3 style={{ marginBottom: 8 }}>Отклонено: {rejected.length}</h3>
                  {rejected.length ? (
                    <div className="tablewrap">
                      <table className="table table--compact">
                        <thead>
                          <tr>
                            <th>Файл</th>
                            <th>Причина</th>
                          </tr>
                        </thead>
                        <tbody>
                          {rejected.map((r, i) => (
                            <tr key={`${r.name}-${i}`}>
                              <td className="cell-clip" title={r.name}>
                                {r.name}
                              </td>
                              <td title={r.reason_code}>{r.reason || label(UPLOAD_REJECT_LABEL, r.reason_code)}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  ) : (
                    <p className="muted">Отклонённых файлов нет.</p>
                  )}
                </div>
              </div>
            </div>
          ) : null}

          {can('importLocal') ? (
            <div className="section">
              <div className="section__head">
                <h2>Пакет с диска сервера</h2>
                <p>Для больших объёмов: путь внутри разрешённых каталогов импорта (IMPORT_ROOTS)</p>
              </div>
              <div className="toolbar">
                <input className="input mono" style={{ width: 420 }} placeholder="/data/incoming/объект" value={importPath} onChange={(e) => setImportPath(e.target.value)} aria-label="Путь к пакету на сервере" disabled={blocked} />
                <select className="select" value={importStage} onChange={(e) => setImportStage(e.target.value as Zone)} aria-label="Стадия пакета" disabled={blocked}>
                  <option value="AUTO">Определить автоматически</option>
                  <option value="PD">ПД</option>
                  <option value="RD">РД</option>
                  <option value="ID">ИД</option>
                </select>
                <button type="button" className="btn" onClick={runImport} disabled={importing || !importPath.trim() || blocked || phase === 'sending'}>
                  {importing ? 'Импортируем…' : 'Импортировать'}
                </button>
              </div>
            </div>
          ) : null}
        </div>
      </div>
    </div>
  )
}
