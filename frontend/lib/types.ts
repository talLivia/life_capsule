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
  /** Stable question id. Takes are grouped by this, never by question_index,
   *  which is positional and moves when the question set is edited. */
  question_id?: string | null
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
  /** The automatic work is done and the pipeline is paused on a person.
   *  Distinct from still_processing: conflating them left this screen saying
   *  "hang on a moment" indefinitely while the questions sat ready. */
  awaiting_confirmation?: boolean
  entities_unavailable: boolean
  /** Which analysis node is running, and what to call it. Both null once the
   *  run is over. The label is resolved server-side so the node list has one
   *  home — a copy here would drift the first time one is renamed. */
  progress_stage?: string | null
  progress_label?: string | null
}

// ── Interview flow (docs/INTERVIEW_RESTRUCTURE.md step 4) ────────────────
// Rendered as-is by the accordion. Position, reachability and completeness
// are all DERIVED server-side — the client recomputes none of it, so there
// is no second opinion that can disagree.

export interface GateOption {
  value: string
  label: string
}

export interface FlowStep {
  kind: 'question' | 'gate'
  id: string
  text: string
  done: boolean
  /** question steps only — a question can hold several takes */
  takes?: number | null
  /** gate steps only. Data-driven: render one control per option, never
   *  assume a yes/no pair. */
  options?: GateOption[] | null
  answer?: string | null
}

export interface FlowCategory {
  id: string
  label: string
  /** Only REACHABLE steps — an unanswered gate ends the list. */
  steps: FlowStep[]
  /** No reachable gate is still unanswered, so the shape is known. Distinct
   *  from `complete`: a settled category can still have questions to record. */
  settled: boolean
  position?: number | null
  /** null until `settled`. Do NOT substitute a guess — the agreed rule is
   *  that no counter shows at all until the total is real (§8.4). */
  total?: number | null
  done_count: number
  current_step_id?: string | null
  complete: boolean
  current: boolean
  /** Whether the accordion may open it. Server-decided, not a styling hint. */
  reachable: boolean
}

export interface InterviewFlow {
  interview_session_id: string
  free_navigation: boolean
  current_category_id?: string | null
  complete: boolean
  categories: FlowCategory[]
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

export interface IdentityQuestion {
  name: string
  // One candidate -> a simple yes/no question. Two or more -> the
  // storyteller picks which existing person/place this is (or "someone
  // new") instead of being asked a yes/no about an arbitrary single guess.
  candidates: EntityCandidate[]
  question: string
}

export interface TypeQuestion {
  name: string
  // Exactly two options, always — the extractor names the runner-up it was
  // torn between rather than reporting a confidence score, so the screen
  // never has to invent choices or render a slider.
  type: string
  alternative_type: string
  question: string
}

/** One family relation the extractor proposed. SKIPPABLE — see
 *  EntityBatchAnswer.relations for why this class differs from the others. */
export interface RelationQuestion {
  index: number
  from_name: string
  to_name: string
  relation_type: string
  evidence?: string | null
}

/** An event with no year yet. Skippable — free text, parsed server-side. */
export interface YearQuestion {
  name: string
  type: string
  question: string
}

export interface PendingConfirmation {
  segment_id: string
  interview_session_id: string
  question_asked: string
  // EVERY question one recording raises, asked on ONE screen with ONE submit.
  // Either list may be empty; the payload only exists when at least one is not.
  pending_confirmation: {
    identity_questions: IdentityQuestion[]
    type_questions: TypeQuestion[]
    relation_questions?: RelationQuestion[]
    year_questions?: YearQuestion[]
    parentage_questions?: ParentageQuestion[]
    /** Not a question — every name this recording produced, so any of them
     *  can be corrected. Never causes the screen to appear on its own. */
    editable_entities?: { name: string; type?: string | null }[]
  }
}

/** Whose child is this sibling?
 *
 *  The only class NOT raised by the recording being confirmed: these are
 *  siblings from earlier recordings who still have no parent recorded, so the
 *  tree places them in the right row and can draw no line to them. */
export interface ParentageQuestion {
  entity_id: string
  name: string
  question: string
  /** The producer's own recorded parents, offered as options. A LIST, not a
   *  yes/no, because a half-sibling shares one parent. */
  parents: { id: string; name: string }[]
}

export interface EntityBatchAnswer {
  /** Proposed relation index (as a string) -> accepted?
   *
   *  Skippable, unlike identity and types: an unanswered relation has a real
   *  empty outcome (store nothing), whereas both silent defaults for the
   *  others are wrong in opposite directions. Omitting a key and sending
   *  false mean the same thing. */
  relations?: Record<string, boolean>
  /** Entity name -> whatever the producer typed. Free text on purpose:
   *  "בערך 1973" is a fine answer and the client should not parse it. The
   *  server resolves it to one year or refuses with a reason. */
  years?: Record<string, string>
  // Keyed by entity name, matching the pending payload. Every question must
  // be answered — the server rejects a partial submit rather than defaulting,
  // because both plausible defaults are wrong in opposite directions.
  /** Sibling ENTITY ID -> whose child they are. Skippable: omit a sibling to
   *  say nothing. Keyed by id rather than name because, unlike every other
   *  class, these are people already in the archive. */
  parentage?: Record<string, { parent_ids: string[]; new_parent_name?: string }>
  /** Extracted name -> what it should say. The extractor can be CONFIDENTLY
   *  wrong, and a name it never doubted raises no question to answer. */
  name_edits?: Record<string, string>
  identity: Record<string, { same_as_existing: boolean; candidate_uuid?: string }>
  types: Record<string, string>
}

/** An entity type the producer's answer actually changed. Returned by
 *  confirm-entities so the answer visibly takes effect — it used to be
 *  accepted and silently discarded. */
export interface AppliedTypeChange {
  name: string
  was: string
  now: string
}

/** A year that could not be resolved to one number. Reported rather than
 *  guessed — a wrong year reorders a life and nothing would look broken. */
export interface RejectedYear {
  name: string
  given: string
  reason: string
}

export interface ConfirmEntitiesResult {
  segment: RawSegment
  applied_type_changes: AppliedTypeChange[]
  rejected_years: RejectedYear[]
}

// ── Family tree (docs/FAMILY_TREE_TIMELINE.md Phase 4) ───────────────────

export interface TreePerson {
  id: string
  name: string
  is_self: boolean
  year_start?: number | null
  year_end?: number | null
  /** null for anyone with no family path to the root — see `unplaced`. */
  generation?: number | null
}

export interface TreeGeneration {
  /** Negative is up the tree (ancestors), 0 is the producer. */
  generation: number
  people: TreePerson[]
}

export interface TreeEdge {
  from_id: string
  to_id: string
  relation_type: string
  label_en: string
  label_he: string
  /** The recording that established it — "brother" can play them saying so. */
  source_segment_id: string
}

export interface TreeContradiction {
  from_id: string
  to_id: string
  relation_type: string
  source_segment_id: string
  kept_generation: number
  implied_generation: number
}

export interface FamilyTree {
  root_id?: string | null
  generations: TreeGeneration[]
  /** Real people with no family path to the root. Shown separately so nobody
   *  is dropped and nobody is placed in a row they don't belong to. */
  unplaced: TreePerson[]
  edges: TreeEdge[]
  contradictions: TreeContradiction[]
  missing_generation_delta: string[]
}

export interface EntityMoment {
  segment_id: string
  question_asked: string
  question_id?: string | null
  video_url?: string | null
  transcript?: string | null
  summary?: string | null
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
