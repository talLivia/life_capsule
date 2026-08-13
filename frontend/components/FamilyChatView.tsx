'use client'

import { useEffect, useState } from 'react'
import { Heart, Loader2 } from 'lucide-react'
import { TalkInterface } from '@/components/talk/TalkInterface'
import { VideoClipTalkInterface } from '@/components/talk/VideoClipTalkInterface'
import { api } from '@/lib/api'
import type { ApiError, TalkAvailability } from '@/lib/types'

/**
 * The family member's Chat view inside the regular app shell — everything
 * the dedicated /talk page used to do after its auth/redeem gates
 * (docs/FAMILY_UNIFIED_SHELL_PLAN.md §2.2): load availability, hold on
 * "still preparing" until the archive is ready, then render whichever chat
 * the LINKED PRODUCER's mode selects. The interfaces themselves are the
 * unchanged /talk components (calm theme and all) — the page around them
 * moved, the experience did not.
 */
export function FamilyChatView() {
  const [availability, setAvailability] = useState<TalkAvailability | null>(null)
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState<string | null>(null)

  const loadAvailability = async () => {
    setLoading(true)
    setLoadError(null)
    try {
      const data: TalkAvailability = await api.getTalkAvailability()
      setAvailability(data)
    } catch (err: unknown) {
      const detail = (err as ApiError)?.response?.data?.detail || (err as ApiError)?.message
      setLoadError(detail || 'Could not check availability')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    loadAvailability()
  }, [])

  if (loading) {
    return (
      <div className="min-h-[60vh] flex items-center justify-center">
        <Loader2 size={26} className="animate-spin text-calm-sage-600" />
      </div>
    )
  }

  if (loadError || !availability) {
    return (
      <div className="min-h-[60vh] flex items-center justify-center px-6">
        <div className="max-w-sm text-center flex flex-col items-center gap-4">
          <p className="text-sm text-gray-300">{loadError || 'Something went wrong'}</p>
          <button onClick={loadAvailability} className="btn-primary">
            Try again
          </button>
        </div>
      </div>
    )
  }

  const stillPreparing = (
    <div className="min-h-[60vh] flex items-center justify-center px-6">
      <div className="max-w-sm text-center flex flex-col items-center gap-4">
        <Heart size={28} className="text-calm-sage-600 dark:text-calm-sage-300" />
        <h1 className="text-lg font-semibold text-gray-200">
          {availability.producer_name} is still preparing their stories
        </h1>
        <p className="text-sm text-gray-400">
          Check back soon — you&apos;ll be able to talk with them here once they&apos;ve
          recorded some memories.
        </p>
        <button onClick={loadAvailability} className="btn-secondary">
          Check again
        </button>
      </div>
    </div>
  )

  // Mode-aware server-side: v2 needs only a ready recording; avatar mode
  // also needs a ready avatar (V2_PRIMARY_AVATAR_DORMANT_PLAN §3.4).
  if (!availability.available) return stillPreparing

  // The PRODUCER's own setting picks which chat renders — every family
  // member talking to this producer gets the same mode.
  if (availability.chat_mode === 'video_clips_v2') {
    return <VideoClipTalkInterface producerName={availability.producer_name} />
  }

  // Avatar mode renders the avatar itself; `available` implies one exists,
  // and the guard keeps an inconsistent response honest instead of crashing.
  if (!availability.avatar_id) return stillPreparing

  return (
    <TalkInterface
      avatarId={availability.avatar_id}
      avatarImageUrl={availability.avatar_image_url}
      producerName={availability.producer_name}
    />
  )
}
