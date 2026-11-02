import { useState } from 'react'
import { api } from '../api.js'

export default function Login({ onSignedIn, note }) {
  const [u, setU] = useState('')
  const [p, setP] = useState('')
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)
  const submit = async (e) => {
    e.preventDefault()
    setBusy(true); setErr('')
    try {
      const r = await api.login(u.trim(), p)
      setP('')
      onSignedIn(r.user)
    } catch (x) { setErr(x.message) } finally { setBusy(false) }
  }
  return (
    <div className="login-page">
      <form className="login" onSubmit={submit} aria-labelledby="login-title">
        <span className="logo" aria-hidden>RO</span>
        <h2 id="login-title">Restaurant Ops Agents</h2>
        <p className="small muted">Sign in with your staff account.</p>
        {note && <p className="warn-box small">{note}</p>}
        <label>Username <input autoFocus autoComplete="username" value={u} onChange={(e) => setU(e.target.value)} required /></label>
        <label>Password <input type="password" autoComplete="current-password" value={p} onChange={(e) => setP(e.target.value)} required /></label>
        <button className="primary" type="submit" disabled={busy}>{busy ? 'Signing in…' : 'Sign in'}</button>
        {err && <p className="err small" role="alert">{err}</p>}
        <p className="small muted">Demo accounts (when DEMO_USERS is on): manager / manager-demo, server / server-demo, kitchen / kitchen-demo.
          The first admin password is in <code>data/FIRST_RUN_ADMIN.txt</code> unless ADMIN_PASSWORD is set.</p>
        <a className="small" href="#/online">Customer online-order page →</a>
      </form>
    </div>
  )
}
