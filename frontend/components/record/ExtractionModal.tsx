'use client'

import { useCallback, useEffect, useState } from 'react'
import { X, Loader2, Sparkles, Tag, Scissors, FileText, Users, AlertTriangle } from 'lucide-react'
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
}

export function ExtractionModal({ segmentId, title, onClose }: ExtractionModalProps) {
  const [data, setData] = useState<SegmentExtraction | null>(null)
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

  // Escape closes. A read-only panel should never trap someone who opened it
  // out of curiosity.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 backdrop-blur-sm px-4 py-8 animate-fade-in"
      role="dialog"
      aria-modal="true"
      aria-labelledby="extraction-title"
      onClick={onClose}
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
          <button onClick={onClose} className="btn-icon shrink-0" aria-label="Close">
            <X size={16} />
          </button>
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
              {data.still_processing && (
                // "We haven't looked yet" and "we found nothing" look
                // identical otherwise, and they mean opposite things.
                <div className="flex items-center gap-3 px-4 py-3 rounded-xl bg-amber-500/10 border border-amber-500/30 text-amber-300 text-sm">
                  <Loader2 size={16} className="animate-spin shrink-0" />
                  Still being processed — what&apos;s below may be incomplete.
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
                    {data.entities.map(e => (
                      <li
                        key={e.name}
                        className="px-4 py-3 rounded-xl bg-surface-700/50 border border-white/10"
                      >
                        <p className="text-sm font-medium text-white">{e.name}</p>
                        {e.summary && (
                          // The summary is where a wrong-but-plausible
                          // extraction shows itself — not that a name was
                          // picked up, but what it was taken to MEAN.
                          <p className="text-xs text-gray-400 mt-1 leading-relaxed">{e.summary}</p>
                        )}
                      </li>
                    ))}
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
