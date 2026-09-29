// Прогресс конвейера по этапам: это последовательность, поэтому этапы пронумерованы.
// Пока идёт обработка (или она упала) — сетка этапов открыта; после — одна строка с итогом и кнопкой «показать».
import { useState } from 'react'
import type { ProcessOut, StageProgress as StageProgressT } from '../api/types'
import { formatInt, formatSeconds } from '../lib/format'
import { PIPELINE_STAGES, STAGE_RUN_LABEL, label } from '../lib/labels'

export type StageKind = 'done' | 'running' | 'error' | 'skipped' | 'pending'

export function stageKind(p: StageProgressT | undefined, isCurrent: boolean, processStatus: string): StageKind {
  const s = (p?.status ?? '').toLowerCase()
  if (s === 'error' || s === 'failed') return 'error'
  if (s === 'done' || s === 'ok' || s === 'finished' || s === 'completed') return 'done'
  if (s === 'skipped') return 'skipped'
  if (s === 'running' || (isCurrent && processStatus === 'PARSING')) return 'running'
  if (isCurrent && processStatus === 'FAILED') return 'error'
  return 'pending'
}

export function StageProgress({ process }: { process: ProcessOut }) {
  const progress = process.progress ?? {}
  const totalSeconds = Object.values(progress).reduce((s, p) => s + (p?.seconds ?? 0), 0)
  const active = process.status === 'PARSING' || process.status === 'FAILED'
  const [open, setOpen] = useState(false)
  const kinds = PIPELINE_STAGES.map((st) => stageKind(progress[st.key], process.stage === st.key, process.status))
  const done = kinds.filter((k) => k === 'done' || k === 'skipped').length

  if (!active && !open) {
    return (
      <p className="stagesline">
        Обработка: {done} из {PIPELINE_STAGES.length} этапов
        {totalSeconds ? `, ${formatSeconds(totalSeconds)}` : ''}.{' '}
        <button type="button" className="linkbtn" onClick={() => setOpen(true)}>
          Показать этапы
        </button>
      </p>
    )
  }

  return (
    <div>
      <div className="stages" role="list" aria-label="Этапы обработки">
        {PIPELINE_STAGES.map((st, i) => {
          const p = progress[st.key]
          const kind = kinds[i]
          const pct =
            kind === 'done' ? 100 : p?.total ? Math.min(100, Math.round(((p.done ?? 0) / p.total) * 100)) : kind === 'running' ? 8 : 0
          const state =
            kind === 'error'
              ? p?.error || 'ошибка'
              : p?.total
                ? `${formatInt(p.done ?? 0)} из ${formatInt(p.total)}`
                : label(STAGE_RUN_LABEL, p?.status ?? (kind === 'running' ? 'running' : 'pending'))
          return (
            <div key={st.key} className={`stg stg--${kind}`} role="listitem" title={`${st.label}: ${st.hint}`}>
              <div className="stg__name">
                <span className="stg__no">{i + 1}</span>
                {st.label}
              </div>
              <div className="stg__state">
                {state}
                {p?.seconds ? <span className="muted">, {formatSeconds(p.seconds)}</span> : null}
              </div>
              <div className="progress" aria-hidden>
                <div className="progress__fill" style={{ width: `${pct}%` }} />
              </div>
            </div>
          )
        })}
      </div>
      <p className="stagesline">
        {totalSeconds ? `Время обработки по этапам: ${formatSeconds(totalSeconds)}.` : null}{' '}
        {!active ? (
          <button type="button" className="linkbtn" onClick={() => setOpen(false)}>
            Свернуть
          </button>
        ) : null}
      </p>
    </div>
  )
}
