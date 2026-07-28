import axios, { AxiosError, InternalAxiosRequestConfig } from 'axios'
import { isJwtExpired } from './jwt'

const API_URL = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000'

const STORAGE_KEY = 'avatar-system-storage'

function readToken(): string | null {
  if (typeof window === 'undefined') return null
  try {
    const stored = localStorage.getItem(STORAGE_KEY)
    if (!stored) return null
    const { state } = JSON.parse(stored)
    const token = state?.token
    // Treat the synthetic guest token as "no auth" — the backend falls
    // back to demo-user when no Authorization header is present. Same for
    // an expired token: sending it gets rejected anyway (WS auth closes
    // with 4401), so there's no upside to attaching it over just omitting it.
    if (!token || token === 'guest' || isJwtExpired(token)) return null
    return token
  } catch {
    return null
  }
}

// Create axios instance with defaults
const apiClient = axios.create({
  baseURL: API_URL,
  timeout: 30000,
  // Send the httpOnly auth cookie the backend sets on login. Combined with
  // the bearer header (kept for cross-origin API use), this lets the browser
  // authenticate via the XSS-safe cookie. Requires the backend's CORS
  // allow_credentials=true + explicit origin (both already configured).
  withCredentials: true,
  headers: {
    'Content-Type': 'application/json',
  },
})

// Request interceptor — attach auth token
apiClient.interceptors.request.use(
  (config: InternalAxiosRequestConfig) => {
    const token = readToken()
    if (token) {
      config.headers.Authorization = `Bearer ${token}`
    }
    return config
  },
  (error) => Promise.reject(error)
)

// Response interceptor — handle 401 globally
apiClient.interceptors.response.use(
  (response) => response,
  (error: AxiosError) => {
    if (error.response?.status === 401) {
      if (typeof window !== 'undefined') {
        localStorage.removeItem(STORAGE_KEY)
        window.dispatchEvent(new CustomEvent('auth:logout'))
      }
    }
    return Promise.reject(error)
  }
)

export const api = {
  // Expose the token getter so non-axios code (WebSocket) can authenticate
  getToken: readToken,

  // Auth
  register: async (data: { email: string; username: string; password: string; full_name?: string }) => {
    const response = await apiClient.post('/api/v1/users/register', data)
    return response.data
  },

  login: async (email: string, password: string) => {
    const formData = new URLSearchParams()
    formData.append('username', email)
    formData.append('password', password)
    const response = await apiClient.post('/api/v1/users/login', formData, {
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    })
    return response.data
  },

  logout: async () => {
    // Clears the server-set httpOnly cookie. Best-effort — ignore failures.
    try {
      await apiClient.post('/api/v1/users/logout')
    } catch {
      /* network error on logout is non-fatal */
    }
  },

  getProfile: async () => {
    const response = await apiClient.get('/api/v1/users/me')
    return response.data
  },

  updateProfile: async (data: { email?: string; username?: string; full_name?: string; password?: string; chat_mode?: 'avatar' | 'video_clips' | 'video_clips_v2' }) => {
    const response = await apiClient.put('/api/v1/users/me', data)
    return response.data
  },

  // Avatars
  uploadAvatar: async (formData: FormData) => {
    const response = await apiClient.post('/api/v1/avatars/upload', formData, {
      headers: { 'Content-Type': 'multipart/form-data' },
    })
    return response.data
  },

  getAvatars: async () => {
    const response = await apiClient.get('/api/v1/avatars/')
    return response.data
  },

  deleteAvatar: async (avatarId: string) => {
    const response = await apiClient.delete(`/api/v1/avatars/${avatarId}`)
    return response.data
  },

  setAvatarVoice: async (avatarId: string, voiceId: string) => {
    const response = await apiClient.put(`/api/v1/avatars/${avatarId}/voice?voice_id=${encodeURIComponent(voiceId)}`)
    return response.data
  },

  unsetAvatarVoice: async (avatarId: string) => {
    // Empty voice_id query → backend unassigns
    const response = await apiClient.put(`/api/v1/avatars/${avatarId}/voice`)
    return response.data
  },

  setAvatarMetadata: async (avatarId: string, metadata: Record<string, unknown>) => {
    const response = await apiClient.patch(`/api/v1/avatars/${avatarId}/metadata`, metadata)
    return response.data
  },

  renameAvatar: async (avatarId: string, name: string) => {
    const response = await apiClient.patch(`/api/v1/avatars/${avatarId}/name`, { name })
    return response.data
  },

  // Sessions
  createSession: async (avatarId: string) => {
    const response = await apiClient.post('/api/v1/sessions/create', {
      avatar_id: avatarId,
    })
    return response.data
  },

  getSessions: async () => {
    const response = await apiClient.get('/api/v1/sessions/')
    return response.data
  },

  getSession: async (sessionId: string) => {
    const response = await apiClient.get(`/api/v1/sessions/${sessionId}`)
    return response.data
  },

  endSession: async (sessionId: string) => {
    const response = await apiClient.post(`/api/v1/sessions/${sessionId}/end`)
    return response.data
  },

  deleteSession: async (sessionId: string) => {
    const response = await apiClient.delete(`/api/v1/sessions/${sessionId}`)
    return response.data
  },

  exportSession: async (sessionId: string) => {
    const response = await apiClient.get(`/api/v1/sessions/${sessionId}/export`, {
      responseType: 'blob',
    })
    return response.data as Blob
  },

  // Messages
  sendMessage: async (sessionId: string, content: string) => {
    const response = await apiClient.post('/api/v1/messages/send', {
      session_id: sessionId,
      content,
    })
    return response.data
  },

  getMessages: async (sessionId: string) => {
    const response = await apiClient.get(`/api/v1/messages/session/${sessionId}`)
    return response.data
  },

  editMessage: async (messageId: string, content: string) => {
    const response = await apiClient.patch(`/api/v1/messages/${messageId}`, { content })
    return response.data
  },

  deleteMessage: async (messageId: string) => {
    const response = await apiClient.delete(`/api/v1/messages/${messageId}`)
    return response.data
  },

  // Voices
  listVoices: async () => {
    const response = await apiClient.get('/api/v1/voices/')
    return response.data
  },

  cloneVoice: async (audio: Blob, name: string, language: string = 'en') => {
    const formData = new FormData()
    formData.append('audio', audio, 'voice_sample.webm')
    formData.append('name', name)
    formData.append('language', language)
    const response = await apiClient.post('/api/v1/voices/clone', formData, {
      headers: { 'Content-Type': 'multipart/form-data' },
    })
    return response.data
  },

  deleteVoice: async (voiceId: string) => {
    const response = await apiClient.delete(`/api/v1/voices/${voiceId}`)
    return response.data
  },

  // Returns a fully-qualified URL for the preview WAV. Auth-protected, so the
  // caller must fetch it with credentials (the <audio> tag can't send headers,
  // so we read it via fetch and turn the blob into an object URL).
  getVoicePreviewUrl: (voiceId: string) => `${API_URL}/api/v1/voices/${encodeURIComponent(voiceId)}/preview`,

  fetchVoicePreviewBlob: async (voiceId: string) => {
    const response = await apiClient.get(`/api/v1/voices/${voiceId}/preview`, {
      responseType: 'blob',
    })
    return response.data as Blob
  },

  // Conversations
  listConversations: async () => {
    const response = await apiClient.get('/api/v1/conversations/')
    return response.data
  },

  getSessionConversations: async (sessionId: string) => {
    const response = await apiClient.get(`/api/v1/conversations/session/${sessionId}`)
    return response.data
  },

  renameConversation: async (conversationId: string, title: string) => {
    const response = await apiClient.patch(`/api/v1/conversations/${conversationId}/rename`, { title })
    return response.data
  },

  summarizeConversation: async (conversationId: string) => {
    const response = await apiClient.post(`/api/v1/conversations/${conversationId}/summarize`)
    return response.data
  },

  deleteConversation: async (conversationId: string) => {
    const response = await apiClient.delete(`/api/v1/conversations/${conversationId}`)
    return response.data
  },

  synthesizeVoicePreview: async (voiceId: string, text?: string, language?: string) => {
    const form = new FormData()
    if (text) form.append('text', text)
    if (language) form.append('language', language)
    const response = await apiClient.post(
      `/api/v1/voices/${voiceId}/synthesize`,
      form,
      { responseType: 'blob', headers: { 'Content-Type': 'multipart/form-data' } },
    )
    return response.data as Blob
  },

  // Health
  getHealth: async () => {
    const response = await apiClient.get('/health')
    return response.data
  },

  // Interview / /record (Prompt 4)
  getInterviewQuestions: async () => {
    const response = await apiClient.get('/api/v1/interview/questions')
    return response.data
  },

  getInterviewSession: async () => {
    const response = await apiClient.get('/api/v1/interview/session')
    return response.data
  },

  updateInterviewSession: async (sessionId: string, currentQuestionIndex: number) => {
    const response = await apiClient.patch(`/api/v1/interview/session/${sessionId}`, {
      current_question_index: currentQuestionIndex,
    })
    return response.data
  },

  presignSegmentUpload: async (questionIndex: number, contentType: string = 'video/webm') => {
    const response = await apiClient.post('/api/v1/interview/segments/presign', {
      question_index: questionIndex,
      content_type: contentType,
    })
    return response.data
  },

  ingestSegment: async (params: {
    interview_session_id: string
    question_index: number
    question_asked: string
    video_key: string
  }) => {
    const response = await apiClient.post('/api/v1/interview/segments/ingest', params)
    return response.data
  },

  deleteSegment: async (segmentId: string) => {
    await apiClient.delete(`/api/v1/interview/segments/${segmentId}`)
  },

  getSegmentExtraction: async (segmentId: string) => {
    const response = await apiClient.get(`/api/v1/interview/segments/${segmentId}/extraction`)
    return response.data
  },

  getPendingConfirmations: async () => {
    const response = await apiClient.get('/api/v1/interview/segments/pending-confirmations')
    return response.data
  },

  confirmEntity: async (
    segmentId: string,
    params: { entity_name: string; same_as_existing: boolean; candidate_uuid?: string },
  ) => {
    const response = await apiClient.post(
      `/api/v1/interview/segments/${segmentId}/confirm-entity`,
      params,
    )
    return response.data
  },

  // Family access (Prompt 9)
  createFamilyInvite: async () => {
    const response = await apiClient.post('/api/v1/family/invites')
    return response.data
  },

  listFamilyInvites: async () => {
    const response = await apiClient.get('/api/v1/family/invites')
    return response.data
  },

  revokeFamilyInvite: async (inviteId: string) => {
    const response = await apiClient.delete(`/api/v1/family/invites/${inviteId}`)
    return response.data
  },

  redeemFamilyInvite: async (token: string) => {
    const response = await apiClient.post('/api/v1/family/invites/redeem', { token })
    return response.data
  },

  getTalkAvailability: async () => {
    const response = await apiClient.get('/api/v1/family/talk-availability')
    return response.data
  },
}

/**
 * Upload a recorded take to a presigned (or local-dev) PUT URL, reporting
 * progress. Deliberately bypasses the shared axios instance — in production
 * this goes straight to object storage (R2) via a presigned URL, which must
 * NOT receive this app's auth cookie/bearer token. We only attach those when
 * the upload target is our own backend (the local-storage dev fallback,
 * which needs auth like any other endpoint).
 */
export function uploadSegmentBlob(
  uploadUrl: string,
  blob: Blob,
  contentType: string,
  onProgress?: (fraction: number) => void,
): Promise<void> {
  return new Promise((resolve, reject) => {
    let sameOrigin = false
    try {
      sameOrigin = new URL(uploadUrl, API_URL).origin === new URL(API_URL).origin
    } catch {
      /* malformed URL — treat as cross-origin (no credentials) */
    }

    const xhr = new XMLHttpRequest()
    xhr.open('PUT', uploadUrl, true)
    xhr.setRequestHeader('Content-Type', contentType)
    if (sameOrigin) {
      xhr.withCredentials = true
      const token = readToken()
      if (token) xhr.setRequestHeader('Authorization', `Bearer ${token}`)
    }
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable && onProgress) onProgress(e.loaded / e.total)
    }
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) resolve()
      else reject(new Error(`Upload failed with status ${xhr.status}`))
    }
    xhr.onerror = () => reject(new Error('Upload failed (network error)'))
    xhr.send(blob)
  })
}

/**
 * Build a WebSocket URL for a session, appending the JWT as a query parameter
 * (the WebSocket constructor does not let us attach an Authorization header).
 */
export function buildSessionWsUrl(sessionId: string): string {
  const rawUrl = process.env.NEXT_PUBLIC_WS_URL || process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000'
  const wsBase = rawUrl.replace(/^http/, 'ws')
  const token = readToken()
  const path = `${wsBase}/ws/session/${encodeURIComponent(sessionId)}`
  return token ? `${path}?token=${encodeURIComponent(token)}` : path
}

export { apiClient }
