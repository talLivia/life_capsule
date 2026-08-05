'use client'

import { useCallback, useEffect, useMemo, useState } from 'react'
import { toast } from 'react-hot-toast'
import { api } from '@/lib/api'
import type { ApiError, FlowCategory, FlowStep, InterviewFlow } from '@/lib/types'

/**
 * The accordion's whole behaviour, with no layout of its own.
 *
 * Server state (position, completeness, reachability) is NOT recomputed here —
 * it is derived once in app/services/interview_flow.py and rendered. The only
 * state this hook owns is what the producer is currently LOOKING at, which is
 * a viewing concern and deliberately separate:
 *
 *   openCategoryId  which accordion section is expanded
 *   viewingStepId   which step is shown in the main area
 *
 * Both default to the server's current position and follow it as it advances.
 * "Back" moves `viewingStepId` alone; it never asks the server to move the
 * cursor, because there is no cursor — going back to re-record something is
 * looking at an earlier step, not undoing progress.
 *
 * ## Forward navigation is not possible from here
 *
 * `selectStep` refuses anything past the category's current step. The server
 * refuses it too (interview_flow.can_record), and that is the enforcement —
 * this is just the UI not offering something that would be rejected.
 */

export interface UseInterviewFlow {
  flow: InterviewFlow | null
  loading: boolean
  error: string | null
  reload: () => Promise<void>

  openCategory: FlowCategory | null
  /** The step being shown. Null only while loading or when everything is done. */
  viewingStep: FlowStep | null
  /** True when looking at an earlier step rather than the live position —
   *  the panel uses it to explain why a completed question is on screen. */
  isReviewing: boolean

  /** Counter text, or null when no honest count exists yet (§8.4). */
  progressLabel: string | null
  /** Progress through the whole interview. See `buildOverallProgress`. */
  overall: OverallProgress

  openCategoryId: string | null
  setOpenCategory: (categoryId: string) => void
  selectStep: (stepId: string) => void
  goBack: () => void
  canGoBack: boolean

  answerGate: (gateId: string, value: string) => Promise<void>
  answering: boolean
  /** Call after a recording is accepted — refetches and auto-advances. */
  onRecordingAccepted: () => Promise<void>
}

export interface OverallProgress {
  /** 0-100, for the bar. */
  percent: number
  sectionsDone: number
  sectionsTotal: number
  /** A bare count with no denominator, deliberately — see below. */
  questionsRecorded: number
}

/**
 * Progress through the whole interview, measured in SECTIONS.
 *
 * ## Why not a percentage of questions — decision 6.A, resolved differently
 *
 * 6.A recommended counting reachable questions, recomputed as gates are
 * answered, on the grounds that 129 is discouraging and wrong once gates rule
 * some out. The first half of that is right and the fix does not work, because
 * a reachable-question denominator MOVES — and mostly upward.
 *
 * Measured against the real question set: 9 gates across 7 of the 16
 * categories, and the reachable count runs from **89 with no gate answered to
 * 129 with every branch opened**. Answering "yes, I served" adds 8 questions;
 * the two `relationships` gates add 15. So a producer at 20 of 89 (22%) who
 * answers a gate truthfully drops to 20 of 97 (21%). The bar goes BACKWARDS as
 * a reward for answering, which is the same discouragement 6.A set out to
 * avoid, arriving by a different route.
 *
 * Sections do not have this problem. There are always exactly 16 regardless of
 * any gate answer — a gate changes how many questions a section holds, never
 * whether the section exists — so the denominator is fixed for the life of the
 * interview and the bar only moves forward. (It can move back if a producer
 * DELETES recordings, which is real progress being undone rather than an
 * artifact of counting.)
 *
 * The question count rides alongside as a bare number with no denominator, for
 * the same reason §8.4 shows no counter until a category is settled: a total
 * that is not knowable must not be implied. "38 recorded" claims nothing about
 * a ceiling; "38 of 89" would claim something false.
 */
function buildOverallProgress(flow: InterviewFlow | null): OverallProgress {
  const categories = flow?.categories ?? []
  const sectionsDone = categories.filter(c => c.complete).length
  const questionsRecorded = categories.reduce((n, c) => n + c.done_count, 0)
  return {
    sectionsDone,
    sectionsTotal: categories.length,
    questionsRecorded,
    percent: categories.length
      ? Math.round((sectionsDone / categories.length) * 100)
      : 0,
  }
}

/**
 * §8.4, as decided: NO counter until the category is settled. Before that the
 * total genuinely depends on an answer the producer has not given, and any
 * number — even "9+" — implies a ceiling that does not exist.
 *
 * The noun follows the step KIND: a screening prompt is a "Step", a real
 * content question is a "Question". Same counter, honest about which is being
 * asked — calling a yes/no screening prompt a "Question" reads wrong.
 *
 * UI chrome is English throughout this panel. The interview CONTENT is Hebrew
 * and stays so; only the labels around it are translated.
 */
function buildProgressLabel(
  category: FlowCategory | null,
  step: FlowStep | null
): string | null {
  if (!category || !step) return null
  if (!category.settled || category.total == null || category.position == null) {
    return null
  }
  const noun = step.kind === 'gate' ? 'Step' : 'Question'
  const index = category.steps.findIndex(s => s.id === step.id)
  const position = index >= 0 ? index + 1 : category.position
  return `${noun} ${position} of ${category.total}`
}

export function useInterviewFlow(): UseInterviewFlow {
  const [flow, setFlow] = useState<InterviewFlow | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [answering, setAnswering] = useState(false)

  // Null means "follow the server". They only hold a value once the producer
  // has deliberately looked somewhere else, so normal forward progress needs
  // no synchronising.
  const [openOverride, setOpenOverride] = useState<string | null>(null)
  const [viewingOverride, setViewingOverride] = useState<string | null>(null)

  const load = useCallback(async () => {
    setError(null)
    try {
      setFlow(await api.getInterviewFlow())
    } catch (err: unknown) {
      const detail = (err as ApiError)?.response?.data?.detail || (err as ApiError)?.message
      setError(detail || 'Could not load the interview')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    load()
  }, [load])

  const openCategoryId = useMemo(() => {
    if (!flow) return null
    if (openOverride) {
      const cat = flow.categories.find(c => c.id === openOverride)
      // An override can go stale — the category may have become unreachable,
      // or the question set may have changed underneath it. Fall back rather
      // than rendering a section the server says is closed.
      if (cat?.reachable) return cat.id
    }
    return flow.current_category_id ?? null
  }, [flow, openOverride])

  const openCategory = useMemo(
    () => flow?.categories.find(c => c.id === openCategoryId) ?? null,
    [flow, openCategoryId]
  )

  const viewingStep = useMemo(() => {
    if (!openCategory) return null
    if (viewingOverride) {
      const step = openCategory.steps.find(s => s.id === viewingOverride)
      if (step) return step
    }
    if (openCategory.current_step_id) {
      return openCategory.steps.find(s => s.id === openCategory.current_step_id) ?? null
    }
    // Category complete: show its last step, so opening a finished section
    // lands somewhere real instead of blank.
    return openCategory.steps[openCategory.steps.length - 1] ?? null
  }, [openCategory, viewingOverride])

  const isReviewing = Boolean(
    openCategory && viewingStep && openCategory.current_step_id !== viewingStep.id
  )

  const setOpenCategory = useCallback(
    (categoryId: string) => {
      const cat = flow?.categories.find(c => c.id === categoryId)
      // Unreachable sections are inert — no expansion, no message. The server
      // already decided this; the click simply does nothing.
      if (!cat?.reachable) return
      setOpenOverride(categoryId)
      setViewingOverride(null)
    },
    [flow]
  )

  const selectStep = useCallback(
    (stepId: string) => {
      if (!openCategory) return
      const target = openCategory.steps.findIndex(s => s.id === stepId)
      if (target < 0) return
      const currentIndex = openCategory.current_step_id
        ? openCategory.steps.findIndex(s => s.id === openCategory.current_step_id)
        : openCategory.steps.length - 1
      // Backwards and sideways only — unless free navigation is on, which
      // means everything is reachable: any category AND any question in it.
      //
      // This is the SECOND guard on the same action; the accordion has one
      // too. Fixing only that one left the click offered and then silently
      // dropped here. And the old comment was wrong about why: can_record
      // checks that the CATEGORY is reachable and nothing about position, so
      // the server would have accepted the recording all along.
      if (target > currentIndex && !flow?.free_navigation) return
      setViewingOverride(stepId)
    },
    [openCategory, flow?.free_navigation]
  )

  const viewingIndex = useMemo(
    () =>
      openCategory && viewingStep
        ? openCategory.steps.findIndex(s => s.id === viewingStep.id)
        : -1,
    [openCategory, viewingStep]
  )

  const canGoBack = viewingIndex > 0

  const goBack = useCallback(() => {
    if (!openCategory || viewingIndex <= 0) return
    setViewingOverride(openCategory.steps[viewingIndex - 1].id)
  }, [openCategory, viewingIndex])

  const answerGate = useCallback(async (gateId: string, value: string) => {
    setAnswering(true)
    try {
      // The response IS the new flow — no second fetch, so there is no frame
      // where the branch has been chosen but not yet shown.
      setFlow(await api.answerGate(gateId, value))
      setViewingOverride(null)
    } catch (err: unknown) {
      const detail = (err as ApiError)?.response?.data?.detail
      toast.error(detail || 'Could not save that answer')
    } finally {
      setAnswering(false)
    }
  }, [])

  const onRecordingAccepted = useCallback(async () => {
    // Refetching is what advances the flow: the recording changed what is
    // done, and the server recomputes position from that. Clearing the
    // override is what makes it AUTO-advance rather than sitting on the
    // question just answered.
    setViewingOverride(null)
    setOpenOverride(null)
    await load()
  }, [load])

  return {
    flow,
    loading,
    error,
    reload: load,
    openCategory,
    viewingStep,
    isReviewing,
    progressLabel: buildProgressLabel(openCategory, viewingStep),
    overall: buildOverallProgress(flow),
    openCategoryId,
    setOpenCategory,
    selectStep,
    goBack,
    canGoBack,
    answerGate,
    answering,
    onRecordingAccepted,
  }
}
