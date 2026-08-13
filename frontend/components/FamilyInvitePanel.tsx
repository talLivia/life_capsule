'use client'

import { useEffect, useState } from 'react'
import { Gift, Copy, Loader2, X, Check, Users } from 'lucide-react'
import { toast } from 'react-hot-toast'
import { api } from '@/lib/api'
import type { ApiError, FamilyInvite, FamilyMember } from '@/lib/types'

function buildInviteUrl(token: string): string {
  const origin = typeof window !== 'undefined' ? window.location.origin : ''
  // Invite links land on the shell now; the /talk stub still forwards any
  // link copied before the move (FAMILY_UNIFIED_SHELL_PLAN §2.5).
  return `${origin}/?invite=${encodeURIComponent(token)}`
}

export function FamilyInvitePanel() {
  const [invites, setInvites] = useState<FamilyInvite[]>([])
  const [members, setMembers] = useState<FamilyMember[]>([])
  const [loading, setLoading] = useState(true)
  const [creating, setCreating] = useState(false)
  const [revokingId, setRevokingId] = useState<string | null>(null)
  const [copiedId, setCopiedId] = useState<string | null>(null)
  // Two-click removal: first click arms, second confirms. The copy names
  // what it destroys — account AND chat history — because it is permanent.
  const [confirmingRemove, setConfirmingRemove] = useState<string | null>(null)
  const [removingId, setRemovingId] = useState<string | null>(null)

  // One lifecycle, one load: an invite redeemed elsewhere moves from
  // Pending to Active on the next refresh because the two queries
  // partition on the same fact (invite status <-> account linkage).
  const load = async () => {
    setLoading(true)
    try {
      const [invitesData, membersData] = await Promise.all([
        api.listFamilyInvites() as Promise<FamilyInvite[]>,
        api.listFamilyMembers() as Promise<FamilyMember[]>,
      ])
      setInvites(invitesData)
      setMembers(membersData)
      setLoadedAt(Date.now())
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

  const removeMember = async (member: FamilyMember) => {
    setRemovingId(member.user_id)
    try {
      await api.removeFamilyMember(member.user_id)
      toast.success(`${member.display_name} removed`)
      setConfirmingRemove(null)
      await load()
    } catch (err: unknown) {
      toast.error((err as ApiError)?.response?.data?.detail || 'Could not remove them')
    } finally {
      setRemovingId(null)
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

  // Pending = still redeemable. Redeemed invites appear as Active users
  // below; revoked/expired ones are dead weight and not shown. Expiry is
  // judged at load time (a re-render must stay pure), which is when the
  // list was fetched anyway.
  const [loadedAt, setLoadedAt] = useState(() => Date.now())
  const pending = invites.filter(
    (inv) => inv.status === 'pending' && new Date(inv.expires_at).getTime() > loadedAt
  )

  return (
    <div className="card flex flex-col gap-5 mt-6">
      <div className="flex items-center gap-2">
        <Gift size={16} className="text-primary-400" />
        <h2 className="text-xl font-bold text-ink">Family access</h2>
      </div>
      <div className="divider" />
      <p className="text-sm text-muted">
        Invite a family member to talk with your stories on the{' '}
        chat. Each link works once.
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
        <div className="flex items-center justify-center py-8 text-muted2 text-sm">
          <Loader2 size={16} className="animate-spin mr-2" />
          Loading…
        </div>
      ) : (
        <>
          {/* Pending invites: sent, not yet redeemed */}
          <h3 className="text-sm font-semibold text-ink-soft">Pending invites</h3>
          {pending.length === 0 ? (
            <p className="text-sm text-muted2">No pending invites.</p>
          ) : (
            <div className="flex flex-col gap-2">
              {pending.map((invite) => (
                <div
                  key={invite.id}
                  className="flex items-center gap-3 px-4 py-3 rounded-xl bg-surface-800/60 border border-edge"
                >
                  <div className="flex-1 min-w-0">
                    <span className="text-xs text-muted2">
                      created {new Date(invite.created_at).toLocaleDateString()}
                    </span>
                    <p className="text-xs text-muted2 mt-1 truncate">
                      {buildInviteUrl(invite.token)}
                    </p>
                  </div>
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
                    className="text-xs text-muted2 hover:text-red-400 transition-colors px-2 py-1.5 rounded-lg hover:bg-red-500/10 flex-shrink-0"
                    title="Revoke invite"
                  >
                    {revokingId === invite.id ? (
                      <Loader2 size={13} className="animate-spin" />
                    ) : (
                      <X size={13} />
                    )}
                  </button>
                </div>
              ))}
            </div>
          )}

          {/* Active users: accounts that redeemed an invite */}
          <div className="flex items-center gap-2 mt-2">
            <Users size={14} className="text-primary-400" />
            <h3 className="text-sm font-semibold text-ink-soft">Active users</h3>
          </div>
          {members.length === 0 ? (
            <p className="text-sm text-muted2">
              Nobody has joined yet — they appear here the moment an invite is used.
            </p>
          ) : (
            <div className="flex flex-col gap-2">
              {members.map((member) => (
                <div
                  key={member.user_id}
                  className="flex items-center gap-3 px-4 py-3 rounded-xl bg-surface-800/60 border border-edge"
                >
                  <div className="flex-1 min-w-0">
                    <p className="text-sm text-ink truncate" dir="auto">
                      {member.display_name}
                    </p>
                    {member.joined_at && (
                      <p className="text-xs text-muted2 mt-0.5">
                        joined {new Date(member.joined_at).toLocaleDateString()}
                      </p>
                    )}
                  </div>
                  <button
                    onClick={() =>
                      confirmingRemove === member.user_id
                        ? removeMember(member)
                        : setConfirmingRemove(member.user_id)
                    }
                    disabled={removingId === member.user_id}
                    className={`shrink-0 px-2.5 py-1.5 rounded-lg border text-xs transition-colors ${
                      confirmingRemove === member.user_id
                        ? 'border-red-400 bg-red-500/15 text-red-200'
                        : 'border-edge text-muted hover:border-red-400/40 hover:text-red-300'
                    }`}
                    title="Deletes their account and chat history — permanent"
                  >
                    {removingId === member.user_id ? (
                      <Loader2 size={13} className="animate-spin" />
                    ) : confirmingRemove === member.user_id ? (
                      'Delete account + history?'
                    ) : (
                      'Remove'
                    )}
                  </button>
                </div>
              ))}
            </div>
          )}
        </>
      )}
    </div>
  )
}
