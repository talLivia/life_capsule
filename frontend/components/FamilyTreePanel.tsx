'use client'

import { useCallback, useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { AlertTriangle, Loader2, Network, User as UserIcon, X } from 'lucide-react'
import { FamilyTreeGraph } from '@/components/FamilyTreeGraph'
import { api } from '@/lib/api'
import type { ApiError, EntityMoment, FamilyTree, TreePerson } from '@/lib/types'

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
function MomentsModal({
  person,
  moments,
  loading,
  onClose,
}: {
  person: TreePerson
  moments: EntityMoment[] | null
  loading: boolean
  onClose: () => void
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
          <button
            ref={closeRef}
            onClick={onClose}
            aria-label="Close"
            className="text-gray-500 hover:text-white shrink-0"
          >
            <X size={17} />
          </button>
        </div>

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
            Lines are recorded parent–child relations. People in the same row are the
            same generation — siblings and partners share a row rather than being joined
            by a line, so a recorded marriage shows as two people side by side. Drag to
            move, scroll to zoom.
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
          moments={moments}
          loading={momentsLoading}
          onClose={() => setSelected(null)}
        />
      )}
    </div>
  )
}
