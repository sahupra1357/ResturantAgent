import { useEffect, useState } from 'react'
import Dashboard from './components/Dashboard.jsx'
import Floor, { KitchenStrip } from './components/Floor.jsx'
import OnlinePage from './components/OnlinePage.jsx'
import TopBar from './components/TopBar.jsx'
import { useLiveStore } from './store.js'

function useHash() {
  const [h, setH] = useState(window.location.hash)
  useEffect(() => {
    const f = () => setH(window.location.hash)
    window.addEventListener('hashchange', f)
    return () => window.removeEventListener('hashchange', f)
  }, [])
  return h
}

export default function App() {
  const { state, mode, setMode } = useLiveStore()
  const hash = useHash()
  const [left, setLeft] = useState('floor')

  if (!state.ready) {
    return (
      <div className="loading">
        <h2>Restaurant Ops Agents</h2>
        <p>{state.error ? `Service error: ${state.error}` : state.connected ? 'Loading service…' : 'Connecting to the backend…'}</p>
      </div>
    )
  }
  if (hash === '#/online') return <OnlinePage state={state} standalone />

  return (
    <div className="app">
      <TopBar state={state} mode={mode} setMode={setMode} />
      <main className="layout">
        <div className="left">
          <div className="tabs" role="tablist">
            <button role="tab" aria-selected={left === 'floor'} className={left === 'floor' ? 'on' : ''} onClick={() => setLeft('floor')}>Dining floor</button>
            <button role="tab" aria-selected={left === 'online'} className={left === 'online' ? 'on' : ''} onClick={() => setLeft('online')}>Online order page</button>
          </div>
          {left === 'floor' ? <Floor state={state} /> : <OnlinePage state={state} />}
          <KitchenStrip state={state} />
        </div>
        <div className="right">
          <Dashboard state={state} />
        </div>
      </main>
    </div>
  )
}
