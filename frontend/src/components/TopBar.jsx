import { useState } from 'react'
import { api } from '../api.js'

const SPEEDS = [[0, '⏸'], [1, '1x'], [10, '10x'], [60, '60x']]

function NewServiceDialog({ state, onClose, run }) {
  const s = state.service || {}
  const [period, setPeriod] = useState(s.period || 'dinner')
  const [date, setDate] = useState(s.date || '')
  const [speed, setSpeed] = useState(10)
  const [seed, setSeed] = useState('')
  const submit = (e) => {
    e.preventDefault()
    run(api.start({ period, date: date || null, speed: Number(speed), seed: seed === '' ? null : Number(seed) })).then((r) => r && onClose())
  }
  return (
    <div className="modal-back" onClick={onClose}>
      <form className="modal narrow" onClick={(e) => e.stopPropagation()} onSubmit={submit} aria-label="Start a new service">
        <div className="modal-head"><h3>Start a new service</h3><button type="button" className="ghost" onClick={onClose} aria-label="Close">✕</button></div>
        <p className="small muted">The current service is saved first. Setup changes (menu, tables, taxes, roster…) apply from this new service.</p>
        <div className="form-grid">
          <label>Meal period
            <select value={period} onChange={(e) => setPeriod(e.target.value)}>
              <option value="lunch">Lunch</option><option value="dinner">Dinner</option><option value="late">Late</option>
            </select>
          </label>
          <label>Date <input type="date" value={date} onChange={(e) => setDate(e.target.value)} /></label>
          <label>Speed <select value={speed} onChange={(e) => setSpeed(e.target.value)}>{[1, 10, 60].map((v) => <option key={v} value={v}>{v}x</option>)}</select></label>
          <label>Seed <input type="number" placeholder="default" value={seed} onChange={(e) => setSeed(e.target.value)} /></label>
        </div>
        <div className="form-row"><button className="primary" type="submit">Start</button><button type="button" onClick={onClose}>Cancel</button></div>
      </form>
    </div>
  )
}

function UserMenu({ me, onSignOut }) {
  const [open, setOpen] = useState(false)
  const [pw, setPw] = useState({ cur: '', next: '' })
  const [msg, setMsg] = useState('')
  const change = async (e) => {
    e.preventDefault()
    try { await api.changePassword(pw.cur, pw.next); setMsg('Password changed'); setPw({ cur: '', next: '' }) } catch (x) { setMsg(x.message) }
  }
  return (
    <>
      <button onClick={() => setOpen(true)} aria-label="Account">{me.display_name || me.username} · {me.role}</button>
      {open && (
        <div className="modal-back" onClick={() => setOpen(false)}>
          <form className="modal narrow" onClick={(e) => e.stopPropagation()} onSubmit={change}>
            <div className="modal-head"><h3>{me.display_name || me.username}</h3><button type="button" className="ghost" onClick={() => setOpen(false)} aria-label="Close">✕</button></div>
            <p className="small muted">Signed in as <b>{me.username}</b> ({me.role}).</p>
            <label>Current password <input type="password" autoComplete="current-password" value={pw.cur} onChange={(e) => setPw({ ...pw, cur: e.target.value })} /></label>
            <label>New password <input type="password" autoComplete="new-password" value={pw.next} onChange={(e) => setPw({ ...pw, next: e.target.value })} /></label>
            <div className="form-row">
              <button type="submit" disabled={!pw.cur || pw.next.length < 8}>Change password</button>
              <button type="button" className="danger" onClick={onSignOut}>Sign out</button>
            </div>
            {msg && <p className="small">{msg}</p>}
          </form>
        </div>
      )}
    </>
  )
}

export default function TopBar({ state, mode, setMode, alerts, me, onSignOut, layout, setLayout }) {
  const [svcs, setSvcs] = useState(null)
  const [err, setErr] = useState('')
  const [newSvc, setNewSvc] = useState(false)
  const isMgr = me && ['manager', 'admin'].includes(me.role)
  const c = state.clock || {}
  const s = state.service || {}
  const replay = mode.kind === 'replay'
  const run = (p) => p.catch((e) => { setErr(e.message); return null })
  const pending = Object.values(state.decisions || {}).filter((d) => d.status === 'pending').length
  return (
    <header className="topbar">
      <div className="brand">
        <span className="logo" aria-hidden>RO</span>
        <div>
          <b>Restaurant Ops Agents</b>
          <small>{s.weekday} {s.date} · {s.period} · bus {s.bus} · agents {s.llm_mode === 'mock' ? 'rule-based (mock)' : s.llm_mode}</small>
        </div>
      </div>
      <div className="clock">
        <span className="time">{c.hhmm || '--:--'}</span>
        <span className={`dot ${state.connected ? 'on' : ''}`} title={state.connected ? 'connected' : 'disconnected'} />
        {replay ? <span className="pill warn">replay {state.replay?.done ? '(done)' : ''}</span> : (
          <div className="seg" role="group" aria-label="Clock speed">
            {SPEEDS.map(([v, l]) => (
              <button key={v} disabled={!isMgr} className={c.speed === v ? 'on' : ''} onClick={() => run(api.speed(v))} aria-label={v ? `${v}x speed` : 'pause'}>{l}</button>
            ))}
          </div>
        )}
        {s.status === 'ended' && <span className="pill">service ended</span>}
        {pending > 0 && <span className="pill warn">{pending} awaiting approval</span>}
      </div>
      {!replay && isMgr && (
        <div className="scenarios" role="group" aria-label="Inject disruption">
          {(state.scenarios || []).map((x) => (
            <button key={x.name} className="inject" onClick={() => run(api.scenario(x.name))} title={x.label}>
              {{ party_of_12: 'Party of 12', salmon_short: 'Salmon short', cooler_failure: 'Cooler fails', heatwave: 'Heatwave', forecast_miss: 'Forecast miss' }[x.name] || x.name}
            </button>
          ))}
        </div>
      )}
      <div className="svc-actions">
        {replay ? (
          <button onClick={() => setMode({ kind: 'live' })}>Back to live</button>
        ) : (
          <>
            {isMgr && <button onClick={() => setNewSvc(true)}>New service…</button>}
            <button onClick={() => run(api.services().then(setSvcs))}>Replay…</button>
          </>
        )}
        <button onClick={() => setLayout(layout === 'stack' ? 'side' : 'stack')} title="Floor above the dashboard, or side by side"
                aria-label="Toggle layout">{layout === 'stack' ? 'Side by side' : 'Floor on top'}</button>
        <a className="btn" href="#/kds">Kitchen display</a>
        {me?.role === 'admin' && <a className="btn" href="#/admin">Setup</a>}
        <a className="btn" href="#/online" target="_blank" rel="noreferrer">Online page ↗</a>
        <button onClick={alerts.toggle} aria-pressed={alerts.on} title="Chime and desktop notification for new approvals">{alerts.on ? 'Alerts on' : 'Alerts off'}</button>
        {me && <UserMenu me={me} onSignOut={onSignOut} />}
      </div>
      {svcs && (
        <div className="modal-back" onClick={() => setSvcs(null)}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <div className="modal-head"><h3>Replay a logged service</h3><button className="ghost" onClick={() => setSvcs(null)}>✕</button></div>
            <ul className="svc-list">
              {svcs.map((r) => (
                <li key={r.service_id}>
                  <span>{r.service_id}<small className="muted"> · {r.events} events · {r.status}</small></span>
                  <span>
                    {[10, 60, 240].map((sp) => (
                      <button key={sp} onClick={() => { setSvcs(null); setMode({ kind: 'replay', serviceId: r.service_id, speed: sp }) }}>{sp}x</button>
                    ))}
                  </span>
                </li>
              ))}
            </ul>
          </div>
        </div>
      )}
      {newSvc && <NewServiceDialog state={state} onClose={() => setNewSvc(false)} run={run} />}
      {err && <div className="toast" onClick={() => setErr('')}>{err}</div>}
    </header>
  )
}
