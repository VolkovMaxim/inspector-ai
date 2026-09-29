import type { FindingCard, Stage } from '../../api/types'
import { DecisionBadge, PriorityMark, StageTag, StatusBadge } from '../../components/Badges'
import { Copyable } from '../../components/Feedback'
import { Stamp } from '../../components/Stamp'
import { formatDateTime, shortHash } from '../../lib/format'
import { APPROVAL_LABEL, BBOX_SOURCE_LABEL, DECISION_LABEL, DISCOVERY_LABEL, FINDING_STATUS_LABEL, REASON_LABEL, VERIFY_ANSWER, label } from '../../lib/labels'

interface Props {
  card: FindingCard
  onFocus: (stage: Stage, sourceIndex?: number) => void
}

const VALUE_ROWS: { stage: Stage; title: string; sub: string }[] = [
  { stage: 'PD', title: 'ПД', sub: 'ожидаемое' },
  { stage: 'RD', title: 'РД', sub: 'фактическое' },
  { stage: 'ID', title: 'ИД', sub: 'фактическое' },
]

export function FindingCardView({ card, onFocus }: Props) {
  const valueOf = (stage: Stage): string | null =>
    stage === 'PD' ? card.expected_value : (card.actual_values[stage as 'RD' | 'ID'] ?? null)
  const hasSource = (stage: Stage) => card.sources.some((s) => s.stage === stage && s.page)
  const v = card.verification
  const history = card.history ?? []
  const decided = card.inspector.status && card.inspector.status !== 'PENDING'

  return (
    <article className="card">
      <div className="card__status">
        <StatusBadge status={card.status} />
        <PriorityMark value={card.review_priority} />
        {card.machine_status !== card.status ? (
          <span className="muted" style={{ fontSize: 12 }} title={card.machine_status}>
            машинный вывод: {label({ CANDIDATE: 'кандидат' }, card.machine_status)}
          </span>
        ) : null}
      </div>

      <header>
        <div className="card__code">
          <span className="mono">{card.code}</span>
          <span>Раздел {card.section || '—'}</span>
          {card.source !== 'matrix' ? <span>свободный поиск</span> : null}
        </div>
        <h2 className="card__title">{card.parameter_name}</h2>
        <p className="card__loc">
          <span>Локация: </span>
          {card.location || '—'}
        </p>
      </header>

      <table className="values">
        <tbody>
          {VALUE_ROWS.map((r) => {
            const val = valueOf(r.stage)
            const link = hasSource(r.stage)
            return (
              <tr
                key={r.stage}
                className={link ? 'is-link' : undefined}
                onClick={link ? () => onFocus(r.stage) : undefined}
                title={link ? 'Показать доказательство в панели' : undefined}
              >
                <th scope="row">
                  <b>{r.title}</b>
                  {r.sub}
                </th>
                <td className={val ? undefined : 'is-empty'}>{val ?? 'нет значения'}</td>
              </tr>
            )
          })}
        </tbody>
      </table>

      {card.delta ? (
        <div className="card__block">
          <h3>Отклонение</h3>
          <p className="card__delta">{card.delta}</p>
        </div>
      ) : null}

      <div className="card__block">
        <h3>Критичность</h3>
        <p>{card.criticality || '—'}</p>
      </div>

      {card.rationale ? (
        <div className="card__block">
          <h3>Обоснование</h3>
          <p>{card.rationale}</p>
        </div>
      ) : null}

      {v ? (
        <div className="card__block">
          <h3>Верификатор</h3>
          <dl className="verif">
            <dt>Тот же элемент</dt>
            <dd>{label(VERIFY_ANSWER, v.same_entity)}</dd>
            <dt>Условие нарушения</dt>
            <dd>{label(VERIFY_ANSWER, v.trigger_met)}</dd>
            {v.analysis ? (
              <>
                <dt>Анализ</dt>
                <dd>{v.analysis}</dd>
              </>
            ) : null}
          </dl>
        </div>
      ) : null}

      {card.discovery_method ? (
        <div className="card__block">
          <h3>Как найдено</h3>
          <p>{label(DISCOVERY_LABEL, card.discovery_method)}</p>
        </div>
      ) : null}

      <div className="card__block">
        <h3>Источники</h3>
        <div className="sources">
          {card.sources.length === 0 ? <p className="muted">Страниц-доказательств нет.</p> : null}
          {card.sources.map((s, i) => {
            const idx = card.sources.filter((x, j) => j < i && x.stage === s.stage && x.page).length
            return (
              <div
                key={`${s.role}-${s.file_pk}-${s.page}-${i}`}
                className="source"
                role="button"
                tabIndex={0}
                onClick={() => onFocus(s.stage, idx)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' || e.key === ' ') {
                    e.preventDefault()
                    onFocus(s.stage, idx)
                  }
                }}
              >
                <div className="source__head">
                  <StageTag stage={s.stage} />
                  <span>
                    {s.value ? <span className="mono">{s.value}</span> : null}
                    {s.value ? ', ' : ''}стр. {s.page}
                  </span>
                  <span className="muted">{label(BBOX_SOURCE_LABEL, s.bbox_source, '')}</span>
                </div>
                <Stamp
                  dense
                  className="stamp--wrap"
                  columns="minmax(0,1.5fr) minmax(0,0.5fr) minmax(0,0.6fr)"
                  cells={[
                    { label: 'Файл', value: <><span className="mono">{s.file_id}</span> {s.file_name}</>, span: 3 },
                    { label: 'Шифр', value: s.document_code, mono: true },
                    { label: 'Изм.', value: s.revision, mono: true },
                    { label: 'Лист', value: s.sheet, mono: true },
                    { label: 'Утверждение', value: label(APPROVAL_LABEL, s.approval_status, 'не указан'), muted: !s.approval_status || s.approval_status === 'UNKNOWN' },
                    {
                      label: 'SHA-256',
                      value: s.sha256 ? <Copyable value={s.sha256} display={shortHash(s.sha256, 16)} /> : '—',
                      span: 2,
                    },
                  ]}
                />
                {s.snippet ? (
                  <p className="muted" style={{ marginTop: 4, fontSize: 12 }}>
                    «{s.snippet}»
                  </p>
                ) : null}
              </div>
            )
          })}
        </div>
      </div>

      <div className="card__block">
        <h3>Решение инспектора</h3>
        {decided ? (
          <p>
            <DecisionBadge status={card.inspector.status} /> {card.inspector.user ?? ''}, {formatDateTime(card.inspector.decided_at)}
            {card.inspector.reason_code ? <><br />Причина: {label(REASON_LABEL, card.inspector.reason_code)}</> : null}
            {card.inspector.comment ? <><br />«{card.inspector.comment}»</> : null}
          </p>
        ) : card.machine_status === 'CANDIDATE' ? (
          <p>
            <DecisionBadge status="PENDING" />
          </p>
        ) : (
          <p className="muted">Не требуется для этого статуса.</p>
        )}
        {history.length ? (
          <ol className="history" aria-label="История решений">
            {[...history].reverse().map((h, i) => (
              <li key={`${h.created_at}-${i}`}>
                {label(DECISION_LABEL, h.decision)}
                {h.reason_code ? `: ${label(REASON_LABEL, h.reason_code)}` : ''}
                {h.comment ? <span className="muted"> «{h.comment}»</span> : null}
                <div className="history__meta">
                  {h.user ?? 'система'}, {formatDateTime(h.created_at)}; было: {label(FINDING_STATUS_LABEL, h.previous_status).toLowerCase()}
                </div>
              </li>
            ))}
          </ol>
        ) : null}
      </div>

      <div className="card__block">
        <h3>Трассируемость</h3>
        <Stamp
          dense
          className="stamp--wrap"
          columns="1fr 1fr"
          cells={[
            { label: 'Карточка', value: card.finding_id, mono: true, span: 2 },
            { label: 'Правило', value: card.rule_version, mono: true },
            { label: 'Появилась в версии', value: `v${card.created_in_version}`, mono: true },
            {
              label: 'Машинная метка сабмита',
              value: card.submission.include ? card.submission.violation_label : 'не выгружается',
              mono: card.submission.include,
              title: 'Без учёта решения инспектора; в выгрузку идёт метка по решению',
            },
            { label: 'Машинный статус', value: card.submission.protocol_status ?? '—', mono: true },
          ]}
        />
      </div>
    </article>
  )
}
