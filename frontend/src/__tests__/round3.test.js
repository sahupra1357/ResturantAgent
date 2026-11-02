import { describe, expect, it } from 'vitest'
import { coerce, csvPreview, dishCost, formatRecipe, parseRecipe } from '../admin-logic.js'
import { isRetryable, makeQueue } from '../offline.js'
import { lobbySlots } from '../components/Floor.jsx'
import { reducer, initialState } from '../store.js'

function memStorage() {
  const m = {}
  return { getItem: (k) => (k in m ? m[k] : null), setItem: (k, v) => { m[k] = String(v) }, removeItem: (k) => { delete m[k] } }
}

describe('offline order queue', () => {
  it('keeps orders across reloads and flushes them in order', async () => {
    const st = memStorage()
    const q = makeQueue(st)
    q.add({ key: 'a', path: '/api/orders', body: { table_id: 'T1' }, label: 'T1' })
    q.add({ key: 'b', path: '/api/orders', body: { table_id: 'T2' }, label: 'T2' })
    expect(makeQueue(st).size()).toBe(2) // a new page load sees the same queue
    const sent = []
    const done = await q.flush(async (i) => { sent.push(i.key); return { ok: true } })
    expect(sent).toEqual(['a', 'b'])
    expect(done).toHaveLength(2)
    expect(q.size()).toBe(0)
  })

  it('stops at a retryable failure and drops a permanent one', async () => {
    const q = makeQueue(memStorage())
    for (const k of ['a', 'b', 'c']) q.add({ key: k, path: '/api/orders', body: {} })
    const fail = (retry) => Object.assign(new Error(retry ? 'down' : 'table unknown'), { retry })
    let n = 0
    await q.flush(async (i) => { n++; if (i.key === 'a') throw fail(false); if (i.key === 'b') throw fail(true); return {} })
    expect(n).toBe(2)                              // c was not attempted after b failed retryably
    expect(q.list().map((i) => i.key)).toEqual(['b', 'c'])
    expect(q.list()[0].tries).toBe(1)
  })

  it('treats network errors and 5xx as retryable, 4xx as final', () => {
    expect(isRetryable({ network: true })).toBe(true)
    expect(isRetryable({ status: 503 })).toBe(true)
    expect(isRetryable({ status: 400 })).toBe(false)
    expect(isRetryable({ status: 409 })).toBe(false)
  })
})

describe('setup helpers', () => {
  it('round-trips recipes and prices them', () => {
    const r = parseRecipe('bun:1; beef_patty : 0.2\nlettuce:0.05')
    expect(r).toEqual([{ ingredient_id: 'bun', qty_per_portion: 1 }, { ingredient_id: 'beef_patty', qty_per_portion: 0.2 }, { ingredient_id: 'lettuce', qty_per_portion: 0.05 }])
    expect(parseRecipe(formatRecipe(r))).toEqual(r)
    const c = dishCost(r, [{ id: 'bun', ref_cost: 0.5 }, { id: 'beef_patty', ref_cost: 12 }])
    expect(c.cost).toBe(2.9)
    expect(c.missing).toEqual(['lettuce'])
  })

  it('coerces form strings to column types and previews CSV', () => {
    const cols = [{ key: 'price', type: 'number' }, { key: 'is_cold', type: 'bool' }, { key: 'name' }]
    expect(coerce({ price: '12.5', is_cold: true, name: 'x' }, cols)).toEqual({ price: 12.5, is_cold: 1, name: 'x' })
    const p = csvPreview('id,name\na,A\nb,B\n')
    expect(p.header).toEqual(['id', 'name'])
    expect(p.total).toBe(2)
  })
})

describe('2D lobby', () => {
  const lobby = { x: 640, y: 488, w: 345, h: 132 }
  it('draws waiting parties inside the lobby and counts the overflow', () => {
    const wl = Array.from({ length: 20 }, (_, i) => ({ party_id: `W${i}`, size: 2, name: `N${i}`, waited_min: i }))
    const { slots, hidden } = lobbySlots(lobby, wl)
    expect(slots.length + hidden).toBe(20)
    for (const s of slots) {
      expect(s.x).toBeGreaterThanOrEqual(lobby.x)
      expect(s.x + 110).toBeLessThanOrEqual(lobby.x + lobby.w)
      expect(s.y + 20).toBeLessThanOrEqual(lobby.y + lobby.h)
    }
  })
})

describe('store auth handling', () => {
  it('marks the session as gone on an auth message', () => {
    const s = reducer({ ...initialState, connected: true }, { type: 'auth', required: true })
    expect(s.authRequired).toBe(true)
    expect(s.connected).toBe(false)
  })
})
