import { describe, expect, it } from 'vitest'
import {
  ZOOM_MAX,
  ZOOM_MIN,
  bboxStyle,
  imageWidthFor,
  normalizeBbox,
  scrollForBbox,
  stepZoom,
  zoomForBbox,
} from './bbox'

describe('normalizeBbox', () => {
  it('принимает корректную рамку как есть', () => {
    expect(normalizeBbox([0.412, 0.518, 0.468, 0.534])).toEqual([0.412, 0.518, 0.468, 0.534])
  })
  it('упорядочивает перевёрнутые углы и обрезает по [0;1]', () => {
    expect(normalizeBbox([0.5, 0.6, 0.2, -0.1])).toEqual([0.2, 0, 0.5, 0.6])
    expect(normalizeBbox([0.9, 0.9, 1.4, 1.2])).toEqual([0.9, 0.9, 1, 1])
  })
  it('мусор и нулевая площадь → null', () => {
    expect(normalizeBbox(null)).toBeNull()
    expect(normalizeBbox([0.1, 0.2, 0.3])).toBeNull()
    expect(normalizeBbox([0.1, 'x', 0.3, 0.4])).toBeNull()
    expect(normalizeBbox([0.1, 0.2, 0.1, 0.4])).toBeNull()
    expect(normalizeBbox([NaN, 0, 1, 1])).toBeNull()
  })
})

describe('bboxStyle', () => {
  it('переводит доли в проценты от картинки', () => {
    expect(bboxStyle([0.25, 0.5, 0.75, 0.625])).toEqual({ left: '25%', top: '50%', width: '50%', height: '12.5%' })
  })
})

describe('zoomForBbox', () => {
  const base = { fitWidth: 320, aspect: 1.414, viewportWidth: 320, viewportHeight: 480 }
  it('мелкая строка на A4 → увеличение, чтобы текст читался', () => {
    const z = zoomForBbox({ ...base, bbox: [0.412, 0.518, 0.468, 0.534] })
    expect(z).toBeGreaterThan(2)
    expect(z).toBeLessThanOrEqual(ZOOM_MAX)
  })
  it('крупная рамка → масштаб «по ширине»', () => {
    expect(zoomForBbox({ ...base, bbox: [0, 0, 1, 0.5] })).toBe(ZOOM_MIN)
  })
  it('крошечная рамка → не больше максимума', () => {
    expect(zoomForBbox({ ...base, bbox: [0.5, 0.5, 0.5001, 0.5001] })).toBe(ZOOM_MAX)
  })
  it('нулевые размеры окна не ломают расчёт', () => {
    expect(zoomForBbox({ ...base, fitWidth: 0, bbox: [0.1, 0.1, 0.2, 0.2] })).toBe(ZOOM_MIN)
  })
})

describe('scrollForBbox', () => {
  it('центрирует рамку в окне', () => {
    // центр: x = 0,5 × 1000 = 500, y = 0,51 × 1414 ≈ 721; минус половина окна 300 × 400
    expect(scrollForBbox([0.4, 0.5, 0.6, 0.52], 1000, 1414, 300, 400)).toEqual({ left: 350, top: 521 })
  })
  it('не уходит в отрицательную прокрутку у края листа', () => {
    expect(scrollForBbox([0, 0, 0.05, 0.02], 1000, 1414, 300, 400)).toEqual({ left: 0, top: 0 })
  })
})

describe('imageWidthFor', () => {
  it('ступени 1400 / 2200 / 3000 с учётом плотности пикселей', () => {
    expect(imageWidthFor(320, 1)).toBe(1400)
    expect(imageWidthFor(900, 2)).toBe(2200)
    expect(imageWidthFor(1920, 2)).toBe(3000)
    expect(imageWidthFor(5000, 1)).toBe(3000)
  })
})

describe('stepZoom', () => {
  it('шаги вверх и вниз с упором в границы', () => {
    expect(stepZoom(1, 1)).toBe(1.25)
    expect(stepZoom(1.3, 1)).toBe(1.5)
    expect(stepZoom(1.3, -1)).toBe(1.25)
    expect(stepZoom(ZOOM_MAX, 1)).toBe(ZOOM_MAX)
    expect(stepZoom(ZOOM_MIN, -1)).toBe(ZOOM_MIN)
  })
})
