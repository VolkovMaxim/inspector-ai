// Небольшой набор линейных иконок (16 px, цвет — currentColor). Без внешних библиотек.

const PATHS = {
  chevronLeft: 'M10 3.5 5.5 8l4.5 4.5',
  chevronRight: 'M6 3.5 10.5 8 6 12.5',
  minus: 'M3.5 8h9',
  plus: 'M8 3.5v9M3.5 8h9',
  fitWidth: 'M2.5 4v8M13.5 4v8M5 8h6M6.5 6.5 5 8l1.5 1.5M9.5 6.5 11 8l-1.5 1.5',
  target: 'M8 1.5v3M8 11.5v3M1.5 8h3M11.5 8h3M5 5h6v6H5z',
  text: 'M3 3.5h10M3 6.5h10M3 9.5h7M3 12.5h5',
  expand: 'M9.5 2.5h4v4M13.5 2.5 9 7M6.5 13.5h-4v-4M2.5 13.5 7 9',
  collapse: 'M13 3 9 7M9 3.5V7h3.5M3 13l4-4M7 12.5V9H3.5',
  download: 'M8 2.5v8M4.5 7 8 10.5 11.5 7M3 13.5h10',
  upload: 'M8 10.5v-8M4.5 6 8 2.5 11.5 6M3 13.5h10',
  copy: 'M5.5 5.5h7v7h-7zM3.5 10.5v-7h7',
  check: 'M3 8.5 6.5 12 13 4.5',
  close: 'M4 4l8 8M12 4l-8 8',
  alert: 'M8 2 14.5 13.5h-13zM8 6.5v3.5M8 11.8v.2',
  refresh: 'M13 3.5v3h-3M3 12.5v-3h3M12.6 6.5A5 5 0 0 0 3.8 5M3.4 9.5a5 5 0 0 0 8.8 1.5',
  folder: 'M1.5 4.5v8h13v-7H7.5l-1.5-1.5h-4.5z',
  file: 'M4 1.5h5.5L12.5 4.5v10h-8.5zM9.5 1.5v3h3',
  sidebar: 'M2 3h12v10H2zM6 3v10',
  back: 'M6.5 3.5 2 8l4.5 4.5M2.5 8h11',
  send: 'M2 8 14 2.5 11 14 8 9.5zM8 9.5 14 2.5',
  lock: 'M4 7.5h8v6H4zM5.5 7.5V5a2.5 2.5 0 0 1 5 0v2.5',
  info: 'M8 14.5a6.5 6.5 0 1 0 0-13 6.5 6.5 0 0 0 0 13zM8 7.2v4.3M8 4.6v.2',
  edit: 'M10.5 2.5l3 3-7.5 7.5H3v-3zM9 4l3 3',
} as const

export type IconName = keyof typeof PATHS

export function Icon({ name, size = 16, title }: { name: IconName; size?: number; title?: string }) {
  return (
    <svg
      className="icon"
      width={size}
      height={size}
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.5}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden={title ? undefined : true}
      role={title ? 'img' : undefined}
    >
      {title ? <title>{title}</title> : null}
      <path d={PATHS[name]} />
    </svg>
  )
}
