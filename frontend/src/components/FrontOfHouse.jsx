import { useEffect, useState } from 'react'
import { api } from '../api.js'
import { money } from '../format.js'
import { Panel, useAct } from './ui.jsx'

export function HostPanel({ state }) {
  const { act, err, msg } = useAct()
  const [bk, setBk] = useState({ name: '', party_size: 2, time: '19:30', phone: '', notes: '', allow_overbook: false })
  const [wl, setWl] = useState({ name: '', party_size: 2, phone: '' })
  const [quotes, setQuotes] = useState(null)
  const bookings = state.host?.bookings || []
  const waitlist = state.host?.waitlist || []
  const freeTables = Object.values(state.tables || {}).filter((t) => t.state === 'available' && t.shape !== 'bar')
  useEffect(() => { api.waitlist().then((r) => setQuotes(r.quote_for)).catch(() => {}) }, [waitlist.length, state.clock?.hhmm?.slice(0, 4)])
  const upcoming = bookings.filter((b) => b.status === 'booked')
  const done = bookings.filter((b) => b.status !== 'booked')
  return (
    <Panel title="Reservations & waitlist" className="span2" right={<span className="muted small">{upcoming.length} upcoming · {waitlist.length} waiting</span>}>
      <div className="two-col">
        <div>
          <h4>New booking</h4>
          <form className="form-row" onSubmit={(e) => { e.preventDefault(); act(() => api.book({ ...bk, party_size: Number(bk.party_size) }), 'Booked') }}>
            <input aria-label="Guest name" placeholder="Name" required value={bk.name} onChange={(e) => setBk({ ...bk, name: e.target.value })} />
            <input aria-label="Party size" type="number" min="1" max="20" value={bk.party_size} onChange={(e) => setBk({ ...bk, party_size: e.target.value })} />
            <input aria-label="Time" type="time" value={bk.time} onChange={(e) => setBk({ ...bk, time: e.target.value })} />
            <input aria-label="Phone" placeholder="Phone (links profile)" value={bk.phone} onChange={(e) => setBk({ ...bk, phone: e.target.value })} />
            <label className="small"><input type="checkbox" checked={bk.allow_overbook} onChange={(e) => setBk({ ...bk, allow_overbook: e.target.checked })} /> allow overbook (manager)</label>
            <button className="primary" type="submit">Book</button>
          </form>
          <h4>Tonight's book</h4>
          <ul className="tight list">
            {upcoming.slice(0, 14).map((b) => (
              <li key={b.booking_id}>
                <span><b>{b.time}</b> {b.name} · {b.size}{b.guest_id ? ' ★' : ''}{b.overbooked ? <span className="pill warn">overbook</span> : null}</span>
                <span className="btns">
                  <button className="ghost" onClick={() => act(() => api.bookingAction(b.booking_id, 'arrive'))}>Arrived</button>
                  <button className="ghost" onClick={() => act(() => api.bookingAction(b.booking_id, 'no_show'))}>No-show</button>
                  <button className="ghost danger" onClick={() => act(() => api.bookingAction(b.booking_id, 'cancel'))}>Cancel</button>
                </span>
              </li>
            ))}
            {!upcoming.length && <li className="muted">No upcoming bookings.</li>}
          </ul>
          <p className="muted small">{done.filter((b) => b.status === 'seated').length} seated · {done.filter((b) => b.status === 'no_show').length} no-shows · {done.filter((b) => b.status === 'cancelled').length} cancelled</p>
        </div>
        <div>
          <h4>Add to waitlist</h4>
          <form className="form-row" onSubmit={(e) => { e.preventDefault(); act(() => api.addWaitlist({ ...wl, party_size: Number(wl.party_size) }), 'Added') }}>
            <input aria-label="Waitlist name" placeholder="Name" required value={wl.name} onChange={(e) => setWl({ ...wl, name: e.target.value })} />
            <input aria-label="Waitlist party size" type="number" min="1" max="20" value={wl.party_size} onChange={(e) => setWl({ ...wl, party_size: e.target.value })} />
            <input aria-label="Waitlist phone" placeholder="Phone" value={wl.phone} onChange={(e) => setWl({ ...wl, phone: e.target.value })} />
            <button className="primary" type="submit">Add</button>
          </form>
          {quotes && <p className="small muted">Quoted wait now: {Object.entries(quotes).map(([n, m]) => `${n}p ${m} min`).join(' · ')}</p>}
          <ul className="tight list">
            {waitlist.map((w) => (
              <li key={w.party_id}>
                <span><b>{w.position}.</b> {w.name} · {w.size} · waited {w.waited_min}m{w.quoted_min != null ? ` · quoted ${w.quoted_min}m` : ''}</span>
                <span className="btns">
                  <select aria-label={`Table for ${w.name}`} defaultValue="" onChange={(e) => e.target.value && act(() => api.seatWaiting(w.party_id, [e.target.value]))}>
                    <option value="">Seat at…</option>
                    {freeTables.filter((t) => t.seats >= w.size).map((t) => <option key={t.id} value={t.id}>{t.id} ({t.seats})</option>)}
                  </select>
                  <button className="ghost" onClick={() => act(() => api.seatWaiting(w.party_id))}>Best fit</button>
                </span>
              </li>
            ))}
            {!waitlist.length && <li className="muted">Nobody waiting.</li>}
          </ul>
          <TableOps state={state} act={act} />
        </div>
      </div>
      {(err || msg) && <p className={err ? 'err small' : 'ok small'} role="status">{err || msg}</p>}
    </Panel>
  )
}

function TableOps({ state, act }) {
  const [party, setParty] = useState('')
  const [table, setTable] = useState('')
  const parties = {}
  for (const t of Object.values(state.tables || {})) if (t.party_id) (parties[t.party_id] = parties[t.party_id] || []).push(t.id)
  const free = Object.values(state.tables || {}).filter((t) => t.state === 'available').map((t) => t.id)
  const own = parties[party] || []
  return (
    <>
      <h4>Table operations</h4>
      <div className="form-row">
        <select aria-label="Party" value={party} onChange={(e) => setParty(e.target.value)}>
          <option value="">Party…</option>
          {Object.entries(parties).map(([p, ts]) => <option key={p} value={p}>{p} at {ts.join('+')}</option>)}
        </select>
        <select aria-label="Table" value={table} onChange={(e) => setTable(e.target.value)}>
          <option value="">Table…</option>
          {[...own, ...free].map((t) => <option key={t} value={t}>{t}{own.includes(t) ? ' (theirs)' : ''}</option>)}
        </select>
        <button disabled={!party || !table || own.includes(table)} onClick={() => act(() => api.moveParty(party, [table]), 'Moved')}>Move</button>
        <button disabled={!party || !table || own.includes(table)} onClick={() => act(() => api.combine(party, table), 'Combined')}>Combine</button>
        <button disabled={!party || !own.includes(table) || own.length < 2} onClick={() => act(() => api.splitTables(party, table), 'Split')}>Split off</button>
      </div>
    </>
  )
}

export function ChecksPanel({ state }) {
  const { act, err, msg } = useAct()
  const [sel, setSel] = useState(null)
  const [pay, setPay] = useState({ amount: '', tip: '', method: 'card' })
  const [disc, setDisc] = useState({ pct: 10, reason: '' })
  const [ref, setRef] = useState({ amount: '', reason: '' })
  const checks = Object.values(state.checks || {}).sort((a, b) => (a.check_id < b.check_id ? 1 : -1))
  const open = checks.filter((c) => c.status === 'open')
  const c = sel ? state.checks?.[sel] : null
  const k = state.kpis || {}
  return (
    <Panel title="Checks & payments" right={<span className="muted small">{money(k.pay_collected)} collected · {money(k.pay_tips)} tips</span>}>
      <ul className="tight list">
        {checks.slice(0, 10).map((x) => (
          <li key={x.check_id} className={sel === x.check_id ? 'sel' : ''}>
            <button className="linkish" onClick={() => setSel(x.check_id)}>
              <b>{x.check_id}</b> {x.table_ids?.join('+') || 'online'} · {money(x.due)} · <span className={`pill ${x.status === 'open' ? 'warn' : 'ok'}`}>{x.status}</span>
            </button>
          </li>
        ))}
        {!checks.length && <li className="muted">No checks yet.</li>}
      </ul>
      <p className="small muted">{open.length} open</p>
      {c && (
        <div className="check-detail">
          <h4>{c.check_id} · {c.table_ids?.join('+') || 'online'}</h4>
          <table className="grid"><tbody>
            {c.lines.map((l, i) => <tr key={i}><td>{l.name}{l.seat ? <small className="muted"> seat {l.seat}</small> : null}</td><td>{money(l.price)}</td></tr>)}
            <tr><td>Subtotal</td><td>{money(c.subtotal)}</td></tr>
            {c.discounts > 0 && <tr><td>Discounts / comps</td><td>-{money(c.discounts)}</td></tr>}
            {(c.tax_lines || []).length > 0
              ? c.tax_lines.map((t) => <tr key={t.name}><td className="small">{t.name}</td><td>{money(t.amount)}</td></tr>)
              : <tr><td>Tax</td><td>{money(c.tax)}</td></tr>}
            <tr><td><b>Due</b></td><td><b>{money(c.due)}</b></td></tr>
            <tr><td>Paid (+ tips {money(c.tips)})</td><td>{money(c.paid)}</td></tr>
            {c.refunded > 0 && <tr><td>Refunded</td><td>{money(c.refunded)}</td></tr>}
            <tr><td>Balance</td><td>{money(c.balance)}</td></tr>
          </tbody></table>
          {c.splits?.length > 0 && <p className="small">Splits: {c.splits.map((s, i) => <span key={i} className={`pill ${s.paid ? 'ok' : ''}`}>{s.label} {money(s.amount)}</span>)}</p>}
          {c.status === 'open' && (
            <>
              <div className="form-row">
                <button onClick={() => act(() => api.splitCheck(c.check_id, 'even', 2))}>Split 2 ways</button>
                <button onClick={() => act(() => api.splitCheck(c.check_id, 'seat', 0))}>Split by seat</button>
              </div>
              <div className="form-row">
                <input aria-label="Payment amount" placeholder={`amount (${c.balance})`} value={pay.amount} onChange={(e) => setPay({ ...pay, amount: e.target.value })} />
                <input aria-label="Tip" placeholder="tip" value={pay.tip} onChange={(e) => setPay({ ...pay, tip: e.target.value })} />
                <select aria-label="Method" value={pay.method} onChange={(e) => setPay({ ...pay, method: e.target.value })}>{[['card', 'card'], ['cash', 'cash'], ['wallet', 'wallet'], ['gift_card', 'gift card'], ['points', 'loyalty points']].map(([m, l]) => <option key={m} value={m}>{l}</option>)}</select>
                {pay.method === 'gift_card' && <input aria-label="Gift card code" placeholder="gift card code" value={pay.gift_code || ''} onChange={(e) => setPay({ ...pay, gift_code: e.target.value.toUpperCase() })} />}
                <button className="primary" onClick={() => act(() => api.pay(c.check_id, { amount: Number(pay.amount || c.balance), tip: Number(pay.tip || 0), method: pay.method,
                  gift_code: pay.method === 'gift_card' ? pay.gift_code : null }), 'Paid')}>Pay</button>
              </div>
              <div className="form-row">
                <input aria-label="Discount percent" type="number" min="1" max="100" value={disc.pct} onChange={(e) => setDisc({ ...disc, pct: e.target.value })} />
                <input aria-label="Discount reason" placeholder="reason (required)" value={disc.reason} onChange={(e) => setDisc({ ...disc, reason: e.target.value })} />
                <button onClick={() => act(() => api.discount(c.check_id, { pct: Number(disc.pct), reason: disc.reason }), 'Discount applied')}>Discount %</button>
              </div>
            </>
          )}
          {c.paid > 0 && (
            <div className="form-row">
              <input aria-label="Refund amount" placeholder="refund amount" value={ref.amount} onChange={(e) => setRef({ ...ref, amount: e.target.value })} />
              <input aria-label="Refund reason" placeholder="reason (required)" value={ref.reason} onChange={(e) => setRef({ ...ref, reason: e.target.value })} />
              <button className="danger" onClick={() => act(() => api.refund(c.check_id, { amount: Number(ref.amount), reason: ref.reason }), 'Refunded')}>Refund</button>
            </div>
          )}
        </div>
      )}
      {(err || msg) && <p className={err ? 'err small' : 'ok small'} role="status">{err || msg}</p>}
    </Panel>
  )
}

export function GuestsPanel() {
  const { act, err, msg } = useAct()
  const [q, setQ] = useState('')
  const [rows, setRows] = useState([])
  const [g, setG] = useState(null)
  const [allergies, setAllergies] = useState('')
  const [newG, setNewG] = useState({ name: '', phone: '', allergies: '' })
  const search = () => api.guests(q).then(setRows).catch(() => setRows([]))
  useEffect(() => { search() }, [])  // eslint-disable-line react-hooks/exhaustive-deps
  const open = (id) => api.guest(id).then((x) => { setG(x); setAllergies(x.allergies.join(', ')) })
  return (
    <Panel title="Guest profiles & loyalty">
      <div className="form-row">
        <input aria-label="Search guests" placeholder="name or phone" value={q} onChange={(e) => setQ(e.target.value)} onKeyDown={(e) => e.key === 'Enter' && search()} />
        <button onClick={search}>Search</button>
      </div>
      <ul className="tight list">
        {rows.slice(0, 8).map((r) => (
          <li key={r.guest_id}><button className="linkish" onClick={() => open(r.guest_id)}>{r.name} · {r.tier} · {r.points} pts{r.allergies.length ? ` · ⚠ ${r.allergies.join('/')}` : ''}</button></li>
        ))}
      </ul>
      {g && (
        <div className="check-detail">
          <h4>{g.name} <span className="pill">{g.tier}</span></h4>
          <p className="small">{g.phone} · {g.visits} visits · {money(g.total_spend)} spent · {g.points} points{g.preferences ? ` · prefers ${g.preferences}` : ''}</p>
          <div className="form-row">
            <input aria-label="Allergies" value={allergies} onChange={(e) => setAllergies(e.target.value)} placeholder="allergies, e.g. nuts, milk" />
            <button onClick={() => act(() => api.patchGuest(g.guest_id, { allergies }).then((x) => { setG({ ...g, ...x }); return x }), 'Saved: applied to every future order')}>Save allergies</button>
          </div>
          {g.visits_history?.length > 0 && <ul className="tight small">{g.visits_history.slice(0, 5).map((v, i) => <li key={i}>{v.date} · party {v.party_size} · {money(v.spend)}</li>)}</ul>}
        </div>
      )}
      <h4>New guest</h4>
      <form className="form-row" onSubmit={(e) => { e.preventDefault(); act(() => api.createGuest(newG).then((x) => { setG({ ...x, visits_history: [] }); return x }), 'Profile created') }}>
        <input aria-label="New guest name" placeholder="Name" required value={newG.name} onChange={(e) => setNewG({ ...newG, name: e.target.value })} />
        <input aria-label="New guest phone" placeholder="Phone" value={newG.phone} onChange={(e) => setNewG({ ...newG, phone: e.target.value })} />
        <input aria-label="New guest allergies" placeholder="Allergies" value={newG.allergies} onChange={(e) => setNewG({ ...newG, allergies: e.target.value })} />
        <button type="submit">Create</button>
      </form>
      {(err || msg) && <p className={err ? 'err small' : 'ok small'} role="status">{err || msg}</p>}
    </Panel>
  )
}
