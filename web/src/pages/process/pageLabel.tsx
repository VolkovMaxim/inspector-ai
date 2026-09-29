// Общее для правки типа страницы: список типов по группам, отметка источника метки, справочник с сервера.
import { useEffect, useState, type Ref } from 'react'
import { api } from '../../api/endpoints'
import type { PageRow, PageTaxonomy } from '../../api/types'
import { LABEL_SOURCE_LABEL, PAGE_CLASS_GROUP_LABEL, PAGE_CLASS_LABEL, label } from '../../lib/labels'
import { confidenceTone, pct } from '../../lib/pages'

let taxonomyPromise: Promise<PageTaxonomy> | null = null

/** Справочник типов и разделов: один запрос на вкладку браузера, ошибка — повтор при следующем вызове. */
export function useTaxonomy(): PageTaxonomy | null {
  const [tax, setTax] = useState<PageTaxonomy | null>(null)
  useEffect(() => {
    let alive = true
    taxonomyPromise ??= api.pageTaxonomy().catch((e) => {
      taxonomyPromise = null
      throw e
    })
    taxonomyPromise.then((t) => alive && setTax(t)).catch(() => undefined)
    return () => {
      alive = false
    }
  }, [])
  return tax
}

export function classLabel(code: string | null | undefined): string {
  return label(PAGE_CLASS_LABEL, code, 'не определён')
}

export function ClassSelect({
  taxonomy,
  value,
  onChange,
  keepLabel,
  selectRef,
  small,
  ariaLabel = 'Тип страницы',
}: {
  taxonomy: PageTaxonomy | null
  value: string
  onChange: (v: string) => void
  /** пустое значение — «не менять» (массовая правка) */
  keepLabel?: string
  selectRef?: Ref<HTMLSelectElement>
  small?: boolean
  ariaLabel?: string
}) {
  const groups = ['sheet', 'id', 'other'] as const
  return (
    <select
      ref={selectRef}
      className={`select${small ? ' select--small' : ''}`}
      value={value}
      onChange={(e) => onChange(e.target.value)}
      aria-label={ariaLabel}
      disabled={!taxonomy}
    >
      {keepLabel !== undefined ? <option value="">{keepLabel}</option> : null}
      {groups.map((g) => (
        <optgroup key={g} label={PAGE_CLASS_GROUP_LABEL[g]}>
          {(taxonomy?.classes ?? [])
            .filter((c) => c.group === g)
            .map((c) => (
              <option key={c.code} value={c.code} title={c.description}>
                {classLabel(c.code)}
              </option>
            ))}
        </optgroup>
      ))}
    </select>
  )
}

export function SectionSelect({
  taxonomy,
  value,
  onChange,
  keepLabel,
  small,
}: {
  taxonomy: PageTaxonomy | null
  value: string
  onChange: (v: string) => void
  keepLabel?: string
  small?: boolean
}) {
  return (
    <select className={`select${small ? ' select--small' : ''}`} value={value} onChange={(e) => onChange(e.target.value)} aria-label="Раздел" disabled={!taxonomy}>
      {keepLabel !== undefined ? <option value="">{keepLabel}</option> : null}
      {(taxonomy?.sections ?? []).map((s) => (
        <option key={s} value={s}>
          {s === 'НЕТ' ? 'нет раздела' : s}
        </option>
      ))}
    </select>
  )
}

/** Кто поставил метку и насколько уверен классификатор. */
export function SourceMark({ row }: { row: PageRow }) {
  const src = row.label_source ?? 'none'
  const tone = confidenceTone(row.model_confidence)
  const modelSaid = row.model_class && row.model_class !== row.page_class ? `классификатор: ${classLabel(row.model_class)}` : null
  const title = [
    label(LABEL_SOURCE_LABEL, src),
    row.model_confidence !== null ? `уверенность классификатора ${pct(row.model_confidence)}` : null,
    modelSaid,
    row.label_model,
  ]
    .filter(Boolean)
    .join(' · ')
  if (src === 'inspector') {
    return (
      <span className="srcmark srcmark--inspector" title={title}>
        ✓ исправлено
        {modelSaid ? <span className="srcmark__was">было: {classLabel(row.model_class)}</span> : null}
      </span>
    )
  }
  return (
    <span className={`srcmark srcmark--${src}`} title={title}>
      {src === 'qwen' ? 'Qwen' : src === 'model' ? 'классификатор' : 'источник —'}
      {row.model_confidence !== null ? <span className={`srcmark__conf srcmark__conf--${tone}`}>{pct(row.model_confidence)}</span> : null}
    </span>
  )
}
