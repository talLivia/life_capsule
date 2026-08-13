'use client'

import { useEffect, useMemo, useState } from 'react'
import { HelpCircle, Loader2, UserPlus, Check, X } from 'lucide-react'
import { toast } from 'react-hot-toast'
import { api } from '@/lib/api'
import { usePendingConfirmations } from '@/components/providers/PendingConfirmationsProvider'
// The one implementation, shared with the notification row that says how many
// things this recording needs checked. Two counts of the same payload is how
// a badge and the screen it opens come to disagree.
import { countQuestions } from '@/lib/pendingQuestions'
import type {
  ApiError,
  ConfirmEntitiesResult,
  PendingConfirmation,
  RelationEdit,
} from '@/lib/types'

/** Sentinel for "someone new" — never a real candidate id. */
const NEW_ENTITY = '__new__'

/** The producer themselves, as the relation payload names them. */
const SELF = '__SELF__'

/**
 * Pick a person for one end of a corrected relation.
 *
 * A `select` rather than the datalist the parentage question uses, and the
 * difference is deliberate: parentage has to admit a parent nobody has ever
 * mentioned, so it must accept typing. A correction only ever re-points a
 * relation at someone the archive already knows, so the stricter control is
 * available — and a value that cannot be typed cannot be a typo, which is the
 * whole reason the datalist existed. The server refuses anything it did not
 * offer either way; this makes that refusal unreachable by normal use.
 */
function PersonSelect({
  value,
  people,
  disabled,
  label,
  onChange,
}: {
  value: string
  people: { name: string }[]
  disabled: boolean
  label: string
  onChange: (name: string) => void
}) {
  return (
    <select
      value={value}
      disabled={disabled}
      aria-label={label}
      onChange={e => onChange(e.target.value)}
      dir="auto"
      className="px-2.5 py-1.5 rounded-lg bg-surface-800 border border-edge text-sm text-ink max-w-[10rem]"
    >
      <option value={SELF}>You</option>
      {people.map(person => (
        <option key={person.name} value={person.name}>
          {person.name}
        </option>
      ))}
      {/* A proposal can name someone who is not in the offered list — the
          server builds it from this recording's own proposals too, but an
          older stored payload may not have them. Keeping the current value
          selectable stops the control silently changing the answer. */}
      {value !== SELF && !people.some(p => p.name === value) && (
        <option value={value}>{value}</option>
      )}
    </select>
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
 *
 * It no longer opens ITSELF, and no longer chooses which recording to show.
 * It used to poll, appear over whatever the producer was doing, and work
 * through the pending list head-first. Now a notification row names the
 * recording and this screen answers exactly that one
 * (docs/GUIDED_INTERVIEW.md §14, §17). Three consequences this file honours:
 *
 *  - It must be closable. As an auto-opened popup it was a dead end on
 *    purpose — answering was the only way out, because it appeared at the one
 *    moment the recording was still fresh. Reached by choosing a row, the same
 *    dead end is a trap.
 *  - It answers ONE recording and stops. Advancing to the next by itself would
 *    contradict the list the producer just chose from, and take them somewhere
 *    they did not ask to go.
 *  - Its question SECTIONS are unchanged, and deliberately so. However it is
 *    reached, it POSTs the identical `EntityBatchConfirmRequest` and the
 *    server cannot tell the difference — the hard constraint of §7.
 */
export function EntityConfirmModal({
  /** The recording to answer. Chosen by the producer from the notification
   *  list, never by this component. */
  segmentId,
  onClose,
}: {
  segmentId: string
  onClose: () => void
}) {
  const { items, refresh } = usePendingConfirmations()
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
  // Proposal index -> the relation it should have been. Present only for
  // proposals the producer opened "Not quite" on; an edited proposal is
  // stored as corrected without also needing its tick.
  const [relationEdits, setRelationEdits] = useState<Record<number, RelationEdit>>({})
  // A name telling a new person apart from the one already in the archive.
  // Only asked for when the extracted name EXACTLY matches a candidate: the
  // merge key is the name, so without a different one "a different אמנון" is
  // stored as "the same אמנון" — which is how one row came to hold both an
  // uncle and an army friend.
  const [distinctNames, setDistinctNames] = useState<Record<string, string>>({})
  const [answering, setAnswering] = useState(false)

  // Escape leaves, except mid-submit — closing on a request already in flight
  // would hide whether the answers landed.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !answering) onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose, answering])

  // Read from the shared list rather than fetched again — one poller, and a
  // screen that cannot disagree with the badge that opened it. Looking the
  // recording up by id also means a background poll can reorder the list
  // without moving the ground under a half-answered form.
  const pending: PendingConfirmation | null =
    items.find(item => item.segment_id === segmentId) ?? null

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
    setRelationEdits({})
    setDistinctNames({})
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
  const correctionPeople = useMemo(
    () => pending?.pending_confirmation.correction_people ?? [],
    [pending],
  )
  const correctionTypes = useMemo(
    () => pending?.pending_confirmation.correction_types ?? [],
    [pending],
  )
  // Corrections can only be offered when the server sent the options. A
  // payload stored before this existed has neither, and the control is simply
  // absent rather than offering a dropdown that would be refused on submit.
  const canCorrect = correctionTypes.length > 0

  /**
   * ONE CARD PER PERSON, carrying everything asked about them.
   *
   * The screen used to be seven blocks in payload order — identity, then
   * type, then relations, then names, then parentage, then sides, then years
   * — so resolving one person meant scrolling between five of them and
   * remembering which row you were on. Five of the seven were already keyed by
   * entity name and only rendered apart.
   *
   * The spine is every name mentioned by anything on this screen, not just
   * `editable_entities`: a parentage or side question can be about somebody
   * from an EARLIER recording, who has no entry there at all. Their card still
   * has to exist, just without a name field — this screen edits what the
   * extractor produced, and an archive person was not produced here.
   *
   * A relation anchors to the end that ISN'T the producer, and to `from_name`
   * when neither is, so it appears exactly once rather than on both cards. The
   * anchor comes from the ORIGINAL proposal, so correcting an endpoint cannot
   * make the card jump to a different person mid-edit.
   */
  const groups = useMemo(() => {
    const order: string[] = []
    const seen = new Set<string>()
    const add = (name?: string | null) => {
      if (!name || name === SELF || seen.has(name)) return
      seen.add(name)
      order.push(name)
    }

    // Extraction order first — it is the order the recording named people in,
    // which is the order the producer told the story.
    editableEntities.forEach(e => add(e.name))
    identityQuestions.forEach(q => add(q.name))
    typeQuestions.forEach(q => add(q.name))
    yearQuestions.forEach(q => add(q.name))
    relationQuestions.forEach(q =>
      add(q.to_name === SELF ? q.from_name : q.to_name),
    )
    parentageQuestions.forEach(g => (g.siblings ?? []).forEach(s => add(s.name)))
    sideQuestions.forEach(g => (g.relatives ?? []).forEach(r => add(r.name)))

    const editableByName = new Map(editableEntities.map(e => [e.name, e]))

    return order.map(name => {
      const relations = relationQuestions.filter(
        q => (q.to_name === SELF ? q.from_name : q.to_name) === name,
      )
      // A sibling only gets the parent picker when the SAME screen is asking
      // about their parentage — the grouped question decides who that is.
      const parentageGroup =
        parentageQuestions.find(g =>
          (g.siblings ?? []).some(s => s.name === name),
        ) ?? null
      const sideGroup =
        sideQuestions.find(g => (g.relatives ?? []).some(r => r.name === name)) ?? null
      // Which edge this person's answer writes — a grandparent is the PARENT
      // of the chosen parent, an aunt or uncle their SIBLING. Absent on a
      // payload stored before grandparents were asked about, which then reads
      // as the aunt/uncle it always was.
      const sideKind =
        (sideGroup?.relatives ?? []).find(r => r.name === name)?.kind ?? 'aunt_uncle'

      return {
        name,
        entity: editableByName.get(name) ?? null,
        identity: identityQuestions.find(q => q.name === name) ?? null,
        type: typeQuestions.find(q => q.name === name) ?? null,
        year: yearQuestions.find(q => q.name === name) ?? null,
        relations,
        parentageGroup,
        sideGroup,
        sideKind,
      }
    })
  }, [
    editableEntities,
    identityQuestions,
    typeQuestions,
    yearQuestions,
    relationQuestions,
    parentageQuestions,
    sideQuestions,
  ])

  /** Cards that actually ask something, and the rest.
   *
   *  A name field alone is not a question — it is there in case the extractor
   *  misheard. Giving those a full card each turns an eight-person recording
   *  into eight cards when two of them ask anything, and makes the screen
   *  far longer than the "N things to check" heading promises. They keep the
   *  compact strip they already had. */
  const asking = groups.filter(
    g => g.identity || g.type || g.year || g.relations.length > 0 || g.parentageGroup || g.sideGroup,
  )
  const nameOnly = groups.filter(g => !asking.includes(g) && g.entity)

  // The server rejects a partial submit of identity/type, so the button must
  // not offer one. Relations are deliberately NOT counted here: they are
  // skippable, and including them would make the button demand answers the
  // server does not require — turning "you may skip this" into "you may not".
  // A correction that relates somebody to themselves is refused by the server
  // and by a CHECK constraint below it. Blocked here so it is caught while the
  // two dropdowns are still on screen, rather than as an error after submit.
  const brokenEdit = Object.values(relationEdits).some(
    edit => edit.from_name === edit.to_name,
  )
  // "Someone new" about a name that EXACTLY matches an existing person is not
  // a complete answer: the merge key is the name, so storing it as given would
  // record the opposite of what was said. The server refuses it too — blocked
  // here so the empty field is on screen when it is pointed at, rather than
  // arriving as a 422 after the whole form is submitted.
  const missingDistinctName = identityQuestions.some(
    (q) =>
      identity[q.name] === NEW_ENTITY &&
      q.candidates.some((c) => c.name.trim() === q.name.trim()) &&
      !(distinctNames[q.name] ?? '').trim(),
  )
  const allAnswered =
    identityQuestions.every((q) => identity[q.name]) &&
    typeQuestions.every((q) => types[q.name]) &&
    !missingDistinctName &&
    !brokenEdit
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
    setAnswering(true)
    try {
      const outcome: ConfirmEntitiesResult = await api.confirmEntities(pending.segment_id, {
        identity: Object.fromEntries(
          identityQuestions.map((q) => {
            const choice = identity[q.name]
            return [
              q.name,
              choice === NEW_ENTITY
                ? {
                    same_as_existing: false,
                    new_name: (distinctNames[q.name] ?? '').trim() || undefined,
                  }
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
        // A corrected proposal is stored as corrected and needs no separate
        // acceptance. Sent keyed by the same proposal index.
        relation_edits: relationEdits,
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
      // One fetch, through the shared provider, so the badge, the list and
      // this screen cannot disagree about what is left. Awaited BEFORE closing
      // so the caller returns to a list this recording has already dropped out
      // of, rather than one still showing a row that has been answered.
      await refresh()
      onClose()
    } catch (err: unknown) {
      const detail = (err as ApiError)?.response?.data?.detail || (err as ApiError)?.message
      toast.error(detail || 'Could not save your answers — please try again')
    } finally {
      setAnswering(false)
    }
  }

  // This screen only ever appears because someone asked for it, so an empty
  // list gets an answer rather than a silently absent dialog.
  if (!pending || questionCount === 0) {
    return (
      <div className="fixed inset-0 z-[80] flex items-center justify-center bg-black/70 backdrop-blur-sm px-4 animate-fade-in">
        <div className="w-full max-w-md glass-card p-6 flex flex-col items-center gap-3 text-center">
          <Check size={22} className="text-primary-400" />
          <h2 className="text-lg font-bold text-ink">Nothing to check</h2>
          <p className="text-sm text-muted">
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
        : 'border-edge hover:border-primary-500/40'
    }`

  const radio = (selected: boolean) => (
    <span
      className={`mt-0.5 w-4 h-4 rounded-full border flex items-center justify-center flex-shrink-0 ${
        selected ? 'border-primary-500 bg-primary-500' : 'border-gray-500'
      }`}
    >
      {selected && <Check size={11} className="text-ink" />}
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
        <div className="flex items-start justify-between gap-4">
          <div className="min-w-0">
            {/* No "N of M" any more. The producer picked this recording off a
                list that shows what else is waiting, so a position counter
                would describe a queue they are not in. */}
            <div className="flex items-center gap-2 text-primary-400">
              <HelpCircle size={18} />
              <span className="text-sm font-semibold">Quick check</span>
            </div>
            <h2 id="entity-confirm-heading" className="text-ink text-base mt-2 leading-relaxed">
              {questionCount === 1
                ? 'One thing to check about this recording:'
                : `${questionCount} things to check about this recording:`}
            </h2>
            {/* The recording these questions are about. With the popup gone
                nobody answers while the recording is fresh, so the context
                has to travel with the question — §12. */}
            <p dir="auto" className="text-xs text-muted mt-1 italic">{pending.question_asked}</p>
          </div>
          <button
            onClick={onClose}
            disabled={answering}
            className="btn-icon shrink-0 disabled:opacity-40"
            aria-label="Close"
          >
            <X size={16} />
          </button>
        </div>

        {asking.map((group) => {
          const entity = group.entity
          const nameValue = nameEdits[group.name] ?? group.name
          const nameChanged = nameValue.trim() !== group.name
          const parents = group.parentageGroup?.parents ?? []
          const shared = parentage[group.name] ?? []
          const typedParent = (newParent[group.name] ?? '').trim()
          const sideParents = group.sideGroup?.parents ?? []

          /**
           * Does the parentage answer say this person is NOT your sibling?
           *
           * Mirrors the server rule exactly ("shares no parent with you", see
           * human_confirm_node): naming only somebody who is not one of your
           * own parents means no shared parent, so the sibling relation is
           * replaced rather than stored alongside a parent edge it
           * contradicts.
           *
           * The client needs its own copy for ONE reason — so the screen
           * cannot show a ticked sibling box that the server is about to
           * discard. The server stays authoritative; this only stops the UI
           * claiming something different from what will be saved.
           */
          const typedIsOwnParent = parents.some(
            (p) => p.name.trim().toLowerCase() === typedParent.toLowerCase(),
          )
          const parentageSaysNotSibling =
            Boolean(group.parentageGroup) &&
            shared.length === 0 &&
            Boolean(typedParent) &&
            !typedIsOwnParent

          return (
            <section
              key={`person-${group.name}`}
              className="flex flex-col gap-3 p-4 rounded-xl border border-edge bg-surface-800/40"
            >
              <div className="flex items-baseline gap-2 flex-wrap">
                <h3 dir="auto" className="text-sm font-semibold text-ink">
                  {group.name}
                </h3>
                {entity?.type && (
                  <span className="text-[11px] text-muted2">{entity.type}</span>
                )}
              </div>

              {/* Name and year: the two plain FIELDS, side by side at the top.
                  Neither is a question — one exists because the extractor can
                  be confidently wrong, the other because a year is optional —
                  so they sit above the things that must be answered. */}
              {(entity || group.year) && (
                <div className="flex flex-wrap items-end gap-3">
                  {/* Only for entities THIS recording produced. Somebody pulled
                      in from an earlier recording by the parentage question has
                      no name field: this screen corrects what the extractor
                      heard, and it never heard them. */}
                  {entity && (
                    <label className="flex flex-col gap-1">
                      <span className="text-[11px] text-muted">Name</span>
                      <input
                        type="text"
                        dir="auto"
                        value={nameValue}
                        onChange={(e) =>
                          setNameEdits((s) => ({ ...s, [group.name]: e.target.value }))
                        }
                        disabled={answering}
                        aria-label={`Name: ${group.name}`}
                        className={`w-40 px-3 py-1.5 rounded-lg bg-surface-800 border text-sm text-ink ${
                          nameChanged ? 'border-primary-400' : 'border-edge'
                        }`}
                      />
                      {nameChanged && (
                        <span className="text-[10px] text-primary-300">
                          was &ldquo;{group.name}&rdquo;
                        </span>
                      )}
                    </label>
                  )}
                  {group.year && (
                    <label className="flex flex-col gap-1">
                      <span className="text-[11px] text-muted">Year — optional</span>
                      <input
                        type="text"
                        inputMode="numeric"
                        value={years[group.name] ?? ''}
                        onChange={(e) =>
                          setYears((s) => ({ ...s, [group.name]: e.target.value }))
                        }
                        disabled={answering}
                        placeholder="e.g. 1973"
                        aria-label={`Year for ${group.name}`}
                        className="w-32 px-3 py-1.5 rounded-lg bg-surface-800 border border-edge text-sm text-ink placeholder:text-muted2"
                      />
                    </label>
                  )}
                </div>
              )}

              {/* IDENTITY. One candidate -> yes/no. Two or more -> a picker, so
                  a bare "Moshe" matching both "Moshe Cohen" and "Moshe Levi"
                  asks which rather than a yes/no about an arbitrary guess. */}
              {group.identity && (
                <fieldset className="flex flex-col gap-2">
                  <legend className="text-sm text-ink leading-relaxed mb-1">
                    {group.identity.question}
                  </legend>
                  {group.identity.candidates.map((c) => (
                    <button
                      key={c.uuid}
                      type="button"
                      onClick={() => setIdentity((s) => ({ ...s, [group.name]: c.uuid }))}
                      disabled={answering}
                      className={optionClass(identity[group.name] === c.uuid)}
                    >
                      {radio(identity[group.name] === c.uuid)}
                      <span>
                        <span className="block text-sm font-medium text-ink">{c.name}</span>
                        {c.summary && (
                          <span className="block text-xs text-muted mt-0.5">{c.summary}</span>
                        )}
                      </span>
                    </button>
                  ))}
                  <button
                    type="button"
                    onClick={() => setIdentity((s) => ({ ...s, [group.name]: NEW_ENTITY }))}
                    disabled={answering}
                    className={optionClass(identity[group.name] === NEW_ENTITY)}
                  >
                    {radio(identity[group.name] === NEW_ENTITY)}
                    <span className="flex items-center gap-1.5 text-sm font-medium text-ink">
                      <UserPlus size={14} />
                      {group.identity.candidates.length === 1
                        ? 'No, someone different'
                        : 'Someone new, not listed'}
                    </span>
                  </button>

                  {/* A DIFFERENT PERSON WITH THE SAME NAME NEEDS A DIFFERENT
                      NAME. The merge key is the name, so without one the
                      archive stores "the same person" — which is how a single
                      row came to hold both an uncle and an army friend.
                      Only when a candidate's name matches exactly: a bare
                      "משה" that is not "משה כהן" already has its own key. */}
                  {identity[group.name] === NEW_ENTITY &&
                    group.identity.candidates.some(
                      (c) => c.name.trim() === group.name.trim(),
                    ) && (
                      <div className="flex flex-col gap-1 pl-1">
                        <label
                          htmlFor={`distinct-${group.name}`}
                          className="text-xs text-muted"
                        >
                          What should I call this one, to tell them apart?
                        </label>
                        <input
                          id={`distinct-${group.name}`}
                          type="text"
                          dir="auto"
                          value={distinctNames[group.name] ?? ''}
                          onChange={(e) =>
                            setDistinctNames((current) => ({
                              ...current,
                              [group.name]: e.target.value,
                            }))
                          }
                          disabled={answering}
                          placeholder={`e.g. ${group.name} ...`}
                          className="w-64 px-3 py-1.5 rounded-lg bg-surface-800 border border-edge text-sm text-ink placeholder:text-muted2"
                        />
                        <span className="text-[11px] text-muted2">
                          Required — otherwise they would be saved as the same person.
                        </span>
                      </div>
                    )}
                </fieldset>
              )}

              {/* TYPE. Always exactly two options, because the extractor
                  reports the runner-up it was torn between rather than a
                  confidence score. */}
              {group.type && (
                <fieldset className="flex flex-col gap-2">
                  <legend className="text-sm text-ink leading-relaxed mb-1">
                    {group.type.question}
                  </legend>
                  <div className="flex gap-2">
                    {[group.type.type, group.type.alternative_type].map((option) => (
                      <button
                        key={option}
                        type="button"
                        onClick={() => setTypes((s) => ({ ...s, [group.name]: option }))}
                        disabled={answering}
                        className={`${optionClass(types[group.name] === option)} flex-1 items-center`}
                      >
                        {radio(types[group.name] === option)}
                        <span className="text-sm font-medium text-ink capitalize">{option}</span>
                      </button>
                    ))}
                  </div>
                </fieldset>
              )}

              {/* RELATIONS about this person. Anchored to the end that is not
                  the producer, so a relation appears once rather than on both
                  people's cards. Optional, unlike the two above. */}
              {group.relations.length > 0 && (
                <fieldset className="flex flex-col gap-2">
                  <legend className="text-sm text-ink leading-relaxed mb-1">
                    Did we get this right? — optional
                  </legend>
                  {group.relations.map((q) => {
                    // Only a sibling relation with the PRODUCER on one end is
                    // what a parentage answer can speak to. An aunt_uncle or a
                    // relation between two other people is untouched by it.
                    const overriddenByParentage =
                      parentageSaysNotSibling &&
                      q.relation_type === 'sibling' &&
                      (q.from_name === SELF || q.to_name === SELF)
                    const shown =
                      !overriddenByParentage &&
                      (Boolean(relations[q.index]) || Boolean(relationEdits[q.index]))
                    return (
                    <div key={`rel-${q.index}`} className="flex flex-col gap-1.5">
                      <button
                        type="button"
                        onClick={() => setRelations((s) => ({ ...s, [q.index]: !s[q.index] }))}
                        // A corrected proposal is already accepted; ticking it
                        // as well would say nothing more, and unticking could
                        // not take the correction back.
                        //
                        // Held too when the parentage answer has already
                        // decided this: a box that stays ticked while the
                        // server discards it is the silent contradiction this
                        // whole redesign exists to remove.
                        disabled={
                          answering || Boolean(relationEdits[q.index]) || overriddenByParentage
                        }
                        className={`${optionClass(shown)} ${
                          overriddenByParentage ? 'opacity-50' : ''
                        }`}
                      >
                        {radio(shown)}
                        <span className="flex flex-col gap-0.5 text-left">
                          <span className="text-sm font-medium text-ink">
                            <span dir="auto">{q.from_name === SELF ? 'You' : q.from_name}</span>
                            {' is the '}
                            {q.relation_type.replace(/_/g, ' ')}
                            {' of '}
                            <span dir="auto">{q.to_name === SELF ? 'you' : q.to_name}</span>
                          </span>
                          {q.evidence && (
                            <span dir="auto" className="text-xs text-muted">
                              &ldquo;{q.evidence}&rdquo;
                            </span>
                          )}
                        </span>
                      </button>

                      {/* Why it just went grey, said where it happened rather
                          than only under the parent picker below. */}
                      {overriddenByParentage && (
                        <span className="text-[11px] text-primary-300 pl-1">
                          Replaced by the parent you chose — <span dir="auto">{typedParent}</span>{' '}
                          isn&apos;t your parent, so {group.name} isn&apos;t your sibling.
                        </span>
                      )}

                      {canCorrect && !relationEdits[q.index] && !overriddenByParentage && (
                        <button
                          type="button"
                          disabled={answering}
                          onClick={() =>
                            setRelationEdits((current) => ({
                              ...current,
                              [q.index]: {
                                relation_type: q.relation_type,
                                from_name: q.from_name,
                                to_name: q.to_name,
                              },
                            }))
                          }
                          className="self-start text-xs text-primary-300 hover:text-primary-200 pl-1"
                        >
                          Not quite — fix this
                        </button>
                      )}

                      {relationEdits[q.index] && (
                        <div className="flex flex-col gap-2 px-3 py-2.5 rounded-xl border border-primary-500/30 bg-primary-500/5">
                          <div className="flex flex-wrap items-center gap-2 text-sm text-ink">
                            <PersonSelect
                              value={relationEdits[q.index].from_name}
                              people={correctionPeople}
                              disabled={answering}
                              label={`Who, in relation ${q.index + 1}`}
                              onChange={(name) =>
                                setRelationEdits((current) => ({
                                  ...current,
                                  [q.index]: { ...current[q.index], from_name: name },
                                }))
                              }
                            />
                            <span className="text-muted">is the</span>
                            <select
                              value={relationEdits[q.index].relation_type}
                              disabled={answering}
                              aria-label={`Relation ${q.index + 1}`}
                              onChange={(e) =>
                                setRelationEdits((current) => ({
                                  ...current,
                                  [q.index]: {
                                    ...current[q.index],
                                    relation_type: e.target.value,
                                  },
                                }))
                              }
                              className="px-2.5 py-1.5 rounded-lg bg-surface-800 border border-edge text-sm text-ink"
                            >
                              {correctionTypes.map((option) => (
                                <option key={option.value} value={option.value}>
                                  {option.label}
                                </option>
                              ))}
                            </select>
                            <span className="text-muted">of</span>
                            <PersonSelect
                              value={relationEdits[q.index].to_name}
                              people={correctionPeople}
                              disabled={answering}
                              label={`Of whom, in relation ${q.index + 1}`}
                              onChange={(name) =>
                                setRelationEdits((current) => ({
                                  ...current,
                                  [q.index]: { ...current[q.index], to_name: name },
                                }))
                              }
                            />
                          </div>
                          <div className="flex items-center gap-3">
                            <button
                              type="button"
                              disabled={answering}
                              onClick={() =>
                                setRelationEdits((current) => {
                                  const next = { ...current }
                                  delete next[q.index]
                                  return next
                                })
                              }
                              className="text-xs text-muted hover:text-ink"
                            >
                              Cancel this fix
                            </button>
                            {relationEdits[q.index].from_name ===
                              relationEdits[q.index].to_name && (
                              <span className="text-xs text-amber-300">
                                Pick two different people.
                              </span>
                            )}
                          </div>
                        </div>
                      )}
                    </div>
                    )
                  })}
                </fieldset>
              )}

              {/* PARENTAGE for this sibling. The grouped screen it replaces
                  asked about everyone at once; the bulk answer that made that
                  worth having still exists, at the bottom, because it is the
                  one action here that spans people. */}
              {group.parentageGroup && parents.length > 0 && (
                <fieldset className="flex flex-col gap-2">
                  <legend className="text-sm text-ink leading-relaxed mb-1">
                    Whose child are they?
                  </legend>
                  <div className="flex flex-wrap items-center gap-2">
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
                              [group.name]: ticked
                                ? shared.filter((n) => n !== parent.name)
                                : [...shared, parent.name],
                            }))
                          }
                          className={`px-2.5 py-1 rounded-lg border text-xs transition-colors ${
                            ticked
                              ? 'border-primary-400 bg-primary-500/15 text-white'
                              : 'border-edge bg-surface-800 text-ink-soft hover:border-edge-strong'
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
                          const opening = !current[group.name]
                          if (!opening) {
                            // Backing out must not leave a stale name to be
                            // submitted for a path that was abandoned.
                            setNewParent((n) => ({ ...n, [group.name]: '' }))
                          }
                          return { ...current, [group.name]: opening }
                        })
                      }
                      className={`px-2.5 py-1 rounded-lg border text-xs transition-colors ${
                        otherOpen[group.name]
                          ? 'border-primary-400 bg-primary-500/15 text-white'
                          : 'border-edge bg-surface-800 text-muted hover:border-edge-strong'
                      }`}
                    >
                      Someone else
                    </button>
                  </div>

                  {otherOpen[group.name] && (
                    <div className="flex flex-col gap-1">
                      <label
                        htmlFor={`other-parent-${group.name}`}
                        className="text-xs text-muted"
                      >
                        Then whose child are they?
                      </label>
                      <input
                        id={`other-parent-${group.name}`}
                        type="text"
                        dir="auto"
                        list="parentage-known-people"
                        value={newParent[group.name] ?? ''}
                        onChange={(e) =>
                          setNewParent((current) => ({
                            ...current,
                            [group.name]: e.target.value,
                          }))
                        }
                        disabled={answering}
                        placeholder="a name"
                        className="w-56 px-3 py-1.5 rounded-lg bg-surface-800 border border-edge text-sm text-ink placeholder:text-muted2"
                      />
                    </div>
                  )}

                  {shared.length === 0 && !typedParent && (
                    <span className="text-[11px] text-muted2">
                      Skipped — nothing recorded, and we won&apos;t ask again.
                    </span>
                  )}

                  {/* Naming someone who is NOT one of your parents means this
                      person shares no parent with you — so they are not your
                      sibling, and that relation is replaced rather than kept
                      to contradict this one. Said out loud, because a relation
                      disappearing without a word is how the last round of
                      confusion started. */}
                  {parentageSaysNotSibling && (
                    <span className="text-[11px] text-primary-300">
                      Then {group.name} isn&apos;t your sibling — we&apos;ll record them
                      as <span dir="auto">{typedParent}</span>&apos;s child instead.
                    </span>
                  )}
                </fieldset>
              )}

              {/* SIDE. Which parent an aunt or uncle is a sibling of — the
                  edge that puts them beside the right parent instead of
                  floating in the parents' row. */}
              {group.sideGroup && sideParents.length > 0 && (
                <fieldset className="flex flex-col gap-2">
                  {/* The same question for both kinds — which of your parents
                      is this person on the side of — but asked in the words
                      that fit. A grandparent is that parent's PARENT; an aunt
                      or uncle is their SIBLING. */}
                  <legend className="text-sm text-ink leading-relaxed mb-1">
                    {group.sideKind === 'grandparent'
                      ? 'Whose mother or father are they?'
                      : 'Whose brother or sister are they?'}
                  </legend>
                  <div className="flex flex-wrap items-center gap-2">
                    {sideParents.map((parent) => {
                      const chosen = sides[group.name] === parent.name
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
                              [group.name]: chosen ? '' : parent.name,
                            }))
                          }
                          className={`px-2.5 py-1 rounded-lg border text-xs transition-colors ${
                            chosen
                              ? 'border-primary-400 bg-primary-500/15 text-white'
                              : 'border-edge bg-surface-800 text-ink-soft hover:border-edge-strong'
                          }`}
                        >
                          <span dir="auto">{parent.name}</span>
                        </button>
                      )
                    })}
                  </div>
                  {!sides[group.name] && (
                    <span className="text-[11px] text-muted2">
                      Not sure — we won&apos;t ask again
                    </span>
                  )}
                </fieldset>
              )}
            </section>
          )
        })}

        {/* The one action that spans people, and the reason the grouped
            parentage screen was worth having: "yes, all of them" is the
            common answer, and making it one click per sibling is how a
            screen gets skipped wholesale. It stays whole, below the cards
            it fills in. */}
        {parentageQuestions.map((group) => {
          const siblings = group.siblings ?? []
          const parents = group.parents ?? []
          if (siblings.length === 0 || parents.length === 0) return null
          const allShared = siblings.every(
            (sibling) => (parentage[sibling.name] ?? []).length === parents.length,
          )
          return (
            <div
              key="parentage-bulk"
              className="flex flex-wrap items-center justify-between gap-3 px-4 py-3 rounded-xl border border-edge bg-surface-800/60"
            >
              <p dir="auto" className="text-xs text-muted min-w-0">
                {group.question}
              </p>
              <button
                type="button"
                disabled={answering}
                onClick={() =>
                  setParentage((current) => {
                    const next = { ...current }
                    for (const sibling of siblings) {
                      next[sibling.name] = allShared ? [] : parents.map((p) => p.name)
                    }
                    return next
                  })
                }
                className={`shrink-0 px-4 py-2 rounded-lg border text-sm transition-colors ${
                  allShared
                    ? 'border-primary-400 bg-primary-500/15 text-white'
                    : 'border-edge bg-surface-800 text-ink-soft hover:border-edge-strong'
                }`}
              >
                {allShared ? 'Yes — all of them' : 'Yes — all of them'}
              </button>
            </div>
          )
        })}

        {/* Picking beats typing: a typed name resolves by normalised match,
            so one different character makes a second person instead of
            linking to the first. Shared by every card's "someone else". */}
        <datalist id="parentage-known-people">
          {(parentageQuestions[0]?.known_people ?? []).map((person) => (
            <option key={person.name} value={person.name} />
          ))}
        </datalist>

        {/* Everything else the recording named. Not questions — these are
            here in case the extractor misheard, which it can do with complete
            confidence: "ליאן" for "אליאן" raises nothing to answer, because a
            brand-new name has nothing similar to disambiguate against. */}
        {nameOnly.length > 0 && (
          <fieldset className="flex flex-col gap-2 pt-1 border-t border-edge">
            <legend className="text-sm text-ink leading-relaxed mb-1">
              Also picked up
            </legend>
            <p className="text-xs text-muted -mt-1 mb-1">
              Nothing to answer here — fix any name that was misheard.
            </p>
            <div className="flex flex-wrap gap-2">
              {nameOnly.map((group) => {
                const value = nameEdits[group.name] ?? group.name
                const changed = value.trim() !== group.name
                return (
                  <div key={`only-${group.name}`} className="flex flex-col gap-0.5">
                    <input
                      type="text"
                      dir="auto"
                      value={value}
                      onChange={(e) =>
                        setNameEdits((s) => ({ ...s, [group.name]: e.target.value }))
                      }
                      disabled={answering}
                      aria-label={`Name: ${group.name}`}
                      className={`w-36 px-3 py-1.5 rounded-lg bg-surface-800 border text-sm text-ink ${
                        changed ? 'border-primary-400' : 'border-edge'
                      }`}
                    />
                    {changed && (
                      <span className="text-[10px] text-primary-300">
                        was &ldquo;{group.name}&rdquo;
                      </span>
                    )}
                  </div>
                )
              })}
            </div>
          </fieldset>
        )}

        <div className="flex items-center justify-between gap-3 pt-1">
          <span className="text-xs text-muted">
            {requiredCount === 0
              ? // Nothing is compulsory here — saying "0 of 0 answered" reads
                // as an error, and saying "All answered" claims something the
                // producer has not done.
                'Everything here is optional'
              : allAnswered
                ? 'All answered'
                : // Every question can be answered and the button still be
                  // disabled — a missing distinguishing name is not a question
                  // and would otherwise read as "3 of 3 answered" beside a
                  // dead button.
                  missingDistinctName && answeredCount === requiredCount
                  ? 'Needs a name to tell them apart'
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
