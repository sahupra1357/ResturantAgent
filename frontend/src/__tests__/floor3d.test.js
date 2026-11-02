import { describe, expect, it } from 'vitest'
import { chairSpots, colorForParty, personTargets, stepToward, toWorld, WAIT_COLS, WAIT_ORIGIN, waitSpots } from '../floor3d.js'

const T = (id, seats, shape, extra = {}) => ({ id, seats, shape, x: 100, y: 100, w: shape === 'rect' ? 120 : 62, h: shape === 'rect' ? 84 : 62, state: 'available', ...extra })

describe('3D floor scene math', () => {
  it('maps the plan centre to the world origin', () => {
    expect(toWorld(500, 350)).toEqual({ x: 0, z: 0 })
  })

  it('places one chair per seat', () => {
    expect(chairSpots(T('T1', 2, 'round'))).toHaveLength(2)
    expect(chairSpots(T('T17', 6, 'rect'))).toHaveLength(6)
    expect(chairSpots(T('B1', 1, 'bar'))).toHaveLength(1)
  })

  it('seats a party at its table and spreads a big party across joined tables', () => {
    const tables = {
      T1: T('T1', 2, 'round', { state: 'eating', party_id: 'P1', party_size: 2 }),
      T17: T('T17', 6, 'rect', { state: 'ordered', party_id: 'P2', party_size: 12 }),
      T18: T('T18', 6, 'rect', { state: 'ordered', party_id: 'P2', party_size: 12, x: 240 }),
      T2: T('T2', 2, 'round', { state: 'bussing', party_id: 'P3', party_size: 2 }),
    }
    const p = personTargets(tables, [{ party_id: 'P9', size: 3 }])
    const byParty = (pid) => Object.values(p).filter((x) => x.partyId === pid)
    expect(byParty('P1')).toHaveLength(2)
    expect(byParty('P2')).toHaveLength(12)
    expect(new Set(byParty('P2').map((x) => x.tableId))).toEqual(new Set(['T17', 'T18']))
    expect(byParty('P3')).toHaveLength(0) // bussing: guests have left
    const waiting = byParty('P9')
    expect(waiting).toHaveLength(3)
    expect(waiting.every((x) => x.pose === 'stand' && x.z >= WAIT_ORIGIN.z)).toBe(true)
  })

  it('queues waiting parties inside the lobby without splitting a party across rows', () => {
    // lobby on the plan: x 640-985, y 488-620
    const lobby = { x0: toWorld(640, 0).x, x1: toWorld(985, 0).x, z0: toWorld(0, 488).z, z1: toWorld(0, 620).z }
    const wl = Array.from({ length: 8 }, (_, i) => ({ party_id: `W${i}`, size: 2 + (i % 4), checking_in: i === 0 }))
    const p = personTargets({}, wl)
    const pts = Object.values(p)
    expect(pts).toHaveLength(wl.reduce((a, w) => a + w.size, 0))
    for (const q of pts) {
      expect(q.x).toBeGreaterThanOrEqual(lobby.x0)
      expect(q.x).toBeLessThanOrEqual(lobby.x1)
      expect(q.z).toBeGreaterThanOrEqual(lobby.z0)
      expect(q.z).toBeLessThanOrEqual(lobby.z1)
    }
    for (const w of wl) expect(new Set(pts.filter((q) => q.partyId === w.party_id).map((q) => q.z)).size).toBe(1)
    expect(pts.filter((q) => q.checkingIn)).toHaveLength(2)
    expect(Object.keys(waitSpots([{ party_id: 'X', size: 40 }]))).toEqual(['X'])
    expect(waitSpots([{ party_id: 'X', size: 40 }]).X.n).toBe(WAIT_COLS)
  })

  it('gives each party a stable colour', () => {
    expect(colorForParty('P001')).toBe(colorForParty('P001'))
    expect(colorForParty('P001')).not.toBe(colorForParty('P002'))
  })

  it('walks toward a target and arrives', () => {
    let p = { x: 0, z: 0 }
    for (let i = 0; i < 20; i++) p = stepToward(p, { x: 3, z: 4 }, 3, 0.1)
    expect(p.arrived).toBe(true)
    expect([p.x, p.z]).toEqual([3, 4])
  })
})
