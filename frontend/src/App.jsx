import { lazy, Suspense, useEffect, useState } from 'react'
import { useApprovalAlerts } from './alerts.js'
import Dashboard from './components/Dashboard.jsx'
import Floor, { KitchenStrip } from './components/Floor.jsx'
import OnlinePage from './components/OnlinePage.jsx'
import TopBar from './components/TopBar.jsx'
import { useLiveStore } from './store.js'

const Floor3D = lazy(() => import('./components/Floor3D.jsx'))

function useHash() {
  const [h, setH] = useState(window.location.hash)
  useEffect(() => {
    const f = () => setH(window.location.hash)
    window.addEventListener('hashchange', f)
    return () => window.removeEventListener('hashchange', f)
  }, [])
  return h
}

const LEFT_TABS = [['floor3d', 'Floor 3D'], ['floor', 'Floor 2D'], ['online', 'Online order page']]

export default function App() {
  const { state, mode, setMode } = useLiveStore()
  const hash = useHash()
  const [left, setLeft] = useState(() => { try { return localStorage.getItem('ro-left') || 'floor3d' } catch { return 'floor3d' } })
  const alerts = useApprovalAlerts(state)
  const pick = (k) => { setLeft(k); try { localStorage.setItem('ro-left', k) } catch { /* private mode */ } }

  if (!state.ready) {
    return (
      <div className="loading" role="status">
        <h2>Restaurant Ops Agents</h2>
        <p>{state.error ? `Service error: ${state.error}` : state.connected ? 'Loading service…' : 'Connecting to the backend…'}</p>
      </div>
    )
  }
  if (hash === '#/online') return <OnlinePage state={state} standalone />

  return (
    <div className="app">
      <a className="skip" href="#dashboard">Skip to dashboard</a>
      <TopBar state={state} mode={mode} setMode={setMode} alerts={alerts} />
      <main className="layout">
        <div className="left">
          <div className="tabs" role="tablist" aria-label="Floor view">
            {LEFT_TABS.map(([k, label]) => (
              <button key={k} role="tab" aria-selected={left === k} className={left === k ? 'on' : ''} onClick={() => pick(k)}>{label}</button>
            ))}
          </div>
          {left === 'floor' && <Floor state={state} />}
          {left === 'floor3d' && (
            <Suspense fallback={<div className="floor"><p className="muted">Loading 3D floor…</p></div>}>
              <Floor3D state={state} onFallback={() => pick('floor')} />
            </Suspense>
          )}
          {left === 'online' && <OnlinePage state={state} />}
          <KitchenStrip state={state} />
        </div>
        <div className="right" id="dashboard">
          <Dashboard state={state} />
        </div>
      </main>
    </div>
  )
}
