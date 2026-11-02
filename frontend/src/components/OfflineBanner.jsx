import { useEffect, useState } from 'react'
import { flushQueue } from '../api.js'
import { queue } from '../offline.js'

// Shows the connection state and the offline order queue; flushes when the browser comes back online.
export default function OfflineBanner({ connected }) {
  const [items, setItems] = useState(queue.list())
  const [online, setOnline] = useState(typeof navigator === 'undefined' ? true : navigator.onLine)
  const [msg, setMsg] = useState('')
  useEffect(() => queue.subscribe(setItems), [])
  useEffect(() => {
    const up = () => { setOnline(true); flushQueue().then(report).catch(() => {}) }
    const down = () => setOnline(false)
    window.addEventListener('online', up)
    window.addEventListener('offline', down)
    const id = setInterval(() => { if (queue.size()) flushQueue().then(report).catch(() => {}) }, 10000)
    return () => { window.removeEventListener('online', up); window.removeEventListener('offline', down); clearInterval(id) }
  }, [])
  const report = (done) => {
    if (!done?.length) return
    const sent = done.filter((d) => !d.error).length
    const failed = done.filter((d) => d.error)
    setMsg(`${sent} offline order(s) sent${failed.length ? `; ${failed.length} rejected: ${failed.map((f) => `${f.item.label} (${f.error})`).join(', ')}` : ''}`)
  }
  if (connected && online && !items.length && !msg) return null
  return (
    <div className={`offline-banner ${!connected || !online ? 'down' : ''}`} role="status" aria-live="polite">
      {(!connected || !online) && <b>Offline: reconnecting… Table orders are saved on this device and sent when the connection is back.</b>}
      {items.length > 0 && (
        <span> {items.length} order(s) waiting: {items.map((i) => i.label || i.key.slice(0, 6)).join(', ')}
          <button className="ghost small" onClick={() => flushQueue().then(report)}>Send now</button></span>
      )}
      {msg && <span> {msg} <button className="ghost small" onClick={() => setMsg('')} aria-label="Dismiss">✕</button></span>}
    </div>
  )
}
