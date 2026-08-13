'use client'

import { useEffect, useState } from 'react'
import { Gift, Loader2 } from 'lucide-react'
import { toast } from 'react-hot-toast'
import { api } from '@/lib/api'
import { useStore } from '@/store/useStore'
import type { ApiError } from '@/lib/types'

/**
 * Invite redemption inside the shell — /talk's RedeemForm re-homed
 * (docs/FAMILY_UNIFIED_SHELL_PLAN.md §2.5), restyled onto the shell's own
 * design system. Rendered for a family account with no producer linkage
 * (never linked, or unlinked later); an `initialToken` (from ?invite=)
 * auto-submits exactly as the old page did.
 */
export function FamilyRedeem({
  initialToken,
  onRedeemed,
}: {
  initialToken?: string
  onRedeemed?: () => void
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
      onRedeemed?.()
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
    <div className="min-h-[70vh] flex items-center justify-center px-6">
      <div className="max-w-sm w-full text-center flex flex-col items-center gap-4 glass-card p-8 rounded-2xl">
        <Gift size={28} className="text-primary-400" />
        <h1 className="text-lg font-semibold text-white">Enter your invite code</h1>
        <p className="text-sm text-gray-400">
          Ask the person who invited you for the link or code they shared with you.
        </p>
        <input
          value={token}
          onChange={(e) => setToken(e.target.value)}
          placeholder="Paste your invite code"
          className="w-full px-4 py-3 rounded-xl border border-white/10 bg-surface-800 text-white placeholder:text-gray-500"
        />
        <button
          onClick={() => redeem(token)}
          disabled={loading || !token.trim()}
          className="btn-primary w-full"
        >
          {loading ? <Loader2 size={16} className="animate-spin" /> : 'Connect'}
        </button>
      </div>
    </div>
  )
}
