# V2 as the primary path; avatar mode dormant — investigation and plan

**Written 2026-08-13. Re-verified function-by-function the same day (§0
addendum below); the corrections it produced are folded into the steps.
EXECUTED the same day on branch `avatar-dormant` (stacked on `remove-v1`),
steps 1–8, suite green after each — see PROJECT_STATUS.md for the landing
record. The live smoke test in step 8 is the one piece still pending a
running stack.**
Goal: a new producer's onboarding never touches avatar/photo/voice setup;
`video_clips_v2` stands fully on its own with **zero rows in `avatars`**;
avatar mode becomes an explicitly-enabled, self-contained optional feature.

Everything below was traced to specific call sites on the current tree
(branch `remove-v1`, HEAD `155b45f`). Line numbers are from that tree and
will drift; the function names won't. **This plan assumes `remove-v1` is
merged first** — it edits the same `websocket.py` dispatch region the
removal just simplified, and building it against pre-removal `main` would
merge-conflict for no reason.

---

## 1. Why avatar setup is mandatory today — the trace

### 1.1 What a brand-new producer actually experiences

1. **Register** (`users.register_user`) — creates a `User` row (role
   `producer`, self-entity in the same transaction) and **nothing else**.
   No avatar is created and none is asked for. So far so good.
2. **`chat_mode` defaults to `'avatar'`** (`models.py:67`,
   `server_default="avatar"`). Out of the box, this producer's `/talk` — and
   their own Chat tab — is the MuseTalk avatar mode. v2 is opt-in via
   Settings.
3. **`/record` works with no avatar.** The Record/Family/Timeline views are
   gated only on `role === 'producer'` (`page.tsx:257-265`); ingestion,
   extraction, tree and timeline never mention avatars. A producer can
   record their entire archive without an avatars row.
4. **Chat is where it bites.** Every conversation needs a `Session`, and
   `sessions.avatar_id` is **NOT NULL** (`models.py:127-129`).
   `POST /sessions/create` 404s without a real `avatar_id`, 400s unless
   that avatar is `status='ready'`, and derives ALL of its authorization
   from `avatar.user_id` (`sessions.py:32-57`).
5. **In v2 the frontend papers over this** rather than removing it: the
   producer's Chat view silently auto-resolves `avatars.find(ready) ??
   avatars[0]` (`page.tsx:192-205`) because "clips still need one to anchor
   a session" (`page.tsx:536-537`). With zero avatars it dead-ends on *"One
   quick setup step left … add a photo in Avatar mode, then switch back"*
   (`page.tsx:538-551`).
6. **Family is hard-blocked.** `GET /family/talk-availability` returns
   `available = ready_count > 0 AND avatar is not None`
   (`family.py:195-206`) — with no mode check — and `/talk` additionally
   refuses to render without `availability.avatar_id` (`talk/page.tsx:169`).
   A producer with 50 ready recordings on v2 and no photo shows their
   family *"still preparing their stories"*.

So the full contortion for a producer who wants v2 today is: register →
Settings → switch mode to v2 → Chat dead-ends → switch BACK to avatar mode
so the Avatars tab reappears → upload a face photo (10MB image, PIL
processing, `avatar_processor`) → switch to v2 again. The photo is then
never shown to anyone — v2 plays real footage — but its row is what every
session, WS connection and availability check hangs off.

### 1.2 The four load-bearing couplings, precisely

| # | coupling | where | what actually depends on it |
| --- | --- | --- | --- |
| 1 | `Session.avatar_id` NOT NULL FK, `ondelete=CASCADE` | `models.py:127-129` | every session row, both modes |
| 2 | Session creation authorizes via the avatar's owner | `sessions.py:43-57` (`is_owner = avatar.user_id == uid`; family check compares `current_user.producer_id == avatar.user_id`) | who may talk to whose archive |
| 3 | WS resolves the producer THROUGH the avatar | `websocket.py:254-300` (`_load_session_data`: `producer_id`, `producer_recording_language`, `producer_chat_mode`, `language` all read off `session.avatar.user_id`) | v2's `group_id` — no avatar ⇒ `group_id=None` ⇒ `_NO_PIPELINE_MSG` no-story on every question |
| 4 | Family availability requires a ready avatar unconditionally | `family.py:195-213`, consumed at `talk/page.tsx:169` | whether `/talk` renders at all |

Everything else is presentation-layer echo of #1: `SessionCreate.avatar_id`
required (`schemas.py:65`), `SessionResponse.avatar_id` typed as required
(`schemas.py:72`, mirrored at `lib/types.ts:30`), `api.createSession(avatarId)`
(`api.ts:146`), `useVideoClipChat(avatarId)` (`useVideoClipChat.ts:62,242`),
the avatarId props on `VideoClipTalkInterface` / `ProducerVideoClipChat`,
the page.tsx auto-resolve + dead-end, and even
`eval_no_story_subject.py:75-80`, which must hunt for an avatar row just to
seed a test session ("producer has no avatar row; cannot seed a session").

### §0 re-verification addendum (2026-08-13) — what the second pass added

Re-traced every symbol above against the tree before building. The four
couplings are **exhaustive**: the only readers of the `Session.avatar`
relationship anywhere are `websocket.py:219/254`, and the only readers of
`avatar.user_id` outside `avatars.py`'s own ownership checks are
`sessions.py:48,52`, `family.py:197`, `websocket.py:277` and the eval
script. No `app/services/*` module imports the `Session` or `Avatar`
models at all; `conversations.py` and `messages.py` are clean. Corrections
the pass produced, now reflected in the steps:

- `SessionResponse.avatar_id` and `SessionCreate.avatar_id` must become
  `Optional` (schemas.py:72/65), and `lib/types.ts:30` nullable — missed as
  explicit step items in the first draft.
- **Seven test files construct `Session(...)` directly** and break the
  moment `producer_id` is NOT NULL: `test_conversations.py:28`,
  `test_regressions.py:42`, `test_video_clip_e2e.py:138`,
  `test_ws_auth.py:32`, `test_ws_e2e.py:55`, `test_websocket.py:499`,
  `test_retrieval_service.py:92`. Their constructors are updated in the
  step-1 commit itself, not later.
- `test_users.py`'s `test_update_profile_defaults_chat_mode_to_avatar`
  encodes the old default and is rewritten (not deleted) in step 1 to
  assert the new v2 default.
- The FK to alter is named: `sessions_avatar_id_fkey` (recreated with
  CASCADE by migration `0003`). Removing the ORM `delete-orphan` cascade
  yields SQLAlchemy's default nullify-on-parent-delete, which matches the
  new DB-level `SET NULL` — including on the SQLite test engine, which
  never runs migrations.
- `delete_avatar`'s comment ("sessions/messages/conversations cascade",
  `avatars.py` delete endpoint) becomes false at step 1 and is corrected in
  the same commit.
- `websocket_manager.set_avatar` (`websocket.py:1185`) has **zero callers**
  — dead base-project code. Left alone (dormant), recorded here so nobody
  re-traces it.

### 1.3 Verdict: wired that way, not load-bearing

**No part of v2 consumes the avatar.** `_handle_video_clip_question_inner`
(`websocket.py:752-794`) needs exactly two things from the session:
`producer_id` and `producer_recording_language`. Both are properties of the
**User** row; the avatar is merely the pointer the base project happened to
route them through, because in the original AvatarAI product a session
existed *to talk to an avatar* — the avatar WAS the product. `/record`,
ingestion, entities, tree, timeline, media, retrieval: none of them touch
`avatars`. The coupling is real (schema + auth derivation), but it is
shallow — a pointer that should aim at `users` aiming at `avatars` instead,
plus one availability check written when MuseTalk was the only renderer
("must have … a ready avatar image for MuseTalk to animate" —
`family.py:179`).

One genuinely bad consequence of the current shape, found in the trace and
worth fixing regardless: **`Avatar.sessions` carries
`cascade="all, delete-orphan"` (`models.py:110-113`) — deleting an avatar
deletes every session that ever used it, with all its messages.** In v2
that means deleting a photo nobody sees destroys the family's conversation
history and the shown-unit memory inside it. The comment there says the
cascade exists to avoid an FK violation; producer-keyed sessions remove the
need for it entirely.

## 2. What v2 genuinely needs from onboarding

If avatar mode did not exist, the minimal producer setup is:

1. **An account** — `User` row + self-entity, which `register_user` already
   creates in one transaction.
2. **Recordings** — `/record`, which already works avatar-free end to end.
3. **A family invite** — `POST /family/invites` → redeem link, which links
   the family account via `User.producer_id`. Already avatar-free.
4. **≥ 1 ready segment** — the honest availability condition for v2.

That is the whole list. The authorization model needs nothing from avatars
either: "who may talk to whose archive" is answerable from `users` alone —
a producer talks to their own archive; a family account talks to
`user.producer_id`'s archive. `create_session`'s current avatar-mediated
check is exactly this check with one extra join in the middle.

**"Producer setup", with v2 primary, is therefore: register → record →
invite. Nothing else.** The first question a family member asks should work
against an account that has never opened Avatar Studio, never uploaded a
photo, and has zero rows in `avatars` and `voices`.

## 3. The inversion — decisions, resolved

### 3.1 Sessions become producer-keyed; the avatar becomes optional cargo

- **`sessions.producer_id`** — new column, `String FK users.id NOT NULL`,
  indexed. THE anchor: whose archive this conversation is against.
- **`sessions.avatar_id`** — becomes **nullable**, FK changes
  `ondelete=CASCADE` → `ondelete=SET NULL`. Kept (not dropped) because
  avatar-mode sessions still legitimately record which avatar spoke, and
  HistoryPanel resumes against it. The ORM cascade
  `Avatar.sessions = relationship(..., cascade="all, delete-orphan")` is
  **removed** — deleting an avatar orphans no conversations.
- **Single source of truth:** `producer_id` is authoritative and always
  written server-side. When `avatar_id` is present it is *validated* at
  create time to belong to `producer_id` (`avatar.user_id == producer_id`)
  and never read for identity again. This is the "two places hold one fact"
  rule: the second place is allowed to exist only because the first is the
  only one anything reads.

### 3.2 `POST /sessions/create` — the new contract

`avatar_id` becomes optional in `SessionCreate`. The server:

1. Resolves the producer from the **caller**, never the body:
   `producer_id = user.id` if `role == 'producer'`, else
   `user.producer_id`; 403 for an unlinked family account. (Same
   authorization semantics as today — owner or linked family — minus the
   avatar join. The `demo-user` DEBUG fallback keeps working: it is a
   producer id like any other.)
2. Loads the producer's `chat_mode`.
   - **v2:** `avatar_id` is ignored if absent; if the client did send one,
     validate ownership and store it (harmless, honest history).
   - **avatar mode:** an avatar is genuinely required to produce output, so
     resolve it server-side — the body's `avatar_id` if given (validated:
     exists, `ready`, owned by the producer — today's three checks,
     unchanged), else the producer's most recent `ready` avatar, else
     **400 naming the real problem** ("Avatar mode is enabled but no avatar
     is ready"). This moves page.tsx's `?? avatars[0]` guess — which could
     pick a non-ready avatar and 400 obscurely — into one place that picks
     correctly.
3. Writes the session with both columns.

Frontend and backend deploy together (the standing convention, same as
`confirm-entities`), so the contract changes outright — no versioning, no
window where one side speaks the other's old shape.

### 3.3 WS: producer resolved from the session row

`_load_session_data` loads the producer via `session.producer_id` directly;
the entire avatar block (`avatar_image_key`, `_resolve_local_image`, voice
auto-load) runs **only when `session.avatar_id` is set** — which after 3.2
means only avatar-mode sessions. v2 sessions never touch storage for an
avatar image again (today every v2 WS connect resolves/downloads the unused
photo — `websocket.py:257`). The existing "no avatar image → drain queue
silently" guard (`websocket.py:1020-1022`) stays as defence in depth for an
avatar deleted mid-session (now SET NULL instead of session destruction).

### 3.4 `talk-availability` becomes mode-aware

```python
avatar_needed = producer.chat_mode == "avatar"
available = ready_count > 0 and (avatar is not None if avatar_needed else True)
```

`avatar_id` / `avatar_image_url` stay in the response (already Optional) —
they are simply `None` for a photo-less v2 producer. `talk/page.tsx` drops
the `|| !availability.avatar_id` condition at line 169 and gates on
`available` alone; `VideoClipTalkInterface` no longer receives or needs
`avatarId`. The avatar-mode branch (`TalkInterface`) keeps requiring
`avatar_id` — its idle image is real UI there — with a null-guard rendering
the same "still preparing" state (an avatar-mode producer with no avatar is
now representable, and honest UI beats a crash).

### 3.5 `chat_mode` defaults to `video_clips_v2`; existing rows backfilled where avatar mode cannot function

- `User.chat_mode` default and `server_default` flip to
  `"video_clips_v2"`. Every new producer starts on v2.
- Backfill in the same migration:
  `UPDATE users SET chat_mode='video_clips_v2' WHERE chat_mode='avatar' AND
  NOT EXISTS (SELECT 1 FROM avatars WHERE avatars.user_id = users.id AND
  avatars.status='ready')` — flips only accounts where avatar mode is
  *already non-functional* (no ready avatar ⇒ `/talk` unavailable today),
  so nobody's working experience changes. Verified against live data before
  running (step 0): expected result is every `'avatar'` account flips (all
  have zero recordings; the one real producer is already v2), but the WHERE
  clause is what makes the migration correct rather than the expectation.
- `CHAT_MODES` itself is unchanged — both modes remain valid values.

### 3.6 The activation gate: switching TO avatar mode is where avatar setup lives

Avatar mode's on-switch is the existing Settings control, and that switch
becomes the **only** place the app ever demands an avatar:

- `update_profile` (`users.py:280-286`) gains one validation: setting
  `chat_mode='avatar'` requires the account to own a `ready` avatar —
  else 400 with a message naming what to do. This is the same shape as the
  CHAT_MODES vocabulary check beside it, and it makes "avatar mode is on
  but can never produce output" unrepresentable going forward.
- `SettingsPanel` renders the avatar-mode card as the gate's UI: when no
  ready avatar exists, the card shows "requires an avatar — set one up" and
  routes to Avatar Studio instead of calling `updateProfile`. The
  Avatars/Voice nav tabs stay exactly as they are today — hidden in
  video-clip mode (`page.tsx:266-271`), visible in avatar mode — which is
  already the dormant-UI behaviour; with v2 the default, a new producer
  simply never sees them.
- Nothing else about avatar mode changes. `ChatInterface`, `TalkInterface`,
  `voices.py`, `animator.py`, `gpu_client`, MuseTalk, the `set_voice` /
  `set_avatar` WS messages — untouched, reachable exactly as today once the
  mode is on. Runtime dormancy is already real: MuseTalk/Chatterbox are
  lazy-loaded (`/health` reports "lazy (not yet loaded)" until first use),
  and the only eager model is Whisper, which is the shared STT fallback,
  not an avatar component. **No model-loading changes are part of this
  plan.**

### 3.7 Frontend shell cleanup (page.tsx and the hook)

- `useVideoClipChat(avatarId)` → `useVideoClipChat()`;
  `api.createSession(avatarId)` → `api.createSession(avatarId?)` (body
  includes `avatar_id` only when given). `ProducerVideoClipChat` and
  `VideoClipTalkInterface` drop the `avatarId` prop.
- page.tsx deletes: the auto-resolve effect (`192-205`), `avatarResolving`,
  the "One quick setup step left" dead-end (`538-551`), and the loader
  branch (`530-534`). The chat-view condition becomes
  `view === 'chat' && isVideoClipMode` with no `selectedAvatar` term; the
  nav item's `disabled` term keeps its avatar condition **for avatar mode
  only** (unchanged: `!selectedAvatar && !isVideoClipMode`).
- `HistoryPanel.onResume` — `SessionResponse.avatar_id` is now nullable.
  Resume in avatar mode keys on it as today; a v2 session (or a null
  `avatar_id`) resumes by simply opening the Chat view (the v2 screen
  starts its own session and does not replay history — already true today,
  not a regression). Guard the `avatarMap[s.avatar_id]` lookups for null.
- The `key={selectedAvatar}` on `ProducerVideoClipChat` (page.tsx:510)
  becomes unnecessary and is dropped — the component no longer varies by
  avatar.

### 3.8 What this deliberately does NOT do

- **Does not remove avatar mode, or any of its code.** V1's removal was
  earned by an A/B and zero users; avatar mode keeps working end to end
  behind its switch. If its removal is ever wanted, that is
  V1_REMOVAL_PLAN's §7.4 caveat — a separate plan over
  `retrieval_service`'s shared machinery, not an extension of this one.
- **Does not drop `sessions.avatar_id`** — see 3.1.
- **Does not touch `/record`'s read-aloud.** It uses `tts.py`, which lives
  near the avatar stack but is a producer-facing recording feature, not
  avatar mode.
- **Does not change retrieval, ingestion, entities, media, or any eval
  baseline.** No prompt text changes anywhere in this plan, so
  `prompt_regression.py` is not implicated.

## 4. Build order — each step ends with the suite green

**Step 0 — pre-flight (live queries, recorded in PROJECT_STATUS):**
counts of `sessions` (and how many have a non-null avatar whose owner ≠
`user_id` — expected 0 for producers, >0 for family sessions, which is
exactly what `producer_id` will record correctly), `avatars` per user with
status, `chat_mode` distribution, and confirmation that the only
avatar-owning producer is already `video_clips_v2`. Merge `remove-v1`
first. Full suite + frontend build green as baseline.

**Step 1 — migration `0027` + models.** Add `sessions.producer_id`
(nullable) → backfill `UPDATE sessions SET producer_id = avatars.user_id
FROM avatars WHERE sessions.avatar_id = avatars.id` → `SET NOT NULL` +
index. Alter `avatar_id` nullable; drop `sessions_avatar_id_fkey` and
re-add with `ondelete=SET NULL`. Flip `users.chat_mode` server_default and
run the 3.5 backfill. Model edits: `Session.producer_id`, nullable
`avatar_id`, remove the `Avatar.sessions` delete-orphan cascade
(relationship stays; ORM default nullifies on delete, matching the DB).
`SessionResponse.avatar_id` becomes `Optional` (a NULL is representable
from this step on, via avatar deletion). `create_session` starts
**writing** `producer_id` (derived per 3.2) while its external contract is
unchanged — the safest intermediate state: every new row carries the new
anchor, nothing reads it yet. Same commit: the seven direct
`Session(...)` test constructors gain `producer_id`, the
`eval_no_story_subject.py` seeder gains it too, `delete_avatar`'s stale
cascade comment is corrected, and the `test_users` default-mode test is
rewritten for the v2 default.

**Step 2 — WS reads the new anchor.** `_load_session_data` per 3.3.
After the step-1 backfill every session row has `producer_id`, so there is
no fallback path to keep. Tests: a v2 session with `avatar_id=NULL` gets
`producer_id`/language/mode loaded and answers a clip question; an
avatar-mode session still loads image + voice.

**Step 3 — `create_session` contract.** `SessionCreate.avatar_id`
optional; the 3.2 derivation + validations; `api.createSession` optional
arg + `lib/types.ts` Session type nullable `avatar_id`;
`useVideoClipChat()` + both clip components drop `avatarId`. Rewrite
`test_sessions.py`'s creation tests: v2 producer with zero avatars
creates; linked family creates against the producer; unlinked family 403s;
avatar-mode create resolves the latest ready avatar / 400s with none; a
body `avatar_id` not owned by the resolved producer 403s.

**Step 4 — family availability + /talk gating** per 3.4. Tests: v2
producer, one ready segment, zero avatars → `available=true`,
`avatar_id=null`; avatar-mode producer without an avatar →
`available=false`; with one → unchanged.

**Step 5 — page.tsx cleanup** per 3.7. `tsc`, `eslint`, `next build`.

**Step 6 — the activation gate** per 3.6: `update_profile` validation +
SettingsPanel affordance. Tests: switching to `avatar` without a ready
avatar 400s; with one succeeds; switching to v2 never requires anything.

**Step 7 — de-avatar the periphery.** `eval_no_story_subject.py` seeds
`Session(user_id=…, producer_id=…, status="active")` and drops the avatar
lookup + its RuntimeError. New regression test: deleting an avatar leaves
its sessions and messages intact (`avatar_id` nulled) — the 1.3 cascade
hazard, pinned.

**Step 8 — docs pass + final verification.** CLAUDE.md (chat-modes table:
default is v2; avatar optional, gated on owning a ready avatar),
PROJECT_STATUS entry, this file's status line. Grep gate: `avatar` appears
in none of `full_archive_retrieval.py`, `useVideoClipChat.ts`,
`VideoClipTalkInterface.tsx`, `ProducerVideoClipChat.tsx` (except
comments/history). Full suite; frontend build. **Live smoke, the one that
defines success: a freshly registered producer records one answer, invites
a family account, and the family gets a real clip answer on /talk — with
`SELECT count(*) FROM avatars WHERE user_id = :new_producer` = 0.**

## 5. Explicit keep-list (things that look avatar-shaped and must not move)

- `avatars.py`, `voices.py`, `avatar_processor`, `animator`, `gpu_client`,
  the MuseTalk worker, `ChatInterface`, `TalkInterface`, `AvatarUpload`,
  `AvatarList`, `VoicePanel` — avatar mode's own body, dormant but intact.
- `websocket.py`'s avatar pipeline (`_animate_from_queue`,
  `_handle_text_input_inner`, `set_avatar`/`set_voice` handlers) — reached
  only when `producer_chat_mode == 'avatar'`, exactly as today.
- `tts.py` — shared by /record's read-aloud (§3.8).
- Whisper eager load in `main.py` — live STT fallback, not avatar code.
- `sessions.avatar_id` column and `SessionResponse.avatar_id` — nullable,
  kept for avatar-mode sessions and history.
- The `/health` `avatar_engine` line — reporting, not a dependency.
- `ChatInterface`'s `getAvatars` fetch (its idle image) and
  `websocket_manager.set_avatar` (dead, zero callers) — avatar-mode
  surface, untouched.

## 6. What is genuinely hard here — honest answer: not much, and here is the full list

1. **The migration touches live rows** (`sessions` backfill via the avatars
   join, `chat_mode` backfill). Both are single UPDATEs with verifiable
   before/after counts, and the 0012 lesson applies: run against Neon via
   `alembic upgrade head` only, never `create_all`; the
   `_is_local_database` guard already protects this.
2. **Authorization moves from one derivation to another.** Today: "may use
   this avatar" (owner-or-linked-family of `avatar.user_id`). After: "may
   talk to this producer" (self-or-linked via `users`). These are provably
   the same relation minus the join — but auth changes deserve their own
   tests, and step 3's set covers each arm including the 403s.
3. **Blast radius is wide but shallow in tests**: ~11 test files build
   sessions through avatar fixtures (`test_sessions`, `test_websocket`,
   `test_ws_e2e`, `test_ws_auth`, `test_video_clip_e2e`, `test_family`,
   `test_conversations`, `test_e2e_flow`, `test_regressions`,
   `test_retrieval_service`, `test_users`). Most keep their avatar
   fixtures (avatar-mode paths still need them); the change is adding
   `producer_id` to fixtures and new avatar-less cases, not rewrites.
4. **One deliberate behaviour change to say out loud:** deleting an avatar
   stops deleting conversations (CASCADE → SET NULL). Strictly better, but
   it is a semantics change to an existing endpoint, and the old behaviour
   was documented in a model comment as intentional. The step-7 test pins
   the new semantics.

There is **no** deep architectural reason for avatar-as-prerequisite. It is
the base project's data model outliving the base project's product: session
→ avatar → owner was load-bearing when the avatar was the thing being
talked to; v2 talks to an archive, and the archive's owner has been sitting
one column away on `users` the whole time.

## 7. Residual risks, stated honestly

1. Eval scripts that seed sessions are credits-gated, so a mistake in
   step 7 surfaces on their next run, not in CI. Mitigation: the seeding
   change is mechanical and the script hard-fails loudly by design.
2. A live WS session open across the deploy holds pre-loaded
   `session_data` from the old code; it dies with its process on deploy
   and reconnects through the new path. Same exposure as every deploy;
   nothing here is stateful across restarts except the DB rows the
   migration handles.
3. `HistoryPanel` null-avatar handling is small but easy to miss in
   review — it is named in step 5 so it cannot be skipped silently.
4. If any external client (none known) calls `POST /sessions/create` with
   the old required-`avatar_id` expectation, it keeps working — the field
   remains accepted and validated. The contract only *loosened*.
