'use client'

import { useCallback, useEffect, useRef, useState } from 'react'
import { pickPreferredAudioDevice } from '@/lib/audioDevices'

// Continuous (hands-free) voice turn-taking tuning. These are heuristics,
// not adaptive — a noisy room or a hot mic can misfire the thresholds below,
// and there's an inherent latency/naturalness tradeoff in SILENCE_DURATION_MS
// (shorter cuts users off mid-thought during a normal speaking pause; longer
// feels laggy). Half-duplex only: the mic is paused while the avatar is
// speaking (not true barge-in) — even with noiseSuppression/autoGainControl
// on, there's no reason to have the mic live while there's nothing valid to
// capture, and it sidesteps any chance of the avatar's own audio being
// picked up and self-triggering.
//
// End-of-turn tuning. Raised modestly from the original 750/300 (which fired
// on a mid-sentence breath and let a cough count as a turn) — but NOT as high
// as 1300/700, which overshot: in a real mic environment with a narrow
// speech-vs-ambient contrast, a full sentence's CUMULATIVE above-threshold
// time didn't reach 700ms and/or a genuine end-of-sentence pause didn't reach
// 1300ms of continuous silence, so legitimate speech stopped registering at
// all. These conservative values stay close enough to the known-working
// baseline that normal speech comfortably clears them, while still requiring
// a longer pause than before:
// - SILENCE_DURATION_MS = 1000: end-of-sentence pauses are ~1s+, so this ends
//   the turn on a real stop, not a mid-sentence breath (~500-900ms), but is
//   low enough to still fire reliably.
// - MIN_SPEECH_MS = 400: filters the briefest blips (a cough is ~200-400ms)
//   without risking a normal sentence (which yields well over 400ms of
//   above-threshold time). CUMULATIVE above-threshold, not wall-clock (see
//   the onstop handler).
// If speech still under-registers, it's the calibrated THRESHOLD being too
// high for the room — check the "[voice] calibrated" / "segment ended
// spokeMs=" console logs, not these two numbers.
const SILENCE_DURATION_MS = 1000
const MIN_SPEECH_MS = 400
// The "hearing you" indicator flipping off on the very first frame below
// threshold flickers constantly — natural speech has brief micro-pauses
// between syllables/words that dip below the level even mid-sentence. This
// only smooths the VISUAL state; SILENCE_DURATION_MS still governs the
// actual end-of-turn decision.
const HEARING_INDICATOR_GRACE_MS = 250
// Fallback only — the real threshold is calibrated per-session from a brief
// ambient-noise sample, since a fixed number doesn't hold up across
// mics/rooms/gain settings.
const FALLBACK_SPEECH_LEVEL_THRESHOLD = 10
const AMBIENT_CALIBRATION_MS = 500
const AMBIENT_MARGIN = 8
// Human speech energy is concentrated below ~4kHz; averaging the FULL FFT
// spectrum (up to ~24kHz with a typical 48kHz sample rate) dilutes the
// signal with ~85% of bins that are near-zero regardless of speech, badly
// compressing the contrast between "silence" and "talking" (confirmed live:
// ambient floor 9, speech only 17-22 — barely distinguishable — when
// averaging all 128 bins of a 256-point FFT).
const VOICE_BAND_HZ = 4000

function computeVoiceLevel(data: Uint8Array, voiceBinCount: number): number {
  const n = Math.min(voiceBinCount, data.length)
  if (n <= 0) return 0
  let sum = 0
  for (let i = 0; i < n; i++) sum += data[i]
  return sum / n
}

export interface ContinuousVoiceInput {
  micMuted: boolean
  setMicMuted: React.Dispatch<React.SetStateAction<boolean>>
  isListening: boolean
  hearingSpeech: boolean
  micLevel: number
  permissionDenied: boolean
}

/**
 * Half-duplex, hands-free voice turn-taking: acquires the mic once, detects
 * end-of-speech via a calibrated silence threshold, calls `onSegment` with
 * base64-encoded webm audio, then automatically resumes listening for the
 * next turn once `avatarBusy` clears. `connected` gates the whole loop (no
 * point recording while the WS is down); `avatarBusy` gates recording so the
 * avatar's own playback can't be picked up by the mic and self-trigger.
 */
export function useContinuousVoiceInput(
  connected: boolean,
  avatarBusy: boolean,
  onSegment: (base64Audio: string) => void
): ContinuousVoiceInput {
  const [micMuted, setMicMuted] = useState(false)
  const [isListening, setIsListening] = useState(false)
  const [hearingSpeech, setHearingSpeech] = useState(false)
  const [micLevel, setMicLevel] = useState(0)
  const [permissionDenied, setPermissionDenied] = useState(false)

  const continuousStreamRef = useRef<MediaStream | null>(null)
  const audioCtxRef = useRef<AudioContext | null>(null)
  const analyserRef = useRef<AnalyserNode | null>(null)
  const vadRafRef = useRef<number | null>(null)
  const mediaRecorderRef = useRef<MediaRecorder | null>(null)
  const segmentChunksRef = useRef<Blob[]>([])
  const speechDetectedAtRef = useRef<number | null>(null)
  const silenceStartRef = useRef<number | null>(null)
  const speechMsAccumulatedRef = useRef(0)
  const lastVadTickAtRef = useRef<number | null>(null)
  const speechThresholdRef = useRef(FALLBACK_SPEECH_LEVEL_THRESHOLD)
  const voiceBinCountRef = useRef(0)
  const avatarBusyRef = useRef(false)
  // Set the instant a segment is sent, cleared once `avatarBusy` actually
  // catches up (or after a safety timeout). Closes a race: `beginSegment`
  // runs synchronously right after onSegmentRef fires, but the caller's
  // `avatarBusy` prop (derived from its own isProcessing/showVideo state)
  // can only update on its NEXT render — so avatarBusyRef.current would
  // still read stale/false for one tick, letting a second segment start
  // and barge-in-cancel the first turn before it ever produces a reply.
  const pendingSendRef = useRef(false)
  const micMutedRef = useRef(false)
  const connectedRef = useRef(false)
  const loopActiveRef = useRef(false)
  const resumeOnGestureRef = useRef<(() => void) | null>(null)
  const onSegmentRef = useRef(onSegment)

  useEffect(() => {
    onSegmentRef.current = onSegment
  }, [onSegment])
  useEffect(() => {
    avatarBusyRef.current = avatarBusy
    // The real busy signal caught up with our provisional guess — safe to
    // drop it now (see pendingSendRef's declaration for why it exists).
    if (avatarBusy) pendingSendRef.current = false
  }, [avatarBusy])
  useEffect(() => {
    micMutedRef.current = micMuted
  }, [micMuted])

  // Starts the self-perpetuating record → detect-silence → send → wait for
  // avatar → record-next-segment cycle. Idempotent (loopActiveRef) so it's
  // safe to call from both the mic-acquisition effect and the connect effect
  // without ever running two concurrent MediaRecorders on the same stream.
  const startListeningLoop = useCallback(() => {
    if (loopActiveRef.current) return
    loopActiveRef.current = true

    const runVadTick = () => {
      const analyser = analyserRef.current
      if (!analyser || mediaRecorderRef.current?.state !== 'recording') return
      const data = new Uint8Array(analyser.frequencyBinCount)
      analyser.getByteFrequencyData(data)
      const level = computeVoiceLevel(data, voiceBinCountRef.current)
      setMicLevel(level)

      const now = Date.now()
      const dt = lastVadTickAtRef.current === null ? 0 : now - lastVadTickAtRef.current
      lastVadTickAtRef.current = now

      if (level > speechThresholdRef.current) {
        speechMsAccumulatedRef.current += dt
        if (speechDetectedAtRef.current === null) {
          console.info('[voice] speech detected, level=', level, 'threshold=', speechThresholdRef.current)
          speechDetectedAtRef.current = now
        }
        silenceStartRef.current = null
        setHearingSpeech(true)
      } else if (speechDetectedAtRef.current !== null) {
        if (silenceStartRef.current === null) {
          silenceStartRef.current = now
        } else if (now - silenceStartRef.current >= SILENCE_DURATION_MS) {
          mediaRecorderRef.current?.stop() // triggers onstop below, which restarts the loop
          return
        }
        if (silenceStartRef.current !== null && now - silenceStartRef.current >= HEARING_INDICATOR_GRACE_MS) {
          setHearingSpeech(false)
        }
      }
      vadRafRef.current = requestAnimationFrame(runVadTick)
    }

    const beginSegment = () => {
      if (!connectedRef.current || !continuousStreamRef.current) {
        console.info(
          '[voice] loop stopped: connected=',
          connectedRef.current,
          'hasStream=',
          !!continuousStreamRef.current
        )
        loopActiveRef.current = false
        return
      }
      if (micMutedRef.current || avatarBusyRef.current || pendingSendRef.current) {
        setTimeout(beginSegment, 150)
        return
      }
      segmentChunksRef.current = []
      speechDetectedAtRef.current = null
      silenceStartRef.current = null
      speechMsAccumulatedRef.current = 0
      lastVadTickAtRef.current = null
      setHearingSpeech(false)

      const recorder = new MediaRecorder(continuousStreamRef.current)
      console.info('[voice] segment started, threshold=', speechThresholdRef.current)
      recorder.ondataavailable = (e) => segmentChunksRef.current.push(e.data)
      recorder.onstop = async () => {
        setIsListening(false)
        setMicLevel(0)
        // Real accumulated time above threshold, not just elapsed wall-clock
        // time since the first crossing — a single brief noise blip (mic
        // bump, breath) followed by the 750ms silence window would otherwise
        // ALWAYS satisfy "elapsed >= 300ms" trivially, sending near-empty
        // audio that fails STT with "Could not transcribe audio".
        const spokeLongEnough = speechMsAccumulatedRef.current >= MIN_SPEECH_MS
        console.info(
          '[voice] segment ended, spokeMs=',
          speechMsAccumulatedRef.current,
          'chunks=',
          segmentChunksRef.current.length,
          'sending=',
          spokeLongEnough
        )
        if (spokeLongEnough) {
          const blob = new Blob(segmentChunksRef.current, { type: 'audio/webm' })
          const buffer = await blob.arrayBuffer()
          const b64 = btoa(new Uint8Array(buffer).reduce((s, b) => s + String.fromCharCode(b), ''))
          console.info('[voice] calling onSegment, blob bytes=', buffer.byteLength)
          pendingSendRef.current = true
          setTimeout(() => {
            pendingSendRef.current = false
          }, 2000) // safety backstop in case avatarBusy never arrives, for any reason
          onSegmentRef.current(b64)
        }
        beginSegment() // wait for the avatar (if now busy) then listen for the next turn
      }
      mediaRecorderRef.current = recorder
      recorder.start()
      setIsListening(true)
      vadRafRef.current = requestAnimationFrame(runVadTick)
    }

    beginSegment()
  }, [])

  useEffect(() => {
    connectedRef.current = connected
    if (connected) {
      startListeningLoop()
    } else if (mediaRecorderRef.current?.state === 'recording') {
      mediaRecorderRef.current.stop()
    }
  }, [connected, startListeningLoop])

  // Acquire the mic once, for the whole conversation — reusing the same
  // loopback-safe device-selection fix already applied in VideoRecorder.tsx
  // and VoicePanel.tsx (unconstrained getUserMedia({audio:true}) can resolve
  // to a "Stereo Mix" monitor device instead of the real mic on some systems).
  useEffect(() => {
    let cancelled = false
    ;(async () => {
      try {
        const probe = await navigator.mediaDevices.getUserMedia({ audio: true })
        probe.getTracks().forEach((t) => t.stop())
        const devices = await navigator.mediaDevices.enumerateDevices()
        const preferred = pickPreferredAudioDevice(devices)
        // Unlike VideoRecorder.tsx/VoicePanel.tsx (single monologue / voice-
        // clone sample capture, where raw unprocessed fidelity is preferable
        // and there's no simultaneous avatar playback to cancel against),
        // this IS a live back-and-forth conversation where noiseSuppression
        // and autoGainControl directly help STT — a quiet mic's speech can
        // otherwise sit only ~8 points above its own noise floor on a 0-255
        // scale, barely distinguishable from ambient noise (confirmed live:
        // ambient floor ~36, speech ~44-46 with these disabled). echoCancellation
        // stays off since half-duplex already avoids the avatar's own audio
        // reaching the mic (paused during playback), so there's nothing to
        // cancel, and cancellation can subtly color the voice unnecessarily.
        const stream = await navigator.mediaDevices.getUserMedia({
          audio: preferred
            ? {
                deviceId: { exact: preferred.deviceId },
                echoCancellation: false,
                noiseSuppression: true,
                autoGainControl: true,
              }
            : { echoCancellation: false, noiseSuppression: true, autoGainControl: true },
        })
        if (cancelled) {
          stream.getTracks().forEach((t) => t.stop())
          return
        }
        console.info(
          '[voice] mic acquired:',
          stream.getAudioTracks().map((t) => t.label)
        )
        continuousStreamRef.current = stream
        const audioCtx = new AudioContext()
        const analyser = audioCtx.createAnalyser()
        analyser.fftSize = 256
        audioCtx.createMediaStreamSource(stream).connect(analyser)
        audioCtxRef.current = audioCtx
        analyserRef.current = analyser

        const binHz = audioCtx.sampleRate / analyser.fftSize
        voiceBinCountRef.current = Math.max(
          1,
          Math.min(analyser.frequencyBinCount, Math.round(VOICE_BAND_HZ / binHz))
        )
        console.info(
          '[voice] sampleRate=',
          audioCtx.sampleRate,
          'voiceBinCount=',
          voiceBinCountRef.current,
          'of',
          analyser.frequencyBinCount
        )

        // Browsers' autoplay policy can create this AudioContext already
        // "suspended" since nothing here is a direct user gesture (it's
        // acquired automatically on mount) — while suspended, the analyser
        // silently reports all-zero levels forever, so the VAD never
        // detects speech and a segment just records forever without ever
        // auto-sending. resume() is a no-op if already running; the
        // document-level listener below is the fallback for browsers that
        // won't actually let resume() take effect until a real user gesture
        // happens anywhere on the page.
        audioCtx.resume().catch(() => {})
        if (audioCtx.state === 'suspended') {
          const resumeOnGesture = () => {
            audioCtx.resume().catch(() => {})
            document.removeEventListener('pointerdown', resumeOnGesture)
            document.removeEventListener('keydown', resumeOnGesture)
          }
          resumeOnGestureRef.current = resumeOnGesture
          document.addEventListener('pointerdown', resumeOnGesture)
          document.addEventListener('keydown', resumeOnGesture)
        }

        // Calibrate the speech-detection threshold against this mic/room's
        // actual noise floor instead of trusting one fixed number — a "hot"
        // mic or noisy room can otherwise sit permanently above a fixed
        // threshold (never triggers silence → never auto-sends) or a very
        // quiet setup can make real speech barely register above it.
        const ambientFloor = await new Promise<number>((resolve) => {
          const samples: number[] = []
          const data = new Uint8Array(analyser.frequencyBinCount)
          const start = performance.now()
          const sample = () => {
            analyser.getByteFrequencyData(data)
            samples.push(computeVoiceLevel(data, voiceBinCountRef.current))
            if (performance.now() - start < AMBIENT_CALIBRATION_MS) {
              requestAnimationFrame(sample)
            } else {
              resolve(samples.reduce((a, b) => a + b, 0) / samples.length)
            }
          }
          sample()
        })
        if (!cancelled) {
          speechThresholdRef.current = Math.max(
            ambientFloor + AMBIENT_MARGIN,
            FALLBACK_SPEECH_LEVEL_THRESHOLD
          )
        }
        console.info(
          '[voice] calibrated: ambientFloor=',
          ambientFloor,
          'threshold=',
          speechThresholdRef.current,
          'audioCtxState=',
          audioCtx.state,
          'connected=',
          connectedRef.current
        )

        if (connectedRef.current) startListeningLoop()
      } catch (err) {
        console.error('[voice] mic acquisition failed:', err)
        if (!cancelled) setPermissionDenied(true)
      }
    })()

    return () => {
      cancelled = true
      if (vadRafRef.current) cancelAnimationFrame(vadRafRef.current)
      mediaRecorderRef.current?.stop()
      continuousStreamRef.current?.getTracks().forEach((t) => t.stop())
      audioCtxRef.current?.close().catch(() => {})
      if (resumeOnGestureRef.current) {
        document.removeEventListener('pointerdown', resumeOnGestureRef.current)
        document.removeEventListener('keydown', resumeOnGestureRef.current)
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  return { micMuted, setMicMuted, isListening, hearingSpeech, micLevel, permissionDenied }
}
