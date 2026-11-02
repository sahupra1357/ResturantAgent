import { useEffect, useState } from 'react'
import { api } from '../api.js'
import { STATION_LABEL, money, num } from '../format.js'
import { Panel, useAct } from './ui.jsx'

export function PrepPanel({ state }) {
  const p = state.prep
  if (!p) return null
  const rows = [...(p.dishes || [])].sort((a, b) => (a.prepped - a.forecast_remaining) - (b.prepped - b.forecast_remaining)).slice(0, 10)
  const max = Math.max(8, ...rows.map((r) => Math.max(r.par, r.forecast_remaining)))
  return (
    <Panel title="Prep & staffing" right={<span className="muted small">surge labour {money(p.surge_spent)} / {money(p.surge_budget)}</span>}>
      <div className="cooks">
        {Object.entries(p.cooks || {}).map(([s, n]) => (
          <span key={s} className="pill">{STATION_LABEL[s]} {n} cook{n > 1 ? 's' : ''}{p.extra?.[s] ? ` (+${p.extra[s]} called in)` : ''}</span>
        ))}
        {(p.arriving || []).map((a, i) => <span key={i} className="pill warn">{STATION_LABEL[a.station]} cook arriving {a.at}</span>)}
      </div>
      <div className="portions">
        {rows.map((r) => (
          <div key={r.dish_id} className={`prow ${r.prepped < r.forecast_remaining * 0.5 ? 'short' : ''}`}>
            <span className="pname">{r.name}</span>
            <div className="pbar"><div className="have" style={{ width: `${(r.prepped / max) * 100}%` }} />
              <div className="need" style={{ left: `${Math.min(100, (r.forecast_remaining / max) * 100)}%` }} /></div>
            <span className="pnum">{r.prepped}</span>
          </div>
        ))}
      </div>
      <p className="small muted">Bar = prepped portions (mise en place) · tick = still forecast · {p.scratch_items} items cooked from scratch
        {p.batches_pending?.length ? ` · prep batch ready ${p.batches_pending[0].ready_at}` : ''}</p>
    </Panel>
  )
}

export function SafetyPanel({ state }) {
  const { act, err } = useAct()
  const s = state.safety
  if (!s) return null
  return (
    <Panel title="Food safety (HACCP)" right={s.failed?.length ? <span className="pill bad">unit failed: {s.failed.join(', ')}</span> : <span className="pill ok">units OK</span>}>
      <div className="temps">
        {(s.units || []).map((u) => (
          <div key={u.unit} className={`tile ${u.ok ? '' : 'bad'}`}><span>{u.label}</span><b>{num(u.temp_c, 1)} °C</b><small className="muted">limit {u.limit[0]}…{u.limit[1]} · {u.at}</small></div>
        ))}
      </div>
      <button className="ghost small" onClick={() => act(() => api.logTemps())}>Log temperatures now</button>
      <h4>Checklist</h4>
      <ul className="tight list small">
        {(s.checklist || []).map((c) => (
          <li key={c.id} className={c.overdue ? 'warn' : ''}>
            <span>{c.done_ts ? (c.ok ? '✓' : '✗') : c.overdue ? '!' : '○'} <b>{c.due}</b> {c.item}{c.note ? <small className="muted"> ({c.note}{c.done_by ? `, ${c.done_by}` : ''})</small> : null}</span>
            {!c.done_ts && <button className="ghost" onClick={() => act(() => api.haccp(c.id, true))}>Done</button>}
          </li>
        ))}
      </ul>
      {err && <p className="err small" role="alert">{err}</p>}
    </Panel>
  )
}

export function WastePanel({ state }) {
  const [w, setW] = useState(null)
  const [days, setDays] = useState(30)
  useEffect(() => { api.waste(days).then(setW).catch(() => setW(null)) }, [days, state.kpis?.waste_value])
  const max = Math.max(1, ...((w?.by_reason || []).map((r) => r.value || 0)))
  return (
    <Panel title="Waste analytics" right={
      <select aria-label="Window" value={days} onChange={(e) => setDays(Number(e.target.value))}>{[1, 7, 30].map((d) => <option key={d} value={d}>{d === 1 ? 'tonight' : `${d} days`}</option>)}</select>}>
      {!w && <p className="muted">Loading…</p>}
      {w && (
        <>
          <p className="small">Wasted <b>{money(w.total_value)}</b> · donated <b>{money(w.donated_value)}</b> (not waste)</p>
          <h4>By reason</h4>
          {(w.by_reason || []).map((r) => (
            <div key={r.reason} className="prow"><span className="pname">{r.reason}</span>
              <div className="pbar"><div className="have warnbar" style={{ width: `${((r.value || 0) / max) * 100}%` }} /></div><span className="pnum">{money(r.value)}</span></div>
          ))}
          {!w.by_reason?.length && <p className="muted small">No waste recorded in this window.</p>}
          {w.by_ingredient?.length > 0 && (
            <>
              <h4>Top ingredients</h4>
              <ul className="tight small">{w.by_ingredient.slice(0, 6).map((r) => <li key={r.ingredient_id}>{r.name} ({r.category}) · {money(r.value)}</li>)}</ul>
            </>
          )}
          {w.donations?.length > 0 && (
            <>
              <h4>Donations</h4>
              <ul className="tight small">{w.donations.slice(0, 5).map((d, i) => <li key={i}>{d.date} {d.ingredient_id} {num(d.qty, 2)} → {d.partner} ({money(d.value)})</li>)}</ul>
            </>
          )}
        </>
      )}
    </Panel>
  )
}
