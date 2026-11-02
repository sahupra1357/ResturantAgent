import { useEffect, useMemo, useRef, useState } from 'react'
import * as THREE from 'three'
import { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js'
import { STATE_LABEL } from '../format.js'
import { DOOR, ROOM, STATE_COLORS, chairSpots, colorForParty, personTargets, stepToward, tableCenter, toWorld, waitSpots } from '../floor3d.js'
import OrderDialog from './OrderDialog.jsx'

const WALK_SPEED = 3.2 // world units per second
const reduceMotion = () => typeof window !== 'undefined' && window.matchMedia?.('(prefers-reduced-motion: reduce)').matches

function labelSprite(text, sub) {
  const c = document.createElement('canvas')
  c.width = 192
  c.height = 72
  const g = c.getContext('2d')
  g.fillStyle = 'rgba(20,22,26,0.78)'
  g.beginPath()
  g.roundRect(4, 4, 184, 64, 12)
  g.fill()
  g.fillStyle = '#fff'
  g.font = 'bold 28px system-ui, sans-serif'
  g.textAlign = 'center'
  g.fillText(text, 96, sub ? 34 : 46)
  if (sub) {
    g.font = '20px system-ui, sans-serif'
    g.fillStyle = '#d6d3d1'
    g.fillText(sub, 96, 60)
  }
  const tex = new THREE.CanvasTexture(c)
  tex.colorSpace = THREE.SRGBColorSpace
  const s = new THREE.Sprite(new THREE.SpriteMaterial({ map: tex, depthTest: false }))
  s.scale.set(1.25, 0.47, 1)
  s.renderOrder = 10
  return s
}

// A guest: body + head, colored by party. Seated guests are lower; walking guests bob.
function makePerson(color, geo) {
  const grp = new THREE.Group()
  const mat = new THREE.MeshStandardMaterial({ color, roughness: 0.7 })
  const body = new THREE.Mesh(geo.body, mat)
  body.position.y = 0.42
  const head = new THREE.Mesh(geo.head, new THREE.MeshStandardMaterial({ color: '#f1c9a5', roughness: 0.8 }))
  head.position.y = 0.95
  grp.add(body, head)
  grp.userData = { body, head }
  return grp
}

export default function Floor3D({ state, onFallback }) {
  const mount = useRef(null)
  const ctx = useRef(null)
  const [sel, setSel] = useState(null)
  const [err, setErr] = useState('')
  const tables = state.tables || {}
  const waitlist = state.host?.waitlist || []
  const targets = useMemo(() => personTargets(tables, waitlist), [tables, waitlist])
  const spots = useMemo(() => waitSpots(waitlist), [waitlist])

  // build the scene once
  useEffect(() => {
    const el = mount.current
    let renderer
    try {
      renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false })
    } catch (e) {
      setErr('3D view needs WebGL, which this browser does not provide.')
      onFallback?.()
      return undefined
    }
    const dark = window.matchMedia?.('(prefers-color-scheme: dark)').matches
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2))
    el.appendChild(renderer.domElement)
    renderer.domElement.setAttribute('aria-label', '3D dining floor: drag to rotate, scroll to zoom, click a table to order')
    const scene = new THREE.Scene()
    scene.background = new THREE.Color(dark ? '#16181b' : '#efece7')
    const camera = new THREE.PerspectiveCamera(40, 1.6, 0.1, 200)
    camera.position.set(0.6, 17.5, 15.5)
    const controls = new OrbitControls(camera, renderer.domElement)
    controls.target.set(0.6, 0, 1.2)
    controls.maxPolarAngle = Math.PI / 2.15
    controls.minDistance = 6
    controls.maxDistance = 32
    controls.enableDamping = true
    scene.add(new THREE.HemisphereLight('#ffffff', dark ? '#222' : '#bbb', dark ? 1.4 : 1.7))
    const sun = new THREE.DirectionalLight('#fff', dark ? 1.2 : 1.6)
    sun.position.set(-6, 14, 8)
    scene.add(sun)

    // room
    const floor = new THREE.Mesh(new THREE.PlaneGeometry(ROOM.w, ROOM.d), new THREE.MeshStandardMaterial({ color: dark ? '#2a2622' : '#d9c7ad', roughness: 0.95 }))
    floor.rotation.x = -Math.PI / 2
    scene.add(floor)
    const wallMat = new THREE.MeshStandardMaterial({ color: dark ? '#3a3d42' : '#f7f4ef', roughness: 0.9 })
    const hw = ROOM.w / 2
    const hd = ROOM.d / 2
    const doorHalf = 0.9
    // the right wall has a gap for the entrance
    for (const [w, d, x, z] of [[ROOM.w, 0.2, 0, -hd], [0.2, ROOM.d, -hw, 0], [ROOM.w, 0.2, 0, hd],
      [0.2, DOOR.z - doorHalf + hd, hw, (-hd + DOOR.z - doorHalf) / 2], [0.2, hd - DOOR.z - doorHalf, hw, (hd + DOOR.z + doorHalf) / 2]]) {
      const wall = new THREE.Mesh(new THREE.BoxGeometry(w, 0.9, d), wallMat)
      wall.position.set(x, 0.45, z)
      scene.add(wall)
    }
    const door = new THREE.Mesh(new THREE.BoxGeometry(0.12, 0.05, doorHalf * 2), new THREE.MeshStandardMaterial({ color: '#7c4a2d' }))
    door.position.set(hw, 0.03, DOOR.z)
    scene.add(door)
    // fixtures from the plan: solid counters, a floor marking for the waiting area, the entrance
    const fx = state.layout?.fixtures || {}
    const fixMat = new THREE.MeshStandardMaterial({ color: dark ? '#55504a' : '#8b6f55', roughness: 0.6 })
    for (const [k, f] of Object.entries(fx)) {
      const c = toWorld(f.x + f.w / 2, f.y + f.h / 2)
      const kind = f.kind || 'solid'
      if (kind === 'zone') {
        const rug = new THREE.Mesh(new THREE.PlaneGeometry(f.w / 40, f.h / 40),
          new THREE.MeshStandardMaterial({ color: dark ? '#3b4a5c' : '#b9cde0', roughness: 1, transparent: true, opacity: 0.8 }))
        rug.rotation.x = -Math.PI / 2
        rug.position.set(c.x, 0.01, c.z)
        scene.add(rug)
      } else if (kind === 'solid') {
        const tall = k === 'bar_counter' ? 1.05 : 0.9
        const m = new THREE.Mesh(new THREE.BoxGeometry(f.w / 40, tall, f.h / 40), fixMat)
        m.position.set(c.x, tall / 2, c.z)
        scene.add(m)
      }
      const lab = labelSprite(f.label)
      lab.position.set(kind === 'door' ? c.x - 0.6 : c.x, kind === 'zone' ? 0.5 : 1.5, kind === 'zone' ? c.z + f.h / 80 - 0.25 : c.z)
      lab.scale.set(1.0, 0.38, 1)
      scene.add(lab)
    }

    const geo = {
      body: new THREE.CylinderGeometry(0.17, 0.22, 0.62, 12),
      head: new THREE.SphereGeometry(0.15, 14, 12),
      chair: new THREE.BoxGeometry(0.34, 0.45, 0.34),
      stool: new THREE.CylinderGeometry(0.16, 0.16, 0.7, 12),
    }
    const chairMat = new THREE.MeshStandardMaterial({ color: dark ? '#5b5047' : '#6b4f3a', roughness: 0.8 })
    const tableObjs = {}
    for (const t of Object.values(state.tables || {})) {
      const c = tableCenter(t)
      let mesh
      if (t.shape === 'bar') {
        mesh = new THREE.Mesh(geo.stool, new THREE.MeshStandardMaterial({ color: STATE_COLORS[t.state] || '#ccc' }))
        mesh.position.set(c.x, 0.35, c.z - 0.15)
      } else if (t.shape === 'round') {
        mesh = new THREE.Mesh(new THREE.CylinderGeometry(t.w / 80, t.w / 80, 0.08, 28), new THREE.MeshStandardMaterial({ color: STATE_COLORS[t.state] || '#ccc' }))
        mesh.position.set(c.x, 0.75, c.z)
      } else {
        mesh = new THREE.Mesh(new THREE.BoxGeometry(t.w / 40, 0.08, t.h / 40), new THREE.MeshStandardMaterial({ color: STATE_COLORS[t.state] || '#ccc' }))
        mesh.position.set(c.x, 0.75, c.z)
      }
      mesh.userData.tableId = t.id
      scene.add(mesh)
      if (t.shape !== 'bar') {
        const leg = new THREE.Mesh(new THREE.CylinderGeometry(0.06, 0.06, 0.72, 8), chairMat)
        leg.position.set(c.x, 0.36, c.z)
        scene.add(leg)
        for (const ch of chairSpots(t)) {
          const chair = new THREE.Mesh(geo.chair, chairMat)
          chair.position.set(ch.x, 0.22, ch.z)
          chair.rotation.y = ch.rot
          scene.add(chair)
        }
      }
      const lab = labelSprite(t.id)
      lab.position.set(c.x, t.shape === 'bar' ? 1.25 : 1.55, c.z)
      if (t.shape === 'bar') lab.scale.set(0.8, 0.3, 1)
      scene.add(lab)
      tableObjs[t.id] = { mesh, lab, key: t.id }
    }

    const people = {}
    const ray = new THREE.Raycaster()
    const ndc = new THREE.Vector2()
    const onClick = (ev) => {
      const r = renderer.domElement.getBoundingClientRect()
      ndc.set(((ev.clientX - r.left) / r.width) * 2 - 1, -((ev.clientY - r.top) / r.height) * 2 + 1)
      ray.setFromCamera(ndc, camera)
      const hit = ray.intersectObjects(Object.values(tableObjs).map((o) => o.mesh))[0]
      if (hit) setSel(hit.object.userData.tableId)
    }
    let downAt = null
    renderer.domElement.addEventListener('pointerdown', (e) => { downAt = [e.clientX, e.clientY] })
    renderer.domElement.addEventListener('pointerup', (e) => {
      if (downAt && Math.hypot(e.clientX - downAt[0], e.clientY - downAt[1]) < 5) onClick(e)
      downAt = null
    })

    const resize = () => {
      const w = el.clientWidth || 600
      const h = Math.round(Math.max(360, Math.min(w * 0.7, (window.innerHeight || 900) * 0.7)))
      renderer.setSize(w, h)
      camera.aspect = w / h
      camera.updateProjectionMatrix()
    }
    resize()
    const ro = new ResizeObserver(resize)
    ro.observe(el)

    let last = performance.now()
    let raf = 0
    const tick = (now) => {
      const dt = Math.min(0.1, (now - last) / 1000)
      last = now
      const snap = reduceMotion()
      for (const [id, p] of Object.entries(people)) {
        const tgt = p.leaving ? { x: DOOR.x, z: DOOR.z } : p.target
        const nxt = snap ? { x: tgt.x, z: tgt.z, arrived: true } : stepToward(p.pos, tgt, WALK_SPEED, dt)
        p.pos = { x: nxt.x, z: nxt.z, heading: nxt.heading }
        const seated = nxt.arrived && !p.leaving && p.target.pose === 'sit'
        const bob = nxt.arrived || snap ? 0 : Math.abs(Math.sin(now / 110)) * 0.06
        p.obj.position.set(nxt.x, (seated ? -0.18 : 0) + bob, nxt.z)
        p.obj.rotation.y = nxt.arrived ? (p.target.rot ?? 0) : (nxt.heading ?? 0)
        if (p.leaving && nxt.arrived) {
          scene.remove(p.obj)
          delete people[id]
        }
      }
      controls.update()
      renderer.render(scene, camera)
      raf = requestAnimationFrame(tick)
    }
    raf = requestAnimationFrame(tick)
    ctx.current = { scene, people, tableObjs, geo, partyLabels: {} }
    return () => {
      cancelAnimationFrame(raf)
      ro.disconnect()
      controls.dispose()
      renderer.dispose()
      el.removeChild(renderer.domElement)
      ctx.current = null
    }
    // the static scene is built once; live updates go through the effects below
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // table colors and labels follow table state
  useEffect(() => {
    const c = ctx.current
    if (!c) return
    const now = state.clock?.t || 0
    for (const t of Object.values(tables)) {
      const o = c.tableObjs[t.id]
      if (!o) continue
      o.mesh.material.color.set(STATE_COLORS[t.state] || '#ccc')
      const sub = t.shape === 'bar' ? '' : t.party_id ? `${t.party_size}/${t.seats} · ${Math.max(0, Math.round((now - t.since) / 60))}m` : `${t.seats} seats`
      const key = `${t.id}|${sub}`
      if (o.key !== key) {
        const pos = o.lab.position.clone()
        const sc = o.lab.scale.clone()
        c.scene.remove(o.lab)
        o.lab.material.map.dispose()
        o.lab = labelSprite(t.id, sub)
        o.lab.position.copy(pos)
        o.lab.scale.copy(sc)
        c.scene.add(o.lab)
        o.key = key
      }
    }
  }, [tables, state.clock?.t])

  // guests: arrive through the door and walk to their chair or the waiting area; leave through the door
  useEffect(() => {
    const c = ctx.current
    if (!c) return
    for (const [id, tgt] of Object.entries(targets)) {
      const p = c.people[id]
      if (p) {
        p.target = tgt
        p.leaving = false
      } else {
        const obj = makePerson(colorForParty(tgt.partyId), c.geo)
        obj.position.set(DOOR.x, 0, DOOR.z)
        c.scene.add(obj)
        c.people[id] = { obj, target: tgt, pos: { x: DOOR.x, z: DOOR.z }, leaving: false }
      }
    }
    for (const [id, p] of Object.entries(c.people)) {
      if (!targets[id]) p.leaving = true
    }
  }, [targets])

  // a name tag over each waiting party (with "checking in" while the host greets them)
  useEffect(() => {
    const c = ctx.current
    if (!c) return
    for (const [pid, sp] of Object.entries(spots)) {
      const w = sp.party
      const sub = w.checking_in ? (w.reason || 'checking in').replace('waiting for the rest of the party', 'party not all here') : `${w.size} · ${w.waited_min ?? 0}m${w.quoted_min != null ? ` / ${w.quoted_min}m` : ''}`
      const key = `${w.name}|${sub}`
      const old = c.partyLabels[pid]
      const pos = { x: sp.x - ((sp.n - 1) * 0.55) / 2, z: sp.z }
      if (old && old.key === key) { old.lab.position.set(pos.x, 1.45, pos.z); continue }
      if (old) { c.scene.remove(old.lab); old.lab.material.map.dispose() }
      const lab = labelSprite(String(w.name || pid).slice(0, 14), sub)
      lab.scale.set(1.1, 0.41, 1)
      lab.position.set(pos.x, 1.45, pos.z)
      c.scene.add(lab)
      c.partyLabels[pid] = { lab, key }
    }
    for (const [pid, o] of Object.entries(c.partyLabels)) {
      if (!spots[pid]) { c.scene.remove(o.lab); o.lab.material.map.dispose(); delete c.partyLabels[pid] }
    }
  }, [spots])

  const seatedGuests = Object.values(targets).filter((t) => t.pose === 'sit').length
  const waiting = Object.values(targets).filter((t) => t.pose === 'stand').length
  const selTable = sel ? tables[sel] : null
  return (
    <div className="floor floor3d">
      {err ? <p className="err" role="alert">{err}</p> : <div ref={mount} className="three-mount" />}
      <div className="legend">
        {Object.entries(STATE_COLORS).map(([s, col]) => (
          <span key={s} className="lg"><i style={{ background: col }} />{STATE_LABEL[s]}</span>
        ))}
        <span className="lg-occ">{seatedGuests} seated · {waiting} waiting in {waitlist.length} parties · drag to rotate, scroll to zoom, click a table</span>
      </div>
      {selTable && <OrderDialog state={state} table={selTable} onClose={() => setSel(null)} />}
    </div>
  )
}
