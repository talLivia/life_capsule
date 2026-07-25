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
// Silero decides ONLY when speech starts and stops. Everything else is
// unchanged and deliberately kept as a safety net around it: the mic is still
// acquired through the loopback-safe device check (and refused outright if
// there's no real input), MediaRecorder is still the transport so the backend
// keeps receiving the same base64 webm, playback still aborts an in-flight
// segment, micMuted still gates, and the max-duration ceiling still applies.
//
// Half-duplex: the mic is gated while the storyteller's clip plays, rather
// than true barge-in.
const MODEL_ASSET_PATH = '/vad/'  // self-hosted; never a CDN fetch (offline dev)
// Silero speech probability thresholds. The library's own defaults, kept
// rather than invented: 0.5 to enter speech, 0.35 to leave it (hysteresis, so
// a brief dip mid-word doesn't end the turn).
const POSITIVE_SPEECH_THRESHOLD = 0.5
const NEGATIVE_SPEECH_THRESHOLD = 0.35
// How long Silero must see non-speech before declaring the turn over. This is
// the direct equivalent of the old SILENCE_DURATION_MS and serves the same
// purpose: long enough not to cut the speaker off at a natural mid-sentence
// pause, short enough not to feel laggy.
const REDEMPTION_MS = 1000
// Anything shorter than this isn't an utterance — Silero fires
// `onVADMisfire` instead of `onSpeechEnd`, so a cough or a door closing never
// becomes a turn. Replaces the old MIN_SPEECH_MS, but note the difference
// that matters: this is the length of a stretch the MODEL classified as
// speech, not cumulative time above a loudness line, so noise can't
// accumulate its way past it.
const MIN_SPEECH_MS = 320
// Hard ceiling on one segment. Silero ends turns reliably, but a recorder must
// never be able to run unbounded: previously one captured 48 seconds. Past
// this, a segment Silero never marked as speech is DISCARDED; one it did is
// sent as a normal end-of-turn so a long question still works.
const MAX_SEGMENT_MS = 20000
// A segment that has heard no speech is recycled this often, so the audio we
// eventually send carries at most this much leading silence (which would
// otherwise slow STT down for no benefit). Restarting while nothing is being
// said cannot clip speech.
const SILENT_SEGMENT_RECYCLE_MS = 5000
// The "hearing you" indicator flipping off the instant probability dips
// flickers during natural micro-pauses. Visual smoothing only — REDEMPTION_MS
// governs the actual end-of-turn decision.
const HEARING_INDICATOR_GRACE_MS = 250
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
  /** Why the mic is unusable, when it is. 'denied' = the user/OS refused
   *  permission; 'no-input-device' = there is no REAL microphone to use (see
   *  the refusal in the acquisition effect — we never fall back to whatever
   *  device the browser would pick, because that can be a loopback that
   *  records system output). null when the mic is fine. */
  micUnavailable: 'denied' | 'no-input-device' | null
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
  const [micUnavailable, setMicUnavailable] =
    useState<'denied' | 'no-input-device' | null>(null)

  const continuousStreamRef = useRef<MediaStream | null>(null)
  const audioCtxRef = useRef<AudioContext | null>(null)
  const analyserRef = useRef<AnalyserNode | null>(null)
  const vadRafRef = useRef<number | null>(null)
  const mediaRecorderRef = useRef<MediaRecorder | null>(null)
  const segmentChunksRef = useRef<Blob[]>([])
  // Set by Silero's onSpeechStart, cleared on misfire/segment start. Its only
  // job now is to record whether THIS segment contained real speech, which
  // decides send-vs-discard.
  const speechDetectedAtRef = useRef<number | null>(null)
  const voiceBinCountRef = useRef(0)
  const vadRef = useRef<MicVAD | null>(null)
  const avatarBusyRef = useRef(false)
  // Set the instant a segment is sent, cleared once `avatarBusy` actually
  // catches up (or after a safety timeout). Closes a race: `beginSegment`
  // runs synchronously right after onSegmentRef fires, but the caller's
  // `avatarBusy` prop (derived from its own isProcessing/showVideo state)
  // can only update on its NEXT render — so avatarBusyRef.current would
  // still read stale/false for one tick, letting a second segment start
  // and barge-in-cancel the first turn before it ever produces a reply.
  const pendingSendRef = useRef(false)
  // Set when a segment must be thrown away rather than sent — specifically
  // when playback starts WHILE we're already recording. See runVadTick.
  const discardSegmentRef = useRef(false)
  const segmentStartedAtRef = useRef(0)
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

      // CONFIRMED LIVE: `avatarBusy` used to be checked only when a segment
      // STARTED, never while one was running. So a recording already in
      // flight when a clip began playing kept going and captured the clip's
      // own audio out of the speakers — which STT then transcribed and sent
      // back as the user's next "question". Real example from the DB: a
      // 19-second answer was replayed verbatim as a user turn, and the
      // system dutifully answered it, producing what looked like a
      // retrieval bug. (The window opens whenever playback starts late —
      // e.g. autoplay blocked until the user clicks play — because by then
      // the mic has legitimately reopened.)
      // Aborting and DISCARDING is the only correct move: audio captured
      // while the storyteller is talking is never the user's question.
      if (avatarBusyRef.current) {
        discardSegmentRef.current = true
        mediaRecorderRef.current?.stop()  // -> onstop, which drops it
        return
      }

      // Runaway-segment ceiling — see MAX_SEGMENT_MS.
      if (Date.now() - segmentStartedAtRef.current >= MAX_SEGMENT_MS) {
        if (speechDetectedAtRef.current === null) {
          discardSegmentRef.current = true
          console.info('[voice] segment DISCARDED — hit max duration with no speech')
        }
        mediaRecorderRef.current?.stop()
        return
      }

      // Recycle a segment that has heard nothing, so whatever we eventually
      // send carries at most SILENT_SEGMENT_RECYCLE_MS of leading silence
      // (long leading silence only slows STT down). Safe to restart here
      // precisely BECAUSE nothing is being said.
      if (
        speechDetectedAtRef.current === null &&
        Date.now() - segmentStartedAtRef.current >= SILENT_SEGMENT_RECYCLE_MS
      ) {
        discardSegmentRef.current = true
        mediaRecorderRef.current?.stop()
        return
      }

      // Level is now used ONLY to drive the UI meter. It no longer decides
      // anything — Silero does (see the onSpeechStart/onSpeechEnd callbacks).
      const data = new Uint8Array(analyser.frequencyBinCount)
      analyser.getByteFrequencyData(data)
      setMicLevel(computeVoiceLevel(data, voiceBinCountRef.current))

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
      segmentStartedAtRef.current = Date.now()
      speechDetectedAtRef.current = null
      setHearingSpeech(false)

      const recorder = new MediaRecorder(continuousStreamRef.current)
      recorder.ondataavailable = (e) => segmentChunksRef.current.push(e.data)
      recorder.onstop = async () => {
        setIsListening(false)
        setMicLevel(0)
        // Playback started mid-recording — this audio is the storyteller's
        // own clip, not a question. Drop it and wait for playback to finish.
        if (discardSegmentRef.current) {
          discardSegmentRef.current = false
          console.info('[voice] segment DISCARDED — playback started while recording')
          beginSegment()
          return
        }
        // Silero is the sole authority on whether this segment contained
        // speech: it set speechDetectedAt via onSpeechStart, and cleared it
        // again on a misfire (too short to be a real utterance).
        const hadSpeech = speechDetectedAtRef.current !== null
        console.info(
          '[voice] segment ended, hadSpeech=', hadSpeech,
          'chunks=', segmentChunksRef.current.length,
          'sending=', hadSpeech
        )
        if (hadSpeech) {
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
        // and autoGainControl directly help STT — a quiet mic's speech can
        // otherwise sit only ~8 points above its own noise floor on a 0-255
        // scale, barely distinguishable from ambient noise (confirmed live:
        // ambient floor ~36, speech ~44-46 with these disabled).
        // echoCancellation stays OFF. It was briefly switched on when clips
        // were turning up as user questions, on the theory that the speakers
        // were bleeding into the mic — but the trace disproved that: the
        // capture was a DIGITAL loopback device, not an acoustic path, which
        // cancellation cannot touch. Turning it on bought nothing and can
        // colour the signal. The real fixes are the device refusal above, the
        // max-duration cap, and the mid-recording playback abort.
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

        // ── Silero VAD ────────────────────────────────────────────────────
        // Replaces the old ambient calibration entirely — there is no
        // threshold to calibrate any more. Loaded dynamically so the ~15 MB
        // of model + ONNX runtime is fetched only on pages that actually
        // listen, and never during SSR (it touches AudioWorklet/WASM).
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
          onSpeechStart: () => {
            // Guards still apply: never treat playback or a muted mic as a turn.
            if (micMutedRef.current || avatarBusyRef.current) return
            if (speechDetectedAtRef.current === null) {
              console.info('[voice] SPEECH START (silero)')
            }
            speechDetectedAtRef.current = Date.now()
            setHearingSpeech(true)
          },
          onVADMisfire: () => {
            // Too short to be a real utterance — unset so the segment is
            // discarded rather than sent (this is what a cough now does).
            console.info('[voice] speech misfire (too short) — not a turn')
            speechDetectedAtRef.current = null
            setHearingSpeech(false)
          },
          onSpeechEnd: () => {
            setHearingSpeech(false)
            if (micMutedRef.current || avatarBusyRef.current) return
            if (speechDetectedAtRef.current === null) return
            console.info('[voice] SPEECH END (silero) — closing segment')
            // Stop the recorder; its onstop sends, because speechDetectedAt
            // is set. The audio itself comes from MediaRecorder, not Silero,
            // so the backend contract (base64 webm) is unchanged.
            if (mediaRecorderRef.current?.state === 'recording') {
              mediaRecorderRef.current.stop()
            }
          },
        })
        if (cancelled) {
          await vad.destroy().catch(() => {})
          stream.getTracks().forEach((t) => t.stop())
          return
        }
        vadRef.current = vad
        console.info(
          '[voice] Silero VAD ready in', Math.round(performance.now() - initStartedAt),
          'ms; audioCtxState=', audioCtx.state, 'connected=', connectedRef.current
        )

        if (connectedRef.current) startListeningLoop()
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
      if (vadRafRef.current) cancelAnimationFrame(vadRafRef.current)
      vadRef.current?.destroy().catch(() => {})
      vadRef.current = null
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
