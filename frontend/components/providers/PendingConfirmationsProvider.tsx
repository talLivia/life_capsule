'use client'

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from 'react'
import { api } from '@/lib/api'
import type { PendingConfirmation } from '@/lib/types'

/**
 * The ONE poller for pending confirmation questions.
 *
 * Everything that needs to know about outstanding questions reads this: the
 * bell's badge, the list that answers them, and the nudge. Two components
 * polling the same endpoint is how two counts drift apart, and a count that
 * disagrees with the list it opens is worse than no count.
 *
 * A poll rather than a WebSocket, deliberately. The endpoint, its auth and its
 * shape already exist and are proven; a badge that is a few seconds stale
 * costs nothing; and a poll survives sleep, navigation and reconnection with
 * no reconnection logic. A socket would be a second transport carrying one
 * integer.
 *
 * The count is always DERIVED from a live fetch, never cached across a
 * refresh. `pending_confirmation` lives on the segment row, so deleting a
 * recording deletes its questions — a cached badge would keep counting things
 * that no longer exist.
 */

/**
 * One cadence, because nobody is ever waiting on this.
 *
 * There was a 2s "a recording is in flight" mode here, on the reasoning that
 * questions can appear at any moment and the producer is watching. They are
 * not, by design: finishing a recording goes straight to the next question and
 * the extraction screen no longer opens, so nothing on screen is waiting for
 * this badge to change. A badge that appears a few seconds later is invisible;
 * a second cadence that has to be turned on and off by whichever screen
 * happens to know is not.
 */
const POLL_MS = 8000

interface PendingContext {
  items: PendingConfirmation[]
  /** No `count` here. The badge counts NOTIFICATIONS, of which pending
   *  confirmations are currently the only kind — a count published from this
   *  source would be right today and quietly wrong the moment there is a
   *  second one. See `lib/notifications.ts`. */
  /** Fetch now — after answering. */
  refresh: () => Promise<void>
  /** Is there anything the producer has never been shown?
   *
   *  True once per producer — the mechanism has to be discovered, and after
   *  that it is a recurring interruption. WHEN to act on it is the caller's
   *  call, not this provider's: the one moment it must not fire is over
   *  someone part-way through recording an answer, and only the screen knows
   *  that. See `app/page.tsx`. */
  autoOpenPending: boolean
  markAutoOpened: () => void
}

const Context = createContext<PendingContext | null>(null)

const SEEN_KEY = 'lc.pendingBellSeen'

export function PendingConfirmationsProvider({
  children,
}: {
  children: React.ReactNode
}) {
  const [items, setItems] = useState<PendingConfirmation[]>([])
  const [seen, setSeen] = useState(true) // assume seen until storage says otherwise

  useEffect(() => {
    setSeen(window.localStorage.getItem(SEEN_KEY) === '1')
  }, [])

  // Polls unconditionally. There was a guard here meant to hold the list
  // still while someone was answering, but nothing ever set it — and a poll
  // landing mid-answer is harmless anyway, because the list is only ever read
  // through a PINNED segment id (see EntityConfirmModal). A guard that looks
  // like protection and is not is worse than neither.
  const refresh = useCallback(async () => {
    try {
      setItems(await api.getPendingConfirmations())
    } catch {
      /* a dropped poll is not worth surfacing — the next one either succeeds
         or the producer is offline and already knows */
    }
  }, [])

  useEffect(() => {
    let cancelled = false
    const tick = async () => {
      if (cancelled) return
      await refresh()
    }
    tick()
    const id = setInterval(tick, POLL_MS)
    return () => {
      cancelled = true
      clearInterval(id)
    }
  }, [refresh])

  const markAutoOpened = useCallback(() => {
    window.localStorage.setItem(SEEN_KEY, '1')
    setSeen(true)
  }, [])

  const value = useMemo<PendingContext>(
    () => ({
      items,
      refresh,
      autoOpenPending: !seen && items.length > 0,
      markAutoOpened,
    }),
    [items, refresh, seen, markAutoOpened]
  )

  return <Context.Provider value={value}>{children}</Context.Provider>
}

export function usePendingConfirmations(): PendingContext {
  const context = useContext(Context)
  if (!context) {
    throw new Error('usePendingConfirmations must be used inside its provider')
  }
  return context
}
