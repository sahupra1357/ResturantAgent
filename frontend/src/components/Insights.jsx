import { useEffect, useState } from 'react'
import { api } from '../api.js'
import { money } from '../format.js'
import { Panel } from './ui.jsx'

const CLASS_LABEL = { star: 'Star', plowhorse: 'Plowhorse', puzzle: 'Puzzle', dog: 'Dog' }

export function MenuEngineeringPanel({ state }) {
  const [m, setM] = useState(null)
  useEffect(() => { api.menuEngineering().then(setM).catch(() => setM(null)) }, [state.service?.service_id, Math.floor((state.clock?.t || 0) / 1800)])
  return (
    <Panel title="Menu engineering" className="span2" right={m && <span className="muted small">last {m.days} days + tonight · avg margin {money(m.avg_cm)}</span>}>
      {!m && <p className="muted">Loading…</p>}
      {m && (
        <>
          <table className="grid">
            <thead><tr><th>Dish</th><th>Class</th><th>Mix</th><th>Margin</th><th>Food cost</th><th>Suggestion</th></tr></thead>
            <tbody>{m.dishes.map((d) => (
              <tr key={d.dish_id}>
                <td>{d.name}</td><td><span className={`pill cls-${d.class}`}>{CLASS_LABEL[d.class]}</span></td>
                <td>{d.mix_pct}%</td><td>{money(d.cm)}</td><td>{d.food_cost_pct}%</td>
                <td className="small">{d.suggestion.action === 'price_increase' ? `→ ${money(d.suggestion.new_price)} ` : ''}{d.suggestion.why}</td>
              </tr>
            ))}</tbody>
          </table>
          <p className="small muted">Price suggestions assume elasticity {m.assumed_elasticity}: the history never varies prices, so it cannot be estimated from it. Accepted price changes take effect next service.</p>
          {m.rotation?.length > 0 && <ul className="tight small">{m.rotation.map((r) => <li key={r.dish_id}><b>{r.name}</b> {r.change} season by {r.by}: {r.why}</li>)}</ul>}
        </>
      )}
    </Panel>
  )
}

export function SuppliersPanel({ state }) {
  const [rows, setRows] = useState([])
  const learned = (state.feed || []).filter((e) => e.type === 'supplier.learned').length
  useEffect(() => { api.suppliers().then(setRows).catch(() => setRows([])) }, [learned, state.service?.service_id])
  return (
    <Panel title="Supplier reliability (learned)">
      <table className="grid">
        <thead><tr><th>Supplier</th><th>Lead</th><th>Min order</th><th>Fill rate</th><th>Delay risk</th></tr></thead>
        <tbody>{rows.map((r) => (
          <tr key={r.id}><td>{r.name}</td><td>{r.lead_time_days}d</td><td>{money(r.min_order_value)}</td>
            <td><span className={r.fill_rate < 0.85 ? 'pill bad' : 'pill'}>{Math.round(r.fill_rate * 100)}%</span></td><td>{Math.round(r.delay_prob * 100)}%</td></tr>
        ))}</tbody>
      </table>
      <p className="small muted">Fill rates blend catalog priors with delivery history and move on every shortfall; quotes price that risk in.</p>
    </Panel>
  )
}

export function ReportPanel({ state }) {
  const ended = state.service?.status === 'ended'
  return (
    <Panel title="End-of-service report" right={ended ? <span className="pill ok">service ended</span> : <span className="muted small">live so far</span>}>
      <p className="small">KPIs, payments, sales by dish, every decision and the waste summary. Open the printable page (Print → Save as PDF) or download a CSV.</p>
      <div className="form-row">
        <a className="btn" href="/api/report.html" target="_blank" rel="noreferrer">Open report</a>
        <a className="btn" href="/api/report.csv">Download CSV</a>
        <a className="btn" href="/metrics" target="_blank" rel="noreferrer">Prometheus metrics</a>
      </div>
      <p className="small muted">Report links need a manager or admin sign-in.</p>
    </Panel>
  )
}
