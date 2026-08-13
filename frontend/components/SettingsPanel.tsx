'use client'

import { useEffect, useState } from 'react'
import { Save, Loader2, User, KeyRound, Trash2, Sparkles, Film, Compass } from 'lucide-react'
import { toast } from 'react-hot-toast'
import { api } from '@/lib/api'
import { useStore } from '@/store/useStore'
import { FamilyInvitePanel } from '@/components/FamilyInvitePanel'
import type { ApiError, ChatMode } from '@/lib/types'

const CHAT_MODE_LABELS: Record<ChatMode, string> = {
  avatar: 'Avatar mode',
  video_clips_v2: 'Original video clips',
}

/** What has to be typed before the archive can be destroyed. A button
 *  alone is one misclick; a checkbox is one misclick and a shrug. Typing
 *  the word is the smallest thing that cannot happen by accident. */
const RESET_PHRASE = 'DELETE'

interface SettingsPanelProps {
  /** Navigate to Avatar Studio — avatar mode's setup surface, whose nav tab
   *  is hidden while a video-clip mode is active. Wired by the shell. */
  onOpenAvatarStudio?: () => void
}

export function SettingsPanel({ onOpenAvatarStudio }: SettingsPanelProps) {
  const { user, setAuth, token, clearAuth } = useStore()
  const [fullName, setFullName] = useState(user?.full_name || '')
  const [username, setUsername] = useState(user?.username || '')
  const [email, setEmail] = useState(user?.email || '')
  const [newPassword, setNewPassword] = useState('')
  const [confirmPassword, setConfirmPassword] = useState('')
  const [savingProfile, setSavingProfile] = useState(false)
  const [savingPassword, setSavingPassword] = useState(false)
  const [savingChatMode, setSavingChatMode] = useState(false)
  const [savingFreeNav, setSavingFreeNav] = useState(false)

  const isGuest = token === 'guest' || user?.id === 'demo-user'
  const chatMode = user?.chat_mode || 'video_clips_v2'

  const freeNavigation = Boolean(user?.free_navigation)
  const [resetPhrase, setResetPhrase] = useState('')
  const [resetting, setResetting] = useState(false)

  // Whether avatar mode's activation gate is satisfied. null = not yet
  // known (the card stays clickable and the server re-checks anyway — the
  // gate is the 400 in update_profile, this is just honest UI around it).
  const [hasReadyAvatar, setHasReadyAvatar] = useState<boolean | null>(null)
  useEffect(() => {
    if (isGuest || user?.role === 'family') return
    let cancelled = false
    api.getAvatars()
      .then((avatars: { status: string }[]) => {
        if (!cancelled) setHasReadyAvatar(avatars.some(a => a.status === 'ready'))
      })
      .catch(() => { /* unknown — leave null, the server still gates */ })
    return () => { cancelled = true }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // Mirrors setChatMode: same updateProfile path, same auth refresh, same
  // guest guard. See docs/INTERVIEW_RESTRUCTURE.md §7A for why this exists —
  // it is the escape hatch for rehoming footage and for adding content to a
  // category that was previously screened out, not a convenience toggle.
  const setFreeNavigation = async (enabled: boolean) => {
    if (isGuest || enabled === freeNavigation) return
    setSavingFreeNav(true)
    try {
      const updated = await api.updateProfile({ free_navigation: enabled })
      if (token) setAuth(token, updated)
      toast.success(enabled ? 'Free navigation on' : 'Free navigation off')
    } catch (err: unknown) {
      toast.error((err as ApiError)?.response?.data?.detail || 'Could not update navigation')
    } finally {
      setSavingFreeNav(false)
    }
  }

  const setChatMode = async (mode: ChatMode) => {
    if (isGuest || mode === chatMode) return
    if (mode === 'avatar' && hasReadyAvatar === false) {
      // The activation gate, known-unsatisfied: go set up an avatar instead
      // of sending a request that will 400 with the same instruction.
      toast('Avatar mode needs an avatar image first — set one up here', { icon: '📷' })
      onOpenAvatarStudio?.()
      return
    }
    setSavingChatMode(true)
    try {
      const updated = await api.updateProfile({ chat_mode: mode })
      if (token) setAuth(token, updated)
      toast.success(`Switched to ${CHAT_MODE_LABELS[mode]}`)
    } catch (err: unknown) {
      toast.error((err as ApiError)?.response?.data?.detail || 'Could not update talk mode')
    } finally {
      setSavingChatMode(false)
    }
  }

  const saveProfile = async () => {
    if (isGuest) {
      toast.error('Sign in with a real account to edit your profile')
      return
    }
    setSavingProfile(true)
    try {
      const update: Record<string, string> = {}
      if (fullName !== (user?.full_name || '')) update.full_name = fullName
      if (username && username !== user?.username) update.username = username
      if (email && email !== user?.email) update.email = email
      if (Object.keys(update).length === 0) {
        toast('Nothing to update', { icon: 'ℹ️' })
        return
      }
      const updated = await api.updateProfile(update)
      if (token) setAuth(token, updated)
      toast.success('Profile updated')
    } catch (err: unknown) {
      toast.error((err as ApiError)?.response?.data?.detail || 'Could not save profile')
    } finally {
      setSavingProfile(false)
    }
  }

  const changePassword = async () => {
    if (isGuest) {
      toast.error('Sign in with a real account to change your password')
      return
    }
    if (newPassword.length < 8) {
      toast.error('Password must be at least 8 characters')
      return
    }
    if (newPassword !== confirmPassword) {
      toast.error('Passwords do not match')
      return
    }
    setSavingPassword(true)
    try {
      await api.updateProfile({ password: newPassword })
      setNewPassword('')
      setConfirmPassword('')
      toast.success('Password updated')
    } catch (err: unknown) {
      toast.error((err as ApiError)?.response?.data?.detail || 'Could not change password')
    } finally {
      setSavingPassword(false)
    }
  }

  return (
    <div className="max-w-3xl mx-auto px-6 py-10 animate-fade-in">
      <div className="mb-8">
        <h1 className="text-3xl font-black gradient-text mb-2">Settings</h1>
        <p className="text-muted">Manage your account and preferences.</p>
      </div>

      {isGuest && (
        <div className="card-glow mb-6 flex items-start gap-3">
          <User size={16} className="text-amber-400 mt-0.5 flex-shrink-0" />
          <div>
            <p className="text-sm text-ink font-semibold">You&apos;re signed in as a guest.</p>
            <p className="text-xs text-muted mt-1">Sign out and register an account to save your profile and access multi-device sync.</p>
          </div>
        </div>
      )}

      {/* Profile card */}
      <div className="card flex flex-col gap-5">
        <div className="flex items-center gap-2">
          <User size={16} className="text-primary-400" />
          <h2 className="text-xl font-bold text-ink">Profile</h2>
        </div>
        <div className="divider" />
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          <div className="space-y-1.5">
            <label className="text-sm font-medium text-ink-soft">Full name</label>
            <input
              type="text"
              value={fullName}
              onChange={(e) => setFullName(e.target.value)}
              className="input-field"
              placeholder="Your name"
              disabled={isGuest}
            />
          </div>
          <div className="space-y-1.5">
            <label className="text-sm font-medium text-ink-soft">Username</label>
            <input
              type="text"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              className="input-field"
              placeholder="username"
              disabled={isGuest}
            />
          </div>
          <div className="space-y-1.5 md:col-span-2">
            <label className="text-sm font-medium text-ink-soft">Email</label>
            <input
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              className="input-field"
              placeholder="you@example.com"
              disabled={isGuest}
            />
          </div>
        </div>
        <button
          onClick={saveProfile}
          disabled={savingProfile || isGuest}
          className="btn-primary w-full md:w-auto md:self-end"
        >
          {savingProfile ? <Loader2 size={15} className="animate-spin" /> : <Save size={15} />}
          Save changes
        </button>
      </div>

      {/* Password card */}
      <div className="card flex flex-col gap-5 mt-6">
        <div className="flex items-center gap-2">
          <KeyRound size={16} className="text-primary-400" />
          <h2 className="text-xl font-bold text-ink">Password</h2>
        </div>
        <div className="divider" />
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          <div className="space-y-1.5">
            <label className="text-sm font-medium text-ink-soft">New password</label>
            <input
              type="password"
              value={newPassword}
              onChange={(e) => setNewPassword(e.target.value)}
              className="input-field"
              placeholder="At least 8 characters"
              disabled={isGuest}
              autoComplete="new-password"
            />
          </div>
          <div className="space-y-1.5">
            <label className="text-sm font-medium text-ink-soft">Confirm new password</label>
            <input
              type="password"
              value={confirmPassword}
              onChange={(e) => setConfirmPassword(e.target.value)}
              className="input-field"
              placeholder="Repeat your password"
              disabled={isGuest}
              autoComplete="new-password"
            />
          </div>
        </div>
        <button
          onClick={changePassword}
          disabled={savingPassword || isGuest || !newPassword || !confirmPassword}
          className="btn-primary w-full md:w-auto md:self-end"
        >
          {savingPassword ? <Loader2 size={15} className="animate-spin" /> : <KeyRound size={15} />}
          Update password
        </button>
      </div>

      {/* Talk mode card (Prompt 14) — producer-level: picks which chat
          experience EVERY family member gets on /talk. Not shown to family
          accounts, since they never have their own copy of this setting. */}
      {!isGuest && user?.role !== 'family' && (
        <div className="card flex flex-col gap-5 mt-6">
          <div className="flex items-center gap-2">
            <Film size={16} className="text-primary-400" />
            <h2 className="text-xl font-bold text-ink">Talk mode</h2>
          </div>
          <div className="divider" />
          <p className="text-sm text-muted">
            Choose how family members experience your stories on /talk.
          </p>
          <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
            <button
              onClick={() => setChatMode('video_clips_v2')}
              disabled={savingChatMode}
              aria-pressed={chatMode === 'video_clips_v2'}
              className={`text-left p-4 rounded-xl border transition-colors ${
                chatMode === 'video_clips_v2'
                  ? 'border-primary-400 bg-primary-400/10'
                  : 'border-edge hover:border-edge-strong'
              }`}
            >
              <div className="flex items-center gap-2 mb-1">
                <Film size={15} className="text-primary-400" />
                <span className="font-semibold text-ink">Original video clips</span>
              </div>
              <p className="text-xs text-muted">
                Family members see the real recorded moment that answers their
                question — your own face and voice (default).
              </p>
            </button>
            <button
              onClick={() => setChatMode('avatar')}
              disabled={savingChatMode}
              aria-pressed={chatMode === 'avatar'}
              className={`text-left p-4 rounded-xl border transition-colors ${
                chatMode === 'avatar'
                  ? 'border-primary-400 bg-primary-400/10'
                  : 'border-edge hover:border-edge-strong'
              }`}
            >
              <div className="flex items-center gap-2 mb-1">
                <Sparkles size={15} className="text-primary-400" />
                <span className="font-semibold text-ink">Avatar</span>
              </div>
              <p className="text-xs text-muted">
                A talking-head avatar speaks your stories aloud.
                {hasReadyAvatar === false && (
                  <span className="block mt-1 text-amber-400/90">
                    Needs an avatar image — click to set one up in Avatar Studio.
                  </span>
                )}
              </p>
            </button>
          </div>
        </div>
      )}

      {user?.role === 'producer' && (
        <div className="card flex flex-col gap-4 mt-6">
          <div className="flex items-center gap-2">
            <Compass size={16} className="text-primary-400" />
            <h2 className="text-xl font-bold text-ink">Recording navigation</h2>
          </div>
          <div className="divider" />
          <label className="flex items-start gap-3 cursor-pointer">
            <input
              type="checkbox"
              checked={freeNavigation}
              disabled={isGuest || savingFreeNav}
              onChange={e => setFreeNavigation(e.target.checked)}
              className="mt-1 w-4 h-4 accent-primary-500 flex-shrink-0"
            />
            <span>
              <span className="font-semibold text-ink block">
                Let me jump between categories
              </span>
              <span className="text-xs text-muted block mt-1">
                Off by default, so the interview walks you through in order. Turn it on to
                open any category and record or upload into it out of sequence — useful for
                filling in a section you skipped earlier.
              </span>
              {/* Says what it does NOT do, because "unlock everything" is the
                  natural but wrong reading — the server refuses a question
                  behind an unanswered screening question either way. */}
              <span className="text-xs text-muted2 block mt-1.5">
                Screening questions still apply — you&apos;ll be asked those before a
                category&apos;s questions open up.
              </span>
            </span>
          </label>
        </div>
      )}

      {!isGuest && user?.role !== 'family' && <FamilyInvitePanel />}

      {/* Danger zone */}
      <div className="card flex flex-col gap-4 mt-6 border border-red-500/20">
        <div className="flex items-center gap-2">
          <Trash2 size={16} className="text-red-400" />
          <h2 className="text-xl font-bold text-ink">Delete all my recordings</h2>
        </div>
        <div className="divider" />
        <p className="text-sm text-muted">
          Every recording, transcript, and the people and relationships found in them.
          The videos are deleted from storage too.{' '}
          <span className="text-ink-soft">This cannot be undone.</span>
        </p>
        <p className="text-xs text-muted2">
          Your account, avatars and voice samples are not touched, and you stay in your
          own family tree — you just start with an empty archive.
        </p>
        <label htmlFor="reset-confirm" className="text-xs text-muted">
          Type <span className="text-red-300 font-semibold">{RESET_PHRASE}</span> to confirm
        </label>
        <input
          id="reset-confirm"
          type="text"
          value={resetPhrase}
          onChange={(e) => setResetPhrase(e.target.value)}
          disabled={resetting}
          placeholder={RESET_PHRASE}
          className="w-full md:w-56 px-3 py-2 rounded-lg bg-surface-800 border border-edge text-sm text-ink placeholder:text-muted2"
        />
        <button
          onClick={async () => {
            if (resetPhrase !== RESET_PHRASE) return
            setResetting(true)
            try {
              const result = await api.resetArchive()
              toast.success(
                `Deleted ${result.recordings_deleted} recording${
                  result.recordings_deleted === 1 ? '' : 's'
                }`,
              )
              setResetPhrase('')
            } catch {
              toast.error('Could not delete your recordings — please try again')
            } finally {
              setResetting(false)
            }
          }}
          disabled={resetting || resetPhrase !== RESET_PHRASE}
          className="btn-danger w-full md:w-auto md:self-end disabled:opacity-40 disabled:cursor-not-allowed"
        >
          {resetting ? 'Deleting…' : 'Delete everything'}
        </button>
      </div>

      <div className="card flex flex-col gap-5 mt-6 border border-red-500/20">
        <div className="flex items-center gap-2">
          <Trash2 size={16} className="text-red-400" />
          <h2 className="text-xl font-bold text-ink">Danger zone</h2>
        </div>
        <div className="divider" />
        <p className="text-sm text-muted">
          Sign out of this device. Your avatars, voices, and conversations remain on the server.
        </p>
        <button
          onClick={() => {
            api.logout()
            clearAuth()
            toast('Signed out', { icon: '👋' })
          }}
          className="btn-secondary w-full md:w-auto md:self-end"
        >
          Sign out
        </button>
      </div>
    </div>
  )
}
