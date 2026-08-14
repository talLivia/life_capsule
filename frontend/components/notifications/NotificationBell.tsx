'use client'

import { useEffect, useRef } from 'react'
import { Bell } from 'lucide-react'
import { NotificationList } from '@/components/notifications/NotificationList'
import type { NotificationItem } from '@/lib/notifications'

/**
 * Outstanding items, reachable from every screen.
 *
 * Replaces a popup that interrupted recording. The trade is deliberate: the
 * popup guaranteed an answer but broke the flow, and this guarantees the flow
 * but not the answer — so the badge has to be visible enough that things do
 * not silently accumulate forever.
 *
 * Clicking it opens a LIST, never a question. Going straight into the first
 * pending recording made the bell a shortcut to one arbitrary item and gave no
 * sense of what else was waiting or which recording was about to be discussed;
 * an item is chosen, not served up.
 *
 * The count counts ITEMS, which today is one per recording with questions
 * outstanding rather than one per question — answering happens per recording,
 * so that is the number matching what clicking it opens.
 */
export function NotificationBell({
  items,
  open,
  onOpenChange,
  onSelect,
  onSeeAll,
}: {
  items: NotificationItem[]
  open: boolean
  onOpenChange: (open: boolean) => void
  onSelect: (item: NotificationItem) => void
  onSeeAll: () => void
}) {
  const count = items.length
  const wrapper = useRef<HTMLDivElement>(null)

  // Click-away and Escape. A dropdown that can only be dismissed by the
  // control that opened it is a dropdown people leave open.
  useEffect(() => {
    if (!open) return
    const onDown = (e: MouseEvent) => {
      if (!wrapper.current?.contains(e.target as Node)) onOpenChange(false)
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onOpenChange(false)
    }
    document.addEventListener('mousedown', onDown)
    window.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDown)
      window.removeEventListener('keydown', onKey)
    }
  }, [open, onOpenChange])

  return (
    <div className="relative" ref={wrapper}>
      <button
        type="button"
        onClick={() => onOpenChange(!open)}
        aria-label={
          count === 0 ? 'No notifications' : `${count} notification${count === 1 ? '' : 's'}`
        }
        aria-expanded={open}
        aria-haspopup="menu"
        className="relative p-2 rounded-lg text-muted hover:text-ink transition-colors"
      >
        <Bell size={18} />
        {count > 0 && (
          <span className="absolute -top-0.5 -right-0.5 min-w-[18px] h-[18px] px-1 rounded-full bg-primary-500 text-[11px] font-semibold text-white flex items-center justify-center">
            {count > 9 ? '9+' : count}
          </span>
        )}
      </button>

      {open && (
        <div
          role="menu"
          className="absolute right-0 top-full mt-2 w-[min(22rem,calc(100vw-2rem))] z-50
            modal-card overflow-hidden animate-fade-in flex flex-col"
        >
          <div className="px-4 py-2.5 border-b border-edge flex items-center justify-between gap-3">
            <span className="text-xs uppercase tracking-wide font-semibold text-muted">
              Notifications
            </span>
            {count > 0 && <span className="text-[11px] text-muted2">{count} waiting</span>}
          </div>

          {/* Bounded, because this is a dropdown and not the page. Anything
              past a few rows is what "See all" is for. */}
          <div className="max-h-[min(60vh,24rem)] overflow-y-auto messages-scroll">
            <NotificationList items={items} onSelect={onSelect} />
          </div>

          <button
            type="button"
            onClick={onSeeAll}
            className="px-4 py-2.5 border-t border-edge text-xs font-medium
              text-primary-300 hover:text-primary-200 hover:bg-veil transition-colors"
          >
            See all
          </button>
        </div>
      )}
    </div>
  )
}
