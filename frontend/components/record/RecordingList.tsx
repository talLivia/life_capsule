'use client'

import { useState } from 'react'
import {
  CheckCircle2,
  ChevronDown,
  Clock,
  Loader2,
  Trash2,
  AlertTriangle,
  Sparkles,
} from 'lucide-react'
import { toast } from 'react-hot-toast'
import { api } from '@/lib/api'
import { ExtractionModal } from '@/components/record/ExtractionModal'
import type { ApiError, RawSegment } from '@/lib/types'

/**
 * The takes recorded for ONE interview question.
 *
 * A question holds several recordings, so reviewing them is a list rather
 * than the single "you already answered this" panel the recorder used to
 * show. Replacing an answer is now delete + record, both explicit, instead
 * of a new take silently destroying the previous one.
 *
 * ONE video area (§13.1). Several takes stacked as full players made the
 * page scroll past the question being answered, and put three copies of the
 * same face on screen at once. The other takes are collapsed rows that expand
 * into the player when chosen — and with a single take there is no list at
 * all, because a list of one is just furniture.
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
      tone: 'text-green-400',
    }
  }
  if (status === 'failed') {
    return {
      icon: <AlertTriangle size={14} />,
      text: 'Something went wrong processing this',
      tone: 'text-amber-400',
    }
  }
  return {
    icon: <Clock size={14} />,
    text: 'Still processing…',
    tone: 'text-muted2',
  }
}

function takeLabel(index: number, total: number): string {
  return total === 1 ? 'Your answer' : `Take ${index + 1} of ${total}`
}

/** The generated content title is a take's name everywhere (§1.10); the
 *  take label survives only as the fallback while a title doesn't exist —
 *  mid-processing, or a save whose title generation failed. */
function segmentTitle(segment: RawSegment, index: number, total: number): string {
  return segment.moment_title || takeLabel(index, total)
}

export function RecordingList({ recordings, onDeleted }: RecordingListProps) {
  // Which take is mid-delete. Per-id rather than a single boolean so one
  // slow delete doesn't disable the others or spin the wrong row.
  const [deletingId, setDeletingId] = useState<string | null>(null)
  const [confirmingId, setConfirmingId] = useState<string | null>(null)
  // Which take's extraction panel is open, if any.
  const [inspectingId, setInspectingId] = useState<string | null>(null)
  // Which take is in the player. Null means "the newest", which is what a
  // producer who just recorded wants to see.
  const [openId, setOpenId] = useState<string | null>(null)

  const handleDelete = async (segment: RawSegment) => {
    setDeletingId(segment.id)
    try {
      await api.deleteSegment(segment.id)
      // Deletion reaches the graph and the derived caches, not just the row,
      // so this can take a moment and is worth confirming out loud.
      toast.success('Recording deleted')
      setConfirmingId(null)
      // The open take may be the one just destroyed; fall back rather than
      // leaving the player pointed at a row that no longer exists.
      setOpenId(null)
      await onDeleted()
    } catch (err: unknown) {
      const detail = (err as ApiError)?.response?.data?.detail || (err as ApiError)?.message
      toast.error(detail || 'Could not delete that recording')
    } finally {
      setDeletingId(null)
    }
  }

  if (recordings.length === 0) return null

  const open =
    recordings.find(r => r.id === openId) ?? recordings[recordings.length - 1]
  const openIndex = recordings.findIndex(r => r.id === open.id)
  const total = recordings.length

  return (
    <div className="flex flex-col gap-3">
      {/* ── The player ─────────────────────────────────────────────── */}
      <div className="glass-card">
        <div className="flex items-center justify-between gap-3 px-4 py-3 border-b border-edge">
          <div className="min-w-0">
            <p dir="auto" className="text-sm font-semibold text-ink">
              {segmentTitle(open, openIndex, total)}
            </p>
            <p
              className={`text-xs flex items-center gap-1.5 mt-0.5 ${
                statusLabel(open.status).tone
              }`}
            >
              {statusLabel(open.status).icon}
              {statusLabel(open.status).text}
            </p>
          </div>
          <div className="flex items-center gap-2 shrink-0">
            <button
              onClick={() => setInspectingId(open.id)}
              className="btn-secondary py-1.5 px-3 text-sm"
            >
              <Sparkles size={14} />
              Extracted from this
            </button>
            <button
              onClick={() => setConfirmingId(confirmingId === open.id ? null : open.id)}
              disabled={deletingId === open.id}
              className="w-9 h-9 flex items-center justify-center rounded-lg text-muted hover:text-red-300 hover:bg-red-600/20 border border-transparent hover:border-red-500/50 disabled:opacity-40 transition-all duration-200"
              aria-label={`Delete ${takeLabel(openIndex, total).toLowerCase()}`}
            >
              {deletingId === open.id ? (
                <Loader2 size={16} className="animate-spin" />
              ) : (
                <Trash2 size={16} />
              )}
            </button>
          </div>
        </div>

        {confirmingId === open.id && (
          // Deleting a recording destroys the footage, its transcript and
          // what the archive learned from it — there is no undo, so it
          // asks first, inline rather than in a modal that would cover
          // the very video being judged.
          <div className="flex flex-wrap items-center justify-between gap-3 px-4 py-3 bg-red-500/10 border-b border-red-500/20">
            <p className="text-sm text-red-300">
              Delete this recording for good? Its transcript goes too.
            </p>
            <div className="flex items-center gap-2">
              <button onClick={() => setConfirmingId(null)} className="btn-secondary py-1.5 text-sm">
                Keep it
              </button>
              <button
                onClick={() => handleDelete(open)}
                disabled={deletingId === open.id}
                className="btn-danger py-1.5 disabled:opacity-40"
              >
                {deletingId === open.id && <Loader2 size={14} className="animate-spin" />}
                Delete
              </button>
            </div>
          </div>
        )}

        {open.video_url ? (
          <video
            // Keyed by take, so choosing another row loads that video rather
            // than leaving the previous one's frames on screen.
            key={open.id}
            controls
            preload="metadata"
            src={open.video_url}
            className="w-full bg-black aspect-video"
          />
        ) : (
          <div className="flex items-center justify-center gap-2 py-10 text-sm text-muted2">
            <Loader2 size={16} className="animate-spin" />
            Preparing playback…
          </div>
        )}
      </div>

      {/* ── The other takes ────────────────────────────────────────── */}
      {/* Only when there ARE others. One take needs no list saying so. */}
      {total > 1 && (
        <ul className="flex flex-col gap-1.5">
          {recordings.map((segment, i) => {
            if (segment.id === open.id) return null
            const { icon, text, tone } = statusLabel(segment.status)
            return (
              <li key={segment.id}>
                <button
                  type="button"
                  onClick={() => setOpenId(segment.id)}
                  className="w-full flex items-center gap-3 px-4 py-2.5 rounded-xl border border-edge
                    bg-surface-800/40 hover:border-edge-strong hover:bg-veil transition-colors text-left"
                >
                  <ChevronDown size={15} className="text-muted2 shrink-0 -rotate-90" aria-hidden />
                  <span className="min-w-0 flex-1">
                    <span dir="auto" className="block text-sm text-ink">
                      {segmentTitle(segment, i, total)}
                    </span>
                    <span className={`text-xs flex items-center gap-1.5 mt-0.5 ${tone}`}>
                      {icon}
                      {text}
                    </span>
                  </span>
                  <span className="text-[11px] text-muted2 shrink-0">Play</span>
                </button>
              </li>
            )
          })}
        </ul>
      )}

      {inspectingId && (
        <ExtractionModal
          segmentId={inspectingId}
          title={segmentTitle(
            recordings.find(r => r.id === inspectingId) ?? open,
            recordings.findIndex(r => r.id === inspectingId),
            total,
          )}
          onClose={() => setInspectingId(null)}
        />
      )}
    </div>
  )
}
