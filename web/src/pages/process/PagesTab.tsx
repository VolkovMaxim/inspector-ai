// Вкладка «Страницы»: миниатюры всех страниц проверки с типом, первой страницей, разделом и источником метки;
// фильтры, выделение мышью и клавишами, массовая правка метки, крупный просмотр. Правки идут в дообучение.
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { errorText } from '../../api/client'
import { api } from '../../api/endpoints'
import type { PageRow } from '../../api/types'
import { useAuth } from '../../auth/AuthContext'
import { Empty, ErrorBox, Loading } from '../../components/Feedback'
import { Icon } from '../../components/Icon'
import { useToast } from '../../components/Toasts'
import { formatInt } from '../../lib/format'
import { onControl, useAsync, useHotkeys } from '../../lib/hooks'
import { LABEL_SOURCE_LABEL, STAGE_SHORT, STAGES, label } from '../../lib/labels'
import { pageThumbUrl } from '../../lib/pageImages'
import {
  EMPTY_FILTER,
  applyCorrection,
  applyPageFilter,
  correctionsFor,
  countBy,
  isEmptyPatch,
  moveFocus,
  orderRows,
  rangeSelect,
  scopeRows,
  type CorrectionPatch,
  type PageFilter,
  type PageOrder,
} from '../../lib/pages'
import { PagePreview } from './PagePreview'
import { ClassSelect, SectionSelect, SourceMark, classLabel, useTaxonomy } from './pageLabel'

const CHUNK = 120
const SOURCES = ['model', 'qwen', 'inspector', 'none']
const CONF_STEPS = [0.9, 0.7, 0.5]

function PageThumb({ filePk, page }: { filePk: number; page: number }) {
  const box = useRef<HTMLDivElement>(null)
  const [visible, setVisible] = useState(false)
  const [url, setUrl] = useState<string | null>(null)
  const [failed, setFailed] = useState(false)

  useEffect(() => {
    const el = box.current
    if (!el || visible) return
    const io = new IntersectionObserver((es) => es.some((e) => e.isIntersecting) && setVisible(true), { rootMargin: '400px' })
    io.observe(el)
    return () => io.disconnect()
  }, [visible])

  useEffect(() => {
    if (!visible) return
    let alive = true
    const width = (window.devicePixelRatio || 1) >= 1.5 ? 360 : 240
    pageThumbUrl(filePk, page, width)
      .then((u) => alive && setUrl(u))
      .catch(() => alive && setFailed(true))
    return () => {
      alive = false
    }
  }, [visible, filePk, page])

  return (
    <div className="pgcard__thumb" ref={box}>
      {url ? <img src={url} alt="" draggable={false} /> : failed ? <span className="pgcard__ph">не загрузилась</span> : <span className="pgcard__ph" />}
    </div>
  )
}

function PageCard({
  row,
  idx,
  selected,
  focused,
  onClick,
  onOpen,
  cardRef,
}: {
  row: PageRow
  idx: number
  selected: boolean
  focused: boolean
  onClick: (e: React.MouseEvent, idx: number) => void
  onOpen: (idx: number) => void
  cardRef: (el: HTMLDivElement | null) => void
}) {
  const cls = [
    'pgcard',
    selected ? 'is-selected' : '',
    focused ? 'is-focused' : '',
    row.is_first_page ? 'is-first' : '',
    row.corrected ? 'is-corrected' : '',
  ]
    .filter(Boolean)
    .join(' ')
  return (
    <div
      ref={cardRef}
      className={cls}
      role="option"
      aria-selected={selected}
      aria-label={`${row.file_id}, страница ${row.page_no}: ${classLabel(row.page_class)}`}
      data-idx={idx}
      onClick={(e) => onClick(e, idx)}
      onDoubleClick={() => onOpen(idx)}
    >
      <PageThumb filePk={row.file_pk} page={row.page_no} />
      <span className="pgcard__check" aria-hidden>
        {selected ? <Icon name="check" size={12} /> : null}
      </span>
      {row.is_first_page ? <span className="pgcard__first" title="С этой страницы начинается документ">начало</span> : null}
      <button
        type="button"
        className="pgcard__open"
        tabIndex={-1}
        title="Открыть крупно (Enter)"
        aria-label="Открыть крупно"
        onClick={(e) => {
          e.stopPropagation()
          onOpen(idx)
        }}
      >
        <Icon name="expand" size={14} />
      </button>
      <div className="pgcard__meta">
        <div className="pgcard__cls" title={row.title ?? undefined}>
          <span>{classLabel(row.page_class)}</span>
          {row.section && row.section !== 'НЕТ' ? <span className="pgcard__sec">{row.section}</span> : null}
        </div>
        <div className="pgcard__sub" title={`${row.file_id} — ${row.file_name}`}>
          стр. {row.page_no} · {row.file_name}
        </div>
        <SourceMark row={row} />
      </div>
    </div>
  )
}

export function PagesTab({ processId }: { processId: string }) {
  const toast = useToast()
  const { can } = useAuth()
  const canLabel = can('label')
  const data = useAsync((signal) => api.processPages(processId, signal), [processId])
  const taxonomy = useTaxonomy()
  const rows = useMemo(() => data.data ?? [], [data.data])

  const [filter, setFilter] = useState<PageFilter>(EMPTY_FILTER)
  const [order, setOrder] = useState<PageOrder>('file')
  const scope = useMemo(() => scopeRows(rows, filter), [rows, filter])
  const shown = useMemo(() => orderRows(applyPageFilter(rows, filter), order), [rows, filter, order])
  const [limit, setLimit] = useState(CHUNK)
  useEffect(() => setLimit(CHUNK), [filter, order])

  const files = useMemo(() => {
    const m = new Map<number, { file_pk: number; file_id: string; file_name: string; stage: string | null; n: number }>()
    for (const r of rows) {
      if (filter.stage && r.stage !== filter.stage) continue
      const f = m.get(r.file_pk)
      if (f) f.n++
      else m.set(r.file_pk, { file_pk: r.file_pk, file_id: r.file_id, file_name: r.file_name, stage: r.stage, n: 1 })
    }
    return [...m.values()]
  }, [rows, filter.stage])
  const clsCounts = useMemo(() => countBy(scope, (r) => r.page_class ?? ''), [scope])
  const srcCounts = useMemo(() => countBy(scope, (r) => r.label_source ?? 'none'), [scope])
  const stageCounts = useMemo(() => countBy(rows, (r) => r.stage ?? ''), [rows])
  const hasModel = useMemo(() => rows.some((r) => r.model_confidence !== null), [rows])
  const noSource = rows.length > 0 && (srcCounts.get('none') ?? 0) === scope.length

  // выделение: page_pk; фокус и якорь — позиции в показанном списке
  const [selected, setSelected] = useState<Set<number>>(() => new Set())
  const [focus, setFocus] = useState(-1)
  const [anchor, setAnchor] = useState(-1)
  const [preview, setPreview] = useState<number | null>(null)
  const [busy, setBusy] = useState(false)
  const [lastPatch, setLastPatch] = useState<CorrectionPatch | null>(null)
  const [bar, setBar] = useState<{ cls: string; first: '' | 'yes' | 'no'; section: string }>({ cls: '', first: '', section: '' })
  const clsRef = useRef<HTMLSelectElement>(null)
  const grid = useRef<HTMLDivElement>(null)
  const cards = useRef(new Map<number, HTMLDivElement>())

  // смена фильтра: выделение — только среди показанных, фокус — в пределах списка
  useEffect(() => {
    const pks = new Set(shown.map((r) => r.page_pk))
    setSelected((prev) => {
      const next = new Set([...prev].filter((pk) => pks.has(pk)))
      return next.size === prev.size ? prev : next
    })
    setFocus((f) => Math.min(f, shown.length - 1))
    setAnchor((a) => Math.min(a, shown.length - 1))
  }, [shown])

  useLayoutEffect(() => {
    if (focus >= 0) cards.current.get(focus)?.scrollIntoView({ block: 'nearest' })
  }, [focus])

  const cols = () => {
    const el = grid.current
    if (!el) return 1
    return getComputedStyle(el).gridTemplateColumns.split(' ').filter(Boolean).length || 1
  }

  const targets = useCallback((): PageRow[] => {
    if (selected.size) return shown.filter((r) => selected.has(r.page_pk))
    return focus >= 0 && shown[focus] ? [shown[focus]] : []
  }, [selected, shown, focus])

  const apply = async (patch: CorrectionPatch, target: PageRow[]): Promise<boolean> => {
    if (!canLabel || busy || isEmptyPatch(patch) || !target.length) return false
    const items = correctionsFor(target, patch)
    if (!items.length) {
      toast.info('Ничего не меняется: у выбранных страниц уже такие метки')
      return true
    }
    setBusy(true)
    try {
      await api.correctPages(processId, items)
      const byPk = new Map(items.map((i) => [i.page_pk, i]))
      data.setData((prev) => prev?.map((r) => (byPk.has(r.page_pk) ? applyCorrection(r, byPk.get(r.page_pk)!) : r)))
      setLastPatch(patch)
      toast.ok(`Исправлено страниц: ${items.length} — правки пойдут в дообучение классификатора`)
      return true
    } catch (e) {
      toast.error(`Не сохранено: ${errorText(e)}`)
      return false
    } finally {
      setBusy(false)
    }
  }

  const barPatch = (): CorrectionPatch => {
    const p: CorrectionPatch = {}
    if (bar.cls) p.page_class = bar.cls
    if (bar.first) p.is_first_page = bar.first === 'yes'
    if (bar.section) p.section = bar.section
    return p
  }

  const applyBar = async () => {
    if (await apply(barPatch(), targets())) setBar({ cls: '', first: '', section: '' })
  }

  const toggle = (pk: number) =>
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(pk)) next.delete(pk)
      else next.add(pk)
      return next
    })

  const onCardClick = (e: React.MouseEvent, idx: number) => {
    const r = shown[idx]
    setFocus(idx)
    if (e.shiftKey && anchor >= 0) {
      setSelected((prev) => new Set([...prev, ...rangeSelect(shown, anchor, idx)]))
      return
    }
    toggle(r.page_pk)
    setAnchor(idx)
  }

  useHotkeys((e) => {
    if (!shown.length || onControl(e)) return
    if (e.key.startsWith('Arrow')) {
      e.preventDefault()
      const next = moveFocus(focus, e.key, cols(), shown.length)
      if (next >= limit) setLimit((l) => l + CHUNK)
      if (e.shiftKey) {
        const a = anchor >= 0 ? anchor : Math.max(0, focus)
        setAnchor(a)
        setSelected(new Set(rangeSelect(shown, a, next)))
      } else setAnchor(next)
      setFocus(next)
      return
    }
    switch (e.code) {
      case 'Space':
        e.preventDefault()
        if (focus >= 0) toggle(shown[focus].page_pk)
        break
      case 'Enter':
        if (focus >= 0) {
          e.preventDefault()
          setPreview(focus)
        }
        break
      case 'Escape':
        setSelected(new Set())
        break
      case 'KeyA':
        e.preventDefault()
        setSelected(new Set(shown.map((r) => r.page_pk)))
        break
      case 'KeyT':
        if (canLabel && targets().length) {
          e.preventDefault()
          clsRef.current?.focus()
        }
        break
      case 'Digit1':
        apply({ is_first_page: true }, targets())
        break
      case 'Digit0':
        apply({ is_first_page: false }, targets())
        break
      case 'KeyR':
        if (lastPatch) apply(lastPatch, targets())
        break
    }
  }, preview === null)

  const set = (patch: Partial<PageFilter>) => setFilter((f) => ({ ...f, ...patch }))

  if (data.error && !data.data) return <ErrorBox error={data.error} onRetry={() => data.reload()} />
  if (data.loading && !data.data) return <Loading text="Загрузка страниц…" />
  if (!rows.length) return <Empty title="Страниц пока нет">Они появятся после этапа «Разбор страниц».</Empty>

  const nTargets = selected.size || (focus >= 0 ? 1 : 0)
  const visible = shown.slice(0, limit)

  return (
    <section className="section pgtab">
      <div className="section__head">
        <h2>Страницы и их типы</h2>
        <p>Тип, первая страница и раздел определяют, как собираются документы и где искать значения. Исправленные метки идут в дообучение классификатора.</p>
      </div>

      <div className="toolbar">
        <div className="chips" role="group" aria-label="Стадия">
          <button type="button" className="chip" aria-pressed={!filter.stage} onClick={() => set({ stage: '', filePk: null })}>
            Все <span className="chip__n">{formatInt(rows.length)}</span>
          </button>
          {STAGES.filter((s) => stageCounts.get(s)).map((s) => (
            <button key={s} type="button" className="chip" aria-pressed={filter.stage === s} onClick={() => set({ stage: s, filePk: null })}>
              {STAGE_SHORT[s]} <span className="chip__n">{formatInt(stageCounts.get(s))}</span>
            </button>
          ))}
        </div>
        <select
          className="select"
          value={filter.filePk ?? ''}
          onChange={(e) => set({ filePk: e.target.value ? Number(e.target.value) : null })}
          aria-label="Файл"
          style={{ maxWidth: 320 }}
        >
          <option value="">Все файлы ({files.length})</option>
          {files.map((f) => (
            <option key={f.file_pk} value={f.file_pk}>
              {f.file_id} — {f.file_name} ({f.n})
            </option>
          ))}
        </select>
        <select className="select" value={filter.cls} onChange={(e) => set({ cls: e.target.value })} aria-label="Тип страницы">
          <option value="">Все типы</option>
          {[...clsCounts.entries()]
            .sort((a, b) => b[1] - a[1])
            .map(([c, n]) => (
              <option key={c} value={c}>
                {classLabel(c || null)} ({n})
              </option>
            ))}
        </select>
        <select className="select" value={filter.source} onChange={(e) => set({ source: e.target.value })} aria-label="Источник метки">
          <option value="">Любой источник</option>
          {SOURCES.filter((s) => srcCounts.get(s)).map((s) => (
            <option key={s} value={s}>
              {label(LABEL_SOURCE_LABEL, s)} ({srcCounts.get(s)})
            </option>
          ))}
        </select>
        {hasModel ? (
          <select
            className="select"
            value={filter.confBelow ?? ''}
            onChange={(e) => set({ confBelow: e.target.value ? Number(e.target.value) : null })}
            aria-label="Уверенность классификатора"
          >
            <option value="">Любая уверенность</option>
            {CONF_STEPS.map((c) => (
              <option key={c} value={c}>
                Уверенность ниже {Math.round(c * 100)} %
              </option>
            ))}
          </select>
        ) : null}
        <label className="check">
          <input type="checkbox" checked={filter.firstOnly} onChange={(e) => set({ firstOnly: e.target.checked })} />
          только начальные
        </label>
        <label className="check">
          <input type="checkbox" checked={filter.correctedOnly} onChange={(e) => set({ correctedOnly: e.target.checked })} />
          только исправленные
        </label>
        {hasModel ? (
          <select className="select select--small" value={order} onChange={(e) => setOrder(e.target.value as PageOrder)} aria-label="Порядок">
            <option value="file">По файлам</option>
            <option value="confidence">Сначала неуверенные</option>
          </select>
        ) : null}
        <span className="toolbar__spacer" />
        <span className="muted num">{shown.length === rows.length ? `Страниц: ${formatInt(rows.length)}` : `Показано ${formatInt(shown.length)} из ${formatInt(rows.length)}`}</span>
      </div>

      <p className="pgtab__keys muted">
        Щелчок — выбрать, <kbd>Shift</kbd>+щелчок — диапазон, двойной щелчок или <kbd>Enter</kbd> — крупно · <kbd>←</kbd>
        <kbd>→</kbd>
        <kbd>↑</kbd>
        <kbd>↓</kbd> — переход, <kbd>Пробел</kbd> — выбрать, <kbd>A</kbd> — все показанные, <kbd>Esc</kbd> — снять
        {canLabel ? (
          <>
            {' '}
            · <kbd>T</kbd> — тип, <kbd>1</kbd> / <kbd>0</kbd> — первая / не первая, <kbd>R</kbd> — повторить последнюю правку
          </>
        ) : null}
      </p>

      {noSource ? (
        <p className="note" style={{ marginBottom: 12 }}>
          Источник меток для этой проверки не записан: её обработали до того, как сервис стал хранить историю меток.
          Типы здесь поставил Qwen; после повторного запуска появятся уверенность классификатора и перепроверки.
        </p>
      ) : null}

      {!shown.length ? (
        <Empty title="Под фильтр не попала ни одна страница">
          <button type="button" className="btn btn--small" onClick={() => setFilter(EMPTY_FILTER)}>
            Сбросить фильтры
          </button>
        </Empty>
      ) : (
        <div
          className="pggrid"
          ref={grid}
          role="listbox"
          aria-multiselectable
          aria-label="Страницы проверки"
          tabIndex={0}
          onFocus={() => focus < 0 && setFocus(0)}
        >
          {visible.map((r, i) => (
            <PageCard
              key={r.page_pk}
              row={r}
              idx={i}
              selected={selected.has(r.page_pk)}
              focused={i === focus}
              onClick={onCardClick}
              onOpen={setPreview}
              cardRef={(el) => {
                if (el) cards.current.set(i, el)
                else cards.current.delete(i)
              }}
            />
          ))}
        </div>
      )}
      {shown.length > limit ? <MoreSentinel onMore={() => setLimit((l) => l + CHUNK)} left={shown.length - limit} /> : null}

      {canLabel && nTargets ? (
        <div className="pgbar" role="region" aria-label="Правка выбранных страниц">
          <strong className="num">{selected.size ? `Выбрано: ${selected.size}` : '1 страница под курсором'}</strong>
          <ClassSelect taxonomy={taxonomy} value={bar.cls} onChange={(v) => setBar({ ...bar, cls: v })} keepLabel="Тип: не менять" selectRef={clsRef} small />
          <select className="select select--small" value={bar.first} onChange={(e) => setBar({ ...bar, first: e.target.value as '' | 'yes' | 'no' })} aria-label="Первая страница">
            <option value="">Первая: не менять</option>
            <option value="yes">Первая: да</option>
            <option value="no">Первая: нет</option>
          </select>
          <SectionSelect taxonomy={taxonomy} value={bar.section} onChange={(v) => setBar({ ...bar, section: v })} keepLabel="Раздел: не менять" small />
          <button type="button" className="btn btn--small btn--primary" disabled={busy || isEmptyPatch(barPatch())} onClick={applyBar}>
            {busy ? 'Сохраняем…' : 'Применить'}
          </button>
          {selected.size ? (
            <button type="button" className="btn btn--small btn--ghost" onClick={() => setSelected(new Set())}>
              Снять выбор <kbd>Esc</kbd>
            </button>
          ) : null}
          {lastPatch ? (
            <button type="button" className="btn btn--small btn--ghost" disabled={busy} onClick={() => apply(lastPatch, targets())} title="Повторить последнюю правку для выбранных">
              Повторить <kbd>R</kbd>
            </button>
          ) : null}
        </div>
      ) : null}

      {preview !== null && shown[preview] ? (
        <PagePreview
          rows={shown}
          index={preview}
          onIndex={(i) => {
            setPreview(i)
            setFocus(i)
            if (i >= limit) setLimit((l) => l + CHUNK)
          }}
          onClose={() => setPreview(null)}
          taxonomy={taxonomy}
          canLabel={canLabel}
          busy={busy}
          onSave={(row, patch) => apply(patch, [row])}
        />
      ) : null}
    </section>
  )
}

function MoreSentinel({ onMore, left }: { onMore: () => void; left: number }) {
  const ref = useRef<HTMLDivElement>(null)
  const cb = useRef(onMore)
  cb.current = onMore
  useEffect(() => {
    const el = ref.current
    if (!el) return
    const io = new IntersectionObserver((es) => es.some((e) => e.isIntersecting) && cb.current(), { rootMargin: '600px' })
    io.observe(el)
    return () => io.disconnect()
  }, [left])
  return (
    <div ref={ref} className="pgmore">
      <button type="button" className="btn btn--small" onClick={onMore}>
        Показать ещё ({formatInt(left)})
      </button>
    </div>
  )
}

