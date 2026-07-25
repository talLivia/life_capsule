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
export function useVideoClipChat(avatarId: string) {
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
      case 'video_clip_response':
        setIsThinking(false)
        setMessages((prev) => [
          ...prev,
          { id: `clip-${Date.now()}`, role: 'assistant', content: '', videoUrl: msg.video_url },
        ])
        // Hold the mic gated briefly on arrival even if autoplay is blocked
        // (see clipGrace) — autoplay engaging isClipPlaying takes over for the
        // clip's real duration when it works.
        setClipGrace(true)
        if (clipGraceTimerRef.current) clearTimeout(clipGraceTimerRef.current)
        clipGraceTimerRef.current = setTimeout(() => setClipGrace(false), CLIP_GRACE_MS)
        break
      case 'video_clip_no_story':
        setIsThinking(false)
        setMessages((prev) => [
          ...prev,
          { id: `no-story-${Date.now()}`, role: 'assistant', content: msg.message, noStory: true },
        ])
        break
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
        .createSession(avatarId)
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
  }, [avatarId])

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

  const sendAudioSegment = useCallback((base64Audio: string) => {
    if (wsRef.current?.readyState !== WebSocket.OPEN) return
    // Spoken input: we're transcribing first — the user's words (and then the
    // clip search) come once the backend's transcription/status messages
    // arrive. Show "Transcribing…" until then, not "Finding a clip…".
    setStatusText('Transcribing…')
    setIsThinking(true)
    wsRef.current.send(JSON.stringify({ type: 'audio', audio: base64Audio }))
  }, [])

  const { micMuted, setMicMuted, isListening, hearingSpeech, micLevel, permissionDenied } =
    useContinuousVoiceInput(connected, isThinking || isClipPlaying || clipGrace, sendAudioSegment)

  useEffect(() => {
    if (permissionDenied) {
      toast.error('Microphone access denied — you can still type your questions')
    }
  }, [permissionDenied])

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
    // mic
    micMuted,
    setMicMuted,
    isListening,
    hearingSpeech,
    micLevel,
    permissionDenied,
  }
}
