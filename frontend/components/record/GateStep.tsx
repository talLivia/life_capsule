'use client'

import { Loader2 } from 'lucide-react'
import type { FlowStep } from '@/lib/types'

/**
 * A screening/branching question — a distinct, simple prompt, not an
 * open-recording question. Nothing is filmed here; the producer picks an
 * option and the flow moves.
 *
 * Options are rendered FROM THE DATA, one control each. There is no yes/no
 * pair anywhere in this file: the relationships status question has three
 * options today and a future screening question may have four, and neither
 * should require touching a component.
 *
 * Visually lighter than a recording question on purpose — the producer should
 * be able to tell at a glance that this one does not need the camera.
 */

interface GateStepProps {
  step: FlowStep
  onAnswer: (value: string) => void
  answering: boolean
}

export function GateStep({ step, onAnswer, answering }: GateStepProps) {
  const options = step.options ?? []

  return (
    <div className="glass-card p-6 flex flex-col gap-5" dir="rtl">
      <div>
        <span className="text-xs uppercase tracking-wide text-primary-400 font-semibold">
          שאלת סינון
        </span>
        <h2 className="text-xl md:text-2xl font-bold text-white mt-1.5 leading-snug">
          {step.text}
        </h2>
        <p className="text-xs text-gray-500 mt-2">
          אין צורך בהקלטה — בחר תשובה כדי להמשיך
        </p>
      </div>

      <div className="flex flex-wrap gap-2.5">
        {options.map(option => {
          const selected = step.answer === option.value
          return (
            <button
              key={option.value}
              type="button"
              onClick={() => onAnswer(option.value)}
              disabled={answering}
              className={selected ? 'btn-primary' : 'btn-secondary'}
            >
              {answering && selected && <Loader2 size={15} className="animate-spin" />}
              {option.label}
            </button>
          )
        })}
      </div>

      {/* An answered gate stays changeable — the producer may have picked
          wrong. Changing it never deletes anything already recorded. */}
      {step.answer && (
        <p className="text-xs text-gray-500">
          אפשר לשנות את התשובה — הקלטות שכבר נשמרו לא יימחקו.
        </p>
      )}
    </div>
  )
}
