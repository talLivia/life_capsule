'use client'

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Maximize2, Minus, Plus } from 'lucide-react'
import {
  TransformComponent,
  TransformWrapper,
  type ReactZoomPanPinchRef,
} from 'react-zoom-pan-pinch'
import {
  NODE_H,
  NODE_W,
  PAD,
  LABEL_W,
  computeTreeLayout,
  type Descent,
  type Placed,
} from '@/lib/treeLayout'
import type { FamilyTree, TreePerson } from '@/lib/types'

/**
 * The family tree as a node graph with drawn connections.
 *
 * EVERY COORDINATE COMES FROM lib/treeLayout.ts, which is pure and runs
 * outside a browser (scripts/tree_layout_report.mjs prints the segments for a
 * live tree — how "there is an extra line here" reports get root-caused).
 * This file owns SVG, pointer handling, and the viewport; it computes
 * nothing about the family.
 *
 * Node and edge rendering is hand-built SVG, deliberately. A family tree is
 * not a tree — two parents point at one child, so it is a DAG, and the usual
 * layout packages assume exactly one parent per node. Only the VIEWPORT is a
 * library (react-zoom-pan-pinch): pointer-anchored wheel zoom, pinch, and
 * telling a drag apart from a click are fiddly in ways that feel broken when
 * slightly wrong, and none of them are about family structure.
 *
 * ## Highlighting: hover (or select) a PERSON, see THEIR lines
 *
 * Two children of one parent share a trunk and a bus, so their connectors
 * overlap by construction and "which line is hers?" has no answer at rest.
 * Hovering a node lights the segments on that person's own path — their
 * drop, the shared trunk/bar/bus, the parent stems — and NOT their siblings'
 * drops, which is exactly the distinction the overlap erases. Hovering a
 * parent lights the whole family's fork. Selection (click) keeps the same
 * highlight for touch screens, where hover does not exist.
 */

const MIN_SCALE = 0.15
const MAX_SCALE = 2.5
/** Never open smaller than this — a readable view you have to pan beats a
 *  complete one you cannot read. */
const READABLE_SCALE = 0.6
/**
 * Wheel and button zoom are both LINEAR steps, via `smooth={false}`: the
 * library's default multiplies the step by |deltaY|, and a Windows mouse
 * wheel click reports 100, saturating the zoom range in one click.
 */
const WHEEL_STEP = 0.1
const BUTTON_STEP = 0.15

/** How far a pointer may travel between down and up and still count as a
 *  click — nudging the canvas while pressing a node must stay a pan. */
const CLICK_SLOP_PX = 5

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

const IDLE_STROKE = 'rgb(148 163 184 / 0.45)'
const IDLE_ARC_STROKE = 'rgb(148 163 184 / 0.3)'
const IDLE_SPOUSE_STROKE = 'rgb(148 163 184 / 0.55)'
/** The highlight. A colour change alone can vanish on a thin line at low
 *  zoom, so the width moves with it. */
const ACTIVE_STROKE = 'rgb(96 165 250 / 0.95)'
const ACTIVE_WIDTH = 2.5

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
  const [hoveredId, setHoveredId] = useState<string | null>(null)

  const layout = useMemo(() => computeTreeLayout(tree), [tree])
  const {
    placed, rowLabels, descents, siblingLinks, spouseLinks, siblingArcs,
    bandHeadings, arcSpacePx, width, height,
  } = layout

  // Hover wins over selection while it lasts: the mouse is asking a more
  // specific question than the last click did.
  const activeId = hoveredId ?? selectedId

  /** Scale at which the whole chart fits the container. Never above 1. */
  const fitScale = useCallback(() => {
    const el = containerRef.current
    if (!el) return 1
    return Math.max(
      MIN_SCALE,
      Math.min(1, (el.clientWidth - 32) / width, (el.clientHeight - 32) / height)
    )
  }, [width, height])

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

  const inGroup = (d: Descent, id: string | null) =>
    id !== null &&
    (d.parents.some((p) => p.person.id === id) ||
      d.children.some((c) => c.person.id === id))
  const isParent = (d: Descent, id: string | null) =>
    id !== null && d.parents.some((p) => p.person.id === id)

  const seg = (active: boolean, idle: string = IDLE_STROKE) => ({
    stroke: active ? ACTIVE_STROKE : idle,
    strokeWidth: active ? ACTIVE_WIDTH : 1.5,
  })

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
              <defs>
                {/* One shared clip for every node's portrait: userSpaceOnUse
                    resolves inside each node's translated <g>, so the same
                    local circle fits them all. */}
                <clipPath id="tree-portrait-clip" clipPathUnits="userSpaceOnUse">
                  <circle cx={NODE_H / 2} cy={NODE_H / 2} r={16} />
                </clipPath>
              </defs>
              {/* A side branch gets a heading and a divider, so "same
                  generation" and "same branch" stop being the same position. */}
              {bandHeadings.map((heading) => (
                <g key={heading.band}>
                  <line
                    x1={heading.dividerX}
                    y1={PAD + arcSpacePx}
                    x2={heading.dividerX}
                    y2={height - PAD}
                    stroke="rgb(148 163 184 / 0.15)"
                    strokeWidth={1}
                    strokeDasharray="2 6"
                  />
                  <text
                    x={heading.x}
                    y={heading.y}
                    textAnchor="middle"
                    dominantBaseline="hanging"
                    className="fill-gray-500 text-[11px] uppercase tracking-wide"
                  >
                    {heading.text}
                  </text>
                </g>
              ))}

              {/* Row labels, inside the transform so they travel with the rows. */}
              <g>
                {rowLabels.map((label) => (
                  <text
                    key={label.y}
                    x={PAD + LABEL_W - 18}
                    y={label.y + NODE_H / 2}
                    textAnchor="end"
                    dominantBaseline="central"
                    className="fill-gray-500 text-[11px] uppercase tracking-wide"
                  >
                    {label.text}
                  </text>
                ))}
              </g>

              {/* Connectors first so nodes paint over their endpoints. */}
              <g fill="none">
                {/* The one line joining a side branch to the family. Dashed
                    and light: "these two are siblings", never "the spine". */}
                {siblingArcs.map((arc) => {
                  const active = activeId === arc.a || activeId === arc.b
                  return (
                    <path
                      key={arc.key}
                      d={arc.d}
                      strokeDasharray="5 5"
                      {...seg(active, IDLE_ARC_STROKE)}
                    />
                  )
                })}
                {/* Aunt/uncle beside the parent they are a sibling of.
                    Dashed, so it never reads as a parent-child descent. */}
                {siblingLinks.map((link) => {
                  const active = activeId === link.a || activeId === link.b
                  return (
                    <path
                      key={link.key}
                      d={`M ${link.x1} ${link.y} H ${link.x2}`}
                      strokeDasharray="4 4"
                      {...seg(active)}
                    />
                  )
                })}
                {/* A recorded marriage: the genealogy double line. Solid where
                    the sibling dash is dashed, so the two same-row claims can
                    never be misread as each other. ONE connector per couple —
                    the layout deduplicates by pair. */}
                {spouseLinks.map((link) => {
                  const active = activeId === link.a || activeId === link.b
                  const style = seg(active, IDLE_SPOUSE_STROKE)
                  return (
                    <g key={link.key} {...style} strokeWidth={active ? 2 : 1.5}>
                      <path d={`M ${link.x1} ${link.y - 2.5} H ${link.x2}`} />
                      <path d={`M ${link.x1} ${link.y + 2.5} H ${link.x2}`} />
                    </g>
                  )
                })}
                {descents.map((d) => {
                  const cx = (p: Placed) => p.x + NODE_W / 2
                  const parentXs = d.parents.map(cx)
                  const childXs = d.children.map(cx)
                  const busLeft = Math.min(d.junctionX, ...childXs)
                  const busRight = Math.max(d.junctionX, ...childXs)
                  // Shared pieces light for anyone in the family; a child's
                  // hover keeps their SIBLINGS' drops dim — that distinction
                  // is what the highlight exists for.
                  const familyActive = inGroup(d, activeId)
                  const parentActive = isParent(d, activeId)

                  return (
                    <g key={d.key}>
                      {/* a stem down from each parent */}
                      {d.parents.map((p) => (
                        <path
                          key={p.person.id}
                          d={`M ${cx(p)} ${p.y + NODE_H} V ${d.stemY}`}
                          {...seg(familyActive)}
                        />
                      ))}
                      {/* joining bar — "both are parents of these children",
                          which is recorded; not a marriage line */}
                      {d.parents.length > 1 && (
                        <path
                          d={`M ${Math.min(...parentXs)} ${d.stemY} H ${Math.max(...parentXs)}`}
                          {...seg(familyActive)}
                        />
                      )}
                      {/* one trunk down to the bus, then the bus itself */}
                      <path
                        d={`M ${d.junctionX} ${d.stemY} V ${d.busY}`}
                        {...seg(familyActive)}
                      />
                      {busRight - busLeft > 0.5 && (
                        <path
                          d={`M ${busLeft} ${d.busY} H ${busRight}`}
                          {...seg(familyActive)}
                        />
                      )}
                      {/* a drop to each child */}
                      {d.children.map((c) => (
                        <path
                          key={c.person.id}
                          d={`M ${cx(c)} ${d.busY} V ${c.y}`}
                          {...seg(parentActive || activeId === c.person.id)}
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
                      onPointerEnter={() => setHoveredId(person.id)}
                      onPointerLeave={() =>
                        setHoveredId((current) => (current === person.id ? null : current))
                      }
                      onFocus={() => setHoveredId(person.id)}
                      onBlur={() =>
                        setHoveredId((current) => (current === person.id ? null : current))
                      }
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

                      {/* The primary photo swaps into this same circle when
                          one exists — same size, same position, the §9.6
                          rule. Initials remain the placeholder. */}
                      <circle
                        cx={NODE_H / 2}
                        cy={NODE_H / 2}
                        r={16}
                        className={person.is_self ? 'fill-primary-500/30' : 'fill-white/8'}
                      />
                      {person.photo_url ? (
                        <image
                          href={person.photo_url}
                          x={NODE_H / 2 - 16}
                          y={NODE_H / 2 - 16}
                          width={32}
                          height={32}
                          preserveAspectRatio="xMidYMid slice"
                          clipPath="url(#tree-portrait-clip)"
                        />
                      ) : (
                        <text
                          x={NODE_H / 2}
                          y={NODE_H / 2}
                          textAnchor="middle"
                          dominantBaseline="central"
                          className="fill-white text-[12px] font-semibold"
                        >
                          {initials(person.name)}
                        </text>
                      )}

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
