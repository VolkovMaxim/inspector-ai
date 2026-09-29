// Тип открытой страницы на рабочем месте и его правка: инспектор видит, что страница размечена неверно (не тот тип,
// не начало документа) — исправляет здесь же; правка меняет метку и идёт в дообучение классификатора страниц.
import { useEffect, useState } from 'react'
import { errorText } from '../../api/client'
import { api } from '../../api/endpoints'
import type { PageCorrectionIn } from '../../api/types'
import { useAuth } from '../../auth/AuthContext'
import { Icon } from '../../components/Icon'
import { Modal } from '../../components/Modal'
import { useToast } from '../../components/Toasts'
import { useAsync } from '../../lib/hooks'
import { ClassSelect, SectionSelect, classLabel, useTaxonomy } from '../process/pageLabel'

export function PageTypeControl({ processId, filePk, page }: { processId: string; filePk: number; page: number }) {
  const { can } = useAuth()
  const toast = useToast()
  const info = useAsync((signal) => api.pageInfo(filePk, page, signal), [filePk, page])
  const taxonomy = useTaxonomy()
  const [open, setOpen] = useState(false)
  const [cls, setCls] = useState('')
  const [first, setFirst] = useState<boolean | null>(null)
  const [section, setSection] = useState('')
  const [comment, setComment] = useState('')
  const [busy, setBusy] = useState(false)
  const d = info.data

  useEffect(() => {
    if (!open || !d) return
    setCls(d.page_class ?? '')
    setFirst(d.is_first_page)
    setSection(d.section ?? '')
    setComment('')
  }, [open, d])

  if (!d) return null
  const item: PageCorrectionIn = { file_pk: filePk, page_no: page }
  if (cls && cls !== d.page_class) item.page_class = cls
  if (first !== null && first !== !!d.is_first_page) item.is_first_page = first
  if (section && section !== d.section) item.section = section
  const dirty = Object.keys(item).length > 2

  const save = async () => {
    setBusy(true)
    try {
      await api.correctPages(processId, [item], comment.trim() || undefined)
      toast.ok('Метка страницы исправлена — правка пойдёт в дообучение классификатора')
      setOpen(false)
      info.reload(true)
    } catch (e) {
      toast.error(`Не сохранено: ${errorText(e)}`)
    } finally {
      setBusy(false)
    }
  }

  const text = `${classLabel(d.page_class)}${d.is_first_page ? ' · начало документа' : ''}`
  if (!can('label')) {
    return (
      <span className="pagetype" title="Тип страницы">
        {text}
      </span>
    )
  }
  return (
    <>
      <button type="button" className="pagetype pagetype--btn" onClick={() => setOpen(true)} title="Тип страницы — исправить, если неверный">
        {text}
        <Icon name="edit" size={12} />
      </button>
      <Modal
        open={open}
        title={`Метка страницы ${page}`}
        onClose={() => setOpen(false)}
        footer={
          <>
            <button type="button" className="btn" onClick={() => setOpen(false)}>
              Отмена
            </button>
            <button type="button" className="btn btn--primary" onClick={save} disabled={busy || !dirty}>
              {busy ? 'Сохраняем…' : 'Сохранить'}
            </button>
          </>
        }
      >
        <p className="muted">
          Тип и начало документа определяют, как сервис собирает документы и где ищет значения. Правка сразу меняет метку и
          попадает в обучающие данные классификатора; сборка документов обновится при следующем запуске проверки.
        </p>
        <label className="field">
          <span className="field__label">Тип страницы</span>
          <ClassSelect taxonomy={taxonomy} value={cls} onChange={setCls} />
        </label>
        <div className="field">
          <span className="field__label">С этой страницы начинается документ</span>
          <div className="btngroup" role="group" aria-label="Первая страница">
            <button type="button" className={`btn btn--small${first ? ' btn--primary' : ''}`} onClick={() => setFirst(true)}>
              да
            </button>
            <button type="button" className={`btn btn--small${first === false ? ' btn--primary' : ''}`} onClick={() => setFirst(false)}>
              нет
            </button>
          </div>
        </div>
        <label className="field">
          <span className="field__label">Раздел</span>
          <SectionSelect taxonomy={taxonomy} value={section} onChange={setSection} />
        </label>
        <label className="field">
          <span className="field__label">Комментарий (необязательно)</span>
          <textarea className="textarea" value={comment} onChange={(e) => setComment(e.target.value)} maxLength={2000} />
        </label>
      </Modal>
    </>
  )
}
