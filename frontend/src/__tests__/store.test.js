import { describe, expect, it } from 'vitest'
import { applyEvent, initialState, reducer } from '../store.js'

const ev = (type, payload, seq = 1) => ({ type, payload, seq, source: 'test', sim_ts: '2026-11-02T18:00:00' })

describe('store reducer', () => {
  it('replaces state on snapshot and reverses the feed (newest first)', () => {
    const s = reducer(initialState, { type: 'snapshot', payload: { tables: { T1: { id: 'T1' } }, feed: [{ seq: 1 }, { seq: 2 }] } })
    expect(s.ready).toBe(true)
    expect(s.feed.map((f) => f.seq)).toEqual([2, 1])
  })

  it('folds table, decision, host, check, prep and safety events', () => {
    let s = { ...initialState, tables: { T1: { id: 'T1', state: 'available' } } }
    s = applyEvent(s, ev('table.updated', { tables: [{ id: 'T1', state: 'seated' }] }))
    s = applyEvent(s, ev('decision.proposed', { id: 7, status: 'pending', title: '86 salmon' }, 2))
    s = applyEvent(s, ev('decision.resolved', { id: 7, status: 'executed', title: '86 salmon' }, 3))
    s = applyEvent(s, ev('host.updated', { bookings: [{ booking_id: 'B001' }], waitlist: [] }, 4))
    s = applyEvent(s, ev('check.updated', { check_id: 'C0001', status: 'open' }, 5))
    s = applyEvent(s, ev('prep.updated', { dishes: [], cooks: { grill: 3 } }, 6))
    s = applyEvent(s, ev('safety.updated', { units: [], checklist: [] }, 7))
    expect(s.tables.T1.state).toBe('seated')
    expect(s.decisions[7].status).toBe('executed')
    expect(s.host.bookings).toHaveLength(1)
    expect(s.checks.C0001.status).toBe('open')
    expect(s.prep.cooks.grill).toBe(3)
    expect(s.safety.units).toEqual([])
  })

  it('keeps high-frequency events out of the activity feed', () => {
    let s = applyEvent(initialState, ev('kitchen.load', { stations: [] }))
    s = applyEvent(s, ev('clock.tick', { hhmm: '18:00' }, 2))
    expect(s.feed).toHaveLength(0)
    s = applyEvent(s, ev('stock.low', { dish: 'Salmon', remaining_portions: 6, forecast_remaining: 15, ingredient: 'Salmon' }, 3))
    expect(s.feed).toHaveLength(1)
    s = applyEvent(s, ev('quote.updated', { order_id: 'W1', notify: true, notifications: [] }, 4))
    expect(s.feed).toHaveLength(2)
  })

  it('caps the feed at 250 entries', () => {
    let s = initialState
    for (let i = 0; i < 300; i++) s = applyEvent(s, ev('agent.log', { agent: 'menu', message: `m${i}` }, i))
    expect(s.feed).toHaveLength(250)
    expect(s.feed[0].seq).toBe(299)
  })

  it('a replay starts from an empty state', () => {
    const s = reducer({ ...initialState, tables: { T1: {} } }, { type: 'replay.start', payload: { service_id: 'svc-x', events: 10 } })
    expect(s.tables).toEqual({})
    expect(s.replay.service_id).toBe('svc-x')
  })
})
