'use client'

import { useEffect, useMemo, useRef, useState } from 'react'
import { HelpCircle, Loader2, UserPlus, Check } from 'lucide-react'
import { toast } from 'react-hot-toast'
import { api } from '@/lib/api'
import type { ApiError, ConfirmEntitiesResult, PendingConfirmation } from '@/lib/types'

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
  // Keyed by proposal INDEX, not name: two people can hold the same relation
  // to the speaker, so a name would not identify one.
  const [relations, setRelations] = useState<Record<number, boolean>>({})
  // Free text, sent as typed — the server parses it and refuses what it
  // cannot resolve, rather than the client guessing.
  const [years, setYears] = useState<Record<string, string>>({})
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
    setRelations({})
    setYears({})
  }, [pending?.segment_id])

  const identityQuestions = useMemo(
    () => pending?.pending_confirmation.identity_questions ?? [],
    [pending],
  )
  const typeQuestions = useMemo(
    () => pending?.pending_confirmation.type_questions ?? [],
    [pending],
  )
  const relationQuestions = useMemo(
    () => pending?.pending_confirmation.relation_questions ?? [],
    [pending],
  )
  const yearQuestions = useMemo(
    () => pending?.pending_confirmation.year_questions ?? [],
    [pending],
  )

  // The server rejects a partial submit of identity/type, so the button must
  // not offer one. Relations are deliberately NOT counted here: they are
  // skippable, and including them would make the button demand answers the
  // server does not require — turning "you may skip this" into "you may not".
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
      const outcome: ConfirmEntitiesResult = await api.confirmEntities(pending.segment_id, {
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
        // Only the accepted ones. An untouched relation is simply absent,
        // which the server reads as "not stored" — the same as declining.
        relations: Object.fromEntries(
          Object.entries(relations).filter(([, accepted]) => accepted),
        ),
        years: Object.fromEntries(
          Object.entries(years).filter(([, v]) => v.trim()),
        ),
      })
      // Say what the answer DID. A type answer used to be accepted and then
      // discarded by the "existing value wins" rule with no feedback at all;
      // it now takes effect, and the producer is told so rather than having
      // to go and check.
      for (const change of outcome?.applied_type_changes ?? []) {
        toast.success(`${change.name}: ${change.was} → ${change.now}`)
      }
      // A year the server could not resolve is NOT stored, and saying so is
      // the whole point — guessing at it would put a wrong date on a life.
      for (const bad of outcome?.rejected_years ?? []) {
        toast.error(`Couldn't read "${bad.given}" as a year — ${bad.reason}. Not saved.`)
      }
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

        {relationQuestions.length > 0 && (
          <fieldset className="flex flex-col gap-2 pt-1 border-t border-white/10">
            {/* Visually separated because it is a DIFFERENT KIND of question:
                everything above must be answered, this may be skipped. Saying
                so beats leaving the producer to infer it from the button
                staying enabled. */}
            <legend className="text-sm text-white leading-relaxed mb-1">
              Family connections I picked up — optional
            </legend>
            <p className="text-xs text-gray-400 -mt-1 mb-1">
              Tick the ones that are right. Anything you leave alone is simply not saved.
            </p>
            {relationQuestions.map((q) => (
              <button
                key={`rel-${q.index}`}
                type="button"
                onClick={() =>
                  setRelations((s) => ({ ...s, [q.index]: !s[q.index] }))
                }
                disabled={answering}
                className={optionClass(Boolean(relations[q.index]))}
              >
                {radio(Boolean(relations[q.index]))}
                <span className="flex flex-col gap-0.5 text-left">
                  <span className="text-sm font-medium text-white">
                    <span dir="auto">{q.from_name === '__SELF__' ? 'You' : q.from_name}</span>
                    {' is the '}
                    {q.relation_type.replace(/_/g, ' ')}
                    {' of '}
                    <span dir="auto">{q.to_name === '__SELF__' ? 'you' : q.to_name}</span>
                  </span>
                  {q.evidence && (
                    <span dir="auto" className="text-xs text-gray-400">
                      &ldquo;{q.evidence}&rdquo;
                    </span>
                  )}
                </span>
              </button>
            ))}
          </fieldset>
        )}

        {yearQuestions.length > 0 && (
          <fieldset className="flex flex-col gap-2 pt-1 border-t border-white/10">
            <legend className="text-sm text-white leading-relaxed mb-1">
              Roughly when? — optional
            </legend>
            <p className="text-xs text-gray-400 -mt-1 mb-1">
              A year helps place these on the timeline. Leave blank to skip.
            </p>
            {yearQuestions.map((q) => (
              <label key={`year-${q.name}`} className="flex items-center gap-3">
                <span dir="auto" className="text-sm text-white flex-1">{q.name}</span>
                <input
                  type="text"
                  inputMode="numeric"
                  value={years[q.name] ?? ''}
                  onChange={(e) => setYears((s) => ({ ...s, [q.name]: e.target.value }))}
                  disabled={answering}
                  placeholder="e.g. 1973"
                  className="w-32 px-3 py-1.5 rounded-lg bg-surface-800 border border-white/10 text-sm text-white placeholder:text-gray-600"
                />
              </label>
            ))}
          </fieldset>
        )}

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
