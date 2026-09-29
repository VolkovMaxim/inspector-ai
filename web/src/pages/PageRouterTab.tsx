// Отбор страниц F40 (ml, admin): действующая версия модели отбора, метки из решений экспертов по кандидатам,
// итерация дообучения с метриками до и после, журнал итераций, ручное утверждение или отклонение версии
// (автопубликации нет — Q&A №24) и откат.
import { useEffect, useRef, useState } from 'react'
import { errorText } from '../api/client'
import { api } from '../api/endpoints'
import type { AccPart, PageRouterStatus, RetrainLogRow, RouterMetrics } from '../api/types'
import { Copyable, Empty, ErrorBox, Loading } from '../components/Feedback'
import { Modal } from '../components/Modal'
import { useToast } from '../components/Toasts'
import { useAuth } from '../auth/AuthContext'
import { formatDateTime, formatInt, formatSeconds, shortHash } from '../lib/format'
import { useAsync, useInterval } from '../lib/hooks'
import { MODEL_APPROVAL_LABEL, RETRAIN_STATUS_LABEL, label } from '../lib/labels'
import { AccDelta, StatusTag } from './PageClassifierTab'

const ACTIVE = ['QUEUED', 'RUNNING']

function part(m: RouterMetrics | null | undefined, key: 'found' | 'dropped'): AccPart | undefined {
  return m?.labels?.[key]
}

function accText(p: AccPart | undefined): string {
  return p ? `${(p.acc * 100).toFixed(1).replace('.', ',')} % (${p.ok} из ${p.n})` : '—'
}

function LogTable({
  rows,
  topK,
  canRetrain,
  pending,
  onDecide,
}: {
  rows: RetrainLogRow[]
  topK: number
  canRetrain: boolean
  pending: Set<string>
  onDecide: (kind: 'approve' | 'reject', v: string) => void
}) {
  if (!rows.length) return <Empty title="Итераций ещё не было">Нажмите «Дообучить», когда эксперты примут решения по кандидатам.</Empty>
  return (
    <div className="tablewrap">
      <table className="table">
        <thead>
          <tr>
            <th className="col-num">№</th>
            <th>Итог</th>
            <th>Версия: исходная → новая</th>
            <th className="col-num">Страниц с метками</th>
            <th>Страницы со значением в первых {topK}</th>
            <th>Неверные страницы вне первых {topK}</th>
            <th>Правило</th>
            <th>Кто, когда</th>
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
                {canRetrain && (r.status === 'DRAFT' || r.status === 'ACCEPTED') && r.new_version && pending.has(r.new_version) ? (
                  <span className="btnrow" style={{ flexWrap: 'nowrap', marginTop: 6 }}>
                    <button type="button" className="btn btn--small btn--primary" onClick={() => onDecide('approve', r.new_version!)}>
                      Утвердить
                    </button>
                    <button type="button" className="btn btn--small" onClick={() => onDecide('reject', r.new_version!)}>
                      Отклонить
                    </button>
                  </span>
                ) : null}
              </td>
              <td className="mono" style={{ overflowWrap: 'anywhere', maxWidth: 220 }}>
                {r.base_version ?? '—'} → {r.new_version ?? '—'}
              </td>
              <td className="col-num">
                {r.corrections_sha256 ? r.n_corrections : '—'}
                {r.corrections_sha256 ? <span className="cell-sub mono" title={r.corrections_sha256}>{shortHash(r.corrections_sha256, 8)}</span> : null}
              </td>
              <td>
                <AccDelta before={part(r.metrics_before as RouterMetrics | null, 'found')} after={part(r.metrics_after as RouterMetrics | null, 'found')} />
              </td>
              <td>
                <AccDelta before={part(r.metrics_before as RouterMetrics | null, 'dropped')} after={part(r.metrics_after as RouterMetrics | null, 'dropped')} />
              </td>
              <td className="cell-clip" title={r.rule ?? ''} style={{ maxWidth: 220 }}>
                {r.rule ?? '—'}
              </td>
              <td className="nowrap">
                {r.decided_by || r.requested_by || '—'}
                <span className="cell-sub">
                  {formatDateTime(r.finished_at ?? r.created_at)}
                  {r.seconds ? ` · ${formatSeconds(r.seconds)}` : ''}
                </span>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function Versions({ st, canRetrain, onRollback }: { st: PageRouterStatus; canRetrain: boolean; onRollback: (v: string) => void }) {
  if (!st.versions.length) return <Empty title="Версий пока нет">Исходная модель регистрируется при первом отборе страниц.</Empty>
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
            <th>Страницы со значением в первых {st.top_k}</th>
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
              <td>{accText(part(v.metrics as RouterMetrics | null, 'found'))}</td>
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

function ParamList({ title, codes }: { title: string; codes: string[] }) {
  if (!codes.length) return null
  return (
    <>
      <dt>{title}</dt>
      <dd className="mono" style={{ overflowWrap: 'anywhere' }}>
        {codes.join(', ')}
      </dd>
    </>
  )
}

export function PageRouterTab() {
  const toast = useToast()
  const { can } = useAuth()
  const canRetrain = can('retrain')
  const st = useAsync((signal) => api.pageRouter(signal), [])
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
  const objects = Object.keys(man.trained_on ?? {})
  const ex = man.expert_labels
  const lab = s.labels

  const retrain = async () => {
    setBusy(true)
    try {
      const r = await api.retrainPageRouter()
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
        await api.rejectPageRouter(confirm.version)
        toast.ok(`Версия ${confirm.version} отклонена — опубликовать её нельзя`)
      } else {
        const r = confirm.kind === 'approve' ? await api.approvePageRouter(confirm.version) : await api.rollbackPageRouter(confirm.version)
        toast.ok(`Действующая версия отбора: ${r.current}`)
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
    ? 'Модели отбора нет'
    : running
      ? 'Итерация уже идёт'
      : lab.positive + lab.negative === 0
        ? 'Решений экспертов по кандидатам пока нет'
        : !lab.new
          ? 'Новых решений нет'
          : null

  return (
    <>
      <div className="twocol">
        <div>
          <section className="section">
            <div className="section__head">
              <h2>Действующая версия</h2>
              <p>
                Модель выбирает страницы, где вероятнее значение параметра; её список сливается с отбором по словам, на
                параметр и стадию идут первые {s.top_k} страниц
              </p>
            </div>
            {s.select_mode !== 'fused' ? (
              <p className="note note--warn">Отбор по модели выключен настройкой SELECT_MODE={s.select_mode}: страницы выбираются только по словам.</p>
            ) : null}
            <dl className="kv">
              <dt>Версия</dt>
              <dd className="mono">{s.current ?? '—'}</dd>
              {man.n_models ? (
                <>
                  <dt>Параметров с моделью</dt>
                  <dd className="num">{formatInt(man.n_models)}</dd>
                </>
              ) : null}
              {man.n_pages ? (
                <>
                  <dt>Исходное обучение</dt>
                  <dd>
                    {formatInt(man.n_pages)} стр.{objects.length ? ` · ${objects.length} объекта` : ''}
                    {man.labels ? <span className="muted"> · {man.labels}</span> : null}
                  </dd>
                </>
              ) : null}
              {ex ? (
                <>
                  <dt>Дообучена на</dt>
                  <dd>
                    решениях экспертов: {formatInt(ex.decisions)} карточек, {formatInt(ex.positive)} стр. со значением, {formatInt(ex.negative)} неверных
                  </dd>
                </>
              ) : null}
              <ParamList title="Дообучены параметры" codes={man.adaptation?.updated_params ?? []} />
              <ParamList title="Новые модели параметров" codes={man.adaptation?.new_params ?? []} />
            </dl>
          </section>
        </div>
        <div>
          <section className="section">
            <div className="section__head">
              <h2>Метки из решений экспертов</h2>
              <p>Решения по кандидатам в рабочем месте; страницы значений берутся из карточки</p>
            </div>
            <dl className="kv">
              <dt>Карточек с решением</dt>
              <dd className="num">{formatInt(lab.decisions)}</dd>
              <dt>Страниц со значением</dt>
              <dd className="num">{formatInt(lab.positive)}</dd>
              <dt>Страниц с неверным значением</dt>
              <dd className="num">{formatInt(lab.negative)}</dd>
              <dt>Параметров · проверок</dt>
              <dd className="num">
                {formatInt(lab.params)} · {formatInt(lab.processes)}
              </dd>
              <dt>Новые решения</dt>
              <dd>{lab.new ? <strong>есть — можно дообучить</strong> : 'нет — на всех решениях уже учились'}</dd>
            </dl>
            {canRetrain ? (
              <div className="btnrow" style={{ marginTop: 14 }}>
                <button type="button" className="btn btn--primary" disabled={busy || !!blocked} onClick={retrain} title={blocked ?? undefined}>
                  {running ? 'Идёт дообучение…' : 'Дообучить на решениях экспертов'}
                </button>
                {blocked && !running ? <span className="muted">{blocked}</span> : null}
              </div>
            ) : null}
            <p className="field__hint" style={{ marginTop: 8 }}>
              «Подтвердить» и отклонение с верным значением (согласованное изменение, ошибка OCR, не та локация,
              неактуальная редакция) — на страницах карточки значение есть; «значение извлечено неверно» — нет. Итерация
              начинается с исходной модели и берёт все решения к этому моменту: параметры с метками дообучаются от своих
              весов, остальные не меняются. Новая версия сама не публикуется — её утверждает или отклоняет ответственный.
            </p>
          </section>
        </div>
      </div>

      <section className="section">
        <div className="section__head">
          <h2>Журнал итераций</h2>
          <p>ML_Retraining_Log: версии, число страниц с метками и их SHA-256, метрики до и после, правило приёмки</p>
        </div>
        <LogTable
          rows={s.log}
          topK={s.top_k}
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
        title={confirm?.kind === 'approve' ? 'Утвердить версию отбора' : confirm?.kind === 'reject' ? 'Отклонить версию отбора' : 'Откатить отбор страниц'}
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
            модель не меняется. Решения экспертов остаются и войдут в следующую итерацию. Решение попадёт в журнал.
          </p>
        ) : (
          <p>
            Версия <Copyable value={confirm?.version ?? ''} /> станет действующей: с новых запусков страницы отбирает она.
            Прежняя остаётся в списке версий — к ней можно вернуться. Действие попадёт в журнал.
          </p>
        )}
      </Modal>
    </>
  )
}
