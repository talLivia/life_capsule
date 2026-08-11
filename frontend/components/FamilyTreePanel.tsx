'use client'

import { useCallback, useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { AlertTriangle, Loader2, Network, User as UserIcon, X } from 'lucide-react'
import { FamilyTreeGraph } from '@/components/FamilyTreeGraph'
import { EntityPortrait } from '@/components/media/EntityPortrait'
import { toast } from 'react-hot-toast'
import { api } from '@/lib/api'
import type { ApiError, EntityMoment, FamilyTree, TreeEdge, TreePerson } from '@/lib/types'

/**
 * The producer's family tree — read-only, and the whole page.
 *
 * Everything shown here is decided by the server (app/services/family_tree.py):
 * which row someone sits in, who could not be placed, and which recordings
 * disagree. This file lays it out and does not recompute any of it.
 *
 * THE TREE NEVER GUESSES, and that shows up in the UI as three deliberate
 * things rather than as tidier output:
 *
 *   * people with no family path to the producer get their own list below the
 *     chart instead of being dropped or parked in the middle row;
 *   * a disagreement between recordings is shown, not resolved silently;
 *   * an empty tree says so and explains where relations come from, rather
 *     than rendering a blank canvas that looks broken.
 *
 * The chart gets the page and the moments open over it, rather than the two
 * splitting the width. `max-w-7xl` matches the chat view — the widest column
 * the site uses — and the height is what shows several generations at once.
 */

function lifespan(person: TreePerson): string | null {
  if (!person.year_start && !person.year_end) return null
  return `${person.year_start ?? '?'}–${person.year_end ?? ''}`.replace(/–$/, '–')
}

function PersonChip({
  person,
  onSelect,
  selected,
}: {
  person: TreePerson
  onSelect: (p: TreePerson) => void
  selected: boolean
}) {
  const years = lifespan(person)
  return (
    <button
      type="button"
      onClick={() => onSelect(person)}
      className={`px-3.5 py-2 rounded-xl border text-left transition-colors ${
        selected
          ? 'border-primary-400 bg-primary-500/15'
          : 'border-white/10 bg-surface-800/40 hover:border-white/25'
      }`}
    >
      <span dir="auto" className="block text-sm font-medium text-white">
        {person.name}
      </span>
      <span className="block text-[11px] text-gray-500 mt-0.5">
        {years || 'Tap to see their moments'}
      </span>
    </button>
  )
}

/**
 * One person's recordings, over the chart rather than beside it.
 *
 * Two things here are load-bearing and look like styling choices:
 *
 * The card is OPAQUE and does not use `glass-card`. That class carries
 * `backdrop-blur-xl`, and a backdrop-filtered element nested inside another
 * one (the overlay's own blur) filters against the outer backdrop ROOT — it
 * samples the page as it was *before* the overlay darkened it. The card then
 * shows the undimmed page straight through itself and the whole dialog reads
 * as transparent. One backdrop-filter per stack.
 *
 * It renders through a PORTAL to document.body. `position: fixed` is relative
 * to the nearest ancestor with a transform, filter or backdrop-filter rather
 * than to the viewport, and this page has all three above it. Portalling puts
 * the overlay out of reach of anything a future ancestor might do to it.
 */
/**
 * Set how this person is related to somebody else, from the tree itself.
 *
 * Extraction proposals are one-shot: the confirmation screen appears once per
 * recording and never comes back, so a relation the extractor missed had no
 * later home at all. This is that home.
 *
 * SAVING REPLACES. A relation that contradicts this one is removed rather than
 * kept beside it — the producer is making a deliberate statement while looking
 * at the tree it changes, and two edges the tree cannot both honour would
 * leave it to pick one at render, which is what made an earlier chosen parent
 * look as though it had failed to save. What was replaced is reported after,
 * never asked about before.
 */
function RelationEditor({
  person,
  people,
  types,
  parentsOf,
  onSaved,
}: {
  person: TreePerson
  people: TreePerson[]
  types: { value: string; label: string }[]
  /** Recorded parents by entity id, for the side-of-family question. */
  parentsOf: Map<string, TreePerson[]>
  onSaved: () => void
}) {
  const [relationType, setRelationType] = useState('')
  const [otherId, setOtherId] = useState('')
  const [sideParentId, setSideParentId] = useState('')
  const [saving, setSaving] = useState(false)

  const others = people.filter((p) => p.id !== person.id)

  /**
   * WHICH SIDE OF THE FAMILY — the same clarifying question the
   * post-recording questionnaire asks, in the same words, because the edge
   * alone has the same gap here: "uncle" places somebody in the parents' row
   * and says nothing about which parent they are the sibling of. Offered
   * whenever the other end has recorded parents to choose between; optional,
   * exactly like the questionnaire's version — not knowing the side must not
   * block recording the relation.
   */
  const sideKind =
    relationType === 'aunt_uncle' || relationType === 'grandparent' ? relationType : null
  const sideParents = sideKind && otherId ? (parentsOf.get(otherId) ?? []) : []

  const save = async () => {
    if (!relationType || !otherId) return
    setSaving(true)
    try {
      const result = await api.setEntityRelation(person.id, {
        other_entity_id: otherId,
        relation_type: relationType,
        // The card belongs to `person`, and the sentence reads
        // "<person> is the <type> of <other>" — so this end is the subject.
        direction: 'outgoing',
        ...(sideParents.length && sideParentId ? { side_parent_id: sideParentId } : {}),
      })
      const replaced = result?.replaced?.length ?? 0
      toast.success(
        replaced
          ? `Saved — the ${result.replaced[0].relation_type} link was replaced`
          : 'Saved',
      )
      setRelationType('')
      setOtherId('')
      setSideParentId('')
      onSaved()
    } catch (err: unknown) {
      const detail = (err as ApiError)?.response?.data?.detail || (err as ApiError)?.message
      toast.error(detail || 'Could not save that')
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="flex flex-col gap-2 p-3 rounded-xl border border-primary-500/30 bg-primary-500/5">
      <p className="text-xs font-semibold text-primary-200">How are they related?</p>
      <div className="flex flex-wrap items-center gap-2 text-sm text-white">
        <span dir="auto" className="font-medium">{person.name}</span>
        <span className="text-gray-400">is the</span>
        <select
          value={relationType}
          onChange={(e) => setRelationType(e.target.value)}
          disabled={saving}
          aria-label="Relation"
          className="px-2.5 py-1.5 rounded-lg bg-surface-800 border border-white/10 text-sm text-white"
        >
          <option value="">choose…</option>
          {/* Straight from the relation_types table, so adding a type is a
              data change and an invented one cannot be picked at all. */}
          {types.map((t) => (
            <option key={t.value} value={t.value}>{t.label}</option>
          ))}
        </select>
        <span className="text-gray-400">of</span>
        <select
          value={otherId}
          onChange={(e) => setOtherId(e.target.value)}
          disabled={saving}
          aria-label="Of whom"
          dir="auto"
          className="px-2.5 py-1.5 rounded-lg bg-surface-800 border border-white/10 text-sm text-white max-w-[11rem]"
        >
          <option value="">choose…</option>
          {others.map((p) => (
            <option key={p.id} value={p.id}>{p.is_self ? 'you' : p.name}</option>
          ))}
        </select>
      </div>

      {/* Same wording as the questionnaire (EntityConfirmModal's side
          question), so the two flows read as one feature. Clicking the
          chosen parent again clears it — a way back to saying nothing. */}
      {sideParents.length > 0 && (
        <fieldset className="flex flex-col gap-1.5">
          <legend className="text-xs text-white leading-relaxed mb-1">
            {sideKind === 'grandparent'
              ? 'Whose mother or father are they?'
              : 'Whose brother or sister are they?'}
          </legend>
          <div className="flex flex-wrap items-center gap-2">
            {sideParents.map((parent) => {
              const chosen = sideParentId === parent.id
              return (
                <button
                  key={parent.id}
                  type="button"
                  disabled={saving}
                  onClick={() => setSideParentId(chosen ? '' : parent.id)}
                  className={`px-2.5 py-1 rounded-lg border text-xs transition-colors ${
                    chosen
                      ? 'border-primary-400 bg-primary-500/15 text-white'
                      : 'border-white/10 bg-surface-800 text-gray-300 hover:border-white/25'
                  }`}
                >
                  <span dir="auto">{parent.name}</span>
                </button>
              )
            })}
          </div>
        </fieldset>
      )}

      {/* Stated before the click, on the same screen — not a dialog asking
          permission. The producer went out of their way to say this. */}
      <p className="text-[11px] text-gray-500">
        Saving replaces any relation that contradicts this one.
      </p>

      <div className="flex items-center gap-3">
        <button
          type="button"
          onClick={save}
          disabled={saving || !relationType || !otherId}
          className="btn-primary py-1.5 text-xs disabled:opacity-40"
        >
          {saving ? <Loader2 size={13} className="animate-spin" /> : 'Save'}
        </button>
      </div>
    </div>
  )
}

/**
 * This person's recorded relations, each with a way out.
 *
 * The editor above SETS relations, replacing what contradicts them — but a
 * relation that is simply WRONG, with nothing true to put in its place, had
 * no exit at all. Reported live: the questionnaire attached אילן to ג'ולי
 * (his wife's mother), and no possible edit could remove the claim without
 * making a different false one.
 *
 * Removal is TWO CLICKS ON THE SAME BUTTON, not a dialog: the second click
 * confirms, clicking anywhere else forgets. Same reasoning as the editor's
 * "saving replaces" note — state the consequence where the finger already
 * is, don't interrupt.
 */
function RelationList({
  person,
  people,
  edges,
  onRemoved,
}: {
  person: TreePerson
  people: TreePerson[]
  edges: TreeEdge[]
  onRemoved: () => void
}) {
  const [confirming, setConfirming] = useState<string | null>(null)
  const [removing, setRemoving] = useState(false)
  const nameOf = new Map(people.map((p) => [p.id, p.is_self ? 'you' : p.name]))

  const mine = edges.filter(
    (e) => e.from_id === person.id || e.to_id === person.id
  )
  if (!mine.length) return null

  const remove = async (edge: TreeEdge) => {
    setRemoving(true)
    try {
      await api.deleteEntityRelation(person.id, edge.id)
      toast.success('Relation removed')
      setConfirming(null)
      onRemoved()
    } catch (err: unknown) {
      const detail = (err as ApiError)?.response?.data?.detail || (err as ApiError)?.message
      toast.error(detail || 'Could not remove that')
    } finally {
      setRemoving(false)
    }
  }

  return (
    <div className="flex flex-col gap-1.5">
      <p className="text-xs uppercase tracking-wide text-gray-400 font-semibold">
        Recorded relations
      </p>
      {mine.map((edge) => {
        // One directed row per relation: the sentence reads from the edge's
        // own subject, whichever end this card is.
        const subject = nameOf.get(edge.from_id) ?? '?'
        const object = nameOf.get(edge.to_id) ?? '?'
        const label = edge.label_en || edge.relation_type.replace('_', ' ')
        return (
          <div
            key={edge.id}
            className="flex items-center justify-between gap-2 px-3 py-2 rounded-lg border border-white/8 bg-surface-800/40"
          >
            <span className="text-xs text-gray-300">
              <span dir="auto" className="text-white font-medium">{subject}</span>
              {` is the ${label} of `}
              <span dir="auto" className="text-white font-medium">{object}</span>
              <span className="text-gray-500">
                {edge.source_segment_id ? ' · from a recording' : ' · added by hand'}
              </span>
            </span>
            <button
              type="button"
              disabled={removing}
              onClick={() =>
                confirming === edge.id ? remove(edge) : setConfirming(edge.id)
              }
              className={`shrink-0 px-2 py-1 rounded-md border text-[11px] transition-colors ${
                confirming === edge.id
                  ? 'border-red-400 bg-red-500/15 text-red-200'
                  : 'border-white/10 text-gray-400 hover:border-white/25 hover:text-white'
              }`}
            >
              {confirming === edge.id ? 'Remove — sure?' : 'Remove'}
            </button>
          </div>
        )
      })}
    </div>
  )
}

function MomentsModal({
  person,
  people,
  relationTypes,
  parentsOf,
  edges,
  moments,
  loading,
  onClose,
  onSaved,
}: {
  person: TreePerson
  people: TreePerson[]
  relationTypes: { value: string; label: string }[]
  parentsOf: Map<string, TreePerson[]>
  edges: TreeEdge[]
  moments: EntityMoment[] | null
  loading: boolean
  onClose: () => void
  onSaved: () => void
}) {
  const closeRef = useRef<HTMLButtonElement>(null)
  const [mounted, setMounted] = useState(false)

  useEffect(() => setMounted(true), [])

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    closeRef.current?.focus()
    // The page behind must not scroll while a dialog is over it.
    const previous = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    return () => {
      window.removeEventListener('keydown', onKey)
      document.body.style.overflow = previous
    }
  }, [onClose])

  const years = lifespan(person)
  if (!mounted) return null

  return createPortal(
    <div
      className="fixed inset-0 z-[60] flex items-center justify-center bg-surface-950/95 backdrop-blur-md p-4"
      role="dialog"
      aria-modal="true"
      aria-labelledby="moments-title"
      onClick={onClose}
    >
      {/* Clicks inside the card must not reach the backdrop's close handler. */}
      <div
        className="w-full max-w-2xl max-h-[85vh] overflow-y-auto rounded-2xl p-5 animate-scale-in flex flex-col gap-3 bg-surface-800 border border-white/10 shadow-glass"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-start justify-between gap-3 sticky top-0">
          <div className="flex items-center gap-3">
            {/* The card's upload affordance (§9.6): the same circle the tree
                node shows, clickable — one flow shared with the extraction
                panel, and the new photo becomes the face. onSaved refetches
                the tree, so the node's circle updates with it. */}
            <EntityPortrait
              entityId={person.id}
              name={person.name}
              photoUrl={person.photo_url}
              size={44}
              onChanged={onSaved}
            />
            <div>
              <h2 id="moments-title" dir="auto" className="text-lg font-bold text-white">
                {person.name}
              </h2>
              {(years || person.is_self) && (
                <p className="text-xs text-gray-500 mt-0.5">
                  {person.is_self ? 'You' : years}
                </p>
              )}
            </div>
          </div>
          <button
            ref={closeRef}
            onClick={onClose}
            aria-label="Close"
            className="text-gray-500 hover:text-white shrink-0"
          >
            <X size={17} />
          </button>
        </div>

        {/* Offered for ANYONE, not only the unplaced. A relation that is
            wrong is at least as worth fixing as one that is missing, and
            restricting this to floating people would put the fix out of reach
            exactly when the tree looks confidently wrong. */}
        {!person.is_self && (
          <RelationEditor
            person={person}
            people={people}
            types={relationTypes}
            parentsOf={parentsOf}
            onSaved={onSaved}
          />
        )}

        <RelationList
          person={person}
          people={people}
          edges={edges}
          onRemoved={onSaved}
        />


        {loading && <Loader2 size={20} className="animate-spin text-primary-400 self-center" />}

        {moments?.length === 0 && (
          <p className="text-sm text-gray-500">No recordings mention them yet.</p>
        )}

        {moments?.map((moment) => (
          <article key={moment.segment_id} className="flex flex-col gap-1.5">
            {/* The interview question is the title — it is what the recording
                is an answer to. */}
            <h3 dir="auto" className="text-xs text-primary-300 leading-snug">
              {moment.question_asked}
            </h3>
            {moment.video_url && (
              <video
                src={moment.video_url}
                controls
                playsInline
                className="w-full rounded-lg border border-white/10"
              />
            )}
            {moment.summary && (
              <p dir="auto" className="text-xs text-gray-400">{moment.summary}</p>
            )}
            {moment.transcript && (
              <details>
                <summary className="text-[11px] text-gray-500 cursor-pointer">Transcript</summary>
                <p dir="auto" className="text-xs text-gray-400 mt-1 leading-relaxed">
                  {moment.transcript}
                </p>
              </details>
            )}
          </article>
        ))}
      </div>
    </div>,
    document.body
  )
}

export function FamilyTreePanel() {
  const [tree, setTree] = useState<FamilyTree | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const [selected, setSelected] = useState<TreePerson | null>(null)
  const [moments, setMoments] = useState<EntityMoment[] | null>(null)
  const [momentsLoading, setMomentsLoading] = useState(false)
  // The relation vocabulary, fetched once. From the relation_types table, so
  // the picker cannot offer a value the write would refuse.
  const [relationTypes, setRelationTypes] = useState<{ value: string; label: string }[]>([])

  const load = useCallback(async () => {
    setError(null)
    try {
      setTree(await api.getFamilyTree())
    } catch (err: unknown) {
      const detail = (err as ApiError)?.response?.data?.detail || (err as ApiError)?.message
      setError(detail || 'Could not load your family tree')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    load()
  }, [load])

  // Every person the archive knows, placed or not — the picker's options. An
  // unplaced person is exactly who a producer opens this to connect, so
  // leaving them out would omit the common case.
  const everyone: TreePerson[] = tree
    ? [...tree.generations.flatMap((row) => row.people), ...tree.unplaced]
    : []

  // Recorded parents by person, for the side-of-family question — the same
  // list the questionnaire offers. From the edges the tree already carries,
  // so no extra request and no second source of truth.
  const parentsOf = new Map<string, TreePerson[]>()
  if (tree) {
    const byId = new Map(everyone.map((p) => [p.id, p]))
    for (const edge of tree.edges) {
      // One directed row per relation: 'parent' reads "from is the parent of
      // to", 'child' the reverse. Both spell out a parent.
      const [parentId, childId] =
        edge.relation_type === 'parent'
          ? [edge.from_id, edge.to_id]
          : edge.relation_type === 'child'
            ? [edge.to_id, edge.from_id]
            : [null, null]
      if (!parentId || !childId) continue
      const parent = byId.get(parentId)
      if (!parent) continue
      const list = parentsOf.get(childId) ?? []
      if (!list.some((p) => p.id === parent.id)) list.push(parent)
      parentsOf.set(childId, list)
    }
  }

  useEffect(() => {
    api
      .getRelationTypes()
      .then(setRelationTypes)
      // Fail soft: without the vocabulary the editor simply is not offered,
      // which is better than a picker that cannot be submitted.
      .catch(() => setRelationTypes([]))
  }, [])

  const selectPerson = async (person: TreePerson) => {
    setSelected(person)
    setMoments(null)
    setMomentsLoading(true)
    try {
      setMoments(await api.getEntityMoments(person.id))
    } catch {
      setMoments([])
    } finally {
      setMomentsLoading(false)
    }
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center py-24">
        <Loader2 size={26} className="animate-spin text-primary-400" />
      </div>
    )
  }

  if (error || !tree) {
    return (
      <div className="flex items-center justify-center py-24 px-6">
        <div className="max-w-sm text-center flex flex-col items-center gap-4">
          <p className="text-sm text-gray-300">{error || 'Something went wrong'}</p>
          <button onClick={load} className="btn-primary">Try again</button>
        </div>
      </div>
    )
  }

  const hasFamily = tree.generations.some((g) => g.people.some((p) => !p.is_self))

  return (
    <div className="animate-fade-in max-w-7xl mx-auto px-6 pt-6 pb-16">
      <header className="flex items-center gap-2 text-primary-400 mb-4">
        <Network size={16} />
        <span className="text-sm font-medium">Family tree</span>
      </header>

      {/* An archive with no family captured yet is an empty tree, not an
          error — and the empty state is where the producer finds out that
          relations come from recording, which nothing else tells them. */}
      {!hasFamily ? (
        <div className="glass-card p-6 flex flex-col items-center gap-3 text-center max-w-lg mx-auto">
          <UserIcon size={26} className="text-primary-400" />
          <h2 className="text-lg font-bold text-white">Just you, so far</h2>
          <p className="text-sm text-gray-400 max-w-sm">
            Family appears here as you record. When you mention someone and say how
            they&apos;re related — &ldquo;my brother Nir&rdquo; — you&apos;ll be asked to
            confirm it, and they&apos;ll join the tree.
          </p>
        </div>
      ) : (
        <>
          {/* Portrait rather than letterbox: a tree is read down the
              generations, and the page it sits on is a narrow column like the
              rest of the site. Height is what lets several rows show at once;
              panning covers the width. */}
          <div className="h-[calc(100vh-13rem)] min-h-[620px]">
            <FamilyTreeGraph
              tree={tree}
              selectedId={selected?.id ?? null}
              onSelect={selectPerson}
            />
          </div>
          <p className="text-[11px] text-gray-500 mt-2.5 leading-relaxed">
            Lines are recorded parent–child relations; a double line between two people
            is a recorded marriage. People in the same row are the same generation —
            siblings share a row rather than being joined by a line. Drag to move,
            scroll to zoom.
          </p>
        </>
      )}

      <div className="flex flex-col gap-4 mt-6">
        {/* Not an error and not hidden: these are real people the producer
            talked about whose connection was never stated. Placing them in a
            row would draw a family that does not exist. */}
        {tree.unplaced.length > 0 && (
          <section className="glass-card p-4">
            <h2 className="text-xs uppercase tracking-wide text-gray-400 font-semibold mb-1">
              Mentioned, not yet placed
            </h2>
            <p className="text-xs text-gray-500 mb-2.5">
              People from your stories whose family connection hasn&apos;t been recorded.
            </p>
            <div className="flex flex-wrap gap-2">
              {tree.unplaced.map((person) => (
                <PersonChip
                  key={person.id}
                  person={person}
                  selected={selected?.id === person.id}
                  onSelect={selectPerson}
                />
              ))}
            </div>
          </section>
        )}

        {tree.contradictions.length > 0 && (
          <section className="glass-card p-4 border border-amber-500/30">
            <h2 className="flex items-center gap-2 text-xs uppercase tracking-wide text-amber-400 font-semibold mb-2">
              <AlertTriangle size={13} />
              Recordings that disagree
            </h2>
            <p className="text-xs text-gray-400">
              {tree.contradictions.length} relation
              {tree.contradictions.length === 1 ? '' : 's'} couldn&apos;t be drawn without
              moving someone already placed. The tree kept the first version rather than
              guessing which is right.
            </p>
          </section>
        )}
      </div>

      {selected && (
        <MomentsModal
          person={selected}
          people={everyone}
          relationTypes={relationTypes}
          parentsOf={parentsOf}
          edges={tree?.edges ?? []}
          moments={moments}
          loading={momentsLoading}
          onClose={() => setSelected(null)}
          onSaved={load}
        />
      )}
    </div>
  )
}
