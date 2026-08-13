'use client'

import { useEffect } from 'react'
import { Loader2 } from 'lucide-react'

/**
 * The dedicated /talk page is GONE — family members use the regular app
 * shell (docs/FAMILY_UNIFIED_SHELL_PLAN.md). This stub exists only so
 * invite links shared before the move (they carry a 7-day TTL) and old
 * bookmarks land somewhere real: it forwards to the shell, preserving the
 * invite token.
 */
export default function TalkRedirect() {
  useEffect(() => {
    const invite = new URLSearchParams(window.location.search).get('invite')
    window.location.replace(invite ? `/?invite=${encodeURIComponent(invite)}` : '/')
  }, [])
  return (
    <div className="min-h-screen flex items-center justify-center">
      <Loader2 size={26} className="animate-spin text-primary-400" />
    </div>
  )
}
