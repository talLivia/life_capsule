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
 * ## Only parent-child relations and marriages are drawn
 *
 * Sibling relations are shown by SHARING A ROW, not by a line. The row
 * already carries that information, and the connectors were pure noise: with
 * four siblings all recorded as siblings of the producer, the lines fanned
 * out from one node and had to be routed around the nodes in between to
 * avoid reading as a chain that nobody recorded. Deleting them deleted that
 * whole problem.
 *
 * A recorded MARRIAGE is the exception, drawn as the short double line
 * genealogy charts use. It is safe where the sibling connectors were not,
 * for the same reason the adjacent-column sibling dash is: COUPLES ARE KEPT
 * ADJACENT by the ordering pass, so the line never has anyone behind it. A
 * couple here means a spouse edge OR two people with a recorded child in
 * common — the joining bar below already says the second kind, the double
 * line says only the first, because only the first is a recorded marriage.
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

/** Space between the main line and a side branch. Wide enough that the
 *  gap reads as a separation rather than an unusually large column gap. */
const BAND_GAP = 96
/** Room above the rows for the branch headings. */
const BAND_LABEL_H = 26
/**
 * Height of one connector lane above the chart.
 *
 * The uncle connector arcs OVER the rows rather than dipping below them. Both
 * are node-free — every inter-row gap is, which is why neither reintroduces
 * the occlusion bug — but everything else that is drawn (parent trunks, child
 * buses) lives below the rows, so above is the only space nothing competes
 * for. With four uncles that is four dips fighting the trunks, or four arcs
 * over empty canvas.
 *
 * Lanes stack so two arcs never overlap, the same nesting the dips used.
 */
const ARC_LANE_H = 16

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

/**
 * Short on purpose. These are drawn in a fixed-width gutter beside the rows,
 * and "You and your generation" is wider than the gutter — it ran underneath
 * the first node of its own row. Reserving space is only half the fix if the
 * text can still outgrow it, so the text is what shrank.
 */
const GENERATION_LABELS: Record<number, string> = {
  [-2]: 'Grandparents',
  [-1]: 'Parents',
  0: 'You',
  1: 'Children',
  2: 'Grandchildren',
}

function generationLabel(generation: number): string {
  if (GENERATION_LABELS[generation]) return GENERATION_LABELS[generation]
  // Beyond the named rows, say the distance rather than inventing a word for
  // it — "3 generations up" is honest where "great-grandparents" might not be.
  const n = Math.abs(generation)
  return generation < 0 ? `${n} up` : `${n} down`
}

/**
 * Who forms a COUPLE, for layout purposes: a spouse edge, or two people with
 * a recorded child in common. Both are recorded facts — this never infers a
 * marriage, it only reads what an edge already says.
 *
 * Derived from co-parenthood as well as spouse edges because the two arrive
 * separately: the questionnaire captures "their children are ניצן and יובל"
 * without anyone ever saying "married", and a marriage can be recorded for a
 * couple whose children are not. Either fact alone is enough that drawing
 * somebody ELSE between the two misreads the family.
 */
function couplePairs(edges: FamilyTree['edges']): [string, string][] {
  const pairs = new Map<string, [string, string]>()
  const add = (a: string, b: string) => {
    if (a === b) return
    const key = a < b ? `${a}|${b}` : `${b}|${a}`
    if (!pairs.has(key)) pairs.set(key, [a, b])
  }
  const parentsOfChild = new Map<string, Set<string>>()
  for (const e of edges) {
    if (e.relation_type === 'spouse') add(e.from_id, e.to_id)
    // One directed row per relation: 'parent' reads "from is the parent of
    // to", 'child' the reverse.
    const [parent, child] =
      e.relation_type === 'parent'
        ? [e.from_id, e.to_id]
        : e.relation_type === 'child'
          ? [e.to_id, e.from_id]
          : [null, null]
    if (!parent || !child) continue
    if (!parentsOfChild.has(child)) parentsOfChild.set(child, new Set())
    parentsOfChild.get(child)!.add(parent)
  }
  parentsOfChild.forEach((parents) => {
    const list = Array.from(parents)
    for (let i = 0; i < list.length; i++)
      for (let j = i + 1; j < list.length; j++) add(list[i], list[j])
  })
  return Array.from(pairs.values())
}

/**
 * Order each row to reduce crossings: a node sits near the average position of
 * the neighbours it is already connected to in the row above.
 *
 * One pass, top-down. Enough for family-sized graphs, and predictable — an
 * iterative solver would reshuffle the whole chart when a single relation is
 * added, which is worse than a stray crossing on a page people revisit.
 *
 * After the sort, COUPLES ARE PULLED ADJACENT. The barycentric pass cannot do
 * this on its own: a spouse with no relations in the row above sorts to the
 * end of the row alphabetically, and a sibling whose average lands between
 * two partners splits them — which is how אמנון נחום came to be drawn in the
 * middle of a married couple. The partner later in the sorted row moves to
 * sit beside the earlier one, so the pass disturbs as little as possible.
 */
function orderRows(
  generations: FamilyTree['generations'],
  edges: FamilyTree['edges'],
  couples: [string, string][]
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
    // Couples adjacent, whatever the sort said. The mover is the LATER
    // partner, spliced in immediately after the earlier one.
    for (const [a, b] of couples) {
      const ia = ordered.findIndex((p) => p.id === a)
      const ib = ordered.findIndex((p) => p.id === b)
      if (ia === -1 || ib === -1 || Math.abs(ia - ib) === 1) continue
      const [anchor, mover] = ia < ib ? [ia, ib] : [ib, ia]
      const [moved] = ordered.splice(mover, 1)
      ordered.splice(anchor + 1, 0, moved)
    }
    rows.push(ordered)
    previousIndex = new Map(ordered.map((p, i) => [p.id, i]))
  }
  return rows
}

/** How far a pointer may travel between down and up and still count as a
 *  click. Without this, nudging the canvas while pressing a node opens
 *  somebody's moments on what the hand meant as a pan. */
const CLICK_SLOP_PX = 5

/**
 * Which people are on the producer's own line, and which hang off a side
 * branch.
 *
 * The problem this solves: generation was the only thing position encoded, so
 * a cousin — same generation, different branch — sat inline between the
 * producer's siblings. With a real family that row becomes an unreadable
 * strip, and the two facts it conflates are not the same fact.
 *
 * The SPINE is what you reach from the producer through parent, child and
 * sibling edges only: parents, siblings, children, grandparents,
 * grandchildren.
 *
 * Aunts and uncles are reached through `aunt_uncle`, so they are NOT on the
 * spine, and they head a band of their own together with their descendants —
 * a genuinely separate little tree off to the side. The single line back to
 * the parent they are a sibling of is the only thing joining it to the main
 * family, which is the point: it should read as an arc reaching over, not as
 * a spine line cutting through.
 *
 * A COUPLE OF HEADS IS ONE BAND. An aunt and the spouse she raised children
 * with are one household, not two side branches — and treating them as two
 * heads did worse than look odd: each ran its own walk over the SAME
 * children, whichever ran last silently claimed them, and the two halves of
 * the couple were left heading separate bands with another head's band free
 * to land between them. Heads whose couple includes somebody ON the spine
 * are not banded at all — their place is beside their spouse in the main
 * family, and the ordering pass keeps them there.
 */
function assignBands(
  tree: FamilyTree,
  generationOf: Map<string, number>,
  couples: [string, string][]
): { bandOf: Map<string, string>; headName: Map<string, string> } {
  const family = new Map<string, { id: string; type: string }[]>()
  for (const edge of tree.edges) {
    family.set(edge.from_id, [
      ...(family.get(edge.from_id) ?? []),
      { id: edge.to_id, type: edge.relation_type },
    ])
    family.set(edge.to_id, [
      ...(family.get(edge.to_id) ?? []),
      { id: edge.from_id, type: edge.relation_type },
    ])
  }

  // Ancestors and descendants always extend the spine. A SIBLING edge extends
  // it only from the producer themselves — your siblings are your line, your
  // parent's siblings are aunts and uncles, and the edge is identical.
  //
  // Getting this wrong is not subtle in its effect and is very subtle to spot:
  // the side question writes `uncle -sibling-> parent`, so treating sibling as
  // a spine edge everywhere pulled every uncle onto the spine and their
  // children with them, and the banding silently did nothing at all.
  const spine = new Set<string>()
  const root = tree.root_id
  if (root) {
    const queue = [root]
    spine.add(root)
    while (queue.length) {
      const current = queue.shift()!
      for (const next of family.get(current) ?? []) {
        const extends_ =
          next.type === 'parent' ||
          next.type === 'child' ||
          (next.type === 'sibling' && current === root)
        if (!extends_ || spine.has(next.id)) continue
        spine.add(next.id)
        queue.push(next.id)
      }
    }
  }

  const bandOf = new Map<string, string>()
  const headName = new Map<string, string>()
  const nameOf = new Map<string, string>()
  for (const row of tree.generations) {
    for (const person of row.people) nameOf.set(person.id, person.name)
  }

  // Branch heads: everyone in an ancestor row who is not on the spine — the
  // aunts and uncles. They stay in the main band; their descendants do not.
  const heads: string[] = []
  for (const row of tree.generations) {
    if (row.generation >= 0) continue
    for (const head of row.people) {
      if (!spine.has(head.id)) heads.push(head.id)
    }
  }

  // Union couples into shared groups. Tiny union-find — a family tree's
  // couple list is a handful of pairs, so path compression would be ceremony.
  const canon = new Map<string, string>()
  const find = (id: string): string => {
    let current = id
    while (canon.get(current) !== undefined && canon.get(current) !== current) {
      current = canon.get(current)!
    }
    return current
  }
  for (const [a, b] of couples) {
    const ra = find(a)
    const rb = find(b)
    if (ra !== rb) canon.set(ra < rb ? rb : ra, ra < rb ? ra : rb)
  }

  const headSet = new Set(heads)
  const groups = new Map<string, string[]>()
  for (const head of heads) {
    // A head married into the spine belongs beside their spouse in the main
    // family, not at the top of a side branch.
    const groupIds = [head, ...couples.flatMap(([a, b]) =>
      a === head ? [b] : b === head ? [a] : []
    )]
    if (groupIds.some((id) => spine.has(id))) continue
    const key = find(head)
    groups.set(key, [...(groups.get(key) ?? []), head])
  }

  groups.forEach((groupHeads) => {
    // Stable band id whichever order the heads arrive in.
    const band = [...groupHeads].sort()[0]
    const names = groupHeads
      .map((id) => nameOf.get(id) ?? '')
      .filter(Boolean)
      .sort((a, b) => a.localeCompare(b))
    headName.set(band, names.join(' & '))
    // ONE walk from the whole couple, with one seen-set — two walks over the
    // shared children had whichever ran last silently claim them.
    const queue = [...groupHeads]
    const seen = new Set(groupHeads)
    for (const id of groupHeads) bandOf.set(id, band)
    while (queue.length) {
      const current = queue.shift()!
      for (const next of family.get(current) ?? []) {
        if (next.type !== 'parent' && next.type !== 'child') continue
        if (seen.has(next.id) || spine.has(next.id) || headSet.has(next.id)) continue
        // Downwards only: an uncle's PARENT is not part of his branch.
        if ((generationOf.get(next.id) ?? 0) <= (generationOf.get(current) ?? 0)) continue
        seen.add(next.id)
        bandOf.set(next.id, band)
        queue.push(next.id)
      }
    }
  })
  return { bandOf, headName }
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
  const containerRef = useRef<HTMLDivElement>(null)
  const zoomRef = useRef<ReactZoomPanPinchRef>(null)
  const pressRef = useRef<{ x: number; y: number } | null>(null)

  const {
    placed, rowLabels, descents, siblingLinks, siblingArcs, spouseLinks,
    bandHeadings, arcSpacePx, width, height,
  } =
    useMemo(() => {
    const couples = couplePairs(tree.edges)
    const rows = orderRows(tree.generations, tree.edges, couples)
    const generationOf = new Map<string, number>()
    tree.generations.forEach((row) =>
      row.people.forEach((p) => generationOf.set(p.id, row.generation))
    )
    const { bandOf, headName } = assignBands(tree, generationOf, couples)

    // One band for the producer's own line, then one per side branch. Order is
    // fixed by first appearance so the chart does not reshuffle between loads.
    const MAIN = '__main__'
    const bandOrder: string[] = [MAIN]
    rows.forEach((row) =>
      row.forEach((p) => {
        const band = bandOf.get(p.id) ?? MAIN
        if (!bandOrder.includes(band)) bandOrder.push(band)
      })
    )

    const membersByRowBand = rows.map((row) => {
      const grouped = new Map<string, TreePerson[]>()
      row.forEach((p) => {
        const band = bandOf.get(p.id) ?? MAIN
        grouped.set(band, [...(grouped.get(band) ?? []), p])
      })
      return grouped
    })

    const spanOf = (n: number) => (n ? n * NODE_W + (n - 1) * COL_GAP : 0)
    const bandWidth = new Map<string, number>()
    bandOrder.forEach((band) => {
      bandWidth.set(
        band,
        Math.max(0, ...membersByRowBand.map((g) => spanOf((g.get(band) ?? []).length)))
      )
    })

    const bandStart = new Map<string, number>()
    let cursor = PAD + LABEL_W
    bandOrder.forEach((band) => {
      bandStart.set(band, cursor)
      cursor += (bandWidth.get(band) ?? 0) + BAND_GAP
    })
    const totalWidth = cursor - BAND_GAP

    const hasBranches = bandOrder.length > 1
    // One lane per cross-band sibling connector, reserved ABOVE the headings
    // so an arc never runs through a heading or a band divider.
    const arcCount = tree.edges.filter((edge) => {
      if (edge.relation_type !== SIBLING_RELATION) return false
      const a = bandOf.get(edge.from_id) ?? MAIN
      const b = bandOf.get(edge.to_id) ?? MAIN
      return a !== b
    }).length
    const arcSpace = arcCount * ARC_LANE_H
    const topPad = PAD + arcSpace + (hasBranches ? BAND_LABEL_H : 0)

    const out = new Map<string, Placed>()
    const labels: { text: string; y: number }[] = []
    const columnOf = new Map<string, number>()
    rows.forEach((row, rowIndex) => {
      const y = topPad + rowIndex * (NODE_H + ROW_GAP)
      labels.push({ text: generationLabel(tree.generations[rowIndex].generation), y })
      membersByRowBand[rowIndex].forEach((people, band) => {
        // Centred within its own band, so each band reads as one shape rather
        // than a left-aligned stack against the next one.
        const start =
          (bandStart.get(band) ?? PAD + LABEL_W) +
          ((bandWidth.get(band) ?? 0) - spanOf(people.length)) / 2
        people.forEach((person, i) => {
          out.set(person.id, { person, x: start + i * (NODE_W + COL_GAP), y })
          columnOf.set(person.id, i)
        })
      })
    })

    const bandHeadings = bandOrder
      .filter((band) => band !== MAIN)
      .map((band) => ({
        band,
        text: `${headName.get(band) ?? ''}'s family`,
        x: (bandStart.get(band) ?? 0) + (bandWidth.get(band) ?? 0) / 2,
        y: PAD + arcSpace + 4,
        left: bandStart.get(band) ?? 0,
      }))

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
    const rootY = out.get(tree.root_id ?? '')?.y
    // Same-band, adjacent columns: a short dash between facing edges, as
    // before. Nothing can be between them, so nothing can be behind the line.
    const siblingLinks = tree.edges
      .filter((edge) => {
        const a = out.get(edge.from_id)
        const b = out.get(edge.to_id)
        if (!a || !b || edge.relation_type !== SIBLING_RELATION) return false
        if (a.y !== b.y) return false
        if ((bandOf.get(edge.from_id) ?? MAIN) !== (bandOf.get(edge.to_id) ?? MAIN)) {
          return false
        }
        if (rootY === undefined || a.y >= rootY) return false
        return (
          Math.abs((columnOf.get(edge.from_id) ?? 0) - (columnOf.get(edge.to_id) ?? 0)) === 1
        )
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

    /**
     * Cross-band sibling connectors — the uncle and the parent they belong to.
     *
     * Routed through the gap ABOVE their row: up from one top edge, across,
     * down into the other. Every inter-row gap is node-free by construction,
     * so the line passes behind nothing; above the topmost row it crosses
     * nothing at all, not even another line. That is what makes this safe
     * where a straight line across the gutter is not — a straight line crosses
     * everything BETWEEN the endpoints, not just the empty gutter, and with
     * two uncle bands it would pass behind the first uncle to reach the second.
     *
     * Lanes stack so two arcs never sit on top of each other.
     */
    const siblingArcs = tree.edges
      .filter((edge) => {
        const a = out.get(edge.from_id)
        const b = out.get(edge.to_id)
        if (!a || !b || edge.relation_type !== SIBLING_RELATION) return false
        if (a.y !== b.y) return false
        return (bandOf.get(edge.from_id) ?? MAIN) !== (bandOf.get(edge.to_id) ?? MAIN)
      })
      .map((edge, index) => {
        const a = out.get(edge.from_id)!
        const b = out.get(edge.to_id)!
        // Deepest lane nearest the row, so a longer arc never crosses a
        // shorter one on its way over.
        const lane = a.y - 18 - index * ARC_LANE_H
        return {
          key: `arc-${edge.from_id}-${edge.to_id}`,
          d: `M ${a.x + NODE_W / 2} ${a.y} V ${lane} H ${b.x + NODE_W / 2} V ${b.y}`,
        }
      })

    /**
     * The marriage line — a recorded spouse edge, drawn as the double line
     * genealogy charts use, between the couple's facing edges.
     *
     * Restricted to ADJACENT columns of the same band for the same reason the
     * sibling dash is: a longer line passes behind whoever sits between. The
     * ordering pass makes couples adjacent, so in practice the restriction is
     * a guard, not a gap — and if a future layout ever fails to seat a couple
     * together, silently drawing a line through a stranger would be the worse
     * of the two bugs.
     *
     * Only for SPOUSE edges. A couple detected by co-parenthood already shows
     * as the joining bar below; giving them the marriage line too would claim
     * a marriage nobody recorded.
     */
    const spouseLinks = tree.edges
      .filter((edge) => {
        const a = out.get(edge.from_id)
        const b = out.get(edge.to_id)
        if (!a || !b || edge.relation_type !== 'spouse') return false
        if (a.y !== b.y) return false
        if ((bandOf.get(edge.from_id) ?? MAIN) !== (bandOf.get(edge.to_id) ?? MAIN)) {
          return false
        }
        return (
          Math.abs((columnOf.get(edge.from_id) ?? 0) - (columnOf.get(edge.to_id) ?? 0)) === 1
        )
      })
      .map((edge) => {
        const a = out.get(edge.from_id)!
        const b = out.get(edge.to_id)!
        const left = a.x < b.x ? a : b
        const right = a.x < b.x ? b : a
        return {
          key: `spouse-${edge.from_id}-${edge.to_id}`,
          x1: left.x + NODE_W,
          x2: right.x,
          y: left.y + NODE_H / 2,
        }
      })

    return {
      placed: out,
      rowLabels: labels,
      siblingLinks,
      siblingArcs,
      spouseLinks,
      descents: descentList,
      bandHeadings,
      arcSpacePx: arcSpace,
      width: totalWidth + PAD,
      height:
        topPad + rows.length * NODE_H + Math.max(0, rows.length - 1) * ROW_GAP + PAD,
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
              {/* A side branch gets a heading and a divider, so "same
                  generation" and "same branch" stop being the same position. */}
              {bandHeadings.map((heading) => (
                <g key={heading.band}>
                  <line
                    x1={heading.left - BAND_GAP / 2}
                    y1={PAD + arcSpacePx}
                    x2={heading.left - BAND_GAP / 2}
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
                    // Anchored to the RIGHT edge of the gutter, so the label
                    // grows away from the nodes instead of into them.
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

              {/* Descents first so nodes paint over their endpoints. */}
              <g fill="none" stroke="rgb(148 163 184 / 0.45)" strokeWidth={1.5}>
                {/* Aunt/uncle beside the parent they are a sibling of.
                    Dashed, so it never reads as a parent-child descent. */}
                {/* The one line joining a side branch to the family. Dashed
                    and light: it says "these two are siblings", never "this is
                    the spine". */}
                {siblingArcs.map((arc) => (
                  <path
                    key={arc.key}
                    d={arc.d}
                    stroke="rgb(148 163 184 / 0.3)"
                    strokeDasharray="5 5"
                  />
                ))}
                {siblingLinks.map((link) => (
                  <path
                    key={link.key}
                    d={`M ${link.x1} ${link.y} H ${link.x2}`}
                    strokeDasharray="4 4"
                  />
                ))}
                {/* A recorded marriage: the genealogy double line. Solid where
                    the sibling dash is dashed, so the two same-row claims can
                    never be misread as each other. */}
                {spouseLinks.map((link) => (
                  <g key={link.key} stroke="rgb(148 163 184 / 0.55)">
                    <path d={`M ${link.x1} ${link.y - 2.5} H ${link.x2}`} />
                    <path d={`M ${link.x1} ${link.y + 2.5} H ${link.x2}`} />
                  </g>
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
