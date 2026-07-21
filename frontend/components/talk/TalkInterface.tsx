'use client'

import { useCallback, useEffect, useRef, useState } from 'react'
import { Send, Mic, MicOff, Loader2, Sparkles } from 'lucide-react'
import { toast } from 'react-hot-toast'
import { api, buildSessionWsUrl } from '@/lib/api'
import type { ApiError, WsMessage } from '@/lib/types'

interface TalkMessage {
  id: string
  role: 'user' | 'assistant'
  content: string
}

interface VideoChunk {
  url: string
}

interface TalkInterfaceProps {
  avatarId: string
  avatarImageUrl?: string | null
  producerName: string
}

const MAX_RECONNECT_ATTEMPTS = 5

export function TalkInterface({ avatarId, avatarImageUrl, producerName }: TalkInterfaceProps) {
  const [messages, setMessages] = useState<TalkMessage[]>([])
  const [inputText, setInputText] = useState('')
  const [isThinking, setIsThinking] = useState(false)
  const [isRecording, setIsRecording] = useState(false)
  const [showVideo, setShowVideo] = useState(false)
  const [connected, setConnected] = useState(false)

  const sessionIdRef = useRef<string | null>(null)
  const wsRef = useRef<WebSocket | null>(null)
  const reconnectAttemptsRef = useRef(0)
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const videoRef = useRef<HTMLVideoElement>(null)
  const chunkQueueRef = useRef<VideoChunk[]>([])
  const isPlayingRef = useRef(false)
  const messagesEndRef = useRef<HTMLDivElement>(null)
  const mediaRecorderRef = useRef<MediaRecorder | null>(null)
  const streamingIdRef = useRef<string | null>(null)
  const wsInstanceCounterRef = useRef(0) // diagnostic only — labels each socket instance created

  const scrollToBottom = useCallback(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [])
  useEffect(scrollToBottom, [messages, scrollToBottom])

  const playNextChunk = useCallback(() => {
    const next = chunkQueueRef.current.shift()
    if (!next) {
      isPlayingRef.current = false
      setShowVideo(false)
      return
    }
    isPlayingRef.current = true
    setShowVideo(true)
    if (videoRef.current) {
      videoRef.current.src = next.url
      videoRef.current.play().catch(() => {
        if (chunkQueueRef.current.length > 0) playNextChunk()
        else {
          isPlayingRef.current = false
          setShowVideo(false)
        }
      })
    }
  }, [])

  useEffect(() => {
    const video = videoRef.current
    if (!video) return
    const onEnded = () => playNextChunk()
    const onError = () => {
      if (chunkQueueRef.current.length > 0) playNextChunk()
      else {
        isPlayingRef.current = false
        setShowVideo(false)
      }
    }
    video.addEventListener('ended', onEnded)
    video.addEventListener('error', onError)
    return () => {
      video.removeEventListener('ended', onEnded)
      video.removeEventListener('error', onError)
    }
  }, [playNextChunk])

  const handleWsMessage = useCallback((msg: WsMessage) => {
    switch (msg.type) {
      case 'token': {
        // The full reply is already known (retrieval, not live generation)
        // — show it as soon as it arrives rather than waiting for `message`.
        const id = `stream-${Date.now()}`
        streamingIdRef.current = id
        setIsThinking(false)
        setMessages((prev) => [...prev, { id, role: 'assistant', content: msg.token }])
        break
      }
      case 'message': {
        // Replace the streaming placeholder (if any) with the final,
        // persisted-shape message rather than appending a duplicate.
        //
        // idToReplace/finalId are captured HERE, outside the updater, and
        // streamingIdRef is cleared HERE too — not inside the setMessages
        // callback. React 18 Strict Mode double-invokes the updater
        // function in dev mode specifically to catch impure updaters;
        // mutating streamingIdRef.current INSIDE it meant the two
        // invocations saw DIFFERENT ref values (the second saw the first
        // invocation's mutation already applied), so one invocation
        // replaced the placeholder while the other, impure-affected
        // invocation found no placeholder to replace and appended a brand
        // new message instead — two bubbles with identical text, every
        // single turn, confirmed via logging both invocations' inputs.
        const idToReplace = streamingIdRef.current
        const finalId = `final-${Date.now()}`
        streamingIdRef.current = null
        setMessages((prev) => {
          if (idToReplace) {
            const replaced = prev.map((m) =>
              m.id === idToReplace ? { ...m, id: finalId, content: msg.content } : m
            )
            return replaced
          }
          return [...prev, { id: finalId, role: 'assistant', content: msg.content }]
        })
        break
      }
      case 'video_chunk_start':
        chunkQueueRef.current = []
        break
      case 'video_chunk':
        chunkQueueRef.current.push({ url: msg.video_url })
        if (!isPlayingRef.current) playNextChunk()
        break
      case 'video_chunk_end':
        setIsThinking(false)
        break
      case 'status':
        setIsThinking(true)
        break
      case 'error':
        setIsThinking(false)
        toast.error(msg.message)
        break
      case 'interrupted':
        chunkQueueRef.current = []
        break
      default:
        break
    }
  }, [playNextChunk])

  const connectWs = useCallback(
    (sessionId: string) => {
      const socket = new WebSocket(buildSessionWsUrl(sessionId))
      const socketLabel = `ws#${++wsInstanceCounterRef.current}`
      wsRef.current = socket
      console.info(`[TalkInterface] ${socketLabel} connecting for session ${sessionId}`)

      socket.onopen = () => {
        reconnectAttemptsRef.current = 0
        setConnected(true)
      }
      socket.onmessage = (event) => {
        // Diagnostic only: which socket instance received this frame, and
        // is it still the one currently tracked in wsRef (vs. a stale
        // connection from an earlier StrictMode remount / reconnect that
        // should have been superseded)? If duplicate assistant messages
        // ever trace back to two DIFFERENT socketLabels both delivering
        // the same turn, that confirms two live connections rather than a
        // single-connection rendering bug.
        const isCurrent = socket === wsRef.current
        let type = '?'
        try {
          type = JSON.parse(event.data)?.type ?? '?'
        } catch {
          /* fall through with type '?' */
        }
        console.info(
          `[TalkInterface] ${socketLabel} received type=${type} isCurrentSocket=${isCurrent}`
        )
        try {
          handleWsMessage(JSON.parse(event.data))
        } catch {
          /* ignore malformed frames */
        }
      }
      socket.onclose = () => {
        setConnected(false)
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
    console.info('[TalkInterface] session-creation effect running, cancelled=false at start')

    api
      .createSession(avatarId)
      .then((session) => {
        console.info(
          `[TalkInterface] createSession resolved: session=${session.id} cancelled=${cancelled}`
        )
        if (cancelled) return
        sessionIdRef.current = session.id
        connectWs(session.id)
      })
      .catch(() => toast.error('Could not start a conversation — please try again'))

    return () => {
      console.info(`[TalkInterface] effect cleanup running for session=${sessionIdRef.current}`)
      cancelled = true
      if (reconnectTimerRef.current) clearTimeout(reconnectTimerRef.current)
      wsRef.current?.close()
      if (sessionIdRef.current) {
        api.endSession(sessionIdRef.current).catch(() => {})
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [avatarId])

  const sendText = () => {
    const text = inputText.trim()
    if (!text || !wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) return
    setMessages((prev) => [...prev, { id: `user-${Date.now()}`, role: 'user', content: text }])
    setIsThinking(true)
    wsRef.current.send(JSON.stringify({ type: 'text', text }))
    setInputText('')
  }

  const toggleRecording = async () => {
    if (isRecording) {
      mediaRecorderRef.current?.stop()
      setIsRecording(false)
      return
    }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true })
      const recorder = new MediaRecorder(stream)
      const chunks: Blob[] = []
      recorder.ondataavailable = (e) => chunks.push(e.data)
      recorder.onstop = async () => {
        stream.getTracks().forEach((t) => t.stop())
        const blob = new Blob(chunks, { type: 'audio/webm' })
        const buffer = await blob.arrayBuffer()
        const b64 = btoa(new Uint8Array(buffer).reduce((s, b) => s + String.fromCharCode(b), ''))
        if (wsRef.current?.readyState === WebSocket.OPEN) {
          setIsThinking(true)
          wsRef.current.send(JSON.stringify({ type: 'audio', audio: b64 }))
        }
      }
      recorder.start()
      mediaRecorderRef.current = recorder
      setIsRecording(true)
    } catch {
      toast.error('Microphone access denied')
    }
  }

  return (
    <div className="min-h-screen bg-calm-paper dark:bg-calm-paperDark text-calm-ink dark:text-calm-inkDark flex flex-col">
      <header className="max-w-2xl mx-auto w-full px-6 pt-8 pb-4 flex items-center gap-2">
        <Sparkles size={16} className="text-calm-sage-600 dark:text-calm-sage-300" />
        <p className="text-sm text-calm-inkmuted dark:text-calm-inkmutedDark">
          Talking with {producerName}&apos;s stories
        </p>
        {!connected && (
          <span className="ml-auto text-xs text-calm-inkmuted dark:text-calm-inkmutedDark">
            Reconnecting…
          </span>
        )}
      </header>

      <main className="max-w-2xl mx-auto w-full flex-1 flex flex-col px-6 gap-6">
        {/* Avatar panel */}
        <div className="relative w-full aspect-video rounded-2xl overflow-hidden bg-black/80 border border-calm-border dark:border-calm-borderDark">
          <video ref={videoRef} className="w-full h-full object-cover" playsInline />
          {!showVideo && (
            avatarImageUrl ? (
              // eslint-disable-next-line @next/next/no-img-element
              <img
                src={avatarImageUrl}
                alt={producerName}
                className="absolute inset-0 w-full h-full object-cover"
              />
            ) : (
              <div className="absolute inset-0 flex items-center justify-center text-white/40">
                <Sparkles size={32} />
              </div>
            )
          )}
        </div>

        {/* Conversation history */}
        <div className="flex-1 overflow-y-auto flex flex-col gap-3 pb-4 messages-scroll">
          {messages.map((m) => (
            <div
              key={m.id}
              className={`max-w-[85%] px-4 py-2.5 rounded-2xl text-sm leading-relaxed ${
                m.role === 'user'
                  ? 'self-end bg-calm-sage-600 text-white rounded-br-sm'
                  : 'self-start bg-calm-card dark:bg-calm-cardDark border border-calm-border dark:border-calm-borderDark rounded-bl-sm'
              }`}
            >
              {m.content}
            </div>
          ))}
          {isThinking && (
            <div className="self-start flex items-center gap-1.5 text-calm-inkmuted dark:text-calm-inkmutedDark text-sm px-4 py-2.5">
              <Loader2 size={14} className="animate-spin" />
              Thinking…
            </div>
          )}
          <div ref={messagesEndRef} />
        </div>
      </main>

      {/* Input bar */}
      <div className="max-w-2xl mx-auto w-full px-6 pb-8 pt-2 flex items-center gap-2">
        <button
          onClick={toggleRecording}
          aria-label={isRecording ? 'Stop recording' : 'Ask by voice'}
          className={`w-11 h-11 rounded-full flex items-center justify-center flex-shrink-0 transition-all
            ${isRecording ? 'bg-red-500 text-white' : 'calm-btn-secondary !rounded-full !p-0'}`}
        >
          {isRecording ? <MicOff size={18} /> : <Mic size={18} />}
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
