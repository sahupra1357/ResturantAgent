// Pure helpers for the Setup screen (unit-tested).

// "beef_patty:0.2; bun:1" <-> [{ingredient_id, qty_per_portion}]
export function parseRecipe(text) {
  return String(text || '').split(/[;\n]/).map((s) => s.trim()).filter(Boolean).map((part) => {
    const [id, qty] = part.split(':').map((x) => x.trim())
    return { ingredient_id: id, qty_per_portion: Number(qty) }
  })
}
export const formatRecipe = (lines) => (lines || []).map((l) => `${l.ingredient_id}:${l.qty_per_portion}`).join('; ')

// Recipe food cost and margin from the ingredient reference costs.
export function dishCost(recipe, ingredients) {
  const cost = Object.fromEntries((ingredients || []).map((i) => [i.id, i.ref_cost]))
  let total = 0
  const missing = []
  for (const l of recipe || []) {
    if (cost[l.ingredient_id] == null) missing.push(l.ingredient_id)
    else total += cost[l.ingredient_id] * Number(l.qty_per_portion || 0)
  }
  return { cost: Math.round(total * 100) / 100, missing }
}

// Coerce form values to the column types before saving.
export function coerce(row, cols) {
  const out = { ...row }
  for (const c of cols) {
    if (!(c.key in out)) continue
    if (c.type === 'number') out[c.key] = out[c.key] === '' || out[c.key] == null ? null : Number(out[c.key])
    if (c.type === 'bool') out[c.key] = out[c.key] ? 1 : 0
  }
  return out
}

// Simple CSV preview: header + first rows, for the importer.
export function csvPreview(text, n = 5) {
  const lines = String(text || '').split(/\r?\n/).filter((l) => l.trim())
  if (!lines.length) return { header: [], rows: [], total: 0 }
  const split = (l) => l.split(',').map((x) => x.trim())
  return { header: split(lines[0]), rows: lines.slice(1, n + 1).map(split), total: lines.length - 1 }
}

export const WEEKDAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
