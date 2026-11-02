import { useEffect, useState } from 'react'
import { api } from '../api.js'
import { AGENT_LABEL, STATION_LABEL, describe, hhmm, money, num } from '../format.js'
import { useNow } from '../store.js'
import { ChecksPanel, GuestsPanel, HostPanel } from './FrontOfHouse.jsx'
import { MenuEngineeringPanel, ReportPanel, SuppliersPanel } from './Insights.jsx'
import { PrepPanel, SafetyPanel, WastePanel } from './KitchenOps.jsx'
import { AuditSection, BackupsPanel, DayClosePanel, GiftCardPanel, LlmPanel, PoOutboxPanel } from './BackOffice.jsx'
import { Panel } from './ui.jsx'


const STATUS_CLASS = { pending: 'warn', executed: 'ok', approved: 'ok', rejected: 'bad', blocked: 'bad', timeout: 'muted', failed: 'bad', auto: 'ok' }

function Trace({ d }) {
  return (
    <div className="trace">
      <p><b>Reason:</b> {d.reason}</p>
      {d.violations?.length > 0 && (
        <ul className="viol">{d.violations.map((v, i) => <li key={i} className={v.severity}>{v.severity}: {v.code} - {v.message}</li>)}</ul>
      )}
      {d.trace?.length > 0 && (
        <ol className="calls">
          {d.trace.map((t, i) => (
            <li key={i}><code>{t.tool}({Object.entries(t.args || {}).map(([k, v]) => `${k}=${JSON.stringify(v)}`).join(', ')})</code>
              <pre>{JSON.stringify(t.result, null, 1).slice(0, 700)}</pre></li>
          ))}
        </ol>
      )}
      <p className="muted small">payload: <code>{JSON.stringify(d.payload)}</code></p>
      {d.result && <p className="muted small">result: <code>{JSON.stringify(d.result)}</code></p>}
    </div>
  )
}

export function DecisionFeed({ state }) {
  const [open, setOpen] = useState(null)
  const [filter, setFilter] = useState('all')
  const ds = Object.values(state.decisions || {}).sort((a, b) => b.id - a.id)
    .filter((d) => filter === 'all' || d.agent === filter)
  return (
    <Panel title="Live decision feed" className="span2" right={
      <select value={filter} onChange={(e) => setFilter(e.target.value)} aria-label="Filter by agent">
        <option value="all">all agents</option>
        {['order', 'inventory', 'forecasting', 'procurement', 'menu'].map((a) => <option key={a} value={a}>{AGENT_LABEL[a]}</option>)}
      </select>}>
      {ds.length === 0 && <p className="muted">No decisions yet.</p>}
      <ul className="decisions">
        {ds.slice(0, 80).map((d) => (
          <li key={d.id} className={open === d.id ? 'open' : ''}>
            <button className="drow" onClick={() => setOpen(open === d.id ? null : d.id)}>
              <span className="dtime">{hhmm(d.created_sim)}</span>
              <span className={`agent a-${d.agent}`}>{AGENT_LABEL[d.agent]}</span>
              <span className="dtitle">{d.title}</span>
              <span className={`pill ${STATUS_CLASS[d.status] || ''}`}>{d.status === 'executed' && !d.needs_approval ? 'auto' : d.status}</span>
            </button>
            <div className="dsub">
              <span>{(d.tools || []).join(', ') || 'rules'}</span>
              <span>conf {num((d.confidence || 0) * 100)}%</span>
              <span>{d.mode === 'llm' ? 'LLM-reviewed' : 'rule-based'}</span>
              {d.merged > 0 && <span>+{d.merged} merged</span>}
            </div>
            {open === d.id && <Trace d={d} />}
          </li>
        ))}
      </ul>
    </Panel>
  )
}

function ApprovalCard({ d, state }) {
  const now = useNow(500)
  const [reason, setReason] = useState('')
  const [edit, setEdit] = useState(false)
  const [payload, setPayload] = useState(JSON.stringify(d.payload, null, 1))
  const [err, setErr] = useState('')
  const speed = state.clock?.speed || 0
  // the timeout runs in service minutes (frozen while paused); estimate between server updates
  const leftMin = Math.max(0, (d.remaining_min ?? d.timeout_min) - (speed ? ((now - (d.receivedAt || now)) / 1000) * speed / 60 : 0))
  const leftWall = speed ? Math.ceil((leftMin * 60) / speed) : null
  const act = async (approved) => {
    setErr('')
    try {
      let p = null
      if (edit && approved) p = JSON.parse(payload)
      if (!approved && !reason.trim()) { setErr('Give a reason: it goes back to the agent'); return }
      await api.resolve(d.id, { approved, note: reason, payload: p })
    } catch (e) { setErr(e.message) }
  }
  const alts = d.action_type === 'allergen_swap' ? d.payload.alternatives || [] : []
  return (
    <li className="approval">
      <div className="ap-head">
        <span className={`agent a-${d.agent}`}>{AGENT_LABEL[d.agent]}</span>
        <span className="ap-timer" title={`then: ${d.timeout_default === 'approve' ? 'approve' : 'no action'}`}>
          {Math.ceil(leftMin)} service min{leftWall != null ? ` (~${leftWall}s)` : ' (paused)'} → {d.timeout_default === 'approve' ? 'auto-approve' : 'no action'}
        </span>
      </div>
      <p className="ap-title">{d.title}</p>
      <p className="muted small">{d.reason}</p>
      {d.violations?.filter((v) => v.severity === 'warn').map((v, i) => <p key={i} className="warn small">⚠ {v.message}</p>)}
      {alts.length > 0 && (
        <label className="row small">Swap to
          <select value={JSON.parse(payload).to_dish || ''} onChange={(e) => { setEdit(true); setPayload(JSON.stringify({ ...JSON.parse(payload), to_dish: e.target.value || null })) }}>
            {alts.map((a) => <option key={a} value={a}>{state.dishes?.[a]?.name || a}</option>)}
            <option value="">drop the item</option>
          </select>
        </label>
      )}
      {edit && alts.length === 0 && <textarea rows={5} value={payload} onChange={(e) => setPayload(e.target.value)} aria-label="Edit payload" />}
      <input placeholder="Reason (required to reject; sent to the agent)" value={reason} onChange={(e) => setReason(e.target.value)} />
      <div className="ap-actions">
        <button className="primary" onClick={() => act(true)}>{edit ? 'Edit & approve' : 'Approve'}</button>
        <button className="danger" onClick={() => act(false)}>Reject</button>
        {alts.length === 0 && <button className="ghost" onClick={() => setEdit(!edit)}>{edit ? 'Cancel edit' : 'Edit'}</button>}
      </div>
      {err && <p className="err small">{err}</p>}
    </li>
  )
}

export function ApprovalQueue({ state }) {
  const pending = Object.values(state.decisions || {}).filter((d) => d.status === 'pending').sort((a, b) => a.id - b.id)
  return (
    <Panel title="Approval queue" right={<span className={`count ${pending.length ? 'hot' : ''}`}>{pending.length}</span>}>
      {pending.length === 0 && <p className="muted">Nothing needs you right now.</p>}
      <ul className="approvals">{pending.map((d) => <ApprovalCard key={d.id} d={d} state={state} />)}</ul>
    </Panel>
  )
}

export function KitchenPanel({ state }) {
  const k = state.kitchen
  const mode = state.online?.mode
  if (!k) return null
  return (
    <Panel title="Kitchen load" right={<span className={`pill busy-${k.busyness}`}>{k.busyness}</span>}>
      <table className="grid">
        <thead><tr><th>Station</th><th>Cooks</th><th>Queue</th><th>Wait</th><th>Load</th></tr></thead>
        <tbody>{k.stations.map((s) => (
          <tr key={s.station}><td>{STATION_LABEL[s.station]}</td><td>{s.cooks}</td><td>{s.queue_depth}</td><td>{s.wait_min} min</td>
            <td><div className="mini-bar"><div style={{ width: `${Math.min(100, s.utilisation * 100)}%` }} className={s.utilisation >= 0.9 ? 'hot' : ''} /></div></td></tr>
        ))}</tbody>
      </table>
      <p className="small muted">{k.explanation} · {num(k.expected_next_30)} covers expected next 30 min</p>
      <div className="seg" role="group" aria-label="Online ordering">
        {['open', 'quick_only', 'paused'].map((m) => (
          <button key={m} className={mode === m ? 'on' : ''} onClick={() => api.onlineMode(m)}>{m === 'open' ? 'Online open' : m === 'quick_only' ? 'Quick items only' : 'Pause online'}</button>
        ))}
      </div>
    </Panel>
  )
}

export function OnlineOrdersPanel({ state }) {
  const [ov, setOv] = useState({})
  const orders = Object.values(state.online?.orders || {}).filter((o) => o.status !== 'picked_up' && o.status !== 'delivered').sort((a, b) => (a.order_id < b.order_id ? 1 : -1))
  const done = Object.values(state.online?.orders || {}).filter((o) => o.status === 'picked_up' || o.status === 'delivered').length
  return (
    <Panel title="Online orders" right={<span className="muted small">{orders.length} active · {done} done</span>}>
      {orders.length === 0 && <p className="muted">No active online orders.</p>}
      <table className="grid">
        <thead><tr><th>Order</th><th>Items</th><th>Quoted</th><th>Now</th><th></th></tr></thead>
        <tbody>{orders.slice(0, 14).map((o) => (
          <tr key={o.order_id} className={o.late ? 'late' : ''}>
            <td><b>{o.order_id}</b><br /><small className="muted">{o.customer}</small></td>
            <td className="small">{o.items.map((i) => i.name).join(', ')}<br /><span className="muted">{o.fulfilment === 'delivery' ? `delivery · zone ${o.zone}${o.driver_id ? ` · ${o.driver_id}` : ''}` : 'pickup'}</span></td>
            <td>{o.quoted_hhmm.join('–')}</td>
            <td>{o.status === 'cooking' ? o.current_hhmm.join('–') : o.status === 'out_for_delivery' ? `on the way, ${o.eta_hhmm}` : o.status.replaceAll('_', ' ')}{o.late && <span className="pill bad">late {o.late_by_min}m</span>}{o.override && <span className="pill">override</span>}</td>
            <td className="actions">
              {o.status === 'cooking' && <>
                <input size={5} placeholder="HH:MM" value={ov[o.order_id] || ''} onChange={(e) => setOv({ ...ov, [o.order_id]: e.target.value })} aria-label="Override ready time" />
                <button className="ghost" disabled={!/^\d\d:\d\d$/.test(ov[o.order_id] || '')} onClick={() => api.override(o.order_id, ov[o.order_id])}>Set</button>
                <button className="ghost" onClick={() => api.notify(o.order_id, '')}>Notify</button>
              </>}
            </td>
          </tr>
        ))}</tbody>
      </table>
    </Panel>
  )
}

export function InventoryPanel({ state }) {
  const inv = state.inventory
  if (!inv) return null
  const dishes = Object.entries(inv.portions || {}).map(([id, n]) => ({ id, n, fc: inv.forecast_remaining?.[id] || 0 }))
    .sort((a, b) => (a.n - a.fc) - (b.n - b.fc)).slice(0, 12)
  const max = Math.max(10, ...dishes.map((d) => Math.max(d.n, d.fc)))
  return (
    <Panel title="Inventory" right={<span className="muted small">{inv.quarantined ? `${inv.quarantined} lots quarantined · ` : ''}{Object.keys(inv.shortages || {}).length ? 'supplier shortfall' : ''}</span>}>
      <div className="portions">
        {dishes.map((d) => (
          <div key={d.id} className={`prow ${d.n < d.fc ? 'short' : ''}`}>
            <span className="pname">{state.dishes?.[d.id]?.name}</span>
            <div className="pbar">
              <div className="have" style={{ width: `${(d.n / max) * 100}%` }} />
              <div className="need" style={{ left: `${Math.min(100, (d.fc / max) * 100)}%` }} title={`forecast ${num(d.fc)}`} />
            </div>
            <span className="pnum">{d.n}</span>
          </div>
        ))}
      </div>
      <p className="small muted">Bar = portions left · tick = still forecast tonight</p>
      {inv.expiring?.length > 0 && (
        <>
          <h4>Expiring within 24 h</h4>
          <ul className="small tight">{inv.expiring.slice(0, 6).map((l) => (
            <li key={l.lot_id}>{l.ingredient_id.replace('_', ' ')} {l.qty} {l.unit} · {money(l.value)} · {l.days_left === 0 ? 'today' : 'tomorrow'}{l.quarantined ? ' · quarantined' : ''}</li>
          ))}</ul>
        </>
      )}
    </Panel>
  )
}

function CoversChart({ f, clockT }) {
  if (!f?.curve?.length) return null
  const W = 360, H = 150, P = 26
  const maxT = f.curve[f.curve.length - 1].t_min
  const maxY = Math.max(f.base?.p90 || 0, f.revised?.p90 || 0, ...(f.history || []).map((h) => h.seated)) * 1.08 || 10
  const x = (t) => P + (t / maxT) * (W - P - 8)
  const y = (v) => H - 18 - (v / maxY) * (H - 30)
  const band = f.curve.map((c) => `${x(c.t_min)},${y(c.p90)}`).join(' ') + ' ' + [...f.curve].reverse().map((c) => `${x(c.t_min)},${y(c.p10)}`).join(' ')
  const mid = f.curve.map((c) => `${x(c.t_min)},${y(c.p50)}`).join(' ')
  const act = (f.history || []).map((h) => `${x(h.t_min)},${y(h.seated)}`).join(' ')
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="chart" aria-label="Covers so far vs forecast band">
      <polygon points={band} className="band" />
      <polyline points={mid} className="mid" />
      {act && <polyline points={act} className="act" />}
      <line x1={x(Math.min(maxT, clockT / 60))} x2={x(Math.min(maxT, clockT / 60))} y1={8} y2={H - 18} className="now" />
      {[0, 60, 120, 180, 240, 300].filter((t) => t <= maxT).map((t) => <text key={t} x={x(t)} y={H - 4} className="ax">{17 + t / 60}:00</text>)}
      <text x={P} y={12} className="ax">{Math.round(maxY)}</text>
    </svg>
  )
}

export function ForecastPanel({ state }) {
  const f = state.forecast
  if (!f) return null
  const t = f.tomorrow_period
  return (
    <Panel title="Forecast" right={<span className="muted small">model P50 {num(f.base?.p50)} · now {num(f.revised?.p50)}</span>}>
      <CoversChart f={f} clockT={state.clock?.t || 0} />
      <p className="small">Covers so far <b>{f.revised?.seated ?? state.kpis?.covers ?? 0}</b> vs {num(f.revised?.expected_so_far)} expected · tonight {num(f.revised?.p10)}–{num(f.revised?.p90)}
        {f.revised?.miss_pct ? <span className={Math.abs(f.revised.miss_pct) >= 15 ? 'pill warn' : 'pill'}>{f.revised.miss_pct > 0 ? '+' : ''}{f.revised.miss_pct}%</span> : null}</p>
      {t && <p className="small">Tomorrow ({t.weekday}) {t.meal_period}: <b>{num(t.p10)}–{num(t.p90)}</b> covers (P50 {num(t.p50)})</p>}
      <ul className="small tight">{(f.all_signals || []).map((s) => (
        <li key={s.signal} className={s.significant ? '' : 'muted'}>{s.signal.replaceAll('_', ' ')}: {s.effect_pct > 0 ? '+' : ''}{num(s.effect_pct, 1)}% {s.significant ? '(significant)' : '(no effect)'}</li>
      ))}</ul>
    </Panel>
  )
}

export function KpiPanel({ state }) {
  const k = state.kpis || {}
  const tiles = [
    ['Covers', k.covers], ['Revenue', money(k.revenue)], ['Ticket avg', k.ticket_avg_min != null ? `${k.ticket_avg_min} min` : '–'],
    ['Late tickets', k.late_tickets], ['Quote accuracy', k.quote_accuracy_pct != null ? `${k.quote_accuracy_pct}%` : '–'],
    ['Order→station p95', k.order_latency_p95_ms != null ? `${k.order_latency_p95_ms} ms` : '–'],
    ['86 count', `${k.eighty_six ?? 0} (${k.eighty_six_caught ?? 0} caught)`], ['Stockouts', k.stockouts],
    ['Waste', money(k.waste_value)], ['Food cost', k.food_cost_pct != null ? `${k.food_cost_pct}%` : '–'],
    ['Auto / approval', `${k.decisions_auto ?? 0} / ${k.decisions_approval ?? 0}`], ['Comps', money(k.comps)],
    ['Collected', money(k.pay_collected)], ['Tips', money(k.pay_tips)], ['Delivery on time', k.delivery_accuracy_pct != null ? `${k.delivery_accuracy_pct}%` : '–'],
    ['Waitlist', k.waitlist ?? 0], ['No-shows', k.no_shows ?? 0], ['Surge labour', money(k.surge_labor)],
  ]
  return (
    <Panel title="Service KPIs" className="span2">
      <div className="tiles">{tiles.map(([l, v]) => <div className="tile" key={l}><span>{l}</span><b>{v ?? '–'}</b></div>)}</div>
      {state.forecastError && <p className="small muted">Forecast error tonight: {state.forecastError.ape_pct}% (P50 {state.forecastError.forecast_p50} vs {state.forecastError.actual} actual)</p>}
    </Panel>
  )
}

export function ActivityPanel({ state }) {
  return (
    <Panel title="Event stream" className="span2">
      <ul className="activity">
        {(state.feed || []).slice(0, 60).map((e) => (
          <li key={e.seq}><span className="dtime">{hhmm(e.sim_ts)}</span><span className={`src s-${e.source}`}>{e.source}</span>{describe(e, state)}</li>
        ))}
      </ul>
    </Panel>
  )
}

export function BaselinePanel() {
  const [b, setB] = useState(null)
  useEffect(() => { api.baseline().then(setB).catch(() => setB({ error: 'unavailable' })) }, [])
  const c = b?.comparison
  return (
    <Panel title="Agents vs fixed-par baseline (7-day sim)">
      {!b && <p className="muted">Loading…</p>}
      {b && !c && <p className="muted small">{b.hint || b.error}</p>}
      {c && (
        <table className="grid">
          <thead><tr><th>KPI</th><th>Agents</th><th>Baseline</th></tr></thead>
          <tbody>{['waste_value', 'stockouts', 'food_cost_pct', 'revenue'].map((key) => (
            <tr key={key}><td>{key.replaceAll('_', ' ')}</td><td>{num(c.agent?.[key], 1)}</td><td>{num(c.baseline?.[key], 1)}</td></tr>
          ))}</tbody>
        </table>
      )}
    </Panel>
  )
}

const TABS = [
  ['ops', 'Operations'], ['foh', 'Front of house'], ['kitchen', 'Kitchen & safety'], ['insights', 'Insights'], ['office', 'Back office'],
]

export default function Dashboard({ state, me }) {
  const [tab, setTab] = useState(() => { try { return localStorage.getItem('ro-tab') || 'ops' } catch { return 'ops' } })
  const pick = (t) => { setTab(t); try { localStorage.setItem('ro-tab', t) } catch { /* private mode */ } }
  const pending = Object.values(state.decisions || {}).filter((d) => d.status === 'pending').length
  return (
    <>
      <div className="tabs dash-tabs" role="tablist" aria-label="Dashboard sections">
        {TABS.map(([k, label]) => (
          <button key={k} role="tab" aria-selected={tab === k} className={tab === k ? 'on' : ''} onClick={() => pick(k)}>
            {label}{k === 'ops' && pending > 0 ? <span className="count hot">{pending}</span> : null}
          </button>
        ))}
      </div>
      <div className="dash" role="tabpanel">
        {tab === 'ops' && <>
          <ApprovalQueue state={state} />
          <KitchenPanel state={state} />
          <DecisionFeed state={state} />
          <OnlineOrdersPanel state={state} />
          <InventoryPanel state={state} />
          <KpiPanel state={state} />
          <ActivityPanel state={state} />
        </>}
        {tab === 'foh' && <>
          <HostPanel state={state} />
          <ChecksPanel state={state} />
          <GuestsPanel />
        </>}
        {tab === 'kitchen' && <>
          <PrepPanel state={state} />
          <SafetyPanel state={state} />
          <InventoryPanel state={state} />
          <WastePanel state={state} />
        </>}
        {tab === 'insights' && <>
          <ForecastPanel state={state} />
          <ReportPanel state={state} />
          <MenuEngineeringPanel state={state} />
          <SuppliersPanel state={state} />
          <BaselinePanel />
          <LlmPanel state={state} />
        </>}
        {tab === 'office' && (['manager', 'admin'].includes(me?.role) ? <>
          <DayClosePanel state={state} />
          <GiftCardPanel />
          <PoOutboxPanel state={state} />
          <BackupsPanel me={me} />
          <LlmPanel state={state} />
          <AuditSection />
        </> : <p className="muted">The back office needs a manager or admin account.</p>)}
      </div>
    </>
  )
}
