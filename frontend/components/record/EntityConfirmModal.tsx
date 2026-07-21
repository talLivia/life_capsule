'use client'

import { useEffect, useRef, useState } from 'react'
import { HelpCircle, Loader2, UserPlus, Check } from 'lucide-react'
import { toast } from 'react-hot-toast'
import { api } from '@/lib/api'
import type { ApiError, PendingConfirmation } from '@/lib/types'

const POLL_INTERVAL_MS = 8000

/**
 * Surfaces Prompt 5's human_confirm question between interview steps, by
 * polling GET /segments/pending-confirmations. Analysis runs in the
 * background (Celery), so this has nothing to show most of the time — it
 * only pops up when the pipeline actually pauses on an ambiguous name.
 *
 * A single candidate renders as a plain yes/no ("Is 'Gila' the same Gila
 * from your military service story?"). Two or more candidates (e.g. a bare
 * "Moshe" that could be either "Moshe Cohen" or "Moshe Levi" already in the
 * archive) render as a picker instead — asking the storyteller to name
 * which one (or that it's someone new) rather than guessing and asking a
 * yes/no about an arbitrary single match.
 */
export function EntityConfirmModal() {
  const [pending, setPending] = useState<PendingConfirmation | null>(null)
  const [selectedUuid, setSelectedUuid] = useState<string | null>(null)
  const answeringRef = useRef(false)
  const [answering, setAnswering] = useState(false)

  useEffect(() => {
    let cancelled = false

    const poll = async () => {
      if (answeringRef.current) return
      try {
        const list: PendingConfirmation[] = await api.getPendingConfirmations()
        if (!cancelled) setPending(list[0] ?? null)
      } catch {
        /* transient network errors are fine to ignore on a background poll */
      }
    }

    poll()
    const id = setInterval(poll, POLL_INTERVAL_MS)
    return () => {
      cancelled = true
      clearInterval(id)
    }
  }, [])

  // Reset the picker selection whenever a new question appears.
  useEffect(() => {
    setSelectedUuid(null)
  }, [pending?.segment_id, pending?.pending_confirmation.entity_name])

  const submit = async (sameAsExisting: boolean, candidateUuid?: string) => {
    if (!pending) return
    answeringRef.current = true
    setAnswering(true)
    try {
      await api.confirmEntity(pending.segment_id, {
        entity_name: pending.pending_confirmation.entity_name,
        same_as_existing: sameAsExisting,
        candidate_uuid: candidateUuid,
      })
      setPending(null)
      // Pick up the next pending question (if this segment had more than one).
      try {
        const list: PendingConfirmation[] = await api.getPendingConfirmations()
        setPending(list[0] ?? null)
      } catch {
        /* next poll tick will catch it */
      }
    } catch (err: unknown) {
      const detail = (err as ApiError)?.response?.data?.detail || (err as ApiError)?.message
      toast.error(detail || 'Could not save your answer — please try again')
    } finally {
      answeringRef.current = false
      setAnswering(false)
    }
  }

  if (!pending) return null

  const { question, candidates, entity_name } = pending.pending_confirmation
  const isSingleCandidate = candidates.length === 1

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-sm px-4"
      role="dialog"
      aria-modal="true"
      aria-labelledby="entity-confirm-question"
    >
      <div className="w-full max-w-md rounded-2xl bg-calm-card dark:bg-calm-cardDark border border-calm-border dark:border-calm-borderDark p-6 flex flex-col gap-4 shadow-xl">
        <div className="flex items-center gap-2 text-calm-sage-600 dark:text-calm-sage-300">
          <HelpCircle size={18} />
          <span className="text-sm font-semibold">Quick check</span>
        </div>
        <p id="entity-confirm-question" className="text-calm-ink dark:text-calm-inkDark text-base leading-relaxed">
          {question}
        </p>

        {isSingleCandidate ? (
          <>
            {candidates[0].summary && (
              <p className="text-sm text-calm-inkmuted dark:text-calm-inkmutedDark italic">
                &ldquo;{candidates[0].summary}&rdquo;
              </p>
            )}
            <div className="flex items-center justify-end gap-3 pt-2">
              <button
                onClick={() => submit(false)}
                disabled={answering}
                className="calm-btn-secondary"
              >
                No, different
              </button>
              <button
                onClick={() => submit(true, candidates[0].uuid)}
                disabled={answering}
                className="calm-btn-primary"
              >
                {answering ? <Loader2 size={16} className="animate-spin" /> : 'Yes, same'}
              </button>
            </div>
          </>
        ) : (
          <>
            <div className="flex flex-col gap-2">
              {candidates.map((c) => {
                const isSelected = selectedUuid === c.uuid
                return (
                  <button
                    key={c.uuid}
                    onClick={() => setSelectedUuid(c.uuid)}
                    disabled={answering}
                    className={`flex items-start gap-3 text-left px-4 py-3 rounded-xl border transition-all duration-150
                      ${isSelected
                        ? 'border-calm-sage-500 bg-calm-sage-50 dark:bg-white/10'
                        : 'border-calm-border dark:border-calm-borderDark hover:border-calm-sage-300'
                      }`}
                  >
                    <span
                      className={`mt-0.5 w-4 h-4 rounded-full border flex items-center justify-center flex-shrink-0
                        ${isSelected ? 'border-calm-sage-600 bg-calm-sage-600' : 'border-calm-inkmuted dark:border-calm-inkmutedDark'}`}
                    >
                      {isSelected && <Check size={11} className="text-white" />}
                    </span>
                    <span>
                      <span className="block text-sm font-medium text-calm-ink dark:text-calm-inkDark">
                        {c.name}
                      </span>
                      {c.summary && (
                        <span className="block text-xs text-calm-inkmuted dark:text-calm-inkmutedDark mt-0.5">
                          {c.summary}
                        </span>
                      )}
                    </span>
                  </button>
                )
              })}

              <button
                onClick={() => setSelectedUuid('__new__')}
                disabled={answering}
                className={`flex items-center gap-3 text-left px-4 py-3 rounded-xl border transition-all duration-150
                  ${selectedUuid === '__new__'
                    ? 'border-calm-sage-500 bg-calm-sage-50 dark:bg-white/10'
                    : 'border-calm-border dark:border-calm-borderDark hover:border-calm-sage-300'
                  }`}
              >
                <span
                  className={`w-4 h-4 rounded-full border flex items-center justify-center flex-shrink-0
                    ${selectedUuid === '__new__' ? 'border-calm-sage-600 bg-calm-sage-600' : 'border-calm-inkmuted dark:border-calm-inkmutedDark'}`}
                >
                  {selectedUuid === '__new__' && <Check size={11} className="text-white" />}
                </span>
                <span className="flex items-center gap-1.5 text-sm font-medium text-calm-ink dark:text-calm-inkDark">
                  <UserPlus size={14} />
                  Someone new, not listed above
                </span>
              </button>
            </div>

            <div className="flex justify-end pt-2">
              <button
                onClick={() =>
                  selectedUuid === '__new__'
                    ? submit(false)
                    : selectedUuid
                      ? submit(true, selectedUuid)
                      : undefined
                }
                disabled={answering || !selectedUuid}
                className="calm-btn-primary"
              >
                {answering ? <Loader2 size={16} className="animate-spin" /> : `Confirm "${entity_name}"`}
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  )
}
