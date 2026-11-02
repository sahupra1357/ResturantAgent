import { describe, expect, it } from 'vitest'
import { newPending } from '../alerts.js'
import { availability } from '../components/OrderDialog.jsx'
import { describe as describeEvent } from '../format.js'

describe('approval alerts', () => {
  it('reports only decisions that became pending since last time', () => {
    const d1 = { 1: { id: 1, status: 'pending' }, 2: { id: 2, status: 'executed' } }
    const a = newPending(new Set(), d1)
    expect(a.fresh.map((d) => d.id)).toEqual([1])
    const b = newPending(a.ids, { ...d1, 3: { id: 3, status: 'pending' } })
    expect(b.fresh.map((d) => d.id)).toEqual([3])
    expect([...b.ids].sort()).toEqual([1, 3])
  })
})

describe('menu availability', () => {
  it("hides 86'd and out-of-season dishes and applies '86 after N' limits", () => {
    const state = {
      dishes: { a: {}, b: {}, c: {}, d: {} },
      inventory: { portions: { a: 10, b: 10, c: 10, d: 0 } },
      menu: { eighty_sixed: [{ dish_id: 'b' }], season_off: ['c'], limits: { a: 3 } },
    }
    const av = availability(state)
    expect(av.a).toBe(3)
    expect(av.b).toBe(0)
    expect('c' in av).toBe(false)
    expect(av.d).toBe(0)
  })
})

describe('event descriptions', () => {
  it('describes key events in one line', () => {
    const st = { dishes: {} }
    expect(describeEvent({ type: 'supplier.learned', payload: { supplier_id: 'sup_freshcatch', fill_rate_before: 0.93, fill_rate: 0.86, reason: 'salmon shortfall' } }, st))
      .toContain('93% → 86%')
    expect(describeEvent({ type: 'temp.alert', payload: { message: 'Walk-in cooler at 9.8 °C' } }, st)).toContain('9.8')
    expect(describeEvent({ type: 'unknown.thing', payload: {} }, st)).toBe('unknown.thing')
  })
})
