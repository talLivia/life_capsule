'use client'

import { Check, ChevronDown, Lock } from 'lucide-react'
import type { FlowCategory } from '@/lib/types'

/**
 * The category list — one expanded, completed ones collapsed but reopenable,
 * unreached ones inert.
 *
 * "Inert" is literal: an unreachable category has NO click handler at all,
 * rather than a disabled-looking one that still responds. The server decides
 * reachability (`interview_flow.can_record`), and this only reflects it — a
 * category that looks open but is refused on submit would be worse than one
 * that plainly is not offered.
 *
 * Only the OPEN category lists its steps. Showing every category's steps at
 * once would put 129 questions on screen, which is exactly the "running total
 * across everything" the per-category progress rule exists to avoid.
 */

interface InterviewAccordionProps {
  categories: FlowCategory[]
  openCategoryId: string | null
  viewingStepId: string | null
  onOpenCategory: (categoryId: string) => void
  onSelectStep: (stepId: string) => void
}

export function InterviewAccordion({
  categories,
  openCategoryId,
  viewingStepId,
  onOpenCategory,
  onSelectStep,
}: InterviewAccordionProps) {
  return (
    <div className="flex flex-col gap-2" dir="rtl">
      {categories.map(category => {
        const isOpen = category.id === openCategoryId
        const inert = !category.reachable

        return (
          <div
            key={category.id}
            className={`rounded-xl border overflow-hidden transition-colors ${
              isOpen
                ? 'border-primary-500/40 bg-surface-800/60'
                : 'border-white/8 bg-surface-800/30'
            }`}
          >
            <button
              type="button"
              // No handler at all when unreachable — see the component note.
              onClick={inert ? undefined : () => onOpenCategory(category.id)}
              aria-expanded={isOpen}
              aria-disabled={inert}
              className={`w-full flex items-center gap-2.5 px-3.5 py-3 text-right ${
                inert ? 'cursor-default opacity-45' : 'hover:bg-white/5'
              }`}
            >
              <span className="flex-shrink-0">
                {category.complete ? (
                  <Check size={15} className="text-primary-400" />
                ) : inert ? (
                  <Lock size={13} className="text-gray-500" />
                ) : (
                  <span className="block w-[15px] text-center text-xs text-primary-300">•</span>
                )}
              </span>

              <span
                className={`flex-1 text-sm ${
                  category.current
                    ? 'font-semibold text-white'
                    : category.complete
                      ? 'text-gray-300'
                      : 'text-gray-400'
                }`}
              >
                {category.label}
              </span>

              {/* Only a settled category can show a count — before that the
                  total is not knowable and no number is shown at all (§8.4). */}
              {category.settled && category.total != null && (
                <span className="text-[11px] tabular-nums text-gray-500">
                  {category.done_count}/{category.total}
                </span>
              )}

              {!inert && (
                <ChevronDown
                  size={14}
                  className={`text-gray-500 transition-transform ${isOpen ? 'rotate-180' : ''}`}
                />
              )}
            </button>

            {isOpen && category.steps.length > 0 && (
              <ol className="px-2 pb-2 flex flex-col gap-0.5">
                {category.steps.map((step, i) => {
                  const isViewing = step.id === viewingStepId
                  const currentIndex = category.current_step_id
                    ? category.steps.findIndex(s => s.id === category.current_step_id)
                    : category.steps.length - 1
                  // Forward jumps are not offered — the server refuses the
                  // recording anyway, so a clickable step here would only
                  // produce a rejection.
                  const selectable = i <= currentIndex

                  return (
                    <li key={step.id}>
                      <button
                        type="button"
                        onClick={selectable ? () => onSelectStep(step.id) : undefined}
                        className={`w-full flex items-start gap-2 px-2 py-1.5 rounded-lg text-right text-xs leading-relaxed transition-colors ${
                          isViewing
                            ? 'bg-primary-500/15 text-primary-100'
                            : selectable
                              ? 'text-gray-400 hover:bg-white/5'
                              : 'text-gray-600 cursor-default'
                        }`}
                      >
                        <span className="flex-shrink-0 mt-[3px]">
                          {step.done ? (
                            <Check size={11} className="text-primary-400" />
                          ) : (
                            <span className="block w-[11px] text-center">·</span>
                          )}
                        </span>
                        <span className="flex-1 line-clamp-2">{step.text}</span>
                        {step.kind === 'question' && (step.takes ?? 0) > 1 && (
                          <span className="flex-shrink-0 text-[10px] text-gray-500 mt-[2px]">
                            ×{step.takes}
                          </span>
                        )}
                      </button>
                    </li>
                  )
                })}
              </ol>
            )}
          </div>
        )
      })}
    </div>
  )
}
