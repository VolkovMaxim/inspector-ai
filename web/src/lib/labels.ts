// Русские подписи кодов API. Код остаётся в подсказке (title), подпись — в интерфейсе.
import type { Role, Stage } from '../api/types'

export const ROLE_LABEL: Record<Role, string> = {
  inspector: 'Инспектор',
  supervisor: 'Супервизор',
  admin: 'Администратор',
  ml: 'ML-инженер',
}

export const PROCESS_STATUS_LABEL: Record<string, string> = {
  PENDING: 'Ожидает запуска',
  PARSING: 'Обработка',
  READY: 'Готов к проверке',
  VERIFYING: 'Идёт верификация',
  COMPLETED: 'Верификация завершена',
  FINALIZED: 'Финализирован',
  FAILED: 'Ошибка обработки',
}

export const PROCESS_STATUSES = ['PENDING', 'PARSING', 'READY', 'VERIFYING', 'COMPLETED', 'FINALIZED', 'FAILED']

/** Статусы параметра/находки (ТЗ 9.2). */
export const FINDING_STATUS_LABEL: Record<string, string> = {
  CANDIDATE: 'Кандидат',
  CONFIRMED_VIOLATION: 'Подтверждённое нарушение',
  NEGATIVE_VERIFIED: 'Нарушения нет',
  MISSING_EVIDENCE: 'Нет доказательств',
  NOT_APPLICABLE: 'Неприменим',
  NOT_COMPARABLE: 'Несопоставимо',
  CLARIFICATION_REQUIRED: 'Требует уточнения',
  SUSPICION: 'Гипотеза',
}

/** Решение инспектора по кандидату. */
export const DECISION_LABEL: Record<string, string> = {
  PENDING: 'Ждёт решения',
  CONFIRMED_VIOLATION: 'Подтверждено',
  NEGATIVE_VERIFIED: 'Отклонено',
  CLARIFICATION_REQUIRED: 'На уточнение',
  PROMOTE: 'Привязано к кандидатам',
}

export const REASON_CODES: { code: string; label: string }[] = [
  { code: 'ACTUAL_REVISION_WRONG', label: 'Актуальная редакция выбрана неверно' },
  { code: 'APPROVED_CHANGE', label: 'Есть согласованное изменение' },
  { code: 'OCR_ERROR', label: 'Ошибка распознавания (OCR)' },
  { code: 'BINDING_ERROR', label: 'Ошибка привязки (не та локация или элемент)' },
  { code: 'EXTRACTION_ERROR', label: 'Значение извлечено неверно' },
  { code: 'NOT_APPLICABLE', label: 'Параметр неприменим к объекту' },
  { code: 'OTHER', label: 'Другое' },
]

export const REASON_LABEL: Record<string, string> = Object.fromEntries(REASON_CODES.map((r) => [r.code, r.label]))

export const PRIORITY_LABEL: Record<string, string> = { HIGH: 'Высокий', MEDIUM: 'Средний', LOW: 'Низкий' }

export const STAGE_SHORT: Record<Stage, string> = { PD: 'ПД', RD: 'РД', ID: 'ИД' }
export const STAGE_LONG: Record<Stage, string> = {
  PD: 'Проектная документация',
  RD: 'Рабочая документация',
  ID: 'Исполнительная документация',
}
export const STAGES: Stage[] = ['PD', 'RD', 'ID']

export const ROLE_OF_SOURCE: Record<string, string> = {
  expected: 'ожидаемое значение',
  actual_rd: 'фактическое значение',
  actual_id: 'фактическое значение',
}

export const UPLOAD_STATUS_LABEL: Record<string, string> = {
  PD_UPLOADED: 'ПД загружена',
  PD_PARTIAL: 'ПД загружена частично',
  PD_MISSING: 'ПД нет',
  RD_UPLOADED: 'РД загружена',
  RD_PARTIAL: 'РД загружена частично',
  RD_MISSING: 'РД нет',
  ID_UPLOADED: 'ИД загружена',
  ID_PARTIAL: 'ИД загружена частично',
  ID_MISSING: 'ИД нет',
}

export const SCENARIO_LABEL: Record<string, string> = {
  FULL: 'Полный комплект: ПД, РД и ИД',
  PD_RD_ONLY: 'Только ПД и РД',
  PD_ID_ONLY: 'Только ПД и ИД',
  RD_ID_ONLY: 'Только РД и ИД',
  SINGLE_ONLY: 'Одна стадия',
  PARTIALLY_LOADED: 'Загружено частично',
}

export const STAGE_SOURCE_LABEL: Record<string, string> = {
  declared: 'указана при загрузке',
  manifest: 'из манифеста организатора',
  content: 'определена по содержимому',
  folder: 'по имени папки — только подсказка',
}

export const FILE_STATUS_LABEL: Record<string, string> = {
  pending: 'ожидает обработки',
  ok: 'читается',
  duplicate: 'дубликат',
  junk: 'служебный файл',
  unsupported: 'формат не поддерживается',
  broken: 'повреждён',
  too_large: 'слишком большой',
  archive: 'архив, распакован',
  converted_source: 'исходник, конвертирован в PDF',
}

export const APPROVAL_LABEL: Record<string, string> = {
  UNKNOWN: 'не указан',
  APPROVED: 'утверждён',
  IN_WORK: 'в производство работ',
  NOT_APPROVED: 'не утверждён',
}

export const BBOX_SOURCE_LABEL: Record<string, string> = {
  layer: 'рамка по текстовому слою',
  rapidocr: 'рамка по распознанным словам',
  page: 'место на странице не определено',
}

export const UPLOAD_REJECT_LABEL: Record<string, string> = {
  FILE_TOO_LARGE: 'Больше 50 МБ',
  UNSUPPORTED_FORMAT: 'Формат не поддерживается',
  EMPTY_FILE: 'Пустой файл',
  UPLOAD_TOO_LARGE: 'Пакет больше 200 МБ',
  DUPLICATE: 'Уже добавлен',
  NOT_SENT: 'Не отправлен',
}

export const SYNC_LABEL: Record<string, string> = {
  NONE: 'Не отправлялся',
  PENDING_SYNC: 'Ждёт повторной отправки',
  SYNCED: 'Передан в РиН',
  FAILED: 'Передача не удалась',
  SCHEDULED: 'Запланирован повтор',
}

export const VERSION_KIND_LABEL: Record<string, string> = {
  machine: 'Машинный',
  incremental: 'После дозагрузки',
  final: 'Итоговый',
  unfinalize: 'После отмены финализации',
}

export const DISCOVERY_LABEL: Record<string, string> = {
  SEMANTIC_DISSONANCE: 'Смысловое расхождение',
}

export const VERIFY_ANSWER: Record<string, string> = { YES: 'да', NO: 'нет', UNSURE: 'не уверен' }

export const INDICATOR_LABEL: Record<string, string> = {
  red: 'Красный: подтверждённое нарушение или нерешённый кандидат высокого приоритета',
  yellow: 'Жёлтый: есть кандидаты, несопоставимые значения или вопросы на уточнение',
  green: 'Зелёный: нарушений и открытых вопросов нет',
  gray: 'Серый: протокол ещё не сформирован',
}

/** Этапы конвейера в порядке выполнения (поле progress процесса). */
export const PIPELINE_STAGES: { key: string; label: string; hint: string }[] = [
  { key: 'intake', label: 'Приём файлов', hint: 'распаковка, дубликаты, конвертация в PDF' },
  { key: 'pages', label: 'Разбор страниц', hint: 'текстовый слой, рамки слов, повороты' },
  { key: 'ocr', label: 'Распознавание', hint: 'RapidOCR: текст страниц без пригодного слоя' },
  { key: 'classify', label: 'Классы страниц', hint: 'класс, первая страница, раздел, качество: модель, спорные — Qwen' },
  { key: 'documents', label: 'Сборка документов', hint: 'документы, паспорт, редакции' },
  { key: 'select', label: 'Отбор страниц', hint: 'обучаемый отбор + слова: 40 страниц на параметр и стадию' },
  { key: 'reocr', label: 'Точный текст', hint: 'отобранные сканы перечитывает Qwen' },
  { key: 'extract', label: 'Извлечение значений', hint: 'значение, цитата, рамка' },
  { key: 'compare', label: 'Сопоставление', hint: 'судья и верификатор ПД ↔ РД ↔ ИД' },
  { key: 'freesearch', label: 'Свободный поиск', hint: 'экспликации и квартирография — гипотезы' },
  { key: 'protocol', label: 'Протокол', hint: 'снимок протокола и сабмит' },
]

export const STAGE_RUN_LABEL: Record<string, string> = {
  pending: 'ожидает',
  queued: 'в очереди',
  running: 'выполняется',
  done: 'готово',
  ok: 'готово',
  skipped: 'пропущен',
  error: 'ошибка',
  failed: 'ошибка',
}

export const AUDIT_ACTION_LABEL: Record<string, string> = {
  login: 'Вход',
  login_failed: 'Неудачный вход',
  process_create: 'Создание проверки',
  upload: 'Загрузка файлов',
  import_local: 'Импорт с диска сервера',
  start: 'Запуск обработки',
  decision: 'Решение по карточке',
  bulk_confirm: 'Массовое подтверждение',
  promote: 'Привязка гипотезы',
  finalize: 'Финализация',
  unfinalize: 'Отмена финализации',
  sync: 'Отправка в РиН',
  RIN_SYNC_FAILED: 'Передача в РиН не удалась',
  params_patch: 'Изменение параметра матрицы',
  dataset_publish: 'Публикация версии GOLD',
  login_locked: 'Вход заблокирован: много неудачных попыток',
  protocol_view: 'Просмотр протокола',
  protocol_download: 'Выгрузка протокола',
  file_download: 'Скачивание исходного файла',
  protocol_ready: 'Протокол сформирован',
  PIPELINE_FAILED: 'Обработка остановлена с ошибкой',
  page_correction: 'Правка типа страницы',
  retrain_requested: 'Запуск дообучения классификатора страниц',
  model_approved: 'Утверждение версии классификатора',
  model_rollback: 'Откат версии классификатора',
  model_rejected: 'Отклонение версии классификатора',
  router_retrain_requested: 'Запуск дообучения отбора страниц',
  router_approved: 'Утверждение версии отбора страниц',
  router_rejected: 'Отклонение версии отбора страниц',
  router_rollback: 'Откат версии отбора страниц',
  STORE_INTEGRITY_OK: 'Проверка хранилища: файлы целы',
  STORE_INTEGRITY_FAILED: 'Проверка хранилища: найдены повреждения',
  RETRAINING_REPORT: 'Еженедельный отчёт по дообучению',
  MAINTENANCE_FAILED: 'Сбой регламентной задачи',
}

/** Тип страницы (inspector/pipeline/classify.py CLASSES): короткая подпись; описание приходит с сервера. */
export const PAGE_CLASS_LABEL: Record<string, string> = {
  ТИТУЛ: 'Титул',
  СОДЕРЖАНИЕ: 'Содержание',
  ТЕКСТ: 'Текст',
  ТЭП: 'ТЭП',
  ОБЩИЕ_ДАННЫЕ: 'Общие данные',
  ТАБЛИЦА: 'Таблица',
  ПЛАН: 'План',
  РАЗРЕЗ_ФАСАД: 'Разрез, фасад',
  УЗЕЛ: 'Узел',
  СХЕМА: 'Схема',
  ЧЕРТЁЖ_КОНСТРУКЦИЙ: 'Чертёж конструкций',
  РАСЧЁТ: 'Расчёт',
  ГРАФИК: 'График',
  СМЕТА: 'Смета',
  АОСР: 'АОСР',
  АКТ_ПРОЧИЙ: 'Акт (прочий)',
  ИСПОЛНИТЕЛЬНАЯ_СХЕМА: 'Исполнительная схема',
  ДОКУМЕНТ_О_КАЧЕСТВЕ: 'Документ о качестве',
  СЕРТИФИКАТ_ДЕКЛАРАЦИЯ: 'Сертификат, декларация',
  ПАСПОРТ_ИЗДЕЛИЯ: 'Паспорт изделия',
  ПРОТОКОЛ: 'Протокол испытаний',
  ЖУРНАЛ: 'Журнал работ',
  ТЕХПЛАН_БТИ: 'Техплан, БТИ',
  РАЗРЕШИТЕЛЬНЫЙ: 'Разрешительный',
  ЭНЕРГОПАСПОРТ: 'Энергопаспорт',
  ДОГОВОР: 'Договор',
  УЧЁТНЫЙ: 'Учётный (накладная, КС)',
  ФОТО: 'Фото',
  РЕЕСТР: 'Реестр, опись',
  ПИСЬМО_ПРИКАЗ: 'Письмо, приказ',
  ПРОЧЕЕ: 'Прочее',
  ПУСТАЯ: 'Пустая',
}

export const PAGE_CLASS_GROUP_LABEL: Record<string, string> = {
  sheet: 'Листы ПД и РД',
  id: 'Исполнительная документация',
  other: 'Прочее',
}

/** Кто поставил метку типа страницы. */
export const LABEL_SOURCE_LABEL: Record<string, string> = {
  model: 'Классификатор',
  qwen: 'Qwen',
  inspector: 'Исправлено вручную',
  none: 'Источник не записан',
}

export const TEXT_SOURCE_LABEL: Record<string, string> = {
  layer: 'текстовый слой PDF',
  rapidocr: 'распознан RapidOCR',
  ocr: 'прочитан Qwen',
  none: 'текста нет',
}

export const RETRAIN_STATUS_LABEL: Record<string, string> = {
  QUEUED: 'В очереди',
  RUNNING: 'Обучается',
  ACCEPTED: 'Прошла приёмку — ждёт утверждения',
  REJECTED: 'Отклонена приёмкой',
  DRAFT: 'Ждёт утверждения',
  FAILED: 'Ошибка',
  APPROVED: 'Утверждена вручную',
  ROLLBACK: 'Откат',
}

export const MODEL_APPROVAL_LABEL: Record<string, string> = {
  APPROVED: 'утверждена',
  DRAFT: 'черновик',
  REJECTED: 'отклонена',
}

export function label(map: Record<string, string>, code: string | null | undefined, fallback = '—'): string {
  if (code === null || code === undefined || code === '') return fallback
  return map[code] ?? code
}
