import { useEffect, useMemo, useRef, useState } from 'react'
import { useParams } from 'react-router-dom'
import { api } from '../../api/endpoints'
import type { ProtocolFile, Source, Stage } from '../../api/types'
import { StageTag } from '../../components/Badges'
import { ErrorBox, Loading } from '../../components/Feedback'
import { Icon } from '../../components/Icon'
import { PageViewer, type ViewerFocus } from '../../components/PageViewer'
import { Stamp } from '../../components/Stamp'
import { ZOOM_MAX, ZOOM_MIN, normalizeBbox, stepZoom } from '../../lib/bbox'
import { useAsync } from '../../lib/hooks'
import { APPROVAL_LABEL, BBOX_SOURCE_LABEL, STAGE_LONG, STAGE_SHORT, TEXT_SOURCE_LABEL, label } from '../../lib/labels'
import { classLabel } from '../process/pageLabel'
import { PageTypeControl } from './PageTypeControl'

export interface FocusRequest {
  seq: number
  stage: Stage | 'all'
  sourceIndex?: number
}

interface Props {
  stage: Stage
  sources: Source[]
  files: Map<number, ProtocolFile>
  stagePresent: boolean
  /** значение стадии из карточки (на случай, если источника со страницей нет) */
  cardValue: string | null
  cardKey: string
  focusReq: FocusRequest
  linkedZoom: { linked: boolean; zoom: number; setZoom: (z: number) => void }
  expanded: boolean
  onToggleExpand: () => void
  /** меняется при перестройке раскладки (развернули панель, скрыли карточку) — вернуть взгляд к доказательству */
  layoutKey: string
}

function PageText({ filePk, page, snippet, value }: { filePk: number; page: number; snippet: string | null; value: string | null }) {
  const info = useAsync((signal) => api.pageInfo(filePk, page, signal), [filePk, page])
  if (info.loading && !info.data) return <Loading text="Текст страницы…" />
  if (info.error && !info.data) return <div className="pagetext"><ErrorBox error={info.error} onRetry={() => info.reload()} /></div>
  const d = info.data
  if (!d) return null
  const text = d.text || ''
  const needle = (snippet || value || '').trim()
  let parts: [string, string, string] | null = null
  if (needle) {
    const i = text.toLocaleLowerCase('ru').indexOf(needle.toLocaleLowerCase('ru'))
    if (i >= 0) parts = [text.slice(0, i), text.slice(i, i + needle.length), text.slice(i + needle.length)]
  }
  return (
    <div className="pagetext">
      <div className="pagetext__meta">
        Текст: {label(TEXT_SOURCE_LABEL, d.text_source, 'нет')}
        {d.page_class ? `; тип страницы: ${classLabel(d.page_class)}` : ''}
        {d.section ? `; раздел: ${d.section}` : ''}
        {d.low_quality ? `; низкое качество${d.quality_label ? ` (${d.quality_label})` : ''}` : ''}
        {needle && !parts ? '; цитата в тексте страницы дословно не найдена' : ''}
      </div>
      {text ? (
        parts ? (
          <>
            {parts[0]}
            <mark>{parts[1]}</mark>
            {parts[2]}
          </>
        ) : (
          text
        )
      ) : (
        <span className="muted">Текста на странице нет.</span>
      )}
    </div>
  )
}

export function EvidencePanel({ stage, sources, files, stagePresent, cardValue, cardKey, focusReq, linkedZoom, expanded, onToggleExpand, layoutKey }: Props) {
  const { id: processId = '' } = useParams()
  const withPage = useMemo(() => sources.filter((s) => s.file_pk && s.page), [sources])
  const [srcIdx, setSrcIdx] = useState(0)
  const src = withPage[Math.min(srcIdx, Math.max(0, withPage.length - 1))] as Source | undefined
  const [page, setPage] = useState<number>(src?.page ?? 1)
  const [localZoom, setLocalZoom] = useState(1)
  const [focus, setFocus] = useState<ViewerFocus>({ seq: 0, autoZoom: true })
  const [showText, setShowText] = useState(false)
  const [pageInput, setPageInput] = useState(String(src?.page ?? 1))

  const zoom = linkedZoom.linked ? linkedZoom.zoom : localZoom
  const setZoom = (z: number) => {
    const v = Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, z))
    if (linkedZoom.linked) linkedZoom.setZoom(v)
    else setLocalZoom(v)
  }

  // новая карточка — к первому доказательству стадии, с подбором масштаба
  useEffect(() => {
    setSrcIdx(0)
    const first = withPage[0]
    setPage(first?.page ?? 1)
    setShowText(false)
    setFocus((f) => ({ seq: f.seq + 1, autoZoom: !linkedZoom.linked }))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cardKey])

  // клик по значению или источнику в карточке
  useEffect(() => {
    if (!focusReq.seq || (focusReq.stage !== 'all' && focusReq.stage !== stage)) return
    const idx = focusReq.sourceIndex !== undefined ? Math.min(focusReq.sourceIndex, withPage.length - 1) : srcIdx
    const s = withPage[idx]
    if (!s) return
    setSrcIdx(idx)
    setPage(s.page)
    setShowText(false)
    setFocus((f) => ({ seq: f.seq + 1, autoZoom: !linkedZoom.linked }))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [focusReq.seq])

  // ширина панели резко изменилась — старая прокрутка ничего не значит: снова к рамке на текущей странице
  const firstLayout = useRef(true)
  useEffect(() => {
    if (firstLayout.current) {
      firstLayout.current = false
      return
    }
    setFocus((f) => ({ seq: f.seq + 1, autoZoom: !linkedZoom.linked }))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [layoutKey])

  useEffect(() => setPageInput(String(page)), [page])

  const goEvidence = () => {
    if (!src) return
    setPage(src.page)
    setShowText(false)
    setFocus((f) => ({ seq: f.seq + 1, autoZoom: !linkedZoom.linked }))
  }

  const switchSource = (idx: number) => {
    const s = withPage[idx]
    if (!s) return
    setSrcIdx(idx)
    setPage(s.page)
    setFocus((f) => ({ seq: f.seq + 1, autoZoom: !linkedZoom.linked }))
  }

  if (!src) {
    return (
      <section className="panel panel--empty" aria-label={STAGE_LONG[stage]}>
        <div className="panel__head">
          <div className="panel__title">
            <StageTag stage={stage} />
            <strong>{STAGE_LONG[stage]}</strong>
          </div>
        </div>
        <div className="panel__empty">
          <strong>Доказательства {STAGE_SHORT[stage]} нет</strong>
          {!stagePresent
            ? `Файлов ${STAGE_SHORT[stage]} в пакете нет — сравнение по этой стадии невозможно.`
            : cardValue
              ? `Значение «${cardValue}» указано без страницы-источника.`
              : `Значение на страницах ${STAGE_SHORT[stage]} не найдено.`}
        </div>
      </section>
    )
  }

  const file = files.get(src.file_pk)
  const pages = file?.pages || null
  const bbox = normalizeBbox(src.bbox)
  const onEvidencePage = page === src.page
  const noPlace = !bbox

  return (
    <section className="panel" aria-label={STAGE_LONG[stage]}>
      <div className="panel__head">
        <div className="panel__title">
          <StageTag stage={stage} />
          <strong title={STAGE_LONG[stage]}>{STAGE_LONG[stage]}</strong>
          <span className="grow" />
          {withPage.length > 1 ? (
            <span className="panel__srcnav">
              <button type="button" className="iconbtn iconbtn--small" onClick={() => switchSource(srcIdx - 1)} disabled={srcIdx === 0} aria-label="Предыдущий источник">
                <Icon name="chevronLeft" size={13} />
              </button>
              источник {srcIdx + 1} из {withPage.length}
              <button type="button" className="iconbtn iconbtn--small" onClick={() => switchSource(srcIdx + 1)} disabled={srcIdx >= withPage.length - 1} aria-label="Следующий источник">
                <Icon name="chevronRight" size={13} />
              </button>
            </span>
          ) : null}
          <button type="button" className="iconbtn iconbtn--small" onClick={onToggleExpand} aria-label={expanded ? 'Вернуть три панели' : 'Развернуть панель'} title={expanded ? 'Вернуть три панели' : 'Развернуть панель'}>
            <Icon name={expanded ? 'collapse' : 'expand'} size={14} />
          </button>
        </div>
        <Stamp
          dense
          columns="minmax(0,1.6fr) minmax(0,0.45fr) minmax(0,0.55fr)"
          cells={[
            { label: 'Шифр', value: src.document_code, mono: true, title: src.document_code ?? '' },
            { label: 'Изм.', value: src.revision, mono: true },
            { label: 'Лист', value: src.sheet, mono: true },
            {
              label: 'Файл',
              value: (
                <>
                  <span className="mono">{src.file_id}</span> {src.file_name}
                </>
              ),
              title: `${src.file_name ?? ''}\nSHA-256: ${src.sha256 ?? '—'}`,
            },
            {
              label: 'Утверждение',
              value: label(APPROVAL_LABEL, src.approval_status, 'не указан'),
              muted: !src.approval_status || src.approval_status === 'UNKNOWN',
              span: 2,
            },
          ]}
        />
        <div className="panel__value">
          <span className="muted">{stage === 'PD' ? 'Ожидаемое' : 'Фактическое'}</span>
          <span className="mono" title={src.snippet ?? src.value ?? ''}>
            {src.value ?? cardValue ?? '—'}
          </span>
          <button type="button" className="linkbtn" onClick={goEvidence} title="Открыть страницу доказательства и показать рамку">
            стр. {src.page}
          </button>
          {noPlace ? <span className="panel__nobox">{BBOX_SOURCE_LABEL.page}</span> : null}
        </div>
      </div>

      <div className="panel__tools">
        <button type="button" className="iconbtn" onClick={() => setPage((p) => Math.max(1, p - 1))} disabled={page <= 1} aria-label="Предыдущая страница">
          <Icon name="chevronLeft" />
        </button>
        <label className="pageinput">
          <input
            value={pageInput}
            inputMode="numeric"
            aria-label="Номер страницы"
            onChange={(e) => setPageInput(e.target.value.replace(/\D/g, ''))}
            onKeyDown={(e) => {
              if (e.key === 'Enter') {
                const n = Number(pageInput)
                if (n >= 1 && (!pages || n <= pages)) setPage(n)
                else setPageInput(String(page))
                ;(e.target as HTMLInputElement).blur()
              }
              if (e.key === 'Escape') {
                setPageInput(String(page))
                ;(e.target as HTMLInputElement).blur()
              }
            }}
            onBlur={() => setPageInput(String(page))}
          />
          {pages ? `из ${pages}` : ''}
        </label>
        <button type="button" className="iconbtn" onClick={() => setPage((p) => (pages ? Math.min(pages, p + 1) : p + 1))} disabled={!!pages && page >= pages} aria-label="Следующая страница">
          <Icon name="chevronRight" />
        </button>
        <span className="sep" />
        <button type="button" className="iconbtn" onClick={() => setZoom(stepZoom(zoom, -1))} disabled={zoom <= ZOOM_MIN} aria-label="Уменьшить">
          <Icon name="minus" />
        </button>
        <span className="zoomlabel" title="Масштаб относительно ширины панели">
          {Math.round(zoom * 100)}%
        </span>
        <button type="button" className="iconbtn" onClick={() => setZoom(stepZoom(zoom, 1))} disabled={zoom >= ZOOM_MAX} aria-label="Увеличить">
          <Icon name="plus" />
        </button>
        <button type="button" className="iconbtn" onClick={() => setZoom(1)} aria-label="По ширине" title="По ширине">
          <Icon name="fitWidth" />
        </button>
        <span className="grow" />
        <PageTypeControl processId={processId} filePk={src.file_pk} page={page} />
        <button type="button" className="iconbtn" onClick={goEvidence} aria-label="К доказательству" title="К доказательству: страница и рамка значения">
          <Icon name="target" />
        </button>
        <button
          type="button"
          className={`iconbtn${showText ? ' iconbtn--on' : ''}`}
          onClick={() => setShowText((v) => !v)}
          aria-pressed={showText}
          aria-label="Текст страницы"
          title="Текст страницы (слой или OCR) с цитатой"
        >
          <Icon name="text" />
        </button>
      </div>

      {showText ? (
        <PageText filePk={src.file_pk} page={page} snippet={onEvidencePage ? src.snippet : null} value={onEvidencePage ? src.value : null} />
      ) : (
        <PageViewer
          filePk={src.file_pk}
          page={page}
          bbox={onEvidencePage ? bbox : null}
          wholePage={onEvidencePage && !bbox}
          zoom={zoom}
          onZoom={setZoom}
          focus={focus}
          evidencePage={src.page}
          onGoEvidence={goEvidence}
        />
      )}
    </section>
  )
}
