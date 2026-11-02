// Manager alerts for new pending approvals: tab-title badge, a short chime, and a desktop notification.
import { useEffect, useRef, useState } from 'react'

const KEY = 'ro-alerts'

function chime() {
  try {
    const Ctx = window.AudioContext || window.webkitAudioContext
    if (!Ctx) return
    const ctx = new Ctx()
    const o = ctx.createOscillator()
    const g = ctx.createGain()
    o.type = 'sine'
    o.frequency.setValueAtTime(880, ctx.currentTime)
    o.frequency.setValueAtTime(1175, ctx.currentTime + 0.12)
    g.gain.setValueAtTime(0.0001, ctx.currentTime)
    g.gain.exponentialRampToValueAtTime(0.18, ctx.currentTime + 0.02)
    g.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + 0.35)
    o.connect(g).connect(ctx.destination)
    o.start()
    o.stop(ctx.currentTime + 0.4)
  } catch { /* audio blocked until a user gesture: the title badge still works */ }
}

export function newPending(prevIds, decisions) {
  const now = Object.values(decisions || {}).filter((d) => d.status === 'pending')
  return { ids: new Set(now.map((d) => d.id)), fresh: now.filter((d) => !prevIds.has(d.id)) }
}

export function useApprovalAlerts(state, baseTitle = 'Restaurant Ops Live') {
  const [on, setOn] = useState(() => { try { return localStorage.getItem(KEY) !== 'off' } catch { return true } })
  const seen = useRef(new Set())
  const first = useRef(true)
  useEffect(() => {
    const { ids, fresh } = newPending(seen.current, state.decisions)
    seen.current = ids
    document.title = ids.size ? `(${ids.size}) approval${ids.size > 1 ? 's' : ''} · ${baseTitle}` : baseTitle
    if (first.current) { first.current = false; return }
    if (!on || !fresh.length || state.replay) return
    chime()
    if ('Notification' in window && Notification.permission === 'granted' && document.hidden) {
      const d = fresh[0]
      try { new Notification('Approval needed', { body: d.title, tag: `decision-${d.id}` }) } catch { /* ignore */ }
    }
  }, [state.decisions, on, baseTitle, state.replay])
  const toggle = async () => {
    const next = !on
    setOn(next)
    try { localStorage.setItem(KEY, next ? 'on' : 'off') } catch { /* private mode */ }
    if (next && 'Notification' in window && Notification.permission === 'default') {
      try { await Notification.requestPermission() } catch { /* ignore */ }
    }
  }
  return { on, toggle }
}
