// Страница документа как PNG с сервера и рамка доказательства поверх неё.
// bbox = [x0, y0, x1, y1] в долях видимой страницы → абсолютный div в процентах от картинки.
import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { errorText } from '../api/client'
import type { BBox } from '../api/types'
import { bboxStyle, imageWidthFor, scrollForBbox, zoomForBbox } from '../lib/bbox'
import { pageImageUrl } from '../lib/pageImages'
import { Icon } from './Icon'

const PAD = 12

export interface ViewerFocus {
  /** растёт на каждый запрос «к доказательству» */
  seq: number
  /** подобрать масштаб, при котором значение в рамке читается */
  autoZoom: boolean
}

interface Props {
  filePk: number
  page: number
  /** рамка — только на странице доказательства */
  bbox: BBox | null
  /** доказательство на этой странице, но место не определено: тонкая рамка по всей странице */
  wholePage: boolean
  zoom: number
  onZoom: (z: number) => void
  focus: ViewerFocus
  evidencePage: number | null
  onGoEvidence: () => void
}

function prefersReducedMotion(): boolean {
  return typeof window !== 'undefined' && !!window.matchMedia?.('(prefers-reduced-motion: reduce)').matches
}

export function PageViewer({ filePk, page, bbox, wholePage, zoom, onZoom, focus, evidencePage, onGoEvidence }: Props) {
  const viewport = useRef<HTMLDivElement>(null)
  const [size, setSize] = useState({ w: 0, h: 0 })

  useLayoutEffect(() => {
    const el = viewport.current
    if (!el) return
    const measure = () => setSize({ w: el.clientWidth, h: el.clientHeight })
    measure()
    const ro = new ResizeObserver(measure)
    ro.observe(el)
    return () => ro.disconnect()
  }, [])

  const pageKey = `${filePk}/${page}`
  const fitWidth = Math.max(120, size.w - PAD * 2)
  const sheetW = Math.round(fitWidth * zoom)
  const [aspect, setAspect] = useState<{ key: string; value: number } | null>(null)
  const asp = aspect?.key === pageKey ? aspect.value : null

  // картинка: ступень ширины по размеру на экране, без понижения качества при уменьшении
  const dpr = typeof window !== 'undefined' ? window.devicePixelRatio || 1 : 1
  const bucket = imageWidthFor(sheetW, dpr)
  const [img, setImg] = useState<{ key: string; url: string; width: number } | null>(null)
  const [error, setError] = useState<{ key: string; message: string } | null>(null)
  const [retry, setRetry] = useState(0)
  const shown = img && img.key === pageKey ? img : null
  const want = shown ? Math.max(bucket, shown.width) : bucket
  const measured = size.w > 0

  useEffect(() => {
    if (!measured) return
    if (img && img.key === pageKey && img.width >= want) return
    let alive = true
    setError(null)
    pageImageUrl(filePk, page, want)
      .then((url) => {
        if (alive) setImg({ key: pageKey, url, width: want })
      })
      .catch((e) => {
        if (alive) setError({ key: pageKey, message: errorText(e) })
      })
    return () => {
      alive = false
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filePk, page, want, measured, retry])

  // 1) ручное изменение масштаба — держим центр окна на месте
  const prevSheet = useRef<{ w: number; key: string } | null>(null)
  const pending = useRef<{ auto: boolean } | null>(null)
  useLayoutEffect(() => {
    const el = viewport.current
    const prev = prevSheet.current
    if (el && prev && prev.key === pageKey && prev.w !== sheetW && !pending.current) {
      const ratio = sheetW / prev.w
      const cx = el.scrollLeft + el.clientWidth / 2 - PAD
      const cy = el.scrollTop + el.clientHeight / 2 - PAD
      el.scrollLeft = cx * ratio + PAD - el.clientWidth / 2
      el.scrollTop = cy * ratio + PAD - el.clientHeight / 2
    }
    prevSheet.current = { w: sheetW, key: pageKey }
  }, [sheetW, pageKey])

  // листание: новая страница — с верха (горизонтальная прокрутка остаётся)
  const prevPageKey = useRef(pageKey)
  useLayoutEffect(() => {
    if (prevPageKey.current !== pageKey && viewport.current) viewport.current.scrollTop = 0
    prevPageKey.current = pageKey
  }, [pageKey])

  // 2) «к доказательству»: подобрать масштаб и прокрутить к рамке, когда страница загружена и измерена
  const handled = useRef(0)
  const [flash, setFlash] = useState(0)
  useLayoutEffect(() => {
    if (focus.seq !== handled.current) {
      handled.current = focus.seq
      pending.current = { auto: focus.autoZoom }
    }
    const pf = pending.current
    const el = viewport.current
    if (!pf || !el || !measured || !asp || !shown) return
    if (!bbox) {
      if (pf.auto && Math.abs(zoom - 1) > 0.01) {
        pending.current = { auto: false }
        onZoom(1)
        return
      }
      pending.current = null
      el.scrollTo({ left: 0, top: 0 })
      return
    }
    if (pf.auto) {
      const target = zoomForBbox({ bbox, fitWidth, aspect: asp, viewportWidth: size.w, viewportHeight: size.h })
      pending.current = { auto: false }
      if (Math.abs(target - zoom) > 0.01) {
        onZoom(target)
        return
      }
    }
    pending.current = null
    const { left, top } = scrollForBbox(bbox, sheetW, sheetW * asp, size.w, size.h)
    el.scrollTo({ left: left + PAD, top: top + PAD, behavior: prefersReducedMotion() ? 'auto' : 'smooth' })
    setFlash((f) => f + 1)
  })

  const sheetStyle = { width: sheetW }
  const err = error && error.key === pageKey ? error.message : null

  return (
    <div className="viewerwrap">
      {evidencePage && evidencePage !== page ? (
        <div className="viewer__elsewhere">
          <button type="button" onClick={onGoEvidence}>
            Доказательство на стр. {evidencePage}
          </button>
        </div>
      ) : null}
      <div className="viewer" ref={viewport} tabIndex={0} aria-label={`Страница ${page}`}>
        <div className="viewer__pad" style={{ width: sheetW + PAD * 2 }}>
          <div className="viewer__sheet" style={sheetStyle}>
            {shown ? (
              <>
                <img
                  src={shown.url}
                  alt={`Страница ${page}`}
                  draggable={false}
                  onLoad={(e) => {
                    const t = e.currentTarget
                    if (t.naturalWidth) setAspect({ key: pageKey, value: t.naturalHeight / t.naturalWidth })
                  }}
                />
                {bbox ? <div key={flash} className="viewer__bbox" style={bboxStyle(bbox)} aria-label="Рамка доказательства" /> : null}
                {!bbox && wholePage ? <div className="viewer__pageframe" aria-label="Доказательство — вся страница" /> : null}
              </>
            ) : err ? (
              <div className="viewer__ph viewer__ph--error">
                <Icon name="alert" />
                <span>Страница не загрузилась: {err}</span>
                <button type="button" className="btn btn--small" onClick={() => setRetry((r) => r + 1)}>
                  Повторить
                </button>
              </div>
            ) : (
              <div className="viewer__ph">Загрузка страницы {page}…</div>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}
