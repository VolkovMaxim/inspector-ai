// Крупный просмотр страницы из сетки: страница, всё, что о ней знает сервис, и правка метки.
// ← → — соседние страницы списка; 1 / 0 — первая / не первая; T — тип; Enter — сохранить и дальше; Esc — закрыть.
import { useEffect, useRef, useState } from 'react'
import { api } from '../../api/endpoints'
import type { PageRow, PageTaxonomy } from '../../api/types'
import { StageTag } from '../../components/Badges'
import { Icon } from '../../components/Icon'
import { PageViewer } from '../../components/PageViewer'
import { ZOOM_MAX, ZOOM_MIN } from '../../lib/bbox'
import { onControl, useHotkeys } from '../../lib/hooks'
import { LABEL_SOURCE_LABEL, TEXT_SOURCE_LABEL, label } from '../../lib/labels'
import { isEmptyPatch, pct, type CorrectionPatch } from '../../lib/pages'
import { ClassSelect, SectionSelect, SourceMark, classLabel } from './pageLabel'

const NO_FOCUS = { seq: 0, autoZoom: false }

function PageText({ row }: { row: PageRow }) {
  const [text, setText] = useState<string | null>(null)
  const [open, setOpen] = useState(false)
  useEffect(() => setText(null), [row.page_pk])
  useEffect(() => {
    if (!open || text !== null) return
    let alive = true
    api
      .pageInfo(row.file_pk, row.page_no)
      .then((p) => alive && setText(p.text || ''))
      .catch(() => alive && setText('— текст не загрузился —'))
    return () => {
      alive = false
    }
  }, [open, text, row.file_pk, row.page_no])
  return (
    <details className="pgprev__text" onToggle={(e) => setOpen((e.target as HTMLDetailsElement).open)}>
      <summary>Текст страницы ({label(TEXT_SOURCE_LABEL, row.text_source)})</summary>
      <pre>{text === null ? 'Загрузка…' : text || '— пусто —'}</pre>
    </details>
  )
}

export function PagePreview({
  rows,
  index,
  onIndex,
  onClose,
  taxonomy,
  canLabel,
  onSave,
  busy,
}: {
  rows: PageRow[]
  index: number
  onIndex: (i: number) => void
  onClose: () => void
  taxonomy: PageTaxonomy | null
  canLabel: boolean
  onSave: (row: PageRow, patch: CorrectionPatch) => Promise<boolean>
  busy: boolean
}) {
  const ref = useRef<HTMLDialogElement>(null)
  const clsRef = useRef<HTMLSelectElement>(null)
  const row = rows[index]
  const [zoom, setZoom] = useState(1)
  const [cls, setCls] = useState('')
  const [first, setFirst] = useState<boolean | null>(null)
  const [section, setSection] = useState('')

  useEffect(() => {
    const d = ref.current
    if (d && !d.open) d.showModal()
    d?.querySelector<HTMLElement>('.viewer')?.focus()        // не на первую кнопку: пробел не должен менять масштаб
  }, [])

  // новая страница — форма с её текущими метками
  useEffect(() => {
    if (!row) return
    setCls(row.page_class ?? '')
    setFirst(row.is_first_page)
    setSection(row.section ?? '')
  }, [row])

  const patch: CorrectionPatch = {}
  if (row && cls && cls !== row.page_class) patch.page_class = cls
  if (row && first !== null && first !== !!row.is_first_page) patch.is_first_page = first
  if (row && section && section !== row.section) patch.section = section
  const dirty = !isEmptyPatch(patch)
  const go = (d: number) => {
    const next = index + d
    if (next >= 0 && next < rows.length) onIndex(next)
  }
  const save = async (andNext: boolean) => {
    if (!row) return
    if (dirty && !(await onSave(row, patch))) return
    if (andNext) go(1)
  }

  useHotkeys((e) => {
    if (onControl(e)) return
    if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') {
      e.preventDefault()
      go(e.key === 'ArrowLeft' ? -1 : 1)
    } else if (canLabel && e.code === 'Digit1') setFirst(true)
    else if (canLabel && e.code === 'Digit0') setFirst(false)
    else if (canLabel && e.code === 'KeyT') {
      e.preventDefault()
      clsRef.current?.focus()
    } else if (e.key === 'Enter') {
      e.preventDefault()
      if (canLabel) save(true)
      else go(1)
    }
  }, true, { inDialog: true })

  if (!row) return null
  return (
    <dialog
      ref={ref}
      className="modal pgprev"
      onCancel={(e) => {
        e.preventDefault()
        onClose()
      }}
      onClick={(e) => {
        if (e.target === ref.current) onClose()
      }}
    >
      <div className="pgprev__inner">
        <header className="modal__head">
          <div className="pgprev__title">
            <StageTag stage={row.stage} />
            <span className="mono">{row.file_id}</span>
            <span>стр. {row.page_no}</span>
            <span className="muted cell-clip" title={row.file_name}>
              {row.file_name}
            </span>
          </div>
          <div className="pgprev__nav">
            <button type="button" className="iconbtn" onClick={() => setZoom((z) => Math.max(ZOOM_MIN, +(z / 1.25).toFixed(2)))} aria-label="Уменьшить" title="Уменьшить">
              <Icon name="minus" />
            </button>
            <button type="button" className="iconbtn" onClick={() => setZoom(1)} aria-label="По ширине" title="По ширине">
              <Icon name="fitWidth" />
            </button>
            <button type="button" className="iconbtn" onClick={() => setZoom((z) => Math.min(ZOOM_MAX, +(z * 1.25).toFixed(2)))} aria-label="Увеличить" title="Увеличить">
              <Icon name="plus" />
            </button>
            <span className="pgprev__pos num">
              {index + 1} из {rows.length}
            </span>
            <button type="button" className="iconbtn" onClick={() => go(-1)} disabled={index === 0} aria-label="Предыдущая" title="Предыдущая (←)">
              <Icon name="chevronLeft" />
            </button>
            <button type="button" className="iconbtn" onClick={() => go(1)} disabled={index >= rows.length - 1} aria-label="Следующая" title="Следующая (→)">
              <Icon name="chevronRight" />
            </button>
            <button type="button" className="iconbtn" onClick={onClose} aria-label="Закрыть" title="Закрыть (Esc)">
              <Icon name="close" />
            </button>
          </div>
        </header>
        <div className="pgprev__body">
          <div className="pgprev__viewer">
            <PageViewer
              filePk={row.file_pk}
              page={row.page_no}
              bbox={null}
              wholePage={false}
              zoom={zoom}
              onZoom={setZoom}
              focus={NO_FOCUS}
              evidencePage={null}
              onGoEvidence={() => undefined}
            />
          </div>
          <aside className="pgprev__side">
            <dl className="kv kv--tight">
              <dt>Тип</dt>
              <dd title={taxonomy?.classes.find((c) => c.code === row.page_class)?.description}>{classLabel(row.page_class)}</dd>
              {row.other_classes.length ? (
                <>
                  <dt>Ещё на странице</dt>
                  <dd>{row.other_classes.map(classLabel).join(', ')}</dd>
                </>
              ) : null}
              <dt>Первая страница</dt>
              <dd>{row.is_first_page ? 'да — с неё начинается документ' : 'нет'}</dd>
              <dt>Раздел</dt>
              <dd>{row.section && row.section !== 'НЕТ' ? row.section : '—'}</dd>
              <dt>Заголовок</dt>
              <dd>{row.title || '—'}</dd>
              <dt>Качество</dt>
              <dd>{row.quality_label || '—'}</dd>
              <dt>Метка</dt>
              <dd>
                <SourceMark row={row} />
              </dd>
              {row.model_class ? (
                <>
                  <dt>Классификатор</dt>
                  <dd>
                    {classLabel(row.model_class)}, {pct(row.model_confidence)}
                  </dd>
                </>
              ) : null}
            </dl>

            {canLabel ? (
              <form
                className="pgprev__form"
                onSubmit={(e) => {
                  e.preventDefault()
                  save(true)
                }}
              >
                <h3>Исправить метку</h3>
                <label className="field">
                  <span className="field__label">
                    Тип <kbd>T</kbd>
                  </span>
                  <ClassSelect taxonomy={taxonomy} value={cls} onChange={setCls} selectRef={clsRef} />
                </label>
                <div className="field">
                  <span className="field__label">
                    Первая страница документа <kbd>1</kbd> / <kbd>0</kbd>
                  </span>
                  <div className="btngroup" role="group" aria-label="Первая страница">
                    <button type="button" className={`btn btn--small${first ? ' btn--primary' : ''}`} onMouseDown={(e) => e.preventDefault()} onClick={() => setFirst(true)}>
                      да
                    </button>
                    <button type="button" className={`btn btn--small${first === false ? ' btn--primary' : ''}`} onMouseDown={(e) => e.preventDefault()} onClick={() => setFirst(false)}>
                      нет
                    </button>
                  </div>
                </div>
                <label className="field">
                  <span className="field__label">Раздел</span>
                  <SectionSelect taxonomy={taxonomy} value={section} onChange={setSection} />
                </label>
                <div className="btnrow">
                  <button type="submit" className="btn btn--primary" disabled={busy}>
                    {dirty ? 'Сохранить и дальше' : 'Дальше'} <kbd>Enter</kbd>
                  </button>
                  {dirty ? (
                    <button type="button" className="btn" disabled={busy} onClick={() => save(false)}>
                      Сохранить
                    </button>
                  ) : null}
                </div>
                <p className="field__hint">
                  Правка сразу меняет метку страницы и попадает в обучающие данные классификатора. Сборка документов
                  этой проверки обновится при следующем запуске.
                </p>
              </form>
            ) : (
              <p className="muted">{label(LABEL_SOURCE_LABEL, row.label_source ?? 'none')}. Исправлять метки может разметчик.</p>
            )}
            <PageText row={row} />
          </aside>
        </div>
      </div>
    </dialog>
  )
}
