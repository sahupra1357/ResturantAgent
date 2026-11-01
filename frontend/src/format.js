export const AGENT_LABEL = {
  order: 'Order', inventory: 'Inventory', forecasting: 'Forecasting', procurement: 'Procurement', menu: 'Menu',
  coordinator: 'Coordinator', scenario: 'Scenario', manager: 'Manager',
}

export const STATE_LABEL = {
  available: 'Available', seated: 'Seated', ordered: 'Ordered', entrees_fired: 'Entrées fired', eating: 'Eating',
  check: 'Check', bussing: 'Needs bussing',
}

export const STATION_LABEL = { grill: 'Grill', saute: 'Sauté', cold: 'Cold', pastry: 'Pastry' }

export const money = (x) => (x == null ? '–' : `$${Number(x).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`)
export const num = (x, d = 0) => (x == null ? '–' : Number(x).toFixed(d))
export const hhmm = (iso) => (iso ? iso.slice(11, 16) : '–')

export function dishName(state, id) {
  return state.dishes?.[id]?.name || id
}

// One line of text for an event in the activity stream.
export function describe(ev, state) {
  const p = ev.payload || {}
  const d = (id) => dishName(state, id)
  switch (ev.type) {
    case 'order.created':
      return `${p.channel === 'online' ? 'Online ' : ''}${p.order_id}: ${p.n_items ?? p.items?.length ?? 0} items → ${(p.stations || []).map((s) => STATION_LABEL[s] || s).join(', ') || '–'}`
        + `${p.allergens?.length ? ` · allergy ${p.allergens.join('/')}` : ''}${p.held ? ` · ${p.held} held` : ''} · ${p.latency_ms} ms`
    case 'party.arrived': return `Party of ${p.size} (${p.kind.replace('_', ' ')}) ${p.seated ? `seated at ${p.table_ids.join('+')}` : 'waiting'}`
    case 'party.left': return `Party of ${p.size} left (${p.reason})`
    case 'stock.low': return `Low: ${p.dish} ${p.remaining_portions} left vs ${num(p.forecast_remaining)} forecast (${p.ingredient})`
    case 'stock.out': return `Out of stock: ${p.name || d(p.dish_id)}`
    case 'lot.expiring': return `Expiring: ${p.ingredient} ${p.qty} ${p.unit} (${money(p.value)}) by ${p.expiry_date}`
    case 'lot.quarantined': return `Quarantined ${p.lots?.length} lots (${money(p.value)}): ${p.reason}`
    case 'supplier.shortfall': return `Supplier shortfall: ${p.supplier_id} fills ${Math.round(p.fill_rate * 100)}% of ${p.ingredient_id} until ${p.until}`
    case 'po.sent': return `PO ${p.po_id} sent: ${money(p.total)}, delivers ${p.expected_delivery}`
    case 'forecast.miss': return `Forecast miss ${p.miss_pct > 0 ? '+' : ''}${p.miss_pct}%: ${p.reason}`
    case 'weather.alert': return `Weather alert: ${p.alert} ${p.temp_c} °C`
    case 'online.mode': return `Online ordering → ${p.mode.replace('_', ' ')} (${p.reason})`
    case 'quote.updated': return `${p.order_id}: ${p.notifications?.[p.notifications.length - 1]?.text || 'quote updated'}`
    case 'decision.proposed': return `${AGENT_LABEL[p.agent] || p.agent} proposes: ${p.title}${p.status === 'pending' ? ' (needs approval)' : ''}`
    case 'decision.resolved': return `#${p.id} ${p.status}: ${p.title}${p.note && p.status !== 'executed' ? ` (${p.note})` : ''}`
    case 'agent.log': return `${AGENT_LABEL[p.agent] || p.agent}: ${p.message}`
    case 'scenario.injected': return `▶ ${p.label}`
    case 'item.held': return `Held ${d(p.dish_id)}: allergy ${p.conflict?.join('/')}`
    case 'service.ended': return 'Service ended'
    case 'order.rejected': return `Order rejected: ${p.reason}`
    default: return ev.type
  }
}
