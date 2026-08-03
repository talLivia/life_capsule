'use client'

import { useCallback, useEffect, useState } from 'react'
import Link from 'next/link'
import { ChevronRight, Feather, Gift, Loader2, PartyPopper, Plus, ShieldOff } from 'lucide-react'
import { EntityConfirmModal } from '@/components/record/EntityConfirmModal'
import { GateStep } from '@/components/record/GateStep'
import { InterviewAccordion } from '@/components/record/InterviewAccordion'
import { RecordingList } from '@/components/record/RecordingList'
import { SegmentUpload } from '@/components/record/SegmentUpload'
import { VideoRecorder } from '@/components/record/VideoRecorder'
import { api } from '@/lib/api'
import { useInterviewFlow } from '@/lib/useInterviewFlow'
import { useStore } from '@/store/useStore'
import type { RawSegment } from '@/lib/types'

/**
 * The story-recording interview, as an in-shell view.
 *
 * Rebuilt around the category accordion (docs/INTERVIEW_RESTRUCTURE.md step 5).
 * What changed from the linear flow it replaces:
 *
 *   * There is no Next button. Finishing a recording refetches the flow, and
 *     the server's recomputed position IS the advance — so the panel cannot
 *     disagree with the backend about where the producer is.
 *   * Progress is per category, and shows nothing at all until the category
 *     is settled (§8.4) — no invented denominator.
 *   * Screening questions render as their own kind of step, without a camera.
 *
 * Position, completeness and reachability all come from the server. This file
 * decides layout and nothing else.
 */

/** Takes for the question on screen. The flow knows HOW MANY takes exist, but
 *  the player needs the rows themselves, which only the segments list has. */
function useTakesFor(sessionId: string | undefined, questionId: string | undefined) {
  const [takes, setTakes] = useState<RawSegment[]>([])

  const load = useCallback(async () => {
    if (!sessionId || !questionId) {
      setTakes([])
      return
    }
    try {
      const segments: RawSegment[] = await api.listSessionSegments(sessionId)
      setTakes(segments.filter(s => s.question_id === questionId))
    } catch {
      setTakes([])
    }
  }, [sessionId, questionId])

  useEffect(() => {
    load()
  }, [load])

  return { takes, reloadTakes: load }
}

export function RecordPanel() {
  const { user } = useStore()
  const isProducer = user?.role === 'producer'

  const {
    flow, loading, error, reload,
    openCategory, viewingStep, isReviewing, progressLabel,
    openCategoryId, setOpenCategory, selectStep, goBack, canGoBack,
    answerGate, answering, onRecordingAccepted,
  } = useInterviewFlow()

  const questionId = viewingStep?.kind === 'question' ? viewingStep.id : undefined
  const { takes, reloadTakes } = useTakesFor(flow?.interview_session_id, questionId)

  // Opening the recorder on a question that ALREADY has takes. Cleared when
  // the step changes, so navigating back lands on the takes rather than a
  // live camera the producer did not ask for.
  const [addingTake, setAddingTake] = useState(false)
  useEffect(() => {
    setAddingTake(false)
  }, [viewingStep?.id])

  const handleAccepted = async () => {
    setAddingTake(false)
    await reloadTakes()
    await onRecordingAccepted()
  }

  if (!isProducer) {
    return (
      <div className="flex items-center justify-center py-24 px-6">
        <div className="max-w-sm text-center flex flex-col items-center gap-4">
          <ShieldOff size={28} className="text-gray-500" />
          <h1 className="text-lg font-bold text-white">This section is for the account owner</h1>
          <p className="text-sm text-gray-400">
            Recording is only available to the producer account that owns this story archive.
          </p>
        </div>
      </div>
    )
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center py-24">
        <Loader2 size={26} className="animate-spin text-primary-400" />
      </div>
    )
  }

  if (error || !flow) {
    return (
      <div className="flex items-center justify-center py-24 px-6">
        <div className="max-w-sm text-center flex flex-col items-center gap-4">
          <p className="text-sm text-gray-300">{error || 'Something went wrong'}</p>
          <button onClick={reload} className="btn-primary">Try again</button>
        </div>
      </div>
    )
  }

  if (flow.complete) {
    return (
      <div className="animate-fade-in">
        <EntityConfirmModal />
        <div className="flex items-center justify-center py-24 px-6">
          <div className="max-w-md text-center flex flex-col items-center gap-5">
            <PartyPopper size={32} className="text-primary-400" />
            <div>
              <h1 className="text-2xl font-black gradient-text mb-2">
                You&apos;ve answered every question
              </h1>
              <p className="text-sm text-gray-400">
                Your story is saved. Invite a family member from Settings so they can talk
                with it, or reopen any category below to review or re-record an answer.
              </p>
            </div>
            <Link href="/" className="btn-primary justify-center">
              <Gift size={16} />
              Go invite family
            </Link>
          </div>
        </div>
        <div className="max-w-md mx-auto px-6 pb-16">
          <InterviewAccordion
            categories={flow.categories}
            openCategoryId={openCategoryId}
            viewingStepId={viewingStep?.id ?? null}
            onOpenCategory={setOpenCategory}
            onSelectStep={selectStep}
          />
        </div>
      </div>
    )
  }

  const showRecorder = takes.length === 0 || addingTake

  return (
    <div className="animate-fade-in">
      <EntityConfirmModal />

      <header className="max-w-6xl mx-auto px-6 pt-10 pb-5">
        <div className="flex items-center gap-2 text-primary-400">
          <Feather size={16} />
          <span className="text-sm font-medium">Your Story</span>
        </div>
      </header>

      <div className="max-w-6xl mx-auto px-6 pb-16 grid grid-cols-1 lg:grid-cols-5 gap-6 items-start">
        {/* ── The step being answered ─────────────────────────────────── */}
        <main className="lg:col-span-3 flex flex-col gap-5 order-2 lg:order-1">
          {viewingStep?.kind === 'gate' ? (
            <GateStep
              step={viewingStep}
              onAnswer={value => answerGate(viewingStep.id, value)}
              answering={answering}
            />
          ) : viewingStep ? (
            <>
              <div dir="rtl">
                <div className="flex items-center gap-2 flex-wrap">
                  <span className="text-xs uppercase tracking-wide text-primary-400 font-semibold">
                    {openCategory?.label}
                  </span>
                  {/* No counter at all until the category is settled — the
                      total genuinely is not knowable before then (§8.4). */}
                  {progressLabel && (
                    <span className="text-xs text-gray-500">· {progressLabel}</span>
                  )}
                  {isReviewing && (
                    <span className="text-[11px] px-2 py-0.5 rounded-full bg-white/8 text-gray-400">
                      צפייה בתשובה קודמת
                    </span>
                  )}
                </div>
                <h1 className="text-2xl md:text-3xl font-bold text-white mt-1.5 leading-snug">
                  {viewingStep.text}
                </h1>
              </div>

              {showRecorder ? (
                <VideoRecorder
                  key={`${viewingStep.id}-${takes.length}`}
                  sessionId={flow.interview_session_id}
                  questionIndex={openCategory?.steps.findIndex(s => s.id === viewingStep.id) ?? 0}
                  questionId={viewingStep.id}
                  questionText={viewingStep.text}
                  onAccepted={handleAccepted}
                  onCancel={takes.length > 0 ? () => setAddingTake(false) : undefined}
                />
              ) : (
                <RecordingList recordings={takes} onDeleted={handleAccepted} />
              )}

              <div className="flex flex-wrap items-center gap-3">
                {!showRecorder && (
                  <button onClick={() => setAddingTake(true)} className="btn-secondary">
                    <Plus size={16} />
                    {takes.length === 1 ? 'Add another answer' : 'Add another take'}
                  </button>
                )}
                <SegmentUpload
                  sessionId={flow.interview_session_id}
                  questionIndex={openCategory?.steps.findIndex(s => s.id === viewingStep.id) ?? 0}
                  questionId={viewingStep.id}
                  questionText={viewingStep.text}
                  onAccepted={handleAccepted}
                />
              </div>
            </>
          ) : null}

          {/* Back only. There is no Next: finishing a recording advances the
              flow by itself, and jumping ahead is not offered here or
              accepted by the server. */}
          <div className="flex items-center pt-1" dir="rtl">
            <button onClick={goBack} disabled={!canGoBack} className="btn-secondary">
              <ChevronRight size={16} />
              לשאלה הקודמת
            </button>
          </div>
        </main>

        {/* ── Categories ──────────────────────────────────────────────── */}
        <aside className="lg:col-span-2 order-1 lg:order-2 lg:sticky lg:top-6">
          <InterviewAccordion
            categories={flow.categories}
            openCategoryId={openCategoryId}
            viewingStepId={viewingStep?.id ?? null}
            onOpenCategory={setOpenCategory}
            onSelectStep={selectStep}
          />
          {flow.free_navigation && (
            <p className="text-[11px] text-gray-500 mt-3 px-1" dir="rtl">
              ניווט חופשי פעיל — אפשר לפתוח כל קטגוריה.
            </p>
          )}
        </aside>
      </div>
    </div>
  )
}
