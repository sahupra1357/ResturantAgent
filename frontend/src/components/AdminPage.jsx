import { useCallback, useEffect, useState } from 'react'
import { api } from '../api.js'
import { WEEKDAYS, csvPreview, dishCost, formatRecipe, parseRecipe } from '../admin-logic.js'
import { money } from '../format.js'
import { AuditSection } from './BackOffice.jsx'
import EditTable from './EditTable.jsx'
import { Panel, useAct } from './ui.jsx'

const SECTIONS = [
  ['profile', 'Restaurant'], ['taxes', 'Taxes'], ['hours', 'Hours'], ['tables', 'Tables'], ['stations', 'Stations'],
  ['zones', 'Delivery zones'], ['menu', 'Menu & recipes'], ['ingredients', 'Ingredients'], ['suppliers', 'Suppliers'],
  ['staff', 'Staff & roster'], ['import', 'Import'], ['users', 'Users'], ['audit', 'Audit log'],
]

function ProfileSection({ cfg, reload }) {
  const [p, setP] = useState(cfg.profile)
  const { act, err, msg } = useAct()
  useEffect(() => setP(cfg.profile), [cfg.profile])
  const set = (k, v) => setP({ ...p, [k]: v })
  const loy = p.loyalty || {}
  return (
    <Panel title="Restaurant profile">
      <div className="form-grid">
        <label>Name <input value={p.name || ''} onChange={(e) => set('name', e.target.value)} /></label>
        <label>Address <input value={p.address || ''} onChange={(e) => set('address', e.target.value)} /></label>
        <label>Phone <input value={p.phone || ''} onChange={(e) => set('phone', e.target.value)} /></label>
        <label>Timezone <input value={p.timezone || ''} onChange={(e) => set('timezone', e.target.value)} /></label>
        <label>Overbooking factor <input type="number" step="0.05" value={p.overbook_factor ?? 1.1} onChange={(e) => set('overbook_factor', Number(e.target.value))} /></label>
        <label>Manager max discount % <input type="number" value={p.discount_max_pct?.manager ?? 30}
          onChange={(e) => set('discount_max_pct', { ...p.discount_max_pct, manager: Number(e.target.value) })} /></label>
        <label>Server max discount % <input type="number" value={p.discount_max_pct?.server ?? 10}
          onChange={(e) => set('discount_max_pct', { ...p.discount_max_pct, server: Number(e.target.value) })} /></label>
        <label>Loyalty points per $ <input type="number" step="0.1" value={loy.points_per_dollar ?? 1}
          onChange={(e) => set('loyalty', { ...loy, points_per_dollar: Number(e.target.value) })} /></label>
        <label>Point value ($) <input type="number" step="0.001" value={loy.redeem_value_per_point ?? 0.01}
          onChange={(e) => set('loyalty', { ...loy, redeem_value_per_point: Number(e.target.value) })} /></label>
      </div>
      <p className="small muted">Loyalty tiers: {Object.entries(loy.tiers || {}).map(([k, v]) => `${k} ≥ ${v} pts`).join(' · ') || '–'}</p>
      <button className="primary" onClick={() => act(async () => { await api.saveProfile(p); await reload() }, 'Saved')}>Save profile</button>
      {err && <p className="err small">{err}</p>}{msg && <p className="ok small">{msg}</p>}
    </Panel>
  )
}

function HoursSection({ cfg, reload }) {
  const { act, err, msg } = useAct()
  const periods = cfg.choices.periods
  const byKey = Object.fromEntries(cfg.hours.map((h) => [`${h.weekday}-${h.meal_period}`, h]))
  const [d, setD] = useState({})
  const v = (wd, p) => ({ ...(byKey[`${wd}-${p}`] || { weekday: wd, meal_period: p, open: '', close: '', closed: 1 }), ...(d[`${wd}-${p}`] || {}) })
  const set = (wd, p, k, x) => setD({ ...d, [`${wd}-${p}`]: { ...(d[`${wd}-${p}`] || {}), [k]: x } })
  return (
    <Panel title="Opening hours (a closed period cannot be started)">
      <div className="table-scroll"><table className="grid">
        <thead><tr><th>Day</th>{periods.map((p) => <th key={p}>{p}</th>)}</tr></thead>
        <tbody>{WEEKDAYS.map((name, wd) => (
          <tr key={wd}><td>{name}</td>{periods.map((p) => {
            const h = v(wd, p)
            return (
              <td key={p} className="hours-cell">
                <input aria-label={`${name} ${p} open`} className="hhmm" value={h.open} onChange={(e) => set(wd, p, 'open', e.target.value)} />
                –<input aria-label={`${name} ${p} close`} className="hhmm" value={h.close} onChange={(e) => set(wd, p, 'close', e.target.value)} />
                <label className="small"><input type="checkbox" checked={!!h.closed} onChange={(e) => set(wd, p, 'closed', e.target.checked)} /> closed</label>
              </td>
            )
          })}</tr>
        ))}</tbody>
      </table></div>
      <button className="primary" disabled={!Object.keys(d).length} onClick={() => act(async () => {
        for (const k of Object.keys(d)) {
          const [wd, p] = k.split('-')
          const h = v(Number(wd), p)
          await api.saveHours({ weekday: Number(wd), meal_period: p, open: h.open, close: h.close, closed: !!h.closed })
        }
        setD({}); await reload()
      }, 'Hours saved')}>Save hours</button>
      {err && <p className="err small">{err}</p>}{msg && <p className="ok small">{msg}</p>}
    </Panel>
  )
}

function MenuSection({ cfg, reload }) {
  const ch = cfg.choices
  const rows = cfg.dishes.map((d) => ({ ...d, recipe_text: formatRecipe(d.recipe) }))
  const cols = [
    { key: 'id', label: 'Id', readOnlyExisting: true, width: 110 }, { key: 'name', label: 'Name', width: 150 },
    { key: 'category', label: 'Category', type: 'select', options: ch.categories },
    { key: 'price', label: 'Price', type: 'number', width: 70 },
    { key: 'station', label: 'Station', type: 'select', options: cfg.stations.map((s) => s.id) },
    { key: 'prep_minutes', label: 'Prep min', type: 'number', width: 60 },
    { key: 'tax_category', label: 'Tax', type: 'select', options: ['food', 'alcohol'] },
    { key: 'is_cold', label: 'Cold', type: 'bool' },
    { key: 'recipe_text', label: 'Recipe (ingredient:qty; …)', width: 260 },
  ]
  const save = async (id, body) => {
    const recipe = parseRecipe(body.recipe_text)
    const { recipe_text: _, ...rest } = body
    await api.saveDish(id, { ...rest, recipe })
    await reload()
  }
  return (
    <Panel title="Menu & recipes" className="span2">
      <EditTable rows={rows} cols={cols} onSave={save} addLabel="Add dish"
        newRow={() => ({ id: '', name: '', category: 'main', price: 20, station: cfg.stations[0]?.id, prep_minutes: 10, tax_category: 'food', is_cold: 0, recipe_text: '' })}
        extra={(r, run) => {
          const { cost } = dishCost(r.recipe, cfg.ingredients)
          return (<>
            <span className="small muted" title="food cost / food cost %">{money(cost)} · {r.price ? Math.round((cost / r.price) * 100) : '–'}%</span>
            <button className="ghost" onClick={() => run(async () => { await api.dishAction(r.id, r.active ? 'retire' : 'restore'); await reload() }, r.active ? 'Retired' : 'Restored')}>
              {r.active ? 'Retire' : 'Restore'}</button>
          </>)
        }} />
      <p className="small muted">Ingredient ids are on the Ingredients tab. Retired dishes stay in history and reports but leave the menu.</p>
    </Panel>
  )
}

function SuppliersSection({ cfg, reload }) {
  const [open, setOpen] = useState(null)
  const [item, setItem] = useState({ ingredient_id: '', price: '', pack_size: '' })
  const { act, err, msg } = useAct()
  const cols = [
    { key: 'id', label: 'Id', readOnlyExisting: true, width: 100 }, { key: 'name', label: 'Name', width: 150 },
    { key: 'email', label: 'PO e-mail', width: 180 }, { key: 'lead_time_days', label: 'Lead days', type: 'number', width: 55 },
    { key: 'min_order_value', label: 'Min order', type: 'number', width: 70 }, { key: 'fill_rate', label: 'Fill rate', type: 'number', width: 60 },
    { key: 'delay_prob', label: 'Delay p', type: 'number', width: 60 }, { key: 'order_cutoff_hour', label: 'Cutoff h', type: 'number', width: 50 },
  ]
  const sup = cfg.suppliers.find((s) => s.id === open)
  return (
    <Panel title="Suppliers & price lists" className="span2">
      <EditTable rows={cfg.suppliers} cols={cols} addLabel="Add supplier"
        onSave={async (id, b) => { await api.saveSupplier(id, b); await reload() }}
        newRow={() => ({ id: '', name: '', email: '', lead_time_days: 1, min_order_value: 0, fill_rate: 0.95, delay_prob: 0.05, order_cutoff_hour: 14 })}
        extra={(r) => <button className="ghost" onClick={() => setOpen(open === r.id ? null : r.id)}>Price list ({(r.items || []).length})</button>} />
      {sup && (
        <div className="subpanel">
          <h4>{sup.name}: price list</h4>
          <table className="grid"><thead><tr><th>Ingredient</th><th>Price / pack</th><th>Pack size</th><th /></tr></thead>
            <tbody>{(sup.items || []).map((i) => (
              <tr key={i.ingredient_id}><td>{i.ingredient_id}</td><td>{money(i.price)}</td><td>{i.pack_size}</td>
                <td><button className="ghost danger" onClick={() => act(async () => { await api.deleteSupplierItem(sup.id, i.ingredient_id); await reload() }, 'Removed')}>Remove</button></td></tr>
            ))}</tbody></table>
          <form className="form-row" onSubmit={(e) => { e.preventDefault(); act(async () => { await api.saveSupplierItem(sup.id, item.ingredient_id, Number(item.price), Number(item.pack_size)); await reload() }, 'Saved') }}>
            <select aria-label="Ingredient" value={item.ingredient_id} onChange={(e) => setItem({ ...item, ingredient_id: e.target.value })}>
              <option value="">ingredient…</option>{cfg.ingredients.map((i) => <option key={i.id} value={i.id}>{i.name}</option>)}
            </select>
            <input aria-label="Price" type="number" step="0.01" placeholder="price" value={item.price} onChange={(e) => setItem({ ...item, price: e.target.value })} />
            <input aria-label="Pack size" type="number" step="0.01" placeholder="pack size" value={item.pack_size} onChange={(e) => setItem({ ...item, pack_size: e.target.value })} />
            <button type="submit">Save item</button>
          </form>
          {err && <p className="err small">{err}</p>}{msg && <p className="ok small">{msg}</p>}
        </div>
      )}
    </Panel>
  )
}

function StaffSection({ cfg, reload }) {
  const [day, setDay] = useState(() => new Date().toISOString().slice(0, 10))
  const [period, setPeriod] = useState('dinner')
  const [roster, setRoster] = useState([])
  const { act, err, msg } = useAct()
  useEffect(() => { api.roster(day, period).then(setRoster).catch(() => setRoster([])) }, [day, period])
  const on = new Set(roster.map((r) => r.staff_id))
  const toggle = (s) => setRoster(on.has(s.id) ? roster.filter((r) => r.staff_id !== s.id) : [...roster, { staff_id: s.id, station: s.station, name: s.name, role: s.role, hourly_rate: s.hourly_rate }])
  const cols = [
    { key: 'name', label: 'Name', width: 140 }, { key: 'role', label: 'Role', type: 'select', options: cfg.choices.roles },
    { key: 'station', label: 'Station', type: 'select', options: [['', '–'], ...cfg.stations.map((s) => [s.id, s.name])] },
    { key: 'hourly_rate', label: '$/h', type: 'number', width: 60 }, { key: 'active', label: 'Active', type: 'bool' },
  ]
  const active = cfg.staff.filter((s) => s.active)
  return (<>
    <Panel title="Staff">
      <EditTable rows={cfg.staff} cols={cols} addLabel="Add person"
        onSave={async (id, b, isNew) => { await api.saveStaff(isNew ? null : id, b); await reload() }}
        newRow={() => ({ name: '', role: 'server', station: '', hourly_rate: 18, active: 1 })} />
    </Panel>
    <Panel title="Roster (cooks drive the kitchen's capacity)">
      <div className="form-row">
        <input type="date" aria-label="Date" value={day} onChange={(e) => setDay(e.target.value)} />
        <select aria-label="Meal period" value={period} onChange={(e) => setPeriod(e.target.value)}>{cfg.choices.periods.map((p) => <option key={p}>{p}</option>)}</select>
      </div>
      <ul className="roster">{active.map((s) => (
        <li key={s.id}><label><input type="checkbox" checked={on.has(s.id)} onChange={() => toggle(s)} /> {s.name} <span className="muted small">{s.role}{s.station ? ` · ${s.station}` : ''}</span></label></li>
      ))}</ul>
      <button className="primary" onClick={() => act(async () => {
        const r = await api.saveRoster(day, period, roster.map((x) => ({ staff_id: x.staff_id, station: x.station })))
        return r
      }, 'Roster saved')}>Save roster ({roster.length} people, {money(roster.reduce((a, r) => a + (r.hourly_rate || 0), 0))}/h)</button>
      {err && <p className="err small">{err}</p>}{msg && <p className="ok small">{msg}</p>}
    </Panel>
  </>)
}

const IMPORT_HELP = {
  menu: 'id,name,category,price,station,prep_minutes,recipe,tax_category,is_cold,seasonal_months\nveg_burger,Veg burger,main,17,grill,9,bun:1;lettuce:0.05,food,0,',
  inventory: 'ingredient,qty,unit_cost,expiry_date,received_date,supplier_id,lot_id\nsalmon,8,14.5,2026-11-06,,,',
  pos: 'date,time,item,qty,price,check_id  (see data/importer.py for the column mapping)',
}

function ImportSection() {
  const [kind, setKind] = useState('menu')
  const [text, setText] = useState('')
  const [replace, setReplace] = useState(false)
  const [res, setRes] = useState(null)
  const { act, err } = useAct()
  const pv = csvPreview(text)
  const file = (e) => { const f = e.target.files?.[0]; if (f) f.text().then(setText) }
  return (
    <Panel title="Import menu, inventory or POS history (CSV)" className="span2">
      <div className="form-row">
        <select aria-label="Import kind" value={kind} onChange={(e) => { setKind(e.target.value); setRes(null) }}>
          <option value="menu">Menu + recipes</option><option value="inventory">Inventory counts / lots</option><option value="pos">POS sales history</option>
        </select>
        <input type="file" accept=".csv,text/csv" aria-label="CSV file" onChange={file} />
        {kind === 'inventory' && <label className="small"><input type="checkbox" checked={replace} onChange={(e) => setReplace(e.target.checked)} /> replace current stock</label>}
      </div>
      <textarea aria-label="CSV text" rows={6} className="mono" placeholder={IMPORT_HELP[kind]} value={text} onChange={(e) => setText(e.target.value)} />
      {pv.total > 0 && <p className="small muted">{pv.total} rows · columns: {pv.header.join(', ')}</p>}
      <div className="form-row">
        <button disabled={!text} onClick={() => act(async () => setRes(await api.importCsv(kind, text, true, replace)))}>Dry run</button>
        <button className="primary" disabled={!text} onClick={() => act(async () => setRes(await api.importCsv(kind, text, false, replace)))}>Import</button>
      </div>
      {err && <p className="err small">{err}</p>}
      {res && <pre className="small result-pre">{JSON.stringify(res, null, 1).slice(0, 3000)}</pre>}
    </Panel>
  )
}

function UsersSection({ me }) {
  const [users, setUsers] = useState([])
  const [nu, setNu] = useState({ username: '', display_name: '', role: 'server', password: '' })
  const { act, err, msg } = useAct()
  const load = useCallback(() => api.users().then(setUsers).catch(() => {}), [])
  useEffect(() => { load() }, [load])
  return (
    <Panel title="Users and roles" className="span2">
      <table className="grid"><thead><tr><th>User</th><th>Name</th><th>Role</th><th>Active</th><th>Last login</th><th /></tr></thead>
        <tbody>{users.map((u) => (
          <tr key={u.id} className={u.active ? '' : 'retired'}>
            <td className="mono">{u.username}{u.username === me?.username ? ' (you)' : ''}</td><td>{u.display_name}</td>
            <td><select aria-label={`Role of ${u.username}`} value={u.role} onChange={(e) => act(async () => { await api.patchUser(u.id, { role: e.target.value }); await load() }, 'Role changed')}>
              {['admin', 'manager', 'server', 'kitchen'].map((r) => <option key={r}>{r}</option>)}</select></td>
            <td><input type="checkbox" aria-label={`${u.username} active`} checked={!!u.active} onChange={(e) => act(async () => { await api.patchUser(u.id, { active: e.target.checked }); await load() }, 'Saved')} /></td>
            <td className="small muted">{u.last_login || '–'}</td>
            <td><button className="ghost" onClick={() => { const p = window.prompt(`New password for ${u.username} (min 8 chars)`); if (p) act(() => api.patchUser(u.id, { password: p }), 'Password reset; their sessions were signed out') }}>Reset password</button></td>
          </tr>
        ))}</tbody></table>
      <form className="form-row" onSubmit={(e) => { e.preventDefault(); act(async () => { await api.createUser(nu); setNu({ username: '', display_name: '', role: 'server', password: '' }); await load() }, 'User created') }}>
        <input aria-label="Username" placeholder="username" value={nu.username} onChange={(e) => setNu({ ...nu, username: e.target.value })} />
        <input aria-label="Display name" placeholder="display name" value={nu.display_name} onChange={(e) => setNu({ ...nu, display_name: e.target.value })} />
        <select aria-label="Role" value={nu.role} onChange={(e) => setNu({ ...nu, role: e.target.value })}>{['admin', 'manager', 'server', 'kitchen'].map((r) => <option key={r}>{r}</option>)}</select>
        <input aria-label="Initial password" type="password" placeholder="initial password" value={nu.password} onChange={(e) => setNu({ ...nu, password: e.target.value })} />
        <button className="primary" type="submit">Create user</button>
      </form>
      {err && <p className="err small">{err}</p>}{msg && <p className="ok small">{msg}</p>}
    </Panel>
  )
}

export default function AdminPage({ me }) {
  const [cfg, setCfg] = useState(null)
  const [sec, setSec] = useState('profile')
  const [err, setErr] = useState('')
  const reload = useCallback(() => api.config().then(setCfg).catch((e) => setErr(e.message)), [])
  useEffect(() => { reload() }, [reload])
  if (err) return <div className="page"><p className="err">{err}</p><a href="#/">Back</a></div>
  if (!cfg) return <div className="page"><p className="muted">Loading setup…</p></div>
  const ch = cfg.choices
  const simple = {
    taxes: { title: 'Tax rules (applied per line category and channel)', rows: cfg.taxes,
      cols: [{ key: 'name', label: 'Name', width: 130 }, { key: 'jurisdiction', label: 'Jurisdiction', width: 110 },
        { key: 'rate', label: 'Rate (0.0625 = 6.25%)', type: 'number', width: 80 },
        { key: 'applies_to', label: 'Applies to', type: 'select', options: ch.tax_applies },
        { key: 'channel', label: 'Channel', type: 'select', options: ch.tax_channels }, { key: 'active', label: 'Active', type: 'bool' }],
      save: (id, b, isNew) => api.saveTax(isNew ? null : id, b), del: api.deleteTax,
      newRow: () => ({ name: '', jurisdiction: '', rate: 0.05, applies_to: 'all', channel: 'all', active: 1 }) },
    tables: { title: 'Tables (plan is 1000 x 700; the lobby is at x 640-985, y 488-620)', rows: cfg.tables,
      cols: [{ key: 'id', label: 'Id', readOnlyExisting: true, width: 60 }, { key: 'seats', label: 'Seats', type: 'number', width: 50 },
        { key: 'shape', label: 'Shape', type: 'select', options: ch.shapes }, { key: 'x', label: 'x', type: 'number', width: 60 },
        { key: 'y', label: 'y', type: 'number', width: 60 }, { key: 'w', label: 'w', type: 'number', width: 50 }, { key: 'h', label: 'h', type: 'number', width: 50 },
        { key: 'section', label: 'Section', width: 70 }, { key: 'active', label: 'Active', type: 'bool' }],
      save: (id, b) => api.saveTable(b.id || id, b), del: api.deleteTable,
      newRow: () => ({ id: '', seats: 4, shape: 'rect', x: 100, y: 100, w: 90, h: 60, section: 'A', active: 1 }) },
    stations: { title: 'Kitchen stations', rows: cfg.stations,
      cols: [{ key: 'id', label: 'Id', readOnlyExisting: true, width: 80 }, { key: 'name', label: 'Name', width: 150 },
        { key: 'parallel', label: 'Items per cook', type: 'number', width: 55 }, { key: 'cook_factor', label: 'Cook factor', type: 'number', width: 55 },
        { key: 'lunch_cooks', label: 'Lunch cooks', type: 'number', width: 50 }, { key: 'dinner_cooks', label: 'Dinner cooks', type: 'number', width: 50 },
        { key: 'late_cooks', label: 'Late cooks', type: 'number', width: 50 }, { key: 'active', label: 'Active', type: 'bool' }],
      save: (id, b) => api.saveStation(b.id || id, b), del: api.deleteStation,
      newRow: () => ({ id: '', name: '', parallel: 3, cook_factor: 2.4, lunch_cooks: 1, dinner_cooks: 1, late_cooks: 1, active: 1 }) },
    zones: { title: 'Delivery zones', rows: cfg.zones,
      cols: [{ key: 'id', label: 'Id', readOnlyExisting: true, width: 50 }, { key: 'label', label: 'Label', width: 160 },
        { key: 'fee', label: 'Fee', type: 'number', width: 60 }, { key: 'drive_min', label: 'Drive min', type: 'number', width: 60 },
        { key: 'min_order', label: 'Min order', type: 'number', width: 70 }, { key: 'active', label: 'Active', type: 'bool' }],
      save: (id, b) => api.saveZone(b.id || id, b), del: api.deleteZone,
      newRow: () => ({ id: '', label: '', fee: 4, drive_min: 15, min_order: 0, active: 1 }) },
    ingredients: { title: 'Ingredients', rows: cfg.ingredients,
      cols: [{ key: 'id', label: 'Id', readOnlyExisting: true, width: 110 }, { key: 'name', label: 'Name', width: 130 },
        { key: 'unit', label: 'Unit', type: 'select', options: ch.units }, { key: 'category', label: 'Category', width: 80 },
        { key: 'storage', label: 'Storage', type: 'select', options: ch.storage }, { key: 'shelf_life_days', label: 'Shelf days', type: 'number', width: 55 },
        { key: 'ref_cost', label: 'Cost/unit', type: 'number', width: 65 }, { key: 'allergens', label: 'Allergens', width: 110 }],
      save: (id, b) => api.saveIngredient(b.id || id, b), del: api.deleteIngredient,
      newRow: () => ({ id: '', name: '', unit: 'kg', category: 'produce', storage: 'chilled', shelf_life_days: 5, ref_cost: 5, allergens: '' }) },
  }
  const S = simple[sec]
  return (
    <div className="page admin">
      <header className="page-head">
        <h2>Setup</h2>
        <p className="small muted">Changes are saved to the master database and apply when the next service starts
          {cfg.last_change ? ` · last change ${String(cfg.last_change).slice(0, 19).replace('T', ' ')} UTC` : ''}.</p>
        <a className="btn" href="#/">Back to service</a>
      </header>
      <nav className="tabs admin-tabs" aria-label="Setup sections">
        {SECTIONS.map(([k, l]) => <button key={k} className={sec === k ? 'on' : ''} aria-current={sec === k} onClick={() => setSec(k)}>{l}</button>)}
      </nav>
      <div className="dash">
        {sec === 'profile' && <ProfileSection cfg={cfg} reload={reload} />}
        {sec === 'hours' && <HoursSection cfg={cfg} reload={reload} />}
        {S && (
          <Panel title={S.title} className="span2">
            <EditTable rows={S.rows} cols={S.cols} newRow={S.newRow}
              onSave={async (id, b, isNew) => { await S.save(id, b, isNew); await reload() }}
              onDelete={S.del ? async (id) => { await S.del(id); await reload() } : null} />
          </Panel>
        )}
        {sec === 'menu' && <MenuSection cfg={cfg} reload={reload} />}
        {sec === 'suppliers' && <SuppliersSection cfg={cfg} reload={reload} />}
        {sec === 'staff' && <StaffSection cfg={cfg} reload={reload} />}
        {sec === 'import' && <ImportSection />}
        {sec === 'users' && <UsersSection me={me} />}
        {sec === 'audit' && <AuditSection />}
      </div>
    </div>
  )
}
