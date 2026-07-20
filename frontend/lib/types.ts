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
  content: string
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

export interface ApiError {
  response?: {
    data?: {
      detail?: string
    }
  }
  message?: string
}
