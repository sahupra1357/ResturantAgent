import { useEffect, useState } from 'react'
import { coerce } from '../admin-logic.js'

// An editable grid: one row per record, inline inputs, Save / Delete per row, and an "add" row.
// cols: [{key, label, type: text|number|select|bool, options, width, readOnlyExisting}]
export default function EditTable({ rows, cols, rowKey = 'id', onSave, onDelete, newRow, addLabel = 'Add', extra }) {
  const [draft, setDraft] = useState({})
  const [adding, setAdding] = useState(null)
  const [err, setErr] = useState('')
  const [ok, setOk] = useState('')
  useEffect(() => { setDraft({}) }, [rows])
  const val = (r, k) => (draft[r[rowKey]]?.[k] !== undefined ? draft[r[rowKey]][k] : r[k])
  const set = (r, k, v) => setDraft({ ...draft, [r[rowKey]]: { ...(draft[r[rowKey]] || {}), [k]: v } })
  const run = async (fn, msg) => {
    setErr(''); setOk('')
    try { await fn(); setOk(msg) } catch (e) { setErr(e.message) }
  }
  const input = (c, value, onChange, existing) => {
    const id = `${c.key}`
    if (existing && c.readOnlyExisting) return <span className="mono">{value}</span>
    if (c.type === 'select') {
      return (
        <select aria-label={c.label} value={value ?? ''} onChange={(e) => onChange(e.target.value)}>
          {(c.options || []).map((o) => (Array.isArray(o) ? <option key={o[0]} value={o[0]}>{o[1]}</option> : <option key={o} value={o}>{o}</option>))}
        </select>
      )
    }
    if (c.type === 'bool') return <input aria-label={c.label} type="checkbox" checked={!!value} onChange={(e) => onChange(e.target.checked)} />
    return (
      <input aria-label={c.label} id={id} type={c.type === 'number' ? 'number' : 'text'} step={c.step || 'any'} value={value ?? ''}
             style={c.width ? { width: c.width } : undefined} onChange={(e) => onChange(e.target.value)} />
    )
  }
  return (
    <div className="edit-table">
      <div className="table-scroll">
        <table className="grid">
          <thead><tr>{cols.map((c) => <th key={c.key}>{c.label}</th>)}<th /></tr></thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r[rowKey]} className={r.active === 0 ? 'retired' : ''}>
                {cols.map((c) => <td key={c.key}>{input(c, val(r, c.key), (v) => set(r, c.key, v), true)}</td>)}
                <td className="row-actions">
                  <button className="ghost" disabled={!draft[r[rowKey]]}
                          onClick={() => run(() => onSave(r[rowKey], coerce({ ...r, ...draft[r[rowKey]] }, cols), false), `Saved ${r[rowKey]}`)}>Save</button>
                  {extra && extra(r, run)}
                  {onDelete && <button className="ghost danger" onClick={() => run(() => onDelete(r[rowKey]), `Deleted ${r[rowKey]}`)}>Delete</button>}
                </td>
              </tr>
            ))}
            {adding && (
              <tr className="adding">
                {cols.map((c) => <td key={c.key}>{input(c, adding[c.key], (v) => setAdding({ ...adding, [c.key]: v }), false)}</td>)}
                <td className="row-actions">
                  <button className="primary" onClick={() => run(async () => { await onSave(adding[rowKey], coerce(adding, cols), true); setAdding(null) }, 'Added')}>Add</button>
                  <button className="ghost" onClick={() => setAdding(null)}>Cancel</button>
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
      {newRow && !adding && <button className="ghost small" onClick={() => setAdding(newRow())}>+ {addLabel}</button>}
      {err && <p className="err small" role="alert">{err}</p>}
      {ok && <p className="ok small" role="status">{ok}</p>}
    </div>
  )
}
