'use client'

import { useEffect, useRef, useState } from 'react'
import { HelpCircle, Loader2 } from 'lucide-react'
import { toast } from 'react-hot-toast'
import { api } from '@/lib/api'
import type { ApiError, PendingConfirmation } from '@/lib/types'

const POLL_INTERVAL_MS = 8000

/**
 * Surfaces Prompt 5's human_confirm question ("Is 'Gila' the same Gila from
 * your military service story?") between interview steps, by polling
 * GET /segments/pending-confirmations. Analysis runs in the background
 * (Celery), so this has nothing to show most of the time — it only pops up
 * when the pipeline actually pauses on an ambiguous name.
 */
export function EntityConfirmModal() {
  const [pending, setPending] = useState<PendingConfirmation | null>(null)
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

  const answer = async (sameAsExisting: boolean) => {
    if (!pending) return
    answeringRef.current = true
    setAnswering(true)
    try {
      await api.confirmEntity(pending.segment_id, {
        entity_name: pending.pending_confirmation.entity_name,
        same_as_existing: sameAsExisting,
        candidate_uuid: pending.pending_confirmation.candidate_uuid,
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

  const { question, candidate_summary } = pending.pending_confirmation

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
        {candidate_summary && (
          <p className="text-sm text-calm-inkmuted dark:text-calm-inkmutedDark italic">
            &ldquo;{candidate_summary}&rdquo;
          </p>
        )}
        <div className="flex items-center justify-end gap-3 pt-2">
          <button onClick={() => answer(false)} disabled={answering} className="calm-btn-secondary">
            No, different
          </button>
          <button onClick={() => answer(true)} disabled={answering} className="calm-btn-primary">
            {answering ? <Loader2 size={16} className="animate-spin" /> : 'Yes, same'}
          </button>
        </div>
      </div>
    </div>
  )
}
