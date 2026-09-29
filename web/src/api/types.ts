// Типы ответов API и форма протокола (tests/data/protocol_example.json).

export type Role = 'inspector' | 'supervisor' | 'admin' | 'ml'

export interface User {
  id: number
  login: string
  role: Role
  full_name: string
}

export interface LoginResponse {
  access_token: string
  token_type: string
  user: User
}

export type ProcessStatus = 'PENDING' | 'PARSING' | 'READY' | 'VERIFYING' | 'COMPLETED' | 'FINALIZED' | 'FAILED'

export type FindingStatus =
  | 'NEGATIVE_VERIFIED'
  | 'CANDIDATE'
  | 'CONFIRMED_VIOLATION'
  | 'MISSING_EVIDENCE'
  | 'NOT_APPLICABLE'
  | 'NOT_COMPARABLE'
  | 'CLARIFICATION_REQUIRED'
  | 'SUSPICION'

export type Stage = 'PD' | 'RD' | 'ID'
export type UploadStage = Stage | 'AUTO'
export type Indicator = 'gray' | 'green' | 'yellow' | 'red'
export type Priority = 'HIGH' | 'MEDIUM' | 'LOW'
export type Decision = 'CONFIRMED_VIOLATION' | 'NEGATIVE_VERIFIED' | 'CLARIFICATION_REQUIRED'

export interface Items<T> {
  items: T[]
  total?: number
}

export interface StageProgress {
  status?: string
  done?: number
  total?: number
  seconds?: number
  error?: string | null
}

export interface UploadStatusRow {
  stage: Stage
  status: string
  files: number
  pending_files?: number
  problem_files?: number
  broken?: number
  pages: number
  ocr_pages: number
  comment: string
}

export interface ObjectInfo {
  object_id: string
  name: string
  address: string
  customer: string
  contractor: string
  permit_number: string
}

export interface ProcessCounts {
  files: number
  pages: number
  candidates: number
  candidates_pending: number
  confirmed: number
  negatives: number
  suspicions: number
  not_comparable: number
  missing: number
  clarifications: number
}

export interface ProcessOut {
  id: string
  object: ObjectInfo
  status: ProcessStatus
  stage: string | null
  progress: Record<string, StageProgress> | null
  scenario: string | null
  error: string | null
  sync_status: string
  protocol_version: number
  batch: number
  created_at: string
  updated_at: string
  finalized_at: string | null
  counts: Partial<ProcessCounts> | null
  upload_status: UploadStatusRow[] | null
  indicator: Indicator
}

export interface NewProcessBody {
  object_id?: string
  name: string
  address?: string
  customer?: string
  contractor?: string
  permit_number?: string
}

export type BBox = [number, number, number, number]

export interface Source {
  role: string
  stage: Stage
  file_pk: number
  file_id: string | null
  file_name: string | null
  sha256: string | null
  document_code: string | null
  revision: string | null
  approval_status: string | null
  sheet: string | null
  page: number
  bbox: BBox | null
  bbox_source: string | null
  value: string | null
  snippet: string | null
}

export interface InspectorState {
  status: string
  user: string | null
  decided_at: string | null
  reason_code: string | null
  comment: string | null
}

export interface Verification {
  same_entity?: string
  trigger_met?: string
  analysis?: string
}

export interface HistoryRow {
  decision: string
  previous_status: string
  reason_code: string | null
  comment: string | null
  user: string | null
  created_at: string
}

export interface FindingCard {
  id: number
  finding_id: string
  object_id?: string
  source: string
  code: string
  rule_version: string
  section: string
  parameter_name: string
  location: string
  expected_value: string | null
  actual_values: Partial<Record<'RD' | 'ID', string>>
  delta: string | null
  status: FindingStatus
  machine_status: FindingStatus
  finding_status: string | null
  completeness_status: string
  review_priority: Priority
  criticality: string | null
  rationale: string
  verification: Verification | null
  discovery_method: string | null
  approved_change_ref: string
  sources: Source[]
  inspector: InspectorState
  submission: { violation_label: string | null; protocol_status: string | null; include: boolean }
  created_in_version: number
  history?: HistoryRow[]
  /** карточка из /findings: к какой проверке относится и актуальна ли после повторной обработки */
  process_id?: string
  active?: boolean
}

export interface ParamRow {
  code: string
  section: string
  name: string
  status: FindingStatus
  comment: string
  finding_ids: string[]
}

export interface Traceability {
  process_id: string
  matrix_version: string
  dataset_version: string
  model_version: string
  run_timestamp: string | null
  input_manifest_sha256: string
}

export interface ProtocolFile {
  file_pk: number
  file_id: string
  name: string
  rel_path?: string
  stage: Stage | null
  stage_source: string | null
  sha256: string
  pages: number
  ocr_pages: number
  status: string
  error: string | null
  document_code: string | null
  revision: string | null
  approval_status: string | null
  discipline: string | null
}

export interface Protocol {
  protocol_id: string
  process_id: string
  version: number
  process_status: ProcessStatus
  generated_at: string
  object: ObjectInfo
  traceability: Traceability
  scenario: string
  upload_status: UploadStatusRow[]
  summary: Record<string, number>
  findings_summary?: Record<string, number>
  params: ParamRow[]
  findings: FindingCard[]
  files: ProtocolFile[]
  low_quality_pages: { file_id: string; page: number; reason: string }[]
}

export interface FileRow {
  file_pk: number
  file_id: string
  name: string
  rel_path: string
  ext: string
  size: number
  sha256: string
  stage: Stage | null
  stage_source: string | null
  status: string
  error: string | null
  pages: number
  ocr_pages: number
  document_code: string | null
  revision: string | null
  approval_status: string | null
  discipline: string | null
  batch: number
  uploaded_at: string
}

export interface DocumentRow {
  id: number
  file_pk: number
  file_id: string
  file_name: string
  first_page: number
  last_page: number
  doc_type: string
  stage: Stage | null
  section: string | null
  title: string | null
  document_code: string | null
  sheet: string | null
  revision: string | null
  approval_status: string | null
  is_actual: boolean
  clarification: boolean
  clarification_reason: string | null
}

export interface PageInfo {
  page_no: number
  width: number
  height: number
  rotation: number
  text_source: string
  text: string
  page_class: string | null
  other_classes: string[]
  is_first_page: boolean | null
  section: string | null
  title: string | null
  quality_label: string | null
  low_quality: boolean
  document_pk: number | null
}

export interface UploadAccepted {
  file_pk: number
  name: string
  rel_path?: string
  size: number
  stage: string | null
  status: string
}

export interface UploadRejected {
  name: string
  reason_code: string
  reason: string
}

export interface UploadResult {
  process_id: string
  batch?: number
  job_id?: number | null
  accepted: UploadAccepted[]
  rejected: UploadRejected[]
}

export interface ProtocolVersionRow {
  version: number
  kind: string
  status: string
  created_at: string
  finalized_at: string | null
  summary: Record<string, number> | null
  model_version: string
  dataset_version: string
  input_manifest_hash: string
}

export interface BulkResult {
  updated: number[]
  skipped: { id: number | string; reason_code?: string; reason: string }[]
}

/** Ответ на решение/привязку: карточка с историей + новый статус проверки. */
export interface DecisionResult extends FindingCard {
  process_status?: ProcessStatus
  candidates_pending?: number
}

export interface Param {
  code: string
  section: string
  parameter_name: string
  unit: string
  source_pd: string
  source_rd: string
  source_id: string
  trigger_logic: string
  criticality: string
  review_priority: Priority | string
  data_type: string
  min_value: number | null
  max_value: number | null
  regex_pattern?: string | null
  sp_reference: string
  gost_reference: string
  fz_reference: string
  other_normative: string
  is_active: boolean
  updated_at?: string
}

export type ParamPatch = Partial<
  Pick<
    Param,
    | 'trigger_logic'
    | 'min_value'
    | 'max_value'
    | 'is_active'
    | 'review_priority'
    | 'sp_reference'
    | 'gost_reference'
    | 'fz_reference'
    | 'other_normative'
  >
>

export interface AuditRow {
  id: number
  user?: string
  user_login?: string
  action: string
  object_code: string | null
  process_id: string | null
  details: unknown
  ip?: string | null
  user_agent?: string | null
  created_at: string
}

export interface DatasetItem {
  id: number
  finding_pk: number
  evidence_group_id: string
  object_id?: string
  object_code: string
  param_code: string
  gold_label: string
  expert?: string | null
  expert_pk?: number | null
  reason_code: string | null
  dataset_version: string | null
  split: string | null
  created_at: string
}

export interface DatasetVersionRow {
  version: string
  items: number
  positives: number
  negatives: number
  sha256: string
  split_hashes: Record<string, string> | null
  split_objects: Record<string, string[]> | null
  split_counts?: Record<string, number> | null
  created_at: string
  created_by?: string | number | null
}

export interface RetrainingReport {
  period_days: number
  decisions: { confirmed?: number; rejected?: number; clarification?: number }
  rejection_by_reason: Record<string, number> | { reason_code: string; count: number }[]
  rejection_by_section: Record<string, number> | { section: string; count: number }[]
  rejection_by_param: { code: string; count: number }[]
  confirm_rate: number | null
  recommendations: string[]
  dataset: { draft_items?: number; latest_version?: string | null } | null
}

export interface ModelVersionRow {
  id?: number
  is_current?: boolean
  model_version: string
  artifact: string
  dataset_version: string
  metrics: Record<string, unknown> | null
  approval_status: string
  approved_by: string
  deployed_at: string | null
  rollback_to: string | null
}

export interface Health {
  status: string
  db?: string | boolean
  llm?: { url?: string; reachable?: boolean }
  worker?: { queued_jobs?: number; running_jobs?: number; last_heartbeat?: string | null }
}

export interface SyncAttemptRow {
  attempt: number
  status: string
  http_status: number | null
  error: string | null
  next_try_at: string | null
  created_at: string
}

export interface SyncInfo {
  sync_status: string
  attempts: SyncAttemptRow[]
}

// --- классификатор страниц: метки, правки разметчиков, дообучение ---------------

export type LabelSource = 'model' | 'qwen' | 'inspector'

export interface PageRow {
  page_pk: number
  file_pk: number
  file_id: string
  file_name: string
  stage: Stage | null
  page_no: number
  page_class: string | null
  other_classes: string[]
  is_first_page: boolean | null
  section: string | null
  quality_label: string | null
  title: string | null
  text_source: string
  /** кто поставил текущую метку; null — прогон до записи меток */
  label_source: LabelSource | null
  confidence: number | null
  label_model: string | null
  /** что сказал классификатор страниц (даже если потом перепроверил Qwen или поправил человек) */
  model_class: string | null
  model_confidence: number | null
  corrected: boolean
  document_pk: number | null
}

export interface PageClassDef {
  code: string
  description: string
  group: 'sheet' | 'id' | 'other'
}

export interface PageTaxonomy {
  classes: PageClassDef[]
  sections: string[]
}

export interface PageCorrectionIn {
  /** страница — по page_pk или по файлу и номеру */
  page_pk?: number
  file_pk?: number
  page_no?: number
  page_class?: string
  is_first_page?: boolean
  section?: string
}

export interface PageCorrectionResult {
  created: number
  items: { id: number; page_pk: number; page_class: string | null; is_first_page: boolean | null; section: string | null }[]
}

export interface AccPart {
  ok: number
  n: number
  acc: number
}

/** {corrections: {cls, first, section}, frozen?: {...}} — доля совпадений с метками */
export type RetrainMetrics = Record<string, Partial<Record<'cls' | 'first' | 'section', AccPart>>>

export type RetrainStatus = 'QUEUED' | 'RUNNING' | 'ACCEPTED' | 'REJECTED' | 'DRAFT' | 'FAILED' | 'ROLLBACK' | 'APPROVED'

export interface RetrainLogRow {
  id: number
  /** page-classifier | page-router */
  model_kind?: string
  status: RetrainStatus
  base_version: string | null
  new_version: string | null
  n_corrections: number | null
  corrections_sha256: string | null
  metrics_before: RetrainMetrics | null
  metrics_after: RetrainMetrics | null
  rule: string | null
  seconds: number | null
  error: string | null
  requested_by: string | null
  decided_by: string | null
  artifact: string | null
  created_at: string | null
  finished_at: string | null
}

export interface PageClassifierVersion {
  model_version: string
  approval_status: string
  approved_by: string
  deployed_at: string | null
  rollback_to: string | null
  metrics: RetrainMetrics | Record<string, unknown> | null
  artifact: string
  is_current: boolean
}

/** Отбор страниц: {labels: {found, dropped}} — страницы «есть» в первых top_k модели и страницы «нет» вне их */
export type RouterMetrics = Record<string, Partial<Record<'found' | 'dropped', AccPart>>>

export interface PageRouterManifest {
  name?: string
  version?: string
  n_pages?: number
  n_models?: number
  keep_per_param?: number
  trained_on?: Record<string, string>
  labels?: string
  scan_text?: string
  parent_version?: string
  expert_labels?: { decisions: number; positive: number; negative: number; params: number; processes: number }
  adaptation?: { from?: string; updated_params?: string[]; new_params?: string[] }
  created?: string
}

export interface PageRouterStatus {
  enabled: boolean
  /** fused — модель + слова; lexical — модель выключена настройкой SELECT_MODE */
  select_mode: string
  top_k: number
  current: string | null
  manifest: PageRouterManifest | null
  versions: PageClassifierVersion[]
  labels: {
    decisions: number
    positive: number
    negative: number
    params: number
    processes: number
    sha256: string | null
    /** есть решения, на которых ещё не учились */
    new: boolean
  }
  log: RetrainLogRow[]
}

export interface PageClassifierStatus {
  enabled: boolean
  current: string | null
  manifest: Record<string, unknown> | null
  versions: PageClassifierVersion[]
  corrections: { total: number; unused: number; confusions: { from: string; to: string; n: number }[] }
  /** набор для дообучения: размеченные страницы загруженных проверок и правки разметчиков */
  dataset: { pages: number; corrections: number }
  acceptance_set: boolean
  acceptance_pages: number
  /** уверенность ниже — страница уходит к Qwen */
  threshold: number | null
  log: RetrainLogRow[]
}
