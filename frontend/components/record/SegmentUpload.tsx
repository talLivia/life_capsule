'use client'

import { useRef, useState } from 'react'
import { FolderOpen, Loader2 } from 'lucide-react'
import { toast } from 'react-hot-toast'
import { api, uploadSegmentBlob } from '@/lib/api'
import type { ApiError } from '@/lib/types'

/**
 * Answer a question with a video the producer already has, instead of
 * recording one now — old family footage, something filmed on a phone.
 *
 * It runs the SAME three calls as VideoRecorder does after a take ends:
 * presign → PUT → /segments/ingest. There is deliberately no upload-specific
 * endpoint and no second ingestion path. Once ingest is reached the two are
 * indistinguishable, so an uploaded video gets the same transcription, the
 * same entity matching against the existing graph, and the same human
 * confirmation at the end — which is the whole point. A File IS a Blob, so
 * uploadSegmentBlob needed no change at all.
 */

interface SegmentUploadProps {
  sessionId: string
  questionIndex: number
  /** Stable question id — see api.ingestSegment. */
  questionId: string
  questionText: string
  onAccepted: () => void | Promise<void>
}

// Kept in step with _EXT_BY_CONTENT_TYPE in interview.py — the backend
// rejects anything else, this just fails faster and more kindly.
const ACCEPTED = ['video/webm', 'video/mp4', 'video/quicktime']
const ACCEPT_ATTR = 'video/webm,video/mp4,video/quicktime,.webm,.mp4,.mov'
const MAX_BYTES = 500 * 1024 * 1024

export function SegmentUpload({
  sessionId,
  questionIndex,
  questionId,
  questionText,
  onAccepted,
}: SegmentUploadProps) {
  const inputRef = useRef<HTMLInputElement | null>(null)
  const [busy, setBusy] = useState(false)
  const [fraction, setFraction] = useState(0)

  const handleFile = async (file: File) => {
    // Some browsers report an empty type for .mov picked from disk, so the
    // extension is a fallback rather than the type being trusted outright.
    const typeOk = ACCEPTED.includes(file.type) || /\.(webm|mp4|mov)$/i.test(file.name)
    if (!typeOk) {
      toast.error('Please choose a video file (.webm, .mp4 or .mov)')
      return
    }
    if (file.size > MAX_BYTES) {
      toast.error(`That video is too large (max ${MAX_BYTES / (1024 * 1024)} MB)`)
      return
    }
    if (file.size === 0) {
      toast.error('That file is empty')
      return
    }

    const contentType = ACCEPTED.includes(file.type)
      ? file.type
      : file.name.toLowerCase().endsWith('.mov')
        ? 'video/quicktime'
        : file.name.toLowerCase().endsWith('.mp4')
          ? 'video/mp4'
          : 'video/webm'

    setBusy(true)
    setFraction(0)
    try {
      const presign = await api.presignSegmentUpload(questionIndex, contentType)
      await uploadSegmentBlob(presign.upload_url, file, presign.content_type, setFraction)
      await api.ingestSegment({
        interview_session_id: sessionId,
        question_index: questionIndex,
        question_id: questionId,
        question_asked: questionText,
        video_key: presign.video_key,
      })
      toast.success('Video added to your story')
      await onAccepted()
    } catch (err: unknown) {
      const detail = (err as ApiError)?.response?.data?.detail || (err as ApiError)?.message
      toast.error(detail || 'Could not upload that video')
    } finally {
      setBusy(false)
      // Clear the input, or picking the SAME file again fires no change
      // event and the second attempt silently does nothing.
      if (inputRef.current) inputRef.current.value = ''
    }
  }

  return (
    <>
      <input
        ref={inputRef}
        type="file"
        accept={ACCEPT_ATTR}
        className="hidden"
        onChange={e => {
          const file = e.target.files?.[0]
          if (file) handleFile(file)
        }}
      />
      <button
        onClick={() => inputRef.current?.click()}
        disabled={busy}
        className="btn-secondary disabled:opacity-60"
      >
        {busy ? <Loader2 size={16} className="animate-spin" /> : <FolderOpen size={16} />}
        {busy
          ? fraction > 0 && fraction < 1
            ? `Uploading… ${Math.round(fraction * 100)}%`
            : 'Uploading…'
          : 'Upload a video'}
      </button>
    </>
  )
}
