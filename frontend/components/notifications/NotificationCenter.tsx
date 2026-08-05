'use client'

import { useEffect, useRef, useState } from 'react'
import { toast } from 'react-hot-toast'
import { NotificationBell } from '@/components/notifications/NotificationBell'
import { NotificationsPage } from '@/components/notifications/NotificationsPage'
import { EntityConfirmModal } from '@/components/record/EntityConfirmModal'
import { usePendingConfirmations } from '@/components/providers/PendingConfirmationsProvider'
import { useNotifications, type NotificationItem } from '@/lib/notifications'

/**
 * The bell, its dropdown, the full-screen page, and whatever a row opens.
 *
 * All of it lives here so the shell mounts one component and holds none of
 * this state. It is also the ONLY place that switches on a notification's
 * `kind`: the list components render items without knowing what they are, and
 * a second kind of notification is a builder in `lib/notifications.ts` plus a
 * branch below. Nothing else moves.
 */
export function NotificationCenter({
  /** The producer is in the recording flow. Drives two things and nothing
   *  else: the once-ever auto-open is suppressed while it is true, and LEAVING
   *  it is what fires the nudge. The bell itself is always available. */
  onRecordScreen = false,
}: {
  onRecordScreen?: boolean
}) {
  const items = useNotifications()
  const { autoOpenPending, markAutoOpened } = usePendingConfirmations()
  const [dropdownOpen, setDropdownOpen] = useState(false)
  const [pageOpen, setPageOpen] = useState(false)
  const [active, setActive] = useState<NotificationItem | null>(null)
  // Reopen the dropdown after answering, but only if anything is left. Set
  // when a row is opened FROM the dropdown; the full-screen page needs no
  // equivalent because it stays open underneath.
  const [returnToDropdown, setReturnToDropdown] = useState(false)

  /**
   * Show the list once, ever (decision 16.C) — the dropdown, not a question.
   *
   * Removing the popup removed the only thing that ever put these in front of
   * anyone, and a badge nobody has been taught to look at is not a
   * replacement. Opening the dropdown points at the bell it hangs from, which
   * is the thing that has to be learned; opening a question would teach
   * nothing about where questions now live.
   */
  useEffect(() => {
    if (onRecordScreen || !autoOpenPending) return
    setDropdownOpen(true)
    markAutoOpened()
  }, [onRecordScreen, autoOpenPending, markAutoOpened])

  /**
   * The nudge (§13.4): leaving the record screen with things waiting.
   *
   * Non-blocking and never a modal — the producer has just finished a stretch
   * of recording and interrupting them at the door is how a prompt becomes
   * something to dismiss without reading. It is a toast that says how much is
   * waiting and offers to show it.
   *
   * Only on leaving `/record` (decision 16.B). Firing on any navigation was
   * the alternative and it becomes wallpaper: this is the one transition where
   * the producer has demonstrably just generated the things that are waiting.
   *
   * Deliberately NOT fired when the list is about to open by itself — the
   * once-ever auto-open is the stronger signal, and both at once is two
   * interruptions for one event.
   */
  const wasOnRecordScreen = useRef(onRecordScreen)
  useEffect(() => {
    const leaving = wasOnRecordScreen.current && !onRecordScreen
    wasOnRecordScreen.current = onRecordScreen
    if (!leaving || items.length === 0) return
    if (autoOpenPending || dropdownOpen || pageOpen || active) return
    toast(
      t => (
        <span className="flex items-center gap-3">
          <span className="text-sm">
            {items.length === 1
              ? 'One thing is waiting for you.'
              : `${items.length} things are waiting for you.`}
          </span>
          <button
            type="button"
            onClick={() => {
              toast.dismiss(t.id)
              setDropdownOpen(true)
            }}
            className="text-sm font-semibold text-primary-400 hover:text-primary-300 shrink-0"
          >
            Show me
          </button>
        </span>
      ),
      // One id, so bouncing in and out of the record screen replaces the
      // toast rather than stacking a column of them.
      { id: 'pending-nudge', duration: 8000 },
    )
  }, [onRecordScreen, items.length, autoOpenPending, dropdownOpen, pageOpen, active])

  // Nothing left to show. The dropdown is transient and closes; the page is
  // somewhere the producer navigated to and keeps its empty state.
  useEffect(() => {
    if (items.length === 0) setDropdownOpen(false)
  }, [items.length])

  const openFromDropdown = (item: NotificationItem) => {
    setActive(item)
    // Closed rather than left open under the popup: a dropdown dismisses on
    // click-away, and every click inside the popup is a click away from it.
    setDropdownOpen(false)
    setReturnToDropdown(true)
  }

  /**
   * Back to the list once the popup closes — unless answering emptied it.
   *
   * An effect rather than a line in the close handler, deliberately. The
   * handler runs having just awaited the refresh, but the `items` it can see
   * is whatever the last render captured, which may still contain the
   * recording that was just answered. Deciding there means reopening a
   * dropdown that immediately closes itself. An effect sees the settled list
   * and decides once.
   *
   * Also covers closing WITHOUT answering: dismissing the popup should put the
   * producer back where they chose from, not on whatever was behind it.
   */
  useEffect(() => {
    if (active || !returnToDropdown) return
    if (items.length > 0) setDropdownOpen(true)
    setReturnToDropdown(false)
  }, [active, returnToDropdown, items.length])

  return (
    <>
      <NotificationBell
        items={items}
        open={dropdownOpen}
        onOpenChange={setDropdownOpen}
        onSelect={openFromDropdown}
        onSeeAll={() => {
          setDropdownOpen(false)
          setPageOpen(true)
        }}
      />

      {pageOpen && (
        <NotificationsPage
          items={items}
          onSelect={setActive}
          onClose={() => setPageOpen(false)}
        />
      )}

      {/* What a row opens. The only `kind` switch in the feature. */}
      {active?.kind === 'entity_confirmation' && (
        <EntityConfirmModal segmentId={active.target} onClose={() => setActive(null)} />
      )}
    </>
  )
}
