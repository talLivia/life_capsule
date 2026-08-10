import type { FamilyTree, TreePerson } from '@/lib/types'

/**
 * The family tree's LAYOUT, as pure functions over the server's tree.
 *
 * Extracted from FamilyTreeGraph.tsx so the geometry can be executed outside
 * a browser — scripts/tree_layout_report.mjs runs it against the live tree
 * and prints every segment, which is how a "there are extra lines here"
 * report gets root-caused with evidence instead of squinting at a screenshot.
 * The component owns pointer handling and SVG; this owns every coordinate.
 *
 * ## Bands: "same generation" and "same branch" are different facts
 *
 * The producer's own line (the spine) renders as the MAIN band; each family
 * that only touches it sideways renders as a side band with a heading. Three
 * kinds of head start a band:
 *
 *   * an ancestor-row person NOT on the spine — an aunt or uncle;
 *   * a COUPLE of such heads is ONE band (spouse edge or recorded child in
 *     common): two walks over shared children let whichever ran last claim
 *     them, and left another band free to land between the partners;
 *   * a generation-0 person with a family of their own — a spouse or
 *     children — who is not the producer or their partner. A sibling with a
 *     spouse and three children is a household, and inlining all of them in
 *     the producer's row buries the row (reported live: ניר and אירה
 *     squeezed between the producer's other siblings).
 *
 * A head married into the spine is not banded at all — their place is beside
 * their spouse in the main family, and the ordering pass keeps them there.
 *
 * ## Connectors are deduplicated by PAIR, not drawn per edge
 *
 * Symmetric relations arrive once per direction from the questionnaire
 * (ניצן-sibling->יובל AND יובל-sibling->ניצן exist in the live archive), and
 * two edges for one fact drew two connectors — two identical dashes on top
 * of each other, or worse, two ARCS in different lanes reading as two
 * relations. Every same-row connector and arc is emitted once per unordered
 * pair.
 */

export const NODE_W = 148
export const NODE_H = 58
export const COL_GAP = 26
export const ROW_GAP = 104
export const PAD = 20
/** Left gutter for the row labels, which live inside the SVG so that they pan
 *  and zoom with the rows they name — as separate DOM they would drift. */
export const LABEL_W = 150

/**
 * Only these draw a descent. An `aunt_uncle` edge places somebody in the
 * parents' row — correctly, they are that generation — but drawing a trunk
 * down from them to the producer says they are a PARENT. Placement and
 * parenthood are different claims and only one of them is a line.
 */
const DESCENT_TYPES = new Set(['parent', 'child'])

const SIBLING_RELATION = 'sibling'

/** Space between the main line and a side branch. */
export const BAND_GAP = 96
/** Room above the rows for the branch headings. */
export const BAND_LABEL_H = 26
/** Height of one connector lane above the chart — see the arc comment in the
 *  component: above the rows is the only band of space nothing competes for. */
export const ARC_LANE_H = 16

/** Descent geometry, all of it inside the row gap. */
const STEM_DROP = 24 // parent bottom -> joining bar
const BUS_LIFT = 34 // child top -> bus
const BUS_LEVEL_GAP = 13 // separation between overlapping groups' buses

export interface Placed {
  person: TreePerson
  x: number // left edge
  y: number // top edge
}

/** One parent-set and every child hanging off it. */
export interface Descent {
  key: string
  parents: Placed[]
  children: Placed[]
  stemY: number
  busY: number
  junctionX: number
}

export interface RowLink {
  key: string
  a: string
  b: string
  x1: number
  x2: number
  y: number
}

export interface Arc {
  key: string
  a: string
  b: string
  d: string
}

export interface TreeLayout {
  placed: Map<string, Placed>
  rowLabels: { text: string; y: number }[]
  descents: Descent[]
  siblingLinks: RowLink[]
  spouseLinks: RowLink[]
  siblingArcs: Arc[]
  bandHeadings: {
    band: string
    text: string
    x: number
    y: number
    left: number
    dividerX: number
  }[]
  arcSpacePx: number
  width: number
  height: number
}

/**
 * Short on purpose. These are drawn in a fixed-width gutter beside the rows,
 * and longer labels ran underneath the first node of their own row.
 */
const GENERATION_LABELS: Record<number, string> = {
  [-2]: 'Grandparents',
  [-1]: 'Parents',
  0: 'You',
  1: 'Children',
  2: 'Grandchildren',
}

export function generationLabel(generation: number): string {
  if (GENERATION_LABELS[generation]) return GENERATION_LABELS[generation]
  const n = Math.abs(generation)
  return generation < 0 ? `${n} up` : `${n} down`
}

const pairKey = (a: string, b: string) => (a < b ? `${a}|${b}` : `${b}|${a}`)

/**
 * Who forms a COUPLE, for layout purposes: a spouse edge, or two people with
 * a recorded child in common. Both are recorded facts — this never infers a
 * marriage, it only reads what an edge already says.
 */
export function couplePairs(edges: FamilyTree['edges']): [string, string][] {
  const pairs = new Map<string, [string, string]>()
  const add = (a: string, b: string) => {
    if (a === b) return
    if (!pairs.has(pairKey(a, b))) pairs.set(pairKey(a, b), [a, b])
  }
  const parentsOfChild = new Map<string, Set<string>>()
  for (const e of edges) {
    if (e.relation_type === 'spouse') add(e.from_id, e.to_id)
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
 * the neighbours it is already connected to in the row above. One pass,
 * top-down — an iterative solver would reshuffle the whole chart when a
 * single relation is added.
 *
 * After the sort, COUPLES ARE PULLED ADJACENT: a spouse with no relations in
 * the row above sorts to the end of the row alphabetically, and a sibling
 * whose average lands between two partners splits them.
 */
export function orderRows(
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
        return linked.length
          ? linked.reduce((s, i) => s + i, 0) / linked.length
          : Number.MAX_SAFE_INTEGER
      }
      const diff = key(a) - key(b)
      return diff !== 0 ? diff : a.name.localeCompare(b.name)
    })
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

/**
 * Which people are on the producer's own line, and which head or belong to a
 * side band — see the module comment for the three kinds of head.
 */
export function assignBands(
  tree: FamilyTree,
  generationOf: Map<string, number>,
  couples: [string, string][]
): {
  bandOf: Map<string, string>
  headName: Map<string, string>
  /** The generation the band's heads sit in — 0 for a direct sibling's own
   *  family, negative for an aunt/uncle branch. Drives band ORDER: a
   *  sibling's household belongs beside the producer's line, an extended
   *  branch further out. */
  bandGen: Map<string, number>
} {
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
  const bandGen = new Map<string, number>()
  const nameOf = new Map<string, string>()
  for (const row of tree.generations) {
    for (const person of row.people) nameOf.set(person.id, person.name)
  }

  const partnersOf = (id: string) =>
    couples.flatMap(([a, b]) => (a === id ? [b] : b === id ? [a] : []))

  // Ancestor-row heads: everyone up there who is not on the spine.
  const heads: string[] = []
  for (const row of tree.generations) {
    if (row.generation >= 0) continue
    for (const head of row.people) {
      if (!spine.has(head.id)) heads.push(head.id)
    }
  }

  // Generation-0 heads: a sibling (or anyone else in the producer's row) with
  // a family of their own. The producer and their own partner stay home —
  // their family IS the main band.
  const rootPartners = new Set(root ? partnersOf(root) : [])
  const hasChildrenBelow = (id: string) =>
    (family.get(id) ?? []).some(
      (n) =>
        (n.type === 'parent' || n.type === 'child') &&
        (generationOf.get(n.id) ?? 0) > (generationOf.get(id) ?? 0)
    )
  for (const row of tree.generations) {
    if (row.generation !== 0) continue
    for (const head of row.people) {
      if (head.id === root || rootPartners.has(head.id)) continue
      if (partnersOf(head.id).length === 0 && !hasChildrenBelow(head.id)) continue
      heads.push(head.id)
    }
  }

  // Union couples into shared groups. Tiny union-find — a family tree's
  // couple list is a handful of pairs.
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
    // family, not at the top of a side branch. The producer's own row is the
    // exception — there the SPOUSE IS the reason for the band, and the spine
    // member (the sibling) leads it.
    const isAncestor = (generationOf.get(head) ?? 0) < 0
    if (isAncestor && partnersOf(head).some((id) => spine.has(id))) continue
    const key = find(head)
    groups.set(key, [...(groups.get(key) ?? []), head])
  }

  groups.forEach((allHeads) => {
    // An ancestor walk may already have claimed a would-be head (a cousin
    // with children of their own belongs to their parent's band, not a band
    // of their own). Ancestor groups run first — Map iteration is insertion
    // order, and ancestor heads are collected first — so the filter is
    // enough.
    const groupHeads = allHeads.filter((id) => !bandOf.has(id))
    if (!groupHeads.length) return
    const band = [...groupHeads].sort()[0]
    const names = groupHeads
      .map((id) => nameOf.get(id) ?? '')
      .filter(Boolean)
      .sort((a, b) => a.localeCompare(b))
    headName.set(band, names.join(' & '))
    bandGen.set(band, Math.min(...groupHeads.map((id) => generationOf.get(id) ?? 0)))
    // A generation-0 branch is LED by a spine member (the producer's
    // sibling), so its walk may enter the spine — but only ever DOWNWARD,
    // into that sibling's own children. Ancestor branches never absorb the
    // spine at all.
    const genZeroBand = groupHeads.some((id) => (generationOf.get(id) ?? 0) === 0)
    // ONE walk from the whole couple, with one seen-set — two walks over the
    // shared children had whichever ran last silently claim them.
    const queue = [...groupHeads]
    const seen = new Set(groupHeads)
    for (const id of groupHeads) bandOf.set(id, band)
    while (queue.length) {
      const current = queue.shift()!
      for (const next of family.get(current) ?? []) {
        if (next.type !== 'parent' && next.type !== 'child') continue
        if (seen.has(next.id) || headSet.has(next.id) || bandOf.has(next.id)) continue
        // Downwards only: an uncle's PARENT is not part of his branch.
        if ((generationOf.get(next.id) ?? 0) <= (generationOf.get(current) ?? 0)) continue
        if (next.id === tree.root_id) continue
        if (!genZeroBand && spine.has(next.id)) continue
        seen.add(next.id)
        bandOf.set(next.id, band)
        queue.push(next.id)
      }
    }
  })
  return { bandOf, headName, bandGen }
}

export function computeTreeLayout(tree: FamilyTree): TreeLayout {
  const couples = couplePairs(tree.edges)
  const rows = orderRows(tree.generations, tree.edges, couples)
  const generationOf = new Map<string, number>()
  tree.generations.forEach((row) =>
    row.people.forEach((p) => generationOf.set(p.id, row.generation))
  )
  const { bandOf, headName, bandGen } = assignBands(tree, generationOf, couples)

  // Band order encodes CLOSENESS, not discovery: a direct sibling's own
  // family (a generation-0 head) sits BESIDE the producer's line, in the
  // otherwise-empty space to its left; aunt/uncle branches sit further out
  // to the right. "First appearance" alone put ניר & אירה past every
  // extended-family band, purely because row 0 is scanned after row -1.
  // Within each side, first appearance still fixes the order so the chart
  // does not reshuffle between loads.
  const MAIN = '__main__'
  const discovered: string[] = []
  rows.forEach((row) =>
    row.forEach((p) => {
      const band = bandOf.get(p.id) ?? MAIN
      if (band !== MAIN && !discovered.includes(band)) discovered.push(band)
    })
  )
  const bandOrder: string[] = [
    ...discovered.filter((band) => (bandGen.get(band) ?? -1) === 0),
    MAIN,
    ...discovered.filter((band) => (bandGen.get(band) ?? -1) < 0),
  ]

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
  // One lane per cross-band sibling connector, reserved ABOVE the headings.
  // Counted per unordered PAIR: the questionnaire records a symmetric
  // relation once per direction, and counting edges gave the same pair two
  // lanes — and two arcs.
  const arcPairs = new Set<string>()
  for (const edge of tree.edges) {
    if (edge.relation_type !== SIBLING_RELATION) continue
    const a = bandOf.get(edge.from_id) ?? MAIN
    const b = bandOf.get(edge.to_id) ?? MAIN
    if (a !== b) arcPairs.add(pairKey(edge.from_id, edge.to_id))
  }
  const arcSpace = arcPairs.size * ARC_LANE_H
  const topPad = PAD + arcSpace + (hasBranches ? BAND_LABEL_H : 0)

  const out = new Map<string, Placed>()
  const labels: { text: string; y: number }[] = []
  const columnOf = new Map<string, number>()
  rows.forEach((row, rowIndex) => {
    const y = topPad + rowIndex * (NODE_H + ROW_GAP)
    labels.push({ text: generationLabel(tree.generations[rowIndex].generation), y })
    membersByRowBand[rowIndex].forEach((people, band) => {
      const start =
        (bandStart.get(band) ?? PAD + LABEL_W) +
        ((bandWidth.get(band) ?? 0) - spanOf(people.length)) / 2
      people.forEach((person, i) => {
        out.set(person.id, { person, x: start + i * (NODE_W + COL_GAP), y })
        columnOf.set(person.id, i)
      })
    })
  })

  const mainStart = bandStart.get(MAIN) ?? 0
  const bandHeadings = bandOrder
    .filter((band) => band !== MAIN)
    .map((band) => {
      const left = bandStart.get(band) ?? 0
      return {
        band,
        text: `${headName.get(band) ?? ''}'s family`,
        x: left + (bandWidth.get(band) ?? 0) / 2,
        y: PAD + arcSpace + 4,
        left,
        // The divider goes on the band's MAIN-facing side — a band seated to
        // the LEFT of the producer's line needs it on its right, or the two
        // read as one group and the gutter gets a pointless line instead.
        dividerX:
          left < mainStart
            ? left + (bandWidth.get(band) ?? 0) + BAND_GAP / 2
            : left - BAND_GAP / 2,
      }
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
  byGap.forEach((groupsInGap) => {
    const span = (g: { parents: Placed[]; children: Placed[] }) => {
      const xs = [...g.children.map(centreOf), ...g.parents.map(centreOf)]
      return [Math.min(...xs), Math.max(...xs)] as const
    }
    const rightEdgeAt: number[] = []
    groupsInGap
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
        level.set(
          key,
          Math.min(lvl, Math.floor((ROW_GAP - BUS_LIFT - STEM_DROP - 8) / BUS_LEVEL_GAP))
        )
      })
  })

  const descents: Descent[] = []
  grouped.forEach((g, key) => {
    const stemY = Math.max(...g.parents.map((p) => p.y)) + NODE_H + STEM_DROP
    const childY = g.children[0].y
    const busY = Math.max(
      stemY + 8,
      childY - BUS_LIFT - (level.get(key) ?? 0) * BUS_LEVEL_GAP
    )
    const parentXs = g.parents.map(centreOf)
    descents.push({
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
   * Same-row connectors. Emitted once per unordered pair — see the module
   * comment; symmetric relations arrive once per direction.
   *
   * The sibling dash survives only in ancestor rows, between ADJACENT
   * columns of one band: row -1 holds the producer's parents AND their
   * siblings, and the line is what tells an uncle apart from a father.
   */
  const rootY = out.get(tree.root_id ?? '')?.y
  const drawnPairs = new Set<string>()
  const rowLink = (edge: FamilyTree['edges'][number]): RowLink => {
    const a = out.get(edge.from_id)!
    const b = out.get(edge.to_id)!
    const left = a.x < b.x ? a : b
    const right = a.x < b.x ? b : a
    return {
      key: `${edge.relation_type}-${pairKey(edge.from_id, edge.to_id)}`,
      a: edge.from_id,
      b: edge.to_id,
      x1: left.x + NODE_W,
      x2: right.x,
      y: left.y + NODE_H / 2,
    }
  }
  const adjacentSameBand = (edge: FamilyTree['edges'][number]) => {
    const a = out.get(edge.from_id)
    const b = out.get(edge.to_id)
    if (!a || !b || a.y !== b.y) return false
    if ((bandOf.get(edge.from_id) ?? MAIN) !== (bandOf.get(edge.to_id) ?? MAIN)) return false
    return (
      Math.abs((columnOf.get(edge.from_id) ?? 0) - (columnOf.get(edge.to_id) ?? 0)) === 1
    )
  }

  const siblingLinks: RowLink[] = []
  const spouseLinks: RowLink[] = []
  for (const edge of tree.edges) {
    if (edge.relation_type === SIBLING_RELATION) {
      const a = out.get(edge.from_id)
      if (!a || rootY === undefined || a.y >= rootY) continue
      if (!adjacentSameBand(edge)) continue
      const key = `sib-${pairKey(edge.from_id, edge.to_id)}`
      if (drawnPairs.has(key)) continue
      drawnPairs.add(key)
      siblingLinks.push(rowLink(edge))
    }
    if (edge.relation_type === 'spouse') {
      if (!adjacentSameBand(edge)) continue
      const key = `sp-${pairKey(edge.from_id, edge.to_id)}`
      if (drawnPairs.has(key)) continue
      drawnPairs.add(key)
      spouseLinks.push(rowLink(edge))
    }
  }

  /**
   * Cross-band sibling connectors, routed through the space ABOVE the chart —
   * the only band of space nothing else competes for. Lanes are assigned per
   * PAIR, so a symmetric relation stored in both directions cannot occupy
   * two lanes and read as two relations.
   */
  const arcLane = new Map<string, number>()
  const siblingArcs: Arc[] = []
  for (const edge of tree.edges) {
    if (edge.relation_type !== SIBLING_RELATION) continue
    const a = out.get(edge.from_id)
    const b = out.get(edge.to_id)
    if (!a || !b || a.y !== b.y) continue
    if ((bandOf.get(edge.from_id) ?? MAIN) === (bandOf.get(edge.to_id) ?? MAIN)) continue
    const key = pairKey(edge.from_id, edge.to_id)
    if (arcLane.has(key)) continue
    // Deepest lane nearest the row, so a longer arc never crosses a shorter
    // one on its way over.
    const lane = a.y - 18 - arcLane.size * ARC_LANE_H
    arcLane.set(key, lane)
    siblingArcs.push({
      key: `arc-${key}`,
      a: edge.from_id,
      b: edge.to_id,
      d: `M ${a.x + NODE_W / 2} ${a.y} V ${lane} H ${b.x + NODE_W / 2} V ${b.y}`,
    })
  }

  return {
    placed: out,
    rowLabels: labels,
    descents,
    siblingLinks,
    spouseLinks,
    siblingArcs,
    bandHeadings,
    arcSpacePx: arcSpace,
    width: totalWidth + PAD,
    height:
      topPad + rows.length * NODE_H + Math.max(0, rows.length - 1) * ROW_GAP + PAD,
  }
}
