async function call(method, path, body) {
  const r = await fetch(path, {
    method,
    headers: body ? { 'Content-Type': 'application/json' } : {},
    body: body ? JSON.stringify(body) : undefined,
  })
  const data = await r.json().catch(() => ({}))
  if (!r.ok) throw new Error(data.detail || `${r.status} ${r.statusText}`)
  return data
}

export const api = {
  start: (body = {}) => call('POST', '/api/service/start', body),
  speed: (speed) => call('POST', '/api/clock', { speed }),
  scenario: (name) => call('POST', `/api/scenarios/${name}`),
  tableOrder: (body) => call('POST', '/api/orders', body),
  onlineMenu: () => call('GET', '/api/online/menu'),
  quote: (dish_ids) => call('POST', '/api/online/quote', { dish_ids }),
  onlineOrder: (body) => call('POST', '/api/online/orders', body),
  onlineMode: (mode) => call('POST', '/api/online/mode', { mode }),
  override: (id, ready_hhmm) => call('POST', `/api/online/orders/${id}/override`, { ready_hhmm }),
  notify: (id, text) => call('POST', `/api/online/orders/${id}/notify`, { text }),
  resolve: (id, body) => call('POST', `/api/decisions/${id}`, body),
  services: () => call('GET', '/api/services'),
  baseline: () => call('GET', '/api/baseline'),
}
