import { downloadFile, errorText } from '../../api/client'
import { api } from '../../api/endpoints'
import { StageTag } from '../../components/Badges'
import { Copyable, Empty, ErrorBox, Loading } from '../../components/Feedback'
import { Icon } from '../../components/Icon'
import { useToast } from '../../components/Toasts'
import { formatBytes, formatInt, shortHash } from '../../lib/format'
import { useAsync } from '../../lib/hooks'
import { APPROVAL_LABEL, FILE_STATUS_LABEL, STAGE_SOURCE_LABEL, label } from '../../lib/labels'

export function DocumentsTab({ processId }: { processId: string }) {
  const toast = useToast()
  const files = useAsync((signal) => api.processFiles(processId, signal), [processId])
  const docs = useAsync((signal) => api.processDocuments(processId, signal), [processId])

  const download = async (filePk: number, name: string) => {
    try {
      await downloadFile(api.fileDownloadPath(filePk), name)
    } catch (e) {
      toast.error(`Не скачан: ${errorText(e)}`)
    }
  }

  return (
    <>
      <section className="section">
        <div className="section__head">
          <h2>Файлы пакета</h2>
          <p>Стадия, её источник и паспорт по штампу; SHA-256 — для трассируемости</p>
        </div>
        {files.error && !files.data ? <ErrorBox error={files.error} onRetry={() => files.reload()} /> : null}
        {files.loading && !files.data ? <Loading /> : null}
        {files.data && !files.data.length ? <Empty title="Файлов нет">Загрузите пакет через «Дозагрузить».</Empty> : null}
        {files.data?.length ? (
          <div className="tablewrap">
            <table className="table">
              <thead>
                <tr>
                  <th>Файл</th>
                  <th>Стадия</th>
                  <th>Состояние</th>
                  <th className="col-num">Страниц</th>
                  <th>Шифр</th>
                  <th>Изм.</th>
                  <th>Утверждение</th>
                  <th className="col-num">Партия</th>
                  <th>SHA-256</th>
                  <th aria-label="Скачать" />
                </tr>
              </thead>
              <tbody>
                {files.data.map((f) => (
                  <tr key={f.file_pk} className={f.status === 'ok' || f.status === 'pending' ? undefined : 'is-dim'}>
                    <td>
                      <span className="mono">{f.file_id}</span>
                      <span className="cell-sub cell-clip" title={f.rel_path || f.name}>
                        {f.rel_path || f.name}
                      </span>
                    </td>
                    <td>
                      <StageTag stage={f.stage} />
                      <span className="cell-sub">{label(STAGE_SOURCE_LABEL, f.stage_source, 'стадия не определена')}</span>
                    </td>
                    <td>
                      {label(FILE_STATUS_LABEL, f.status)}
                      {f.error ? (
                        <span className="cell-sub cell-clip" title={f.error} style={{ color: 'var(--red-fg)' }}>
                          {f.error}
                        </span>
                      ) : null}
                    </td>
                    <td className="col-num">
                      {formatInt(f.pages)}
                      {f.ocr_pages ? <span className="cell-sub">OCR: {f.ocr_pages}</span> : null}
                    </td>
                    <td className="mono">{f.document_code ?? '—'}</td>
                    <td className="mono">{f.revision ?? '—'}</td>
                    <td>{label(APPROVAL_LABEL, f.approval_status, 'не указан')}</td>
                    <td className="col-num">{f.batch}</td>
                    <td>
                      {f.sha256 ? <Copyable value={f.sha256} display={shortHash(f.sha256, 12)} /> : '—'}
                      <span className="cell-sub">{formatBytes(f.size)}</span>
                    </td>
                    <td className="col-tight">
                      <button type="button" className="iconbtn" onClick={() => download(f.file_pk, f.name)} aria-label={`Скачать ${f.name}`} title="Скачать исходный файл">
                        <Icon name="download" />
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : null}
      </section>

      <section className="section">
        <div className="section__head">
          <h2>Собранные документы</h2>
          <p>Документ — страницы от первой до следующей первой; актуальная редакция выбирается по изменению</p>
        </div>
        {docs.error && !docs.data ? <ErrorBox error={docs.error} onRetry={() => docs.reload()} /> : null}
        {docs.loading && !docs.data ? <Loading /> : null}
        {docs.data && !docs.data.length ? <Empty title="Документы ещё не собраны">Они появятся после этапа «Сборка документов».</Empty> : null}
        {docs.data?.length ? (
          <div className="tablewrap">
            <table className="table">
              <thead>
                <tr>
                  <th>Файл</th>
                  <th className="col-num">Страницы</th>
                  <th>Тип</th>
                  <th>Стадия</th>
                  <th>Раздел</th>
                  <th>Название</th>
                  <th>Шифр</th>
                  <th>Лист</th>
                  <th>Изм.</th>
                  <th>Утверждение</th>
                  <th>Редакция</th>
                </tr>
              </thead>
              <tbody>
                {docs.data.map((d) => (
                  <tr key={d.id} className={d.is_actual ? undefined : 'is-dim'}>
                    <td>
                      <span className="mono">{d.file_id}</span>
                      <span className="cell-sub cell-clip" title={d.file_name}>
                        {d.file_name}
                      </span>
                    </td>
                    <td className="col-num">{d.first_page === d.last_page ? d.first_page : `${d.first_page}–${d.last_page}`}</td>
                    <td>{d.doc_type}</td>
                    <td>
                      <StageTag stage={d.stage} />
                    </td>
                    <td>{d.section ?? '—'}</td>
                    <td className="cell-clip" title={d.title ?? ''}>
                      {d.title ?? '—'}
                    </td>
                    <td className="mono">{d.document_code ?? '—'}</td>
                    <td className="mono">{d.sheet ?? '—'}</td>
                    <td className="mono">{d.revision ?? '—'}</td>
                    <td>{label(APPROVAL_LABEL, d.approval_status, 'не указан')}</td>
                    <td>
                      {d.clarification ? (
                        <span className="badge badge--CLARIFICATION_REQUIRED" title={d.clarification_reason ?? ''}>
                          Уточнить редакцию
                        </span>
                      ) : d.is_actual ? (
                        'актуальная'
                      ) : (
                        <span className="muted">заменена</span>
                      )}
                      {d.clarification && d.clarification_reason ? <span className="cell-sub">{d.clarification_reason}</span> : null}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : null}
      </section>
    </>
  )
}
