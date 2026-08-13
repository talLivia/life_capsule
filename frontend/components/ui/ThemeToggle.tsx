'use client'

import { useEffect } from 'react'
import { Sun, Moon } from 'lucide-react'
import { useStore } from '@/store/useStore'

/**
 * The light/dark switch — real since the green/cream light theme landed
 * (it was a vestigial base-project control while every surface was
 * hard-coded dark). Theme tokens live in globals.css; this component's
 * only job is keeping the html element's theme classes in sync with the
 * persisted store choice:
 *
 *   dark  → html.dark   (also drives the few remaining `dark:` variants,
 *                        e.g. the calm theme and toast styling)
 *   light → html.light  (activates the green/cream token set)
 *
 * :root's token values ARE the dark theme, so an unhydrated first paint
 * (no class yet) renders dark — the store default — and never flashes
 * light at a dark-mode user. A light-mode user gets one dark frame before
 * rehydration applies their choice; the honest cost of client-side
 * persistence, accepted.
 */
export function ThemeToggle() {
  const { theme, toggleTheme } = useStore()

  useEffect(() => {
    const root = document.documentElement
    root.classList.toggle('dark', theme === 'dark')
    root.classList.toggle('light', theme === 'light')
  }, [theme])

  return (
    <button
      onClick={toggleTheme}
      className="p-2 rounded-lg bg-white/10 hover:bg-white/20 transition-colors"
      aria-label={`Switch to ${theme === 'light' ? 'dark' : 'light'} mode`}
      title={`Switch to ${theme === 'light' ? 'dark' : 'light'} mode`}
    >
      {theme === 'light' ? (
        <Moon size={20} className="text-white" />
      ) : (
        <Sun size={20} className="text-yellow-400" />
      )}
    </button>
  )
}
