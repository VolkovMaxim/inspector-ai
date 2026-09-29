// Какие кнопки видит роль (сервер всё равно проверяет права сам; здесь — только чтобы не показывать лишнее).
// Спека, раздел 5: inspector — просмотр и решения; supervisor — + отмена финализации;
// admin — всё, в т.ч. параметры, импорт с диска, журнал; ml — журнал, GOLD, отчёт, дообучение классификатора
// страниц; правка типа страницы — все роли.
import type { ProcessStatus, Role } from '../api/types'

export type Action =
  | 'decide'
  | 'upload'
  | 'start'
  | 'finalize'
  | 'unfinalize'
  | 'sendRin'
  | 'importLocal'
  | 'editParams'
  | 'viewAudit'
  | 'gold'
  | 'label'
  | 'retrain'

const MATRIX: Record<Action, Role[]> = {
  decide: ['inspector', 'supervisor', 'admin'],
  upload: ['inspector', 'supervisor', 'admin'],
  start: ['inspector', 'supervisor', 'admin'],
  finalize: ['inspector', 'supervisor', 'admin'],
  unfinalize: ['supervisor', 'admin'],
  sendRin: ['inspector', 'supervisor', 'admin'],
  importLocal: ['admin'],
  editParams: ['admin'],
  viewAudit: ['admin', 'supervisor', 'ml'],
  gold: ['ml', 'admin'],
  label: ['inspector', 'supervisor', 'admin', 'ml'],
  retrain: ['ml', 'admin'],
}

export function can(role: Role | null | undefined, action: Action): boolean {
  if (!role) return false
  return MATRIX[action].includes(role)
}

// Статусы процесса (inspector/statuses.py): где что разрешено.
export const UPLOAD_ALLOWED: ProcessStatus[] = ['PENDING', 'READY', 'VERIFYING', 'COMPLETED', 'FAILED']
export const DECISIONS_ALLOWED: ProcessStatus[] = ['READY', 'VERIFYING', 'COMPLETED']

export function uploadAllowed(status: ProcessStatus): boolean {
  return UPLOAD_ALLOWED.includes(status)
}

export function decisionsAllowed(status: ProcessStatus): boolean {
  return DECISIONS_ALLOWED.includes(status)
}
