'use client'

import { useCallback, useEffect, useState } from 'react'
import { X, Loader2, Sparkles, Tag, Scissors, FileText, Users, AlertTriangle, HelpCircle } from 'lucide-react'
import { toast } from 'react-hot-toast'
import { api } from '@/lib/api'
import type { ApiError, SegmentExtraction } from '@/lib/types'

/**
 * What the system understood from ONE recording, so the producer can catch a
 * mistake — a misheard name, a person who was missed — while they still
 * remember the recording, instead of meeting it later as a bad answer.
 *
 * READ-ONLY on purpose. Nothing here edits; correcting an extraction is a
 * separate feature that hasn't been asked for yet.
 *
 * Everything comes from ONE endpoint. The component deliberately does not
 * know that entities currently live in Graphiti and topic tags in Postgres —
 * entities are moving to Postgres, and that migration should not reach the
 * UI at all.
 */

interface ExtractionModalProps {
  segmentId: string
  title: string
  onClose: () => void
  /** Opened by the recorder rather than by the producer clicking. Turns on
   *  polling, the progress readout, and closing itself when there is nothing
   *  left to show. Off for the manual button, which reviews a finished
   *  recording and should just sit there. */
  live?: boolean
  /** The pipeline has paused for answers. The extraction screen gets out of
   *  the way so the confirmation popup has the screen to itself — see
   *  "the screens do not stack" in docs/FAMILY_TREE_TIMELINE.md 8.2. */
  onNeedsConfirmation?: () => void
}

/** Fast enough that the stage readout looks alive. Only while live. */
const PROGRESS_POLL_MS = 2000
/** Long enough for the finished state to register as a result rather than a
 *  flash, short enough not to be a step the producer has to sit through. */
const DONE_DWELL_MS = 1600
/** After this long with no progress the lock releases. A modal that can
 *  trap someone forever is worse than one they can leave — but the escape
 *  is for a pipeline that has genuinely stopped, not a general dismiss. */
const STUCK_AFTER_MS = 90_000

export function ExtractionModal({
  segmentId,
  title,
  onClose,
  live = false,
  onNeedsConfirmation,
}: ExtractionModalProps) {
  const [data, setData] = useState<SegmentExtraction | null>(null)
  // Set once the run has gone quiet for too long, or has failed. Until
  // then a live screen cannot be closed: the whole point is that questions
  // arrive attached to the recording they are about, rather than ambushing
  // the producer later against a recording they have moved past.
  const [escapable, setEscapable] = useState(false)
  // Relations removed on this screen, hidden immediately rather than after
  // a refetch — the row is gone server-side the moment the call returns.
  const [removed, setRemoved] = useState<Set<string>>(new Set())
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

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
  // Polls however the screen was opened. A recording still being processed
  // is still being processed whether the producer arrived automatically or
  // by clicking, and a panel that silently stops updating is the thing that
  // made questions appear to come out of nowhere later.
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

  // Release the lock if the pipeline stops reporting progress, or fails.
  useEffect(() => {
    if (data?.status === 'failed') {
      setEscapable(true)
      return
    }
    if (!data?.still_processing) return
    // Restarted on every stage change, so a slow-but-moving run never
    // unlocks — only one that has actually stalled.
    const id = setTimeout(() => setEscapable(true), STUCK_AFTER_MS)
    return () => clearTimeout(id)
  }, [data?.still_processing, data?.status, data?.progress_stage])

  // Hand over to the confirmation popup, or close when there is nothing left
  // to say. Only when opened by the recorder: the manual button reviews a
  // finished recording and must not vanish while it is being read.
  useEffect(() => {
    if (!data) return
    // Hand off however the screen was opened. Leaving it up with the
    // questions rendered UNDERNEATH it — which is what happened — means the
    // producer has to close a panel to discover that anything is waiting.
    if (data.awaiting_confirmation || data.status === 'pending_confirmation') {
      onNeedsConfirmation?.()
      return
    }
    // Only a screen that opened ITSELF closes itself. One opened on purpose
    // to read a finished recording must stay until it is dismissed.
    if (!live || data.still_processing) return
    const id = setTimeout(onClose, DONE_DWELL_MS)
    return () => clearTimeout(id)
  }, [live, data, onClose, onNeedsConfirmation])

  // Escape closes. A read-only panel should never trap someone who opened it
  // out of curiosity.
  // Locked whenever real work is in flight, however the screen was opened.
  // The point is that nobody wanders off mid-processing and meets the
  // questions later attached to a recording they have moved past.
  const locked = !escapable && (data?.still_processing ?? false)

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !locked) onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose, locked])

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 backdrop-blur-sm px-4 py-8 animate-fade-in"
      role="dialog"
      aria-modal="true"
      aria-labelledby="extraction-title"
      onClick={locked ? undefined : onClose}
    >
      <div
        className="w-full max-w-2xl glass-card flex flex-col max-h-full"
        // The backdrop closes on click; the panel must not, or selecting
        // transcript text would dismiss the thing being read.
        onClick={e => e.stopPropagation()}
      >
        <div className="flex items-start justify-between gap-4 p-6 border-b border-white/10">
          <div className="min-w-0">
            <div className="flex items-center gap-2 text-primary-400 mb-1">
              <Sparkles size={16} />
              <span className="text-sm font-semibold">Extracted from this</span>
            </div>
            <h2 id="extraction-title" className="text-lg font-bold text-white truncate">
              {title}
            </h2>
          </div>
          {locked ? (
            <span className="text-[11px] text-gray-500 shrink-0 max-w-[9rem] text-right leading-snug">
              Please wait — any questions will appear here
            </span>
          ) : (
            <button onClick={onClose} className="btn-icon shrink-0" aria-label="Close">
              <X size={16} />
            </button>
          )}
        </div>

        <div className="p-6 overflow-y-auto messages-scroll flex flex-col gap-6">
          {loading && (
            <div className="flex items-center justify-center gap-2 py-12 text-gray-400">
              <Loader2 size={20} className="animate-spin" />
              <span className="text-sm">Loading…</span>
            </div>
          )}

          {error && !loading && (
            <div className="flex flex-col items-center gap-4 py-10 text-center">
              <p className="text-sm text-gray-300">{error}</p>
              <button onClick={load} className="btn-secondary">Try again</button>
            </div>
          )}

          {data && !loading && !error && (
            <>
              {/* Paused on a person, not still working. The manual panel
                  reaches this too — it has no handoff, so without its own
                  words it showed the "hang on" message forever. */}
              {data.awaiting_confirmation && (
                <div className="flex items-center gap-3 px-4 py-3 rounded-xl bg-primary-500/10 border border-primary-500/30 text-primary-200 text-sm">
                  <HelpCircle size={16} className="shrink-0" />
                  <span>
                    A few questions are ready about this recording — answering them
                    is what saves the people and relations it found.
                  </span>
                </div>
              )}

              {data.still_processing && (
                // A real bar, not a spinner and not a vague reassurance. The
                // percentages are weighted by measured stage duration, so it
                // does not sprint to 60% and then appear to hang.
                <div className="flex flex-col gap-2 px-4 py-3 rounded-xl bg-surface-800/60 border border-white/10">
                  <div className="flex items-center justify-between gap-3 text-sm text-white">
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
                  <p className="text-xs text-gray-400">
                    {escapable
                      ? 'This is taking longer than expected — you can close this and carry on; the questions will still find you.'
                      : 'Stay here — if anything needs checking, the questions open next.'}
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
                  <p className="text-sm text-gray-500">
                    Nothing named was picked up.{' '}
                    <span className="text-gray-600">
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
                        className="px-4 py-3 rounded-xl bg-surface-700/50 border border-white/10"
                      >
                        <div className="flex items-baseline gap-2">
                          <span dir="auto" className="text-sm text-white font-medium">{e.name}</span>
                          {e.kind && (
                            <span className="text-[11px] text-gray-500">{e.kind}</span>
                          )}
                        </div>
                        {e.summary && (
                          <p dir="auto" className="text-xs text-gray-400 mt-1">{e.summary}</p>
                        )}
                        {theirs.length > 0 && (
                          <ul className="flex flex-col gap-1 mt-2">
                            {theirs.map(relation => (
                              <li
                                key={relation.id}
                                className="flex items-center justify-between gap-3"
                              >
                                <span dir="auto" className="text-xs text-gray-300">
                                  {relation.label ?? relation.relation_type} of {relation.to_name}
                                  {relation.origin === 'confirmation' && (
                                    <span className="text-gray-500"> (you answered this)</span>
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
                                  className="text-[11px] text-gray-500 hover:text-red-300 shrink-0"
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
                  <p className="text-sm text-gray-500">No topics were tagged.</p>
                ) : (
                  <div className="flex flex-wrap gap-2">
                    {data.topic_tags.map(tag => (
                      <span key={tag} className="badge-purple">{tag}</span>
                    ))}
                  </div>
                )}
              </Section>

              <Section icon={<Scissors size={15} />} label="Split into">
                <p className="text-sm text-gray-300">
                  <span className="text-white font-semibold">{data.unit_count}</span>{' '}
                  {data.unit_count === 1 ? 'utterance unit' : 'utterance units'}
                </p>
                <p className="text-xs text-gray-500 mt-1 leading-relaxed">
                  Answers are built from whole units, cut at the pauses you actually took —
                  so a clip can never stop mid-sentence.
                </p>
              </Section>

              <Section icon={<FileText size={15} />} label="Transcript">
                {data.transcript ? (
                  <p className="text-sm text-gray-300 leading-relaxed whitespace-pre-wrap">
                    {data.transcript}
                  </p>
                ) : (
                  <p className="text-sm text-gray-500">No transcript yet.</p>
                )}
              </Section>
            </>
          )}
        </div>
      </div>
    </div>
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
      <div className="flex items-center gap-2 text-gray-400">
        {icon}
        <h3 className="text-xs uppercase tracking-wide font-semibold">{label}</h3>
      </div>
      {children}
    </section>
  )
}
