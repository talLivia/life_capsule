'use client'

import { useEffect, useRef } from 'react'
import {
  Send, Mic, MicOff, Film, MessageCircle, Sparkles, Wand2, Video, Loader2,
} from 'lucide-react'
import { TurnPhotoGallery } from '@/components/media/TurnPhotoGallery'
import { useVideoClipChat } from '@/lib/useVideoClipChat'

interface ProducerVideoClipChatProps {
  avatarId: string
}

/**
 * The PRODUCER's in-app chat screen for the video-clip modes (video_clips /
 * video_clips_v2) — the producer previewing their own story archive.
 *
 * Deliberately keeps the producer studio's ORIGINAL chat layout: a single
 * video panel on the left that updates in place, and a chat panel on the
 * right. Only the layout is bespoke — every bit of behavior (session, WS
 * contract, video_clip_response/no_story handling, clip-playback + mic gating)
 * comes from useVideoClipChat, the SAME hook the family /talk screen
 * (VideoClipTalkInterface) uses. Two layouts, one shared behavior.
 *
 * The backend picks v1 (chunk retrieval) vs v2 (full-archive reading) from the
 * producer's own chat_mode; the client contract is identical for both, so this
 * one component serves both video-clip modes.
 */
export function ProducerVideoClipChat({ avatarId }: ProducerVideoClipChatProps) {
  const {
    messages,
    inputText,
    setInputText,
    isThinking,
    statusText,
    connected,
    isClipPlaying,
    setIsClipPlaying,
    clipGrace,
    sendText,
    acceptFollowUp,
    declineFollowUp,
    chooseClarification,
    retryQuestion,
    micUnavailable,
    micMuted,
    setMicMuted,
    isListening,
    hearingSpeech,
    micLevel,
  } = useVideoClipChat(avatarId)

  // The single video panel plays the MOST RECENT clip, updating in place as
  // new answers arrive (rather than stacking a player per answer like the
  // family layout). Keyed by message id so a new src autoplays.
  const latestClip = [...messages].reverse().find((m) => m.videoUrl)

  // This screen shows ONE video that is replaced in place (keyed by clip id),
  // unlike /talk which mounts a player per answer. Replacing a still-PLAYING
  // element unmounts it, and an unmounted element fires neither onPause nor
  // onEnded — so isClipPlaying would stay true forever and the mic would
  // never reopen ("I speak and nothing gets in"). Reset the gate whenever the
  // clip changes; the incoming video re-closes it via onPlay if it plays.
  useEffect(() => {
    setIsClipPlaying(false)
  }, [latestClip?.id, setIsClipPlaying])

  const messagesEndRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages, isThinking])

  const clipActive = isClipPlaying || clipGrace

  return (
    <div className="max-w-7xl mx-auto px-6 py-10 animate-fade-in">
      <div className="mb-6">
        <h1 className="text-3xl font-black gradient-text mb-2">Live Conversation</h1>
        <p className="text-gray-400">Ask a question and watch the matching moment from your story.</p>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-5 gap-6 h-[calc(100vh-16rem)]">
        {/* ── Video Panel (single, updates in place) ────────────────────── */}
        <div className="lg:col-span-3 flex flex-col gap-4">
          <div className="card-glow flex-1 relative overflow-hidden rounded-2xl">
            <div className="aspect-video w-full bg-surface-950 rounded-xl overflow-hidden relative">
              {latestClip?.videoUrl ? (
                <video
                  key={latestClip.id}
                  src={latestClip.videoUrl}
                  controls
                  autoPlay
                  playsInline
                  onPlay={() => setIsClipPlaying(true)}
                  onPause={() => setIsClipPlaying(false)}
                  onEnded={() => setIsClipPlaying(false)}
                  className="absolute inset-0 w-full h-full object-contain bg-surface-950"
                />
              ) : (
                <div className="absolute inset-0 flex flex-col items-center justify-center gap-4">
                  <div className="w-20 h-20 rounded-full bg-gradient-to-br from-primary-600/30 to-accent-600/20
                                  flex items-center justify-center border border-white/10 animate-pulse-slow">
                    <Film size={36} className="text-primary-400" />
                  </div>
                  <p className="text-gray-500 text-sm">The matching clip will play here</p>
                </div>
              )}

              {/* Processing overlay while the backend transcribes / finds a clip */}
              {isThinking && (
                <div className="absolute inset-0 bg-surface-950/75 backdrop-blur-sm flex flex-col
                                items-center justify-center gap-4 z-20">
                  <div className="relative">
                    <div className="w-16 h-16 rounded-full border-2 border-primary-500/30 animate-spin-slow" />
                    <div className="absolute inset-2 rounded-full border-2 border-t-primary-400
                                    border-r-transparent border-b-transparent border-l-transparent animate-spin" />
                    <Wand2 className="absolute inset-0 m-auto text-primary-400" size={20} />
                  </div>
                  <p className="text-sm text-gray-300 font-medium animate-pulse">{statusText}</p>
                </div>
              )}
            </div>

            {/* Photos from the life period(s) the current clip's footage
                came from (MEDIA_GALLERY.md §9.4, extended to this screen by
                a producer decision 2026-08-11) — under the panel, tracking
                the clip the panel shows. Renders nothing when those periods
                have no photos. */}
            {latestClip?.photoCategories && latestClip.photoCategories.length > 0 && (
              <div className="mt-4 px-1">
                <TurnPhotoGallery
                  categories={latestClip.photoCategories}
                  variant="app"
                />
              </div>
            )}

            {/* Status bar */}
            <div className="flex items-center justify-between mt-4 px-1">
              <div className="flex items-center gap-1.5 text-xs">
                <span className={`status-dot ${connected ? 'online' : 'processing'}`} />
                <span className="text-gray-400">
                  {!connected
                    ? 'Reconnecting…'
                    : isThinking
                      ? statusText
                      : clipActive
                        ? 'Clip playing…'
                        : micUnavailable
                          ? 'No microphone — type below'
                          : 'Ready'}
                </span>
              </div>
              <div className="flex items-center gap-1.5 text-xs text-gray-500">
                <Video size={12} className="text-primary-400" />
                <span>Original story clips</span>
              </div>
            </div>
          </div>
        </div>

        {/* ── Chat Panel ─────────────────────────────────────────────────── */}
        <div className="lg:col-span-2 flex flex-col glass-card rounded-2xl overflow-hidden p-0">
          <div className="flex items-center justify-between px-5 py-4 border-b border-white/8">
            <div className="flex items-center gap-2">
              <MessageCircle size={16} className="text-primary-400" />
              <span className="font-semibold text-white">Conversation</span>
            </div>
            <div className="flex items-center gap-1.5 text-xs text-gray-500">
              <span>{messages.filter((m) => m.role === 'user').length} questions</span>
            </div>
          </div>

          <div className="flex-1 overflow-y-auto px-4 py-4 space-y-4 messages-scroll">
            {messages.length === 0 ? (
              <div className="flex flex-col items-center justify-center h-full gap-4 py-12 text-center">
                <div className="w-16 h-16 rounded-2xl bg-gradient-to-br from-primary-600/20 to-accent-600/10
                                flex items-center justify-center border border-white/8">
                  <Sparkles size={28} className="text-primary-400" />
                </div>
                <div>
                  <p className="text-white font-medium mb-1">Ask about a memory</p>
                  <p className="text-gray-500 text-sm">Type a question or press the mic button</p>
                </div>
              </div>
            ) : (
              messages.map((m) => {
                const isUser = m.role === 'user'
                return (
                  <div key={m.id} className={`flex gap-2.5 ${isUser ? 'flex-row-reverse' : 'flex-row'}`}>
                    <div className={`w-7 h-7 rounded-full flex-shrink-0 flex items-center justify-center text-xs font-bold
                      ${isUser
                        ? 'bg-gradient-to-br from-accent-600 to-accent-800'
                        : 'bg-gradient-to-br from-primary-600 to-primary-800'
                      }`}
                    >
                      {isUser ? 'U' : <Film size={13} />}
                    </div>
                    <div className={`max-w-[85%] px-4 py-2.5 rounded-2xl text-sm leading-relaxed
                      ${isUser
                        ? 'bg-gradient-to-br from-primary-700/80 to-accent-700/60 text-white rounded-tr-sm'
                        : m.videoUrl
                          ? 'bg-surface-700/80 border border-primary-500/30 text-primary-200 rounded-tl-sm'
                          : `bg-surface-700/80 border border-white/8 rounded-tl-sm ${m.noStory ? 'italic text-gray-400' : 'text-gray-200'}`
                      }`}
                    >
                      {/* The clip's words, not "Playing the matching clip →".
                          The video panel already shows the clip; this panel's
                          job is to be readable after the fact. */}
                      {m.videoUrl && !m.content
                        ? 'Playing the matching clip →'
                        : m.content}
                      {/* "Which אמנון did you mean?" — one button per person.
                          Choosing re-asks the original question with that
                          person named, through the same path as any other. */}
                      {/* The lookup failed — offer the same question again rather
                      than making the listener retype it. */}
                  {m.retryQuestion && !m.retryDismissed && (
                    <div className="flex items-center gap-2 mt-2.5">
                      <button
                        onClick={() => retryQuestion(m.id, m.retryQuestion!)}
                        className="px-3 py-1 rounded-lg text-xs font-medium text-gray-200 bg-surface-700 border border-white/10 hover:bg-surface-600 transition-all active:scale-95"
                      >
                        נסה שוב
                      </button>
                    </div>
                  )}
                  {m.clarifyOptions && !m.clarifyDismissed && (
                        <div className="flex flex-wrap items-center gap-2 mt-2.5">
                          {m.clarifyOptions.map((option) => (
                            <button
                              key={option}
                              onClick={() =>
                                chooseClarification(m.id, option, m.clarifyFor ?? '')
                              }
                              className="px-3 py-1 rounded-lg text-xs font-medium text-gray-200
                                         bg-surface-700 border border-white/10 hover:bg-surface-600
                                         transition-all active:scale-95"
                            >
                              {option}
                            </button>
                          ))}
                        </div>
                      )}
                      {/* Proactive offer — chat text with Yes/No. "Yes" re-asks
                          it as a normal question so it takes the same path. */}
                      {m.followUpQuestion && !m.followUpDismissed && (
                        <div className="flex items-center gap-2 mt-2.5">
                          <button
                            onClick={() => acceptFollowUp(m.id, m.followUpQuestion!)}
                            className="px-3 py-1 rounded-lg text-xs font-medium text-white
                                       bg-gradient-to-br from-primary-600 to-accent-600
                                       hover:shadow-glow transition-all active:scale-95"
                          >
                            כן
                          </button>
                          <button
                            onClick={() => declineFollowUp(m.id)}
                            className="px-3 py-1 rounded-lg text-xs font-medium text-gray-300
                                       bg-surface-700 border border-white/10 hover:bg-surface-600
                                       transition-all active:scale-95"
                          >
                            לא
                          </button>
                        </div>
                      )}
                    </div>
                  </div>
                )
              })
            )}
            {isThinking && (
              <div className="flex items-center gap-1.5 text-gray-500 text-sm px-2">
                <Loader2 size={14} className="animate-spin" />
                {statusText}
              </div>
            )}
            <div ref={messagesEndRef} />
          </div>

          {isListening && (
            <div className="px-4 pb-2">
              <div className="h-1 rounded-full bg-surface-700 overflow-hidden">
                <div className="voice-level h-full" style={{ width: `${Math.min(100, micLevel * 2)}%` }} />
              </div>
            </div>
          )}

          <div className="border-t border-white/8 px-4 py-3">
            {!micMuted && (
              <div className="flex items-center gap-2 mb-3 px-2">
                <span className={`text-xs font-medium ${hearingSpeech ? 'text-red-400 animate-pulse' : 'text-gray-500'}`}>
                  {micUnavailable
                    ? 'NO MICROPHONE'
                    : hearingSpeech
                      ? 'HEARING YOU'
                      : clipActive
                        ? 'CLIP PLAYING'
                        : isThinking
                          ? 'WORKING…'
                          : 'LISTENING'}
                </span>
              </div>
            )}

            <div className="flex gap-2 items-end">
              <button
                onClick={() => setMicMuted((v) => !v)}
                aria-label={micMuted ? 'Unmute microphone' : 'Mute microphone'}
                aria-pressed={micMuted}
                title={micMuted ? 'Microphone muted — click to resume hands-free listening' : 'Listening automatically — click to mute'}
                className={`relative flex-shrink-0 w-10 h-10 rounded-xl flex items-center justify-center
                  transition-all duration-200 active:scale-95
                  ${hearingSpeech
                    ? 'bg-red-600 hover:bg-red-500 text-white shadow-[0_0_20px_rgba(239,68,68,0.5)]'
                    : !micMuted && isListening
                      ? 'bg-primary-600 hover:bg-primary-500 text-white'
                      : 'bg-surface-700 hover:bg-surface-600 border border-white/10 hover:border-primary-500/40 text-gray-400 hover:text-white'
                  }`}
              >
                {micMuted ? <MicOff size={18} /> : <Mic size={18} />}
              </button>

              <div className="flex-1 relative">
                <textarea
                  value={inputText}
                  onChange={(e) => setInputText(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendText() }
                  }}
                  placeholder="Ask about a memory… (Enter to send)"
                  aria-label="Ask about a memory"
                  rows={1}
                  className="w-full px-4 py-2.5 rounded-xl bg-surface-700/80 border border-white/10 text-white text-sm
                             placeholder:text-gray-600 focus:outline-none focus:ring-2 focus:ring-primary-500/50
                             focus:border-primary-500/40 resize-none transition-all duration-200
                             [field-sizing:content] max-h-32 overflow-y-auto"
                />
              </div>

              <button
                onClick={sendText}
                disabled={!inputText.trim()}
                aria-label="Send question"
                className="flex-shrink-0 w-10 h-10 rounded-xl bg-gradient-to-br from-primary-600 to-accent-600
                           flex items-center justify-center text-white hover:shadow-glow
                           disabled:opacity-30 disabled:cursor-not-allowed transition-all duration-200 active:scale-95"
              >
                <Send size={18} />
              </button>
            </div>

            <p className="text-xs text-gray-600 text-center mt-2">
              Mic listens automatically · Enter to send
            </p>
          </div>
        </div>
      </div>
    </div>
  )
}
