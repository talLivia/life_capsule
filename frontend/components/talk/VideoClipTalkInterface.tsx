'use client'

import { useCallback, useEffect, useRef } from 'react'
import { Send, Mic, MicOff, Loader2, Film } from 'lucide-react'
import { TurnPhotoGallery } from '@/components/media/TurnPhotoGallery'
import { useVideoClipChat } from '@/lib/useVideoClipChat'

interface VideoClipTalkInterfaceProps {
  producerName: string
}

/**
 * Original-video-clip chat mode (Prompt 13/14) — the FAMILY /talk layout for
 * an alternative to TalkInterface (avatar mode), selected via the producer's
 * Settings screen. All of the conversation behavior (session, WS contract,
 * clip-playback gating, mic gating) lives in useVideoClipChat — this component
 * is layout only. The producer's in-app chat screen shares the SAME hook
 * behind a DIFFERENT layout (see ProducerVideoClipChat): two layouts, one
 * behavior. Each answer renders as a normal <video controls> player rather
 * than an always-on avatar panel.
 */
export function VideoClipTalkInterface({ producerName }: VideoClipTalkInterfaceProps) {
  const {
    messages,
    inputText,
    setInputText,
    isThinking,
    statusText,
    connected,
    setIsClipPlaying,
    isClipPlaying,
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
  } = useVideoClipChat()

  const messagesEndRef = useRef<HTMLDivElement>(null)
  const scrollToBottom = useCallback(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [])
  useEffect(scrollToBottom, [messages, scrollToBottom])

  return (
    <div className="min-h-screen bg-calm-paper dark:bg-calm-paperDark text-calm-ink dark:text-calm-inkDark flex flex-col">
      <header className="max-w-2xl mx-auto w-full px-6 pt-8 pb-4 flex items-center gap-2">
        <Film size={16} className="text-calm-sage-600 dark:text-calm-sage-300" />
        <p className="text-sm text-calm-inkmuted dark:text-calm-inkmutedDark">
          Watching {producerName}&apos;s stories
        </p>
        <span className="ml-auto text-xs text-calm-inkmuted dark:text-calm-inkmutedDark">
          {!connected
            ? 'Reconnecting…'
            : micUnavailable
              ? 'No microphone — type below'
              : micMuted
                ? 'Mic muted'
                : hearingSpeech
                ? 'Hearing you…'
                : isThinking
                  ? statusText
                  : isClipPlaying || clipGrace
                    ? 'Clip playing…'
                    : 'Listening…'}
        </span>
      </header>

      <main className="max-w-2xl mx-auto w-full flex-1 flex flex-col px-6 gap-6">
        <div className="flex-1 overflow-y-auto flex flex-col gap-3 pb-4 messages-scroll">
          {messages.map((m) => (
            <div
              key={m.id}
              className={`max-w-[85%] ${m.role === 'user' ? 'self-end' : 'self-start'}`}
            >
              {m.videoUrl ? (
                <div className="flex flex-col gap-2">
                  <video
                    src={m.videoUrl}
                    controls
                    autoPlay
                    onPlay={() => setIsClipPlaying(true)}
                    onPause={() => setIsClipPlaying(false)}
                    onEnded={() => setIsClipPlaying(false)}
                    className="w-full rounded-2xl border border-calm-border dark:border-calm-borderDark"
                  />
                  {/* What the clip actually says. Without it a past turn is
                      an unskimmable player you'd have to replay to recall. */}
                  {m.content && (
                    <p className="px-4 py-2.5 rounded-2xl text-sm leading-relaxed bg-calm-card dark:bg-calm-cardDark border border-calm-border dark:border-calm-borderDark">
                      {m.content}
                    </p>
                  )}
                  {/* Photos from the life period(s) the footage came from —
                      under the panel, never inside the video (§9.4). Renders
                      nothing when those periods have no photos. */}
                  {m.photoCategories && m.photoCategories.length > 0 && (
                    <TurnPhotoGallery categories={m.photoCategories} />
                  )}
                </div>
              ) : (
                <div
                  className={`px-4 py-2.5 rounded-2xl text-sm leading-relaxed ${
                    m.role === 'user'
                      ? 'bg-calm-sage-600 text-white rounded-br-sm'
                      : `border border-calm-border dark:border-calm-borderDark rounded-bl-sm ${
                          m.noStory
                            ? 'italic text-calm-inkmuted dark:text-calm-inkmutedDark bg-calm-card dark:bg-calm-cardDark'
                            : 'bg-calm-card dark:bg-calm-cardDark'
                        }`
                  }`}
                >
                  {m.content}
                  {/* Proactive offer — chat text with Yes/No. "Yes" re-asks it
                      as a normal question so it takes the same path. */}
                  {/* "Which אמנון did you mean?" — one button per person.
                      Choosing re-asks the original question with that person
                      named, through the same path as any other question. */}
                  {/* The lookup failed — offer the same question again rather
                      than making the listener retype it. */}
                  {m.retryQuestion && !m.retryDismissed && (
                    <div className="flex items-center gap-2 mt-2.5">
                      <button
                        onClick={() => retryQuestion(m.id, m.retryQuestion!)}
                        className="calm-btn-secondary !py-1.5 !px-4 text-xs"
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
                          className="calm-btn-secondary !py-1.5 !px-4 text-xs"
                        >
                          {option}
                        </button>
                      ))}
                    </div>
                  )}
                  {m.followUpQuestion && !m.followUpDismissed && (
                    <div className="flex items-center gap-2 mt-2.5">
                      <button
                        onClick={() => acceptFollowUp(m.id, m.followUpQuestion!)}
                        className="calm-btn-primary !py-1.5 !px-4 text-xs"
                      >
                        כן
                      </button>
                      <button
                        onClick={() => declineFollowUp(m.id)}
                        className="calm-btn-secondary !py-1.5 !px-4 text-xs"
                      >
                        לא
                      </button>
                    </div>
                  )}
                </div>
              )}
            </div>
          ))}
          {isThinking && (
            <div className="self-start flex items-center gap-1.5 text-calm-inkmuted dark:text-calm-inkmutedDark text-sm px-4 py-2.5">
              <Loader2 size={14} className="animate-spin" />
              {statusText}
            </div>
          )}
          <div ref={messagesEndRef} />
        </div>
      </main>

      {/* Input bar */}
      <div className="max-w-2xl mx-auto w-full px-6 pb-8 pt-2 flex items-center gap-2">
        <button
          onClick={() => setMicMuted((m) => !m)}
          aria-label={micMuted ? 'Unmute microphone' : 'Mute microphone'}
          aria-pressed={micMuted}
          title={
            micMuted
              ? 'Microphone muted — click to resume hands-free listening'
              : 'Listening automatically — click to mute'
          }
          className={`w-11 h-11 rounded-full flex items-center justify-center flex-shrink-0 transition-all
            ${
              micMuted
                ? 'calm-btn-secondary !rounded-full !p-0'
                : hearingSpeech
                  ? 'bg-red-500 text-white'
                  : isListening
                    ? 'bg-calm-sage-500 text-white'
                    : 'calm-btn-secondary !rounded-full !p-0'
            }`}
        >
          {micMuted ? <MicOff size={18} /> : <Mic size={18} />}
        </button>
        <input
          value={inputText}
          onChange={(e) => setInputText(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && sendText()}
          placeholder="Ask a question…"
          className="flex-1 px-4 py-3 rounded-xl border border-calm-border dark:border-calm-borderDark bg-calm-card dark:bg-calm-cardDark text-calm-ink dark:text-calm-inkDark placeholder:text-calm-inkmuted"
        />
        <button
          onClick={sendText}
          disabled={!inputText.trim()}
          aria-label="Send"
          className="calm-btn-primary !rounded-full w-11 h-11 !p-0 flex-shrink-0"
        >
          <Send size={16} />
        </button>
      </div>
    </div>
  )
}
