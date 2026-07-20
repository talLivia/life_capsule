'use client'

import { useCallback, useEffect, useState } from 'react'
import Link from 'next/link'
import { ChevronLeft, ChevronRight, Feather, Loader2, ShieldOff, PartyPopper } from 'lucide-react'
import { toast } from 'react-hot-toast'
import { AuthModal } from '@/components/AuthModal'
import { VideoRecorder } from '@/components/record/VideoRecorder'
import { api } from '@/lib/api'
import { useStore } from '@/store/useStore'
import type { ApiError, InterviewSessionState } from '@/lib/types'

export default function RecordPage() {
  const { isAuthenticated, user } = useStore()
  const [state, setState] = useState<InterviewSessionState | null>(null)
  const [currentIndex, setCurrentIndex] = useState(0)
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState<string | null>(null)

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
    if (isAuthenticated() && isProducer) load()
  }, [isAuthenticated, isProducer, load])

  const goTo = async (index: number) => {
    if (!state) return
    setCurrentIndex(index)
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
    // user navigates back here later, then advance to the next question.
    const nextIndex = Math.min(currentIndex + 1, state.questions.length - 1)
    const atEnd = currentIndex === state.questions.length - 1
    try {
      const data: InterviewSessionState = await api.getInterviewSession()
      setState(data)
    } catch {
      /* segment is already saved server-side; a refresh failure here is non-fatal */
    }
    if (!atEnd) {
      goTo(nextIndex)
    } else {
      setCurrentIndex(nextIndex)
    }
  }

  // ── Auth / role gates ──
  if (!isAuthenticated()) {
    return <AuthModal />
  }

  if (!isProducer) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-calm-paper dark:bg-calm-paperDark px-6">
        <div className="max-w-sm text-center flex flex-col items-center gap-4">
          <ShieldOff size={28} className="text-calm-inkmuted dark:text-calm-inkmutedDark" />
          <h1 className="text-lg font-semibold text-calm-ink dark:text-calm-inkDark">
            This page is for the account owner
          </h1>
          <p className="text-sm text-calm-inkmuted dark:text-calm-inkmutedDark">
            Recording is only available to the producer account that owns this story archive.
          </p>
          <Link href="/" className="calm-btn-secondary">
            Back to home
          </Link>
        </div>
      </div>
    )
  }

  if (loading) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-calm-paper dark:bg-calm-paperDark">
        <Loader2 size={26} className="animate-spin text-calm-sage-600" />
      </div>
    )
  }

  if (loadError || !state) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-calm-paper dark:bg-calm-paperDark px-6">
        <div className="max-w-sm text-center flex flex-col items-center gap-4">
          <p className="text-sm text-calm-ink dark:text-calm-inkDark">{loadError || 'Something went wrong'}</p>
          <button onClick={load} className="calm-btn-primary">Try again</button>
        </div>
      </div>
    )
  }

  const total = state.questions.length
  const question = state.questions[currentIndex]
  const existingSegment = state.segments.find(s => s.question_index === currentIndex)
  const isLast = currentIndex === total - 1
  const isFirst = currentIndex === 0
  const answeredCount = state.segments.length
  const interviewComplete = answeredCount >= total

  return (
    <div className="min-h-screen bg-calm-paper dark:bg-calm-paperDark text-calm-ink dark:text-calm-inkDark">
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

        <VideoRecorder
          key={question.id}
          sessionId={state.session.id}
          questionIndex={currentIndex}
          questionText={question.text}
          existingSegment={existingSegment}
          onAccepted={handleAccepted}
        />

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
