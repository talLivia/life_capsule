'use client'

import { useCallback, useEffect, useState } from 'react'
import { CalendarRange, Clock, Film, Loader2, Play } from 'lucide-react'
import { api } from '@/lib/api'
import type { ApiError, Timeline, TimelinePerson, TimelinePeriod, TimelineRecording } from '@/lib/types'

/**
 * The producer's life, one bubble per period — read-only.
 *
 * ORDER COMES FROM THE SERVER and is the question file's own order. This file
 * never sorts, never reorders by year, and knows no category name. Reordering
 * the interview reorders this page with no change here.
 *
 * A period's content is its RECORDINGS (docs/MEDIA_GALLERY.md §1): every
 * period lists them, titled by their interview question, playable directly.
 * People are a lens — selecting one filters the recordings already shown to
 * those mentioning them; it opens nothing separate. A period whose answers
 * named nobody shows its recordings exactly like every other one.
 *
 * Empty periods are already gone by the time they arrive: a category with no
 * recording is a question not yet answered, not a fact about the life. The
 * count of what was hidden is shown, because a producer looking at three
 * bubbles should know whether that is the whole interview.
 *
 * Years are decoration. A person carries one when the archive knows it, and it
 * never moves anybody — a page ordered two ways is a page that disagrees with
 * itself.
 */

/** Which person filters which period. A person can appear in two periods, so
 *  the selection is scoped to the period whose chip was clicked. */
interface PersonSelection {
  category: string
  person: TimelinePerson
}

interface RecordingSelection {
  category: string
  recording: TimelineRecording
}

function PersonChip({
  person,
  onSelect,
  selected,
}: {
  person: TimelinePerson
  onSelect: (p: TimelinePerson) => void
  selected: boolean
}) {
  return (
    <button
      type="button"
      onClick={() => onSelect(person)}
      title={selected ? 'Show every recording again' : `Only the moments about ${person.name}`}
      className={`px-3 py-1.5 rounded-xl border text-left transition-colors ${
        selected
          ? 'border-primary-400 bg-primary-500/15'
          : 'border-white/10 bg-surface-800/50 hover:border-white/25'
      }`}
    >
      <span dir="auto" className="text-sm text-white">{person.name}</span>
      {person.year_start && (
        <span className="text-[11px] text-gray-500 ml-1.5">{person.year_start}</span>
      )}
      {person.mentions > 1 && (
        <span className="text-[11px] text-primary-300 ml-1.5">×{person.mentions}</span>
      )}
    </button>
  )
}

function RecordingRow({
  recording,
  onSelect,
  selected,
}: {
  recording: TimelineRecording
  onSelect: (r: TimelineRecording) => void
  selected: boolean
}) {
  return (
    <button
      type="button"
      onClick={() => onSelect(recording)}
      className={`w-full flex items-center gap-2 px-3 py-2 rounded-xl border text-left transition-colors ${
        selected
          ? 'border-primary-400 bg-primary-500/15'
          : 'border-white/10 bg-surface-800/50 hover:border-white/25'
      }`}
    >
      <Play size={13} className="shrink-0 text-primary-400" />
      <span dir="auto" className="text-sm text-white leading-snug flex-1">
        {recording.question_asked}
      </span>
      {recording.take_count > 1 && (
        <span className="text-[11px] text-gray-500 shrink-0">
          take {recording.take_index}/{recording.take_count}
        </span>
      )}
    </button>
  )
}

function Period({
  period,
  selectedPerson,
  onSelectPerson,
  playingSegmentId,
  onPlay,
}: {
  period: TimelinePeriod
  selectedPerson: TimelinePerson | null
  onSelectPerson: (p: TimelinePerson) => void
  playingSegmentId: string | null
  onPlay: (r: TimelineRecording) => void
}) {
  const recordings = selectedPerson
    ? period.recordings.filter((r) => selectedPerson.segment_ids.includes(r.segment_id))
    : period.recordings

  return (
    <section className="relative pl-8 pb-8 last:pb-0">
      {/* The spine. Purely decorative — the ORDER is the server's. */}
      <span className="absolute left-[7px] top-2 bottom-0 w-px bg-white/10" aria-hidden />
      <span className="absolute left-0 top-1.5 w-3.5 h-3.5 rounded-full bg-primary-500/80 ring-4 ring-surface-900" aria-hidden />

      <div className="glass-card p-4">
        <header className="flex flex-wrap items-baseline justify-between gap-2 mb-1">
          <h2 dir="auto" className="text-base font-bold text-white">
            {period.category_label}
          </h2>
          <span className="text-[11px] text-gray-500">
            {period.question_count} question{period.question_count === 1 ? '' : 's'} answered
            {period.recording_count !== period.question_count &&
              ` · ${period.recording_count} recordings`}
          </span>
        </header>

        {/* A period the interview no longer contains. Kept visible rather than
            dropped — the recordings in it are real answers, and they stay
            until the producer moves them somewhere. */}
        {period.retired_only && (
          <p className="text-[11px] text-amber-300/80 mb-2">
            From an earlier version of the interview.
          </p>
        )}

        {period.people.length > 0 && (
          <div className="flex flex-wrap gap-2 mt-2">
            {period.people.map((person) => (
              <PersonChip
                key={person.id}
                person={person}
                selected={selectedPerson?.id === person.id}
                onSelect={onSelectPerson}
              />
            ))}
          </div>
        )}

        <div className="flex flex-col gap-1.5 mt-2">
          {recordings.map((recording) => (
            <RecordingRow
              key={recording.segment_id}
              recording={recording}
              selected={playingSegmentId === recording.segment_id}
              onSelect={onPlay}
            />
          ))}
        </div>

        {selectedPerson && (
          <p dir="auto" className="text-[11px] text-gray-500 mt-2">
            Only the moments about {selectedPerson.name} — choose them again to
            see everything.
          </p>
        )}
      </div>
    </section>
  )
}

export function TimelinePanel() {
  const [timeline, setTimeline] = useState<Timeline | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [selected, setSelected] = useState<PersonSelection | null>(null)
  const [playing, setPlaying] = useState<RecordingSelection | null>(null)

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

  const togglePerson = (category: string) => (person: TimelinePerson) => {
    setSelected((current) =>
      current?.category === category && current.person.id === person.id
        ? null
        : { category, person }
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
                selectedPerson={
                  selected?.category === period.category ? selected.person : null
                }
                onSelectPerson={togglePerson(period.category)}
                playingSegmentId={playing?.recording.segment_id ?? null}
                onPlay={(recording) => setPlaying({ category: period.category, recording })}
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
                Choose a moment to watch it here.
              </p>
            </div>
          ) : (
            <div className="glass-card p-4 flex flex-col gap-3">
              <h2 dir="auto" className="text-base font-bold text-white leading-snug">
                {playing.recording.question_asked}
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
        </aside>
      </div>
    </div>
  )
}
