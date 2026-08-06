'use client'

import { useCallback, useEffect, useState } from 'react'
import Link from 'next/link'
import {
  Feather,
  Gift,
  Loader2,
  PanelRightClose,
  PanelRightOpen,
  PartyPopper,
  Plus,
  ShieldOff,
  SkipForward,
} from 'lucide-react'
import { GateStep } from '@/components/record/GateStep'
import { InterviewAccordion } from '@/components/record/InterviewAccordion'
import { ReadAloudButton } from '@/components/record/ReadAloudButton'
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
    openCategory, viewingStep, isReviewing, progressLabel, overall,
    openCategoryId, setOpenCategory, selectStep, skip, canSkip,
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

  // The category list, shown by default. Collapsing it leaves one question on
  // screen and nothing else. Not persisted: it is a per-sitting preference,
  // and a producer who returns to a hidden list has no clue where the
  // questions went.
  const [panelOpen, setPanelOpen] = useState(true)

  // The two halves of "the question's voice never reaches the recording".
  // `readingAloud` holds the record button; `capturing` takes the read-aloud
  // control off the screen. Neither depends on the other being got right.
  const [readingAloud, setReadingAloud] = useState(false)
  const [capturing, setCapturing] = useState(false)
  useEffect(() => {
    setReadingAloud(false)
    setCapturing(false)
  }, [viewingStep?.id])

  /**
   * Finishing a recording goes straight to the next question.
   *
   * Nothing opens, nothing has to be dismissed, nothing is waited for. This
   * panel used to own a sequence — extraction screen, then the confirmation
   * popup, then the extraction screen again — which existed to hold the
   * producer in place until the questions were ready. Both screens are now
   * asynchronous: the recording is read on the server and anything it raises
   * appears in the bell, so there is nothing left here to hold them for and
   * the interruption between one question and the next buys nothing.
   *
   * The extraction screen still exists, opened on demand from a recording in
   * `RecordingList`. It is no longer something that appears on its own.
   */
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
            freeNavigation={flow.free_navigation}
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
      <header className="max-w-7xl mx-auto px-6 pt-10 pb-5">
        <div className="flex items-center justify-between gap-3">
          <div className="flex items-center gap-2 text-primary-400">
            <Feather size={16} />
            <span className="text-sm font-medium">Your Story</span>
          </div>
          {/* Desktop only. On narrow screens the list already stacks above
              the question rather than beside it, so there is no column to
              reclaim and the control would just be a second way to scroll. */}
          <button
            type="button"
            onClick={() => setPanelOpen(open => !open)}
            className="hidden lg:flex items-center gap-1.5 text-xs text-gray-400 hover:text-white transition-colors"
            aria-expanded={panelOpen}
          >
            {panelOpen ? <PanelRightClose size={15} /> : <PanelRightOpen size={15} />}
            {panelOpen ? 'Hide questions' : 'Show all questions'}
          </button>
        </div>

        {/* ── How far through ──────────────────────────────────────────
            Sections, not questions. The reachable-question denominator
            moves from 89 to 129 as gates are answered, so a percentage of
            it falls when a producer answers one truthfully. See
            `buildOverallProgress`. The recorded count carries no
            denominator, deliberately. */}
        <div className="mt-4 flex flex-col gap-1.5">
          <div className="flex items-baseline justify-between gap-3">
            <span className="text-xs text-gray-400">
              {overall.sectionsDone} of {overall.sectionsTotal} sections finished
            </span>
            <span className="text-xs text-gray-500">
              {overall.questionsRecorded === 1
                ? '1 question recorded'
                : `${overall.questionsRecorded} questions recorded`}
            </span>
          </div>
          <div
            className="h-1.5 rounded-full bg-surface-800 overflow-hidden"
            role="progressbar"
            aria-valuenow={overall.percent}
            aria-valuemin={0}
            aria-valuemax={100}
            aria-label="Progress through the interview"
          >
            <div
              className="h-full bg-gradient-to-r from-primary-500 to-accent-500 transition-all duration-700"
              style={{ width: `${overall.percent}%` }}
            />
          </div>
        </div>
      </header>

      <div
        className={`max-w-7xl mx-auto px-6 pb-16 grid grid-cols-1 gap-6 items-start ${
          panelOpen ? 'lg:grid-cols-5' : 'lg:grid-cols-1'
        }`}
      >
        {/* ── The step being answered ─────────────────────────────────── */}
        <main
          className={`flex flex-col gap-5 order-2 lg:order-1 ${
            // Collapsed, the question gets the whole width — but centred and
            // capped, because a question line running the full width of a
            // desktop monitor is harder to read, not easier.
            panelOpen ? 'lg:col-span-3' : 'w-full max-w-3xl mx-auto'
          }`}
        >
          {viewingStep?.kind === 'gate' ? (
            <GateStep
              step={viewingStep}
              onAnswer={value => answerGate(viewingStep.id, value)}
              answering={answering}
            />
          ) : viewingStep ? (
            <>
              <div>
                <div className="flex items-center gap-2 flex-wrap">
                  {/* The category label is interview CONTENT (Hebrew), so it
                      picks its own direction; the chrome around it is English. */}
                  <span dir="auto" className="text-xs uppercase tracking-wide text-primary-400 font-semibold">
                    {openCategory?.label}
                  </span>
                  {/* No counter at all until the category is settled — the
                      total genuinely is not knowable before then (§8.4). */}
                  {progressLabel && (
                    <span className="text-xs text-gray-500">· {progressLabel}</span>
                  )}
                  {isReviewing && (
                    <span className="text-[11px] px-2 py-0.5 rounded-full bg-white/8 text-gray-400">
                      Reviewing an earlier answer
                    </span>
                  )}
                </div>
                <h1 dir="auto" className="text-2xl md:text-3xl font-bold text-white mt-1.5 leading-snug">
                  {viewingStep.text}
                </h1>
                {/* Read aloud sits with the QUESTION, because that is what it
                    reads — and it is absent, not merely disabled, while the
                    camera is capturing. A button that cannot be pressed
                    mid-answer is a stronger guarantee than one that must
                    remember not to be. */}
                {!capturing && (
                  <div className="mt-2 flex items-center gap-4">
                    <ReadAloudButton
                      key={viewingStep.id}
                      questionId={viewingStep.id}
                      onPlayingChange={setReadingAloud}
                    />
                    {/* Nothing is written and nothing is retired — the
                        question stays unanswered and one click away in the
                        accordion. Worded as "not now" because that is exactly
                        what it does; "skip" alone reads like a decision that
                        removes the question for good. */}
                    {canSkip && (
                      <button
                        type="button"
                        onClick={skip}
                        className="inline-flex items-center gap-1.5 text-xs text-gray-400 hover:text-white transition-colors"
                      >
                        <SkipForward size={14} />
                        Nothing to say — next question
                      </button>
                    )}
                  </div>
                )}
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
                  recordDisabled={readingAloud}
                  onCapturingChange={setCapturing}
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

          {/* ── What each control does ────────────────────────────────
              Always visible, never a tooltip or a first-run tour. The
              producer this is built for may use the screen once a month,
              so anything that explains itself only on hover, or only the
              first time, explains itself to nobody. It costs a few lines
              of grey text and removes the need to remember. */}
          {viewingStep?.kind !== 'gate' && (
            <dl className="text-[11px] text-gray-500 leading-relaxed border-t border-white/8 pt-3 flex flex-col gap-1">
              <div className="flex gap-2">
                <dt className="text-gray-400 shrink-0">Read aloud</dt>
                <dd>hear the question spoken. Recording waits until it finishes.</dd>
              </div>
              <div className="flex gap-2">
                <dt className="text-gray-400 shrink-0">Record</dt>
                <dd>answer in your own words. You can watch it back before saving.</dd>
              </div>
              <div className="flex gap-2">
                <dt className="text-gray-400 shrink-0">Add another take</dt>
                <dd>
                  say more without losing what you already said — answers are kept, never
                  replaced.
                </dd>
              </div>
              <div className="flex gap-2">
                <dt className="text-gray-400 shrink-0">Extracted from this</dt>
                <dd>what the archive understood, so you can catch a mistake.</dd>
              </div>
            </dl>
          )}

          {/* No Back button. Every earlier question is one click away in
              the accordion, which says WHICH question it goes to — Back
              only ever went to the same place, without saying so. */}
        </main>

        {/* ── Categories ──────────────────────────────────────────────── */}
        {/* Collapsible (§13.1). Hidden, the screen becomes a single question
            and nothing else — which is the whole guided-interview feel, on
            the screen that already exists rather than a second one. It keeps
            its own scroll container when open: with 16 categories the list is
            taller than the viewport, and letting it grow the PAGE means
            scrolling past the whole interview to reach the recorder. */}
        <aside
          className={`order-1 lg:order-2 lg:sticky lg:top-6 lg:max-h-[calc(100vh-6rem)] lg:overflow-y-auto messages-scroll ${
            panelOpen ? 'lg:col-span-2' : 'hidden'
          }`}
        >
          <InterviewAccordion
            freeNavigation={flow.free_navigation}
            categories={flow.categories}
            openCategoryId={openCategoryId}
            viewingStepId={viewingStep?.id ?? null}
            onOpenCategory={setOpenCategory}
            onSelectStep={selectStep}
          />
          {flow.free_navigation && (
            <p className="text-[11px] text-gray-500 mt-3 px-1">
              Free navigation is on — you can open any category.
            </p>
          )}
        </aside>
      </div>
    </div>
  )
}
