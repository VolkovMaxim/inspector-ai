// Рамка доказательства: bbox = [x0, y0, x1, y1] в долях видимой страницы (после Rotate).

import type { BBox } from '../api/types'

/** Проверить и привести рамку к виду x0<x1, y0<y1 в пределах [0;1]; мусор → null. */
export function normalizeBbox(b: unknown): BBox | null {
  if (!Array.isArray(b) || b.length !== 4) return null
  const n = b.map(Number)
  if (n.some((v) => !Number.isFinite(v))) return null
  const clamp = (v: number) => Math.min(1, Math.max(0, v))
  const x0 = clamp(Math.min(n[0], n[2]))
  const x1 = clamp(Math.max(n[0], n[2]))
  const y0 = clamp(Math.min(n[1], n[3]))
  const y1 = clamp(Math.max(n[1], n[3]))
  if (x1 - x0 <= 0 || y1 - y0 <= 0) return null
  return [x0, y0, x1, y1]
}

/** CSS-позиция рамки в процентах от размеров картинки страницы. */
export function bboxStyle(b: BBox): { left: string; top: string; width: string; height: string } {
  const pct = (v: number) => `${+(v * 100).toFixed(4)}%`
  return { left: pct(b[0]), top: pct(b[1]), width: pct(b[2] - b[0]), height: pct(b[3] - b[1]) }
}

export interface ZoomInput {
  bbox: BBox
  /** ширина листа при масштабе «по ширине», px */
  fitWidth: number
  /** высота / ширина страницы */
  aspect: number
  viewportWidth: number
  viewportHeight: number
}

export const ZOOM_MIN = 1
export const ZOOM_MAX = 6

/**
 * Масштаб (множитель к «по ширине»), при котором значение в рамке читается: высота рамки ≈ 20 px,
 * а сама рамка занимает не больше 70 % окна по ширине и 50 % по высоте.
 */
export function zoomForBbox({ bbox, fitWidth, aspect, viewportWidth, viewportHeight }: ZoomInput, targetPx = 20): number {
  if (fitWidth <= 0 || aspect <= 0) return ZOOM_MIN
  const hFrac = bbox[3] - bbox[1]
  const wFrac = bbox[2] - bbox[0]
  const byHeight = targetPx / (hFrac * fitWidth * aspect)
  const byWidth = (0.7 * viewportWidth) / (wFrac * fitWidth)
  const byViewportH = (0.5 * viewportHeight) / (hFrac * fitWidth * aspect)
  const z = Math.min(byHeight, byWidth, byViewportH)
  return roundZoom(Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, z)))
}

export function roundZoom(z: number): number {
  return Math.round(z * 20) / 20
}

/** Прокрутка, при которой центр рамки оказывается в центре окна. */
export function scrollForBbox(
  bbox: BBox,
  sheetWidth: number,
  sheetHeight: number,
  viewportWidth: number,
  viewportHeight: number,
): { left: number; top: number } {
  const cx = ((bbox[0] + bbox[2]) / 2) * sheetWidth
  const cy = ((bbox[1] + bbox[3]) / 2) * sheetHeight
  return {
    left: Math.max(0, Math.round(cx - viewportWidth / 2)),
    top: Math.max(0, Math.round(cy - viewportHeight / 2)),
  }
}

export const IMAGE_WIDTHS = [1400, 2200, 3000] as const

/** Ширина PNG с сервера (200..3000): ступенями, чтобы кэш рендеров на сервере переиспользовался. */
export function imageWidthFor(displayWidth: number, dpr = 1): number {
  const need = displayWidth * Math.max(1, dpr)
  for (const w of IMAGE_WIDTHS) if (need <= w) return w
  return IMAGE_WIDTHS[IMAGE_WIDTHS.length - 1]
}

export const ZOOM_STEPS = [1, 1.25, 1.5, 2, 2.5, 3, 4, 5, 6]

export function stepZoom(current: number, dir: 1 | -1): number {
  if (dir > 0) return ZOOM_STEPS.find((z) => z > current + 0.001) ?? ZOOM_MAX
  const lower = [...ZOOM_STEPS].reverse().find((z) => z < current - 0.001)
  return lower ?? ZOOM_MIN
}
