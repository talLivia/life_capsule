'use client'

import { useEffect, useMemo, useRef, useState } from 'react'
import { HelpCircle, Loader2, UserPlus, Check } from 'lucide-react'
import { toast } from 'react-hot-toast'
import { api } from '@/lib/api'
import type { ApiError, ConfirmEntitiesResult, PendingConfirmation } from '@/lib/types'

const POLL_INTERVAL_MS = 8000

/** Sentinel for "someone new" — never a real candidate id. */
const NEW_ENTITY = '__new__'

/** Keys in the pending payload that are NOT questions. Everything else in it
 *  is one, counted generically so that a class added on the server appears
 *  here without this file being edited — the omission that caused the bug
 *  below could not then happen again. */
const NON_QUESTION_KEYS = new Set(['editable_entities'])

/**
 * How many questions this recording raises, of ANY class.
 *
 * The bug this replaces: the render guard was `totalCount === 0`, where
 * totalCount counted identity and type ONLY. A recording with ten relation
 * questions, ten year questions and ten editable names — fetched, in state,
 * ready to render — returned null and showed nothing at all, because the two
 * oldest classes happened to be empty. Exactly the same omission as the graph
 * router that skipped confirmation entirely, one layer up.
 */
function countQuestions(
  payload: PendingConfirmation['pending_confirmation'] | undefined,
): number {
  return Object.entries(payload ?? {}).reduce(
    (total, [key, value]) =>
      NON_QUESTION_KEYS.has(key) || !Array.isArray(value) ? total : total + value.length,
    0,
  )
}

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
export function EntityConfirmModal({
  refreshKey = 0,
  onResolved,
  openedFromBell = false,
  onClose,
}: {
  /** Opened deliberately from the bell rather than by its own poll. It then
   *  renders even with nothing outstanding, because a producer who clicked it
   *  is owed an answer either way. */
  openedFromBell?: boolean
  onClose?: () => void
  /** Bumped when the extraction screen hands over, so the questions appear at
   *  once instead of on the next background poll — several seconds of nothing
   *  on screen otherwise. */
  refreshKey?: number
  /** The recording whose questions were just answered. The caller reopens its
   *  extraction screen: entities are only written after these answers land,
   *  so this is the first moment it can show what was actually captured. */
  onResolved?: (segmentId: string) => void
} = {}) {
  const [pending, setPending] = useState<PendingConfirmation | null>(null)
  const [identity, setIdentity] = useState<Record<string, string>>({})
  const [types, setTypes] = useState<Record<string, string>>({})
  // Keyed by proposal INDEX, not name: two people can hold the same relation
  // to the speaker, so a name would not identify one.
  const [relations, setRelations] = useState<Record<number, boolean>>({})
  // Free text, sent as typed — the server parses it and refuses what it
  // cannot resolve, rather than the client guessing.
  const [years, setYears] = useState<Record<string, string>>({})
  // Sibling entity id -> ticked parent ids, and a name for a parent nobody
  // has mentioned. Keyed by id because these are people already in the
  // archive, not names just pulled out of this recording.
  const [parentage, setParentage] = useState<Record<string, string[]>>({})
  const [newParent, setNewParent] = useState<Record<string, string>>({})
  // Whether the "someone else" branch is open for a sibling. A separate flag
  // rather than "is there text": the branch has to be VISIBLE before it can be
  // typed into, and it is the only way to say "not my parents" as opposed to
  // saying nothing at all.
  const [otherOpen, setOtherOpen] = useState<Record<string, boolean>>({})
  // Extracted name -> corrected name. Only entries the producer actually
  // changed are sent; the server ignores blanks and no-ops as well.
  const [nameEdits, setNameEdits] = useState<Record<string, string>>({})
  // Aunt/uncle name -> the parent they are a sibling of. One choice each:
  // an uncle is a sibling of one parent, not both.
  const [sides, setSides] = useState<Record<string, string>>({})
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
    // refreshKey re-runs the effect, which polls immediately — that is the
    // handoff from the extraction screen, and it must not wait for the timer.
  }, [refreshKey])

  // Clear every selection whenever a different recording's screen appears —
  // carrying an answer across recordings would attach it to the wrong name.
  useEffect(() => {
    setIdentity({})
    setTypes({})
    setRelations({})
    setYears({})
    setParentage({})
    setNewParent({})
    setOtherOpen({})
    setNameEdits({})
    setSides({})
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
  const parentageQuestions = useMemo(
    () => pending?.pending_confirmation.parentage_questions ?? [],
    [pending],
  )
  const sideQuestions = useMemo(
    () => pending?.pending_confirmation.side_questions ?? [],
    [pending],
  )
  const editableEntities = useMemo(
    () => pending?.pending_confirmation.editable_entities ?? [],
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
  // Only identity and type are REQUIRED — the server rejects a partial submit
  // of those two and nothing else — so they alone drive the submit button.
  const requiredCount = identityQuestions.length + typeQuestions.length
  // Whether the screen appears at all is a different question, and counting
  // it the same way is what hid thirty questions behind an empty one.
  const questionCount = countQuestions(pending?.pending_confirmation)

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
        // Anyone left unanswered is "not sure" — recorded as asked so it
        // is never raised again, with nothing written.
        sides: Object.fromEntries(
          Object.entries(sides).filter(([, parent]) => parent),
        ),
        name_edits: Object.fromEntries(
          Object.entries(nameEdits).filter(
            ([original, corrected]) => corrected.trim() && corrected.trim() !== original,
          ),
        ),
        // Only siblings actually answered for. An untouched one is absent,
        // which the server reads as a skip: still stamped as asked so the
        // question never returns, but nothing written.
        parentage: Object.fromEntries(
          (parentageQuestions[0]?.siblings ?? [])
            .map((sibling) => {
              const shared = parentage[sibling.name] ?? []
              const typed = (newParent[sibling.name] ?? '').trim()
              return [
                sibling.name,
                { parent_names: shared, new_parent_name: typed || undefined },
              ]
            })
            .filter(([, a]) => {
              const answer = a as { parent_names: string[]; new_parent_name?: string }
              return answer.parent_names.length > 0 || answer.new_parent_name
            }),
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
      const resolvedId = pending.segment_id
      setPending(null)
      // Entities are written only once these answers land, so the extraction
      // screen can finally show what was captured. The caller reopens it.
      onResolved?.(resolvedId)
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

  if (!pending || questionCount === 0) {
    if (!openedFromBell) return null
    return (
      <div className="fixed inset-0 z-[80] flex items-center justify-center bg-black/70 backdrop-blur-sm px-4 animate-fade-in">
        <div className="w-full max-w-md glass-card p-6 flex flex-col items-center gap-3 text-center">
          <Check size={22} className="text-primary-400" />
          <h2 className="text-lg font-bold text-white">Nothing to check</h2>
          <p className="text-sm text-gray-400">
            Every question about your recordings has been answered.
          </p>
          <button onClick={onClose} className="btn-primary mt-1">Close</button>
        </div>
      </div>
    )
  }

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
      className="fixed inset-0 z-[80] flex items-center justify-center bg-black/70 backdrop-blur-sm px-4 animate-fade-in"
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
            {questionCount === 1
              ? 'One thing to check about this recording:'
              : `${questionCount} things to check about this recording:`}
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
            <legend className="flex items-center justify-between gap-3 w-full text-sm text-white leading-relaxed mb-1">
              <span>Did we get these relationships right? — optional</span>
              {relationQuestions.length > 1 && (
                // The common case is "all of them", and making that eight
                // clicks is how a screen gets skipped wholesale.
                <button
                  type="button"
                  disabled={answering}
                  onClick={() =>
                    setRelations(current => {
                      const allAccepted = relationQuestions.every(q => current[q.index])
                      const next: Record<number, boolean> = { ...current }
                      for (const q of relationQuestions) next[q.index] = !allAccepted
                      return next
                    })
                  }
                  className="text-xs text-primary-300 hover:text-primary-200 shrink-0"
                >
                  {relationQuestions.every(q => relations[q.index])
                    ? 'Clear all'
                    : 'Yes to all'}
                </button>
              )}
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

        {/* Every name this recording produced, editable.
            NOT a question and never the reason this screen appears — it sits
            at the top because it is the one thing here that can be wrong
            without anything having asked. The extractor said "ליאן" for
            "אליאן" with complete confidence: a brand-new name has nothing
            similar to disambiguate against, so no identity question was
            raised and there was no way to say it was wrong. */}
        {editableEntities.length > 0 && (
          <fieldset className="flex flex-col gap-2 pt-1 border-t border-white/10">
            <legend className="text-sm text-white leading-relaxed mb-1">
              Did we get these names right?
            </legend>
            <p className="text-xs text-gray-400 -mt-1 mb-1">
              Fix any that were misheard. Leave the rest alone.
            </p>
            <div className="flex flex-wrap gap-2">
              {editableEntities.map((entity) => {
                const value = nameEdits[entity.name] ?? entity.name
                const changed = value.trim() !== entity.name
                return (
                  <div key={`edit-${entity.name}`} className="flex flex-col gap-0.5">
                    <input
                      type="text"
                      dir="auto"
                      value={value}
                      onChange={(e) =>
                        setNameEdits((s) => ({ ...s, [entity.name]: e.target.value }))
                      }
                      disabled={answering}
                      aria-label={`Name: ${entity.name}`}
                      className={`w-36 px-3 py-1.5 rounded-lg bg-surface-800 border text-sm text-white ${
                        changed ? 'border-primary-400' : 'border-white/10'
                      }`}
                    />
                    {changed && (
                      <span className="text-[10px] text-primary-300">
                        was &ldquo;{entity.name}&rdquo;
                      </span>
                    )}
                  </div>
                )
              })}
            </div>
          </fieldset>
        )}

        {/* ONE grouped question, not one per sibling.
            Four near-identical screens whose answer is the same each time is
            how a producer learns to click past a screen without reading —
            which is how a question that DOES matter gets missed.

            The single Yes covers the ordinary case. Anyone who does not fit
            gets the per-person branch, because a half-sibling shares ONE
            parent and no yes/no can say which. */}
        {parentageQuestions.map((group) => {
          // Defensive on purpose. This payload is persisted JSON and can
          // have been written by an older build; a shape we do not
          // recognise must render nothing, never take the screen down and
          // with it every other question on it.
          const siblings = group.siblings ?? []
          const parents = group.parents ?? []
          if (siblings.length === 0 || parents.length === 0) return null
          const allShared =
            siblings.length > 0 &&
            siblings.every(
              (sibling) =>
                (parentage[sibling.name] ?? []).length === parents.length,
            )
          const answerAll = () =>
            setParentage((current) => {
              const next = { ...current }
              for (const sibling of siblings) {
                next[sibling.name] = allShared
                  ? []
                  : parents.map((parent) => parent.name)
              }
              return next
            })

          return (
            <fieldset
              key="parentage"
              className="flex flex-col gap-3 pt-1 border-t border-white/10"
            >
              <legend className="text-sm text-white leading-relaxed mb-1">
                <span dir="auto">{group.question}</span>
              </legend>
              <p className="text-xs text-gray-400 -mt-1 mb-1">
                You&apos;ve said these people are your siblings, but not whose
                children they are — so nothing joins them to anyone in your tree.
                Asked once either way.
              </p>

              <button
                type="button"
                disabled={answering}
                onClick={answerAll}
                className={`self-start px-4 py-2 rounded-lg border text-sm transition-colors ${
                  allShared
                    ? 'border-primary-400 bg-primary-500/15 text-white'
                    : 'border-white/10 bg-surface-800 text-gray-300 hover:border-white/25'
                }`}
              >
                {allShared ? 'Yes — all of them' : 'Yes — all of them'}
              </button>

              <div className="flex flex-col gap-2 pl-1">
                {siblings.map((sibling) => {
                  const shared = parentage[sibling.name] ?? []
                  const typed = (newParent[sibling.name] ?? '').trim()
                  const open = otherOpen[sibling.name]
                  return (
                    <div key={sibling.name} className="flex flex-col gap-1">
                      <div className="flex flex-wrap items-center gap-2">
                        <span dir="auto" className="text-sm text-white w-24 shrink-0">
                          {sibling.name}
                        </span>
                        {parents.map((parent) => {
                          const ticked = shared.includes(parent.name)
                          return (
                            <button
                              key={parent.name}
                              type="button"
                              disabled={answering}
                              onClick={() =>
                                setParentage((current) => ({
                                  ...current,
                                  [sibling.name]: ticked
                                    ? shared.filter((n) => n !== parent.name)
                                    : [...shared, parent.name],
                                }))
                              }
                              className={`px-2.5 py-1 rounded-lg border text-xs transition-colors ${
                                ticked
                                  ? 'border-primary-400 bg-primary-500/15 text-white'
                                  : 'border-white/10 bg-surface-800 text-gray-300 hover:border-white/25'
                              }`}
                            >
                              <span dir="auto">{parent.name}</span>
                            </button>
                          )
                        })}
                        <button
                          type="button"
                          disabled={answering}
                          onClick={() =>
                            setOtherOpen((current) => {
                              const opening = !current[sibling.name]
                              if (!opening) {
                                // Backing out must not leave a stale name to be
                                // submitted for a path that was abandoned.
                                setNewParent((n) => ({ ...n, [sibling.name]: '' }))
                              }
                              return { ...current, [sibling.name]: opening }
                            })
                          }
                          className={`px-2.5 py-1 rounded-lg border text-xs transition-colors ${
                            open
                              ? 'border-primary-400 bg-primary-500/15 text-white'
                              : 'border-white/10 bg-surface-800 text-gray-400 hover:border-white/25'
                          }`}
                        >
                          Someone else
                        </button>
                      </div>

                      {open && (
                        <div className="flex flex-col gap-1 pl-24">
                          <label
                            htmlFor={`other-parent-${sibling.name}`}
                            className="text-xs text-gray-400"
                          >
                            Then whose child are they?
                          </label>
                          <input
                            id={`other-parent-${sibling.name}`}
                            type="text"
                            dir="auto"
                            list="parentage-known-people"
                            value={newParent[sibling.name] ?? ''}
                            onChange={(e) =>
                              setNewParent((current) => ({
                                ...current,
                                [sibling.name]: e.target.value,
                              }))
                            }
                            disabled={answering}
                            placeholder="a name"
                            className="w-56 px-3 py-1.5 rounded-lg bg-surface-800 border border-white/10 text-sm text-white placeholder:text-gray-600"
                          />
                        </div>
                      )}

                      {shared.length === 0 && !typed && (
                        <span className="text-[11px] text-gray-500 pl-24">
                          Skipped — nothing recorded, and we won&apos;t ask again.
                        </span>
                      )}
                    </div>
                  )
                })}
              </div>

              {/* Picking beats typing: a typed name resolves by normalised
                  match, so one different character makes a second person
                  instead of linking to the first. */}
              <datalist id="parentage-known-people">
                {(group.known_people ?? []).map((person) => (
                  <option key={person.name} value={person.name} />
                ))}
              </datalist>
            </fieldset>
          )
        })}

        {/* Which parent an aunt or uncle belongs to.
            Without it, an aunt_uncle edge puts them in the parents' row and
            says nothing more — so the row is four boxes and nothing marks the
            two that are actually parents. This produces the edge the chart
            draws between them and the parent they are a sibling of. */}
        {sideQuestions.map((group) => {
          const relatives = group.relatives ?? []
          const parents = group.parents ?? []
          if (relatives.length === 0 || parents.length === 0) return null
          return (
            <fieldset
              key="sides"
              className="flex flex-col gap-3 pt-1 border-t border-white/10"
            >
              <legend className="text-sm text-white leading-relaxed mb-1">
                <span dir="auto">{group.question}</span>
              </legend>
              <p className="text-xs text-gray-400 -mt-1 mb-1">
                Whose brother or sister are they? This is what connects them to the
                right parent in your tree. Asked once either way.
              </p>
              {relatives.map((relative) => (
                <div key={relative.name} className="flex flex-wrap items-center gap-2">
                  <span dir="auto" className="text-sm text-white w-24 shrink-0">
                    {relative.name}
                  </span>
                  {parents.map((parent) => {
                    const chosen = sides[relative.name] === parent.name
                    return (
                      <button
                        key={parent.name}
                        type="button"
                        disabled={answering}
                        onClick={() =>
                          setSides((current) => ({
                            ...current,
                            // Clicking the chosen one again clears it — one
                            // parent, and a way back to saying nothing.
                            [relative.name]: chosen ? '' : parent.name,
                          }))
                        }
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
                  {!sides[relative.name] && (
                    <span className="text-[11px] text-gray-500">
                      Not sure — we won&apos;t ask again
                    </span>
                  )}
                </div>
              ))}
            </fieldset>
          )
        })}

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
            {requiredCount === 0
              ? // Nothing is compulsory here — saying "0 of 0 answered" reads
                // as an error, and saying "All answered" claims something the
                // producer has not done.
                'Everything here is optional'
              : allAnswered
                ? 'All answered'
                : `${answeredCount} of ${requiredCount} answered`}
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
