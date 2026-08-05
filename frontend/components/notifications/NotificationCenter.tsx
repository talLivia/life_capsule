'use client'

import { useEffect, useState } from 'react'
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
  /** The producer is in the middle of the recording flow. Suppresses the
   *  once-ever auto-open only — the bell itself is always available. */
  suppressAutoOpen = false,
}: {
  suppressAutoOpen?: boolean
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
    if (suppressAutoOpen || !autoOpenPending) return
    setDropdownOpen(true)
    markAutoOpened()
  }, [suppressAutoOpen, autoOpenPending, markAutoOpened])

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
