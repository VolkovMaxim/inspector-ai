// Нормативная база: матрица 132 параметров. Смотрят все; правит администратор (триггер, пороги, активность,
// приоритет, ссылки на нормы).
import { useMemo, useState } from 'react'
import { errorText } from '../api/client'
import { api } from '../api/endpoints'
import type { Param } from '../api/types'
import { useAuth } from '../auth/AuthContext'
import { PriorityMark } from '../components/Badges'
import { Empty, ErrorBox, Field, Loading } from '../components/Feedback'
import { Modal } from '../components/Modal'
import { useToast } from '../components/Toasts'
import { formatDateTime } from '../lib/format'
import { useAsync, useDocumentTitle } from '../lib/hooks'
import { diffParam, toDraft, type Draft } from '../lib/params'

function ParamModal({ param, editable, onClose, onSaved }: { param: Param; editable: boolean; onClose: () => void; onSaved: (p: Param) => void }) {
  const toast = useToast()
  const [d, setD] = useState<Draft>(() => toDraft(param))
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const set = <K extends keyof Draft>(k: K, v: Draft[K]) => setD((x) => ({ ...x, [k]: v }))

  const save = async () => {
    const patch = diffParam(param, d)
    if (typeof patch === 'string') {
      setError(patch)
      return
    }
    if (!Object.keys(patch).length) {
      onClose()
      return
    }
    setBusy(true)
    try {
      const updated = await api.patchParam(param.code, patch)
      toast.ok(`Параметр ${param.code} сохранён`)
      onSaved(updated)
      onClose()
    } catch (e) {
      setError(errorText(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal
      open
      width={860}
      title={`${param.code} — ${param.parameter_name}`}
      onClose={onClose}
      footer={
        editable ? (
          <>
            <button type="button" className="btn" onClick={onClose}>
              Отмена
            </button>
            <button type="button" className="btn btn--primary" onClick={save} disabled={busy}>
              {busy ? 'Сохраняем…' : 'Сохранить'}
            </button>
          </>
        ) : (
          <button type="button" className="btn" onClick={onClose}>
            Закрыть
          </button>
        )
      }
    >
      <dl className="kv">
        <dt>Раздел</dt>
        <dd>{param.section}</dd>
        <dt>Единица</dt>
        <dd>{param.unit || '—'}</dd>
        <dt>Критичность</dt>
        <dd>{param.criticality || '—'}</dd>
        <dt>Где в ПД</dt>
        <dd>{param.source_pd || '—'}</dd>
        <dt>Где в РД</dt>
        <dd>{param.source_rd || '—'}</dd>
        <dt>Где в ИД</dt>
        <dd>{param.source_id || '—'}</dd>
        <dt>Изменён</dt>
        <dd>{formatDateTime(param.updated_at)}</dd>
      </dl>
      <div className="paramedit">
        <div className="span4">
          <Field label="Логика триггера (условие нарушения)">
            <textarea className="textarea" rows={4} value={d.trigger_logic} disabled={!editable} onChange={(e) => set('trigger_logic', e.target.value)} />
          </Field>
        </div>
        <Field label="Минимум">
          <input className="input num" value={d.min_value} disabled={!editable} onChange={(e) => set('min_value', e.target.value)} inputMode="decimal" />
        </Field>
        <Field label="Максимум">
          <input className="input num" value={d.max_value} disabled={!editable} onChange={(e) => set('max_value', e.target.value)} inputMode="decimal" />
        </Field>
        <Field label="Приоритет рассмотрения">
          <select className="select" value={d.review_priority} disabled={!editable} onChange={(e) => set('review_priority', e.target.value)}>
            <option value="HIGH">Высокий</option>
            <option value="MEDIUM">Средний</option>
            <option value="LOW">Низкий</option>
          </select>
        </Field>
        <Field label="Проверяется">
          <label className="check" style={{ minHeight: 32 }}>
            <input type="checkbox" checked={d.is_active} disabled={!editable} onChange={(e) => set('is_active', e.target.checked)} />
            {d.is_active ? 'да, параметр в матрице' : 'нет, исключён'}
          </label>
        </Field>
        <div className="span2">
          <Field label="СП">
            <textarea className="textarea" rows={2} value={d.sp_reference} disabled={!editable} onChange={(e) => set('sp_reference', e.target.value)} />
          </Field>
        </div>
        <div className="span2">
          <Field label="ГОСТ">
            <textarea className="textarea" rows={2} value={d.gost_reference} disabled={!editable} onChange={(e) => set('gost_reference', e.target.value)} />
          </Field>
        </div>
        <div className="span2">
          <Field label="ФЗ">
            <textarea className="textarea" rows={2} value={d.fz_reference} disabled={!editable} onChange={(e) => set('fz_reference', e.target.value)} />
          </Field>
        </div>
        <div className="span2">
          <Field label="Другие нормы">
            <textarea className="textarea" rows={2} value={d.other_normative} disabled={!editable} onChange={(e) => set('other_normative', e.target.value)} />
          </Field>
        </div>
      </div>
      {error ? <p className="dbar__error">{error}</p> : null}
    </Modal>
  )
}

export function ParamsPage() {
  useDocumentTitle('Нормативная база')
  const { can } = useAuth()
  const editable = can('editParams')
  const list = useAsync((signal) => api.params(signal), [])
  const [q, setQ] = useState('')
  const [section, setSection] = useState('')
  const [onlyActive, setOnlyActive] = useState(false)
  const [open, setOpen] = useState<Param | null>(null)

  const items = useMemo(() => list.data ?? [], [list.data])
  const sections = useMemo(() => Array.from(new Set(items.map((p) => p.section))).filter(Boolean), [items])
  const needle = q.trim().toLocaleLowerCase('ru')
  const shown = items.filter(
    (p) =>
      (!section || p.section === section) &&
      (!onlyActive || p.is_active) &&
      (!needle || p.code.toLocaleLowerCase('ru').includes(needle) || p.parameter_name.toLocaleLowerCase('ru').includes(needle)),
  )

  return (
    <div className="page">
      <div className="pagehead">
        <div>
          <h1>Нормативная база</h1>
          <p>
            Матрица контроля: {items.length} параметров. {editable ? 'Откройте параметр, чтобы изменить триггер, пороги или активность.' : 'Изменения вносит администратор.'}
          </p>
        </div>
      </div>
      <div className="toolbar">
        <input className="input" type="search" placeholder="Код или название параметра" value={q} onChange={(e) => setQ(e.target.value)} aria-label="Поиск параметра" />
        <select className="select" value={section} onChange={(e) => setSection(e.target.value)} aria-label="Раздел">
          <option value="">Все разделы</option>
          {sections.map((s) => (
            <option key={s} value={s}>
              {s}
            </option>
          ))}
        </select>
        <label className="check">
          <input type="checkbox" checked={onlyActive} onChange={(e) => setOnlyActive(e.target.checked)} />
          Только проверяемые
        </label>
        <span className="toolbar__spacer" />
        <span className="muted num">Показано: {shown.length}</span>
      </div>
      {list.error && !list.data ? <ErrorBox error={list.error} onRetry={() => list.reload()} /> : null}
      {list.loading && !list.data ? <Loading /> : null}
      {list.data && !shown.length ? <Empty title="Параметры не найдены">Измените раздел или строку поиска.</Empty> : null}
      {shown.length ? (
        <div className="tablewrap">
          <table className="table table--hover">
            <thead>
              <tr>
                <th>Код</th>
                <th>Раздел</th>
                <th>Параметр</th>
                <th>Ед.</th>
                <th>Триггер</th>
                <th>Критичность</th>
                <th>Приоритет</th>
                <th className="col-num">Мин.</th>
                <th className="col-num">Макс.</th>
                <th>Проверяется</th>
              </tr>
            </thead>
            <tbody>
              {shown.map((p) => (
                <tr key={p.code} onClick={() => setOpen(p)} className={p.is_active ? undefined : 'is-dim'}>
                  <td className="mono nowrap">{p.code}</td>
                  <td>{p.section}</td>
                  <td>{p.parameter_name}</td>
                  <td className="nowrap">{p.unit || '—'}</td>
                  <td>
                    <span className="trigger" title={p.trigger_logic}>
                      {p.trigger_logic || '—'}
                    </span>
                  </td>
                  <td>{p.criticality || '—'}</td>
                  <td>
                    <PriorityMark value={String(p.review_priority)} />
                  </td>
                  <td className="col-num">{p.min_value ?? '—'}</td>
                  <td className="col-num">{p.max_value ?? '—'}</td>
                  <td>{p.is_active ? 'да' : <span className="muted">нет</span>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
      {open ? (
        <ParamModal
          param={open}
          editable={editable}
          onClose={() => setOpen(null)}
          onSaved={(u) => list.setData((xs) => (xs ? xs.map((x) => (x.code === u.code ? u : x)) : xs))}
        />
      ) : null}
    </div>
  )
}
