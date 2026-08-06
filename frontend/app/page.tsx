'use client'

import { useEffect, useState, type CSSProperties } from 'react'
import { useRouter } from 'next/navigation'
import dynamic from 'next/dynamic'
import Image from 'next/image'
import { AvatarUpload } from '@/components/AvatarUpload'
import { AvatarList } from '@/components/AvatarList'
import { ConnectionStatus } from '@/components/ui/ConnectionStatus'
import { ThemeToggle } from '@/components/ui/ThemeToggle'
import { AuthModal } from '@/components/AuthModal'
import { NotificationCenter } from '@/components/notifications/NotificationCenter'
import { api } from '@/lib/api'
import { toast } from 'react-hot-toast'
import { useStore } from '@/store/useStore'
import type { Avatar } from '@/lib/types'

// Heavy panels (chat WebSocket pipeline, voice cloning recorder, history
// list with TanStack queries, settings form) load on-demand instead of
// shipping their JS in the home-page bundle. Cuts the initial chunk size
// by ~150 KB and lets the marketing landing page paint sooner.
const ChatInterface = dynamic(
  () => import('@/components/ChatInterface').then(m => m.ChatInterface),
  { ssr: false, loading: () => <PanelLoader label="Connecting…" /> },
)
// Producer's video-clip chat screen — its OWN layout (side chat + single
// video panel), but sharing the exact conversation behavior the family /talk
// screen uses via the useVideoClipChat hook. See the chat_mode routing below.
const ProducerVideoClipChat = dynamic(
  () => import('@/components/ProducerVideoClipChat').then(m => m.ProducerVideoClipChat),
  { ssr: false, loading: () => <PanelLoader label="Connecting…" /> },
)
const VoicePanel = dynamic(
  () => import('@/components/VoicePanel').then(m => m.VoicePanel),
  { ssr: false, loading: () => <PanelLoader label="Loading voice studio…" /> },
)
const HistoryPanel = dynamic(
  () => import('@/components/HistoryPanel').then(m => m.HistoryPanel),
  { ssr: false, loading: () => <PanelLoader label="Loading history…" /> },
)
const SettingsPanel = dynamic(
  () => import('@/components/SettingsPanel').then(m => m.SettingsPanel),
  { ssr: false, loading: () => <PanelLoader label="Loading settings…" /> },
)
// Recording is now an in-shell view (like Settings) rather than a separate
// /record route — see the `record` view below. The old route redirects here.
const TimelinePanel = dynamic(
  () => import('@/components/TimelinePanel').then(m => m.TimelinePanel),
  { ssr: false, loading: () => <PanelLoader label="Loading your timeline…" /> },
)

const FamilyTreePanel = dynamic(
  () => import('@/components/FamilyTreePanel').then(m => m.FamilyTreePanel),
  { ssr: false, loading: () => <PanelLoader label="Building your family tree…" /> },
)

const RecordPanel = dynamic(
  () => import('@/components/RecordPanel').then(m => m.RecordPanel),
  { ssr: false, loading: () => <PanelLoader label="Loading your story…" /> },
)

function PanelLoader({ label }: { label: string }) {
  return (
    <div className="flex items-center justify-center py-20 text-gray-500 text-sm">
      <span className="inline-block w-2 h-2 rounded-full bg-primary-500 animate-pulse mr-2" />
      {label}
    </div>
  )
}
import {
  Camera,
  MessageCircle,
  Mic2,
  Sparkles,
  Zap,
  Globe,
  Shield,
  Play,
  ChevronRight,
  Activity,
  Brain,
  AudioWaveform,
  History,
  Settings,
  Feather,
  Network,
  CalendarRange,
  Video,
  Search,
} from 'lucide-react'

const FEATURES = [
  {
    icon: MessageCircle,
    title: 'Questions worth answering',
    description:
      "129 of them, across 16 chapters of a life — from childhood to what they'd want their grandchildren to know. They answer whichever they like, whenever they like.",
    color: 'from-purple-500 to-pink-500',
    glow: 'rgba(168,85,247,0.3)',
  },
  {
    icon: Video,
    title: 'Their face, their voice',
    description:
      "Every answer is video they recorded themselves. Nothing is generated, imitated, or put into their mouth — if they didn't say it, your family will never hear it.",
    color: 'from-blue-500 to-cyan-500',
    glow: 'rgba(59,130,246,0.3)',
  },
  {
    icon: Search,
    title: 'Ask anything, later',
    description:
      'Your family types a question and gets back the moments where they actually answered it. Ask about a name, a year, a place; the archive finds the right piece of the right recording.',
    color: 'from-emerald-500 to-teal-500',
    glow: 'rgba(16,185,129,0.3)',
  },
  {
    icon: Network,
    title: 'A family tree that fills itself in',
    description:
      'As they record, the people they mention become a tree you can click through. Tap anyone to watch the moments they talk about them.',
    color: 'from-amber-500 to-orange-500',
    glow: 'rgba(245,158,11,0.3)',
  },
  {
    icon: Globe,
    title: 'In the language home was spoken in',
    description:
      'Hebrew, Arabic, Russian, English. They answer in their own language, and it stays in their own language.',
    color: 'from-indigo-500 to-blue-500',
    glow: 'rgba(99,102,241,0.3)',
  },
  {
    icon: Shield,
    title: "Yours, and no one else's",
    description:
      'Private to the family they invite. Nothing public, nothing sold, nothing used to train anything.',
    color: 'from-rose-500 to-pink-500',
    glow: 'rgba(244,63,94,0.3)',
  },
]

const STATS = [
  { value: '16', label: 'Life chapters' },
  { value: '129', label: 'Questions to answer' },
  { value: '∞', label: 'Times your family can ask' },
  { value: '100%', label: 'Their own words' },
]

type View = 'home' | 'avatars' | 'chat' | 'voice' | 'history' | 'settings' | 'record' | 'tree' | 'timeline'

export default function Home() {
  const { isAuthenticated, user, clearAuth } = useStore()
  const router = useRouter()

  // Access-control fix: a family account has no legitimate use for this
  // page at all (avatar upload, voice cloning, interview recording,
  // producer settings) — before this, there was NO role-based redirect
  // anywhere in the app (confirmed: AuthModal's login/register handlers
  // never navigate, and this page's only role check just hid the
  // "Record" nav link). A family account landing here for any reason
  // (bookmark, browser back button, a stale link) rendered the full
  // producer control panel with no gate at all. Redirect BEFORE the
  // producer UI ever renders, not just after login, so every entry path
  // is covered, not only the login flow.
  const isFamilyUser = isAuthenticated() && user?.role === 'family'
  // The producer's own chat_mode decides which chat component their /chat view
  // renders — mirrors /talk's routing. Video-clip modes must NOT use the
  // avatar-only ChatInterface (it can't handle video_clip_response).
  const isVideoClipMode =
    user?.chat_mode === 'video_clips' || user?.chat_mode === 'video_clips_v2'
  useEffect(() => {
    if (isFamilyUser) router.replace('/talk')
  }, [isFamilyUser, router])

  const [selectedAvatar, setSelectedAvatar] = useState<string | null>(null)
  // True while we're auto-resolving the producer's avatar for a video-clip
  // mode (so the Chat view can show a loader instead of the "pick an avatar"
  // redirect, which is meaningless when the Avatars tab is hidden).
  const [avatarResolving, setAvatarResolving] = useState(false)
  const [activeSessionId, setActiveSessionId] = useState<string | null>(null)
  // Session id to RESUME (set only when opening from history). Distinct from
  // activeSessionId (reported back after a session starts) so it can key the
  // ChatInterface without remounting mid-conversation when a fresh session id
  // arrives.
  const [resumeSessionId, setResumeSessionId] = useState<string | null>(null)
  const [view, setView] = useState<View>('home')

  // In video-clip modes the producer never sees the Avatars tab, so silently
  // resolve their existing avatar to satisfy the (avatar-bound) session that
  // the chat still creates under the hood. The producer doesn't have to pick.
  useEffect(() => {
    if (!isVideoClipMode || selectedAvatar) return
    let cancelled = false
    setAvatarResolving(true)
    api.getAvatars()
      .then((avatars: Avatar[]) => {
        if (cancelled) return
        const ready = avatars.find((a) => a.status === 'ready') ?? avatars[0]
        if (ready) setSelectedAvatar(ready.id)
      })
      .catch(() => { /* leave selectedAvatar null → chat view shows guidance */ })
      .finally(() => { if (!cancelled) setAvatarResolving(false) })
    return () => { cancelled = true }
  }, [isVideoClipMode, selectedAvatar])

  // Switching INTO a video-clip mode while parked on a now-hidden tab would
  // leave a blank view — bounce to Chat.
  useEffect(() => {
    if (isVideoClipMode && (view === 'avatars' || view === 'voice')) setView('chat')
  }, [isVideoClipMode, view])

  // Deep-link support for the old /record route (now redirects here with
  // ?view=record). Read post-mount from window.location to avoid a
  // useSearchParams()-driven hydration mismatch. Producer-only.
  useEffect(() => {
    if (typeof window === 'undefined') return
    const wanted = new URLSearchParams(window.location.search).get('view')
    if (wanted === 'record' && user?.role === 'producer') setView('record')
  }, [user])

  const handleVoiceSelect = async (voiceId: string) => {
    if (!selectedAvatar) {
      toast('Select an avatar first to assign this voice', { icon: '💡' })
      return
    }
    try {
      await api.setAvatarVoice(selectedAvatar, voiceId)
      toast.success('Voice assigned to avatar', { icon: '🎙️' })
    } catch {
      toast.error('Failed to assign voice')
    }
  }

  const handleSelectAvatar = (id: string) => {
    setSelectedAvatar(id)
    setResumeSessionId(null)  // picking an avatar starts a fresh conversation
  }

  const handleStartChat = () => {
    if (selectedAvatar) {
      setResumeSessionId(null)  // "Start Conversation" = fresh session
      setView('chat')
    }
  }

  const handleResumeFromHistory = (avatarId: string, sessionId: string) => {
    setSelectedAvatar(avatarId)
    setResumeSessionId(sessionId)  // resume this exact conversation
    setView('chat')
  }

  // Avatar + voice setup only make sense in avatar mode. In the video-clip
  // modes the story clips carry the producer's real face/voice, so hide those
  // tabs entirely (the avatar is auto-resolved under the hood — see below).
  const isProducerUser = user?.role === 'producer'
  const navItems: { id: View; icon: typeof Sparkles; label: string; disabled?: boolean }[] = [
    { id: 'home', icon: Sparkles, label: 'Home' },
    // Recording lives inside the shell now (was the /record route). Producer-only.
    ...(isProducerUser ? [{ id: 'record' as View, icon: Feather, label: 'Record' }] : []),
    // Shown even with no relations captured yet: the empty state is where a
    // producer learns that family comes from recording, so hiding it would
    // hide the only explanation of how to fill it.
    ...(isProducerUser ? [{ id: 'tree' as View, icon: Network, label: 'Family' }] : []),
    ...(isProducerUser ? [{ id: 'timeline' as View, icon: CalendarRange, label: 'Timeline' }] : []),
    ...(isVideoClipMode
      ? []
      : [
          { id: 'avatars' as View, icon: Camera, label: 'Avatars' },
          { id: 'voice' as View, icon: Mic2, label: 'Voice' },
        ]),
    // Chat is reachable without manually picking an avatar in video-clip mode;
    // the auto-resolve effect below fills selectedAvatar in.
    { id: 'chat', icon: MessageCircle, label: 'Chat', disabled: !selectedAvatar && !isVideoClipMode },
    { id: 'history', icon: History, label: 'History' },
    { id: 'settings', icon: Settings, label: 'Settings' },
  ]

  // Render nothing of the producer studio while the redirect above is in
  // flight — router.replace fires from a useEffect (after this render),
  // so without this the full producer UI would still flash for a frame.
  if (isFamilyUser) {
    return (
      <div className="min-h-screen flex items-center justify-center">
        <span className="inline-block w-2 h-2 rounded-full bg-primary-500 animate-pulse" />
      </div>
    )
  }

  return (
    <div className="min-h-screen">
      {/* ── Auth gate ── */}
      {!isAuthenticated() && <AuthModal />}

      {/* ── Navigation ── */}
      <nav className="fixed top-0 left-0 right-0 z-50 h-16">
        <div className="h-full mx-auto max-w-7xl px-6 flex items-center justify-between">
          <div className="flex items-center">
            {/* The real wordmark, not an icon plus type: "life capsule" is
                one locked-up mark. The dark-surface variant is used because
                this bar is always dark — "capsule" is pure black in the
                source file and would be invisible on it. */}
            <Image
              src="/lifecapsule-wordmark.png"
              alt="Life Capsule"
              width={132}
              height={57}
              priority
              className="h-9 w-auto"
            />
          </div>

          <div className="flex items-center gap-1 p-1 rounded-xl bg-surface-800/80 backdrop-blur-xl border border-white/8 overflow-x-auto">
            {navItems.map(({ id, icon: Icon, label, disabled }) => (
              <button
                key={id}
                onClick={() => !disabled && setView(id)}
                disabled={disabled || undefined}
                aria-current={view === id ? 'page' : undefined}
                className={`flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-sm font-medium transition-all duration-200 flex-shrink-0
                  ${view === id
                    ? 'bg-gradient-to-r from-primary-600/80 to-accent-600/80 text-white shadow-glow-sm'
                    : 'text-gray-400 hover:text-white hover:bg-white/5 disabled:opacity-30 disabled:cursor-not-allowed'
                  }`}
              >
                <Icon size={14} />
                <span className="hidden sm:inline">{label}</span>
              </button>
            ))}
          </div>

          <div className="flex items-center gap-3">
            {/* "Record" now lives in the nav bar (left) as an in-shell view. */}
            <ConnectionStatus />
            {/* The bell, its dropdown, the full-screen list and whatever a row
                opens — all of it owns its own state. It needs to know about
                the record screen for two opposite reasons: nothing may open
                over a live camera (the first questions arrive seconds after a
                producer's first recording, possibly part-way through the
                next), and LEAVING it is exactly when the nudge should fire. */}
            {isProducerUser && <NotificationCenter onRecordScreen={view === 'record'} />}
            <ThemeToggle />
            {user && (
              <div className="flex items-center gap-2">
                <span className="text-xs text-gray-400 hidden sm:block">{user.username}</span>
                <button
                  onClick={() => { api.logout(); clearAuth() }}
                  className="text-xs text-gray-500 hover:text-red-400 transition-colors px-2 py-1 rounded-lg hover:bg-red-500/10"
                  title="Sign out"
                >
                  Sign out
                </button>
              </div>
            )}
          </div>
        </div>
        {/* nav glass blur border */}
        <div className="absolute inset-0 -z-10 bg-surface-900/70 backdrop-blur-xl border-b border-white/6" />
      </nav>

      <main className="pt-16">
        {/* ── HOME VIEW ── */}
        {view === 'home' && (
          <div className="animate-fade-in">
            {/* Hero */}
            <section className="relative flex flex-col items-center justify-center min-h-[calc(100vh-4rem)] px-6 text-center overflow-hidden">
              {/* Aurora background */}
              <div className="absolute inset-0 overflow-hidden pointer-events-none">
                <div className="absolute -top-40 -left-40 w-96 h-96 bg-primary-600/20 rounded-full blur-3xl animate-float" />
                <div className="absolute -top-20 -right-40 w-80 h-80 bg-accent-600/15 rounded-full blur-3xl animate-float" style={{ animationDelay: '2s' }} />
                <div className="absolute bottom-20 left-1/4 w-72 h-72 bg-primary-800/20 rounded-full blur-3xl animate-float" style={{ animationDelay: '4s' }} />
                <div className="absolute top-1/2 left-1/2 -translate-x-1/2 -translate-y-1/2 w-[600px] h-[600px] rounded-full opacity-5"
                  style={{ background: 'radial-gradient(circle, #a855f7 0%, transparent 70%)' }} />
              </div>

              {/* Badge */}
              <div className="inline-flex items-center gap-2 px-4 py-1.5 rounded-full bg-primary-500/10 border border-primary-500/30 mb-8 animate-slide-up">
                <Sparkles size={14} className="text-primary-400" />
                <span className="text-sm text-primary-300 font-medium">A family&apos;s story, in their own voice</span>
              </div>

              {/* Headline */}
              <h1 className="text-6xl md:text-7xl lg:text-8xl font-black leading-none mb-6 tracking-tight animate-slide-up" style={{ animationDelay: '0.1s' }}>
                <span className="gradient-text">Their stories,</span>
                <br />
                <span className="text-white">in their own</span>
                <br />
                <span className="gradient-text-gold">words.</span>
              </h1>

              <p className="max-w-2xl text-lg md:text-xl text-gray-400 mb-10 leading-relaxed animate-slide-up" style={{ animationDelay: '0.2s' }}>
                Someone in your family has a lifetime of stories. We ask the questions, they
                record the answers, and everything they say is kept exactly as they said it.
                Years from now your family can ask about a name, a place, a year — and watch
                them answer, in their own voice.
              </p>

              {/* CTAs */}
              <div className="flex flex-wrap items-center justify-center gap-4 animate-slide-up" style={{ animationDelay: '0.3s' }}>
                <button
                  onClick={() => setView('record')}
                  className="btn-primary text-base px-8 py-3.5 rounded-2xl group"
                >
                  <Play size={18} className="group-hover:scale-110 transition-transform" />
                  Start recording
                  <ChevronRight size={16} className="group-hover:translate-x-1 transition-transform" />
                </button>
                {/* Was "Clone a Voice", pointing at voice cloning — which belongs
                    to the avatar mode and contradicts the whole pitch. Every
                    word on this page promises real recorded speech; a CTA
                    offering a synthesised voice undoes that in one click. */}
                <button
                  onClick={() => setView('record')}
                  className="btn-secondary text-base px-8 py-3.5 rounded-2xl"
                >
                  <Feather size={18} />
                  See how it works
                </button>
              </div>

              {/* Stats */}
              <div className="flex flex-wrap items-center justify-center gap-8 mt-16 animate-slide-up" style={{ animationDelay: '0.4s' }}>
                {STATS.map(({ value, label }) => (
                  <div key={label} className="text-center">
                    <div className="text-3xl font-black gradient-text">{value}</div>
                    <div className="text-sm text-gray-500 mt-1">{label}</div>
                  </div>
                ))}
              </div>
            </section>

            {/* Features */}
            <section className="px-6 pb-24 max-w-7xl mx-auto">
              <div className="text-center mb-14">
                <h2 className="text-4xl font-black mb-4">
                  Everything they say,
                  <span className="gradient-text"> kept as they said it</span>
                </h2>
                <p className="text-gray-400 text-lg max-w-2xl mx-auto">
                  No scripts, no rewriting, and nothing invented. Just their answers, ready when someone asks.
                </p>
              </div>

              <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
                {FEATURES.map(({ icon: Icon, title, description, color, glow }) => (
                  <div
                    key={title}
                    className="feature-card group"
                    style={{ '--glow': glow } as CSSProperties}
                  >
                    <div className={`w-12 h-12 rounded-xl bg-gradient-to-br ${color} flex items-center justify-center mb-4 shadow-lg group-hover:scale-110 transition-transform duration-300`}>
                      <Icon size={22} className="text-white" />
                    </div>
                    <h3 className="font-bold text-lg text-white mb-2">{title}</h3>
                    <p className="text-gray-400 text-sm leading-relaxed">{description}</p>
                  </div>
                ))}
              </div>
            </section>
          </div>
        )}

        {/* ── AVATAR VIEW ── */}
        {view === 'avatars' && (
          <div className="max-w-7xl mx-auto px-6 py-10 animate-fade-in">
            <div className="mb-8">
              <h1 className="text-3xl font-black gradient-text mb-2">Avatar Studio</h1>
              <p className="text-gray-400">Upload photos and manage your avatar collection.</p>
            </div>
            <div className="grid grid-cols-1 lg:grid-cols-2 gap-8">
              <AvatarUpload />
              <AvatarList
                selectedAvatar={selectedAvatar}
                onSelectAvatar={handleSelectAvatar}
              />
            </div>
            {selectedAvatar && (
              <div className="mt-8 flex justify-center">
                <button
                  onClick={handleStartChat}
                  className="btn-primary text-lg px-10 py-4 rounded-2xl group"
                >
                  <MessageCircle size={20} />
                  Start Conversation
                  <ChevronRight size={18} className="group-hover:translate-x-1 transition-transform" />
                </button>
              </div>
            )}
          </div>
        )}

        {/* ── VOICE VIEW ── */}
        {view === 'voice' && (
          <div className="max-w-4xl mx-auto px-6 py-10 animate-fade-in">
            <div className="mb-8">
              <h1 className="text-3xl font-black gradient-text mb-2">Voice Studio</h1>
              <p className="text-gray-400">Clone voices and manage your voice library.</p>
            </div>
            <VoicePanel onVoiceSelect={handleVoiceSelect} />
          </div>
        )}

        {/* ── CHAT VIEW ── */}
        {/* Video-clip modes keep the PRODUCER's own layout (side chat + single
            video panel) but share /talk's conversation behavior via the
            useVideoClipChat hook — this is what fixes the old hang on "Finding
            a clip…" (ChatInterface, the avatar-only path, never handled
            video_clip_response). Avatar mode stays on ChatInterface. */}
        {view === 'chat' && selectedAvatar && isVideoClipMode && (
          <ProducerVideoClipChat key={selectedAvatar} avatarId={selectedAvatar} />
        )}

        {view === 'chat' && selectedAvatar && !isVideoClipMode && (
          <div className="max-w-7xl mx-auto px-6 py-10 animate-fade-in">
            <div className="mb-6">
              <h1 className="text-3xl font-black gradient-text mb-2">Live Conversation</h1>
              <p className="text-gray-400">Talk to your AI avatar in real time.</p>
            </div>
            <ChatInterface
              key={`${selectedAvatar}:${resumeSessionId ?? 'new'}`}
              avatarId={selectedAvatar}
              resumeSessionId={resumeSessionId ?? undefined}
              onSessionCreated={setActiveSessionId}
            />
          </div>
        )}

        {/* Video-clip mode, avatar still resolving — brief loader (the Avatars
            tab is hidden, so the "pick an avatar" redirect below never applies). */}
        {view === 'chat' && !selectedAvatar && isVideoClipMode && avatarResolving && (
          <div className="max-w-7xl mx-auto px-6 py-10 flex justify-center">
            <PanelLoader label="Loading your stories…" />
          </div>
        )}

        {/* Video-clip mode but the producer has no avatar at all — clips still
            need one to anchor a session. Guide them rather than dead-end. */}
        {view === 'chat' && !selectedAvatar && isVideoClipMode && !avatarResolving && (
          <div className="max-w-lg mx-auto px-6 py-10 text-center">
            <p className="text-gray-300 mb-2 font-medium">One quick setup step left</p>
            <p className="text-gray-500 mb-4 text-sm">
              Your story clips play under your own face, but a conversation still
              needs one avatar image on file. Add a photo in Avatar mode, then
              switch back — you won&apos;t have to pick it each time.
            </p>
            <button onClick={() => setView('settings')} className="btn-primary">
              <Settings size={18} />
              Open Settings
            </button>
          </div>
        )}

        {/* Avatar mode, no avatar selected — redirect to Avatar Studio. */}
        {view === 'chat' && !selectedAvatar && !isVideoClipMode && (
          <div className="max-w-7xl mx-auto px-6 py-10 text-center">
            <p className="text-gray-400 mb-4">Please select an avatar first.</p>
            <button onClick={() => setView('avatars')} className="btn-primary">
              <Camera size={18} />
              Go to Avatar Studio
            </button>
          </div>
        )}

        {/* ── HISTORY VIEW ── */}
        {view === 'history' && (
          <HistoryPanel onResume={handleResumeFromHistory} />
        )}

        {/* ── SETTINGS VIEW ── */}
        {view === 'settings' && (
          <SettingsPanel />
        )}

        {/* ── RECORD VIEW ── (was the standalone /record route) */}
        {view === 'record' && (
          // No width here. RecordPanel sets its own max-w-7xl to match /chat;
          // a max-w-4xl wrapper silently clamped it and no change inside the
          // component could ever have widened it.
          <div className="animate-fade-in">
            <RecordPanel />
          </div>
        )}

        {view === 'tree' && <FamilyTreePanel />}

        {view === 'timeline' && <TimelinePanel />}
      </main>
    </div>
  )
}
