'use client'

import { useState } from 'react'
import { CheckCircle2, Clock, Loader2, Trash2, AlertTriangle } from 'lucide-react'
import { toast } from 'react-hot-toast'
import { api } from '@/lib/api'
import type { ApiError, RawSegment } from '@/lib/types'

/**
 * The takes recorded for ONE interview question.
 *
 * A question holds several recordings, so reviewing them is a list rather
 * than the single "you already answered this" panel the recorder used to
 * show. Replacing an answer is now delete + record, both explicit, instead
 * of a new take silently destroying the previous one.
 */

interface RecordingListProps {
  recordings: RawSegment[]
  /** Called after a delete lands, so the caller can refetch the session. */
  onDeleted: () => void | Promise<void>
}

/** Analysis runs after upload, so a just-recorded take is not immediately
 *  answerable. Showing that plainly beats a silent gap where the recording
 *  is visible but the archive doesn't know about it yet. */
function statusLabel(status: string): { icon: React.ReactNode; text: string; tone: string } {
  if (status === 'ready' || status === 'analyzed') {
    return {
      icon: <CheckCircle2 size={14} />,
      text: 'Saved to your story',
      tone: 'text-calm-sage-700 dark:text-calm-sage-300',
    }
  }
  if (status === 'failed') {
    return {
      icon: <AlertTriangle size={14} />,
      text: 'Something went wrong processing this',
      tone: 'text-amber-600',
    }
  }
  return {
    icon: <Clock size={14} />,
    text: 'Still processing…',
    tone: 'text-calm-inkmuted dark:text-calm-inkmutedDark',
  }
}

function takeLabel(index: number, total: number): string {
  return total === 1 ? 'Your answer' : `Take ${index + 1} of ${total}`
}

export function RecordingList({ recordings, onDeleted }: RecordingListProps) {
  // Which take is mid-delete. Per-id rather than a single boolean so one
  // slow delete doesn't disable the others or spin the wrong row.
  const [deletingId, setDeletingId] = useState<string | null>(null)
  const [confirmingId, setConfirmingId] = useState<string | null>(null)

  const handleDelete = async (segment: RawSegment) => {
    setDeletingId(segment.id)
    try {
      await api.deleteSegment(segment.id)
      // Deletion reaches the graph and the derived caches, not just the row,
      // so this can take a moment and is worth confirming out loud.
      toast.success('Recording deleted')
      setConfirmingId(null)
      await onDeleted()
    } catch (err: unknown) {
      const detail = (err as ApiError)?.response?.data?.detail || (err as ApiError)?.message
      toast.error(detail || 'Could not delete that recording')
    } finally {
      setDeletingId(null)
    }
  }

  if (recordings.length === 0) return null

  return (
    <ul className="flex flex-col gap-4">
      {recordings.map((segment, i) => {
        const { icon, text, tone } = statusLabel(segment.status)
        const isDeleting = deletingId === segment.id
        const isConfirming = confirmingId === segment.id
        return (
          <li
            key={segment.id}
            className="rounded-2xl border border-calm-border dark:border-calm-borderDark bg-calm-card dark:bg-calm-cardDark overflow-hidden"
          >
            <div className="flex items-center justify-between gap-3 px-4 py-3 border-b border-calm-border dark:border-calm-borderDark">
              <div className="min-w-0">
                <p className="text-sm font-medium text-calm-ink dark:text-calm-inkDark">
                  {takeLabel(i, recordings.length)}
                </p>
                <p className={`text-xs flex items-center gap-1.5 mt-0.5 ${tone}`}>
                  {icon}
                  {text}
                </p>
              </div>
              <button
                onClick={() => setConfirmingId(isConfirming ? null : segment.id)}
                disabled={isDeleting}
                className="shrink-0 p-2 rounded-lg text-calm-inkmuted hover:text-red-600 hover:bg-red-50 dark:hover:bg-red-500/10 disabled:opacity-40 transition-colors"
                aria-label={`Delete ${takeLabel(i, recordings.length).toLowerCase()}`}
              >
                {isDeleting ? <Loader2 size={16} className="animate-spin" /> : <Trash2 size={16} />}
              </button>
            </div>

            {isConfirming && (
              // Deleting a recording destroys the footage, its transcript and
              // what the archive learned from it — there is no undo, so it
              // asks first, inline rather than in a modal that would cover
              // the very video being judged.
              <div className="flex flex-wrap items-center justify-between gap-3 px-4 py-3 bg-red-50 dark:bg-red-500/10 border-b border-red-200/60 dark:border-red-500/20">
                <p className="text-sm text-red-800 dark:text-red-300">
                  Delete this recording for good? Its transcript goes too.
                </p>
                <div className="flex items-center gap-2">
                  <button
                    onClick={() => setConfirmingId(null)}
                    className="calm-btn-secondary py-1.5 text-sm"
                  >
                    Keep it
                  </button>
                  <button
                    onClick={() => handleDelete(segment)}
                    disabled={isDeleting}
                    className="py-1.5 px-3 text-sm rounded-lg bg-red-600 text-white hover:bg-red-700 disabled:opacity-60 flex items-center gap-1.5"
                  >
                    {isDeleting && <Loader2 size={14} className="animate-spin" />}
                    Delete
                  </button>
                </div>
              </div>
            )}

            {segment.video_url ? (
              <video
                controls
                preload="metadata"
                src={segment.video_url}
                className="w-full bg-black aspect-video"
              />
            ) : (
              <div className="flex items-center justify-center gap-2 py-10 text-sm text-calm-inkmuted dark:text-calm-inkmutedDark">
                <Loader2 size={16} className="animate-spin" />
                Preparing playback…
              </div>
            )}
          </li>
        )
      })}
    </ul>
  )
}
