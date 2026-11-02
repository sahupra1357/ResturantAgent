import { useCallback, useEffect, useState } from 'react'
import { api } from '../api.js'
import { money, num } from '../format.js'
import { Panel, useAct } from './ui.jsx'

export function DayClosePanel({ state }) {
  const [cash, setCash] = useState('')
  const [rec, setRec] = useState(null)
  const [done, setDone] = useState(null)
  const [hist, setHist] = useState([])
  const { act, err } = useAct()
  const ended = state.service?.status === 'ended'
  const counted = cash === '' ? null : Number(cash)
  const preview = useCallback(() => api.dayClosePreview(counted).then(setRec).catch(() => {}), [counted])
  useEffect(() => { preview() }, [preview, state.kpis?.revenue])
  useEffect(() => { api.dayCloseHistory().then(setHist).catch(() => {}) }, [done])
  return (
    <Panel title="Day close & reconciliation" className="span2" right={ended ? <span className="pill ok">service ended</span> : <span className="pill warn">service running</span>}>
      {rec && (
        <div className="recon">
          <table className="grid small"><tbody>
            <tr><th>Checks due</th><td>{money(rec.checks_due)}</td><th>Collected</th><td>{money(rec.collected)}</td></tr>
            {Object.entries(rec.collected_by_method).map(([m, v]) => <tr key={m}><td>{m}</td><td>{money(v)}</td><td>tips</td><td>{money(rec.tips_by_method[m])}</td></tr>)}
            <tr><th>Discounts & comps</th><td>{money(rec.discounts_and_comps)}</td><th>Refunds</th><td>{money(rec.refunds)}</td></tr>
            {Object.entries(rec.tax_by_jurisdiction).map(([k, v]) => <tr key={k}><td colSpan={3}>{k}</td><td>{money(v)}</td></tr>)}
            <tr><th>Expected cash</th><td>{money(rec.expected_cash)}</td><th>Over / short</th>
              <td className={rec.cash_over_short ? (rec.cash_over_short < 0 ? 'bad' : 'warn') : ''}>{rec.cash_over_short == null ? 'count the drawer' : money(rec.cash_over_short)}</td></tr>
          </tbody></table>
          {rec.open_checks.length > 0 && <p className="warn-box small">{rec.open_checks.length} open check(s): {rec.open_checks.map((c) => `${c.check_id} ${money(c.balance)}`).join(', ')}</p>}
        </div>
      )}
      <div className="form-row">
        <label>Counted cash <input type="number" step="0.01" value={cash} onChange={(e) => setCash(e.target.value)} placeholder="0.00" /></label>
        <button className="primary" disabled={!ended} title={ended ? '' : 'End the service first, or force close'} onClick={() => act(async () => setDone(await api.dayClose(counted, false)))}>Close day</button>
        <button className="danger" onClick={() => { if (window.confirm('End the service now and close, writing off open checks?')) act(async () => setDone(await api.dayClose(counted, true))) }}>Force close</button>
      </div>
      {err && <p className="err small">{err}</p>}
      {done && (
        <p className="ok small" role="status">Closed {done.date} {done.meal_period} · journal {done.totals.balanced ? 'balanced' : 'NOT balanced'} (debits {money(done.totals.debits)} = credits {money(done.totals.credits)}) · {done.files.journal_csv}</p>
      )}
      {hist.length > 0 && (
        <details><summary className="small">Previous closes ({hist.length})</summary>
          <table className="grid small"><tbody>{hist.map((h) => (
            <tr key={`${h.date}-${h.meal_period}`}><td>{h.date} {h.meal_period}</td><td>{money(h.collected)}</td><td>over/short {money(h.cash_over_short)}</td><td>{h.closed_by}</td><td>{h.balanced ? '✓' : '✗'}</td></tr>
          ))}</tbody></table>
        </details>
      )}
    </Panel>
  )
}

export function GiftCardPanel() {
  const [amt, setAmt] = useState('50')
  const [code, setCode] = useState('')
  const [card, setCard] = useState(null)
  const { act, err, msg } = useAct()
  return (
    <Panel title="Gift cards">
      <form className="form-row" onSubmit={(e) => { e.preventDefault(); act(async () => { const g = await api.issueGift(Number(amt)); setCard({ ...g, ledger: [] }); setCode(g.code) }, 'Issued') }}>
        <input aria-label="Amount" type="number" min="1" value={amt} onChange={(e) => setAmt(e.target.value)} />
        <button type="submit">Issue card</button>
      </form>
      <form className="form-row" onSubmit={(e) => { e.preventDefault(); act(async () => setCard(await api.gift(code.trim()))) }}>
        <input aria-label="Gift card code" placeholder="XXXX-XXXX-XXXX" value={code} onChange={(e) => setCode(e.target.value.toUpperCase())} />
        <button type="submit">Balance</button>
      </form>
      {card && (
        <div className="small">
          <p><b className="mono">{card.code}</b> · {money(card.balance)} of {money(card.initial)} · <span className={card.status === 'active' ? 'pill ok' : 'pill bad'}>{card.status}</span></p>
          {card.ledger?.length > 0 && <ul className="plain">{card.ledger.map((l) => <li key={l.id}>{l.ts.slice(0, 16)} {l.kind} {money(l.amount)} {l.ref}</li>)}</ul>}
          {card.status === 'active' && <button className="ghost danger" onClick={() => { const r = window.prompt('Reason for voiding'); if (r) act(async () => setCard(await api.voidGift(card.code, r)), 'Voided') }}>Void</button>}
        </div>
      )}
      <p className="small muted">Pay with a card from the check panel (method: gift card).</p>
      {err && <p className="err small">{err}</p>}{msg && <p className="ok small">{msg}</p>}
    </Panel>
  )
}

export function LlmPanel({ state }) {
  const [v, setV] = useState(null)
  useEffect(() => { api.llm().then(setV).catch(() => setV(null)) }, [state.clock?.hhmm?.slice(0, 4), state.service?.service_id])
  return (
    <Panel title="LLM review (pilot)" right={v && <span className={`pill ${v.active ? 'ok' : ''}`}>{v.active ? 'active' : 'rule-based'}</span>}>
      {!v && <p className="muted small">Loading…</p>}
      {v && (<>
        <p className="small">{v.active ? `${v.mode} reviews Menu, Procurement and Forecasting proposals (timeout ${v.timeout_s} s).` : `Rule-based fallback: ${v.why_not}.`}</p>
        <table className="grid small"><tbody>
          <tr><td>Calls</td><td>{v.calls} / {v.call_cap}</td><td>Failures</td><td>{v.failures}{v.tripped ? ' (breaker open)' : ''}</td></tr>
          <tr><td>Cost</td><td>{money(v.cost)} / {money(v.cost_cap)}</td><td>Skipped</td><td>{v.refused}</td></tr>
          <tr><td>p50 latency</td><td>{num(v.p50_ms)} ms</td><td>p95 latency</td><td>{num(v.p95_ms)} ms</td></tr>
        </tbody></table>
        {v.recent?.length > 0 && (
          <details><summary className="small">Recent calls</summary>
            <ul className="plain small">{v.recent.map((c, i) => <li key={i}>{c.ts.slice(11, 19)} {c.agent} {c.ok ? '✓' : `✗ ${c.error || ''}`} {num(c.latency_ms)} ms {money(c.cost)}</li>)}</ul>
          </details>
        )}
        <p className="small muted">Eval: <code>python -m restaurant_ops.live.llm_eval build</code> then <code>run --judge live</code>.</p>
      </>)}
    </Panel>
  )
}

export function PoOutboxPanel({ state }) {
  const [rows, setRows] = useState([])
  const { act, err, msg } = useAct()
  const n = Object.values(state.decisions || {}).filter((d) => d.action_type === 'purchase_order' && d.status === 'executed').length
  const load = useCallback(() => api.purchaseOrders().then(setRows).catch(() => setRows([])), [])
  useEffect(() => { load() }, [load, n])
  return (
    <Panel title="Purchase orders sent to suppliers">
      {rows.length === 0 && <p className="muted small">No POs this service yet.</p>}
      <table className="grid small"><tbody>{rows.map((r) => (
        <tr key={r.po_id}><td className="mono">{r.po_id}</td><td>{r.supplier_id}</td><td>{r.email || 'no e-mail'}</td>
          <td><span className={`pill ${r.status === 'sent' ? 'ok' : r.status === 'failed' ? 'bad' : ''}`} title={r.error || ''}>{r.status}</span></td>
          <td><a className="btn small" href={`/api/purchase-orders/${r.po_id}.pdf`} target="_blank" rel="noreferrer">PDF</a>
            <button className="ghost" onClick={() => act(async () => { await api.resendPo(r.po_id); await load() }, 'Resent')}>Resend</button></td></tr>
      ))}</tbody></table>
      <p className="small muted">Without SMTP settings, POs are written to the outbox folder (PDF + .eml) and marked queued.</p>
      {err && <p className="err small">{err}</p>}{msg && <p className="ok small">{msg}</p>}
    </Panel>
  )
}

export function BackupsPanel({ me }) {
  const [rows, setRows] = useState([])
  const [drill, setDrill] = useState(null)
  const { act, err, msg } = useAct()
  const load = useCallback(() => api.backups().then(setRows).catch(() => setRows([])), [])
  useEffect(() => { load() }, [load])
  return (
    <Panel title="Backups & restore drill">
      <div className="form-row">
        <button onClick={() => act(async () => { await api.backupNow(); await load() }, 'Backup written')}>Back up now</button>
        <button onClick={() => act(async () => setDrill(await api.drill()))}>Run restore drill</button>
      </div>
      {drill && <p className={drill.ok ? 'ok small' : 'err small'}>Drill {drill.ok ? 'passed' : 'FAILED'}: {drill.restored_files} files restored, {drill.databases.length} databases integrity-checked, snapshot {drill.snapshot || 'none'}</p>}
      <table className="grid small"><tbody>{rows.slice(0, 8).map((b) => (
        <tr key={b.backup_id}><td className="mono">{b.backup_id}</td><td>{b.created?.slice(0, 16).replace('T', ' ')}</td><td>{b.files} files</td>
          <td><button className="ghost" onClick={() => act(async () => { const v = await api.verifyBackup(b.backup_id); return v }, 'Verified')}>Verify</button>
            {me?.role === 'admin' && <button className="ghost danger" onClick={() => { if (window.confirm(`Restore ${b.backup_id}? The running service is saved, then replaced.`)) act(() => api.restore(b.backup_id), 'Restored; a fresh service started') }}>Restore</button>}</td></tr>
      ))}</tbody></table>
      {err && <p className="err small">{err}</p>}{msg && <p className="ok small">{msg}</p>}
    </Panel>
  )
}

export function AuditSection() {
  const [rows, setRows] = useState([])
  const [who, setWho] = useState('')
  useEffect(() => { api.audit(who || null).then(setRows).catch(() => setRows([])) }, [who])
  return (
    <Panel title="Audit log (every write, who and when)" className="span2" right={
      <input aria-label="Filter by user" placeholder="filter user" value={who} onChange={(e) => setWho(e.target.value.trim())} />}>
      <div className="table-scroll tall"><table className="grid small">
        <thead><tr><th>When (UTC)</th><th>User</th><th>Role</th><th>Action</th><th>Status</th><th>Detail</th></tr></thead>
        <tbody>{rows.map((r) => (
          <tr key={r.id} className={r.status >= 400 ? 'warnrow' : ''}><td>{r.ts.slice(0, 19).replace('T', ' ')}</td><td>{r.username || '–'}</td><td>{r.role || ''}</td>
            <td className="mono">{r.method} {r.path}</td><td>{r.status}</td><td className="mono small">{(r.detail || '').slice(0, 120)}</td></tr>
        ))}</tbody></table></div>
    </Panel>
  )
}
