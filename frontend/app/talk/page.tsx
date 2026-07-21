'use client'

import { Suspense, useEffect, useState } from 'react'
import { useSearchParams } from 'next/navigation'
import { Gift, Heart, Loader2 } from 'lucide-react'
import { toast } from 'react-hot-toast'
import { AuthModal } from '@/components/AuthModal'
import { TalkInterface } from '@/components/talk/TalkInterface'
import { api } from '@/lib/api'
import { useStore } from '@/store/useStore'
import type { ApiError, TalkAvailability } from '@/lib/types'

function RedeemForm({
  initialToken,
  onRedeemed,
}: {
  initialToken?: string
  onRedeemed: () => void
}) {
  const { updateUser } = useStore()
  const [token, setToken] = useState(initialToken || '')
  const [loading, setLoading] = useState(false)

  const redeem = async (t: string) => {
    const trimmed = t.trim()
    if (!trimmed) return
    setLoading(true)
    try {
      const profile = await api.redeemFamilyInvite(trimmed)
      updateUser(profile)
      toast.success('You’re connected!', { icon: '💛' })
      onRedeemed()
    } catch (err: unknown) {
      const detail = (err as ApiError)?.response?.data?.detail || (err as ApiError)?.message
      toast.error(detail || 'That invite code did not work')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    if (initialToken) redeem(initialToken)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialToken])

  return (
    <div className="min-h-screen flex items-center justify-center bg-calm-paper dark:bg-calm-paperDark px-6">
      <div className="max-w-sm w-full text-center flex flex-col items-center gap-4">
        <Gift size={28} className="text-calm-sage-600 dark:text-calm-sage-300" />
        <h1 className="text-lg font-semibold text-calm-ink dark:text-calm-inkDark">
          Enter your invite code
        </h1>
        <p className="text-sm text-calm-inkmuted dark:text-calm-inkmutedDark">
          Ask the person who invited you for the link or code they shared with you.
        </p>
        <input
          value={token}
          onChange={(e) => setToken(e.target.value)}
          placeholder="Paste your invite code"
          className="w-full px-4 py-3 rounded-xl border border-calm-border dark:border-calm-borderDark bg-calm-card dark:bg-calm-cardDark text-calm-ink dark:text-calm-inkDark placeholder:text-calm-inkmuted"
        />
        <button
          onClick={() => redeem(token)}
          disabled={loading || !token.trim()}
          className="calm-btn-primary w-full"
        >
          {loading ? <Loader2 size={16} className="animate-spin" /> : 'Connect'}
        </button>
      </div>
    </div>
  )
}

function TalkPageInner() {
  const { isAuthenticated, user } = useStore()
  const searchParams = useSearchParams()
  const inviteToken = searchParams.get('invite') || undefined

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

  const isLinkedFamily = user?.role === 'family' && !!user?.producer_id

  useEffect(() => {
    if (isAuthenticated() && isLinkedFamily) loadAvailability()
  }, [isAuthenticated, isLinkedFamily])

  if (!isAuthenticated()) {
    return <AuthModal />
  }

  if (!isLinkedFamily) {
    return <RedeemForm initialToken={inviteToken} onRedeemed={loadAvailability} />
  }

  if (loading) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-calm-paper dark:bg-calm-paperDark">
        <Loader2 size={26} className="animate-spin text-calm-sage-600" />
      </div>
    )
  }

  if (loadError || !availability) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-calm-paper dark:bg-calm-paperDark px-6">
        <div className="max-w-sm text-center flex flex-col items-center gap-4">
          <p className="text-sm text-calm-ink dark:text-calm-inkDark">
            {loadError || 'Something went wrong'}
          </p>
          <button onClick={loadAvailability} className="calm-btn-primary">
            Try again
          </button>
        </div>
      </div>
    )
  }

  if (!availability.available || !availability.avatar_id) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-calm-paper dark:bg-calm-paperDark px-6">
        <div className="max-w-sm text-center flex flex-col items-center gap-4">
          <Heart size={28} className="text-calm-sage-600 dark:text-calm-sage-300" />
          <h1 className="text-lg font-semibold text-calm-ink dark:text-calm-inkDark">
            {availability.producer_name} is still preparing their stories
          </h1>
          <p className="text-sm text-calm-inkmuted dark:text-calm-inkmutedDark">
            Check back soon — you&apos;ll be able to talk with them here once they&apos;ve
            recorded some memories.
          </p>
          <button onClick={loadAvailability} className="calm-btn-secondary">
            Check again
          </button>
        </div>
      </div>
    )
  }

  return (
    <TalkInterface
      avatarId={availability.avatar_id}
      avatarImageUrl={availability.avatar_image_url}
      producerName={availability.producer_name}
    />
  )
}

export default function TalkPage() {
  return (
    <Suspense
      fallback={
        <div className="min-h-screen flex items-center justify-center bg-calm-paper dark:bg-calm-paperDark">
          <Loader2 size={26} className="animate-spin text-calm-sage-600" />
        </div>
      }
    >
      <TalkPageInner />
    </Suspense>
  )
}
