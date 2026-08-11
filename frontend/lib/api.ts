import axios, { AxiosError, InternalAxiosRequestConfig } from 'axios'
import { isJwtExpired } from './jwt'
import type { EntityBatchAnswer } from './types'

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

  updateProfile: async (data: { email?: string; username?: string; full_name?: string; password?: string; chat_mode?: 'avatar' | 'video_clips' | 'video_clips_v2'; free_navigation?: boolean }) => {
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
    /** Stable id from interview_questions.json. question_index is positional
     *  and moves when the question set is edited; this is what the timeline
     *  groups life periods by. Optional so an upload outside the guided set
     *  still ingests. */
    question_id?: string
    question_asked: string
    video_key: string
  }) => {
    const response = await apiClient.post('/api/v1/interview/segments/ingest', params)
    return response.data
  },

  /** Remove one relation the archive got wrong. The other half of "nothing is
   *  auto-applied": confirming is only a real decision if undoing is possible. */
  /** Delete every recording and everything derived from it. Irreversible.
   *  The account, avatars, voice samples and the self-entity survive. */
  resetArchive: async () => {
    const response = await apiClient.post('/api/v1/interview/archive/reset')
    return response.data
  },

  deleteRelation: async (relationId: string) => {
    await apiClient.delete(`/api/v1/interview/relations/${relationId}`)
  },

  listSessionSegments: async (sessionId: string) => {
    const response = await apiClient.get(`/api/v1/interview/segments/session/${sessionId}`)
    return response.data
  },

  getTimeline: async () => {
    const response = await apiClient.get('/api/v1/entities/timeline')
    return response.data
  },

  /** Set how one person is related to another, from the tree.
   *  Replaces whatever contradicts it; returns what was replaced. */
  setEntityRelation: async (
    entityId: string,
    body: {
      other_entity_id: string
      relation_type: string
      direction?: 'outgoing' | 'incoming'
      /** For aunt_uncle/grandparent: which of the other end's parents they
       *  attach to — the manual twin of the questionnaire's side question. */
      side_parent_id?: string
    },
  ) => {
    const response = await apiClient.post(`/api/v1/entities/${entityId}/relations`, body)
    return response.data
  },

  /** Remove ONE recorded relation from a person's card — the way out for an
   *  edge that is simply wrong, with nothing true to put in its place. */
  deleteEntityRelation: async (entityId: string, relationId: string) => {
    const response = await apiClient.delete(
      `/api/v1/entities/${entityId}/relations/${relationId}`,
    )
    return response.data
  },

  /** The relation vocabulary, from the relation_types TABLE — so adding a
   *  type is a data change and the picker cannot offer one that would be
   *  refused. */
  getRelationTypes: async () => {
    const response = await apiClient.get('/api/v1/entities/relation-types')
    return response.data
  },

  getFamilyTree: async () => {
    const response = await apiClient.get('/api/v1/entities/tree')
    return response.data
  },

  /** The recordings that mention one person — clicking a name in the tree.
   *  Shared with the timeline's sub-bubbles so the two cannot drift. */
  getEntityMoments: async (entityId: string) => {
    const response = await apiClient.get(`/api/v1/entities/${entityId}/moments`)
    return response.data
  },

  getInterviewFlow: async () => {
    const response = await apiClient.get('/api/v1/interview/flow')
    return response.data
  },

  /** Answer a screening/branching question. Returns the WHOLE updated flow —
   *  answering can reveal a branch, complete a category and move the current
   *  position at once, so re-fetching separately would show a stale frame. */
  answerGate: async (gateId: string, value: string) => {
    const response = await apiClient.post('/api/v1/interview/flow/gate', {
      gate_id: gateId,
      value,
    })
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

  /** The question spoken aloud, in the producer's own recording language.
   *  A blob rather than a URL so the request carries the auth header like
   *  every other call — an <audio src> would go out unauthenticated. */
  getQuestionAudio: async (questionId: string): Promise<Blob> => {
    const response = await apiClient.get(
      `/api/v1/interview/questions/${encodeURIComponent(questionId)}/audio`,
      { responseType: 'blob' },
    )
    return response.data
  },

  // Answers EVERY question for one recording in a single call, so the
  // pipeline resumes once and runs to completion. Replaced confirmEntity,
  // which answered one name and left the graph to pause again with the next.
  confirmEntities: async (segmentId: string, answers: EntityBatchAnswer) => {
    const response = await apiClient.post(
      `/api/v1/interview/segments/${segmentId}/confirm-entities`,
      answers,
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

  // ── Photos on entities and periods (docs/MEDIA_GALLERY.md) ──────────────
  // One flow everywhere: presign → PUT → create the row. `uploadPhoto` below
  // composes the three; these exist separately so the pieces stay testable.

  presignMediaUpload: async (owner: { entity_id?: string; category?: string }, contentType: string) => {
    const response = await apiClient.post('/api/v1/media/presign', {
      ...owner,
      content_type: contentType,
    })
    return response.data
  },

  createMediaAsset: async (params: {
    storage_key: string
    caption?: string
    taken_year?: number
    /** Entity photos: make this upload the face, demoting the current one.
     *  What clicking the portrait circle means. */
    make_primary?: boolean
  }) => {
    const response = await apiClient.post('/api/v1/media', params)
    return response.data
  },

  listMedia: async (owner: { entity_id?: string; category?: string }) => {
    const response = await apiClient.get('/api/v1/media', { params: owner })
    return response.data
  },

  deleteMediaAsset: async (mediaId: string) => {
    await apiClient.delete(`/api/v1/media/${mediaId}`)
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
 * The whole photo upload, in order: presign → PUT the bytes → write the row.
 * Reuses the segment upload's PUT mechanics (same presigned-vs-local-dev
 * handling, same no-token-to-storage rule) — photos are not a second kind of
 * upload any more than uploaded videos are a second kind of recording.
 *
 * The file's own MIME type travels as-is; the server is the authority on
 * what is accepted and its rejections carry the message worth showing
 * (HEIC gets told what to do, not just "unsupported").
 */
export async function uploadPhoto(
  owner: { entity_id?: string; category?: string },
  file: File,
  options?: { makePrimary?: boolean },
): Promise<import('./types').MediaAsset> {
  const contentType = file.type || 'image/jpeg'
  const presign = await api.presignMediaUpload(owner, contentType)
  await uploadSegmentBlob(presign.upload_url, file, presign.content_type)
  return api.createMediaAsset({
    storage_key: presign.storage_key,
    make_primary: options?.makePrimary,
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
