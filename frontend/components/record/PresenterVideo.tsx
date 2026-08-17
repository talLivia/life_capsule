'use client'

import { useEffect, useRef, useState } from 'react'
import { Play, RotateCcw, SkipForward } from 'lucide-react'

/**
 * The presenter reading the question (docs/PRESENTER_VIDEOS_PLAN.md §3) —
 * occupies the same aspect-video slot the recorder will take over, so the
 * two never coexist: while this is mounted there is no record button, and
 * once recording starts this is unmounted. That sequencing is the same
 * voice-never-in-the-recording guarantee the TTS Read-aloud enforced with
 * two states; here it holds by construction.
 *
 * Autoplay, honestly handled: the click that selected the question is a
 * user gesture, so play() normally succeeds with sound. When the browser
 * refuses anyway (deep link, reload mid-question), a play button appears
 * instead of a silently frozen frame.
 *
 * `onUnavailable` fires when the video cannot be fetched/decoded at all
 * (never uploaded, expired URL, codec) — the caller falls back to the TTS
 * Read-aloud path and goes straight to the recorder. A broken video must
 * never block recording.
 */
export function PresenterVideo({
  src,
  onFinished,
  onUnavailable,
  finishLabel = 'Skip to recording',
}: {
  src: string
  onFinished: () => void
  onUnavailable: () => void
  finishLabel?: string
}) {
  const videoRef = useRef<HTMLVideoElement | null>(null)
  const [needsGesture, setNeedsGesture] = useState(false)

  useEffect(() => {
    const video = videoRef.current
    if (!video) return
    video.play().then(
      () => setNeedsGesture(false),
      () => setNeedsGesture(true)
    )
  }, [src])

  return (
    <div className="flex flex-col gap-3">
      <div className="relative bg-black aspect-video rounded-xl overflow-hidden">
        <video
          ref={videoRef}
          src={src}
          className="absolute inset-0 w-full h-full object-cover"
          playsInline
          onEnded={onFinished}
          onError={onUnavailable}
        />
        {needsGesture && (
          <button
            type="button"
            onClick={() => {
              videoRef.current?.play().then(
                () => setNeedsGesture(false),
                () => onUnavailable()
              )
            }}
            className="absolute inset-0 flex items-center justify-center bg-black/50"
            aria-label="Play the question video"
          >
            <span className="w-16 h-16 rounded-full bg-primary-600 flex items-center justify-center">
              <Play size={26} className="text-white ml-1" />
            </span>
          </button>
        )}
      </div>
      <div className="flex items-center gap-4">
        <button
          type="button"
          onClick={onFinished}
          className="inline-flex items-center gap-1.5 text-xs text-muted hover:text-ink transition-colors"
        >
          <SkipForward size={14} />
          {finishLabel}
        </button>
        <button
          type="button"
          onClick={() => {
            const video = videoRef.current
            if (!video) return
            video.currentTime = 0
            video.play().catch(() => setNeedsGesture(true))
          }}
          className="inline-flex items-center gap-1.5 text-xs text-muted hover:text-ink transition-colors"
        >
          <RotateCcw size={14} />
          Replay
        </button>
      </div>
    </div>
  )
}
