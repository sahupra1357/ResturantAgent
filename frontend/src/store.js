// One WebSocket store shared by the floor, the online-order page and the dashboard.
// The server sends {type:'snapshot'} on connect, then {type:'batch', events:[...]}. Every event is folded by
// applyEvent; restaurant_ops/live/replay.py `fold` is the Python twin used by the tests.
import { useEffect, useReducer, useRef, useState } from 'react'

export const FEED_SKIP = new Set([
  'clock.tick', 'kitchen.load', 'inventory.snapshot', 'kpi.updated', 'table.updated', 'quote.updated',
  'menu.updated', 'online.order', 'item.fired', 'item.done', 'order.submitted', 'service.started', 'forecast.updated',
  'host.updated', 'check.updated', 'prep.updated', 'safety.updated',
])
const FEED_MAX = 250

export const initialState = {
  connected: false, ready: false, feed: [], decisions: {}, tables: {}, online: { mode: 'open', orders: {} },
  host: { bookings: [], waitlist: [] }, checks: {},
}

function feedEntry(ev) {
  return { seq: ev.seq, type: ev.type, source: ev.source, sim_ts: ev.sim_ts, payload: ev.payload }
}

export function applyEvent(s, ev) {
  const p = ev.payload || {}
  let n = s
  switch (ev.type) {
    case 'service.started':
      n = { ...s, ...p.snapshot, feed: [], ready: true }
      break
    case 'table.updated': {
      const tables = { ...s.tables }
      for (const t of p.tables || []) tables[t.id] = t
      n = { ...s, tables }
      break
    }
    case 'kitchen.load': n = { ...s, kitchen: p }; break
    case 'clock.tick': n = { ...s, clock: p }; break
    case 'inventory.snapshot': n = { ...s, inventory: p }; break
    case 'forecast.updated': n = { ...s, forecast: p }; break
    case 'menu.updated': n = { ...s, menu: p }; break
    case 'online.mode': n = { ...s, online: { ...s.online, mode: p.mode, reason: p.reason } }; break
    case 'online.order':
    case 'quote.updated':
      n = { ...s, online: { ...s.online, orders: { ...s.online.orders, [p.order_id]: p } } }
      break
    case 'decision.proposed':
    case 'decision.resolved':
      n = { ...s, decisions: { ...s.decisions, [p.id]: { ...p, receivedAt: Date.now() } } }
      break
    case 'kpi.updated': n = { ...s, kpis: p }; break
    case 'host.updated': n = { ...s, host: p }; break
    case 'check.updated': n = { ...s, checks: { ...(s.checks || {}), [p.check_id]: p } }; break
    case 'prep.updated': n = { ...s, prep: p }; break
    case 'safety.updated': n = { ...s, safety: p }; break
    case 'service.ended':
      n = { ...s, kpis: p.kpis, service: { ...s.service, status: 'ended' }, forecastError: p.forecast_error }
      break
    default:
  }
  if (!FEED_SKIP.has(ev.type) || (ev.type === 'quote.updated' && p.notify)) {
    n = { ...n, feed: [feedEntry(ev), ...(n.feed || [])].slice(0, FEED_MAX) }
  }
  return n
}

export function reducer(state, msg) {
  switch (msg.type) {
    case 'snapshot':
      return { ...state, ...msg.payload, feed: [...(msg.payload.feed || [])].reverse(), connected: true, ready: true, error: null }
    case 'batch':
      return msg.events.reduce(applyEvent, state)
    case 'error':
      return { ...state, error: msg.payload.message }
    case 'connection':
      return { ...state, connected: msg.connected }
    case 'replay.start':
      return { ...initialState, connected: true, replay: { ...msg.payload, done: false } }
    case 'replay.end':
      return { ...state, replay: { ...state.replay, done: true } }
    default:
      return state
  }
}

function wsUrl(path) {
  const proto = window.location.protocol === 'https:' ? 'wss' : 'ws'
  return `${proto}://${window.location.host}${path}`
}

// mode: {kind:'live'} or {kind:'replay', serviceId, speed}
export function useLiveStore() {
  const [state, dispatch] = useReducer(reducer, initialState)
  const [mode, setMode] = useState({ kind: 'live' })
  const wsRef = useRef(null)

  useEffect(() => {
    let closed = false
    let retry = null
    const connect = () => {
      const path = mode.kind === 'live' ? '/ws' : `/ws/replay/${mode.serviceId}?speed=${mode.speed || 60}`
      const ws = new WebSocket(wsUrl(path))
      wsRef.current = ws
      ws.onopen = () => dispatch({ type: 'connection', connected: true })
      ws.onmessage = (m) => {
        try { dispatch(JSON.parse(m.data)) } catch (e) { console.error(e) }
      }
      ws.onclose = () => {
        dispatch({ type: 'connection', connected: false })
        if (!closed && mode.kind === 'live') retry = setTimeout(connect, 1500)
      }
    }
    connect()
    return () => { closed = true; clearTimeout(retry); wsRef.current && wsRef.current.close() }
  }, [mode])

  return { state, mode, setMode }
}

// Ticks every second so countdowns and elapsed times move between server events.
export function useNow(ms = 1000) {
  const [now, setNow] = useState(Date.now())
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), ms)
    return () => clearInterval(id)
  }, [ms])
  return now
}
