// Следит за проверками в обработке и сообщает на любой странице, когда протокол готов или обработка упала.
// Дёшево: список только проверок в статусе «Обработка»; пока их нет — раз в 30 с, пока есть — раз в 10 с.
import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../api/endpoints'
import { useInterval } from '../lib/hooks'
import { useToast } from './Toasts'

export function ProcessWatcher() {
  const toast = useToast()
  const known = useRef<Map<string, string> | null>(null)
  const [active, setActive] = useState(0)

  const check = useCallback(async () => {
    let items
    try {
      items = (await api.listProcesses({ status: 'PARSING', limit: 100 })).items
    } catch {
      return
    }
    const now = new Map(items.map((p) => [p.id, p.object.name || p.object.object_id]))
    const prev = known.current
    known.current = now
    setActive(now.size)
    if (!prev) return
    for (const [id, name] of prev) {
      if (now.has(id)) continue
      try {
        const p = await api.getProcess(id)
        const link = { label: 'Открыть', to: `/processes/${id}` }
        if (p.status === 'FAILED') toast.error(`Обработка не завершилась: ${name}`, link)
        else if (p.protocol_version >= 1) toast.ok(`Протокол готов: ${name} (v${p.protocol_version})`, link)
      } catch {
        /* проверку удалили или нет связи — молчим */
      }
    }
  }, [toast])

  useEffect(() => {
    void check()
  }, [check])
  useInterval(() => void check(), active ? 10_000 : 30_000, true)
  return null
}
