// Классификатор страниц (ml, admin): действующая версия и её базовые модели с лицензиями, правки разметчиков,
// итерация дообучения с приёмкой на замороженном наборе, журнал итераций, ручное утверждение или отклонение версии
// (автопубликации нет — Q&A №24) и откат.
import { useEffect, useRef, useState } from 'react'
import { errorText } from '../api/client'
import { api } from '../api/endpoints'
import type { AccPart, PageClassifierStatus, RetrainLogRow, RetrainMetrics } from '../api/types'
import { Copyable, Empty, ErrorBox, Loading } from '../components/Feedback'
import { Modal } from '../components/Modal'
import { useToast } from '../components/Toasts'
import { useAuth } from '../auth/AuthContext'
import { formatDateTime, formatInt, formatSeconds, shortHash } from '../lib/format'
import { useAsync, useInterval } from '../lib/hooks'
import { MODEL_APPROVAL_LABEL, RETRAIN_STATUS_LABEL, label } from '../lib/labels'
import { classLabel } from './process/pageLabel'

const ACTIVE = ['QUEUED', 'RUNNING']
const STATUS_TONE: Record<string, string> = {
  ACCEPTED: 'NEGATIVE_VERIFIED',
  APPROVED: 'NEGATIVE_VERIFIED',
  REJECTED: 'CONFIRMED_VIOLATION',
  FAILED: 'CONFIRMED_VIOLATION',
  DRAFT: 'CANDIDATE',
  QUEUED: 'NOT_APPLICABLE',
  RUNNING: 'CANDIDATE',
  ROLLBACK: 'NOT_COMPARABLE',
}

function accText(p: AccPart | undefined): string {
  return p ? `${(p.acc * 100).toFixed(1).replace('.', ',')} %` : '—'
}

/** Доля совпадений до → после и разница в п. п.: рост — зелёный, падение — красный. */
export function AccDelta({ before, after }: { before?: AccPart; after?: AccPart }) {
  if (!before && !after) return <span className="muted">—</span>
  const d = before && after ? (after.acc - before.acc) * 100 : null
  return (
    <span className="accdelta" title={after ? `${after.ok} из ${after.n}` : undefined}>
      {accText(before)} → <strong>{accText(after)}</strong>
      {d !== null && Math.abs(d) >= 0.05 ? (
        <span className={`accdelta__d accdelta__d--${d > 0 ? 'up' : 'down'}`}>
          {d > 0 ? '+' : '−'}
          {Math.abs(d).toFixed(1).replace('.', ',')} п. п.
        </span>
      ) : null}
    </span>
  )
}

function part(m: RetrainMetrics | null, set: string, key: 'cls' | 'first' | 'section'): AccPart | undefined {
  return m?.[set]?.[key]
}

export function StatusTag({ status }: { status: string }) {
  return (
    <span className={`badge badge--${STATUS_TONE[status] ?? 'NOT_APPLICABLE'}`} title={status}>
      {label(RETRAIN_STATUS_LABEL, status)}
    </span>
  )
}

function Versions({ st, canRetrain, onRollback }: { st: PageClassifierStatus; canRetrain: boolean; onRollback: (v: string) => void }) {
  if (!st.versions.length) return <Empty title="Версий пока нет">Первая появится, когда подключат модель (PAGE_CLASSIFIER_DIR).</Empty>
  return (
    <div className="tablewrap">
      <table className="table">
        <thead>
          <tr>
            <th>Версия</th>
            <th>Статус</th>
            <th>Утвердил</th>
            <th>Опубликована</th>
            <th>Была до неё</th>
            <th>Класс на замороженном наборе</th>
            <th aria-label="Действия" />
          </tr>
        </thead>
        <tbody>
          {st.versions.map((v) => (
            <tr key={v.model_version}>
              <td className="mono" style={{ overflowWrap: 'anywhere' }}>
                {v.model_version}
                {v.is_current ? <span className="cell-sub" style={{ fontFamily: 'var(--font-ui)' }}>действующая</span> : null}
              </td>
              <td>{label(MODEL_APPROVAL_LABEL, v.approval_status)}</td>
              <td>{v.approved_by || '—'}</td>
              <td className="nowrap">{formatDateTime(v.deployed_at)}</td>
              <td className="mono">{v.rollback_to ?? '—'}</td>
              <td>{accText((v.metrics as RetrainMetrics | null)?.frozen?.cls)}</td>
              <td className="col-tight">
                {canRetrain && !v.is_current && v.approval_status === 'APPROVED' ? (
                  <button type="button" className="btn btn--small" onClick={() => onRollback(v.model_version)}>
                    Сделать действующей
                  </button>
                ) : null}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function LogTable({
  rows,
  canRetrain,
  pending,
  onDecide,
}: {
  rows: RetrainLogRow[]
  canRetrain: boolean
  /** версии, ждущие решения ответственного (в реестре — черновик) */
  pending: Set<string>
  onDecide: (kind: 'approve' | 'reject', v: string) => void
}) {
  if (!rows.length) return <Empty title="Итераций ещё не было">Нажмите «Дообучить», когда разметчики исправят страницы.</Empty>
  return (
    <div className="tablewrap">
      <table className="table">
        <thead>
          <tr>
            <th className="col-num">№</th>
            <th>Итог</th>
            <th>Версия: была → стала</th>
            <th className="col-num">Правок</th>
            <th>Класс на правках</th>
            <th>Класс на замороженном наборе</th>
            <th>Первая страница (замороженный)</th>
            <th>Правило</th>
            <th>Кто, когда</th>
            <th aria-label="Действия" />
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.id}>
              <td className="col-num">{r.id}</td>
              <td>
                <StatusTag status={r.status} />
                {r.error ? (
                  <span className="cell-sub cell-clip" title={r.error} style={{ color: 'var(--red-fg)' }}>
                    {r.error}
                  </span>
                ) : null}
              </td>
              <td className="mono" style={{ overflowWrap: 'anywhere', maxWidth: 280 }}>
                {r.base_version ?? '—'} → {r.new_version ?? '—'}
              </td>
              <td className="col-num">
                {r.n_corrections ?? '—'}
                {r.corrections_sha256 ? <span className="cell-sub mono" title={r.corrections_sha256}>{shortHash(r.corrections_sha256, 8)}</span> : null}
              </td>
              <td>
                <AccDelta before={part(r.metrics_before, 'corrections', 'cls')} after={part(r.metrics_after, 'corrections', 'cls')} />
              </td>
              <td>
                <AccDelta before={part(r.metrics_before, 'frozen', 'cls')} after={part(r.metrics_after, 'frozen', 'cls')} />
              </td>
              <td>
                <AccDelta before={part(r.metrics_before, 'frozen', 'first')} after={part(r.metrics_after, 'frozen', 'first')} />
              </td>
              <td className="cell-clip" title={r.rule ?? ''} style={{ maxWidth: 260 }}>
                {r.rule ?? '—'}
              </td>
              <td className="nowrap">
                {r.decided_by || r.requested_by || '—'}
                <span className="cell-sub">
                  {formatDateTime(r.finished_at ?? r.created_at)}
                  {r.seconds ? ` · ${formatSeconds(r.seconds)}` : ''}
                </span>
              </td>
              <td className="col-tight">
                {canRetrain && (r.status === 'DRAFT' || r.status === 'ACCEPTED') && r.new_version && pending.has(r.new_version) ? (
                  <span className="btnrow" style={{ flexWrap: 'nowrap' }}>
                    <button type="button" className="btn btn--small btn--primary" onClick={() => onDecide('approve', r.new_version!)}>
                      Утвердить
                    </button>
                    <button type="button" className="btn btn--small" onClick={() => onDecide('reject', r.new_version!)}>
                      Отклонить
                    </button>
                  </span>
                ) : null}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

export function PageClassifierTab() {
  const toast = useToast()
  const { can } = useAuth()
  const canRetrain = can('retrain')
  const st = useAsync((signal) => api.pageClassifier(signal), [])
  const s = st.data
  const running = !!s?.log.some((r) => ACTIVE.includes(r.status))
  useInterval(() => st.reload(true), 3000, running)
  const [busy, setBusy] = useState(false)
  const [confirm, setConfirm] = useState<{ kind: 'approve' | 'reject' | 'rollback'; version: string } | null>(null)

  // итерация закончилась — сказать, чем
  const watched = useRef<number | null>(null)
  useEffect(() => {
    const top = s?.log[0]
    if (!top) return
    if (ACTIVE.includes(top.status)) watched.current = top.id
    else if (watched.current === top.id) {
      watched.current = null
      const msg = `Итерация №${top.id}: ${label(RETRAIN_STATUS_LABEL, top.status).toLowerCase()}`
      if (top.status === 'FAILED' || top.status === 'REJECTED') toast.error(msg)
      else toast.ok(msg)
    }
  }, [s, toast])

  if (st.error && !s) return <ErrorBox error={st.error} onRetry={() => st.reload()} />
  if (st.loading && !s) return <Loading />
  if (!s) return null

  const man = s.manifest ?? {}
  const bases = (Array.isArray(man.base_models) ? man.base_models : []) as { repo?: string; license?: string; revision?: string }[]
  const adapted = (man.adaptation ?? {}) as { pages?: number }

  const retrain = async () => {
    setBusy(true)
    try {
      const r = await api.retrainPageClassifier()
      toast.info(`Итерация №${r.id} в очереди — выполнит обработчик (worker)`)
      st.reload(true)
    } catch (e) {
      toast.error(`Не запущено: ${errorText(e)}`)
    } finally {
      setBusy(false)
    }
  }

  const decide = async () => {
    if (!confirm) return
    setBusy(true)
    try {
      if (confirm.kind === 'reject') {
        await api.rejectPageClassifier(confirm.version)
        toast.ok(`Версия ${confirm.version} отклонена — опубликовать её нельзя`)
      } else {
        const r = confirm.kind === 'approve' ? await api.approvePageClassifier(confirm.version) : await api.rollbackPageClassifier(confirm.version)
        toast.ok(`Действующая версия: ${r.current}`)
      }
      setConfirm(null)
      st.reload(true)
    } catch (e) {
      toast.error(errorText(e))
    } finally {
      setBusy(false)
    }
  }

  const blocked = !s.enabled
    ? 'Модель не подключена'
    : running
      ? 'Итерация уже идёт'
      : s.dataset.pages === 0
        ? 'Набор не загружен: загрузите и обработайте пакет'
        : s.corrections.total === 0
          ? 'Правок нет: исправьте типы страниц во вкладке «Страницы»'
          : s.corrections.unused === 0
            ? 'Новых правок нет'
            : null

  return (
    <>
      <div className="twocol">
        <div>
          <section className="section">
            <div className="section__head">
              <h2>Действующая версия</h2>
              <p>Модель ставит тип, первую страницу и раздел; неуверенные страницы перепроверяет Qwen</p>
            </div>
            {!s.enabled ? (
              <p className="note note--warn">
                Классификатор не подключён (PAGE_CLASSIFIER_DIR): все страницы размечает Qwen. Правки разметчиков копятся
                и пойдут в первую итерацию после подключения.
              </p>
            ) : null}
            <dl className="kv">
              <dt>Версия</dt>
              <dd className="mono">{s.current ?? '—'}</dd>
              {man.name ? (
                <>
                  <dt>Модель</dt>
                  <dd>{String(man.name)}</dd>
                </>
              ) : null}
              {bases.length ? (
                <>
                  <dt>Базовые модели</dt>
                  <dd>
                    {bases.map((b, i) => (
                      <div key={i}>
                        <span className="mono">{b.repo}</span>
                        {b.license ? <span className="muted"> · лицензия {b.license}</span> : null}
                      </div>
                    ))}
                  </dd>
                </>
              ) : null}
              {adapted.pages ? (
                <>
                  <dt>Дообучена на</dt>
                  <dd className="num">{formatInt(adapted.pages)} стр. загруженных проверок</dd>
                </>
              ) : null}
              <dt>Порог перепроверки</dt>
              <dd>{s.threshold !== null ? `уверенность ниже ${Math.round(s.threshold * 100)} % — страницу перепроверяет Qwen` : '—'}</dd>
              <dt>Замороженный набор</dt>
              <dd>
                {s.acceptance_set
                  ? `${formatInt(s.acceptance_pages)} стр.: утвердить можно только версию, у которой класс на нём не хуже прежнего`
                  : 'не задан — приёмки по набору нет, решение только за ответственным'}
              </dd>
              <dt>Набор для дообучения</dt>
              <dd className="num">
                {s.dataset.pages ? `${formatInt(s.dataset.pages)} размеченных стр. загруженных проверок` : 'не загружен'}
              </dd>
              <dt>Правки разметчиков</dt>
              <dd className="num">
                {formatInt(s.corrections.total)}, из них новых (ещё не в обучении): <strong>{formatInt(s.corrections.unused)}</strong>
              </dd>
            </dl>
            {canRetrain ? (
              <div className="btnrow" style={{ marginTop: 14 }}>
                <button type="button" className="btn btn--primary" disabled={busy || !!blocked} onClick={retrain} title={blocked ?? undefined}>
                  {running ? 'Идёт дообучение…' : 'Дообучить на правках'}
                </button>
                {blocked && !running ? <span className="muted">{blocked}</span> : null}
              </div>
            ) : null}
            <p className="field__hint" style={{ marginTop: 8 }}>
              Итерация дообучает верхние слои (головы) исходной модели от их весов на страницах загруженных проверок:
              исправленные — с меткой разметчика и повышенным весом, остальные — с текущей меткой. Обучающих данных в
              модели нет: без загруженного набора и правок дообучение недоступно. До и после считается точность на правках
              и на замороженном наборе, если он задан. Новая версия сама не публикуется: её утверждает или отклоняет
              ответственный; решение и метрики пишутся в журнал.
            </p>
          </section>
        </div>
        <div>
          <section className="section">
            <div className="section__head">
              <h2>Что исправляют чаще</h2>
              <p>Тип до правки → тип после; по последней правке страницы</p>
            </div>
            {s.corrections.confusions.length ? (
              <div className="tablewrap">
                <table className="table table--compact">
                  <thead>
                    <tr>
                      <th>Было</th>
                      <th>Стало</th>
                      <th className="col-num">Страниц</th>
                    </tr>
                  </thead>
                  <tbody>
                    {s.corrections.confusions.map((c) => (
                      <tr key={`${c.from}>${c.to}`}>
                        <td>{classLabel(c.from)}</td>
                        <td>{classLabel(c.to)}</td>
                        <td className="col-num">{c.n}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <p className="muted">Правок типа пока нет. Исправлять страницы — во вкладке «Страницы» проверки.</p>
            )}
          </section>
        </div>
      </div>

      <section className="section">
        <div className="section__head">
          <h2>Журнал итераций</h2>
          <p>ML_Retraining_Log: версии, число правок и их SHA-256, метрики до и после, правило приёмки</p>
        </div>
        <LogTable
          rows={s.log}
          canRetrain={canRetrain}
          pending={new Set(s.versions.filter((v) => v.approval_status === 'DRAFT').map((v) => v.model_version))}
          onDecide={(kind, v) => setConfirm({ kind, version: v })}
        />
      </section>

      <section className="section">
        <div className="section__head">
          <h2>Версии</h2>
          <p>Откат — к любой утверждённой версии; действующая помечена</p>
        </div>
        <Versions st={s} canRetrain={canRetrain} onRollback={(v) => setConfirm({ kind: 'rollback', version: v })} />
      </section>

      <Modal
        open={!!confirm}
        title={confirm?.kind === 'approve' ? 'Утвердить версию' : confirm?.kind === 'reject' ? 'Отклонить версию' : 'Откатить классификатор'}
        onClose={() => setConfirm(null)}
        footer={
          <>
            <button type="button" className="btn" onClick={() => setConfirm(null)}>
              Отмена
            </button>
            <button type="button" className={`btn ${confirm?.kind === 'reject' ? 'btn--danger' : 'btn--primary'}`} onClick={decide} disabled={busy}>
              {confirm?.kind === 'approve' ? 'Утвердить и опубликовать' : confirm?.kind === 'reject' ? 'Отклонить' : 'Сделать действующей'}
            </button>
          </>
        }
      >
        {confirm?.kind === 'reject' ? (
          <p>
            Версия <Copyable value={confirm?.version ?? ''} /> будет отклонена: опубликовать её станет нельзя, действующая
            модель не меняется. Правки разметчиков остаются и войдут в следующую итерацию. Решение попадёт в журнал.
          </p>
        ) : (
          <p>
            Версия <Copyable value={confirm?.version ?? ''} /> станет действующей: с новых запусков страницы размечает она.
            Прежняя остаётся в списке версий — к ней можно вернуться. Действие попадёт в журнал.
          </p>
        )}
      </Modal>
    </>
  )
}
