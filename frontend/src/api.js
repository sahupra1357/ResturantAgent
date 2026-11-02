// REST client. Mutating endpoints need a role token (manager / server / kitchen), kept in localStorage.
const TOKEN_KEY = 'ro-token'

export function getToken() {
  try { return localStorage.getItem(TOKEN_KEY) || '' } catch { return '' }
}
export function setToken(t) {
  try { t ? localStorage.setItem(TOKEN_KEY, t) : localStorage.removeItem(TOKEN_KEY) } catch { /* private mode */ }
}

async function call(method, path, body) {
  const headers = {}
  if (body) headers['Content-Type'] = 'application/json'
  const tok = getToken()
  if (tok) headers.Authorization = `Bearer ${tok}`
  const r = await fetch(path, { method, headers, body: body ? JSON.stringify(body) : undefined })
  const data = await r.json().catch(() => ({}))
  if (!r.ok) {
    const err = new Error(r.status === 401 ? 'Sign in with a role token to do this' : data.detail || `${r.status} ${r.statusText}`)
    err.status = r.status
    throw err
  }
  return data
}

// links that open in a new tab carry the token as a query parameter
export const withToken = (path) => {
  const t = getToken()
  return t ? `${path}${path.includes('?') ? '&' : '?'}token=${encodeURIComponent(t)}` : path
}

export const api = {
  whoami: () => call('GET', '/api/whoami'),
  start: (body = {}) => call('POST', '/api/service/start', body),
  speed: (speed) => call('POST', '/api/clock', { speed }),
  scenario: (name) => call('POST', `/api/scenarios/${name}`),
  tableOrder: (body) => call('POST', '/api/orders', body),
  onlineMenu: () => call('GET', '/api/online/menu'),
  quote: (dish_ids, fulfilment = 'pickup', zone = null) => call('POST', '/api/online/quote', { dish_ids, fulfilment, zone }),
  onlineOrder: (body) => call('POST', '/api/online/orders', body),
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
  // checks
  splitCheck: (id, mode, ways) => call('POST', `/api/checks/${id}/split`, { mode, ways }),
  pay: (id, body) => call('POST', `/api/checks/${id}/pay`, body),
  discount: (id, body) => call('POST', `/api/checks/${id}/discount`, body),
  refund: (id, body) => call('POST', `/api/checks/${id}/refund`, body),
  // guests
  guests: (q = '') => call('GET', `/api/guests?q=${encodeURIComponent(q)}`),
  guest: (id) => call('GET', `/api/guests/${id}`),
  createGuest: (body) => call('POST', '/api/guests', body),
  patchGuest: (id, body) => call('PATCH', `/api/guests/${id}`, body),
  // kitchen / analytics
  haccp: (id, ok = true, note = '') => call('POST', `/api/safety/checklist/${id}`, { ok, note }),
  logTemps: () => call('POST', '/api/safety/log'),
  waste: (days = 30) => call('GET', `/api/waste?days=${days}`),
  menuEngineering: () => call('GET', '/api/menu/engineering'),
  suppliers: () => call('GET', '/api/suppliers'),
}
