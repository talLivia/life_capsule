'use client'

import { useCallback, useEffect, useState } from 'react'
import Link from 'next/link'
import { ChevronLeft, ChevronRight, Feather, Loader2, ShieldOff, PartyPopper, Gift, ListChecks, Plus } from 'lucide-react'
import { toast } from 'react-hot-toast'
import { EntityConfirmModal } from '@/components/record/EntityConfirmModal'
import { RecordingList } from '@/components/record/RecordingList'
import { SegmentUpload } from '@/components/record/SegmentUpload'
import { VideoRecorder } from '@/components/record/VideoRecorder'
import { api } from '@/lib/api'
import { useStore } from '@/store/useStore'
import type { ApiError, InterviewSessionState } from '@/lib/types'

/**
 * The story-recording interview, as an in-shell VIEW (rendered by the producer
 * studio's `record` view) rather than a standalone `/record` route. The auth
 * gate is handled by the shell, so this only keeps the producer-only guard.
 * The recording flow itself is unchanged from the former route — only the
 * outer full-screen wrappers were relaxed so it sits inside the app shell.
 */
/** How many interview questions have at least one recording. Counts DISTINCT
 *  question_index — a question with three takes is still one question
 *  answered. */
function countAnswered(segments: { question_index: number }[]): number {
  return new Set(segments.map(s => s.question_index)).size
}

export function RecordPanel() {
  const { user } = useStore()
  const [state, setState] = useState<InterviewSessionState | null>(null)
  const [currentIndex, setCurrentIndex] = useState(0)
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState<string | null>(null)
  // True for exactly one render: the moment the LAST unanswered question
  // gets accepted. Distinct from the ongoing "answeredCount >= total" state
  // (which stays true forever after, including while revisiting past
  // answers) — this is what actually shows a "you're done, here's what's
  // next" screen instead of silently leaving the user on the same last
  // question with no forward path (Next was already disabled there).
  const [justCompleted, setJustCompleted] = useState(false)
  // Opening the recorder on a question that ALREADY has takes. Cleared
  // whenever the question changes, so navigating away and back lands on the
  // list rather than a live camera the producer didn't ask for.
  const [addingTake, setAddingTake] = useState(false)

  const isProducer = user?.role === 'producer'

  const load = useCallback(async () => {
    setLoading(true)
    setLoadError(null)
    try {
      const data: InterviewSessionState = await api.getInterviewSession()
      setState(data)
      setCurrentIndex(data.session.current_question_index)
    } catch (err: unknown) {
      const detail = (err as ApiError)?.response?.data?.detail || (err as ApiError)?.message
      setLoadError(detail || 'Could not load the interview')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    if (isProducer) load()
  }, [isProducer, load])

  const goTo = async (index: number) => {
    if (!state) return
    setCurrentIndex(index)
    setAddingTake(false)
    try {
      const updated = await api.updateInterviewSession(state.session.id, index)
      setState(s => (s ? { ...s, session: updated } : s))
    } catch {
      toast.error('Could not save your position — you can keep going')
    }
  }

  const handleAccepted = async () => {
    if (!state) return
    // Refresh segments so the "already answered" state is accurate if the
    // user navigates back here later.
    const nextIndex = Math.min(currentIndex + 1, state.questions.length - 1)
    const atEnd = currentIndex === state.questions.length - 1
    const wasIncomplete = countAnswered(state.segments) < state.questions.length
    // Auto-advance only when this question had NOTHING before — that's the
    // sequential first pass, where moving on is what the producer wants.
    // A deliberate "add another take" must stay put and show the new take
    // beside the old one; advancing there would answer a question they
    // didn't ask and hide the very thing they just recorded.
    const wasFirstTake =
      state.segments.filter(s => s.question_index === currentIndex).length === 0
    setAddingTake(false)  // the new take is saved; show it in the list
    try {
      const data: InterviewSessionState = await api.getInterviewSession()
      setState(data)
      if (wasIncomplete && countAnswered(data.segments) >= data.questions.length) {
        setJustCompleted(true)
      }
    } catch {
      /* segment is already saved server-side; a refresh failure here is non-fatal */
    }
    if (!wasFirstTake) return
    if (!atEnd) {
      goTo(nextIndex)
    } else {
      setCurrentIndex(nextIndex)
    }
  }

  // ── Role gate (auth is handled by the shell) ──
  if (!isProducer) {
    return (
      <div className="flex items-center justify-center py-24 px-6">
        <div className="max-w-sm text-center flex flex-col items-center gap-4">
          <ShieldOff size={28} className="text-calm-inkmuted dark:text-calm-inkmutedDark" />
          <h1 className="text-lg font-semibold text-calm-ink dark:text-calm-inkDark">
            This section is for the account owner
          </h1>
          <p className="text-sm text-calm-inkmuted dark:text-calm-inkmutedDark">
            Recording is only available to the producer account that owns this story archive.
          </p>
        </div>
      </div>
    )
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center py-24">
        <Loader2 size={26} className="animate-spin text-calm-sage-600" />
      </div>
    )
  }

  if (loadError || !state) {
    return (
      <div className="flex items-center justify-center py-24 px-6">
        <div className="max-w-sm text-center flex flex-col items-center gap-4">
          <p className="text-sm text-calm-ink dark:text-calm-inkDark">{loadError || 'Something went wrong'}</p>
          <button onClick={load} className="calm-btn-primary">Try again</button>
        </div>
      </div>
    )
  }

  const total = state.questions.length
  const question = state.questions[currentIndex]
  // A question can now have SEVERAL recordings, so this is a list.
  const recordings = state.segments.filter(s => s.question_index === currentIndex)
  const showRecorder = recordings.length === 0 || addingTake
  const isLast = currentIndex === total - 1
  const isFirst = currentIndex === 0
  // DISTINCT questions answered — never the number of segment rows. With
  // siblings allowed, three takes on one question would have read as
  // "3 of 12 answered" and could trip the "you've answered everything"
  // screen while most questions were still blank. That miscount fails
  // SILENTLY, which is why it changes in the same commit as the backend.
  const answeredCount = countAnswered(state.segments)
  const interviewComplete = answeredCount >= total

  if (justCompleted) {
    return (
      <div className="text-calm-ink dark:text-calm-inkDark flex items-center justify-center py-24 px-6">
        <div className="max-w-md text-center flex flex-col items-center gap-5">
          <PartyPopper size={32} className="text-calm-sage-600 dark:text-calm-sage-300" />
          <div>
            <h1 className="text-2xl font-semibold mb-2">You&apos;ve answered every question</h1>
            <p className="text-sm text-calm-inkmuted dark:text-calm-inkmutedDark">
              Your story is saved. Invite a family member from Settings so they can talk with
              it, or come back anytime to review or re-record any answer.
            </p>
          </div>
          <div className="flex flex-col sm:flex-row gap-3 w-full sm:w-auto">
            <Link href="/" className="calm-btn-primary justify-center">
              <Gift size={16} />
              Go invite family
            </Link>
            <button onClick={() => setJustCompleted(false)} className="calm-btn-secondary justify-center">
              <ListChecks size={16} />
              Review my answers
            </button>
          </div>
        </div>
      </div>
    )
  }

  return (
    <div className="text-calm-ink dark:text-calm-inkDark">
      <EntityConfirmModal />
      <header className="max-w-3xl mx-auto px-6 pt-10 pb-6">
        <div className="flex items-center gap-2 text-calm-sage-600 dark:text-calm-sage-300 mb-3">
          <Feather size={16} />
          <span className="text-sm font-medium">Your Story</span>
        </div>
        <div className="flex items-center justify-between mb-2">
          <p className="text-sm text-calm-inkmuted dark:text-calm-inkmutedDark">
            Question {currentIndex + 1} of {total}
          </p>
          <p className="text-xs text-calm-inkmuted dark:text-calm-inkmutedDark">
            {answeredCount} of {total} answered
          </p>
        </div>
        <div className="w-full h-1.5 rounded-full bg-calm-sage-100 dark:bg-calm-border overflow-hidden">
          <div
            className="h-full rounded-full bg-calm-sage-500 transition-all duration-300"
            style={{ width: `${((currentIndex + 1) / total) * 100}%` }}
          />
        </div>
      </header>

      <main className="max-w-3xl mx-auto px-6 pb-16 flex flex-col gap-6">
        {interviewComplete && (
          <div className="flex items-center gap-3 px-4 py-3 rounded-xl bg-calm-sage-50 dark:bg-white/5 border border-calm-sage-300/50 text-calm-sage-700 dark:text-calm-sage-300 text-sm">
            <PartyPopper size={16} />
            You&apos;ve answered every question — feel free to revisit any of them below.
          </div>
        )}

        <div>
          <span className="text-xs uppercase tracking-wide text-calm-sage-600 dark:text-calm-sage-300 font-semibold">
            {question.category_label}
          </span>
          <h1 className="text-2xl md:text-3xl font-semibold mt-1 leading-snug">
            {question.text}
          </h1>
        </div>

        {/* With no recordings yet, the recorder IS the screen — asking a
            producer to click "add" against an empty list would be a step
            with only one possible answer. Once something is recorded, the
            takes come first and recording another is a deliberate choice. */}
        {showRecorder ? (
          <VideoRecorder
            key={`${question.id}-${recordings.length}`}
            sessionId={state.session.id}
            questionIndex={currentIndex}
            questionText={question.text}
            onAccepted={handleAccepted}
            onCancel={recordings.length > 0 ? () => setAddingTake(false) : undefined}
          />
        ) : (
          <RecordingList recordings={recordings} onDeleted={load} />
        )}

        {/* Uploading is offered in BOTH states — on an empty question it's
            an alternative to recording, and beside existing takes it's
            another way to add one. Recording is the primary action, so it
            stays the bigger button. */}
        <div className="flex flex-wrap items-center gap-3">
          {!showRecorder && (
            <button onClick={() => setAddingTake(true)} className="calm-btn-secondary">
              <Plus size={16} />
              {recordings.length === 1 ? 'Add another answer' : 'Add another take'}
            </button>
          )}
          <SegmentUpload
            sessionId={state.session.id}
            questionIndex={currentIndex}
            questionText={question.text}
            onAccepted={handleAccepted}
          />
        </div>

        <div className="flex items-center justify-between pt-2">
          <button
            onClick={() => goTo(currentIndex - 1)}
            disabled={isFirst}
            className="calm-btn-secondary"
          >
            <ChevronLeft size={16} />
            Previous question
          </button>
          <button
            onClick={() => goTo(currentIndex + 1)}
            disabled={isLast}
            className="calm-btn-secondary"
          >
            Next question
            <ChevronRight size={16} />
          </button>
        </div>
      </main>
    </div>
  )
}
