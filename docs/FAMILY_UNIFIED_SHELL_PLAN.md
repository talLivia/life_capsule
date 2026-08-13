# Family unified shell — /talk removal + invite management

**Written 2026-08-13. Branch `family-unified-shell`, stacked on
`avatar-dormant`** (depends on producer-keyed sessions: family chat in the
shell creates sessions with no avatar and resolves the archive from the
account linkage). The TALK_PAGE_REMOVAL_INVESTIGATION this was to build on
was never written; the trace below is that investigation, done fresh
against the current tree (HEAD `5724f86`).

Two parts, one plan: family members use the regular app shell (Chat +
view-only Timeline + view-only Family tree) instead of a dedicated /talk
page, and the producer's Settings screen manages the invite lifecycle
(pending invites → active users).

---

## 1. Trace — what exists today (all verified on the tree, not assumed)

### 1.1 The /talk page and what it provides

`app/talk/page.tsx` provides four things: (a) an auth gate defaulting to
REGISTER when `?invite=` is present; (b) `RedeemForm` — token redemption,
auto-submitting an `initialToken`; (c) the availability gate ("still
preparing their stories") from `GET /family/talk-availability`; (d) the
mode-routed chat: `VideoClipTalkInterface` (v2) / `TalkInterface` (avatar),
both calm-themed full-screen layouts. The chat behaviour itself lives in
`useVideoClipChat`, already shared with the producer's shell chat.

### 1.2 Family gating today

- `app/page.tsx:170-172`: a family account is REDIRECTED off the shell to
  /talk before anything renders. The shell has never rendered for family.
- Nav: Record/Family/Timeline tabs are `isProducerUser`-gated; Avatars/
  Voice hidden in v2; `NotificationCenter` producer-only. History/Settings/
  Chat/Home are unconditional (family never saw them due to the redirect).
- **`entities.py` — every endpoint is `require_producer`**, including the
  pure reads (`GET /entities/tree`, `/{id}/moments`, `/timeline`,
  `/relation-types`). A family account 403s on the tree and timeline
  today. Writes (`POST .../relations`, `DELETE .../relations/{id}`) are
  also `require_producer` — correct, stays.
- **`media.py` already solved the read-scoping problem**:
  `_archive_owner_id(user)` → producer reads own archive, linked family
  reads `user.producer_id`'s, unlinked family 403s. `GET /media` is
  family-readable (Phase 8); every write is producer-only via key
  ownership.
- Sessions/WS: producer-keyed (avatar-dormant). `create_session` derives
  the archive from the caller's linkage; family creation is tested. WS
  auth (`_verify_ws_session`) checks SESSION OWNERSHIP only — note for
  Part 2: revoking linkage must also end active sessions, or an open
  session id keeps working over WS after revocation.

### 1.3 The invite lifecycle ALREADY EXISTS — no new schema

`family_invites` (model + endpoints in `family.py`): `producer_id`,
`token`, `status` ∈ pending/redeemed/revoked, `expires_at` (7 days),
`redeemed_by_user_id`, `redeemed_at`. `POST /family/invites` creates;
`GET /family/invites` lists the producer's; `DELETE /family/invites/{id}`
revokes a PENDING one; `POST /family/invites/redeem` flips the caller to
`role='family'` + `producer_id=<issuer>` and stamps the invite
`redeemed`. The lifecycle the request describes — pending moves to active
on redemption — is already the data model; what's missing is only the
"active users" READ and its UI.

### 1.4 Display name at signup

`AuthModal`'s register tab already collects `full_name` — but it is
**optional** (placeholder "Optional"). The Active-users list therefore
uses `full_name || username`, the same fallback `talk-availability`
already applies to the producer's own name. Making it required for
invite-flow signups is a two-line change if wanted — **flagged as a
decision, defaulting to optional + fallback** (no new required field).

### 1.5 Inline edit controls that must hide for family (the §Part-1 flag)

Confirmed by enumeration, these are component-level controls, NOT
route-gated, so hiding /talk's replacement route is not enough:

| surface | control | today |
| --- | --- | --- |
| TimelinePanel | `AddPeriodPhotos` (two call sites: gallery card + §5 empty-state entry point) | producer-only page, unconditional render |
| FamilyTreePanel | the "How is X related" relation editor (`set_relation`) | unconditional |
| FamilyTreePanel | per-edge "Remove" (two-click) in Recorded relations | unconditional |
| FamilyTreePanel | `EntityPortrait` click-to-upload | unconditional |

All become conditional on the viewer's role (frontend), AND the backend
writes they call remain `require_producer` (defence in depth — the UI
hiding is presentation, the 403 is the guarantee).

## 2. Part 1 — decisions

**2.1 Family lands on the shell with exactly three views.** The redirect
at `page.tsx:170-172` goes. For `role === 'family'`: nav = Chat / Timeline
/ Family (+ sign-out); default and fallback view is `chat` (the marketing
Home is producer-facing); the `?view=` deep link and every `setView` path
clamp to the allowed set. Record, Settings, History, Avatars, Voice,
NotificationCenter: not rendered, not reachable.

**2.2 Family Chat = the existing /talk chat, mounted as the shell's chat
view.** A new thin `FamilyChatView` does what TalkPageInner did after its
gates: fetch `talk-availability`, show "still preparing" until available,
then render `VideoClipTalkInterface` or `TalkInterface` by the producer's
mode. Everything /talk does — clips, photo galleries, follow-ups,
clarifications, voice input — comes along because it lives in the
components, not the page. ⚠️ Visual note, flagged deliberately: the calm
theme (`calm-*`) was "/talk-only" per the globals.css rule; it now renders
inside the dark shell for family. Accepted for this change (the calm
look IS the family experience); the globals.css comment is updated to say
"family chat surfaces only" rather than naming a route.

**2.3 Backend reads open to linked family — one new dependency.**
`require_archive_member` in `entities.py` (mirroring media's
`_archive_owner_id`): returns the archive owner id — `user.id` for a
producer, `user.producer_id` for linked family, 403 unlinked. Applied to
the three reads family needs: `/entities/tree`, `/entities/{id}/moments`
(tree playback is viewing), `/entities/timeline`. `relation-types` stays
producer-only (it feeds the edit picker, which family never sees).
Writes untouched.

**2.4 Read-only panels.** TimelinePanel and FamilyTreePanel derive
`isFamilyViewer` from the store user and suppress §1.5's controls. The
tree portrait renders as a plain photo/initials circle (no upload
affordance); moments playback stays. `GET /media?category=` already works
for family, so timeline galleries render read-only with no add control.

**2.5 /talk becomes a redirect stub; invite links move to `/?invite=`.**
The page's content (RedeemForm, gates, chat routing) moves into the
shell; `app/talk/page.tsx` is replaced by a ~10-line client redirect to
`/?invite=<token>` (preserving the query) — already-shared links carry a
7-day TTL, so hard-404ing them costs real people for zero benefit.
`FamilyInvitePanel.buildInviteUrl` generates `/?invite=` from now on.
The shell handles `?invite=`: unauthenticated → AuthModal on the register
tab with the invite copy (exactly /talk's behaviour today);
authenticated-with-token → auto-redeem via the ported RedeemForm flow,
after which the store user flips to family and the shell re-renders as
2.1. A fresh signup is `role='producer'` until redemption (existing
semantics, unchanged — redemption is what flips the role, and redeem
already rejects accounts with recorded content).

## 3. Part 2 — decisions

**3.1 No new schema; one new read.** Pending = `GET /family/invites`
filtered client-side to `status='pending'` and unexpired (both fields are
already in the response). Active = new `GET /family/members`: users with
`role='family' AND producer_id = me`, left-joined to their redeemed
invite for `redeemed_at`; returns `{user_id, display_name, joined_at}`
with `display_name = full_name || username`. Sourcing Active from the
USERS table (the linkage itself) rather than from redeemed invite rows is
deliberate: the linkage IS access, so the list can never disagree with
what the account can actually do — and a future revoke (unlink) removes
the row from this list by construction, with no second bookkeeping.

**3.2 One lifecycle in the UI.** `FamilyInvitePanel` becomes two sections
fed by one load: "Pending invites" (link + copy + revoke, as today) and
"Active users" (display name, joined date). Redemption moves a row from
one section to the other automatically because the two queries partition
on the same fact.

**3.3 Revoke/delete semantics — DECIDED 2026-08-13: (b), full account
deletion.** The producer chose deletion over the recommended unlink after
the tradeoff was flagged; the destruction of chat history is deliberate
and the UI's confirm step names it ("Delete account + history?"). The
implementation tears down open WebSockets first (the §1.2 hole), nulls
the redeemed invite's reference (the invite row survives as history), and
lets the account cascade take sessions/messages/conversations. The
original proposal is kept below as the record of what was considered.
Two candidate meanings for "remove this family member's access":

- **(a) Unlink (RECOMMENDED — the safer default):** set
  `user.producer_id = NULL` (role stays `family`) **and end their active
  sessions** (`status='ended'`, disconnect WS). Every family-gated
  surface dies immediately (`require_family`, `_archive_owner_id`,
  `create_session`, `talk-availability` all check the linkage
  per-request); ending sessions closes the WS hole noted in §1.2. The
  person's account and chat history SURVIVE — re-inviting them later
  restores access with their history intact.
- **(b) Delete the account:** `DELETE` the user row — `sessions.user_id`
  cascades, so their entire chat history (and its shown-unit memory) is
  destroyed. Irreversible; also silently deletes history the PRODUCER
  might consider part of the archive's story.

Tradeoff, stated plainly: (a) leaves an orphaned account that can no
longer see anything but still exists (and could redeem a future invite);
(b) is clean but destroys conversation history permanently. The plan
builds the Active list WITHOUT the action button, then stops for the
producer's decision before implementing either.

## 4. Build order — each step ends with the suite green

**Step 1 — backend reads for family.** `require_archive_member` +
apply to tree/moments/timeline. Tests: linked family reads the linked
producer's tree/timeline/moments; unlinked family 403; family still 403
on `set_relation`/`remove_relation`/`relation-types`; producer behaviour
byte-identical.

**Step 2 — the family shell.** Remove the /talk redirect for family;
role-filtered nav + view clamp + default `chat`; `FamilyChatView`
(availability gate + mode-routed interfaces). tsc/eslint/build.

**Step 3 — read-only panels.** Hide §1.5's controls for family;
globals.css comment update.

**Step 4 — /talk stub + invite entry in the shell.** Redirect stub;
`buildInviteUrl` → `/?invite=`; shell invite flow (register-default
AuthModal + auto-redeem). Old copied links keep working through the stub.

**Step 5 — `GET /family/members`** + tests (partition matches linkage;
display-name fallback; producer-only).

**Step 6 — invite panel restructure** (two sections, one lifecycle).

**Step 7 — 🛑 STOP: revoke semantics.** Report §3.3 and wait for the
decision. Implement only after confirmation (either way it includes
ending active sessions).

**Step 8 — docs pass + final verification.** PROJECT_STATUS entry;
CLAUDE.md /talk references updated ("/talk" appears in its chat-modes
prose and mic notes — annotate, don't rewrite history); grep gates:
no `'/talk'` route references outside the stub and historical docs;
full suite; build; push. No merges.

## 5. Explicit keep-list

- `TalkInterface` / `VideoClipTalkInterface` / `useVideoClipChat` — they
  ARE the family chat, re-homed not rewritten.
- `GET /family/talk-availability` — still the family chat's gate/mode
  source (name kept; renaming an API for aesthetics breaks nothing but
  certainty).
- The whole invite backend (`family.py`) — extended by one read, changed
  nowhere else.
- `require_producer` on every write everywhere — untouched.
- Producer's own shell experience — byte-identical except the Settings
  invite panel gaining the Active section.

## 6. Residual risks, stated honestly

1. The calm-theme-inside-dark-shell seam (§2.2) is a visual judgement
   call made without the producer seeing it — flagged for live review.
2. Family deep-linking edge cases (stale `?view=record` bookmarks) are
   clamped, but the clamp is client-side; the backend 403s are the
   guarantee, and they are tested.
3. An unlinked-but-logged-in family account (post-revoke, if (a) is
   chosen) sees the redeem screen again — correct, but worth seeing live.
4. `test_family.py`'s availability tests and `test_sessions.py`'s family
   tests all keep passing unchanged — if any needs editing, that is a
   sign the change widened more than planned.
