// Методы API (все пути — с префиксом /api/v1, кроме /health).
import { asItems, request, uploadForm } from './client'
import type {
  AuditRow,
  BulkResult,
  DatasetItem,
  DatasetVersionRow,
  Decision,
  DecisionResult,
  DocumentRow,
  FileRow,
  FindingCard,
  Health,
  Items,
  LoginResponse,
  ModelVersionRow,
  NewProcessBody,
  PageInfo,
  PageClassifierStatus,
  PageRouterStatus,
  PageCorrectionIn,
  PageCorrectionResult,
  PageRow,
  PageTaxonomy,
  Param,
  ParamPatch,
  ProcessOut,
  Protocol,
  ProtocolVersionRow,
  RetrainLogRow,
  RetrainingReport,
  SyncInfo,
  UploadResult,
  UploadStage,
  User,
} from './types'

const seg = encodeURIComponent

type ListLike<T> = T[] | Items<T>

export const api = {
  // авторизация
  login: (login: string, password: string) =>
    request<LoginResponse>('POST', '/auth/login', { body: { login, password } }),
  me: (signal?: AbortSignal) => request<User>('GET', '/auth/me', { signal }),

  // проверки
  listProcesses: (
    q: { status?: string; q?: string; date_from?: string; date_to?: string; limit?: number; offset?: number },
    signal?: AbortSignal,
  ) =>
    request<Items<ProcessOut>>('GET', '/processes', {
      // границы дней — в поясе браузера (сервер хранит UTC)
      query: { ...q, tz_offset_min: q.date_from || q.date_to ? new Date().getTimezoneOffset() : undefined },
      signal,
    }),
  getProcess: (id: string, signal?: AbortSignal) => request<ProcessOut>('GET', `/processes/${seg(id)}`, { signal }),
  createProcess: (body: NewProcessBody) => request<ProcessOut>('POST', '/processes', { body }),
  startProcess: (id: string) => request<{ job_id: number }>('POST', `/processes/${seg(id)}/start`),
  importLocal: (id: string, path: string, stage: UploadStage) =>
    request<UploadResult>('POST', `/processes/${seg(id)}/import-local`, {
      body: { path, stage },
      timeoutMs: 30 * 60 * 1000,
    }),
  upload: (form: FormData, onProgress?: (loaded: number, total: number) => void, signal?: AbortSignal) =>
    uploadForm<UploadResult>('/documents/upload', form, onProgress, signal),

  // файлы и документы
  processFiles: async (id: string, signal?: AbortSignal) =>
    asItems(await request<ListLike<FileRow>>('GET', `/processes/${seg(id)}/files`, { signal })),
  processDocuments: async (id: string, signal?: AbortSignal) =>
    asItems(await request<ListLike<DocumentRow>>('GET', `/processes/${seg(id)}/documents`, { signal })),
  pageInfo: (filePk: number, page: number, signal?: AbortSignal) =>
    request<PageInfo>('GET', `/files/${filePk}/pages/${page}`, { signal }),
  pageImagePath: (filePk: number, page: number) => `/files/${filePk}/pages/${page}/image`,
  fileDownloadPath: (filePk: number) => `/files/${filePk}/download`,

  // типы страниц и правки разметчиков
  processPages: async (id: string, signal?: AbortSignal) => {
    const out: PageRow[] = []
    for (;;) {
      const r = await request<{ items: PageRow[]; total: number }>('GET', `/processes/${seg(id)}/pages`, {
        query: { limit: 5000, offset: out.length },
        signal,
        timeoutMs: 60_000,
      })
      out.push(...r.items)
      if (!r.items.length || out.length >= r.total) return out
    }
  },
  pageTaxonomy: (signal?: AbortSignal) => request<PageTaxonomy>('GET', '/ml/page-taxonomy', { signal }),
  correctPages: (id: string, items: PageCorrectionIn[], comment?: string) =>
    request<PageCorrectionResult>('POST', `/processes/${seg(id)}/pages/corrections`, { body: { items, comment } }),
  pageClassifier: (signal?: AbortSignal) => request<PageClassifierStatus>('GET', '/ml/page-classifier', { signal }),
  retrainPageClassifier: () => request<RetrainLogRow>('POST', '/ml/page-classifier/retrain'),
  approvePageClassifier: (version: string) =>
    request<{ current: string; rollback_to: string | null }>('POST', '/ml/page-classifier/approve', { body: { version } }),
  rejectPageClassifier: (version: string) =>
    request<{ current: string | null; rejected: string }>('POST', '/ml/page-classifier/reject', { body: { version } }),
  rollbackPageClassifier: (version: string) =>
    request<{ current: string; rollback_to: string | null }>('POST', '/ml/page-classifier/rollback', { body: { version } }),
  pageRouter: (signal?: AbortSignal) => request<PageRouterStatus>('GET', '/ml/page-router', { signal }),
  retrainPageRouter: () => request<RetrainLogRow>('POST', '/ml/page-router/retrain'),
  approvePageRouter: (version: string) =>
    request<{ current: string; rollback_to: string | null }>('POST', '/ml/page-router/approve', { body: { version } }),
  rejectPageRouter: (version: string) =>
    request<{ current: string | null; rejected: string }>('POST', '/ml/page-router/reject', { body: { version } }),
  rollbackPageRouter: (version: string) =>
    request<{ current: string; rollback_to: string | null }>('POST', '/ml/page-router/rollback', { body: { version } }),

  // протокол
  protocol: (id: string, version?: number, signal?: AbortSignal) =>
    request<Protocol>('GET', `/processes/${seg(id)}/protocol`, { query: { version }, signal }),
  protocolVersions: async (id: string, signal?: AbortSignal) =>
    asItems(await request<ListLike<ProtocolVersionRow>>('GET', `/processes/${seg(id)}/protocol/versions`, { signal })),
  exportPath: (id: string, kind: 'pdf' | 'docx' | 'xml' | 'submission') =>
    kind === 'submission' ? `/processes/${seg(id)}/submission.json` : `/processes/${seg(id)}/protocol.${kind}`,

  // находки и решения
  finding: (fid: number, signal?: AbortSignal) => request<FindingCard>('GET', `/findings/${fid}`, { signal }),
  decide: (fid: number, body: { decision: Decision; reason_code?: string; comment?: string }) =>
    request<DecisionResult>('POST', `/findings/${fid}/decision`, { body }),
  bulkConfirm: (ids: number[], comment?: string) =>
    request<BulkResult>('POST', '/findings/bulk-confirm', { body: { finding_ids: ids, comment: comment || undefined } }),
  promote: (fid: number, comment?: string) =>
    request<DecisionResult>('POST', `/findings/${fid}/promote`, { body: { comment: comment || undefined } }),
  finalize: (id: string, comment?: string) =>
    request<ProcessOut>('POST', `/processes/${seg(id)}/finalize`, { body: { comment: comment || undefined } }),
  unfinalize: (id: string, reason: string) =>
    request<ProcessOut>('POST', `/processes/${seg(id)}/unfinalize`, { body: { reason } }),

  // ИАИС РиН (заглушка)
  sendToRin: (id: string) => request<Partial<SyncInfo>>('POST', `/inspection/${seg(id)}`, { timeoutMs: 90_000 }),
  rinStatus: (id: string, signal?: AbortSignal) => request<SyncInfo>('GET', `/inspection/${seg(id)}`, { signal }),

  // GOLD, отчёт, модели
  datasetItems: async (version = 'draft', signal?: AbortSignal) =>
    asItems(await request<ListLike<DatasetItem>>('GET', '/dataset/items', { query: { version }, signal })),
  datasetVersions: async (signal?: AbortSignal) =>
    asItems(await request<ListLike<DatasetVersionRow>>('GET', '/dataset/versions', { signal })),
  publishDataset: (version?: string) =>
    request<DatasetVersionRow>('POST', '/dataset/versions', { body: version ? { version } : {} }),
  datasetExportPath: (version: string) => `/dataset/versions/${seg(version)}/export`,
  retrainingReport: (days: number, signal?: AbortSignal) =>
    request<RetrainingReport>('GET', '/reports/retraining', { query: { days }, signal }),
  models: async (signal?: AbortSignal) =>
    asItems(await request<ListLike<ModelVersionRow>>('GET', '/models', { signal })),

  // нормативная база
  params: async (signal?: AbortSignal) => asItems(await request<ListLike<Param>>('GET', '/params', { signal })),
  auditOfProcess: async (processId: string, signal?: AbortSignal) =>
    asItems(await request<ListLike<AuditRow>>('GET', '/audit', { query: { process_id: processId, limit: 500 }, signal })),
  patchParam: (code: string, patch: ParamPatch) => request<Param>('PATCH', `/params/${seg(code)}`, { body: patch }),

  // журнал и здоровье
  audit: async (
    q: { process_id?: string; user?: string; action?: string; limit?: number },
    signal?: AbortSignal,
  ) => asItems(await request<ListLike<AuditRow>>('GET', '/audit', { query: q, signal })),
  health: (signal?: AbortSignal) => request<Health>('GET', '/health', { signal, timeoutMs: 6000, retries: 0 }),
}
