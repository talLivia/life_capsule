'use client'

import { useCallback, useEffect, useMemo, useRef } from 'react'
import { Maximize2, Minus, Plus } from 'lucide-react'
import {
  TransformComponent,
  TransformWrapper,
  type ReactZoomPanPinchRef,
} from 'react-zoom-pan-pinch'
import type { FamilyTree, TreePerson } from '@/lib/types'

/**
 * The family tree as a node graph with drawn connections.
 *
 * Node and edge rendering is hand-built SVG, deliberately. A family tree is
 * not a tree — two parents point at one child, so it is a DAG, and the usual
 * layout packages (d3-hierarchy, react-d3-tree) assume exactly one parent per
 * node. The genuinely hard part, assigning generations, already happened
 * server-side in family_tree.py; what is left here is arithmetic on rows.
 *
 * Only the VIEWPORT is a library (react-zoom-pan-pinch). Pointer-anchored
 * wheel zoom, trackpad and touch pinch, and telling a drag apart from a click
 * are fiddly in ways that feel broken when slightly wrong, and none of them
 * are about family structure. The split is deliberate: the library never sees
 * a node or an edge.
 *
 * ## Only parent-child relations are drawn
 *
 * Same-generation relations — siblings, partners — are shown by SHARING A ROW,
 * not by a line. The row already carries that information, and the connectors
 * were pure noise: with four siblings all recorded as siblings of the
 * producer, the lines fanned out from one node and had to be routed around
 * the nodes in between to avoid reading as a chain that nobody recorded.
 * Deleting them deleted that whole problem.
 *
 * Note this means a recorded MARRIAGE between two people in the same row is
 * not visible as such — they are simply both in the row. Called out in the
 * caption under the chart rather than left for someone to notice.
 *
 * Positions are computed here and nowhere else. The server owns WHO is in
 * which generation; this owns where they sit on screen.
 */

const NODE_W = 148
const NODE_H = 58
const COL_GAP = 26
const ROW_GAP = 104
const PAD = 20
/** Left gutter for the row labels, which live inside the SVG so that they pan
 *  and zoom with the rows they name — as separate DOM they would drift. */
const LABEL_W = 150

const MIN_SCALE = 0.15
const MAX_SCALE = 2.5

interface Placed {
  person: TreePerson
  x: number // left edge
  y: number // top edge
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

const GENERATION_LABELS: Record<number, string> = {
  [-2]: 'Grandparents',
  [-1]: 'Parents',
  0: 'You and your generation',
  1: 'Children',
  2: 'Grandchildren',
}

function generationLabel(generation: number): string {
  if (GENERATION_LABELS[generation]) return GENERATION_LABELS[generation]
  // Beyond the named rows, say the distance rather than inventing a word for
  // it — "3 generations up" is honest where "great-grandparents" might not be.
  const n = Math.abs(generation)
  return generation < 0 ? `${n} generations up` : `${n} generations down`
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
  edges: FamilyTree['edges']
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

/** How far a pointer may travel between down and up and still count as a
 *  click. Without this, nudging the canvas while pressing a node opens
 *  somebody's moments on what the hand meant as a pan. */
const CLICK_SLOP_PX = 5

export function FamilyTreeGraph({
  tree,
  selectedId,
  onSelect,
}: {
  tree: FamilyTree
  selectedId: string | null
  onSelect: (person: TreePerson) => void
}) {
  const containerRef = useRef<HTMLDivElement>(null)
  const zoomRef = useRef<ReactZoomPanPinchRef>(null)
  const pressRef = useRef<{ x: number; y: number } | null>(null)

  const { placed, rowLabels, width, height } = useMemo(() => {
    const rows = orderRows(tree.generations, tree.edges)
    const rowWidths = rows.map((r) => r.length * NODE_W + Math.max(0, r.length - 1) * COL_GAP)
    const widest = Math.max(0, ...rowWidths)

    const out = new Map<string, Placed>()
    const labels: { text: string; y: number }[] = []
    rows.forEach((row, rowIndex) => {
      // Centre every row against the widest, so the chart reads as one shape
      // rather than a left-aligned stack.
      const startX = PAD + LABEL_W + (widest - rowWidths[rowIndex]) / 2
      const y = PAD + rowIndex * (NODE_H + ROW_GAP)
      labels.push({ text: generationLabel(tree.generations[rowIndex].generation), y })
      row.forEach((person, i) => {
        out.set(person.id, { person, x: startX + i * (NODE_W + COL_GAP), y })
      })
    })

    return {
      placed: out,
      rowLabels: labels,
      width: LABEL_W + widest + PAD * 2,
      height: rows.length * NODE_H + Math.max(0, rows.length - 1) * ROW_GAP + PAD * 2,
    }
  }, [tree])

  /** Scale at which the whole chart fits the container. Never above 1 — a
   *  small family should not be blown up to fill the screen. */
  const fitScale = useCallback(() => {
    const el = containerRef.current
    if (!el) return 1
    return Math.max(
      MIN_SCALE,
      Math.min(1, (el.clientWidth - 32) / width, (el.clientHeight - 32) / height)
    )
  }, [width, height])

  // A tree wider than the viewport opens fitted rather than scrolled off the
  // right edge, where the producer would have to discover panning to find out
  // anything is missing.
  useEffect(() => {
    const id = window.requestAnimationFrame(() => {
      zoomRef.current?.centerView(fitScale(), 0)
    })
    return () => window.cancelAnimationFrame(id)
  }, [fitScale])

  if (placed.size === 0) return null

  const control =
    'p-2 rounded-lg bg-surface-800/80 border border-white/10 text-gray-300 ' +
    'hover:text-white hover:border-white/30 transition-colors'

  return (
    <div
      ref={containerRef}
      className="relative w-full h-full overflow-hidden rounded-2xl bg-surface-900/40 border border-white/6"
    >
      <TransformWrapper
        ref={zoomRef}
        minScale={MIN_SCALE}
        maxScale={MAX_SCALE}
        limitToBounds={false}
        centerOnInit
        doubleClick={{ disabled: true }}
        wheel={{ step: 0.08 }}
        panning={{ velocityDisabled: true }}
      >
        <>
          <div className="absolute top-3 right-3 z-10 flex flex-col gap-1.5">
            <button
              type="button"
              onClick={() => zoomRef.current?.zoomIn()}
              className={control}
              aria-label="Zoom in"
            >
              <Plus size={15} />
            </button>
            <button
              type="button"
              onClick={() => zoomRef.current?.zoomOut()}
              className={control}
              aria-label="Zoom out"
            >
              <Minus size={15} />
            </button>
            <button
              type="button"
              onClick={() => zoomRef.current?.centerView(fitScale(), 200)}
              className={control}
              aria-label="Fit tree to screen"
            >
              <Maximize2 size={15} />
            </button>
          </div>

          <TransformComponent
            wrapperStyle={{ width: '100%', height: '100%' }}
            contentStyle={{ width, height }}
          >
            <svg
              viewBox={`0 0 ${width} ${height}`}
              width={width}
              height={height}
              role="img"
              aria-label="Family tree"
            >
              {/* Row labels, inside the transform so they travel with the rows. */}
              <g>
                {rowLabels.map((label) => (
                  <text
                    key={label.y}
                    x={PAD}
                    y={label.y + NODE_H / 2}
                    dominantBaseline="central"
                    className="fill-gray-500 text-[11px] uppercase tracking-wide"
                  >
                    {label.text}
                  </text>
                ))}
              </g>

              {/* Edges first so nodes paint over their endpoints. */}
              <g>
                {tree.edges.map((edge, i) => {
                  const a = placed.get(edge.from_id)
                  const b = placed.get(edge.to_id)
                  if (!a || !b) return null // an unplaced endpoint has no position
                  // Same generation: siblings and partners are shown by the
                  // shared row, never by a line. See the header.
                  if (a.y === b.y) return null

                  // An orthogonal descent, the shape a genealogy chart uses:
                  // down, across, down.
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
                      onPointerDown={(e) => {
                        pressRef.current = { x: e.clientX, y: e.clientY }
                      }}
                      onPointerUp={(e) => {
                        const down = pressRef.current
                        pressRef.current = null
                        if (!down) return
                        const moved =
                          Math.abs(e.clientX - down.x) + Math.abs(e.clientY - down.y)
                        if (moved <= CLICK_SLOP_PX) onSelect(person)
                      }}
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
          </TransformComponent>
        </>
      </TransformWrapper>
    </div>
  )
}
