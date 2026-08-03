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
 * ## Descents are one trunk per parent group, not one line per child
 *
 * Children sharing the same set of parents hang off a single bus: a stem down
 * from each parent, a joining bar if there are two, one trunk down to the bus,
 * and a drop to each child.
 *
 * This does NOT reintroduce the occlusion bug that the sibling connectors had.
 * That bug existed because a same-row line runs at the row's VERTICAL CENTRE —
 * the exact band the node boxes occupy — so anyone between the endpoints sat
 * behind it. Every segment here lives in the ROW GAP, which contains no nodes
 * by construction. Different band, so the failure cannot happen.
 *
 * The same *class* of error does have a new form, and it is handled: several
 * parent groups descending into one gap put their buses at the same height,
 * and two buses that overlap horizontally would merge into what looks like one
 * family. Groups whose spans overlap are therefore given different bus depths.
 *
 * A joining bar between two parents says "both are parents of these children",
 * which is recorded. It is not a marriage line and does not claim one.
 *
 * KNOWN LIMIT: an edge spanning more than one generation (grandparent with no
 * intervening parent recorded) drops straight through the row in between. No
 * such relation exists in the archive today, and check_layout.py reports any
 * line that crosses a node rather than letting it pass unnoticed.
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

/**
 * Only these draw a descent. An `aunt_uncle` edge places somebody in the
 * parents' row — correctly, they are that generation — but drawing a trunk
 * down from them to the producer says they are a PARENT, and with four boxes
 * up there and four identical lines nothing distinguishes the two who are.
 * Placement and parenthood are different claims and only one of them is a line.
 */
const DESCENT_TYPES = new Set(['parent', 'child'])

/** Same-row connector, drawn ONLY for a sibling of somebody in an ancestor
 *  row. Sibling connectors between the producer's OWN siblings were removed
 *  as noise — the row says it. One generation up the row does not: it holds
 *  parents and their siblings side by side, and the line is what tells an
 *  uncle apart from a father. */
const SIBLING_RELATION = 'sibling'

const MIN_SCALE = 0.15
const MAX_SCALE = 2.5
/** Never open smaller than this, even if the whole tree would not fit. A
 *  fitted view of a wide tree is unreadable, and a readable view you have to
 *  pan is better than a complete one you cannot read. */
const READABLE_SCALE = 0.6
/**
 * Wheel and button zoom are both LINEAR steps, via `smooth={false}`.
 *
 * With the library's default `smooth`, the wheel step is multiplied by
 * `Math.abs(event.deltaY)` — and a Windows mouse wheel click reports
 * deltaY = 100, so any sane-looking step lands on maxScale in one click.
 * Constant steps are the only way to get intermediate levels from a wheel.
 */
const WHEEL_STEP = 0.1
const BUTTON_STEP = 0.15

/** Descent geometry, all of it inside the row gap. */
const STEM_DROP = 24 // parent bottom -> joining bar
const BUS_LIFT = 34 // child top -> bus
const BUS_LEVEL_GAP = 13 // separation between overlapping groups' buses

interface Placed {
  person: TreePerson
  x: number // left edge
  y: number // top edge
}

/** One parent-set and every child hanging off it. */
interface Descent {
  key: string
  parents: Placed[]
  children: Placed[]
  stemY: number
  busY: number
  junctionX: number
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

  const { placed, rowLabels, descents, siblingLinks, width, height } = useMemo(() => {
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

    // ── descents: group children by the exact set of parents they hang from ──
    const parentsOf = new Map<string, Placed[]>()
    for (const edge of tree.edges) {
      const a = out.get(edge.from_id)
      const b = out.get(edge.to_id)
      if (!a || !b || a.y === b.y) continue // same generation: no descent
      if (!DESCENT_TYPES.has(edge.relation_type)) continue
      const upper = a.y < b.y ? a : b
      const lower = a.y < b.y ? b : a
      const list = parentsOf.get(lower.person.id) ?? []
      if (!list.some((p) => p.person.id === upper.person.id)) list.push(upper)
      parentsOf.set(lower.person.id, list)
    }

    // Keyed by child row as well as parent set: the same parents having both a
    // child and a grandchild recorded are two different descents, not one.
    const grouped = new Map<string, { parents: Placed[]; children: Placed[] }>()
    parentsOf.forEach((parents, childId) => {
      const child = out.get(childId)!
      const key = `${child.y}::${parents.map((p) => p.person.id).sort().join('|')}`
      const group = grouped.get(key) ?? { parents, children: [] }
      group.children.push(child)
      grouped.set(key, group)
    })

    const centreOf = (p: Placed) => p.x + NODE_W / 2

    // Bus depth per gap. Two groups whose buses overlap horizontally at the
    // same height would read as one family; give them different depths.
    const byGap = new Map<number, { key: string; g: { parents: Placed[]; children: Placed[] } }[]>()
    grouped.forEach((g, key) => {
      const childY = g.children[0].y
      byGap.set(childY, [...(byGap.get(childY) ?? []), { key, g }])
    })

    const level = new Map<string, number>()
    byGap.forEach((groups) => {
      const span = (g: { parents: Placed[]; children: Placed[] }) => {
        const xs = [...g.children.map(centreOf), ...g.parents.map(centreOf)]
        return [Math.min(...xs), Math.max(...xs)] as const
      }
      const rightEdgeAt: number[] = []
      groups
        .sort((a, b) => span(a.g)[0] - span(b.g)[0])
        .forEach(({ key, g }) => {
          const [left, right] = span(g)
          let lvl = rightEdgeAt.findIndex((edge) => edge < left - COL_GAP)
          if (lvl === -1) {
            lvl = rightEdgeAt.length
            rightEdgeAt.push(right)
          } else {
            rightEdgeAt[lvl] = right
          }
          // The gap is finite; past this the bus would land on the row below.
          level.set(key, Math.min(lvl, Math.floor((ROW_GAP - BUS_LIFT - STEM_DROP - 8) / BUS_LEVEL_GAP)))
        })
    })

    const descentList: Descent[] = []
    grouped.forEach((g, key) => {
      const stemY = Math.max(...g.parents.map((p) => p.y)) + NODE_H + STEM_DROP
      const childY = g.children[0].y
      const busY = Math.max(
        stemY + 8,
        childY - BUS_LIFT - (level.get(key) ?? 0) * BUS_LEVEL_GAP
      )
      const parentXs = g.parents.map(centreOf)
      descentList.push({
        key,
        parents: g.parents,
        children: g.children,
        stemY,
        busY,
        // Between the parents, so one trunk descends from the couple rather
        // than from whichever of them happens to be first.
        junctionX: (Math.min(...parentXs) + Math.max(...parentXs)) / 2,
      })
    })

    /**
     * Sibling links inside an ancestor row — an aunt or uncle beside the
     * parent they are the sibling of.
     *
     * The one same-row connector that survives, and the reason it must:
     * row -1 holds the producer's parents AND their siblings, and without a
     * line there is nothing at all to say which two of four boxes are the
     * parents. That was never true of the producer's own row, where the row
     * label says it.
     *
     * Only drawn between ADJACENT columns. A longer one would pass behind the
     * nodes in between and read as a chain — the occlusion problem from 4a,
     * which is only safe to reintroduce under that restriction.
     */
    const columnOf = new Map<string, number>()
    rows.forEach((row) => row.forEach((p, i) => columnOf.set(p.id, i)))
    const rootRow = out.get(tree.root_id ?? '')?.y
    const siblingLinks = tree.edges
      .filter((edge) => {
        const a = out.get(edge.from_id)
        const b = out.get(edge.to_id)
        if (!a || !b || edge.relation_type !== SIBLING_RELATION) return false
        if (a.y !== b.y) return false
        if (rootRow === undefined || a.y >= rootRow) return false // ancestors only
        return Math.abs((columnOf.get(edge.from_id) ?? 0) - (columnOf.get(edge.to_id) ?? 0)) === 1
      })
      .map((edge) => {
        const a = out.get(edge.from_id)!
        const b = out.get(edge.to_id)!
        const left = a.x < b.x ? a : b
        const right = a.x < b.x ? b : a
        return {
          key: `${edge.from_id}-${edge.to_id}`,
          x1: left.x + NODE_W,
          x2: right.x,
          y: left.y + NODE_H / 2,
        }
      })

    return {
      placed: out,
      rowLabels: labels,
      siblingLinks,
      descents: descentList,
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

  // Opening view: fitted, but never below what can be read. A small family
  // opens whole; a large one opens legible and centred, and panning finds the
  // rest — the fit button is right there when the shape is what you want.
  useEffect(() => {
    const id = window.requestAnimationFrame(() => {
      zoomRef.current?.centerView(Math.max(fitScale(), READABLE_SCALE), 0)
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
        // See WHEEL_STEP: without smooth={false} the wheel step is scaled by
        // deltaY, and one mouse click saturates the zoom range.
        smooth={false}
        wheel={{ step: WHEEL_STEP }}
        panning={{ velocityDisabled: true }}
      >
        <>
          <div className="absolute top-3 right-3 z-10 flex flex-col gap-1.5">
            <button
              type="button"
              onClick={() => zoomRef.current?.zoomIn(BUTTON_STEP)}
              className={control}
              aria-label="Zoom in"
            >
              <Plus size={15} />
            </button>
            <button
              type="button"
              onClick={() => zoomRef.current?.zoomOut(BUTTON_STEP)}
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

              {/* Descents first so nodes paint over their endpoints. */}
              <g fill="none" stroke="rgb(148 163 184 / 0.45)" strokeWidth={1.5}>
                {/* Aunt/uncle beside the parent they are a sibling of.
                    Dashed, so it never reads as a parent-child descent. */}
                {siblingLinks.map((link) => (
                  <path
                    key={link.key}
                    d={`M ${link.x1} ${link.y} H ${link.x2}`}
                    strokeDasharray="4 4"
                  />
                ))}
                {descents.map((d) => {
                  const cx = (p: Placed) => p.x + NODE_W / 2
                  const parentXs = d.parents.map(cx)
                  const childXs = d.children.map(cx)
                  // The bus has to reach the trunk even when every child sits
                  // to one side of the parents.
                  const busLeft = Math.min(d.junctionX, ...childXs)
                  const busRight = Math.max(d.junctionX, ...childXs)

                  return (
                    <g key={d.key}>
                      {/* a stem down from each parent */}
                      {d.parents.map((p) => (
                        <path
                          key={p.person.id}
                          d={`M ${cx(p)} ${p.y + NODE_H} V ${d.stemY}`}
                        />
                      ))}
                      {/* joining bar — "both are parents of these children",
                          which is recorded; not a marriage line */}
                      {d.parents.length > 1 && (
                        <path
                          d={`M ${Math.min(...parentXs)} ${d.stemY} H ${Math.max(...parentXs)}`}
                        />
                      )}
                      {/* one trunk down to the bus, then the bus itself */}
                      <path d={`M ${d.junctionX} ${d.stemY} V ${d.busY}`} />
                      {busRight - busLeft > 0.5 && (
                        <path d={`M ${busLeft} ${d.busY} H ${busRight}`} />
                      )}
                      {/* a drop to each child */}
                      {d.children.map((c) => (
                        <path
                          key={c.person.id}
                          d={`M ${cx(c)} ${d.busY} V ${c.y}`}
                        />
                      ))}
                    </g>
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
