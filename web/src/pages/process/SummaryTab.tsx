import type { Protocol, UploadStatusRow } from '../../api/types'
import { StatusBadge, UploadStatusBadge } from '../../components/Badges'
import { Copyable } from '../../components/Feedback'
import { Stamp } from '../../components/Stamp'
import { summaryRows } from '../../lib/findings'
import { formatDateTime, formatInt, percent, percentValue, shortHash } from '../../lib/format'
import { FINDING_STATUS_LABEL, SCENARIO_LABEL, STAGE_LONG, label } from '../../lib/labels'

export function UploadStatusTable({ rows }: { rows: UploadStatusRow[] }) {
  return (
    <div className="tablewrap">
      <table className="table">
        <thead>
          <tr>
            <th>Стадия</th>
            <th>Статус загрузки</th>
            <th className="col-num">Файлов</th>
            <th className="col-num" title="Файлы, которые не читаются">С ошибкой</th>
            <th className="col-num">Страниц</th>
            <th className="col-num" title="Страниц, распознанных OCR">OCR</th>
            <th>Комментарий</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.stage}>
              <td title={STAGE_LONG[r.stage]}>
                <strong>{r.stage === 'PD' ? 'ПД' : r.stage === 'RD' ? 'РД' : 'ИД'}</strong>
              </td>
              <td>
                <UploadStatusBadge status={r.status} />
              </td>
              <td className="col-num">{formatInt(r.files)}</td>
              <td className="col-num">{r.problem_files ? <strong style={{ color: 'var(--red-fg)' }}>{r.problem_files}</strong> : 0}</td>
              <td className="col-num">{formatInt(r.pages)}</td>
              <td className="col-num">{formatInt(r.ocr_pages)}</td>
              <td>{r.comment}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

export function SummaryTab({ protocol }: { protocol: Protocol }) {
  const s = protocol.summary ?? {}
  const total = s.params_total ?? protocol.params.length
  const rows = summaryRows(s, FINDING_STATUS_LABEL)
  const result = rows.filter((r) => r.group === 'result')
  const completeness = rows.filter((r) => r.group === 'completeness')
  const suspicions = s.SUSPICION ?? protocol.findings.filter((f) => f.status === 'SUSPICION').length
  const t = protocol.traceability

  const statRow = (r: (typeof rows)[number]) => (
    <tr key={r.key}>
      <td>
        <StatusBadge status={r.key} />
      </td>
      <td className="col-num">{formatInt(r.count)}</td>
      <td className="col-num">{percent(r.count, total)}</td>
      <td className="col-bar">
        <div className="bar" aria-hidden>
          <div className={`bar__fill bar__fill--${r.key}`} style={{ width: `${percentValue(r.count, total)}%` }} />
        </div>
      </td>
    </tr>
  )

  return (
    <div className="twocol">
      <div>
        <section className="section">
          <div className="section__head">
            <h2>Статус загрузки по стадиям</h2>
            <p>Сценарий: {label(SCENARIO_LABEL, protocol.scenario)}</p>
          </div>
          <UploadStatusTable rows={protocol.upload_status} />
        </section>

        <section className="section">
          <div className="section__head">
            <h2>Сводная статистика</h2>
            <p>Доля — от {formatInt(total)} параметров матрицы контроля</p>
          </div>
          <div className="tablewrap">
            <table className="table stats">
              <thead>
                <tr>
                  <th>Статус параметра</th>
                  <th className="col-num">Параметров</th>
                  <th className="col-num">Доля</th>
                  <th aria-label="Диаграмма" />
                </tr>
              </thead>
              <tbody>
                <tr className="total">
                  <td>Всего параметров в матрице</td>
                  <td className="col-num">{formatInt(total)}</td>
                  <td className="col-num">100 %</td>
                  <td />
                </tr>
                <tr className="total">
                  <td>Сопоставлено (есть вывод по значениям)</td>
                  <td className="col-num">{formatInt(s.compared ?? 0)}</td>
                  <td className="col-num">{percent(s.compared ?? 0, total)}</td>
                  <td className="col-bar">
                    <div className="bar" aria-hidden>
                      <div className="bar__fill" style={{ width: `${percentValue(s.compared ?? 0, total)}%` }} />
                    </div>
                  </td>
                </tr>
                <tr className="group">
                  <td colSpan={4}>Результат сверки</td>
                </tr>
                {result.map(statRow)}
                <tr className="group">
                  <td colSpan={4}>Комплектность — отдельно от кандидатов</td>
                </tr>
                {completeness.map(statRow)}
              </tbody>
            </table>
          </div>
          <div className="hypo-note">
            <StatusBadge status="SUSPICION" />
            <span>
              Гипотез свободного поиска: <strong>{formatInt(suspicions)}</strong>. Они не входят в число нарушений и в
              статистику параметров.
            </span>
          </div>
        </section>
      </div>

      <div>
        <section className="section">
          <div className="section__head">
            <h2>Паспорт протокола</h2>
            <p>Сформирован {formatDateTime(protocol.generated_at)}</p>
          </div>
          <Stamp
            className="stamp--wrap"
            columns="1.4fr 0.6fr 1fr"
            cells={[
              { label: 'Протокол', value: protocol.protocol_id, mono: true, span: 2 },
              { label: 'Версия', value: protocol.version ? `v${protocol.version}` : 'живой', mono: true },
              { label: 'Объект', value: protocol.object.name, span: 2 },
              { label: 'Код объекта', value: protocol.object.object_id, mono: true },
              { label: 'Адрес', value: protocol.object.address, span: 3 },
              { label: 'Заказчик', value: protocol.object.customer, span: 2 },
              { label: 'Разрешение', value: protocol.object.permit_number, mono: true },
              { label: 'Подрядчик', value: protocol.object.contractor, span: 3 },
              { label: 'Матрица', value: t.matrix_version, mono: true, span: 2 },
              { label: 'Набор GOLD', value: t.dataset_version, mono: true },
              { label: 'Модели и промпты', value: t.model_version, mono: true, span: 3 },
              { label: 'Проверка', value: <Copyable value={t.process_id} />, span: 2 },
              { label: 'Прогон', value: formatDateTime(t.run_timestamp) },
              {
                label: 'SHA-256 манифеста входных файлов',
                value: <Copyable value={t.input_manifest_sha256} display={shortHash(t.input_manifest_sha256, 32)} />,
                span: 3,
              },
            ]}
          />
        </section>

        {protocol.low_quality_pages.length ? (
          <section className="section">
            <div className="section__head">
              <h2>Страницы низкого качества</h2>
              <p>{protocol.low_quality_pages.length}</p>
            </div>
            <div className="tablewrap">
              <table className="table table--compact">
                <thead>
                  <tr>
                    <th>Файл</th>
                    <th className="col-num">Страница</th>
                    <th>Причина</th>
                  </tr>
                </thead>
                <tbody>
                  {protocol.low_quality_pages.slice(0, 200).map((p) => (
                    <tr key={`${p.file_id}-${p.page}`}>
                      <td className="mono">{p.file_id}</td>
                      <td className="col-num">{p.page}</td>
                      <td>{p.reason}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>
        ) : null}
      </div>
    </div>
  )
}
