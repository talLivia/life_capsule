'use client'

import Image from 'next/image'
import { useEffect, useRef, useState } from 'react'
import { Sparkles, Loader2, Eye, EyeOff, UserPlus, LogIn, User } from 'lucide-react'
import { toast } from 'react-hot-toast'
import { api } from '@/lib/api'
import { useStore } from '@/store/useStore'
import type { ApiError } from '@/lib/types'

const FOCUSABLE_SELECTOR =
  'a[href], button:not([disabled]), textarea:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])'

interface AuthModalProps {
  // Callers with a specific reason to expect a brand-new account (e.g. the
  // /talk invite flow, where a first-time family member almost certainly
  // needs to register, not sign into an existing account) can default to
  // the Register tab and swap the generic copy for something that actually
  // ties the modal to why the visitor is here — otherwise this looks
  // identical to visiting the site with no invite at all.
  defaultTab?: 'login' | 'register'
  title?: string
  description?: string
}

export function AuthModal({ defaultTab = 'login', title, description }: AuthModalProps = {}) {
  const { setAuth } = useStore()
  const [tab, setTab] = useState<'login' | 'register'>(defaultTab)
  const [isLoading, setIsLoading] = useState(false)
  const [showPassword, setShowPassword] = useState(false)
  const dialogRef = useRef<HTMLDivElement>(null)

  // Focus trap: keep Tab cycling inside the modal, and auto-focus the first
  // input when it opens. ESC is intentionally not used to close — there's
  // nothing to fall back to (the auth gate IS the app).
  useEffect(() => {
    const dialog = dialogRef.current
    if (!dialog) return

    const focusables = () =>
      Array.from(dialog.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR))
        .filter(el => !el.hasAttribute('disabled') && el.offsetParent !== null)

    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key !== 'Tab') return
      const els = focusables()
      if (els.length === 0) return
      const first = els[0]
      const last = els[els.length - 1]
      const active = document.activeElement as HTMLElement | null
      if (e.shiftKey) {
        if (active === first || !dialog.contains(active)) {
          e.preventDefault()
          last.focus()
        }
      } else if (active === last) {
        e.preventDefault()
        first.focus()
      }
    }

    // Defer focusing the first input until after the modal mounts
    const t = window.setTimeout(() => {
      const el = focusables()[1] || focusables()[0]
      el?.focus()
    }, 50)

    document.addEventListener('keydown', onKeyDown)
    const prevOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    return () => {
      window.clearTimeout(t)
      document.removeEventListener('keydown', onKeyDown)
      document.body.style.overflow = prevOverflow
    }
  }, [])

  // Login fields
  const [loginEmail, setLoginEmail] = useState('')
  const [loginPassword, setLoginPassword] = useState('')

  // Register fields
  const [regEmail, setRegEmail] = useState('')
  const [regUsername, setRegUsername] = useState('')
  const [regPassword, setRegPassword] = useState('')
  const [regFullName, setRegFullName] = useState('')

  const handleLogin = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!loginEmail || !loginPassword) return
    setIsLoading(true)
    try {
      const data = await api.login(loginEmail, loginPassword)
      const profile = await api.getProfile()
      setAuth(data.access_token, profile)
      toast.success(`Welcome back, ${profile.username}!`, { icon: '👋' })
    } catch (err: unknown) {
      toast.error((err as ApiError)?.response?.data?.detail || 'Invalid credentials')
    } finally {
      setIsLoading(false)
    }
  }

  const handleRegister = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!regEmail || !regUsername || !regPassword) return
    setIsLoading(true)
    try {
      await api.register({ email: regEmail, username: regUsername, password: regPassword, full_name: regFullName })
      // Auto-login after register
      const data = await api.login(regEmail, regPassword)
      const profile = await api.getProfile()
      setAuth(data.access_token, profile)
      toast.success(`Account created! Welcome, ${profile.username}!`, { icon: '🎉' })
    } catch (err: unknown) {
      toast.error((err as ApiError)?.response?.data?.detail || 'Registration failed')
    } finally {
      setIsLoading(false)
    }
  }

  const continueAsGuest = () => {
    // Set a synthetic guest user — backend falls back to "demo-user" when no JWT
    setAuth('guest', { id: 'demo-user', email: 'guest@local', username: 'Guest' })
    toast('Continuing as guest — data may not persist', { icon: '👤' })
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 backdrop-blur-md"
      role="dialog"
      aria-modal="true"
      aria-labelledby="auth-modal-title"
      aria-describedby="auth-modal-desc"
    >
      <div ref={dialogRef} className="w-full max-w-md mx-4 glass-card rounded-2xl p-8 animate-scale-in">
        {/* Logo */}
        <div className="flex flex-col items-center gap-4 mb-8">
          {/* Larger here than in the nav — this is the first screen anyone
              sees, and the mark is doing the introducing. */}
          <Image
            src="/lifecapsule-wordmark.png"
            alt="Life Capsule"
            width={132}
            height={57}
            priority
            className="h-14 w-auto"
          />
          <div className="text-center">
            {/* The heading is only rendered when a caller passes one — the
                wordmark above already says the product's name, and printing
                it twice reads as a mistake. */}
            {title && (
              <h1 id="auth-modal-title" className="text-2xl font-black gradient-text">{title}</h1>
            )}
            <p id="auth-modal-desc" className="text-sm text-muted2 mt-0.5">
              {description ?? 'Sign in to your account'}
            </p>
          </div>
        </div>

        {/* Tab switcher */}
        <div className="flex gap-1 p-1 rounded-xl bg-surface-800/80 border border-edge mb-6">
          {(['login', 'register'] as const).map(t => (
            <button
              key={t}
              onClick={() => setTab(t)}
              className={`flex-1 flex items-center justify-center gap-1.5 py-2 rounded-lg text-sm font-medium transition-all duration-200
                ${tab === t
                  ? 'bg-gradient-to-r from-primary-600/80 to-accent-600/80 text-white shadow-glow-sm'
                  : 'text-muted hover:text-ink'
                }`}
            >
              {t === 'login' ? <><LogIn size={14} /> Sign In</> : <><UserPlus size={14} /> Register</>}
            </button>
          ))}
        </div>

        {/* Login form */}
        {tab === 'login' && (
          <form onSubmit={handleLogin} className="space-y-4">
            <div className="space-y-1.5">
              <label className="text-sm font-medium text-ink-soft">Email</label>
              <input
                type="email"
                value={loginEmail}
                onChange={e => setLoginEmail(e.target.value)}
                className="input-field"
                placeholder="you@example.com"
                autoComplete="email"
                required
              />
            </div>
            <div className="space-y-1.5">
              <label className="text-sm font-medium text-ink-soft">Password</label>
              <div className="relative">
                <input
                  type={showPassword ? 'text' : 'password'}
                  value={loginPassword}
                  onChange={e => setLoginPassword(e.target.value)}
                  className="input-field pr-10"
                  placeholder="••••••••"
                  autoComplete="current-password"
                  required
                />
                <button
                  type="button"
                  onClick={() => setShowPassword(s => !s)}
                  className="absolute right-3 top-1/2 -translate-y-1/2 text-muted2 hover:text-ink-soft"
                >
                  {showPassword ? <EyeOff size={16} /> : <Eye size={16} />}
                </button>
              </div>
            </div>
            <button type="submit" disabled={isLoading} className="btn-primary w-full py-3 rounded-xl mt-2">
              {isLoading ? <Loader2 size={16} className="animate-spin" /> : <LogIn size={16} />}
              Sign In
            </button>
          </form>
        )}

        {/* Register form */}
        {tab === 'register' && (
          <form onSubmit={handleRegister} className="space-y-4">
            <div className="grid grid-cols-2 gap-4">
              <div className="space-y-1.5">
                <label className="text-sm font-medium text-ink-soft">Username</label>
                <input
                  type="text"
                  value={regUsername}
                  onChange={e => setRegUsername(e.target.value)}
                  className="input-field"
                  placeholder="cooluser"
                  required
                />
              </div>
              <div className="space-y-1.5">
                <label className="text-sm font-medium text-ink-soft">Full Name</label>
                <input
                  type="text"
                  value={regFullName}
                  onChange={e => setRegFullName(e.target.value)}
                  className="input-field"
                  placeholder="Optional"
                />
              </div>
            </div>
            <div className="space-y-1.5">
              <label className="text-sm font-medium text-ink-soft">Email</label>
              <input
                type="email"
                value={regEmail}
                onChange={e => setRegEmail(e.target.value)}
                className="input-field"
                placeholder="you@example.com"
                required
              />
            </div>
            <div className="space-y-1.5">
              <label className="text-sm font-medium text-ink-soft">Password</label>
              <div className="relative">
                <input
                  type={showPassword ? 'text' : 'password'}
                  value={regPassword}
                  onChange={e => setRegPassword(e.target.value)}
                  className="input-field pr-10"
                  placeholder="Min 8 characters"
                  minLength={8}
                  required
                />
                <button
                  type="button"
                  onClick={() => setShowPassword(s => !s)}
                  className="absolute right-3 top-1/2 -translate-y-1/2 text-muted2 hover:text-ink-soft"
                >
                  {showPassword ? <EyeOff size={16} /> : <Eye size={16} />}
                </button>
              </div>
            </div>
            <button type="submit" disabled={isLoading} className="btn-primary w-full py-3 rounded-xl mt-2">
              {isLoading ? <Loader2 size={16} className="animate-spin" /> : <UserPlus size={16} />}
              Create Account
            </button>
          </form>
        )}

        {/* Divider */}
        <div className="flex items-center gap-3 my-5">
          <div className="flex-1 h-px bg-veil" />
          <span className="text-xs text-muted2">or</span>
          <div className="flex-1 h-px bg-veil" />
        </div>

        {/* Guest mode */}
        <button
          onClick={continueAsGuest}
          className="btn-secondary w-full py-2.5 rounded-xl text-sm"
        >
          <User size={15} />
          Continue as Guest
        </button>
        <p className="text-xs text-center text-muted2 mt-3">
          Guest data is scoped to this browser session only.
        </p>
      </div>
    </div>
  )
}
