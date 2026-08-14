'use client'

import { useCallback, useEffect, useState } from 'react'
import { X, Loader2, Sparkles, Tag, Scissors, FileText, Users, AlertTriangle, HelpCircle, Bell } from 'lucide-react'
import { toast } from 'react-hot-toast'
import { api } from '@/lib/api'
import { EntityConfirmModal } from '@/components/record/EntityConfirmModal'
import { EntityPortrait } from '@/components/media/EntityPortrait'
import type { ApiError, SegmentExtraction } from '@/lib/types'

/**
 * What the system understood from ONE recording, so the producer can catch a
 * mistake — a misheard name, a person who was missed.
 *
 * ON DEMAND ONLY. This screen used to open itself after every recording and
 * hold the producer there — locked, showing a progress bar — until the
 * confirmation questions were ready to hand off to. All of that existed to
 * keep them in place for a wait, and nobody waits any more: a recording
 * processes in the background and anything it raises appears in the bell. So
 * it opens when the producer asks for it and never on its own, which is also
 * what makes it safe to have no lock and no escape hatches — the only person
 * here chose to be here, on a recording they picked.
 *
 * READ-ONLY on purpose. Nothing here edits; correcting an extraction is a
 * separate feature that hasn't been asked for yet.
 *
 * Everything comes from ONE endpoint. The component deliberately does not
 * know where entities are stored.
 */

interface ExtractionModalProps {
  segmentId: string
  title: string
  onClose: () => void
}

/** Opened on a recording that is still being read, results fill in as they
 *  land. Fast enough that the stage readout looks alive. */
const PROGRESS_POLL_MS = 2000

export function ExtractionModal({ segmentId, title, onClose }: ExtractionModalProps) {
  const [data, setData] = useState<SegmentExtraction | null>(null)
  // Relations removed on this screen, hidden immediately rather than after
  // a refetch — the row is gone server-side the moment the call returns.
  const [removed, setRemoved] = useState<Set<string>>(new Set())
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  /**
   * The questions for THIS recording, opened from here (§13.3).
   *
   * The second way in, alongside the notification list, and the one that
   * restores what removing the popup cost: entities are only written once the
   * answers land, so answering from the panel that is already showing this
   * recording is the one place the producer can watch a name they just
   * confirmed appear in it. `awaiting_confirmation` already rides on the
   * extraction payload, so this needed no new field.
   */
  const [showQuestions, setShowQuestions] = useState(false)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      setData(await api.getSegmentExtraction(segmentId))
    } catch (err: unknown) {
      const detail = (err as ApiError)?.response?.data?.detail || (err as ApiError)?.message
      setError(detail || 'Could not load what was extracted')
    } finally {
      setLoading(false)
    }
  }, [segmentId])

  /** Refetch without blanking the panel — `load` flips `loading`, which is
   *  right on first open and wrong for a refresh of something already on
   *  screen. */
  const refetch = useCallback(async () => {
    try {
      setData(await api.getSegmentExtraction(segmentId))
    } catch {
      /* leave what is on screen; it is still the best answer available */
    }
  }, [segmentId])

  useEffect(() => {
    load()
  }, [load])

  /**
   * Poll while the pipeline runs, so results appear as they land.
   *
   * This is not a progress animation over an opaque wait — the pipeline
   * genuinely persists in stages (transcribe commits the transcript,
   * extract_topics commits the tags) and this endpoint reads straight from
   * the database, so each poll returns exactly what exists so far. Entities
   * are the exception and arrive last, because they are only written after
   * the confirmation questions are answered.
   *
   * Refetches quietly — `load` would flip `loading` and blank the panel every
   * two seconds.
   */
  // Still polls, for the one case that survives: a producer who opens this on
  // a recording the pipeline has not finished reading. Results fill in as they
  // land rather than the panel showing a half-empty snapshot until it is
  // reopened. Self-limiting — it stops the moment the run finishes.
  useEffect(() => {
    if (!data?.still_processing) return
    let cancelled = false
    const id = setInterval(async () => {
      try {
        const next = await api.getSegmentExtraction(segmentId)
        if (!cancelled) setData(next)
      } catch {
        /* a dropped poll is not worth showing an error over — the next one
           will either succeed or the producer will close the screen */
      }
    }, PROGRESS_POLL_MS)
    return () => {
      cancelled = true
      clearInterval(id)
    }
  }, [data?.still_processing, segmentId])

  // Escape always closes. There is nothing to hold anyone here for: this
  // panel neither opens itself nor waits on anything, so a read-only screen
  // someone opened out of curiosity should never be harder to leave than to
  // enter. The lock, the 90s stall escape and the failure escape all existed
  // to soften a screen that appeared uninvited and refused to go away.
  //
  // Except while the questions are open on top: that screen answers Escape
  // itself, and both acting on one keypress would dismiss the panel the
  // producer is about to be returned to.
  useEffect(() => {
    if (showQuestions) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose, showQuestions])

  return (
    <>
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 backdrop-blur-sm px-4 py-8 animate-fade-in"
      role="dialog"
      aria-modal="true"
      aria-labelledby="extraction-title"
      onClick={onClose}
    >
      <div
        className="w-full max-w-2xl modal-card flex flex-col max-h-full"
        // The backdrop closes on click; the panel must not, or selecting
        // transcript text would dismiss the thing being read.
        onClick={e => e.stopPropagation()}
      >
        <div className="flex items-start justify-between gap-4 p-6 border-b border-edge">
          <div className="min-w-0">
            <div className="flex items-center gap-2 text-primary-400 mb-1">
              <Sparkles size={16} />
              <span className="text-sm font-semibold">Extracted from this</span>
            </div>
            <h2 id="extraction-title" className="text-lg font-bold text-ink truncate">
              {title}
            </h2>
          </div>
          <button onClick={onClose} className="btn-icon shrink-0" aria-label="Close">
            <X size={16} />
          </button>
        </div>

        <div className="p-6 overflow-y-auto messages-scroll flex flex-col gap-6">
          {loading && (
            <div className="flex items-center justify-center gap-2 py-12 text-muted">
              <Loader2 size={20} className="animate-spin" />
              <span className="text-sm">Loading…</span>
            </div>
          )}

          {error && !loading && (
            <div className="flex flex-col items-center gap-4 py-10 text-center">
              <p className="text-sm text-ink-soft">{error}</p>
              <button onClick={load} className="btn-secondary">Try again</button>
            </div>
          )}

          {data && !loading && !error && (
            <>
              {/* Paused on a person, not still working — and the producer
                  came looking, so this is the moment to say where the
                  questions are. Kept for exactly that reason: a badge
                  appearing in the corner explains itself to nobody. */}
              {data.awaiting_confirmation && (
                <div className="flex items-start gap-3 px-4 py-3 rounded-xl bg-primary-500/10 border border-primary-500/30 text-primary-200 text-sm">
                  <HelpCircle size={16} className="shrink-0 mt-0.5" />
                  <div className="flex flex-col items-start gap-2.5 min-w-0">
                    <span>
                      A few questions are ready about this recording — answering them is
                      what saves the people and relations it found. They&apos;re also
                      waiting under the <Bell size={13} className="inline -mt-0.5" /> at
                      the top of the screen.
                    </span>
                    <button
                      type="button"
                      onClick={() => setShowQuestions(true)}
                      className="btn-primary py-1.5 text-xs"
                    >
                      Show questions
                    </button>
                  </div>
                </div>
              )}

              {/* The pipeline stopped without finishing. Nothing surfaced this
                  before — `failed` only ever unlocked the modal, which was
                  useful to someone trapped here and told them nothing. Opened
                  deliberately, the sections below would otherwise read as
                  "nothing was found in your recording", which is a different
                  and much worse claim than "we could not read it". */}
              {data.status === 'failed' && (
                <div className="flex items-center gap-3 px-4 py-3 rounded-xl bg-amber-500/10 border border-amber-500/30 text-amber-200 text-sm">
                  <AlertTriangle size={16} className="shrink-0" />
                  <span>
                    Something went wrong reading this recording, so what&apos;s below
                    may be incomplete. The video itself is safe — re-recording this
                    answer is the way to try again.
                  </span>
                </div>
              )}

              {data.still_processing && (
                // Kept, in reduced form. Nobody is held here waiting any more,
                // but someone who opens this ON a recording still being read
                // needs to know that is why it looks thin — an empty entity
                // list otherwise reads as a finished, disappointing result.
                // A real bar rather than a spinner: the percentages are
                // weighted by measured stage duration, so it does not sprint
                // to 60% and then appear to hang.
                <div className="flex flex-col gap-2 px-4 py-3 rounded-xl bg-surface-800/60 border border-edge">
                  <div className="flex items-center justify-between gap-3 text-sm text-ink">
                    <span>{data.progress_label ?? 'Reading your recording'}</span>
                    <span className="text-primary-300 tabular-nums">
                      {data.progress_percent ?? 0}%
                    </span>
                  </div>
                  <div
                    className="h-2 rounded-full bg-surface-700 overflow-hidden"
                    role="progressbar"
                    aria-valuenow={data.progress_percent ?? 0}
                    aria-valuemin={0}
                    aria-valuemax={100}
                  >
                    <div
                      className="h-full bg-primary-500 transition-all duration-500"
                      style={{ width: `${data.progress_percent ?? 0}%` }}
                    />
                  </div>
                  {/* No instruction to stay. Closing this changes nothing —
                      the recording is being read on the server either way. */}
                  <p className="text-xs text-muted">
                    You can close this and carry on; anything that needs checking
                    will be waiting under the bell.
                  </p>
                </div>
              )}

              <Section icon={<Users size={15} />} label="People, places and things found">
                {data.entities_unavailable ? (
                  <div className="flex items-center gap-2 text-sm text-amber-300">
                    <AlertTriangle size={14} />
                    Couldn&apos;t reach the entity store, so this list isn&apos;t available.
                  </div>
                ) : data.entities.length === 0 ? (
                  <p className="text-sm text-muted2">
                    Nothing named was picked up.{' '}
                    <span className="text-muted2">
                      This is often correct — only people and places called by NAME are
                      extracted, so &ldquo;my wife&rdquo; or &ldquo;my commander&rdquo; won&apos;t
                      appear here. The interview question is what identifies them instead.
                    </span>
                  </p>
                ) : (
                  <ul className="flex flex-col gap-2">
                    {data.entities.map(e => {
                      // Relations are listed UNDER the person they are about
                      // rather than in a section of their own. Two lists meant
                      // the same names twice, and a relation belongs to its
                      // subject more than it belongs to a list of relations.
                      const theirs = (data.relations ?? []).filter(
                        r => r.from_name === e.name && !removed.has(r.id),
                      )
                      return (
                      <li
                        key={e.name}
                        className="px-4 py-3 rounded-xl bg-surface-700/50 border border-edge"
                      >
                        <div className="flex items-center gap-3">
                          {/* The portrait, clickable to add or change the
                              photo (MEDIA_GALLERY.md §3.3) — the same
                              control the family tree's card uses. This
                              panel stays read-only about what was SAID;
                              a photo is producer-added context, not an
                              edit of the extraction. */}
                          {e.entity_id && (
                            <EntityPortrait
                              entityId={e.entity_id}
                              name={e.name}
                              photoUrl={e.photo_url}
                              size={36}
                              onChanged={refetch}
                            />
                          )}
                          <div className="flex items-baseline gap-2">
                            <span dir="auto" className="text-sm text-ink font-medium">{e.name}</span>
                            {e.kind && (
                              <span className="text-[11px] text-muted2">{e.kind}</span>
                            )}
                          </div>
                        </div>
                        {e.summary && (
                          <p dir="auto" className="text-xs text-muted mt-1">{e.summary}</p>
                        )}
                        {theirs.length > 0 && (
                          <ul className="flex flex-col gap-1 mt-2">
                            {theirs.map(relation => (
                              <li
                                key={relation.id}
                                className="flex items-center justify-between gap-3"
                              >
                                <span dir="auto" className="text-xs text-ink-soft">
                                  {relation.label ?? relation.relation_type} of {relation.to_name}
                                  {relation.origin === 'confirmation' && (
                                    <span className="text-muted2"> (you answered this)</span>
                                  )}
                                </span>
                                <button
                                  type="button"
                                  onClick={async () => {
                                    try {
                                      await api.deleteRelation(relation.id)
                                      setRemoved(current => new Set(current).add(relation.id))
                                      toast.success('Removed')
                                    } catch {
                                      toast.error('Could not remove that — please try again')
                                    }
                                  }}
                                  className="text-[11px] text-muted2 hover:text-red-300 shrink-0"
                                >
                                  Remove
                                </button>
                              </li>
                            ))}
                          </ul>
                        )}
                      </li>
                      )
                    })}
                  </ul>
                )}
              </Section>

              <Section icon={<Tag size={15} />} label="Topic tags">
                {data.topic_tags.length === 0 ? (
                  <p className="text-sm text-muted2">No topics were tagged.</p>
                ) : (
                  <div className="flex flex-wrap gap-2">
                    {data.topic_tags.map(tag => (
                      <span key={tag} className="badge-purple">{tag}</span>
                    ))}
                  </div>
                )}
              </Section>

              <Section icon={<Scissors size={15} />} label="Split into">
                <p className="text-sm text-ink-soft">
                  <span className="text-ink font-semibold">{data.unit_count}</span>{' '}
                  {data.unit_count === 1 ? 'utterance unit' : 'utterance units'}
                </p>
                <p className="text-xs text-muted2 mt-1 leading-relaxed">
                  Answers are built from whole units, cut at the pauses you actually took —
                  so a clip can never stop mid-sentence.
                </p>
              </Section>

              <Section icon={<FileText size={15} />} label="Transcript">
                {data.transcript ? (
                  <p className="text-sm text-ink-soft leading-relaxed whitespace-pre-wrap">
                    {data.transcript}
                  </p>
                ) : (
                  <p className="text-sm text-muted2">No transcript yet.</p>
                )}
              </Section>
            </>
          )}
        </div>
      </div>
    </div>

    {/* A SIBLING of the panel, not a child of it. Nested inside, every click
        in the questions screen would bubble to the backdrop above and close
        the panel underneath — the one the producer is about to be returned
        to. Answering here is where the loop closes: the entities those
        answers wrote appear in the list behind, on a refetch rather than a
        reopen. */}
    {showQuestions && (
      <EntityConfirmModal
        segmentId={segmentId}
        onClose={() => {
          setShowQuestions(false)
          refetch()
        }}
      />
    )}
    </>
  )
}

function Section({
  icon,
  label,
  children,
}: {
  icon: React.ReactNode
  label: string
  children: React.ReactNode
}) {
  return (
    <section className="flex flex-col gap-2">
      <div className="flex items-center gap-2 text-muted">
        {icon}
        <h3 className="text-xs uppercase tracking-wide font-semibold">{label}</h3>
      </div>
      {children}
    </section>
  )
}
