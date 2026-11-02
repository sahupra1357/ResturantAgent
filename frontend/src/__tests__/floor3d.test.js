import { describe, expect, it } from 'vitest'
import { chairSpots, colorForParty, personTargets, stepToward, toWorld, WAIT_ORIGIN } from '../floor3d.js'

const T = (id, seats, shape, extra = {}) => ({ id, seats, shape, x: 100, y: 100, w: shape === 'rect' ? 120 : 62, h: shape === 'rect' ? 84 : 62, state: 'available', ...extra })

describe('3D floor scene math', () => {
  it('maps the plan centre to the world origin', () => {
    expect(toWorld(500, 310)).toEqual({ x: 0, z: 0 })
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
