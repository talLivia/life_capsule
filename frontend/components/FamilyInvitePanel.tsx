'use client'

import { useEffect, useState } from 'react'
import { Gift, Copy, Loader2, X, Check } from 'lucide-react'
import { toast } from 'react-hot-toast'
import { api } from '@/lib/api'
import type { ApiError, FamilyInvite } from '@/lib/types'

function buildInviteUrl(token: string): string {
  const origin = typeof window !== 'undefined' ? window.location.origin : ''
  return `${origin}/talk?invite=${encodeURIComponent(token)}`
}

export function FamilyInvitePanel() {
  const [invites, setInvites] = useState<FamilyInvite[]>([])
  const [loading, setLoading] = useState(true)
  const [creating, setCreating] = useState(false)
  const [revokingId, setRevokingId] = useState<string | null>(null)
  const [copiedId, setCopiedId] = useState<string | null>(null)

  const load = async () => {
    setLoading(true)
    try {
      const data: FamilyInvite[] = await api.listFamilyInvites()
      setInvites(data)
    } catch (err: unknown) {
      toast.error((err as ApiError)?.response?.data?.detail || 'Could not load invites')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    load()
  }, [])

  const createInvite = async () => {
    setCreating(true)
    try {
      const invite: FamilyInvite = await api.createFamilyInvite()
      setInvites((prev) => [invite, ...prev])
      toast.success('Invite link created')
    } catch (err: unknown) {
      toast.error((err as ApiError)?.response?.data?.detail || 'Could not create invite')
    } finally {
      setCreating(false)
    }
  }

  const revokeInvite = async (id: string) => {
    setRevokingId(id)
    try {
      await api.revokeFamilyInvite(id)
      setInvites((prev) =>
        prev.map((inv) => (inv.id === id ? { ...inv, status: 'revoked' } : inv))
      )
    } catch (err: unknown) {
      toast.error((err as ApiError)?.response?.data?.detail || 'Could not revoke invite')
    } finally {
      setRevokingId(null)
    }
  }

  const copyLink = async (invite: FamilyInvite) => {
    try {
      await navigator.clipboard.writeText(buildInviteUrl(invite.token))
      setCopiedId(invite.id)
      setTimeout(() => setCopiedId((cur) => (cur === invite.id ? null : cur)), 2000)
    } catch {
      toast.error('Could not copy link')
    }
  }

  const statusBadge = (status: FamilyInvite['status']) => {
    const styles: Record<FamilyInvite['status'], string> = {
      pending: 'bg-amber-500/10 text-amber-400 border-amber-500/20',
      redeemed: 'bg-emerald-500/10 text-emerald-400 border-emerald-500/20',
      revoked: 'bg-gray-500/10 text-gray-500 border-gray-500/20',
    }
    return (
      <span className={`text-xs px-2 py-0.5 rounded-full border ${styles[status]}`}>
        {status}
      </span>
    )
  }

  return (
    <div className="card flex flex-col gap-5 mt-6">
      <div className="flex items-center gap-2">
        <Gift size={16} className="text-primary-400" />
        <h2 className="text-xl font-bold text-white">Family access</h2>
      </div>
      <div className="divider" />
      <p className="text-sm text-gray-400">
        Invite a family member to talk with your stories on the{' '}
        <code className="text-gray-300">/talk</code> page. Each link works once.
      </p>

      <button
        onClick={createInvite}
        disabled={creating}
        className="btn-primary w-full md:w-auto md:self-start"
      >
        {creating ? <Loader2 size={15} className="animate-spin" /> : <Gift size={15} />}
        Create invite link
      </button>

      {loading ? (
        <div className="flex items-center justify-center py-8 text-gray-500 text-sm">
          <Loader2 size={16} className="animate-spin mr-2" />
          Loading invites…
        </div>
      ) : invites.length === 0 ? (
        <p className="text-sm text-gray-500 text-center py-4">No invites yet.</p>
      ) : (
        <div className="flex flex-col gap-2">
          {invites.map((invite) => (
            <div
              key={invite.id}
              className="flex items-center gap-3 px-4 py-3 rounded-xl bg-surface-800/60 border border-white/8"
            >
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2">
                  {statusBadge(invite.status)}
                  <span className="text-xs text-gray-500">
                    created {new Date(invite.created_at).toLocaleDateString()}
                  </span>
                </div>
                {invite.status === 'pending' && (
                  <p className="text-xs text-gray-500 mt-1 truncate">
                    {buildInviteUrl(invite.token)}
                  </p>
                )}
              </div>
              {invite.status === 'pending' && (
                <>
                  <button
                    onClick={() => copyLink(invite)}
                    className="btn-secondary !px-3 !py-1.5 text-xs flex-shrink-0"
                    title="Copy invite link"
                  >
                    {copiedId === invite.id ? <Check size={13} /> : <Copy size={13} />}
                    {copiedId === invite.id ? 'Copied' : 'Copy'}
                  </button>
                  <button
                    onClick={() => revokeInvite(invite.id)}
                    disabled={revokingId === invite.id}
                    className="text-xs text-gray-500 hover:text-red-400 transition-colors px-2 py-1.5 rounded-lg hover:bg-red-500/10 flex-shrink-0"
                    title="Revoke invite"
                  >
                    {revokingId === invite.id ? (
                      <Loader2 size={13} className="animate-spin" />
                    ) : (
                      <X size={13} />
                    )}
                  </button>
                </>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
