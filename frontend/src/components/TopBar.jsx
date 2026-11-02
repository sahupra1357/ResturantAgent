import { useEffect, useState } from 'react'
import { api, getToken, setToken } from '../api.js'

const SPEEDS = [[0, '⏸'], [1, '1x'], [10, '10x'], [60, '60x']]

function SignIn({ role, setRole }) {
  const [open, setOpen] = useState(false)
  const [tok, setTok] = useState(getToken())
  const [err, setErr] = useState('')
  const save = async (e) => {
    e.preventDefault()
    setToken(tok.trim())
    try {
      const r = await api.whoami()
      if (tok.trim() && !r.role) { setErr('Token not recognised'); return }
      setRole(r.role)
      setOpen(false)
      setErr('')
    } catch (x) { setErr(x.message) }
  }
  return (
    <>
      <button onClick={() => setOpen(true)} aria-label="Sign in with a role token">{role ? `Signed in: ${role}` : 'Sign in'}</button>
      {open && (
        <div className="modal-back" onClick={() => setOpen(false)}>
          <form className="modal narrow" onClick={(e) => e.stopPropagation()} onSubmit={save}>
            <div className="modal-head"><h3>Role token</h3><button type="button" className="ghost" onClick={() => setOpen(false)} aria-label="Close">✕</button></div>
            <p className="small muted">Manager: approvals, scenarios, clock, discounts, refunds, reports. Server: orders, bookings, waitlist, tables, checks, guests. Kitchen: HACCP and temperatures. Demo tokens are in config (MANAGER_TOKEN, SERVER_TOKEN, KITCHEN_TOKEN).</p>
            <input autoFocus aria-label="Token" type="password" value={tok} onChange={(e) => setTok(e.target.value)} placeholder="paste a token" />
            <div className="form-row">
              <button className="primary" type="submit">Sign in</button>
              <button type="button" onClick={() => { setTok(''); setToken(''); setRole(null); setOpen(false) }}>Sign out</button>
            </div>
            {err && <p className="err small">{err}</p>}
          </form>
        </div>
      )}
    </>
  )
}

export default function TopBar({ state, mode, setMode, alerts }) {
  const [svcs, setSvcs] = useState(null)
  const [err, setErr] = useState('')
  const [role, setRole] = useState(null)
  useEffect(() => { api.whoami().then((r) => setRole(r.role)).catch(() => {}) }, [])
  const c = state.clock || {}
  const s = state.service || {}
  const replay = mode.kind === 'replay'
  const run = (p) => p.catch((e) => setErr(e.message))
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
              <button key={v} className={c.speed === v ? 'on' : ''} onClick={() => run(api.speed(v))} aria-label={v ? `${v}x speed` : 'pause'}>{l}</button>
            ))}
          </div>
        )}
        {s.status === 'ended' && <span className="pill">service ended</span>}
        {pending > 0 && <span className="pill warn">{pending} awaiting approval</span>}
      </div>
      {!replay && (
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
            <button onClick={() => run(api.start({ speed: 10 }))}>New service</button>
            <button onClick={() => run(api.services().then(setSvcs))}>Replay…</button>
          </>
        )}
        <a className="btn" href="#/online" target="_blank" rel="noreferrer">Online page ↗</a>
        <button onClick={alerts.toggle} aria-pressed={alerts.on} title="Chime and desktop notification for new approvals">{alerts.on ? 'Alerts on' : 'Alerts off'}</button>
        <SignIn role={role} setRole={setRole} />
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
      {err && <div className="toast" onClick={() => setErr('')}>{err}</div>}
    </header>
  )
}
