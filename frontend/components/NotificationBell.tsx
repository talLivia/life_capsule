'use client'

import { Bell } from 'lucide-react'
import { usePendingConfirmations } from '@/components/providers/PendingConfirmationsProvider'

/**
 * Outstanding confirmation questions, reachable from every screen.
 *
 * Replaces a popup that interrupted recording. The trade is deliberate: the
 * popup guaranteed an answer but broke the flow, and this guarantees the flow
 * but not the answer — so the badge has to be visible enough that questions do
 * not silently accumulate forever.
 *
 * The count comes from the shared provider, never from local state. It counts
 * RECORDINGS with questions outstanding, not individual questions: answering
 * is per recording, one screen at a time, so that is the number that matches
 * what clicking it opens.
 */
export function NotificationBell({ onOpen }: { onOpen: () => void }) {
  const { count } = usePendingConfirmations()

  return (
    <button
      type="button"
      onClick={onOpen}
      aria-label={
        count === 0
          ? 'No questions waiting'
          : `${count} recording${count === 1 ? '' : 's'} with questions waiting`
      }
      className="relative p-2 rounded-lg text-gray-400 hover:text-white transition-colors"
    >
      <Bell size={18} />
      {count > 0 && (
        <span className="absolute -top-0.5 -right-0.5 min-w-[18px] h-[18px] px-1 rounded-full bg-primary-500 text-[11px] font-semibold text-white flex items-center justify-center">
          {count > 9 ? '9+' : count}
        </span>
      )}
    </button>
  )
}
