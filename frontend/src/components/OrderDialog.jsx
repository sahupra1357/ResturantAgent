import { useMemo, useState } from 'react'
import { api } from '../api.js'
import { STATE_LABEL, STATION_LABEL } from '../format.js'

const CATS = ['starter', 'main', 'side', 'dessert']

export function availability(state) {
  const portions = state.inventory?.portions || {}
  const menu = state.menu || {}
  const out86 = new Set((menu.eighty_sixed || []).map((x) => x.dish_id))
  const season = new Set(menu.season_off || [])
  const limits = menu.limits || {}
  const res = {}
  for (const [id] of Object.entries(state.dishes || {})) {
    if (season.has(id)) continue
    let n = portions[id] ?? 0
    if (limits[id] != null) n = Math.min(n, limits[id])
    res[id] = out86.has(id) ? 0 : n
  }
  return res
}

export default function OrderDialog({ state, table, onClose }) {
  const [cart, setCart] = useState([])
  const [notes, setNotes] = useState('')
  const [size, setSize] = useState(Math.min(table.seats, 2))
  const [result, setResult] = useState(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [phone, setPhone] = useState('')
  const avail = useMemo(() => availability(state), [state.inventory, state.menu])
  const specials = new Set((state.menu?.specials || []).map((s) => s.dish_id))
  const free = table.state === 'available'

  const add = (id) => setCart([...cart, { dish_id: id, mods: '' }])
  const submit = async () => {
    setBusy(true); setError('')
    try {
      let guest_id = null
      if (phone.trim()) {
        const found = (await api.guests(phone.trim())).find((g) => g.phone === phone.trim())
        guest_id = found ? found.guest_id : (await api.createGuest({ name: `Guest ${phone.trim()}`, phone: phone.trim() })).guest_id
      }
      const r = await api.tableOrder({ table_id: table.id, items: cart, notes, party_size: free ? Number(size) : null, guest_id })
      setResult(r); setCart([])
    } catch (e) { setError(e.message) } finally { setBusy(false) }
  }

  return (
    <div className="modal-back" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()} role="dialog" aria-label={`Order for ${table.id}`}>
        <div className="modal-head">
          <h3>{table.id} · {table.seats} seats · {STATE_LABEL[table.state]}</h3>
          <button className="ghost" onClick={onClose} aria-label="Close">✕</button>
        </div>
        {table.party_id && <p className="muted">Party of {table.party_size} ({table.party_kind}) · server {table.section}</p>}
        {free && (
          <label className="row">Party size <input type="number" min="1" max={table.seats} value={size} onChange={(e) => setSize(e.target.value)} /></label>
        )}
        <div className="menu-grid">
          {CATS.map((c) => (
            <div key={c}>
              <h4>{c}s</h4>
              {Object.values(state.dishes || {}).filter((d) => d.category === c && avail[d.dish_id] !== undefined).map((d) => (
                <button key={d.dish_id} className="dish" disabled={!avail[d.dish_id]} onClick={() => add(d.dish_id)}
                        title={`${STATION_LABEL[d.station]} · ${d.cook_min} min · ${d.allergens.join(', ') || 'no listed allergens'}`}>
                  <span>{d.name}{specials.has(d.dish_id) && <em className="tag special">special</em>}</span>
                  <small>{avail[d.dish_id] ? (avail[d.dish_id] <= 4 ? `last ${avail[d.dish_id]}` : `$${d.price}`) : "86'd"}</small>
                </button>
              ))}
            </div>
          ))}
        </div>
        {cart.length > 0 && (
          <div className="cart">
            {cart.map((it, i) => (
              <div className="cart-row" key={i}>
                <span>{state.dishes[it.dish_id].name}</span>
                <input aria-label={`Mods for ${state.dishes[it.dish_id].name}`} placeholder="mods, e.g. no dairy" value={it.mods}
                       onChange={(e) => setCart(cart.map((x, j) => (j === i ? { ...x, mods: e.target.value } : x)))} />
                <button className="ghost" onClick={() => setCart(cart.filter((_, j) => j !== i))} aria-label="Remove">✕</button>
              </div>
            ))}
            <input className="wide" aria-label="Table notes" placeholder="Table notes (e.g. nut allergy)" value={notes} onChange={(e) => setNotes(e.target.value)} />
            <input className="wide" aria-label="Guest phone" placeholder="Guest phone (applies saved allergies, earns points)" value={phone} onChange={(e) => setPhone(e.target.value)} />
            <button className="primary" disabled={busy} onClick={submit}>Send to kitchen</button>
          </div>
        )}
        {error && <p className="err">{error}</p>}
        {result && (
          <div className="result">
            <b>{result.order_id}</b> routed in {result.latency_ms} ms
            <ul>
              {result.items.map((i) => (
                <li key={i.item_id}>{i.name} → {STATION_LABEL[i.station]} at {i.fire_at.slice(11, 16)}{i.mods?.length ? ` · ${i.mods.join('; ')}` : ''}</li>
              ))}
              {result.held.map((h) => <li key={h.item_id} className="warn">Held: {state.dishes[h.dish_id].name} ({h.conflict.join('/')}) → manager</li>)}
              {result.rejected.map((r, k) => <li key={k} className="err">{state.dishes[r.dish_id]?.name || r.dish_id}: {r.reason}</li>)}
            </ul>
          </div>
        )}
      </div>
    </div>
  )
}
