// Форматирование чисел, дат, размеров — по-русски.

const nf1 = new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 1 })
const nf0 = new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 0 })

export function formatInt(n: number | null | undefined): string {
  return n === null || n === undefined || Number.isNaN(n) ? '—' : nf0.format(n)
}

/** Размер в двоичных единицах (как считает сервер лимиты 50/200 МБ). */
export function formatBytes(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined || Number.isNaN(bytes)) return '—'
  if (bytes < 1024) return `${bytes} Б`
  const units = ['КБ', 'МБ', 'ГБ', 'ТБ']
  let v = bytes / 1024
  let i = 0
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024
    i++
  }
  return `${v >= 100 ? nf0.format(v) : nf1.format(v)} ${units[i]}`
}

export function percent(part: number, total: number): string {
  if (!total) return '—'
  const p = (part / total) * 100
  if (p > 0 && p < 0.1) return '< 0,1 %'
  return `${nf1.format(p)} %`
}

export function percentValue(part: number, total: number): number {
  if (!total) return 0
  return Math.max(0, Math.min(100, (part / total) * 100))
}

const dtf = new Intl.DateTimeFormat('ru-RU', {
  day: '2-digit',
  month: '2-digit',
  year: 'numeric',
  hour: '2-digit',
  minute: '2-digit',
})
const df = new Intl.DateTimeFormat('ru-RU', { day: '2-digit', month: '2-digit', year: 'numeric' })

function parse(iso: string | null | undefined): Date | null {
  if (!iso) return null
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? null : d
}

export function formatDateTime(iso: string | null | undefined): string {
  const d = parse(iso)
  return d ? dtf.format(d) : '—'
}

export function formatDate(iso: string | null | undefined): string {
  const d = parse(iso)
  return d ? df.format(d) : '—'
}

/** «только что», «5 мин назад», иначе дата и время. */
export function formatAgo(iso: string | null | undefined, now = Date.now()): string {
  const d = parse(iso)
  if (!d) return '—'
  const s = Math.round((now - d.getTime()) / 1000)
  if (s < 0) return dtf.format(d)
  if (s < 45) return 'только что'
  if (s < 3600) return `${Math.max(1, Math.round(s / 60))} мин назад`
  if (s < 6 * 3600) return `${Math.round(s / 3600)} ч назад`
  return dtf.format(d)
}

export function formatSeconds(sec: number | null | undefined): string {
  if (sec === null || sec === undefined || Number.isNaN(sec)) return ''
  // число и единица — неразрывным пробелом, чтобы «1 мин» не переносилось в узких ячейках
  const s = Math.round(sec)
  if (s < 60) return `${s}\u00a0с`
  const m = Math.floor(s / 60)
  if (m < 60) return `${m}\u00a0мин ${String(s % 60).padStart(2, '0')}\u00a0с`
  const h = Math.floor(m / 60)
  return `${h}\u00a0ч ${String(m % 60).padStart(2, '0')}\u00a0мин`
}

export function shortHash(sha: string | null | undefined, n = 12): string {
  if (!sha) return '—'
  return sha.length > n ? `${sha.slice(0, n)}…` : sha
}

/** Склонение: plural(3, ['файл', 'файла', 'файлов']) → «файла». */
export function plural(n: number, forms: [string, string, string]): string {
  const a = Math.abs(n) % 100
  const b = a % 10
  if (a > 10 && a < 20) return forms[2]
  if (b > 1 && b < 5) return forms[1]
  if (b === 1) return forms[0]
  return forms[2]
}

export function countLabel(n: number, forms: [string, string, string]): string {
  return `${formatInt(n)} ${plural(n, forms)}`
}
