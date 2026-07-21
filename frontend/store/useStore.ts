import { create } from 'zustand'
import { persist } from 'zustand/middleware'

interface User {
  id: string
  email: string
  username: string
  full_name?: string
  role?: 'producer' | 'family'
  recording_language?: string
  producer_id?: string | null
}

interface AppState {
  // Auth
  token: string | null
  user: User | null
  setAuth: (token: string, user: User) => void
  // Refresh the cached profile in place (e.g. after redeeming a family
  // invite flips role/producer_id) without needing a new JWT — role/
  // producer_id aren't in the token claims, they're looked up from the DB
  // on every request, so the existing token stays valid.
  updateUser: (user: User) => void
  clearAuth: () => void
  isAuthenticated: () => boolean

  // Theme — UI is dark-first; toggle just for the few light-mode users
  theme: 'light' | 'dark'
  toggleTheme: () => void

  // Session
  activeSessionId: string | null
  selectedAvatarId: string | null
  setActiveSession: (sessionId: string | null) => void
  setSelectedAvatar: (avatarId: string | null) => void

  // WebSocket
  wsConnected: boolean
  setWsConnected: (connected: boolean) => void
}

export const useStore = create<AppState>()(
  persist(
    (set, get) => ({
      // Auth
      token: null,
      user: null,
      setAuth: (token, user) => set({ token, user }),
      updateUser: (user) => set({ user }),
      clearAuth: () =>
        set({
          token: null,
          user: null,
          activeSessionId: null,
          selectedAvatarId: null,
        }),
      isAuthenticated: () => get().token !== null,

      // Theme
      theme: 'dark',
      toggleTheme: () =>
        set((state) => ({
          theme: state.theme === 'light' ? 'dark' : 'light',
        })),

      // Session
      activeSessionId: null,
      selectedAvatarId: null,
      setActiveSession: (sessionId) => set({ activeSessionId: sessionId }),
      setSelectedAvatar: (avatarId) => set({ selectedAvatarId: avatarId }),

      // WebSocket
      wsConnected: false,
      setWsConnected: (connected) => set({ wsConnected: connected }),
    }),
    {
      name: 'avatar-system-storage',
      partialize: (state) => ({
        token: state.token,
        user: state.user,
        theme: state.theme,
        selectedAvatarId: state.selectedAvatarId,
        activeSessionId: state.activeSessionId,
      }),
      // Without this, zustand rehydrates from localStorage as soon as this
      // module is evaluated on the client — before React's first hydration
      // pass — so that first client render already reflects the real
      // token/theme/etc. while the server (no localStorage) rendered with
      // the defaults. Next.js then reports a hydration mismatch on whatever
      // first branches on that state (e.g. app/page.tsx's `!isAuthenticated()
      // && <AuthModal />`). skipHydration defers rehydration to an explicit
      // call — see components/providers/StoreHydration.tsx, which triggers
      // it inside a useEffect (client-only, post-mount) so the first client
      // render matches the server exactly, and the real values apply via a
      // normal post-hydration re-render instead.
      skipHydration: true,
    }
  )
)
