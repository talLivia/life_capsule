export interface Avatar {
  id: string
  name: string
  status: 'ready' | 'processing' | 'failed' | 'pending'
  thumbnail_url?: string
  image_url?: string
  s3_key?: string
  voice_id?: string | null
  avatar_metadata?: {
    background_color?: string
    animation_style?: string
  }
  created_at?: string
}

export interface ChatMessage {
  id: string
  role: 'user' | 'assistant' | 'system'
  /** Readable text of the turn. For a video answer this is what the clip
   *  SAYS — the URL lives in video_url, so past conversations can be scanned
   *  without opening every clip. */
  content: string
  video_url?: string | null
  created_at: string
}

export interface SessionSummary {
  id: string
  user_id: string
  avatar_id: string
  status: 'active' | 'paused' | 'ended'
  started_at: string
  ended_at?: string | null
}

export type WsMessageType =
  | 'token'
  | 'transcription'
  | 'message'
  | 'video_chunk_start'
  | 'video_chunk'
  | 'video_chunk_end'
  | 'status'
  | 'error'
  | 'pong'
  | 'tts_fallback'
  | 'interrupted'
  | 'video_clip_response'
  | 'video_clip_no_story'

// Discriminated union — each WS event has a well-typed payload so the handler
// can rely on field presence without optional-chaining everywhere.
export type WsMessage =
  | { type: 'token'; token: string }
  | { type: 'transcription'; text: string }
  | { type: 'message'; role: 'assistant'; content: string }
  | { type: 'video_chunk_start'; total_chunks: number }
  | { type: 'video_chunk'; chunk_index: number; total_chunks: number; video_url: string; text: string }
  | { type: 'video_chunk_end'; sent_chunks: number }
  | { type: 'status'; message: string; stage?: string }
  | { type: 'error'; message: string }
  | { type: 'pong' }
  | { type: 'tts_fallback'; engine: string; voice_cloned: boolean; message: string }
  | { type: 'interrupted'; message: string }
  // Original-video-clip chat mode (Prompt 13/14) — a separate contract from
  // the avatar path above, sent in response to a 'video_clip_question'
  // outgoing message (see VideoClipTalkInterface). Never repurposes
  // 'message'/'video_chunk': a clip response is a single finished video, not
  // a streamed sequence of lip-sync chunks.
  // `follow_up` (v2 only, optional) offers to continue with related material
  // that genuinely exists in the archive and hasn't been shown yet. It is
  // CHAT TEXT ONLY — never spoken and never part of the video, which stays
  // the storyteller's verbatim footage. Accepting it re-asks the question
  // through the normal path so it gets the same validation/assembly.
  | {
      type: 'video_clip_response'
      video_url: string
      /** The verbatim words the clip speaks, so the chat can show what was
       *  said next to the player instead of a bare video. Empty for v1,
       *  which has no utterance-unit text to report. */
      text?: string
      uncovered_clauses: string[]
      follow_up?: { question: string } | null
    }
  | { type: 'video_clip_no_story'; message: string }

export interface VoiceApiResponse {
  id: string
  name: string
  language: string
  duration: number
  created_at?: string
}

// Interview / /record (Prompt 4)
export interface InterviewQuestion {
  id: string
  category: string
  category_label: string
  text: string
  index: number
}

export interface InterviewSession {
  id: string
  user_id: string
  status: 'active' | 'completed' | 'abandoned'
  current_question_index: number
  created_at: string
  updated_at?: string | null
}

export interface RawSegment {
  id: string
  interview_session_id: string
  question_asked: string
  question_index: number
  video_url?: string | null
  video_key?: string | null
  transcript?: string | null
  status: string
  created_at: string
}

export interface ExtractedEntity {
  name: string
  summary?: string | null
  /** Always null today — the graph stores no person/place/organisation
   *  distinction. Present so typed entities can land without a shape change. */
  kind?: string | null
}

/** What the system understood from one recording. Read-only. Says nothing
 *  about where each piece is stored — entities are moving from Graphiti to
 *  Postgres behind the endpoint. */
export interface SegmentExtraction {
  segment_id: string
  question_asked: string
  status: string
  transcript?: string | null
  topic_tags: string[]
  unit_count: number
  entities: ExtractedEntity[]
  still_processing: boolean
  entities_unavailable: boolean
}

export interface InterviewSessionState {
  session: InterviewSession
  questions: InterviewQuestion[]
  segments: RawSegment[]
}

export interface SegmentPresign {
  upload_url: string
  video_key: string
  method: string
  content_type: string
}

export interface EntityCandidate {
  uuid: string
  name: string
  summary: string
}

export interface PendingConfirmation {
  segment_id: string
  interview_session_id: string
  question_asked: string
  pending_confirmation: {
    entity_name: string
    // One candidate -> a simple yes/no question. Two or more -> the
    // storyteller picks which existing person/place this is (or "someone
    // new") instead of being asked a yes/no about an arbitrary single guess.
    candidates: EntityCandidate[]
    question: string
  }
}

export interface ApiError {
  response?: {
    data?: {
      detail?: string
    }
  }
  message?: string
}

// Family access (Prompt 9)
export interface FamilyInvite {
  id: string
  token: string
  status: 'pending' | 'redeemed' | 'revoked'
  redeemed_by_user_id?: string | null
  created_at: string
  expires_at: string
  redeemed_at?: string | null
}

// Producer-level /talk chat mode. "video_clips_v2" (Prompt 15) is an
// experimental full-archive-reading alternative to "video_clips"; both use
// the same VideoClipTalkInterface (identical response shape).
export type ChatMode = 'avatar' | 'video_clips' | 'video_clips_v2'

export interface TalkAvailability {
  producer_id: string
  producer_name: string
  available: boolean
  ready_segment_count: number
  avatar_id?: string | null
  avatar_image_url?: string | null
  // Prompt 14: which chat mode /talk should render — the PRODUCER's own
  // setting, never something the family viewer picks per-session.
  chat_mode: ChatMode
}
