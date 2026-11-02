import { useCallback, useEffect, useState } from 'react'
import { api } from '../api.js'
import { STATION_LABEL } from '../format.js'

// Kitchen display: one ticket per order and station, oldest first; late tickets turn red; Bump clears a ticket.
// Refreshes on every live event batch (the parent passes the clock) and every 3 s.
export default function KdsPage({ state }) {
  const [station, setStation] = useState(() => { try { return localStorage.getItem('ro-kds-station') || '' } catch { return '' } })
  const [data, setData] = useState(null)
  const [err, setErr] = useState('')
  const load = useCallback(() => api.kds(station || null).then((d) => { setData(d); setErr('') }).catch((e) => setErr(e.message)), [station])
  useEffect(() => { load(); const id = setInterval(load, 3000); return () => clearInterval(id) }, [load, state.clock?.hhmm])
  const pick = (s) => { setStation(s); try { localStorage.setItem('ro-kds-station', s) } catch { /* private mode */ } }
  const bump = (t) => api.bump(t.order_id, t.station).then(load).catch((e) => setErr(e.message))
  const stations = data?.stations || Object.keys(STATION_LABEL)
  return (
    <div className="page kds">
      <header className="page-head">
        <h2>Kitchen display {data && <span className="muted">· {data.clock}</span>}</h2>
        <div className="seg" role="group" aria-label="Station">
          <button className={!station ? 'on' : ''} onClick={() => pick('')}>All</button>
          {stations.map((s) => <button key={s} className={station === s ? 'on' : ''} onClick={() => pick(s)}>{STATION_LABEL[s] || s}</button>)}
        </div>
        <a className="btn" href="#/">Back</a>
      </header>
      {err && <p className="err" role="alert">{err}</p>}
      <div className="kds-grid" aria-live="polite">
        {(data?.tickets || []).map((t) => (
          <article key={`${t.order_id}-${t.station}`} className={`ticket ${t.late ? 'late' : ''} ${t.status}`}>
            <header>
              <b>{t.table_ids?.length ? t.table_ids.join('+') : t.channel === 'online' ? 'Online' : ''} {t.order_id}</b>
              <span>{STATION_LABEL[t.station] || t.station}</span>
              <span className="age">{t.age_min}m</span>
            </header>
            <ul>{t.items.map((i) => (
              <li key={i.item_id} className={i.status}>
                <span>{i.dish}</span>
                {i.mods && <em>{i.mods}</em>}
                <small>{i.status === 'scheduled' ? `fires ${i.fire_at}` : i.status}</small>
              </li>
            ))}</ul>
            {t.allergens?.length > 0 && <p className="allergen">⚠ allergy: {t.allergens.join(', ')}</p>}
            <button className={t.status === 'done' ? 'primary' : ''} onClick={() => bump(t)}>Bump</button>
          </article>
        ))}
        {data && data.tickets.length === 0 && <p className="muted">No open tickets{station ? ` for ${STATION_LABEL[station] || station}` : ''}.</p>}
      </div>
    </div>
  )
}
