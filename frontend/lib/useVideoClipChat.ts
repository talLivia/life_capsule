'use client'

import { useCallback, useEffect, useRef, useState } from 'react'
import { toast } from 'react-hot-toast'
import { api, buildSessionWsUrl } from '@/lib/api'
import { useContinuousVoiceInput } from '@/lib/useContinuousVoiceInput'
import type { WsMessage } from '@/lib/types'

export interface TalkMessage {
  id: string
  role: 'user' | 'assistant'
  content: string
  videoUrl?: string
  noStory?: boolean
  // A proactive "want to hear more about X?" offer. Rendered as chat text
  // with Yes/No; it is never spoken and never part of any video. Cleared
  // (dismissed) once answered either way, so it can't be actioned twice.
  followUpQuestion?: string
  followUpDismissed?: boolean
  // Two people in this archive share a name and the question did not say
  // which. Rendered as chat text with one button per person. Unlike a
  // follow-up this arrives INSTEAD of an answer, never alongside one — a
  // best guess plus "or did you mean the other?" is the conflation the
  // whole feature exists to remove.
  clarifyOptions?: string[]
  // The question that was ambiguous, so choosing an option can re-ask the
  // ORIGINAL intent with the person named, rather than sending a bare name
  // and hoping it reads as a question.
  clarifyFor?: string
  clarifyDismissed?: boolean
  // The lookup failed — shown differently from "no story", and offering to
  // retry the SAME question rather than leaving the listener to retype it.
  retryQuestion?: string
  retryDismissed?: boolean
  // The life periods this answer's footage came from — /talk renders each
  // category's photo gallery under the clip (MEDIA_GALLERY.md §9.4).
  photoCategories?: string[]
}

const MAX_RECONNECT_ATTEMPTS = 5
// How long the mic stays gated after a clip URL arrives even if playback
// hasn't started yet (autoplay blocked / still loading) — see clipGrace.
const CLIP_GRACE_MS = 3000

/**
 * The shared BEHAVIOR of the original-video-clip chat mode (Prompt 13/14),
 * with no layout of its own. Both the family /talk screen
 * (VideoClipTalkInterface) and the producer's in-app chat screen consume this
 * hook and render their OWN layouts around the same view-model — so the two
 * screens can look different while running identical routing / WS-contract
 * handling / playback gating.
 *
 * On the SAME WebSocket contract as avatar mode (one session per
 * conversation), but sends 'video_clip_question' instead of 'text' and expects
 * a single finished 'video_clip_response' (or 'video_clip_no_story') per
 * question rather than a streamed sequence of lip-sync chunks. Spoken input
 * sends the SAME 'audio' message avatar mode uses — the backend's
 * _handle_audio_inner transcribes it and, based on the producer's own
 * chat_mode, routes the text to this mode's turn (never something the client
 * decides).
 */
export function useVideoClipChat() {
  const [messages, setMessages] = useState<TalkMessage[]>([])
  const [inputText, setInputText] = useState('')
  const [isThinking, setIsThinking] = useState(false)
  // What the "thinking" indicator says — driven by the backend's own status
  // stages ("Transcribing audio…" then "Finding a clip…") so a spoken turn
  // reads honestly: transcribe → the user's text appears → THEN the clip
  // search, rather than showing "Finding a clip…" during the STT wait.
  const [statusText, setStatusText] = useState('Finding a clip…')
  const [connected, setConnected] = useState(false)
  // Mirrors avatar mode's TTS-playback gating — without it the mic stays hot
  // while a clip's audio plays out loud, and the clip's own audio/echo
  // re-triggers the VAD into sending new "audio" segments mid-playback. Tracks
  // ANY registered clip's play state (a viewer could replay an earlier answer
  // while a newer one is already in the list).
  const [isClipPlaying, setIsClipPlaying] = useState(false)
  // Bridges the window between a clip URL ARRIVING and playback actually
  // starting. The clip autoplays, but if the browser blocks autoplay-with-sound
  // isClipPlaying would never engage and the mic would reopen the instant the
  // response lands. This keeps the mic gated for a short grace window after
  // arrival regardless.
  const [clipGrace, setClipGrace] = useState(false)
  const clipGraceTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  const sessionIdRef = useRef<string | null>(null)
  const wsRef = useRef<WebSocket | null>(null)
  const reconnectAttemptsRef = useRef(0)
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const startNewSessionRef = useRef<() => void>(() => {})

  const handleWsMessage = useCallback((msg: WsMessage) => {
    switch (msg.type) {
      case 'transcription':
        // Spoken turn: the user's words aren't known client-side until the
        // backend transcribes them (typed turns add their own bubble in
        // sendText), so surface the recognized text as the user bubble here —
        // it arrives just before the clip response.
        setMessages((prev) => [
          ...prev,
          { id: `user-voice-${Date.now()}`, role: 'user', content: msg.text },
        ])
        break
      case 'video_clip_response': {
        setIsThinking(false)
        const followUp = msg.follow_up?.question
        setMessages((prev) => [
          ...prev,
          {
            id: `clip-${Date.now()}`,
            role: 'assistant',
            // The clip's own words, shown WITH the player — a bare video
            // can't be skimmed and tells you nothing once scrolled past.
            content: msg.text || '',
            videoUrl: msg.video_url,
            photoCategories: msg.photo_categories ?? [],
          },
          // The offer is a SEPARATE chat message, never mixed into the clip —
          // the video stays verbatim footage with nothing added to it.
          ...(followUp
            ? [{
                id: `followup-${Date.now()}`,
                role: 'assistant' as const,
                content: followUp,
                followUpQuestion: followUp,
              }]
            : []),
        ])
        // Hold the mic gated briefly on arrival even if autoplay is blocked
        // (see clipGrace) — autoplay engaging isClipPlaying takes over for the
        // clip's real duration when it works.
        setClipGrace(true)
        if (clipGraceTimerRef.current) clearTimeout(clipGraceTimerRef.current)
        clipGraceTimerRef.current = setTimeout(() => setClipGrace(false), CLIP_GRACE_MS)
        break
      }
      case 'video_clip_clarify':
        setIsThinking(false)
        setMessages((prev) => [
          ...prev,
          {
            id: `clarify-${Date.now()}`,
            role: 'assistant',
            content: msg.question,
            clarifyOptions: msg.options,
            // The last thing the listener asked — the question this is a
            // clarification OF. Read from state rather than tracked
            // separately so it cannot disagree with what is on screen.
            clarifyFor: [...prev].reverse().find((m) => m.role === 'user')?.content ?? '',
          },
        ])
        break
      case 'video_clip_failed':
        setIsThinking(false)
        setMessages((prev) => [
          ...prev,
          {
            id: `failed-${Date.now()}`,
            role: 'assistant',
            content: msg.message,
            retryQuestion: msg.question,
          },
        ])
        break
      case 'video_clip_no_story': {
        setIsThinking(false)
        const noStoryFollowUp = msg.follow_up?.question
        setMessages((prev) => [
          ...prev,
          { id: `no-story-${Date.now()}`, role: 'assistant', content: msg.message, noStory: true },
          // A separate message, same as after a clip — the offer is not part
          // of the "I don't have that" sentence, it is the next thing said.
          ...(noStoryFollowUp
            ? [{
                id: `no-story-followup-${Date.now()}`,
                role: 'assistant' as const,
                content: noStoryFollowUp,
                followUpQuestion: noStoryFollowUp,
              }]
            : []),
        ])
        break
      }
      case 'status':
        setIsThinking(true)
        // Reflect the actual stage ("Transcribing audio…" / "Finding a clip…").
        if (msg.message) setStatusText(msg.message)
        break
      case 'error':
        setIsThinking(false)
        toast.error(msg.message)
        break
      default:
        break
    }
  }, [])

  const connectWs = useCallback(
    (sessionId: string) => {
      const socket = new WebSocket(buildSessionWsUrl(sessionId))
      wsRef.current = socket

      socket.onopen = () => {
        reconnectAttemptsRef.current = 0
        setConnected(true)
      }
      socket.onmessage = (event) => {
        try {
          handleWsMessage(JSON.parse(event.data))
        } catch {
          /* ignore malformed frames */
        }
      }
      socket.onclose = (event) => {
        setConnected(false)
        if (event.code === 4401) {
          reconnectAttemptsRef.current = 0
          toast.error('Your session is no longer valid — starting a new conversation')
          startNewSessionRef.current()
          return
        }
        if (reconnectAttemptsRef.current >= MAX_RECONNECT_ATTEMPTS) return
        reconnectAttemptsRef.current += 1
        reconnectTimerRef.current = setTimeout(
          () => connectWs(sessionId),
          Math.min(1000 * 2 ** reconnectAttemptsRef.current, 15000)
        )
      }
      socket.onerror = () => {
        socket.close()
      }
    },
    [handleWsMessage]
  )

  useEffect(() => {
    let cancelled = false

    const startNewSession = () => {
      const previousSessionId = sessionIdRef.current
      api
        .createSession()
        .then((session) => {
          if (cancelled) return
          if (previousSessionId) api.endSession(previousSessionId).catch(() => {})
          sessionIdRef.current = session.id
          connectWs(session.id)
        })
        .catch(() => toast.error('Could not start a conversation — please try again'))
    }
    startNewSessionRef.current = startNewSession
    startNewSession()

    return () => {
      cancelled = true
      if (reconnectTimerRef.current) clearTimeout(reconnectTimerRef.current)
      wsRef.current?.close()
      if (sessionIdRef.current) {
        api.endSession(sessionIdRef.current).catch(() => {})
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const sendText = useCallback(() => {
    const text = inputText.trim()
    if (!text || !wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) return
    // Typed input: text is known now, so its bubble + the clip search start
    // together.
    setMessages((prev) => [...prev, { id: `user-${Date.now()}`, role: 'user', content: text }])
    setStatusText('Finding a clip…')
    setIsThinking(true)
    wsRef.current.send(JSON.stringify({ type: 'video_clip_question', text }))
    setInputText('')
  }, [inputText])

  /** Accept a suggestion: ask it as a NORMAL question over the same WS
   *  contract, so it goes through identical validation and assembly — no
   *  shortcut that could bypass the never-invent guarantees. */
  const acceptFollowUp = useCallback((messageId: string, question: string) => {
    if (wsRef.current?.readyState !== WebSocket.OPEN) return
    setMessages((prev) => [
      ...prev.map((m) => (m.id === messageId ? { ...m, followUpDismissed: true } : m)),
      { id: `user-${Date.now()}`, role: 'user', content: question },
    ])
    setStatusText('Finding a clip…')
    setIsThinking(true)
    wsRef.current.send(JSON.stringify({ type: 'video_clip_question', text: question }))
  }, [])

  /** Answer "which one did you mean?" by re-asking the ORIGINAL question with
   *  the person named. Goes out as a normal question over the same WS
   *  contract, so the second turn gets identical validation and assembly —
   *  the same rule acceptFollowUp follows, and for the same reason. */
  const chooseClarification = useCallback(
    (messageId: string, option: string, original: string) => {
      if (wsRef.current?.readyState !== WebSocket.OPEN) return
      const question = original ? `${original} — ${option}` : option
      setMessages((prev) => [
        ...prev.map((m) => (m.id === messageId ? { ...m, clarifyDismissed: true } : m)),
        { id: `user-${Date.now()}`, role: 'user', content: question },
      ])
      setStatusText('Finding a clip…')
      setIsThinking(true)
      wsRef.current.send(JSON.stringify({ type: 'video_clip_question', text: question }))
    },
    [],
  )

  /** Re-ask the SAME question after a failed lookup. Same WS contract as any
   *  question, so the retry gets identical validation and assembly. */
  const retryQuestion = useCallback((messageId: string, question: string) => {
    if (wsRef.current?.readyState !== WebSocket.OPEN) return
    setMessages((prev) =>
      prev.map((m) => (m.id === messageId ? { ...m, retryDismissed: true } : m))
    )
    setStatusText('Finding a clip…')
    setIsThinking(true)
    wsRef.current.send(JSON.stringify({ type: 'video_clip_question', text: question }))
  }, [])

  const declineFollowUp = useCallback((messageId: string) => {
    setMessages((prev) =>
      prev.map((m) => (m.id === messageId ? { ...m, followUpDismissed: true } : m))
    )
  }, [])

  const sendAudioSegment = useCallback((base64Audio: string) => {
    if (wsRef.current?.readyState !== WebSocket.OPEN) return
    // Spoken input: we're transcribing first — the user's words (and then the
    // clip search) come once the backend's transcription/status messages
    // arrive. Show "Transcribing…" until then, not "Finding a clip…".
    setStatusText('Transcribing…')
    setIsThinking(true)
    wsRef.current.send(JSON.stringify({ type: 'audio', audio: base64Audio }))
  }, [])

  const {
    micMuted, setMicMuted, isListening, hearingSpeech, micLevel, permissionDenied,
    micUnavailable,
  } = useContinuousVoiceInput(
    connected, isThinking || isClipPlaying || clipGrace, sendAudioSegment
  )

  useEffect(() => {
    if (!micUnavailable) return
    // 'no-input-device' is deliberately NOT silent: previously this state fell
    // back to whatever device the browser offered, which could be a loopback
    // recording system output. Saying so plainly is the whole point.
    toast.error(
      micUnavailable === 'no-input-device'
        ? 'No microphone found — connect one to talk, or type your question instead'
        : 'Microphone access denied — you can still type your questions',
      { duration: 6000 }
    )
  }, [micUnavailable])

  useEffect(
    () => () => {
      if (clipGraceTimerRef.current) clearTimeout(clipGraceTimerRef.current)
    },
    []
  )

  return {
    // conversation view-model
    messages,
    inputText,
    setInputText,
    isThinking,
    statusText,
    connected,
    // clip playback gating (the layout wires its <video> events to these)
    isClipPlaying,
    setIsClipPlaying,
    clipGrace,
    // actions
    sendText,
    acceptFollowUp,
    chooseClarification,
    retryQuestion,
    declineFollowUp,
    // mic
    micMuted,
    setMicMuted,
    isListening,
    hearingSpeech,
    micLevel,
    permissionDenied,
    micUnavailable,
  }
}
