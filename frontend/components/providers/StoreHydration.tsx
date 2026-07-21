'use client'

import { useEffect } from 'react'
import { useStore } from '@/store/useStore'

/**
 * Triggers zustand's persist rehydration exactly once, client-side, after
 * mount. Paired with useStore's `skipHydration: true` — without both halves
 * of this, the store rehydrates from localStorage as soon as its module
 * loads (before React's first hydration pass), so the client's first render
 * already reflects the real persisted state while the server (no
 * localStorage) rendered with the plain defaults, which Next.js reports as
 * a hydration mismatch on whatever first branches on that state.
 *
 * Renders nothing — mount this once near the app root (see app/layout.tsx).
 */
export function StoreHydration() {
  useEffect(() => {
    useStore.persist.rehydrate()
  }, [])

  return null
}
