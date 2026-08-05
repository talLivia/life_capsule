import { HelpCircle, type LucideIcon } from 'lucide-react'
import { usePendingConfirmations } from '@/components/providers/PendingConfirmationsProvider'
import { countQuestions } from '@/lib/pendingQuestions'
import type { PendingConfirmation } from '@/lib/types'

/**
 * Everything waiting for the producer, as ONE list of self-describing items.
 *
 * Pending confirmation questions are the only kind today. They are
 * deliberately not the shape of this module: a notification carries its own
 * words, its own icon and its own count, so the surfaces that render it — the
 * bell's dropdown and the full-screen page — know nothing about recordings,
 * segments or question classes. Adding a second kind is a builder function and
 * one line in `useNotifications`; neither list component changes, which is the
 * same bargain `countQuestions` makes with the six question classes.
 *
 * What a surface may rely on: `id`, `title`, `detail`, `count`, `icon`. What
 * it may NOT do is switch on `kind` to decide how to draw a row. `kind` and
 * `target` exist for the one thing a list genuinely cannot generalise — what
 * opens when the row is clicked — and that decision lives in exactly one place
 * (`NotificationCenter`).
 */
export interface NotificationItem {
  /** Unique across every kind, so a mixed list needs no compound React keys. */
  id: string
  /** What this is, for the host deciding what to OPEN. Never for styling. */
  kind: 'entity_confirmation'
  /** The id of the thing `kind` opens — here, the recording. */
  target: string
  /** One line: what is being asked of the producer. */
  title: string
  /** Which thing it is about. For a recording, the interview question it
   *  answers — the only thing that identifies it once time has passed, and
   *  the same reasoning as the evidence phrase in §12. */
  detail?: string
  /** How many things this row covers. Rendered as a pill; omit for kinds
   *  where a count means nothing. */
  count?: number
  icon: LucideIcon
}

/** Pending confirmation questions -> notifications. One row per RECORDING,
 *  because that is the unit answering happens in — one screen, one submit. */
export function entityConfirmationNotifications(
  pending: PendingConfirmation[],
): NotificationItem[] {
  return pending.map(item => {
    const count = countQuestions(item.pending_confirmation)
    return {
      id: `entity_confirmation:${item.segment_id}`,
      kind: 'entity_confirmation' as const,
      target: item.segment_id,
      title: count === 1 ? 'One thing to check' : `${count} things to check`,
      detail: item.question_asked,
      count,
      icon: HelpCircle,
    }
  })
}

/**
 * The whole notification list, from every source.
 *
 * The single place a second kind is added: build it and concatenate. Ordering
 * across kinds will need deciding when there is more than one — today the
 * server's order is the only order there is.
 */
export function useNotifications(): NotificationItem[] {
  const { items } = usePendingConfirmations()
  return entityConfirmationNotifications(items)
}
