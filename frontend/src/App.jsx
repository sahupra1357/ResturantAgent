import { lazy, Suspense, useCallback, useEffect, useState } from 'react'
import { useApprovalAlerts } from './alerts.js'
import { AUTH_EVENT, api } from './api.js'
import Dashboard from './components/Dashboard.jsx'
import Floor, { KitchenStrip } from './components/Floor.jsx'
import KdsPage from './components/KdsPage.jsx'
import Login from './components/Login.jsx'
import OfflineBanner from './components/OfflineBanner.jsx'
import OnlinePage from './components/OnlinePage.jsx'
import TopBar from './components/TopBar.jsx'
import { useLiveStore } from './store.js'

const Floor3D = lazy(() => import('./components/Floor3D.jsx'))
const AdminPage = lazy(() => import('./components/AdminPage.jsx'))

function useHash() {
  const [h, setH] = useState(window.location.hash)
  useEffect(() => {
    const f = () => setH(window.location.hash)
    window.addEventListener('hashchange', f)
    return () => window.removeEventListener('hashchange', f)
  }, [])
  return h
}

const pref = (k, d) => { try { return localStorage.getItem(k) || d } catch { return d } }
const savePref = (k, v) => { try { localStorage.setItem(k, v) } catch { /* private mode */ } }
const FLOOR_TABS = [['floor3d', 'Floor 3D'], ['floor', 'Floor 2D'], ['online', 'Online order page']]

// The customer page: its own public socket, no sign-in.
function PublicOnline() {
  const { state } = useLiveStore({ kind: 'public' })
  if (!state.ready) return <div className="loading" role="status"><h2>Online ordering</h2><p>{state.error || 'Connecting…'}</p></div>
  return <OnlinePage state={state} standalone />
}

function Loading({ state }) {
  return (
    <div className="loading" role="status">
      <h2>Restaurant Ops Agents</h2>
      <p>{state.error ? `Service error: ${state.error}` : state.connected ? 'Loading service…' : 'Connecting to the backend…'}</p>
    </div>
  )
}

function Staff({ me, onSignOut }) {
  const { state, mode, setMode } = useLiveStore({ kind: 'live' })
  const hash = useHash()
  const [view, setView] = useState(() => pref('ro-left', 'floor3d'))
  const [layout, setLayoutRaw] = useState(() => pref('ro-layout', 'stack'))
  const alerts = useApprovalAlerts(state)
  const pick = (k) => { setView(k); savePref('ro-left', k) }
  const setLayout = (l) => { setLayoutRaw(l); savePref('ro-layout', l) }
  useEffect(() => { if (state.authRequired) onSignOut(true) }, [state.authRequired, onSignOut])

  if (!state.ready) return <Loading state={state} />
  if (hash === '#/kds') return <><OfflineBanner connected={state.connected} /><KdsPage state={state} /></>
  if (hash === '#/admin') {
    return (
      <Suspense fallback={<p className="muted">Loading setup…</p>}>
        {me.role === 'admin' ? <AdminPage me={me} /> : <div className="page"><p className="err">Setup needs an admin account.</p><a href="#/">Back</a></div>}
      </Suspense>
    )
  }
  return (
    <div className="app">
      <a className="skip" href="#dashboard">Skip to dashboard</a>
      <TopBar state={state} mode={mode} setMode={setMode} alerts={alerts} me={me} onSignOut={() => onSignOut(false)} layout={layout} setLayout={setLayout} />
      <OfflineBanner connected={state.connected} />
      <main className={`layout layout-${layout}`}>
        <div className="left">
          <div className="tabs" role="tablist" aria-label="Floor view">
            {FLOOR_TABS.map(([k, label]) => (
              <button key={k} role="tab" aria-selected={view === k} className={view === k ? 'on' : ''} onClick={() => pick(k)}>{label}</button>
            ))}
            <span className="tab-note small muted">
              {state.clock?.guests_in_house ?? 0} guests in house · {(state.host?.waitlist || []).length} parties waiting
            </span>
          </div>
          {view === 'floor' && <Floor state={state} />}
          {view === 'floor3d' && (
            <Suspense fallback={<div className="floor"><p className="muted">Loading 3D floor…</p></div>}>
              <Floor3D state={state} onFallback={() => pick('floor')} />
            </Suspense>
          )}
          {view === 'online' && <OnlinePage state={state} />}
          <KitchenStrip state={state} />
        </div>
        <div className="right" id="dashboard">
          <Dashboard state={state} me={me} />
        </div>
      </main>
    </div>
  )
}

export default function App() {
  const hash = useHash()
  const [me, setMe] = useState(undefined) // undefined = checking, null = signed out
  const [note, setNote] = useState('')
  useEffect(() => {
    api.me().then(setMe).catch(() => setMe(null))
    const onAuth = () => { setNote('Your session ended; please sign in again.'); setMe(null) }
    window.addEventListener(AUTH_EVENT, onAuth)
    return () => window.removeEventListener(AUTH_EVENT, onAuth)
  }, [])
  const signOut = useCallback((expired) => {
    if (expired) { setNote('Your session ended; please sign in again.'); setMe(null); return }
    api.logout().catch(() => {}).finally(() => { setNote(''); setMe(null) })
  }, [])

  if (hash === '#/online') return <PublicOnline />
  if (me === undefined) return <div className="loading" role="status"><p>Checking sign-in…</p></div>
  if (me === null) return <Login note={note} onSignedIn={(u) => { setNote(''); setMe(u) }} />
  return <Staff me={me} onSignOut={signOut} />
}
