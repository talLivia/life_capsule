'use client'

import { useCallback, useEffect, useRef, useState } from 'react'
import type { MicVAD } from '@ricky0123/vad-web'
import { pickPreferredAudioDevice } from '@/lib/audioDevices'

// Continuous (hands-free) voice turn-taking.
//
// SPEECH DETECTION IS A NEURAL CLASSIFIER (Silero VAD, via @ricky0123/vad-web),
// not a loudness threshold. Three successive amplitude-based designs failed
// here — a fixed calibrated threshold, then a wider margin, then a rolling
// noise floor with a sustained-onset requirement — and the reason is
// fundamental rather than a matter of tuning: this codebase's own measurements
// record ambient ~36 vs speech ~44-46 on a 0-255 scale. Eight to ten points of
// separation is not enough for ANY level comparison to be reliable, and steady
// mechanical noise (a fan, AC) sits above the line indefinitely, so neither a
// margin nor a "must hold for N ms" rule can reject it. Silero classifies
// speech vs non-speech from spectral/temporal structure, which is the thing
// level was only ever a poor proxy for.
//
// SILERO IS ALSO THE TRANSPORT now, not just the detector: onSpeechEnd hands
// us the utterance's own audio (16 kHz mono, with pre-speech padding), which
// we encode as WAV and send. This replaced the MediaRecorder segment loop
// (2026-08-17) because that loop had two structural ways to LOSE speech that
// a flowing conversation produces constantly:
//   1. The one-shot swallow: onSpeechStart fires once per utterance; if it
//      fired while the avatar was still audible (the user answering on the
//      question's tail), the busy guard dropped it, it never re-fired, and
//      the whole utterance was discarded as "no speech".
//   2. Clipped starts: each send/recycle restarted the recorder, losing the
//      first ~150-300 ms of the next utterance — truncated Hebrew onsets
//      transcribe garbled or empty.
// With Silero delivering the audio there is no recorder to restart and no
// segment to mark: every classified utterance arrives whole, padded, and is
// judged ONCE, at its end, by the guards in onSpeechEnd.
//
// Half-duplex is still the model: speech that lives entirely inside the
// avatar's turn is dropped (audio captured while the storyteller is talking
// is never the user's question — see the tail-overlap rule below for the one
// deliberate exception). The mic is acquired through the loopback-safe
// device check and refused outright if there's no real input, and micMuted
// still gates everything.
const MODEL_ASSET_PATH = '/vad/'  // self-hosted; never a CDN fetch (offline dev)
// Silero speech probability thresholds. The library's own defaults, kept
// rather than invented: 0.5 to enter speech, 0.35 to leave it (hysteresis, so
// a brief dip mid-word doesn't end the turn).
const POSITIVE_SPEECH_THRESHOLD = 0.5
const NEGATIVE_SPEECH_THRESHOLD = 0.35
// How long Silero must see non-speech before declaring the turn over: long
// enough not to cut the speaker off at a natural mid-sentence pause, short
// enough not to feel laggy.
const REDEMPTION_MS = 1000
// Anything shorter than this isn't an utterance — Silero fires
// `onVADMisfire` instead of `onSpeechEnd`, so a cough or a door closing never
// becomes a turn. This is the length of a stretch the MODEL classified as
// speech, not cumulative time above a loudness line, so noise can't
// accumulate its way past it. Known limit: a bare clipped "כן" sits near
// this floor — longer natural forms clear it comfortably.
const MIN_SPEECH_MS = 320
// Audio Silero prepends from BEFORE the detected onset, so the word's first
// phoneme is in the payload even though detection necessarily lags it.
const PRE_SPEECH_PAD_MS = 500
// The one exception to half-duplex: an utterance that STARTED while the
// avatar was still audible is accepted if it began at most this long before
// playback ended — that's a listener answering on the question's tail, the
// natural rhythm of conversation. Anything that started earlier is the
// avatar's own voice reaching the mic (a whole answer heard through
// speakers starts near playback start, far outside this window) and is
// dropped, which is what keeps the documented clip-replayed-as-a-question
// incident impossible.
const OVERLAP_GRACE_MS = 1200
// Hard ceiling on one utterance. Silero ends turns reliably, but nothing may
// buffer unbounded: past this, the VAD is force-reset and the speech
// discarded (previously a recorder once captured 48 seconds).
const MAX_SPEECH_MS = 20000
// vad-web delivers utterance audio at Silero's native rate.
const VAD_SAMPLE_RATE = 16000
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

/** Float32 samples → base64 of a 16-bit PCM mono WAV. WAV because it is
 *  self-describing: Deepgram sniffs the container from the bytes
 *  (Content-Type is octet-stream) and the Whisper fallback goes through
 *  ffmpeg, which does the same — the backend needed no change. */
function encodeWavBase64(samples: Float32Array, sampleRate: number): string {
  const view = new DataView(new ArrayBuffer(44 + samples.length * 2))
  const writeStr = (off: number, s: string) => {
    for (let i = 0; i < s.length; i++) view.setUint8(off + i, s.charCodeAt(i))
  }
  writeStr(0, 'RIFF')
  view.setUint32(4, 36 + samples.length * 2, true)
  writeStr(8, 'WAVE')
  writeStr(12, 'fmt ')
  view.setUint32(16, 16, true)          // fmt chunk size
  view.setUint16(20, 1, true)           // PCM
  view.setUint16(22, 1, true)           // mono
  view.setUint32(24, sampleRate, true)
  view.setUint32(28, sampleRate * 2, true)  // byte rate
  view.setUint16(32, 2, true)           // block align
  view.setUint16(34, 16, true)          // bits per sample
  writeStr(36, 'data')
  view.setUint32(40, samples.length * 2, true)
  let off = 44
  for (let i = 0; i < samples.length; i++, off += 2) {
    const s = Math.max(-1, Math.min(1, samples[i]))
    view.setInt16(off, s < 0 ? s * 0x8000 : s * 0x7fff, true)
  }
  const bytes = new Uint8Array(view.buffer)
  let bin = ''
  for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i])
  return btoa(bin)
}

export interface ContinuousVoiceInput {
  micMuted: boolean
  setMicMuted: React.Dispatch<React.SetStateAction<boolean>>
  isListening: boolean
  hearingSpeech: boolean
  micLevel: number
  permissionDenied: boolean
  /** Why the mic is unusable, when it is. 'denied' = the user/OS refused
   *  permission; 'no-input-device' = there is no REAL microphone to use (see
   *  the refusal in the acquisition effect — we never fall back to whatever
   *  device the browser would pick, because that can be a loopback that
   *  records system output). null when the mic is fine. */
  micUnavailable: 'denied' | 'no-input-device' | null
}

/**
 * Hands-free voice turn-taking: acquires the mic once, lets Silero VAD
 * detect AND deliver each utterance (with pre-speech padding), encodes it
 * as WAV and calls `onSegment` with base64 audio. `connected` gates sending
 * (no point while the WS is down); `avatarBusy` gates which utterances
 * count — see the half-duplex rules in the header comment.
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
  const [micUnavailable, setMicUnavailable] =
    useState<'denied' | 'no-input-device' | null>(null)

  const continuousStreamRef = useRef<MediaStream | null>(null)
  const audioCtxRef = useRef<AudioContext | null>(null)
  const analyserRef = useRef<AnalyserNode | null>(null)
  const meterRafRef = useRef<number | null>(null)
  const meterFrameRef = useRef(0)
  const lastMeterValueRef = useRef(-1)
  const voiceBinCountRef = useRef(0)
  const vadRef = useRef<MicVAD | null>(null)
  const vadReadyRef = useRef(false)
  const avatarBusyRef = useRef(false)
  // When the avatar last STOPPED being busy — the reference point for the
  // tail-overlap acceptance rule in onSpeechEnd.
  const busyClearedAtRef = useRef(0)
  // Set by onSpeechStart; null once the utterance is resolved (sent,
  // dropped, misfired, or ceilinged).
  const speechStartedAtRef = useRef<number | null>(null)
  const speechStartedDuringBusyRef = useRef(false)
  const maxSpeechTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const micMutedRef = useRef(false)
  const connectedRef = useRef(false)
  const resumeOnGestureRef = useRef<(() => void) | null>(null)
  const onSegmentRef = useRef(onSegment)

  // "Listening" = the VAD is up and an utterance spoken right now would be
  // eligible to send. Derived from refs so every gate keeps it honest.
  const refreshListening = useCallback(() => {
    setIsListening(
      vadReadyRef.current &&
        connectedRef.current &&
        !micMutedRef.current &&
        !avatarBusyRef.current
    )
  }, [])

  useEffect(() => {
    onSegmentRef.current = onSegment
  }, [onSegment])
  useEffect(() => {
    const wasBusy = avatarBusyRef.current
    avatarBusyRef.current = avatarBusy
    if (wasBusy && !avatarBusy) busyClearedAtRef.current = Date.now()
    refreshListening()
  }, [avatarBusy, refreshListening])
  useEffect(() => {
    micMutedRef.current = micMuted
    if (micMuted) setHearingSpeech(false)
    refreshListening()
  }, [micMuted, refreshListening])
  useEffect(() => {
    connectedRef.current = connected
    refreshListening()
  }, [connected, refreshListening])

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

        // REFUSE rather than fall back to an unconstrained getUserMedia.
        // CONFIRMED LIVE: with the physical mic disconnected there is no real
        // input to pick, and the old code dropped the deviceId constraint and
        // let the browser choose — which selected a LOOPBACK device (Stereo
        // Mix) that records system OUTPUT. The result was ~30s of silence
        // followed by a verbatim recording of the clip that was playing, sent
        // back as the user's next question. That is exactly the failure
        // audioDevices.ts was written to prevent; the guard filtered loopback
        // devices out of the candidate list but then fell through to
        // "whatever the browser offers" when the list came back empty.
        // No real microphone is the same usable state as denied permission.
        if (!preferred) {
          console.warn(
            '[voice] no real (non-loopback) audio input device — refusing to ' +
              'open a stream rather than risk capturing system output'
          )
          if (!cancelled) setMicUnavailable('no-input-device')
          return
        }
        // Unlike VideoRecorder.tsx/VoicePanel.tsx (single monologue / voice-
        // clone sample capture, where raw unprocessed fidelity is preferable
        // and there's no simultaneous avatar playback to cancel against),
        // this IS a live back-and-forth conversation where noiseSuppression
        // directly helps STT — a quiet mic's speech can otherwise sit only
        // ~8 points above its own noise floor on a 0-255 scale, barely
        // distinguishable from ambient noise (confirmed live: ambient floor
        // ~36, speech ~44-46 with these disabled).
        // echoCancellation stays OFF. It was briefly switched on when clips
        // were turning up as user questions, on the theory that the speakers
        // were bleeding into the mic — but the trace disproved that: the
        // capture was a DIGITAL loopback device, not an acoustic path, which
        // cancellation cannot touch. Turning it on bought nothing and can
        // colour the signal. The real fixes are the device refusal above,
        // the utterance ceiling, and the half-duplex drop rules.
        // autoGainControl is OFF too: it continuously rewrites the very
        // levels any detector reasons about (raising gain during quiet, which
        // is what lifted ambient over the old threshold), and Silero does not
        // need it. Both off means the model sees the mic as it actually is.
        // deviceId is always constrained now — see the refusal above.
        const stream = await navigator.mediaDevices.getUserMedia({
          audio: {
            deviceId: { exact: preferred.deviceId },
            echoCancellation: false,
            noiseSuppression: true,
            autoGainControl: false,
          },
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

        // UI level meter only — no control logic reads this. Throttled to
        // every 6th frame and deduplicated so the meter doesn't re-render
        // the whole chat surface at 60fps.
        const runMeter = () => {
          const a = analyserRef.current
          if (a && meterFrameRef.current++ % 6 === 0) {
            const data = new Uint8Array(a.frequencyBinCount)
            a.getByteFrequencyData(data)
            const level = Math.round(
              computeVoiceLevel(data, voiceBinCountRef.current)
            )
            if (level !== lastMeterValueRef.current) {
              lastMeterValueRef.current = level
              setMicLevel(level)
            }
          }
          meterRafRef.current = requestAnimationFrame(runMeter)
        }
        meterRafRef.current = requestAnimationFrame(runMeter)

        // Browsers' autoplay policy can create this AudioContext already
        // "suspended" since nothing here is a direct user gesture (it's
        // acquired automatically on mount) — while suspended, the analyser
        // silently reports all-zero levels forever and the VAD hears
        // nothing. resume() is a no-op if already running; the
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

        // ── Silero VAD ────────────────────────────────────────────────────
        // Detector AND transport — see the header comment. Loaded
        // dynamically so the ~15 MB of model + ONNX runtime is fetched only
        // on pages that actually listen, and never during SSR (it touches
        // AudioWorklet/WASM).
        const initStartedAt = performance.now()
        const { MicVAD } = await import('@ricky0123/vad-web')
        const vad = await MicVAD.new({
          audioContext: audioCtx,
          // Reuse the stream we already vetted — MicVAD must NOT acquire its
          // own, or it would bypass the loopback-safe device selection above.
          getStream: async () => stream,
          pauseStream: async () => {},
          resumeStream: async () => stream,
          startOnLoad: true,
          model: 'v5',
          // Self-hosted (see /public/vad) — no third-party fetch on page load
          // and it works with no network in dev.
          baseAssetPath: MODEL_ASSET_PATH,
          onnxWASMBasePath: MODEL_ASSET_PATH,
          ortConfig: (ort) => {
            // Single-threaded: multi-threaded ORT needs SharedArrayBuffer,
            // which needs COOP/COEP headers we don't set. Also pins the wasm
            // location so it can't reach for a CDN copy.
            ort.env.wasm.numThreads = 1
            ort.env.wasm.wasmPaths = MODEL_ASSET_PATH
          },
          positiveSpeechThreshold: POSITIVE_SPEECH_THRESHOLD,
          negativeSpeechThreshold: NEGATIVE_SPEECH_THRESHOLD,
          redemptionMs: REDEMPTION_MS,
          minSpeechMs: MIN_SPEECH_MS,
          preSpeechPadMs: PRE_SPEECH_PAD_MS,
          onSpeechStart: () => {
            if (micMutedRef.current) return  // muted speech never becomes a turn
            speechStartedAtRef.current = Date.now()
            speechStartedDuringBusyRef.current = avatarBusyRef.current
            if (!avatarBusyRef.current) setHearingSpeech(true)
            console.info(
              '[voice] SPEECH START (silero) duringBusy=',
              avatarBusyRef.current
            )
            // Utterance ceiling: force-reset the VAD if speech never ends.
            if (maxSpeechTimerRef.current) clearTimeout(maxSpeechTimerRef.current)
            maxSpeechTimerRef.current = setTimeout(() => {
              if (speechStartedAtRef.current !== null) {
                console.warn(
                  '[voice] utterance exceeded ceiling — VAD reset, audio dropped'
                )
                speechStartedAtRef.current = null
                setHearingSpeech(false)
                vadRef.current?.pause()
                vadRef.current?.start()
              }
            }, MAX_SPEECH_MS)
          },
          onVADMisfire: () => {
            // Too short to be a real utterance — a cough, a door closing.
            console.info('[voice] speech misfire (too short) — not a turn')
            if (maxSpeechTimerRef.current) clearTimeout(maxSpeechTimerRef.current)
            speechStartedAtRef.current = null
            setHearingSpeech(false)
          },
          onSpeechEnd: (audio: Float32Array) => {
            if (maxSpeechTimerRef.current) clearTimeout(maxSpeechTimerRef.current)
            setHearingSpeech(false)
            const startedAt = speechStartedAtRef.current
            const startedDuringBusy = speechStartedDuringBusyRef.current
            speechStartedAtRef.current = null
            if (startedAt === null) return  // muted at onset, or force-reset
            if (micMutedRef.current || !connectedRef.current) {
              console.info('[voice] utterance dropped: muted/disconnected')
              return
            }
            if (avatarBusyRef.current) {
              // Ended while the avatar is still talking/processing — either
              // the avatar's own audio or premature; half-duplex drops it.
              console.info('[voice] utterance dropped: avatar busy at end')
              return
            }
            if (startedDuringBusy) {
              const beforeClear = busyClearedAtRef.current - startedAt
              if (beforeClear > OVERLAP_GRACE_MS) {
                console.info(
                  '[voice] utterance dropped: started', beforeClear,
                  'ms before playback ended — treating as the avatar\'s own audio'
                )
                return
              }
              console.info(
                '[voice] tail-overlap utterance accepted, overlapMs=', beforeClear
              )
            }
            const b64 = encodeWavBase64(audio, VAD_SAMPLE_RATE)
            console.info(
              '[voice] sending utterance:', audio.length, 'samples (',
              Math.round((audio.length / VAD_SAMPLE_RATE) * 1000), 'ms )'
            )
            onSegmentRef.current(b64)
          },
        })
        if (cancelled) {
          await vad.destroy().catch(() => {})
          stream.getTracks().forEach((t) => t.stop())
          return
        }
        vadRef.current = vad
        vadReadyRef.current = true
        refreshListening()
        console.info(
          '[voice] Silero VAD ready in', Math.round(performance.now() - initStartedAt),
          'ms; audioCtxState=', audioCtx.state, 'connected=', connectedRef.current
        )
      } catch (err) {
        console.error('[voice] mic acquisition failed:', err)
        if (!cancelled) {
          setPermissionDenied(true)
          setMicUnavailable('denied')
        }
      }
    })()

    return () => {
      cancelled = true
      if (meterRafRef.current) cancelAnimationFrame(meterRafRef.current)
      if (maxSpeechTimerRef.current) clearTimeout(maxSpeechTimerRef.current)
      vadReadyRef.current = false
      vadRef.current?.destroy().catch(() => {})
      vadRef.current = null
      continuousStreamRef.current?.getTracks().forEach((t) => t.stop())
      audioCtxRef.current?.close().catch(() => {})
      if (resumeOnGestureRef.current) {
        document.removeEventListener('pointerdown', resumeOnGestureRef.current)
        document.removeEventListener('keydown', resumeOnGestureRef.current)
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  return {
    micMuted,
    setMicMuted,
    isListening,
    hearingSpeech,
    micLevel,
    permissionDenied,
    micUnavailable,
  }
}
