import { useEffect, useMemo, useRef, useState } from 'react'
import { api } from '../api.js'
import { availability } from './OrderDialog.jsx'

const CATS = ['main', 'starter', 'side', 'dessert']

// Customer-facing online order page: live ready-time range before checkout, live updates after.
export default function OnlinePage({ state, standalone = false }) {
  const [cart, setCart] = useState({})
  const [quote, setQuote] = useState(null)
  const [mine, setMine] = useState(() => { try { return JSON.parse(localStorage.getItem('ro-my-orders') || '[]') } catch { return [] } })
  const [error, setError] = useState('')
  const [name, setName] = useState('')
  const mode = state.online?.mode || 'open'
  const avail = useMemo(() => availability(state), [state.inventory, state.menu])
  const out86 = new Set((state.menu?.eighty_sixed || []).map((x) => x.dish_id))
  const lastN = state.menu?.last_n || {}
  const specials = new Set((state.menu?.specials || []).map((s) => s.dish_id))
  const ids = Object.entries(cart).flatMap(([id, n]) => Array(n).fill(id))
  const key = ids.join(',')
  const timer = useRef(null)

  const allowed = (d) => avail[d.dish_id] > 0 && mode !== 'paused' && (mode !== 'quick_only' || d.quick)

  useEffect(() => {
    if (!ids.length) { setQuote(null); return }
    clearTimeout(timer.current)
    timer.current = setTimeout(() => api.quote(ids).then(setQuote).catch(() => {}), 250)
    return () => clearTimeout(timer.current)
  }, [key, state.kitchen?.score, state.clock?.hhmm])

  useEffect(() => { try { localStorage.setItem('ro-my-orders', JSON.stringify(mine.slice(-6))) } catch { /* private mode */ } }, [mine])

  const inc = (id, by) => {
    const n = Math.max(0, (cart[id] || 0) + by)
    const max = lastN[id] ? 1 : 6
    setCart({ ...cart, [id]: Math.min(n, max) })
  }
  const checkout = async () => {
    setError('')
    try {
      const r = await api.onlineOrder({ items: Object.entries(cart).filter(([, n]) => n > 0).map(([dish_id, qty]) => ({ dish_id, qty })), customer: name || 'Web guest' })
      setMine([...mine, r.order_id]); setCart({})
    } catch (e) { setError(e.message) }
  }
  const total = ids.reduce((a, id) => a + (state.dishes?.[id]?.price || 0), 0)
  const orders = mine.map((id) => state.online?.orders?.[id]).filter(Boolean).reverse()

  return (
    <div className={`online ${standalone ? 'standalone' : ''}`}>
      <div className="online-head">
        <div>
          <h2>Bistro 80 · order for pickup</h2>
          <p className="muted">Kitchen right now: <b className={`busy-${state.kitchen?.busyness}`}>{state.kitchen?.busyness || '…'}</b></p>
        </div>
        {mode !== 'open' && <div className={`banner ${mode}`}>{mode === 'paused' ? 'Online ordering is paused while the kitchen catches up' : 'Kitchen is busy: quick items only'}</div>}
      </div>
      <div className="online-body">
        <div className="online-menu">
          {CATS.map((c) => (
            <section key={c}>
              <h4>{c}s</h4>
              {Object.values(state.dishes || {}).filter((d) => d.category === c && avail[d.dish_id] !== undefined && !out86.has(d.dish_id)).map((d) => (
                <div key={d.dish_id} className={`omenu-row ${allowed(d) ? '' : 'off'}`}>
                  <div>
                    <span className="oname">{d.name}</span>
                    {specials.has(d.dish_id) && <em className="tag special">tonight's special</em>}
                    {lastN[d.dish_id] && <em className="tag last">last {lastN[d.dish_id]}</em>}
                    {mode === 'quick_only' && !d.quick && <em className="tag">not available right now</em>}
                    <small className="muted"> ${d.price}</small>
                  </div>
                  <div className="qty">
                    <button onClick={() => inc(d.dish_id, -1)} disabled={!cart[d.dish_id]} aria-label="less">−</button>
                    <span>{cart[d.dish_id] || 0}</span>
                    <button onClick={() => inc(d.dish_id, 1)} disabled={!allowed(d)} aria-label="more">+</button>
                  </div>
                </div>
              ))}
            </section>
          ))}
        </div>
        <aside className="online-side">
          <div className="checkout">
            <h4>Your order</h4>
            {!ids.length && <p className="muted">Add something to see a ready time.</p>}
            {ids.length > 0 && (
              <>
                <div className="quote-big">ready {quote ? `${quote.range_hhmm[0]}–${quote.range_hhmm[1]}` : '…'}</div>
                {quote && <p className="muted small">{quote.explanation}</p>}
                <input placeholder="Name for pickup" value={name} onChange={(e) => setName(e.target.value)} />
                <button className="primary" onClick={checkout} disabled={mode === 'paused'}>Place order · ${total.toFixed(2)}</button>
              </>
            )}
            {error && <p className="err">{error}</p>}
          </div>
          {orders.map((o) => (
            <div key={o.order_id} className={`track ${o.late ? 'late' : ''} ${o.status}`}>
              <div className="track-head"><b>{o.order_id}</b><span className="pill">{o.status.replace('_', ' ')}</span></div>
              <div className="quote-big">{o.status === 'cooking' ? `${o.current_hhmm[0]}–${o.current_hhmm[1]}` : o.status === 'ready' ? 'Ready!' : 'Picked up'}</div>
              <p className="muted small">quoted {o.quoted_hhmm[0]}–{o.quoted_hhmm[1]}{o.late ? ` · ${o.late_by_min} min late` : ''}</p>
              <ul className="notes">{o.notifications.slice().reverse().map((n, i) => <li key={i}><b>{n.at}</b> {n.text}</li>)}</ul>
            </div>
          ))}
        </aside>
      </div>
    </div>
  )
}
