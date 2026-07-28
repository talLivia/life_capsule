'use client'

import { useEffect, useMemo, useRef, useState } from 'react'
import { HelpCircle, Loader2, UserPlus, Check } from 'lucide-react'
import { toast } from 'react-hot-toast'
import { api } from '@/lib/api'
import type { ApiError, PendingConfirmation } from '@/lib/types'

const POLL_INTERVAL_MS = 8000

/** Sentinel for "someone new" — never a real candidate id. */
const NEW_ENTITY = '__new__'

/**
 * Everything unclear about ONE recording, on one screen, with one submit.
 *
 * This used to be a SEQUENCE of modals — one per ambiguous name, each
 * resuming the pipeline and waiting for it to pause again. Batching is not
 * just fewer clicks: a sequence gives the producer no idea how many are
 * coming, and each answer is given without seeing the others, even though
 * "is this the same Moshe" and "is הכפר הירוק a place or an organisation"
 * are both really the same question — did the system understand this
 * recording. One screen is also the only scale at which a small misreading
 * can be told apart from a big one.
 *
 * Two kinds of question, deliberately rendered differently:
 *  - IDENTITY. One candidate -> yes/no. Two or more -> a picker, so a bare
 *    "Moshe" matching both "Moshe Cohen" and "Moshe Levi" asks which, rather
 *    than a yes/no about an arbitrary single guess.
 *  - TYPE. Always exactly two options, because the extractor reports the
 *    runner-up it was torn between rather than a confidence score. Only
 *    entities it was genuinely torn about appear at all — asking about
 *    everything trains people to click through without reading.
 */
export function EntityConfirmModal() {
  const [pending, setPending] = useState<PendingConfirmation | null>(null)
  const [identity, setIdentity] = useState<Record<string, string>>({})
  const [types, setTypes] = useState<Record<string, string>>({})
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

  // Clear every selection whenever a different recording's screen appears —
  // carrying an answer across recordings would attach it to the wrong name.
  useEffect(() => {
    setIdentity({})
    setTypes({})
  }, [pending?.segment_id])

  const identityQuestions = useMemo(
    () => pending?.pending_confirmation.identity_questions ?? [],
    [pending],
  )
  const typeQuestions = useMemo(
    () => pending?.pending_confirmation.type_questions ?? [],
    [pending],
  )

  // The server rejects a partial submit, so the button must not offer one.
  const allAnswered =
    identityQuestions.every((q) => identity[q.name]) &&
    typeQuestions.every((q) => types[q.name])
  const answeredCount =
    identityQuestions.filter((q) => identity[q.name]).length +
    typeQuestions.filter((q) => types[q.name]).length
  const totalCount = identityQuestions.length + typeQuestions.length

  const submit = async () => {
    if (!pending || !allAnswered) return
    answeringRef.current = true
    setAnswering(true)
    try {
      await api.confirmEntities(pending.segment_id, {
        identity: Object.fromEntries(
          identityQuestions.map((q) => {
            const choice = identity[q.name]
            return [
              q.name,
              choice === NEW_ENTITY
                ? { same_as_existing: false }
                : { same_as_existing: true, candidate_uuid: choice },
            ]
          }),
        ),
        types: Object.fromEntries(typeQuestions.map((q) => [q.name, types[q.name]])),
      })
      setPending(null)
      // Another RECORDING may also be waiting — this screen covers one.
      try {
        const list: PendingConfirmation[] = await api.getPendingConfirmations()
        setPending(list[0] ?? null)
      } catch {
        /* next poll tick will catch it */
      }
    } catch (err: unknown) {
      const detail = (err as ApiError)?.response?.data?.detail || (err as ApiError)?.message
      toast.error(detail || 'Could not save your answers — please try again')
    } finally {
      answeringRef.current = false
      setAnswering(false)
    }
  }

  if (!pending || totalCount === 0) return null

  const optionClass = (selected: boolean) =>
    `flex items-start gap-3 w-full text-left px-4 py-3 rounded-xl border transition-all duration-150 ${
      selected
        ? 'border-primary-500/60 bg-primary-500/10'
        : 'border-white/10 hover:border-primary-500/40'
    }`

  const radio = (selected: boolean) => (
    <span
      className={`mt-0.5 w-4 h-4 rounded-full border flex items-center justify-center flex-shrink-0 ${
        selected ? 'border-primary-500 bg-primary-500' : 'border-gray-500'
      }`}
    >
      {selected && <Check size={11} className="text-white" />}
    </span>
  )

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 backdrop-blur-sm px-4 animate-fade-in"
      role="dialog"
      aria-modal="true"
      aria-labelledby="entity-confirm-heading"
    >
      <div className="w-full max-w-lg glass-card p-6 flex flex-col gap-5 max-h-[85vh] overflow-y-auto">
        <div>
          <div className="flex items-center gap-2 text-primary-400">
            <HelpCircle size={18} />
            <span className="text-sm font-semibold">Quick check</span>
          </div>
          <h2 id="entity-confirm-heading" className="text-white text-base mt-2 leading-relaxed">
            {totalCount === 1
              ? 'One thing to check about this recording:'
              : `${totalCount} things to check about this recording:`}
          </h2>
          <p className="text-xs text-gray-400 mt-1 italic">{pending.question_asked}</p>
        </div>

        {identityQuestions.map((q) => (
          <fieldset key={`id-${q.name}`} className="flex flex-col gap-2">
            <legend className="text-sm text-white leading-relaxed mb-1">{q.question}</legend>
            {q.candidates.map((c) => (
              <button
                key={c.uuid}
                type="button"
                onClick={() => setIdentity((s) => ({ ...s, [q.name]: c.uuid }))}
                disabled={answering}
                className={optionClass(identity[q.name] === c.uuid)}
              >
                {radio(identity[q.name] === c.uuid)}
                <span>
                  <span className="block text-sm font-medium text-white">{c.name}</span>
                  {c.summary && (
                    <span className="block text-xs text-gray-400 mt-0.5">{c.summary}</span>
                  )}
                </span>
              </button>
            ))}
            <button
              type="button"
              onClick={() => setIdentity((s) => ({ ...s, [q.name]: NEW_ENTITY }))}
              disabled={answering}
              className={optionClass(identity[q.name] === NEW_ENTITY)}
            >
              {radio(identity[q.name] === NEW_ENTITY)}
              <span className="flex items-center gap-1.5 text-sm font-medium text-white">
                <UserPlus size={14} />
                {q.candidates.length === 1 ? 'No, someone different' : 'Someone new, not listed'}
              </span>
            </button>
          </fieldset>
        ))}

        {typeQuestions.map((q) => (
          <fieldset key={`type-${q.name}`} className="flex flex-col gap-2">
            <legend className="text-sm text-white leading-relaxed mb-1">{q.question}</legend>
            <div className="flex gap-2">
              {[q.type, q.alternative_type].map((option) => (
                <button
                  key={option}
                  type="button"
                  onClick={() => setTypes((s) => ({ ...s, [q.name]: option }))}
                  disabled={answering}
                  className={`${optionClass(types[q.name] === option)} flex-1 items-center`}
                >
                  {radio(types[q.name] === option)}
                  <span className="text-sm font-medium text-white capitalize">{option}</span>
                </button>
              ))}
            </div>
          </fieldset>
        ))}

        <div className="flex items-center justify-between gap-3 pt-1">
          <span className="text-xs text-gray-400">
            {allAnswered ? 'All answered' : `${answeredCount} of ${totalCount} answered`}
          </span>
          <button
            type="button"
            onClick={submit}
            disabled={answering || !allAnswered}
            className="btn-primary disabled:opacity-40 disabled:cursor-not-allowed"
          >
            {answering ? <Loader2 size={16} className="animate-spin" /> : 'Save answers'}
          </button>
        </div>
      </div>
    </div>
  )
}
