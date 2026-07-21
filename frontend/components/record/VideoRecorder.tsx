'use client'

import { useCallback, useEffect, useRef, useState } from 'react'
import {
  Video, Pause, Play, Square, RotateCcw, UploadCloud, CheckCircle2,
  Loader2, AlertTriangle, Circle,
} from 'lucide-react'
import { toast } from 'react-hot-toast'
import { api, uploadSegmentBlob } from '@/lib/api'
import type { ApiError, RawSegment } from '@/lib/types'

type Phase =
  | 'reviewing_existing'
  | 'acquiring'
  | 'ready'
  | 'recording'
  | 'paused'
  | 'reviewing_new'
  | 'uploading'
  | 'done'
  | 'camera_error'

interface VideoRecorderProps {
  sessionId: string
  questionIndex: number
  questionText: string
  existingSegment?: RawSegment
  onAccepted: () => void
}

const fmtTime = (s: number) =>
  `${Math.floor(s / 60).toString().padStart(2, '0')}:${(s % 60).toString().padStart(2, '0')}`

// vp8,opus first, NOT vp9 — Chromium has a long-documented bug where
// recording with vp9,opus produces a silently EMPTY audio track on many
// Windows GPU/driver combos (isTypeSupported reports true, video records
// perfectly, audio just isn't there): https://crbug.com/1197086 and widely
// reported elsewhere. vp8,opus is the original, most battle-tested
// MediaRecorder audio+video combo and doesn't have this issue. Falls back to
// whatever the browser (e.g. Safari, which lacks webm support) can record.
function pickMimeType(): string {
  const candidates = [
    'video/webm;codecs=vp8,opus',
    'video/webm;codecs=vp9,opus',
    'video/webm',
    'video/mp4',
  ]
  return candidates.find(c => typeof MediaRecorder !== 'undefined' && MediaRecorder.isTypeSupported?.(c)) || ''
}

export function VideoRecorder({
  sessionId,
  questionIndex,
  questionText,
  existingSegment,
  onAccepted,
}: VideoRecorderProps) {
  const [phase, setPhase] = useState<Phase>(existingSegment ? 'reviewing_existing' : 'acquiring')
  const [elapsed, setElapsed] = useState(0)
  const [uploadFraction, setUploadFraction] = useState(0)
  const [cameraErrorMsg, setCameraErrorMsg] = useState<string | null>(null)
  const [recordedUrl, setRecordedUrl] = useState<string | null>(null)

  const videoRef = useRef<HTMLVideoElement | null>(null)
  const reviewVideoRef = useRef<HTMLVideoElement | null>(null)
  const streamRef = useRef<MediaStream | null>(null)
  const recorderRef = useRef<MediaRecorder | null>(null)
  const chunksRef = useRef<Blob[]>([])
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const recordedBlobRef = useRef<Blob | null>(null)
  const recordedUrlRef = useRef<string | null>(null)
  const mimeTypeRef = useRef<string>('video/webm')
  const audioContextRef = useRef<AudioContext | null>(null)
  const audioLevelIntervalRef = useRef<ReturnType<typeof setInterval> | null>(null)

  const stopTimer = () => {
    if (timerRef.current) {
      clearInterval(timerRef.current)
      timerRef.current = null
    }
  }

  const teardownStream = useCallback(() => {
    streamRef.current?.getTracks().forEach(t => t.stop())
    streamRef.current = null
  }, [])

  const acquireCamera = useCallback(async (isCancelled?: () => boolean) => {
    setPhase('acquiring')
    setCameraErrorMsg(null)
    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        video: { facingMode: 'user', width: { ideal: 1280 }, height: { ideal: 720 } },
        // `audio: true` opts into Chrome's default voice-call DSP chain —
        // echo cancellation, noise suppression, auto gain control — all
        // tuned for a live call with simultaneous speaker playback to
        // cancel against. There's no such playback here (a single-person
        // monologue answer), and on some mic/driver combinations this
        // processing chain can over-suppress the signal down to near-
        // silence while the raw hardware signal is completely fine (which
        // is why Windows' own Camera app, which doesn't apply this chain,
        // captures audible sound from the exact same microphone). None of
        // it is needed for this use case, so request the raw signal.
        audio: {
          echoCancellation: false,
          noiseSuppression: false,
          autoGainControl: false,
        },
      })
      // Diagnostic only: which physical/virtual device did Chrome actually
      // pick for audio (audio: true doesn't specify one), and what other
      // input devices exist to pick from? A mismatch here — e.g. Chrome
      // defaulting to a disconnected virtual mic (NVIDIA Broadcast, Discord
      // Krisp, "Stereo Mix", etc.) while Windows' own default/Camera app
      // uses the real physical mic — would explain near-zero signal
      // reaching the browser despite the track reporting live/unmuted.
      const audioTrack = stream.getAudioTracks()[0]
      if (audioTrack) {
        console.info(
          `[VideoRecorder] selected audio device: label="${audioTrack.label}" settings=${JSON.stringify(audioTrack.getSettings())}`
        )
      }
      try {
        const devices = await navigator.mediaDevices.enumerateDevices()
        const inputs = devices.filter(d => d.kind === 'audioinput')
        console.info(
          `[VideoRecorder] available audio input devices: ${JSON.stringify(inputs.map(d => ({ label: d.label, deviceId: d.deviceId })))}`
        )
      } catch (e) {
        console.warn('[VideoRecorder] could not enumerate audio devices:', e)
      }

      if (isCancelled?.()) {
        // The effect that requested this was torn down before getUserMedia
        // resolved (React StrictMode's dev-mode mount→cleanup→remount, or a
        // fast question navigation) — stop THIS call's own tracks and bail
        // without touching streamRef/phase, which may already belong to a
        // newer, still-live acquisition. Racing a teardown against a
        // still-open acquisition on the SAME device is what leaves the
        // microphone track silent while the camera recovers fine (mic
        // drivers release far slower than camera hardware).
        stream.getTracks().forEach(t => t.stop())
        return
      }
      streamRef.current = stream
      setPhase('ready')
    } catch (err) {
      if (isCancelled?.()) return
      const msg =
        err instanceof DOMException && err.name === 'NotAllowedError'
          ? 'Camera/microphone access was denied. Please allow access and try again.'
          : 'Could not access your camera or microphone.'
      setCameraErrorMsg(msg)
      setPhase('camera_error')
    }
  }, [])

  // Keep the live-preview <video> element's srcObject in sync with the
  // acquired stream. Deliberately NOT done inline in acquireCamera/
  // discardAndReRecord: right after calling setPhase(...) to a value that
  // first renders the <video> element, videoRef.current is still null in
  // that same synchronous continuation (React hasn't committed the new
  // render yet) — that left the preview permanently black. Effects run
  // after commit, so this is guaranteed to see the element once it exists.
  useEffect(() => {
    if (phase !== 'ready' && phase !== 'recording' && phase !== 'paused') return
    const video = videoRef.current
    const stream = streamRef.current
    if (!video || !stream || video.srcObject === stream) return
    video.srcObject = stream
    video.play().catch(() => {})
  }, [phase])

  // Reset per-question state whenever the question changes (navigating
  // back/forward through the sequence).
  useEffect(() => {
    let cancelled = false
    setElapsed(0)
    setUploadFraction(0)
    if (recordedUrlRef.current) {
      URL.revokeObjectURL(recordedUrlRef.current)
      recordedUrlRef.current = null
    }
    setRecordedUrl(null)
    recordedBlobRef.current = null
    teardownStream()

    if (existingSegment) {
      setPhase('reviewing_existing')
    } else {
      acquireCamera(() => cancelled)
    }

    return () => {
      cancelled = true
      stopTimer()
      stopAudioLevelMonitor()
      teardownStream()
      if (recordedUrlRef.current) URL.revokeObjectURL(recordedUrlRef.current)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [questionIndex])

  // Diagnostic only: taps the stream with a Web Audio AnalyserNode (a
  // passive listener — doesn't consume the track or interfere with
  // MediaRecorder also reading it) and logs the actual peak signal level
  // seen each second. This is the one thing none of the previous checks
  // could show — whether real, non-zero audio signal is reaching the
  // browser's audio pipeline at all during capture, independent of
  // encoding/muxing/playback entirely.
  const startAudioLevelMonitor = (stream: MediaStream) => {
    if (stream.getAudioTracks().length === 0) return
    try {
      const AudioContextCtor =
        window.AudioContext || (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext
      const audioContext = new AudioContextCtor()
      const source = audioContext.createMediaStreamSource(stream)
      const analyser = audioContext.createAnalyser()
      analyser.fftSize = 2048
      source.connect(analyser)
      const data = new Uint8Array(analyser.frequencyBinCount)
      audioContextRef.current = audioContext

      audioLevelIntervalRef.current = setInterval(() => {
        analyser.getByteTimeDomainData(data)
        let peak = 0
        for (let i = 0; i < data.length; i++) {
          const deviation = Math.abs(data[i] - 128)
          if (deviation > peak) peak = deviation
        }
        console.info(`[VideoRecorder] live audio level: peak deviation ${peak}/128 (0 = total silence)`)
      }, 1000)
    } catch (e) {
      console.warn('[VideoRecorder] could not start audio level monitor:', e)
    }
  }

  const stopAudioLevelMonitor = () => {
    if (audioLevelIntervalRef.current) {
      clearInterval(audioLevelIntervalRef.current)
      audioLevelIntervalRef.current = null
    }
    audioContextRef.current?.close().catch(() => {})
    audioContextRef.current = null
  }

  const startRecording = useCallback(() => {
    if (!streamRef.current) return
    chunksRef.current = []
    const mimeType = pickMimeType()
    mimeTypeRef.current = mimeType || 'video/webm'

    // Diagnostic only (kept deliberately — cheap, and the next place to
    // look if audio is ever missing again): confirms whether the STREAM
    // itself has a live, unmuted audio track at the moment recording
    // starts, vs. the problem being in encoding/muxing/playback instead.
    const audioTracks = streamRef.current.getAudioTracks()
    if (audioTracks.length === 0) {
      console.warn('[VideoRecorder] starting recording with NO audio track in the stream')
    } else {
      audioTracks.forEach(t =>
        console.info(
          `[VideoRecorder] audio track before recording: enabled=${t.enabled} muted=${t.muted} readyState=${t.readyState}`
        )
      )
    }
    console.info(`[VideoRecorder] MediaRecorder mimeType: ${mimeTypeRef.current}`)
    startAudioLevelMonitor(streamRef.current)

    const recorder = new MediaRecorder(
      streamRef.current,
      mimeType ? { mimeType } : undefined,
    )
    recorder.ondataavailable = (e) => {
      if (e.data.size > 0) chunksRef.current.push(e.data)
    }
    recorder.onstop = () => {
      stopAudioLevelMonitor()
      const blob = new Blob(chunksRef.current, { type: mimeTypeRef.current })
      console.info(`[VideoRecorder] recorded blob: size=${blob.size} type=${blob.type}`)
      recordedBlobRef.current = blob
      const url = URL.createObjectURL(blob)
      recordedUrlRef.current = url
      setRecordedUrl(url)
      setPhase('reviewing_new')
    }
    recorder.start()
    recorderRef.current = recorder
    setElapsed(0)
    setPhase('recording')
    timerRef.current = setInterval(() => setElapsed(t => t + 1), 1000)
  }, [])

  const pauseRecording = () => {
    recorderRef.current?.pause()
    stopTimer()
    setPhase('paused')
  }

  const resumeRecording = () => {
    recorderRef.current?.resume()
    timerRef.current = setInterval(() => setElapsed(t => t + 1), 1000)
    setPhase('recording')
  }

  const stopRecording = () => {
    stopTimer()
    recorderRef.current?.stop()
  }

  const discardAndReRecord = () => {
    if (recordedUrlRef.current) {
      URL.revokeObjectURL(recordedUrlRef.current)
      recordedUrlRef.current = null
    }
    setRecordedUrl(null)
    recordedBlobRef.current = null
    setElapsed(0)
    setPhase('ready')
    // Live preview stream is still open — the srcObject-sync effect above
    // reattaches it once the <video> element re-renders for 'ready'.
  }

  const acceptAndUpload = async () => {
    const blob = recordedBlobRef.current
    if (!blob) return
    setPhase('uploading')
    setUploadFraction(0)
    try {
      const presign = await api.presignSegmentUpload(questionIndex, mimeTypeRef.current)
      await uploadSegmentBlob(presign.upload_url, blob, presign.content_type, setUploadFraction)
      await api.ingestSegment({
        interview_session_id: sessionId,
        question_index: questionIndex,
        question_asked: questionText,
        video_key: presign.video_key,
      })
      setPhase('done')
      toast.success('Answer saved')
      onAccepted()
    } catch (err: unknown) {
      const detail = (err as ApiError)?.response?.data?.detail || (err as ApiError)?.message
      toast.error(detail || 'Upload failed — please try again')
      setPhase('reviewing_new')
    }
  }

  const startFreshTake = () => {
    // Coming from "reviewing_existing" — user wants to replace the answer.
    acquireCamera()
  }

  return (
    <div className="calm-recorder rounded-2xl border border-calm-border dark:border-calm-borderDark bg-calm-card dark:bg-calm-cardDark overflow-hidden">
      {/* ── Reviewing an already-recorded answer ── */}
      {phase === 'reviewing_existing' && existingSegment && (
        <div className="flex flex-col gap-4 p-6">
          <div className="flex items-center gap-2 text-calm-sage-700 dark:text-calm-sage-300 text-sm font-medium">
            <CheckCircle2 size={16} />
            You already answered this question
          </div>
          {existingSegment.video_url && (
            <video
              controls
              src={existingSegment.video_url}
              className="w-full rounded-xl bg-black aspect-video"
            />
          )}
          <button onClick={startFreshTake} className="calm-btn-secondary self-start">
            <RotateCcw size={16} />
            Re-record this answer
          </button>
        </div>
      )}

      {/* ── Acquiring camera ── */}
      {phase === 'acquiring' && (
        <div className="flex flex-col items-center justify-center gap-3 py-24 text-calm-inkmuted dark:text-calm-inkmutedDark">
          <Loader2 size={28} className="animate-spin" />
          <p className="text-sm">Requesting camera access…</p>
        </div>
      )}

      {/* ── Camera error ── */}
      {phase === 'camera_error' && (
        <div className="flex flex-col items-center justify-center gap-4 py-20 px-6 text-center">
          <AlertTriangle size={28} className="text-amber-600" />
          <p className="text-sm text-calm-ink dark:text-calm-inkDark max-w-sm">{cameraErrorMsg}</p>
          <button onClick={() => acquireCamera()} className="calm-btn-primary">
            Try again
          </button>
        </div>
      )}

      {/* ── Live preview / recording / paused ── */}
      {(phase === 'ready' || phase === 'recording' || phase === 'paused') && (
        <div className="flex flex-col">
          <div className="relative bg-black aspect-video">
            <video ref={videoRef} muted playsInline className="w-full h-full object-cover" />
            {(phase === 'recording' || phase === 'paused') && (
              <div className="absolute top-4 left-4 flex items-center gap-2 px-3 py-1.5 rounded-full bg-black/50 backdrop-blur-sm">
                <Circle
                  size={9}
                  className={phase === 'recording' ? 'text-red-500 fill-red-500 animate-pulse' : 'text-amber-400 fill-amber-400'}
                />
                <span className="text-white text-sm font-mono">{fmtTime(elapsed)}</span>
              </div>
            )}
          </div>

          <div className="flex items-center justify-center gap-3 p-5">
            {phase === 'ready' && (
              <button
                onClick={startRecording}
                className="calm-btn-primary px-6 py-3 text-base"
                aria-label="Start recording"
              >
                <Video size={18} />
                Start Recording
              </button>
            )}
            {phase === 'recording' && (
              <>
                <button onClick={pauseRecording} className="calm-btn-secondary" aria-label="Pause">
                  <Pause size={16} />
                  Pause
                </button>
                <button onClick={stopRecording} className="calm-btn-primary" aria-label="Stop">
                  <Square size={16} />
                  Stop
                </button>
              </>
            )}
            {phase === 'paused' && (
              <>
                <button onClick={resumeRecording} className="calm-btn-secondary" aria-label="Resume">
                  <Play size={16} />
                  Resume
                </button>
                <button onClick={stopRecording} className="calm-btn-primary" aria-label="Stop">
                  <Square size={16} />
                  Stop
                </button>
              </>
            )}
          </div>
        </div>
      )}

      {/* ── Review the just-recorded take ── */}
      {(phase === 'reviewing_new' || phase === 'uploading' || phase === 'done') && recordedUrl && (
        <div className="flex flex-col">
          <video
            ref={reviewVideoRef}
            controls
            muted={false}
            src={recordedUrl}
            // Chrome persists the last-set volume/mute state across <video>
            // elements on the same page — the live-preview element just
            // above is explicitly muted, and that "0 volume" carries over
            // to this element by default even with no muted attribute of
            // its own. onLoadedMetadata fires right as the browser would
            // apply that inherited default, so re-asserting here overrides
            // it rather than racing it via a separate effect.
            onLoadedMetadata={(e) => {
              e.currentTarget.muted = false
              e.currentTarget.volume = 1
            }}
            className="w-full bg-black aspect-video"
          />
          <div className="flex flex-col gap-3 p-5">
            {phase === 'reviewing_new' && (
              <div className="flex items-center justify-center gap-3">
                <button onClick={discardAndReRecord} className="calm-btn-secondary">
                  <RotateCcw size={16} />
                  Re-record
                </button>
                <button onClick={acceptAndUpload} className="calm-btn-primary">
                  <UploadCloud size={16} />
                  Accept &amp; Continue
                </button>
              </div>
            )}
            {phase === 'uploading' && (
              <div className="flex flex-col items-center gap-2">
                <div className="w-full max-w-xs h-2 rounded-full bg-calm-sage-100 dark:bg-calm-border overflow-hidden">
                  <div
                    className="h-full rounded-full bg-calm-sage-600 transition-all duration-150"
                    style={{ width: `${Math.round(uploadFraction * 100)}%` }}
                  />
                </div>
                <p className="text-xs text-calm-inkmuted dark:text-calm-inkmutedDark">
                  Uploading… {Math.round(uploadFraction * 100)}%
                </p>
              </div>
            )}
            {phase === 'done' && (
              <div className="flex items-center justify-center gap-2 text-calm-sage-700 dark:text-calm-sage-300 text-sm font-medium">
                <CheckCircle2 size={16} />
                Saved
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  )
}
