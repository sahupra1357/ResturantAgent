// REST client. Sign-in sets an HttpOnly session cookie (never readable here); every request carries it, and
// writes add the CSRF header the server requires for cookie sessions. No tokens in URLs or localStorage.
import { isRetryable, newKey, queue } from './offline.js'

export const AUTH_EVENT = 'ro-auth-required'

export async function call(method, path, body, extraHeaders = {}) {
  const headers = { 'X-Requested-With': 'ro-ui', ...extraHeaders }
  if (body !== undefined) headers['Content-Type'] = 'application/json'
  let r
  try {
    r = await fetch(path, { method, headers, credentials: 'same-origin', body: body !== undefined ? JSON.stringify(body) : undefined })
  } catch (e) {
    const err = new Error('Cannot reach the server')
    err.network = true
    throw err
  }
  const data = await r.json().catch(() => ({}))
  if (!r.ok) {
    const detail = typeof data.detail === 'string' ? data.detail : Array.isArray(data.detail) ? data.detail.map((d) => d.msg).join('; ') : ''
    const err = new Error(detail || `${r.status} ${r.statusText}`)
    err.status = r.status
    if (r.status === 401 && typeof window !== 'undefined' && !path.startsWith('/api/auth/')) window.dispatchEvent(new Event(AUTH_EVENT))
    throw err
  }
  return data
}

// A table order: sent with an Idempotency-Key; queued for later when the server cannot be reached.
export async function sendTableOrder(body, label = '') {
  const key = newKey()
  try {
    return await call('POST', '/api/orders', body, { 'Idempotency-Key': key })
  } catch (e) {
    if (!isRetryable(e)) throw e
    queue.add({ key, path: '/api/orders', body, label })
    return { queued: true, key, message: `Saved offline: ${label || 'order'} will be sent when the connection is back` }
  }
}

export function flushQueue() {
  return queue.flush(async (item) => {
    try {
      return await call('POST', item.path, item.body, { 'Idempotency-Key': item.key })
    } catch (e) {
      e.retry = isRetryable(e)
      throw e
    }
  })
}

const q = (o) => new URLSearchParams(Object.entries(o).filter(([, v]) => v !== null && v !== undefined && v !== '')).toString()

export const api = {
  // identity
  me: () => call('GET', '/api/auth/me'),
  login: (username, password) => call('POST', '/api/auth/login', { username, password }),
  logout: () => call('POST', '/api/auth/logout'),
  changePassword: (current_password, new_password) => call('POST', '/api/auth/password', { current_password, new_password }),
  // service
  start: (body = {}) => call('POST', '/api/service/start', body),
  speed: (speed) => call('POST', '/api/clock', { speed }),
  scenario: (name) => call('POST', `/api/scenarios/${name}`),
  tableOrder: (body, label) => sendTableOrder(body, label),
  publicState: () => call('GET', '/api/public/state'),
  onlineMenu: () => call('GET', '/api/online/menu'),
  quote: (dish_ids, fulfilment = 'pickup', zone = null) => call('POST', '/api/online/quote', { dish_ids, fulfilment, zone }),
  onlineOrder: (body) => call('POST', '/api/online/orders', body, { 'Idempotency-Key': newKey() }),
  onlineMode: (mode) => call('POST', '/api/online/mode', { mode }),
  override: (id, ready_hhmm) => call('POST', `/api/online/orders/${id}/override`, { ready_hhmm }),
  notify: (id, text) => call('POST', `/api/online/orders/${id}/notify`, { text }),
  resolve: (id, body) => call('POST', `/api/decisions/${id}`, body),
  services: () => call('GET', '/api/services'),
  baseline: () => call('GET', '/api/baseline'),
  // host stand
  book: (body) => call('POST', '/api/reservations', body),
  bookingAction: (id, action) => call('POST', `/api/reservations/${id}/${action}`),
  waitlist: () => call('GET', '/api/waitlist'),
  addWaitlist: (body) => call('POST', '/api/waitlist', body),
  seatWaiting: (pid, table_ids = null) => call('POST', `/api/waitlist/${pid}/seat`, { table_ids }),
  moveParty: (party_id, table_ids) => call('POST', '/api/tables/move', { party_id, table_ids }),
  combine: (party_id, table_id) => call('POST', '/api/tables/combine', { party_id, table_id }),
  splitTables: (party_id, table_id) => call('POST', '/api/tables/split', { party_id, table_id }),
  // checks, gift cards
  splitCheck: (id, mode, ways) => call('POST', `/api/checks/${id}/split`, { mode, ways }),
  pay: (id, body) => call('POST', `/api/checks/${id}/pay`, body),
  discount: (id, body) => call('POST', `/api/checks/${id}/discount`, body),
  refund: (id, body) => call('POST', `/api/checks/${id}/refund`, body),
  issueGift: (amount, note = '') => call('POST', '/api/giftcards', { amount, note }),
  gift: (code) => call('GET', `/api/giftcards/${encodeURIComponent(code)}`),
  voidGift: (code, reason) => call('POST', `/api/giftcards/${encodeURIComponent(code)}/void`, { reason }),
  // guests
  guests: (qs = '') => call('GET', `/api/guests?q=${encodeURIComponent(qs)}`),
  guest: (id) => call('GET', `/api/guests/${id}`),
  createGuest: (body) => call('POST', '/api/guests', body),
  patchGuest: (id, body) => call('PATCH', `/api/guests/${id}`, body),
  // kitchen
  kds: (station, include_bumped = false) => call('GET', `/api/kds?${q({ station, include_bumped })}`),
  bump: (order_id, station) => call('POST', '/api/kds/bump', { order_id, station }),
  haccp: (id, ok = true, note = '') => call('POST', `/api/safety/checklist/${id}`, { ok, note }),
  logTemps: () => call('POST', '/api/safety/log'),
  // analytics and back office
  waste: (days = 30) => call('GET', `/api/waste?days=${days}`),
  menuEngineering: () => call('GET', '/api/menu/engineering'),
  suppliers: () => call('GET', '/api/suppliers'),
  purchaseOrders: () => call('GET', '/api/purchase-orders'),
  resendPo: (id) => call('POST', `/api/purchase-orders/${id}/resend`),
  llm: () => call('GET', '/api/llm'),
  dayClosePreview: (counted_cash) => call('GET', `/api/dayclose/preview?${q({ counted_cash })}`),
  dayClose: (counted_cash, force = false) => call('POST', '/api/dayclose', { counted_cash, force }),
  dayCloseHistory: () => call('GET', '/api/dayclose/history'),
  backups: () => call('GET', '/api/ops/backups'),
  backupNow: () => call('POST', '/api/ops/backups'),
  verifyBackup: (id) => call('POST', `/api/ops/backups/${id}/verify`),
  drill: () => call('POST', '/api/ops/drill'),
  audit: (username) => call('GET', `/api/admin/audit?${q({ username, limit: 200 })}`),
  // admin (setup)
  config: () => call('GET', '/api/admin/config'),
  saveProfile: (body) => call('PUT', '/api/admin/profile', body),
  saveTax: (id, body) => (id ? call('PUT', `/api/admin/taxes/${id}`, body) : call('POST', '/api/admin/taxes', body)),
  deleteTax: (id) => call('DELETE', `/api/admin/taxes/${id}`),
  saveHours: (body) => call('PUT', '/api/admin/hours', body),
  saveTable: (id, body) => call('PUT', `/api/admin/tables/${encodeURIComponent(id)}`, body),
  deleteTable: (id) => call('DELETE', `/api/admin/tables/${encodeURIComponent(id)}`),
  saveStation: (id, body) => call('PUT', `/api/admin/stations/${id}`, body),
  deleteStation: (id) => call('DELETE', `/api/admin/stations/${id}`),
  saveZone: (id, body) => call('PUT', `/api/admin/zones/${id}`, body),
  deleteZone: (id) => call('DELETE', `/api/admin/zones/${id}`),
  saveIngredient: (id, body) => call('PUT', `/api/admin/ingredients/${id}`, body),
  deleteIngredient: (id) => call('DELETE', `/api/admin/ingredients/${id}`),
  saveDish: (id, body) => call('PUT', `/api/admin/dishes/${id}`, body),
  dishAction: (id, action) => call('POST', `/api/admin/dishes/${id}/${action}`),
  saveSupplier: (id, body) => call('PUT', `/api/admin/suppliers/${id}`, body),
  saveSupplierItem: (sid, iid, price, pack_size) => call('PUT', `/api/admin/suppliers/${sid}/items/${iid}`, { price, pack_size }),
  deleteSupplierItem: (sid, iid) => call('DELETE', `/api/admin/suppliers/${sid}/items/${iid}`),
  saveStaff: (id, body) => (id ? call('PUT', `/api/admin/staff/${id}`, body) : call('POST', '/api/admin/staff', body)),
  roster: (date, meal_period) => call('GET', `/api/admin/roster?${q({ date, meal_period })}`),
  saveRoster: (date, meal_period, entries) => call('PUT', '/api/admin/roster', { date, meal_period, entries }),
  importCsv: (kind, csv, dry_run, replace_stock = false) => call('POST', `/api/admin/import/${kind}`, { csv, dry_run, replace_stock }),
  users: () => call('GET', '/api/admin/users'),
  createUser: (body) => call('POST', '/api/admin/users', body),
  patchUser: (id, body) => call('PATCH', `/api/admin/users/${id}`, body),
  restore: (id) => call('POST', `/api/admin/restore/${id}`),
}
