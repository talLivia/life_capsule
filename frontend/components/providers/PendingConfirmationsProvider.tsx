'use client'

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
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

const IDLE_POLL_MS = 8000
/** While a recording is being processed, questions can appear at any moment
 *  and the producer is watching. Same reasoning as the extraction screen. */
const ACTIVE_POLL_MS = 2000

interface PendingContext {
  items: PendingConfirmation[]
  count: number
  /** Fetch now — after answering, or after a recording is ingested. */
  refresh: () => Promise<void>
  /** Poll faster until things settle. */
  setActive: (active: boolean) => void
  /** Has the producer ever been shown the bell's contents automatically?
   *  Once, so the mechanism is discovered; never again, so it is not a
   *  recurring interruption. */
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
  const [active, setActive] = useState(false)
  const [seen, setSeen] = useState(true) // assume seen until storage says otherwise
  const answering = useRef(false)

  useEffect(() => {
    setSeen(window.localStorage.getItem(SEEN_KEY) === '1')
  }, [])

  const refresh = useCallback(async () => {
    if (answering.current) return
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
    const id = setInterval(tick, active ? ACTIVE_POLL_MS : IDLE_POLL_MS)
    return () => {
      cancelled = true
      clearInterval(id)
    }
  }, [refresh, active])

  const markAutoOpened = useCallback(() => {
    window.localStorage.setItem(SEEN_KEY, '1')
    setSeen(true)
  }, [])

  const value = useMemo<PendingContext>(
    () => ({
      items,
      count: items.length,
      refresh,
      setActive,
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
