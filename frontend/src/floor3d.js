// Pure scene math for the 3D dining floor (no three.js here, so it is unit-tested with vitest).
// Plan coordinates (the 2D floor's 1000 x 620 viewBox) map to world units: 40 px = 1 m-ish unit.

export const SCALE = 40
export const DOOR = { x: 12.6, z: 3.3 }           // entrance by the host stand
export const WAIT_ORIGIN = { x: 7.6, z: 6.6 }     // waiting area in front of the host stand
export const WAIT_COLS = 9

export function toWorld(px, py) {
  return { x: (px - 500) / SCALE, z: (py - 310) / SCALE }
}

export function tableCenter(t) {
  return toWorld(t.x + t.w / 2, t.y + t.h / 2)
}

// Chair positions around one table, in world units, each facing the table centre.
export function chairSpots(t) {
  const c = tableCenter(t)
  const out = []
  if (t.shape === 'bar') {
    // a stool in front of the counter (counter is below the bar seats on the plan)
    return [{ x: c.x, z: c.z - 0.15, rot: 0 }]
  }
  if (t.shape === 'round') {
    const r = t.w / 2 / SCALE + 0.38
    for (let i = 0; i < t.seats; i++) {
      const a = (i / t.seats) * Math.PI * 2 + Math.PI / 2
      out.push({ x: c.x + Math.cos(a) * r, z: c.z + Math.sin(a) * r, rot: -a + Math.PI / 2 })
    }
    return out
  }
  // rectangle: seats split along the two long sides (top / bottom on the plan)
  const w = t.w / SCALE
  const h = t.h / SCALE
  const perSide = Math.ceil(t.seats / 2)
  for (let side = 0; side < 2; side++) {
    const n = side === 0 ? perSide : t.seats - perSide
    for (let i = 0; i < n; i++) {
      const x = c.x - w / 2 + (w * (i + 0.5)) / n
      const z = side === 0 ? c.z - h / 2 - 0.36 : c.z + h / 2 + 0.36
      out.push({ x, z, rot: side === 0 ? 0 : Math.PI })
    }
  }
  return out
}

// Where every guest should be: seated at a chair of their party's table(s), or standing in the waiting area.
// tables: {id: tableView}; waitlist: [{party_id, size}]. Returns {personId: {x, z, pose, partyId, tableId}}.
export function personTargets(tables, waitlist = []) {
  const byParty = {}
  for (const t of Object.values(tables || {})) {
    if (!t.party_id || t.state === 'available' || t.state === 'bussing') continue
    ;(byParty[t.party_id] = byParty[t.party_id] || { size: t.party_size, tables: [] }).tables.push(t)
  }
  const out = {}
  for (const [pid, info] of Object.entries(byParty)) {
    const chairs = []
    for (const t of info.tables.sort((a, b) => a.id.localeCompare(b.id, undefined, { numeric: true }))) {
      for (const ch of chairSpots(t)) chairs.push({ ...ch, tableId: t.id })
    }
    const n = Math.min(info.size || 0, chairs.length)
    for (let i = 0; i < n; i++) {
      out[`${pid}-${i}`] = { x: chairs[i].x, z: chairs[i].z, rot: chairs[i].rot, pose: 'sit', partyId: pid, tableId: chairs[i].tableId }
    }
  }
  let k = 0
  for (const w of waitlist || []) {
    for (let i = 0; i < (w.size || 0); i++) {
      const col = k % WAIT_COLS
      const row = Math.floor(k / WAIT_COLS)
      out[`${w.party_id}-${i}`] = { x: WAIT_ORIGIN.x - col * 0.55, z: WAIT_ORIGIN.z + row * 0.55, rot: Math.PI, pose: 'stand', partyId: w.party_id, tableId: null }
      k++
    }
    k += 1 // a gap between waiting parties
  }
  return out
}

// Stable, distinct-ish color per party (HSL from a string hash).
export function colorForParty(id) {
  // FNV-1a hash, then the golden angle spreads consecutive ids (P001, P002, ...) far apart on the colour wheel
  let h = 2166136261
  for (const ch of String(id)) h = Math.imul(h ^ ch.charCodeAt(0), 16777619) >>> 0
  const hue = Math.round((h % 1000) * 137.508) % 360
  return `hsl(${hue}, 62%, 52%)`
}

export const STATE_COLORS = {
  available: '#d9dfd9', seated: '#93c5fd', ordered: '#a5b4fc', entrees_fired: '#fdba74',
  eating: '#86efac', check: '#d8b4fe', bussing: '#a8a29e',
}

// Move a point toward a target at `speed` units/s; returns the new point and whether it arrived.
export function stepToward(p, target, speed, dt) {
  const dx = target.x - p.x
  const dz = target.z - p.z
  const d = Math.hypot(dx, dz)
  const step = speed * dt
  if (d <= step || d < 1e-6) return { x: target.x, z: target.z, arrived: true, heading: p.heading ?? 0 }
  return { x: p.x + (dx / d) * step, z: p.z + (dz / d) * step, arrived: false, heading: Math.atan2(dx, dz) }
}
