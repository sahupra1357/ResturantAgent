import { useState } from 'react'

export function Panel({ title, right, children, className = '' }) {
  const id = `p-${title.replace(/[^a-z0-9]+/gi, '-').toLowerCase()}`
  return (
    <section className={`panel ${className}`} aria-labelledby={id}>
      <header><h3 id={id}>{title}</h3>{right}</header>
      <div className="panel-body">{children}</div>
    </section>
  )
}

// Run an API call, keep its error / success message for the panel.
export function useAct() {
  const [err, setErr] = useState('')
  const [msg, setMsg] = useState('')
  const act = async (fn, ok = '') => {
    setErr('')
    setMsg('')
    try {
      const r = await fn()
      if (ok) setMsg(ok)
      return r
    } catch (e) {
      setErr(e.message)
      return null
    }
  }
  return { act, err, msg }
}
