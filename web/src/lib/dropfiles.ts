// Файлы из перетаскивания (в т.ч. папки целиком) и из выбора папки — с путём внутри пакета.

export interface PickedFile {
  file: File
  /** путь внутри пакета: «Том 4/КР1.pdf»; сервер сохраняет его как rel_path */
  relPath: string
}

function readAll(reader: FileSystemDirectoryReader): Promise<FileSystemEntry[]> {
  return new Promise((resolve, reject) => reader.readEntries(resolve, reject))
}

function fileOf(entry: FileSystemFileEntry): Promise<File> {
  return new Promise((resolve, reject) => entry.file(resolve, reject))
}

async function walk(entry: FileSystemEntry, prefix: string, out: PickedFile[]): Promise<void> {
  if (entry.isFile) {
    const file = await fileOf(entry as FileSystemFileEntry)
    out.push({ file, relPath: prefix + file.name })
    return
  }
  if (entry.isDirectory) {
    const reader = (entry as FileSystemDirectoryEntry).createReader()
    // readEntries отдаёт содержимое порциями — читаем, пока не вернёт пусто
    for (;;) {
      const batch = await readAll(reader)
      if (!batch.length) break
      for (const child of batch) await walk(child, `${prefix}${entry.name}/`, out)
    }
  }
}

/** Вызывать синхронно в обработчике drop: после него DataTransfer становится недоступен. */
export function filesFromDrop(dt: DataTransfer): Promise<PickedFile[]> {
  const entries: FileSystemEntry[] = []
  for (const item of Array.from(dt.items ?? [])) {
    if (item.kind !== 'file') continue
    const entry = item.webkitGetAsEntry?.()
    if (entry) entries.push(entry)
  }
  if (!entries.length) {
    return Promise.resolve(Array.from(dt.files ?? []).map((file) => ({ file, relPath: file.name })))
  }
  return (async () => {
    const out: PickedFile[] = []
    for (const e of entries) await walk(e, '', out)
    return out
  })()
}

export function filesFromInput(list: FileList | null): PickedFile[] {
  return Array.from(list ?? []).map((file) => ({ file, relPath: file.webkitRelativePath || file.name }))
}
