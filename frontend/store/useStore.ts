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
    }
  )
)
