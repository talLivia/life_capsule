import type { PendingConfirmation } from '@/lib/types'

/** Keys in the pending payload that are NOT questions. Everything else in it
 *  is one, counted generically so that a class added on the server appears
 *  here without this file being edited — the omission that caused the bug
 *  below could not then happen again. */
const NON_QUESTION_KEYS = new Set(['editable_entities'])

/**
 * How many questions one recording raises, of ANY class.
 *
 * The bug this replaces: the render guard was `totalCount === 0`, where
 * totalCount counted identity and type ONLY. A recording with ten relation
 * questions, ten year questions and ten editable names — fetched, in state,
 * ready to render — returned null and showed nothing at all, because the two
 * oldest classes happened to be empty. Exactly the same omission as the graph
 * router that skipped confirmation entirely, one layer up.
 *
 * It lives here, alone, because it now has TWO callers: the popup deciding
 * whether it has anything to show, and the notification row saying how many
 * things need checking. Those two numbers disagreeing is the same class of bug
 * as a count that disagrees with the list it opens — so there is one function,
 * not a second implementation that has to be remembered.
 */
export function countQuestions(
  payload: PendingConfirmation['pending_confirmation'] | undefined,
): number {
  return Object.entries(payload ?? {}).reduce(
    (total, [key, value]) =>
      NON_QUESTION_KEYS.has(key) || !Array.isArray(value) ? total : total + value.length,
    0,
  )
}
