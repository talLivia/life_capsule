/**
 * Print the family-tree layout for a tree JSON — every node position and
 * every connector segment, labelled.
 *
 * The point: when somebody reports "there is an extra line between X and Y",
 * this shows what the layout actually emits there, so the cause is read off
 * a list instead of guessed from a screenshot. The JSON is the exact
 * /api/v1/entities/tree response (dump it with any authenticated GET, or via
 * family_tree.build_tree from a backend shell).
 *
 * Usage:
 *   npx tsx scripts/tree_layout_report.mts <tree.json> [nameA nameB]
 *
 * With two names, additionally lists every segment that passes through the
 * horizontal strip between those two nodes' columns in their shared row.
 */
import { readFileSync } from 'node:fs'

import { NODE_H, NODE_W, computeTreeLayout } from '../lib/treeLayout'
import type { FamilyTree } from '../lib/types'

const [, , file, nameA, nameB] = process.argv
if (!file) {
  console.error('usage: npx tsx scripts/tree_layout_report.mts <tree.json> [nameA nameB]')
  process.exit(2)
}

const tree = JSON.parse(readFileSync(file, 'utf-8')) as FamilyTree
const layout = computeTreeLayout(tree)

const nameOf = new Map<string, string>()
for (const row of tree.generations) for (const p of row.people) nameOf.set(p.id, p.name)
const label = (id: string) => nameOf.get(id) ?? id.slice(0, 8)

console.log(`canvas ${layout.width} x ${layout.height}\n`)

console.log('NODES (x is the left edge; rows top-down):')
const byY = new Map<number, { name: string; x: number }[]>()
layout.placed.forEach(({ person, x, y }) => {
  byY.set(y, [...(byY.get(y) ?? []), { name: person.name, x }])
})
Array.from(byY.keys()).sort((a, b) => a - b).forEach((y) => {
  const row = byY.get(y)!.sort((a, b) => a.x - b.x)
  console.log(`  y=${y}: ${row.map((n) => `${n.name}@${n.x}`).join('  ')}`)
})

interface Seg {
  kind: string
  who: string
  x1: number
  y1: number
  x2: number
  y2: number
}
const segments: Seg[] = []
for (const d of layout.descents) {
  const cx = (p: { x: number }) => p.x + NODE_W / 2
  const parentXs = d.parents.map(cx)
  const childXs = d.children.map(cx)
  const busLeft = Math.min(d.junctionX, ...childXs)
  const busRight = Math.max(d.junctionX, ...childXs)
  for (const p of d.parents) {
    segments.push({ kind: 'stem', who: label(p.person.id), x1: cx(p), y1: p.y + NODE_H, x2: cx(p), y2: d.stemY })
  }
  if (d.parents.length > 1) {
    segments.push({ kind: 'bar', who: d.parents.map((p) => label(p.person.id)).join('+'), x1: Math.min(...parentXs), y1: d.stemY, x2: Math.max(...parentXs), y2: d.stemY })
  }
  segments.push({ kind: 'trunk', who: d.parents.map((p) => label(p.person.id)).join('+'), x1: d.junctionX, y1: d.stemY, x2: d.junctionX, y2: d.busY })
  if (busRight - busLeft > 0.5) {
    segments.push({ kind: 'bus', who: d.parents.map((p) => label(p.person.id)).join('+'), x1: busLeft, y1: d.busY, x2: busRight, y2: d.busY })
  }
  for (const c of d.children) {
    segments.push({ kind: 'drop', who: label(c.person.id), x1: cx(c), y1: d.busY, x2: cx(c), y2: c.y })
  }
}
for (const l of layout.siblingLinks) {
  segments.push({ kind: 'sib-dash', who: `${label(l.a)}~${label(l.b)}`, x1: l.x1, y1: l.y, x2: l.x2, y2: l.y })
}
for (const l of layout.spouseLinks) {
  segments.push({ kind: 'marriage', who: `${label(l.a)}=${label(l.b)}`, x1: l.x1, y1: l.y, x2: l.x2, y2: l.y })
}
for (const a of layout.siblingArcs) {
  segments.push({ kind: 'arc', who: `${label(a.a)}~${label(a.b)}`, x1: 0, y1: 0, x2: 0, y2: 0 })
}

console.log('\nSEGMENTS:')
for (const s of segments) {
  console.log(
    `  ${s.kind.padEnd(9)} ${s.who.padEnd(28)} (${s.x1},${s.y1}) -> (${s.x2},${s.y2})`
  )
}

if (nameA && nameB) {
  const find = (name: string) => {
    for (const [, p] of layout.placed) if (p.person.name === name) return p
    return null
  }
  const a = find(nameA)
  const b = find(nameB)
  if (!a || !b) {
    console.error(`could not find ${!a ? nameA : nameB} among placed nodes`)
    process.exit(2)
  }
  const left = a.x < b.x ? a : b
  const right = a.x < b.x ? b : a
  const stripX1 = left.x + NODE_W
  const stripX2 = right.x
  const stripY1 = Math.min(a.y, b.y)
  const stripY2 = Math.max(a.y, b.y) + NODE_H
  console.log(
    `\nSTRIP between ${nameA} and ${nameB}: x ${stripX1}..${stripX2}, y ${stripY1}..${stripY2}`
  )
  for (const s of segments) {
    if (s.kind === 'arc') continue
    const sx1 = Math.min(s.x1, s.x2)
    const sx2 = Math.max(s.x1, s.x2)
    const sy1 = Math.min(s.y1, s.y2)
    const sy2 = Math.max(s.y1, s.y2)
    if (sx2 < stripX1 || sx1 > stripX2 || sy2 < stripY1 || sy1 > stripY2) continue
    console.log(
      `  CROSSES: ${s.kind} ${s.who} (${s.x1},${s.y1}) -> (${s.x2},${s.y2})`
    )
  }
}
