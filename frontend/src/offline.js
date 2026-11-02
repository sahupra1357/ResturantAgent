// Offline order capture: a table order that cannot reach the server (network down, server restarting, 5xx) is
// queued in localStorage with an Idempotency-Key and replayed in order when the connection is back. The server
// stores the response per key, so a retry after a lost response never creates a second order.
const KEY = 'ro-offline-queue'

export function makeQueue(storage = (typeof localStorage !== 'undefined' ? localStorage : null)) {
  const mem = { v: '[]' }
  const read = () => {
    try { return JSON.parse((storage ? storage.getItem(KEY) : mem.v) || '[]') } catch { return [] }
  }
  const write = (q) => {
    const s = JSON.stringify(q)
    try { storage ? storage.setItem(KEY, s) : (mem.v = s) } catch { mem.v = s }
    listeners.forEach((f) => f(q))
  }
  const listeners = new Set()
  return {
    list: read,
    size: () => read().length,
    add(entry) {
      const q = read()
      const item = { key: entry.key || newKey(), path: entry.path, body: entry.body, label: entry.label || '', queuedAt: Date.now(), tries: 0, error: '' }
      q.push(item)
      write(q)
      return item
    },
    remove(key) { write(read().filter((x) => x.key !== key)) },
    // send(item) -> resolves on success, rejects with {retry: bool}. Stops at the first retryable failure (keeps order).
    async flush(send) {
      const done = []
      for (const item of read()) {
        try {
          const r = await send(item)
          this.remove(item.key)
          done.push({ item, result: r })
        } catch (e) {
          const q = read().map((x) => (x.key === item.key ? { ...x, tries: x.tries + 1, error: e.message || String(e) } : x))
          if (e.retry === false) { write(q.filter((x) => x.key !== item.key)); done.push({ item, error: e.message }); continue }
          write(q)
          break
        }
      }
      return done
    },
    subscribe(f) { listeners.add(f); return () => listeners.delete(f) },
  }
}

export function newKey() {
  if (typeof crypto !== 'undefined' && crypto.randomUUID) return crypto.randomUUID()
  return `k-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`
}

// network failure or a server-side error -> retry later; a 4xx (bad table, sold out) will not succeed on retry
export function isRetryable(err) {
  return err?.network === true || (err?.status >= 500 && err?.status < 600) || err?.status === 0
}

export const queue = makeQueue()
