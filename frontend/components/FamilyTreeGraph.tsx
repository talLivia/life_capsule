'use client'

import { useMemo } from 'react'
import type { FamilyTree, TreeEdge, TreePerson } from '@/lib/types'

/**
 * The family tree as a node graph with drawn connections.
 *
 * Hand-built SVG rather than a layout library, deliberately. A family tree is
 * not a tree — two parents point at one child, so it is a DAG, and the usual
 * packages (d3-hierarchy, react-d3-tree) assume exactly one parent per node.
 * The genuinely hard part, assigning generations, already happened server-side
 * in family_tree.py; what is left here is arithmetic on rows and paths.
 *
 * ## Only recorded relations are drawn
 *
 * Siblings connect to the PRODUCER, not up to the parents, because that is
 * what the archive actually says: "ניר is my brother" and "צבי is my father"
 * are two separate facts, and nothing has ever stated that ניר is צבי's child.
 * A conventional genealogy chart would run every sibling up to the same
 * parents — inventing edges nobody recorded. This looks slightly less tidy and
 * is the honest picture; see "the tree never guesses" in family_tree.py.
 *
 * Positions are computed here and nowhere else. The server owns WHO is in
 * which generation; this owns where they sit on screen.
 */

const NODE_W = 148
const NODE_H = 58
const COL_GAP = 26
const ROW_GAP = 104
const PAD = 20

interface Placed {
  person: TreePerson
  x: number // left edge
  y: number // top edge
  row: number
  col: number
}

/** Up to two initials. Works the same for Hebrew and Latin names. */
function initials(name: string): string {
  return name
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((w) => Array.from(w)[0])
    .join('')
}

function truncate(name: string, max = 16): string {
  const chars = Array.from(name)
  return chars.length > max ? `${chars.slice(0, max - 1).join('')}…` : name
}

/**
 * Order each row to reduce crossings: a node sits near the average position of
 * the neighbours it is already connected to in the row above.
 *
 * One pass, top-down. Enough for family-sized graphs, and predictable — an
 * iterative solver would reshuffle the whole chart when a single relation is
 * added, which is worse than a stray crossing on a page people revisit.
 */
function orderRows(
  generations: FamilyTree['generations'],
  edges: TreeEdge[]
): TreePerson[][] {
  const neighbours = new Map<string, Set<string>>()
  for (const e of edges) {
    if (!neighbours.has(e.from_id)) neighbours.set(e.from_id, new Set())
    if (!neighbours.has(e.to_id)) neighbours.set(e.to_id, new Set())
    neighbours.get(e.from_id)!.add(e.to_id)
    neighbours.get(e.to_id)!.add(e.from_id)
  }

  const rows: TreePerson[][] = []
  let previousIndex = new Map<string, number>()

  for (const row of generations) {
    const ordered = [...row.people].sort((a, b) => {
      const key = (p: TreePerson) => {
        const linked = Array.from(neighbours.get(p.id) ?? [])
          .map((id) => previousIndex.get(id))
          .filter((i): i is number => i !== undefined)
        // No placed neighbour above: keep it stable at the end rather than
        // letting it jump around between renders.
        return linked.length
          ? linked.reduce((s, i) => s + i, 0) / linked.length
          : Number.MAX_SAFE_INTEGER
      }
      const diff = key(a) - key(b)
      return diff !== 0 ? diff : a.name.localeCompare(b.name)
    })
    rows.push(ordered)
    previousIndex = new Map(ordered.map((p, i) => [p.id, i]))
  }
  return rows
}

export function FamilyTreeGraph({
  tree,
  selectedId,
  onSelect,
}: {
  tree: FamilyTree
  selectedId: string | null
  onSelect: (person: TreePerson) => void
}) {
  const { placed, width, height, dipFor } = useMemo(() => {
    const rows = orderRows(tree.generations, tree.edges)
    const rowWidths = rows.map((r) => r.length * NODE_W + Math.max(0, r.length - 1) * COL_GAP)
    const widest = Math.max(0, ...rowWidths)

    const out = new Map<string, Placed>()
    rows.forEach((row, rowIndex) => {
      // Centre every row against the widest, so the chart reads as one shape
      // rather than a left-aligned stack.
      const startX = PAD + (widest - rowWidths[rowIndex]) / 2
      row.forEach((person, i) => {
        out.set(person.id, {
          person,
          x: startX + i * (NODE_W + COL_GAP),
          y: PAD + rowIndex * (NODE_H + ROW_GAP),
          row: rowIndex,
          col: i,
        })
      })
    })

    /**
     * Same-row connectors that skip over a column have to detour BELOW the row.
     *
     * This is the never-guesses rule at the rendering layer. The producer has
     * four siblings, all recorded as siblings OF THE PRODUCER. Drawn as
     * straight horizontal lines those pass behind the nodes in between, and
     * what a reader sees is a chain — חן–ניר–עדי–רז linked to each other —
     * which is a set of relations nobody ever recorded. Occlusion is not
     * neutral: a hidden line segment still reads as a connection between the
     * two things it visibly touches.
     *
     * Longer spans go deeper, so the arcs nest instead of overlapping. Depth
     * stays inside the row gap, above where descent lines run across.
     */
    const dip = new Map<number, number>()
    const span = (edgeIndex: number) => {
      const a = out.get(tree.edges[edgeIndex].from_id)!
      const b = out.get(tree.edges[edgeIndex].to_id)!
      return Math.abs(a.col - b.col)
    }
    const byRow = new Map<number, number[]>()
    tree.edges.forEach((edge, i) => {
      const a = out.get(edge.from_id)
      const b = out.get(edge.to_id)
      if (!a || !b || a.row !== b.row) return
      if (Math.abs(a.col - b.col) <= 1) return // adjacent: nothing in between
      byRow.set(a.row, [...(byRow.get(a.row) ?? []), i])
    })

    let deepestOnLastRow = 0
    byRow.forEach((indices, rowIndex) => {
      indices
        .sort((x, y) => span(x) - span(y))
        .forEach((edgeIndex, k) => {
          const depth = Math.min(14 + k * 9, ROW_GAP / 2 - 10)
          dip.set(edgeIndex, depth)
          if (rowIndex === rows.length - 1) {
            deepestOnLastRow = Math.max(deepestOnLastRow, depth)
          }
        })
    })

    return {
      placed: out,
      dipFor: dip,
      width: widest + PAD * 2,
      // The bottom row's detours have no row gap beneath them to sit in, so
      // the canvas grows to hold them rather than clipping.
      height:
        rows.length * NODE_H +
        Math.max(0, rows.length - 1) * ROW_GAP +
        PAD * 2 +
        deepestOnLastRow,
    }
  }, [tree])

  if (placed.size === 0) return null

  const centre = (p: Placed) => ({ cx: p.x + NODE_W / 2, cy: p.y + NODE_H / 2 })

  return (
    <div className="overflow-x-auto">
      <svg
        viewBox={`0 0 ${width} ${height}`}
        width={width}
        height={height}
        role="img"
        aria-label="Family tree"
        className="max-w-full h-auto"
      >
        {/* Edges first so nodes paint over their endpoints. */}
        <g>
          {tree.edges.map((edge, i) => {
            const a = placed.get(edge.from_id)
            const b = placed.get(edge.to_id)
            if (!a || !b) return null // an unplaced endpoint has no position

            const A = centre(a)
            const B = centre(b)
            const sameRow = a.y === b.y

            if (sameRow) {
              // Sibling or spouse — dashed, so it reads differently from a
              // parent-child descent.
              const left = A.cx < B.cx ? a : b
              const right = A.cx < B.cx ? b : a
              const depth = dipFor.get(i)
              const key = `${edge.from_id}-${edge.to_id}-${edge.relation_type}-${i}`
              const stroke = 'rgb(148 163 184 / 0.35)'

              if (depth === undefined) {
                // Adjacent columns: nothing to pass behind.
                return (
                  <line
                    key={key}
                    x1={left.x + NODE_W}
                    y1={left.y + NODE_H / 2}
                    x2={right.x}
                    y2={right.y + NODE_H / 2}
                    stroke={stroke}
                    strokeWidth={1.5}
                    strokeDasharray="4 4"
                  />
                )
              }

              const base = left.y + NODE_H
              return (
                <path
                  key={key}
                  d={`M ${left.x + NODE_W / 2} ${base} V ${base + depth} H ${
                    right.x + NODE_W / 2
                  } V ${base}`}
                  fill="none"
                  stroke={stroke}
                  strokeWidth={1.5}
                  strokeDasharray="4 4"
                />
              )
            }

            // Different generations — an orthogonal descent, the shape a
            // genealogy chart uses: down, across, down.
            const upper = a.y < b.y ? a : b
            const lower = a.y < b.y ? b : a
            const midY = upper.y + NODE_H + (lower.y - (upper.y + NODE_H)) / 2
            const ux = upper.x + NODE_W / 2
            const lx = lower.x + NODE_W / 2
            return (
              <path
                key={`${edge.from_id}-${edge.to_id}-${edge.relation_type}-${i}`}
                d={`M ${ux} ${upper.y + NODE_H} V ${midY} H ${lx} V ${lower.y}`}
                fill="none"
                stroke="rgb(148 163 184 / 0.45)"
                strokeWidth={1.5}
              />
            )
          })}
        </g>

        {/* Nodes */}
        <g>
          {Array.from(placed.values()).map(({ person, x, y }) => {
            const isSelected = person.id === selectedId
            const years =
              person.year_start || person.year_end
                ? `${person.year_start ?? '?'}–${person.year_end ?? ''}`
                : null

            return (
              <g
                key={person.id}
                transform={`translate(${x}, ${y})`}
                onClick={() => onSelect(person)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' || e.key === ' ') {
                    e.preventDefault()
                    onSelect(person)
                  }
                }}
                tabIndex={0}
                role="button"
                aria-label={`${person.name}${person.is_self ? ' (you)' : ''}`}
                className="cursor-pointer focus:outline-none"
              >
                <rect
                  width={NODE_W}
                  height={NODE_H}
                  rx={12}
                  className={
                    isSelected
                      ? 'fill-primary-500/20 stroke-primary-400'
                      : person.is_self
                        ? 'fill-surface-800 stroke-primary-500/50'
                        : 'fill-surface-800/70 stroke-white/10 hover:stroke-white/30'
                  }
                  strokeWidth={1.5}
                />

                {/* Initials stand in for a photo — none are stored. */}
                <circle
                  cx={NODE_H / 2}
                  cy={NODE_H / 2}
                  r={16}
                  className={person.is_self ? 'fill-primary-500/30' : 'fill-white/8'}
                />
                <text
                  x={NODE_H / 2}
                  y={NODE_H / 2}
                  textAnchor="middle"
                  dominantBaseline="central"
                  className="fill-white text-[12px] font-semibold"
                >
                  {initials(person.name)}
                </text>

                <text
                  x={NODE_H - 4}
                  y={years ? NODE_H / 2 - 6 : NODE_H / 2}
                  dominantBaseline="central"
                  className="fill-white text-[12px] font-medium"
                >
                  {truncate(person.name)}
                </text>
                {years && (
                  <text
                    x={NODE_H - 4}
                    y={NODE_H / 2 + 10}
                    dominantBaseline="central"
                    className="fill-gray-500 text-[10px]"
                  >
                    {years}
                  </text>
                )}
                {person.is_self && !years && (
                  <text
                    x={NODE_H - 4}
                    y={NODE_H / 2 + 10}
                    dominantBaseline="central"
                    className="fill-primary-400/80 text-[10px]"
                  >
                    You
                  </text>
                )}
              </g>
            )
          })}
        </g>
      </svg>
    </div>
  )
}
