'use client'

import { useCallback, useEffect, useRef, useState } from 'react'
import { CalendarRange, ChevronLeft, ChevronRight, Clock, Film, Image as ImageIcon, ImagePlus, Loader2, Play } from 'lucide-react'
import { toast } from 'react-hot-toast'
import { api, uploadPhoto } from '@/lib/api'
import { PhotoLightbox } from '@/components/media/PhotoLightbox'
import type {
  ApiError,
  MediaAsset,
  Timeline,
  TimelineGroup,
  TimelinePeriod,
  TimelineRecording,
} from '@/lib/types'

/**
 * The producer's life, one bubble per period — read-only.
 *
 * ORDER COMES FROM THE SERVER and is the question file's own order. This file
 * never sorts, never reorders by year, and knows no category name.
 *
 * THE SHAPE IS CONSTANT AT ANY ARCHIVE SIZE (docs/MEDIA_GALLERY.md §1.7-9).
 * The collapsed card is title, one sentence, and bubbles made of REAL TAG
 * CONTENT — the period's own topic tags, set-cover-chosen and capped
 * server-side, partitioning the recordings totally: every recording belongs
 * to exactly one bubble (the catch-all holds what no winning tag claimed).
 * The card is static and bubbles are the SOLE route in — there is no
 * period-wide list or filter view. Opening a bubble shows a CAPPED
 * highlight selection; the bubble's own full list is one click deeper. Raw
 * interview-question text never renders at any level — a moment's only name
 * is its generated content title.
 *
 * No year range yet, deliberately — §1.4's producer-scoped attribution is
 * not built. The header slot appears when that lands.
 *
 * Empty periods are already gone by the time they arrive: a category with no
 * recording is a question not yet answered, not a fact about the life.
 */

/** Which bubble is open, and whether it shows highlights or its full list. */
interface Expansion {
  category: string
  group: TimelineGroup
  showAll: boolean
}

interface RecordingSelection {
  category: string
  recording: TimelineRecording
}

/** A moment with no generated title yet (generation retries server-side). */
const UNTITLED = 'Recorded moment'

/**
 * "Add photos" on a period (§5's second upload entry point) — the same
 * presign → PUT → row flow as everywhere else, uploading CATEGORY-owned
 * photos. Compact: an icon in the gallery header. Full: the affordance a
 * deliberately opened, photo-less chapter shows.
 */
function AddPeriodPhotos({
  category,
  compact,
  onUploaded,
}: {
  category: string
  compact: boolean
  onUploaded: () => void
}) {
  const inputRef = useRef<HTMLInputElement>(null)
  const [uploading, setUploading] = useState(false)

  const handleFiles = async (files: FileList | null) => {
    if (!files || files.length === 0) return
    setUploading(true)
    try {
      for (const file of Array.from(files)) {
        try {
          await uploadPhoto({ category }, file)
        } catch (e: unknown) {
          const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail
          toast.error(detail ?? `Couldn't upload ${file.name}`)
        }
      }
      onUploaded()
    } finally {
      setUploading(false)
      if (inputRef.current) inputRef.current.value = ''
    }
  }

  return (
    <>
      <button
        type="button"
        onClick={() => !uploading && inputRef.current?.click()}
        disabled={uploading}
        className={
          compact
            ? 'ml-auto text-gray-500 hover:text-white transition-colors'
            : 'btn-secondary self-start'
        }
        title="Add photos to this chapter"
      >
        {uploading ? (
          <Loader2 size={compact ? 14 : 16} className="animate-spin" />
        ) : (
          <ImagePlus size={compact ? 14 : 16} />
        )}
        {!compact && 'Add photos'}
      </button>
      <input
        ref={inputRef}
        type="file"
        accept="image/jpeg,image/png,image/webp"
        multiple
        className="hidden"
        onChange={(e) => void handleFiles(e.target.files)}
      />
    </>
  )
}

function GroupBubble({
  group,
  onSelect,
  selected,
}: {
  group: TimelineGroup
  onSelect: (g: TimelineGroup) => void
  selected: boolean
}) {
  return (
    <button
      type="button"
      onClick={() => onSelect(group)}
      className={`px-3 py-1.5 rounded-xl border transition-colors ${
        selected
          ? 'border-primary-400 bg-primary-500/15'
          : 'border-white/10 bg-surface-800/50 hover:border-white/25'
      }`}
    >
      <span dir="auto" className="text-sm text-white">{group.label}</span>
      <span className="text-[11px] text-primary-300 ml-1.5">×{group.count}</span>
    </button>
  )
}

function MomentRow({
  recording,
  caption,
  onSelect,
  selected,
}: {
  recording: TimelineRecording
  caption?: string | null
  onSelect: (r: TimelineRecording) => void
  selected: boolean
}) {
  return (
    <button
      type="button"
      onClick={() => onSelect(recording)}
      className={`w-full flex items-start gap-2 px-3 py-2 rounded-xl border text-left transition-colors ${
        selected
          ? 'border-primary-400 bg-primary-500/15'
          : 'border-white/10 bg-surface-800/50 hover:border-white/25'
      }`}
    >
      <Play size={13} className="shrink-0 mt-1 text-primary-400" />
      <span className="flex-1 min-w-0">
        <span dir="auto" className="block text-sm text-white leading-snug">
          {recording.title || UNTITLED}
        </span>
        {caption && (
          <span dir="auto" className="block text-[11px] text-gray-500 leading-snug mt-0.5">
            {caption}
          </span>
        )}
      </span>
      {recording.take_count > 1 && (
        <span className="text-[11px] text-gray-500 shrink-0 mt-0.5">
          take {recording.take_index}/{recording.take_count}
        </span>
      )}
    </button>
  )
}

function Period({
  period,
  expansion,
  onSelectGroup,
  onShowAll,
  onBackToHighlights,
  playingSegmentId,
  onPlay,
  onHover,
}: {
  period: TimelinePeriod
  expansion: Expansion | null
  onSelectGroup: (g: TimelineGroup) => void
  onShowAll: () => void
  onBackToHighlights: () => void
  playingSegmentId: string | null
  onPlay: (r: TimelineRecording) => void
  /** Activates this period's photo gallery in the side panel (§9.2's hover
   *  trigger). Bubbles sit inside the card, so hovering one hovers this too
   *  — same gallery either way, which is the one-per-category rule. */
  onHover: () => void
}) {
  const group = expansion?.group ?? null
  const byId = new Map(period.recordings.map((r) => [r.segment_id, r]))

  // The bubble's own full list — bubbles partition the period, so this is
  // the deepest level and the only one whose length follows the archive.
  // There is no period-wide list: the partition is the navigation.
  const allRecordings = group
    ? group.segment_ids
        .map((sid) => byId.get(sid))
        .filter((r): r is TimelineRecording => r !== undefined)
    : []

  return (
    <section className="relative pl-8 pb-8 last:pb-0">
      {/* The spine. Purely decorative — the ORDER is the server's. */}
      <span className="absolute left-[7px] top-2 bottom-0 w-px bg-white/10" aria-hidden />
      <span className="absolute left-0 top-1.5 w-3.5 h-3.5 rounded-full bg-primary-500/80 ring-4 ring-surface-900" aria-hidden />

      <div className="glass-card p-4" onPointerEnter={onHover}>
        {/* The collapsed shape: title, sentence, bubbles. Nothing else, at
            any archive size — the card itself is static. */}
        <h2 dir="auto" className="text-base font-bold text-white">
          {period.category_label}
        </h2>

        {/* A period the interview no longer contains. Kept visible rather
            than dropped — its recordings are real answers. */}
        {period.retired_only && (
          <p className="text-[11px] text-amber-300/80 mt-1">
            From an earlier version of the interview.
          </p>
        )}

        {period.summary && (
          <p dir="auto" className="text-sm text-gray-300 mt-2 leading-snug">
            {period.summary}
          </p>
        )}

        {period.groups.length > 0 && (
          <div className="flex flex-wrap gap-2 mt-3">
            {period.groups.map((g) => (
              <GroupBubble
                key={g.key}
                group={g}
                selected={group?.key === g.key}
                onSelect={onSelectGroup}
              />
            ))}
          </div>
        )}

        {group && (
          <div className="mt-4 pt-3 border-t border-white/10 flex flex-col gap-2">
            {!expansion?.showAll ? (
              <>
                {group.highlights.map((h) => {
                  const recording = byId.get(h.segment_id)
                  if (!recording) return null
                  return (
                    <MomentRow
                      key={h.segment_id}
                      recording={recording}
                      caption={h.caption}
                      selected={playingSegmentId === h.segment_id}
                      onSelect={onPlay}
                    />
                  )
                })}
                {group.segment_ids.length > group.highlights.length && (
                  <button
                    type="button"
                    onClick={onShowAll}
                    className="self-start flex items-center gap-1 text-xs text-primary-300 hover:text-primary-200 mt-1"
                  >
                    All {group.segment_ids.length} moments
                    <ChevronRight size={13} />
                  </button>
                )}
              </>
            ) : (
              <>
                <button
                  type="button"
                  onClick={onBackToHighlights}
                  className="self-start flex items-center gap-1 text-xs text-primary-300 hover:text-primary-200"
                >
                  <ChevronLeft size={13} />
                  Highlights
                </button>
                <div className="flex flex-col gap-1.5">
                  {allRecordings.map((recording) => (
                    <MomentRow
                      key={recording.segment_id}
                      recording={recording}
                      selected={playingSegmentId === recording.segment_id}
                      onSelect={onPlay}
                    />
                  ))}
                </div>
              </>
            )}
          </div>
        )}
      </div>
    </section>
  )
}

export function TimelinePanel() {
  const [timeline, setTimeline] = useState<Timeline | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [expansion, setExpansion] = useState<Expansion | null>(null)
  const [playing, setPlaying] = useState<RecordingSelection | null>(null)

  // ── The period gallery (MEDIA_GALLERY.md §4.1, corrected wording) ──────
  // ONE gallery per CATEGORY, never per bubble or entity: photos attach to
  // the period as a whole (§2.2), so every bubble in a period surfaces the
  // same set. Hovering a period card activates its gallery (§9.2); clicking
  // a bubble pins it. Hover-away does NOT clear — a gallery that vanishes
  // while the pointer travels to the side panel can never be clicked.
  const [hoveredCategory, setHoveredCategory] = useState<string | null>(null)
  const [galleries, setGalleries] = useState<Record<string, MediaAsset[]>>({})
  const [lightbox, setLightbox] = useState<{ photos: MediaAsset[]; index: number } | null>(null)

  const galleryCategory = expansion?.category ?? hoveredCategory

  useEffect(() => {
    if (!galleryCategory || galleries[galleryCategory] !== undefined) return
    let cancelled = false
    api
      .listMedia({ category: galleryCategory })
      .then((photos: MediaAsset[]) => {
        if (!cancelled) setGalleries((g) => ({ ...g, [galleryCategory]: photos }))
      })
      .catch(() => {
        // Cache the miss as empty: photos are decoration here, and retrying
        // on every hover would hammer a failing endpoint for nothing.
        if (!cancelled) setGalleries((g) => ({ ...g, [galleryCategory]: [] }))
      })
    return () => {
      cancelled = true
    }
  }, [galleryCategory, galleries])

  const galleryPhotos = galleryCategory ? galleries[galleryCategory] : undefined

  /** Drop a category from the cache; the fetch effect reloads it. */
  const refreshGallery = (category: string) =>
    setGalleries(({ [category]: _dropped, ...rest }) => rest)

  const load = useCallback(async () => {
    setError(null)
    try {
      setTimeline(await api.getTimeline())
    } catch (err: unknown) {
      const detail = (err as ApiError)?.response?.data?.detail || (err as ApiError)?.message
      setError(detail || 'Could not load your timeline')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    load()
  }, [load])

  const selectGroup = (category: string) => (group: TimelineGroup) => {
    setExpansion((current) =>
      current?.category === category && current.group.key === group.key
        ? null
        : { category, group, showAll: false }
    )
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center py-24">
        <Loader2 size={26} className="animate-spin text-primary-400" />
      </div>
    )
  }

  if (error || !timeline) {
    return (
      <div className="flex items-center justify-center py-24 px-6">
        <div className="max-w-sm text-center flex flex-col items-center gap-4">
          <p className="text-sm text-gray-300">{error || 'Something went wrong'}</p>
          <button onClick={load} className="btn-primary">Try again</button>
        </div>
      </div>
    )
  }

  return (
    <div className="animate-fade-in max-w-7xl mx-auto px-6 pt-6 pb-16">
      <header className="flex items-center gap-2 text-primary-400 mb-6">
        <CalendarRange size={16} />
        <span className="text-sm font-medium">Your life so far</span>
      </header>

      <div className="grid grid-cols-1 lg:grid-cols-5 gap-6 items-start">
        <div className="lg:col-span-3">
          {timeline.periods.length === 0 ? (
            <div className="glass-card p-6 flex flex-col items-center gap-3 text-center">
              <Clock size={26} className="text-primary-400" />
              <h2 className="text-lg font-bold text-white">Nothing here yet</h2>
              <p className="text-sm text-gray-400 max-w-sm">
                Each answer you record adds to this page. Record one and the first
                chapter of your life appears here.
              </p>
            </div>
          ) : (
            timeline.periods.map((period) => (
              <Period
                key={period.category}
                period={period}
                expansion={expansion?.category === period.category ? expansion : null}
                onSelectGroup={selectGroup(period.category)}
                onShowAll={() =>
                  setExpansion((c) => (c ? { ...c, showAll: true } : c))
                }
                onBackToHighlights={() =>
                  setExpansion((c) => (c ? { ...c, showAll: false } : c))
                }
                playingSegmentId={playing?.recording.segment_id ?? null}
                onPlay={(recording) => setPlaying({ category: period.category, recording })}
                onHover={() => setHoveredCategory(period.category)}
              />
            ))
          )}

          {/* Said out loud rather than left as a gap: a page showing three
              bubbles should say whether that is the whole interview. */}
          {timeline.hidden_empty_periods > 0 && (
            <p className="text-xs text-gray-500 pl-8 mt-2">
              {timeline.hidden_empty_periods} more chapter
              {timeline.hidden_empty_periods === 1 ? '' : 's'} appear here once you
              answer something in {timeline.hidden_empty_periods === 1 ? 'it' : 'them'}.
            </p>
          )}
          {timeline.unplaced_recordings > 0 && (
            <p className="text-xs text-gray-500 pl-8 mt-1">
              {timeline.unplaced_recordings} recording
              {timeline.unplaced_recordings === 1 ? '' : 's'} don&apos;t belong to a
              chapter — they were made outside the guided questions.
            </p>
          )}
        </div>

        <aside className="lg:col-span-2 lg:sticky lg:top-6">
          {!playing ? (
            <div className="glass-card p-5 flex flex-col items-center gap-2 text-center">
              <Film size={18} className="text-gray-500" />
              <p className="text-xs text-gray-500">
                Open a chapter and choose a moment to watch it here.
              </p>
            </div>
          ) : (
            <div className="glass-card p-4 flex flex-col gap-3">
              <h2 dir="auto" className="text-base font-bold text-white leading-snug">
                {playing.recording.title || UNTITLED}
              </h2>
              {playing.recording.take_count > 1 && (
                <p className="text-[11px] text-gray-500">
                  Take {playing.recording.take_index} of {playing.recording.take_count}
                </p>
              )}
              {playing.recording.video_url ? (
                <video
                  key={playing.recording.segment_id}
                  src={playing.recording.video_url}
                  controls
                  playsInline
                  className="w-full rounded-lg border border-white/10"
                />
              ) : (
                <p className="text-xs text-gray-500">
                  This recording is still being processed.
                </p>
              )}
            </div>
          )}

          {/* ── The chapter's photo gallery (§4.1) ─────────────────────
              One per CATEGORY, beside whatever the panel is doing —
              photos accompany the clips, they never replace them. A
              merely-hovered chapter without photos shows nothing (the
              gallery is decoration, not a slot demanding content); a
              deliberately OPENED one offers the §5 "Add photos" entry
              point instead of silence. */}
          {galleryCategory && galleryPhotos && galleryPhotos.length > 0 && (
            <div className="glass-card p-4 mt-4 flex flex-col gap-3">
              <div className="flex items-center gap-2">
                <ImageIcon size={14} className="text-primary-400" />
                <h3 className="text-sm font-medium text-white">
                  Photos from{' '}
                  <span dir="auto">
                    {timeline.periods.find((p) => p.category === galleryCategory)
                      ?.category_label ?? 'this chapter'}
                  </span>
                </h3>
                <AddPeriodPhotos
                  category={galleryCategory}
                  compact
                  onUploaded={() => refreshGallery(galleryCategory)}
                />
              </div>
              <ul className="grid grid-cols-3 gap-2">
                {galleryPhotos.map((photo, i) => (
                  <li key={photo.id}>
                    <button
                      type="button"
                      onClick={() => setLightbox({ photos: galleryPhotos, index: i })}
                      className="block w-full aspect-square rounded-lg overflow-hidden border border-white/10 hover:border-white/30 focus:outline-none focus:ring-2 focus:ring-primary-400"
                      aria-label={photo.caption || 'Open photo'}
                    >
                      {/* eslint-disable-next-line @next/next/no-img-element */}
                      <img
                        src={photo.url}
                        alt={photo.caption ?? ''}
                        className="w-full h-full object-cover"
                      />
                    </button>
                  </li>
                ))}
              </ul>
            </div>
          )}

          {/* A chapter the producer deliberately opened, with no photos yet:
              the one place the empty state earns its chrome. Hover alone
              never shows this. */}
          {expansion &&
            galleryCategory === expansion.category &&
            galleryPhotos !== undefined &&
            galleryPhotos.length === 0 && (
              <div className="glass-card p-4 mt-4 flex flex-col gap-2">
                <p className="text-xs text-gray-500">
                  No photos in this chapter yet — they&apos;ll show beside it here
                  and in the family&apos;s view.
                </p>
                <AddPeriodPhotos
                  category={expansion.category}
                  compact={false}
                  onUploaded={() => refreshGallery(expansion.category)}
                />
              </div>
            )}
        </aside>
      </div>

      {lightbox && (
        <PhotoLightbox
          photos={lightbox.photos}
          initialIndex={lightbox.index}
          onClose={() => setLightbox(null)}
        />
      )}
    </div>
  )
}
