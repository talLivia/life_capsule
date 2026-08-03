'use client'

import { useCallback, useEffect, useState } from 'react'
import { AlertTriangle, HelpCircle, Loader2, Network, User as UserIcon, X } from 'lucide-react'
import { FamilyTreeGraph } from '@/components/FamilyTreeGraph'
import { api } from '@/lib/api'
import type { ApiError, EntityMoment, FamilyTree, TreePerson } from '@/lib/types'

/**
 * The producer's family tree — read-only.
 *
 * Everything shown here is decided by the server (app/services/family_tree.py):
 * which row someone sits in, who could not be placed, and which recordings
 * disagree. This file lays it out and does not recompute any of it.
 *
 * THE TREE NEVER GUESSES, and that shows up in the UI as three deliberate
 * things rather than as tidier output:
 *
 *   * people with no family path to the producer get their own section
 *     instead of being dropped or parked in the middle row;
 *   * a disagreement between recordings is shown, not resolved silently;
 *   * an empty tree says so and explains where relations come from, rather
 *     than rendering a blank canvas that looks broken.
 */

// Must match NODE_H + ROW_GAP in FamilyTreeGraph, or the row labels drift out
// of step with the rows they name.
const GENERATION_ROW_HEIGHT = 58 + 104

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
          : person.is_self
            ? 'border-primary-500/40 bg-surface-800/70 hover:border-primary-400'
            : 'border-white/10 bg-surface-800/40 hover:border-white/25'
      }`}
    >
      <span dir="auto" className="block text-sm font-medium text-white">
        {person.name}
      </span>
      <span className="block text-[11px] text-gray-500 mt-0.5">
        {person.is_self ? 'You' : years || 'Tap to see their moments'}
      </span>
    </button>
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
    <div className="animate-fade-in max-w-6xl mx-auto px-6 pt-10 pb-16">
      <header className="flex items-center gap-2 text-primary-400 mb-6">
        <Network size={16} />
        <span className="text-sm font-medium">Family tree</span>
      </header>

      <div className="grid grid-cols-1 lg:grid-cols-5 gap-6 items-start">
        <div className="lg:col-span-3 flex flex-col gap-4">
          {/* An archive with no family captured yet is an empty tree, not an
              error — and the empty state is where the producer finds out that
              relations come from recording, which nothing else tells them. */}
          {!hasFamily ? (
            <div className="glass-card p-6 flex flex-col items-center gap-3 text-center">
              <UserIcon size={26} className="text-primary-400" />
              <h2 className="text-lg font-bold text-white">Just you, so far</h2>
              <p className="text-sm text-gray-400 max-w-sm">
                Family appears here as you record. When you mention someone and say how
                they&apos;re related — &ldquo;my brother Nir&rdquo; — you&apos;ll be asked to
                confirm it, and they&apos;ll join the tree.
              </p>
            </div>
          ) : (
            <section className="glass-card p-4">
              {/* Row labels live beside the chart rather than as headings over
                  card groups — the drawn lines are what carry the structure
                  now, and repeating it as headers would say it twice. */}
              <div className="flex items-start gap-3">
                <ol className="flex flex-col shrink-0 pt-[20px]" aria-hidden>
                  {tree.generations.map((row) => (
                    <li
                      key={row.generation}
                      style={{ height: GENERATION_ROW_HEIGHT }}
                      className="text-[10px] uppercase tracking-wide text-gray-500 whitespace-nowrap"
                    >
                      {generationLabel(row.generation)}
                    </li>
                  ))}
                </ol>
                <FamilyTreeGraph
                  tree={tree}
                  selectedId={selected?.id ?? null}
                  onSelect={selectPerson}
                />
              </div>
              <p className="text-[11px] text-gray-500 mt-3 leading-relaxed">
                Solid lines are recorded parent–child relations; dashed lines are
                siblings and partners. Only relations you&apos;ve confirmed are
                drawn — siblings connect to you rather than up to your parents,
                because that&apos;s what your recordings actually say.
              </p>
            </section>
          )}

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

        {/* ── The moments behind one person ──────────────────────────── */}
        <aside className="lg:col-span-2 lg:sticky lg:top-6">
          {!selected ? (
            <div className="glass-card p-5 flex flex-col items-center gap-2 text-center">
              <HelpCircle size={18} className="text-gray-500" />
              <p className="text-xs text-gray-500">
                Choose someone to see the moments they appear in.
              </p>
            </div>
          ) : (
            <div className="glass-card p-4 flex flex-col gap-3">
              <div className="flex items-start justify-between gap-2">
                <h2 dir="auto" className="text-base font-bold text-white">{selected.name}</h2>
                <button
                  onClick={() => setSelected(null)}
                  aria-label="Close"
                  className="text-gray-500 hover:text-white"
                >
                  <X size={15} />
                </button>
              </div>

              {momentsLoading && (
                <Loader2 size={18} className="animate-spin text-primary-400 self-center" />
              )}

              {moments?.length === 0 && (
                <p className="text-xs text-gray-500">
                  No recordings mention them yet.
                </p>
              )}

              {moments?.map((moment) => (
                <article key={moment.segment_id} className="flex flex-col gap-1.5">
                  {/* The interview question is the title — it is what the
                      recording is an answer to. */}
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
                      <summary className="text-[11px] text-gray-500 cursor-pointer">
                        Transcript
                      </summary>
                      <p dir="auto" className="text-xs text-gray-400 mt-1 leading-relaxed">
                        {moment.transcript}
                      </p>
                    </details>
                  )}
                </article>
              ))}
            </div>
          )}
        </aside>
      </div>
    </div>
  )
}
