'use client'

import { useEffect, useRef, useState } from 'react'
import { Loader2, Volume2, Square } from 'lucide-react'
import { api } from '@/lib/api'

/**
 * Hear the question read out, for a storyteller who would rather listen than
 * read a screen.
 *
 * Two rules, and both are enforced by what exists rather than by ordering:
 *
 *  - Recording cannot START while this is playing. The parent holds the record
 *    button disabled for exactly as long as `onPlayingChange` says true, so
 *    the synthetic voice can never land in the video or in the transcript
 *    entity extraction reads (Part One §2.3).
 *  - This control is not RENDERED while the camera is capturing, so it cannot
 *    be pressed mid-answer. Not "disabled and hopefully not clicked" — absent.
 *
 * Optional in the strongest sense (§12): if synthesis is unavailable the
 * button reports it and disappears for that question. Nothing about recording
 * depends on it working.
 */
export function ReadAloudButton({
  questionId,
  onPlayingChange,
}: {
  questionId: string
  onPlayingChange: (playing: boolean) => void
}) {
  const [state, setState] = useState<'idle' | 'loading' | 'playing' | 'unavailable'>('idle')
  const audioRef = useRef<HTMLAudioElement | null>(null)
  const urlRef = useRef<string | null>(null)

  const stop = () => {
    audioRef.current?.pause()
    audioRef.current = null
    if (urlRef.current) {
      URL.revokeObjectURL(urlRef.current)
      urlRef.current = null
    }
    setState('idle')
    onPlayingChange(false)
  }

  // Moving to another question must silence the previous one — otherwise the
  // voice carries on over a question it is not reading, and the record button
  // stays held by a sound nobody can see the source of.
  useEffect(() => {
    return () => {
      audioRef.current?.pause()
      audioRef.current = null
      if (urlRef.current) {
        URL.revokeObjectURL(urlRef.current)
        urlRef.current = null
      }
      onPlayingChange(false)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [questionId])

  useEffect(() => {
    setState('idle')
  }, [questionId])

  const play = async () => {
    setState('loading')
    try {
      const blob = await api.getQuestionAudio(questionId)
      const url = URL.createObjectURL(blob)
      urlRef.current = url
      const audio = new Audio(url)
      audioRef.current = audio
      audio.onended = stop
      audio.onerror = stop
      await audio.play()
      setState('playing')
      onPlayingChange(true)
    } catch {
      // No toast. A read-aloud that cannot speak is a missing convenience, not
      // an error the producer has to acknowledge before carrying on.
      setState('unavailable')
      onPlayingChange(false)
    }
  }

  if (state === 'unavailable') {
    return (
      <span className="text-[11px] text-muted2">Read aloud isn&apos;t available here</span>
    )
  }

  return (
    <button
      type="button"
      onClick={state === 'playing' ? stop : play}
      disabled={state === 'loading'}
      className="inline-flex items-center gap-1.5 text-xs text-muted hover:text-ink
        transition-colors disabled:opacity-50"
    >
      {state === 'loading' ? (
        <Loader2 size={14} className="animate-spin" />
      ) : state === 'playing' ? (
        <Square size={13} />
      ) : (
        <Volume2 size={14} />
      )}
      {state === 'playing' ? 'Stop' : 'Read aloud'}
    </button>
  )
}
