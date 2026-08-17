'use client'

import { useCallback, useEffect, useState } from 'react'
import Link from 'next/link'
import {
  Clapperboard,
  Feather,
  Gift,
  Loader2,
  PanelRightClose,
  PanelRightOpen,
  PartyPopper,
  Play,
  Plus,
  ShieldOff,
  SkipForward,
} from 'lucide-react'
import { GateStep } from '@/components/record/GateStep'
import { InterviewAccordion } from '@/components/record/InterviewAccordion'
import { PresenterVideo } from '@/components/record/PresenterVideo'
import { ReadAloudButton } from '@/components/record/ReadAloudButton'
import { RecordingList } from '@/components/record/RecordingList'
import { CategoryPhotoZone } from '@/components/media/CategoryPhotoZone'
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
 *  the player needs the rows themselves, which only the segments list has.
 *  `takesLoaded` marks the first fetch settling — the presenter video must
 *  not auto-play on the takes==[] that merely means "not fetched yet". */
function useTakesFor(sessionId: string | undefined, questionId: string | undefined) {
  const [takes, setTakes] = useState<RawSegment[]>([])
  const [takesLoaded, setTakesLoaded] = useState(false)

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
    } finally {
      setTakesLoaded(true)
    }
  }, [sessionId, questionId])

  useEffect(() => {
    setTakesLoaded(false)
    load()
  }, [load])

  return { takes, takesLoaded, reloadTakes: load }
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
  const { takes, takesLoaded, reloadTakes } = useTakesFor(flow?.interview_session_id, questionId)

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

  // ── Presenter videos (docs/PRESENTER_VIDEOS_PLAN.md) ─────────────────
  // One presigned URL per frozen question id, fetched once per page load.
  // A null map (endpoint down) degrades to today's TTS Read-aloud flow.
  const [presenter, setPresenter] = useState<{
    intro: string | null
    questions: Record<string, string>
  } | null>(null)
  useEffect(() => {
    if (!isProducer) return
    api.getPresenterVideos().then(setPresenter).catch(() => setPresenter(null))
  }, [isProducer])

  const presenterUrl = questionId ? presenter?.questions?.[questionId] : undefined
  // `watchingQuestion` is the mode switch for the shared slot: presenter
  // video vs recorder/takes. `presenterFailed` is per-question — a video
  // that cannot play must never block recording, so failure falls back to
  // the Read-aloud flow (the plan's §3 failure path).
  const [watchingQuestion, setWatchingQuestion] = useState(false)
  const [presenterFailed, setPresenterFailed] = useState(false)
  useEffect(() => {
    setWatchingQuestion(false)
    setPresenterFailed(false)
  }, [viewingStep?.id])
  // Auto-play only once takes have LOADED and are empty — a question that
  // already has answers keeps the takes list as its default view — and
  // never yank the slot away from a camera that is already capturing.
  useEffect(() => {
    if (
      takesLoaded &&
      takes.length === 0 &&
      presenterUrl &&
      !presenterFailed &&
      !capturing &&
      viewingStep?.kind === 'question'
    ) {
      setWatchingQuestion(true)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [takesLoaded, takes.length, presenterUrl, presenterFailed, viewingStep?.id])

  // The Intro walkthrough — never a flow step, never sent anywhere: a
  // client-side explainer that auto-opens once (localStorage flag) and is
  // one click away after.
  const introUrl = presenter?.intro ?? null
  const [introOpen, setIntroOpen] = useState(false)
  const [introFailed, setIntroFailed] = useState(false)
  useEffect(() => {
    if (!introUrl || introFailed) return
    try {
      if (!window.localStorage.getItem('presenter_intro_seen')) setIntroOpen(true)
    } catch {
      /* storage unavailable — the card button still opens it */
    }
  }, [introUrl, introFailed])
  const closeIntro = () => {
    setIntroOpen(false)
    try {
      window.localStorage.setItem('presenter_intro_seen', '1')
    } catch {
      /* fine — it will auto-open again next visit */
    }
  }

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
          <ShieldOff size={28} className="text-muted2" />
          <h1 className="text-lg font-bold text-ink">This section is for the account owner</h1>
          <p className="text-sm text-muted">
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
          <p className="text-sm text-ink-soft">{error || 'Something went wrong'}</p>
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
              <p className="text-sm text-muted">
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
            className="hidden lg:flex items-center gap-1.5 text-xs text-muted hover:text-ink transition-colors"
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
            <span className="text-xs text-muted">
              {overall.sectionsDone} of {overall.sectionsTotal} sections finished
            </span>
            <span className="text-xs text-muted2">
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
          {introOpen && introUrl ? (
            // The Intro walkthrough takes over the shared slot; nothing
            // else about the flow moves — closing it lands exactly where
            // the producer already was.
            <div>
              <h1 className="text-2xl md:text-3xl font-bold text-ink leading-snug">
                How recording works
              </h1>
              <div className="mt-4">
                <PresenterVideo
                  src={introUrl}
                  onFinished={closeIntro}
                  onUnavailable={() => {
                    setIntroFailed(true)
                    setIntroOpen(false)
                  }}
                  finishLabel="Close and start recording"
                />
              </div>
            </div>
          ) : viewingStep?.kind === 'gate' ? (
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
                    <span className="text-xs text-muted2">· {progressLabel}</span>
                  )}
                  {isReviewing && (
                    <span className="text-[11px] px-2 py-0.5 rounded-full bg-veil text-muted">
                      Reviewing an earlier answer
                    </span>
                  )}
                </div>
                <h1 dir="auto" className="text-2xl md:text-3xl font-bold text-ink mt-1.5 leading-snug">
                  {viewingStep.text}
                </h1>
                {/* Read aloud sits with the QUESTION, because that is what it
                    reads — and it is absent, not merely disabled, while the
                    camera is capturing. A button that cannot be pressed
                    mid-answer is a stronger guarantee than one that must
                    remember not to be. */}
                {!capturing && (
                  <div className="mt-2 flex items-center gap-4">
                    {/* TTS Read-aloud survives only where the presenter
                        video can't play — the plan's failure path, not a
                        parallel control. */}
                    {(!presenterUrl || presenterFailed) && (
                      <ReadAloudButton
                        key={viewingStep.id}
                        questionId={viewingStep.id}
                        onPlayingChange={setReadingAloud}
                      />
                    )}
                    {presenterUrl && !presenterFailed && !watchingQuestion && (
                      <button
                        type="button"
                        onClick={() => setWatchingQuestion(true)}
                        className="inline-flex items-center gap-1.5 text-xs text-muted hover:text-ink transition-colors"
                      >
                        <Play size={14} />
                        Watch the question again
                      </button>
                    )}
                    {/* Nothing is written and nothing is retired — the
                        question stays unanswered and one click away in the
                        accordion. Worded as "not now" because that is exactly
                        what it does; "skip" alone reads like a decision that
                        removes the question for good. */}
                    {canSkip && (
                      <button
                        type="button"
                        onClick={skip}
                        className="inline-flex items-center gap-1.5 text-xs text-muted hover:text-ink transition-colors"
                      >
                        <SkipForward size={14} />
                        Nothing to say — next question
                      </button>
                    )}
                  </div>
                )}
              </div>

              {watchingQuestion && presenterUrl && !presenterFailed ? (
                // The presenter reads the question in the recorder's slot;
                // the camera exists only after this unmounts, so the
                // presenter's voice can never land in a take.
                <PresenterVideo
                  key={viewingStep.id}
                  src={presenterUrl}
                  onFinished={() => setWatchingQuestion(false)}
                  onUnavailable={() => {
                    setPresenterFailed(true)
                    setWatchingQuestion(false)
                  }}
                />
              ) : showRecorder ? (
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

              {!watchingQuestion && (
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
              )}
            </>
          ) : null}

          {/* ── Photos for the open chapter (MEDIA_GALLERY.md §9.6) ─────
              One persistent zone per CATEGORY, in a fixed spot below the
              recording area — the same zone whichever question in the
              chapter is being answered, because the photos belong to the
              chapter as a whole, never to a take. Keyed by category so
              moving between its questions neither moves nor reloads it. */}
          {openCategoryId && (
            <CategoryPhotoZone key={openCategoryId} categoryId={openCategoryId} />
          )}

          {/* ── What each control does ────────────────────────────────
              Always visible, never a tooltip or a first-run tour. The
              producer this is built for may use the screen once a month,
              so anything that explains itself only on hover, or only the
              first time, explains itself to nobody. It costs a few lines
              of grey text and removes the need to remember. */}
          {viewingStep?.kind !== 'gate' && (
            <dl className="text-[11px] text-muted2 leading-relaxed border-t border-edge pt-3 flex flex-col gap-1">
              <div className="flex gap-2">
                <dt className="text-muted shrink-0">The question video</dt>
                <dd>plays first; when it ends (or you skip), the recorder appears in its place.</dd>
              </div>
              <div className="flex gap-2">
                <dt className="text-muted shrink-0">Record</dt>
                <dd>answer in your own words. You can watch it back before saving.</dd>
              </div>
              <div className="flex gap-2">
                <dt className="text-muted shrink-0">Add another take</dt>
                <dd>
                  say more without losing what you already said — answers are kept, never
                  replaced.
                </dd>
              </div>
              <div className="flex gap-2">
                <dt className="text-muted shrink-0">Extracted from this</dt>
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
          {/* The Intro walkthrough — a one-time explainer, replayable on
              demand. Purely client-side: never a step, never counted. */}
          {introUrl && !introFailed && (
            <button
              type="button"
              onClick={() => setIntroOpen(true)}
              className="w-full mb-3 flex items-center gap-2 px-3 py-2.5 rounded-xl
                         bg-surface-800/60 border border-edge hover:border-primary-500/40
                         hover:bg-surface-700/60 transition-all text-left"
            >
              <Clapperboard size={15} className="text-primary-400 shrink-0" />
              <span className="text-xs font-medium text-ink-soft">How recording works</span>
            </button>
          )}
          <InterviewAccordion
            freeNavigation={flow.free_navigation}
            categories={flow.categories}
            openCategoryId={openCategoryId}
            viewingStepId={viewingStep?.id ?? null}
            onOpenCategory={setOpenCategory}
            onSelectStep={selectStep}
          />
          {flow.free_navigation && (
            <p className="text-[11px] text-muted2 mt-3 px-1">
              Free navigation is on — you can open any category.
            </p>
          )}
        </aside>
      </div>
    </div>
  )
}
