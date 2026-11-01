import { useState } from 'react'
import { STATE_LABEL, STATION_LABEL } from '../format.js'
import OrderDialog from './OrderDialog.jsx'

const STATE_ORDER = ['available', 'seated', 'ordered', 'entrees_fired', 'eating', 'check', 'bussing']

function TableShape({ t, clockT, onClick, selected }) {
  const cls = `tbl st-${t.state}${selected ? ' sel' : ''}${t.joined?.length ? ' joined' : ''}`
  const elapsed = t.party_id ? Math.max(0, Math.round((clockT - t.since) / 60)) : null
  const cx = t.x + t.w / 2
  const cy = t.y + t.h / 2
  const label = t.shape === 'bar' ? t.id : `${t.id}`
  return (
    <g className={cls} onClick={() => onClick(t)} role="button" tabIndex={0}
       onKeyDown={(e) => (e.key === 'Enter' ? onClick(t) : null)} aria-label={`${t.id} ${STATE_LABEL[t.state]}`}>
      {t.shape === 'round' || t.shape === 'bar'
        ? <circle cx={cx} cy={cy} r={t.w / 2} />
        : <rect x={t.x} y={t.y} width={t.w} height={t.h} rx={8} />}
      <text x={cx} y={t.shape === 'bar' ? cy + 4 : cy - (t.party_id ? 6 : 0) + 4} className="tid">{label}</text>
      {t.shape !== 'bar' && t.party_id && (
        <text x={cx} y={cy + 13} className="tmeta">{t.party_size}/{t.seats} · {elapsed}m</text>
      )}
      {t.shape !== 'bar' && !t.party_id && (
        <text x={cx} y={cy + 16} className="tmeta dim">{t.seats} seats</text>
      )}
      {t.shape !== 'bar' && <text x={t.x + t.w - 4} y={t.y + 12} className="tsrv">{t.section?.[0]}</text>}
    </g>
  )
}

export default function Floor({ state }) {
  const [sel, setSel] = useState(null)
  const tables = Object.values(state.tables || {})
  const fx = state.layout?.fixtures || {}
  const clockT = state.clock?.t || 0
  const counts = STATE_ORDER.map((s) => [s, tables.filter((t) => t.state === s).length])
  return (
    <div className="floor">
      <svg viewBox="0 0 1000 620" className="floor-svg" aria-label="Dining floor">
        <rect x="10" y="10" width="980" height="600" rx="14" className="room" />
        {Object.entries(fx).map(([k, f]) => (
          <g key={k} className={`fixture fx-${k}`}>
            <rect x={f.x} y={f.y} width={f.w} height={f.h} rx="6" />
            <text x={f.x + f.w / 2} y={f.y + f.h / 2 + 4}>{f.label}</text>
          </g>
        ))}
        <text x="24" y="30" className="zone">Window</text>
        {tables.map((t) => (
          <TableShape key={t.id} t={t} clockT={clockT} onClick={setSel} selected={sel?.id === t.id} />
        ))}
      </svg>
      <div className="legend">
        {counts.map(([s, n]) => (
          <span key={s} className={`lg st-${s}`}><i />{STATE_LABEL[s]} <b>{n}</b></span>
        ))}
        <span className="lg-occ">
          {state.clock?.guests_in_house ?? 0} guests · {state.clock?.waiting_parties ?? 0} waiting
        </span>
      </div>
      {sel && <OrderDialog state={state} table={state.tables[sel.id] || sel} onClose={() => setSel(null)} />}
    </div>
  )
}

export function KitchenStrip({ state }) {
  const k = state.kitchen
  if (!k) return null
  return (
    <div className="kitchen-strip">
      {k.stations.map((s) => {
        const pct = Math.min(100, Math.round(s.utilisation * 100))
        const lvl = s.utilisation >= 0.9 ? 'hot' : s.utilisation >= 0.55 ? 'warm' : 'ok'
        return (
          <div className={`station ${lvl}`} key={s.station}>
            <div className="st-head"><b>{STATION_LABEL[s.station]}</b><span>{s.cooks} cooks</span></div>
            <div className="st-bar"><div style={{ width: `${pct}%` }} /></div>
            <div className="st-meta"><span>{s.queue_depth} in queue</span><span>wait {s.wait_min} min</span></div>
          </div>
        )
      })}
      <div className={`busy busy-${k.busyness}`}>
        <span className="busy-level">{k.busyness}</span>
        <span className="busy-why">{k.explanation}</span>
      </div>
    </div>
  )
}
