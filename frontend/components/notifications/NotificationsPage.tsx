'use client'

import { useEffect } from 'react'
import { Bell, X } from 'lucide-react'
import { NotificationList } from '@/components/notifications/NotificationList'
import type { NotificationItem } from '@/lib/notifications'

/**
 * Every notification, with room to read them.
 *
 * The same list as the dropdown, deliberately — it exists because
 * notifications are meant to be a general surface rather than a
 * confirmation-questions surface, and a full screen is where a second and
 * third kind will still be legible. It is not a second implementation of the
 * list, and must not become one.
 *
 * Unlike the dropdown it does NOT close itself when the last item goes: it is
 * somewhere the producer navigated to, and a page that vanishes when you
 * finish the thing you came to do reads as a bug. It shows the empty state.
 */
export function NotificationsPage({
  items,
  onSelect,
  onClose,
}: {
  items: NotificationItem[]
  onSelect: (item: NotificationItem) => void
  onClose: () => void
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  return (
    <div
      className="fixed inset-0 z-[70] bg-surface-900/95 backdrop-blur-xl animate-fade-in overflow-y-auto"
      role="dialog"
      aria-modal="true"
      aria-labelledby="notifications-heading"
    >
      <div className="max-w-2xl mx-auto px-6 py-10">
        <div className="flex items-center justify-between gap-4 mb-6">
          <div className="flex items-center gap-2.5">
            <Bell size={20} className="text-primary-400" />
            <h1 id="notifications-heading" className="text-xl font-bold text-white">
              Notifications
            </h1>
            {items.length > 0 && (
              <span className="text-sm text-gray-500">
                {items.length} waiting
              </span>
            )}
          </div>
          <button onClick={onClose} className="btn-icon shrink-0" aria-label="Close">
            <X size={16} />
          </button>
        </div>

        <div className="glass-card overflow-hidden">
          <NotificationList items={items} onSelect={onSelect} roomy />
        </div>
      </div>
    </div>
  )
}
