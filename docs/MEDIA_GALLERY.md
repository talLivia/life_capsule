# Photos on entities and periods, and what an entity-less period shows

**Written 2026-08-06. Updated 2026-08-10 — see §9. Updated 2026-08-11 — see §9.5.**
Three related gaps in what a timeline period or a tree entity can show beyond
a name:

| | |
| --- | --- |
| **E** | Any entity: upload a photo, shown on its card wherever it renders |
| **F** | Any period: upload several photos, shown as a gallery beside it |
| **G** | A period whose recordings named nobody currently shows NOTHING |

G is first in this document because it is a bug with existing content, it needs
no new storage, and it is the smallest of the three. E and F are features.

---

## 1. G — a period with no named entities shows nothing

### 1.1 What happens today

`timeline.build_timeline` builds each period, then fills its sub-bubbles from
`_people_in()`, which is a join through `entity_mentions`. A period whose
recordings mention nobody gets `people: []` and renders as an empty bubble.

Measured on the live archive — four of twelve recordings extract nothing, and
three of those are CORRECT:

| recording | transcript | entities |
| --- | --- | --- |
| `368642ef` | "כשהייתי ילד הייתי… לשחק עם חברים בחוץ" | none, correctly |
| `8903b346` | "בבית דיברנו עברית… אנגלית" | none, correctly |
| `d34f4645` | "הבית עד שהייתי בן 3…" | none, correctly |

A childhood-hobbies answer names no people and no places. Nothing is wrong with
the extraction; the sub-bubble system simply has no other content type.

This affects a large share of the questionnaire by design — philosophy of life,
reflections, hobbies, what you'd want your grandchildren to know. Those
questions are asked precisely because the answer is not a list of names.

### 1.2 The fix: sub-bubbles are RECORDINGS, and people are a lens on them

Both suggested options work. The second is the one that fits, and the reason is
that the current design has the relationship inverted.

A period's actual content is its **recordings**. A person is a way of
navigating them — "show me the moments about אמנון" — not the content itself.
Today an unnamed recording is unreachable because the only route in is a person,
which makes people load-bearing for something they were never meant to carry.

So:

- **every** period lists its recordings as sub-bubbles, titled by their
  interview question, playable directly;
- people remain sub-bubbles too, and clicking one **filters** the recordings to
  those mentioning them.

A period with no entities then shows its recordings, exactly like every other
period, with no special case anywhere. A "moments" bubble per category would
also work, but it would be a second kind of container that exists only for the
empty case — and the empty case would still look different from the full one.

**Rejected: hiding entity-less periods.** `timeline.py` already hides periods
with no recordings, and states the rule: *"A category with no recordings is a
question not yet answered, not a fact about the life."* A period WITH a
recording is the opposite — the producer answered, and the answer must be
reachable.

### 1.3 Cost

`_people_in` gains a sibling, `_recordings_in`, returning
`{segment_id, question_asked, question_id, created_at, take_index}` per period.
No schema change, no new storage, no migration. The panel gains a list and a
filter interaction.

### ✅ 1.5 Built — 2026-08-10

`_recordings_in` landed as a pure function over the segments `build_timeline`
already loads — the rows were in hand, so a query would have been a second
read of the same data. Three additions to the §1.3 tuple, each because the
spec's own requirements need them:

- **`video_url`** — "playable directly" needs the URL; served as stored,
  exactly as `get_entity_moments` already does. `take_index` is derived from
  `created_at` order within a question (CLAUDE.md's rule — there is no take
  column), and **`take_count`** rides along so the client can label
  "take 2 of 3" without counting.
- **`segment_ids` on each person** — the filter needs to know which
  recordings mention whom. Carried in the payload `_people_in` already joins
  for, so selecting a person filters client-side with no second request and
  no new endpoint. (`_people_in`'s aggregation moved from SQL to Python for
  this; same output order, mentions desc then name.)

The panel: recordings render as rows under each period, titled by their
question; person chips toggle a filter scoped to the period they were clicked
in (a person in two periods filters only the one clicked); the sticky aside
became the player. The aside no longer calls `/entities/{id}/moments` — the
endpoint stays, the tree still uses it.

Deliberately unchanged, noted rather than slipped in: `build_timeline` has
never filtered on `status`, so recordings mid-processing (or failed) were
always counted and are now visible as rows — a mid-flight one shows "still
being processed" in the player. If failed segments should be excluded, that
is a change to the period counts too, and its own decision.

### ✅ 1.6 The compact card — built 2026-08-10, same day

§1.5 as shipped rendered every recording and every chip by default — correct
data, wrong altitude: on a real archive every category arrives as a wall.
Redesigned the same day, decisions approved before building:

**The default card** is category title, one generated sentence, and a handful
of grouped bubbles. The §1.5 lists (per-entity chips + playable recordings)
are the EXPANDED view — click the card, or click a group bubble to arrive
already narrowed to it. A person chip narrows within a group. Nothing from
§1.5 was discarded; it moved behind the expand.

**Summary sentences: stored, refreshed on read** (approved over generate-live
and generate-at-ingest). `period_summaries` table (migration `0022`), one row
per (producer, category) with the source segment ids and language as the
watermark — regenerate exactly when those change, serve from Postgres
otherwise, so repeat views cost zero LLM calls and deletion can never leave a
sentence describing footage that is gone. On generation failure the stale
sentence is served (beats a blank card) and the unchanged watermark retries
next read. Not cache_service, deliberately: without Redis it silently no-ops
and dev would regenerate on every view with nothing surfacing the cost.

**Grouping: relations + a classified subtype** (approved over type-only).
Family is membership in a family-category relation, never the person type —
measured on live data this correctly puts the one relation-less person
(אמנון) in "אנשים". `type` cannot say "school" (three schools, the air force
and a college all share `organisation`), so `entities.subtype` (migration
`0022`, closed CHECK'd vocabulary: school / higher_education / military /
workplace / community / other) is assigned by one batched LLM call over
name + mention summaries. NULL vs 'other' is the never-asked vs
asked-and-unknown split the `*_asked_at` columns use: 'other' is never
re-sent, an unparseable label leaves NULL and retries. Group order:
משפחה, אנשים, מקומות, מוסדות לימוד, צבא, עבודה, קהילה, עוד.

> **Superseded by §1.8** — the subtype classification pass and this grouping
> scheme were replaced by topic_tags-based bubbles the next day. Kept here
> for history; do not implement this version.

**One deviation from the approved wording, flagged at build time:**
classification runs lazily at READ (the same seam as the summary refresh),
not at ingest + a backfill script. Identical outcome and cost — the first
load classifies the existing organisations, later loads classify only what
is new — and it keeps this out of `analysis_graph`, which is unsafe to edit
while a recording is in flight (§9.7 of FAMILY_TREE_TIMELINE.md).

**Year range: omitted** until §1.4 is built, per the approved decision — the
only year in the live archive is צבי's birth 1956, the producer's father's,
sitting in a childhood recording: deriving a range from entity years would
ship precisely the bug §1.4 exists to prevent. The header slot appears when
producer-scoped attribution lands.

### ✅ 1.7 The constant-shape principle — built 2026-08-11

§1.6 shipped the compact card but its expanded state was still Phase 1's
flat lists, and the collapsed header still counted questions. Restated as a
GENERAL principle, to hold for every category at every archive size:

> A period's default view is a summary, not a listing. The collapsed shape
> is constant — title, year range (or nothing), one sentence, a few grouped
> bubbles — whether the category holds 3 recordings or 50. Volume is
> absorbed by grouping and summarization, never expressed as more chips,
> rows, or text. Raw interview-question text never renders by default at
> any archive size. One click deep is still curated — constant shape, not a
> proportionally longer list.

**What renders where now:**

- **Collapsed:** title · summary sentence · bubbles. Counters removed —
  under the strict reading "3 questions answered · 5 recordings" is a
  listing leak too. The card is static (approved): bubbles are the ONLY way
  in.
- **Every period leads with a רגעים bubble.** With a static card this is the
  only route into an entity-less period's recordings — the G bug §1 fixed,
  re-solved one layer up. Not the container §1.2 rejected: that objection
  was to a bubble existing only for the empty case; this one is uniform.
- **Bubble click → capped highlights** (4), approved mechanism "title once +
  ranked selection": ranked by the `importance_score` ingestion already
  computed (zero LLM at read), diversified so one much-discussed person
  cannot fill every slot, presented chronologically — the cap decides WHAT,
  recording order decides WHERE. Captions quote the stored
  `entity_mentions.summary` for the group's most-present member.
- **"All moments" one level deeper** (approved): the complete list plus the
  Phase 1 per-entity chips, so §1.2's reachability rule still holds and
  density is opt-in.

  > **Superseded by §1.9** — "all moments" moved from bubble-scoped to
  > period-scoped, then was removed entirely. Kept here for history.

- **Moment titles** (migration `0023`, `raw_segments.moment_title` +
  language): a recording's only rendered name everywhere, including the
  player — generated once per recording by the same lazy/batched/stored
  seam as §1.6's summaries (batches of 20; unparseable reply leaves NULL
  and retries; untranscribed segments wait for their transcript). The
  question text is model INPUT (it names unnamed referents — CLAUDE.md) but
  never renders. `question_asked` stays in the payload as data.

  > **Superseded by §1.10** — title generation moved from read-time
  > (watermarked) to save-time (single generation point). Kept here for
  > history.

**Costs this adds:** one titling call per ~20 recordings, once ever per
recording; highlight selection is free at read. Summary/subtype costs
unchanged from §1.6.

### ✅ 1.8 Bubbles from topic_tags; titles watermarked — built 2026-08-11

Two directed corrections to §1.6/§1.7, decisions approved before building.

**The subtype classification pass is GONE** (migration `0024` drops
`entities.subtype` and its CHECK; the five classified rows were derived
data with no other consumer). Bubbles now come straight from the
`topic_tags` ingestion already writes per segment — the school segment was
already tagged 'בתי ספר', the army segment 'שירות צבאי', so the label IS
the tag and the classification call bought nothing the archive didn't know.
The relations-derived משפחה bubble is also gone (approved: tags only —
'משפחה' appears when recordings are tagged with it; the relations view
lives on the family tree page). Measured caveat that shaped the mechanism:
43 of 47 distinct live tags appear exactly ONCE, so raw aggregate-and-dedupe
would be the density bug in bubble form. Bubbles are therefore the top
**coverage-ranked** tags (how many of the period's recordings carry the
tag), capped at 5, ties by first appearance. Free-form tags do not
self-dedupe across phrasings (סבתא vs סבתות) — accepted, coverage ranking
hides most of it at scale.

Two consequences, both deliberate:

- **"All moments" is now PERIOD-wide, not bubble-wide.** A capped tag set
  covers less than everything, so reachability moved down one level: any
  open bubble's "all moments" lists the whole period. §1.2 still holds.

  > **Superseded by §1.9** — the period-wide "all moments" screen was
  > removed entirely the same day. Kept here for history.

- **The generic רגעים bubble survives only as a fallback** for a period
  whose segments carry no tags at all (mid-processing, pre-topics archive)
  — with a static card, a period must never render without a way in.
  Where tags exist, real tag content replaces it entirely.

Highlight diversification changed from entity-coverage to QUESTION
coverage — takes of one question are near-duplicates, and three takes of
one answer must not fill the slots.

**Update, later the same day — bubbles are DISJOINT.** A recording tagged
both משפחה and טבריה appeared under both bubbles, which read as a
duplication bug. Bubbles now PARTITION the recordings: tags are chosen
greedily by how many still-unassigned recordings they cover (set cover,
ties by first appearance), each chosen tag claims its unassigned
recordings, and a tag left covering nothing new never renders as an empty
echo. Counts are exclusive and sum to at most the period's recordings.

### ✅ 1.9 The partition is total; bubbles are the sole route — built 2026-08-11

Directed follow-up to §1.8's partition, closing the reachability model:

- **A catch-all bubble makes the partition TOTAL.** Recordings whose tags
  lost the 5-bubble cap, and untagged recordings alike, land in **עוד** —
  appended after the tag bubbles. When it is the ONLY bubble (nothing
  tagged yet) it is labelled **רגעים** instead: "more" than nothing reads
  wrong. Every recording now belongs to exactly one bubble, guaranteed by
  `test_leftovers_land_in_a_catch_all_bubble_nothing_is_stranded`, which
  asserts the exact partition — none stranded, none duplicated.
- **The period-wide "all moments" screen is GONE**, and with it the Phase 1
  per-entity filter chips. Bubbles are the sole navigation into a period's
  recordings; "all N moments" inside an open bubble now reveals the
  BUBBLE's own full list, nothing wider. §1.2's reachability rule holds
  through the partition instead of through an escape hatch.
- **The payload no longer carries `people`.** Nothing rendered it once the
  chips went. `_people_in` survives internally as the caption source (a
  highlight quotes what a recording said about its most-mentioned person);
  entity-centric browsing lives on the family tree page, which has its own
  endpoint.

### ✅ 1.10 One title generation point — built 2026-08-11

Directed consolidation: §1.7/§1.8's read-time title mechanism (generate
lazily at timeline read, watermarked by transcript hash and language) was a
second source of truth beside the pipeline, and the recording screen still
said "Take 1 / Take 2". Now there is ONE generation point and two readers:

- **Generated at save**, inside `extract_topics_node` — the same pipeline
  run that writes `topic_tags`, same non-fatal contract, one plain-text
  call (no batching, no JSON mapping — there is exactly one segment at
  save). Deliberately NOT a new graph node: adding one changes the graph
  topology under any segment paused at `human_confirm` (the §7 mid-flight
  hazard), and "Finding the themes" honestly covers both calls. A
  re-record is a new segment and titles itself on its own way through; an
  in-place re-analysis re-runs this node and re-titles naturally — better
  than the watermark, which only detected the change at the next read.
- **The recording screen shows the title** instead of "Take N of M",
  falling back to the take label only while no title exists
  (mid-processing, or a failed generation). The extraction modal header
  uses it too. The timeline reads the same stored value as-is — zero
  generation logic in the read path, pinned by
  `test_the_timeline_never_generates_titles`.
- **Migration `0025` drops the watermark columns** (`moment_title_source`,
  `moment_title_language`); `moment_title` and its values stay.

Two accepted costs, named rather than buried: a title call that fails at
save is not retried — that recording keeps its take-label fallback until
re-saved; and a language switch no longer re-titles old recordings
(summaries still regenerate; titles keep their save-time language).

**Titles regenerate with their words** (migration `0024`,
`raw_segments.moment_title_source` = sha256 of the transcript the title was
generated from). §1.7's freshness rule was "a title exists", which could
not see an in-place transcript change (re-analysis) — the title would
outlive the words it named. Now staleness is: no title, language changed,
or transcript hash mismatch — the same watermark pattern as
period_summaries, per recording. Noted for accuracy: new and re-recorded
takes are new rows and always titled fresh; this fixes only the in-place
case, and nothing regenerates on unrelated saves. Existing titles were
backfilled with their hash in the migration (they WERE generated from the
transcript in the row), so nothing regenerates on the first read after
upgrade.

### 1.4 Year range per category — "about the producer" only (NEW, see §9.1)

Each category bubble should also show an earliest–latest year range on the
timeline. The earliest year must come from content **about the producer
themselves** (their birth year, when they started school, etc.) — never from
the birth year or life events of someone else mentioned inside that category's
recordings. A grandmother born in 1920 referenced in a "family" recording must
not push that category's start year back to 1920; the range describes the
producer's own timeline, not the oldest fact anyone happened to say.

This depends on however year-attribution currently distinguishes "a year
about the producer" from "a year about someone else in the same recording" —
see `docs/TIMELINE_YEAR_ATTRIBUTION.md`. If that distinction doesn't already
exist, it needs to be defined explicitly (not guessed at as a heuristic)
before this can be built, since silently misattributing a year is worse than
not showing one.

---

## 2. Where photos live

Shared by E and F, and settled first because both depend on it.

### 2.1 Storage: the existing path, unchanged

`storage_service` already does exactly this job for video —
`upload_file`, `presigned_url`, `delete_file`, with an R2/S3 implementation and
a local-disk one behind `USE_LOCAL_STORAGE`. Photos use it as-is.

Key layout, mirroring how video keys are built:

```
photos/{producer_id}/entity/{entity_id}/{uuid}.jpg
photos/{producer_id}/period/{category}/{uuid}.jpg
```

**No second upload path.** CLAUDE.md's rule for video — *"presign → PUT →
ingest… there is no second ingestion path; if you find yourself writing one,
something is wrong"* — applies here for the same reason, and photos should
follow the same presign-then-PUT shape rather than posting bytes through the
API.

### 2.2 Schema: ONE table, not two

```sql
CREATE TABLE media_assets (
    id              varchar PRIMARY KEY,
    producer_id     varchar NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    storage_key     varchar NOT NULL,
    kind            varchar NOT NULL,     -- 'photo' for now
    caption         text,
    taken_year      integer,
    created_at      timestamptz NOT NULL DEFAULT now(),

    -- EXACTLY ONE of these is set.
    entity_id       varchar REFERENCES entities(id) ON DELETE CASCADE,
    category        varchar,              -- an interview_config category id

    CONSTRAINT ck_media_one_owner CHECK (
        (entity_id IS NOT NULL AND category IS NULL) OR
        (entity_id IS NULL     AND category IS NOT NULL)
    )
);
```

One table with a CHECK, rather than `entity_photos` and `category_photos`. The
two would share every column and every code path except the owning id, and
"two places hold one kind of fact" is the shape PROJECT_STATUS already argues
against for `relation_types`. The CHECK is what stops a row claiming both.

**`category` is a string, not an FK**, because categories live in
`interview_questions.json` and not in the database — the same reason
`RawSegment.question_id` is a bare string. It carries the same risk and takes
the same mitigation: a category that is renamed orphans its photos, so the
value must come from `interview_config` and never from client input.

### 2.3 What happens on the merges and deletes that already exist

The question worth asking, and the reason `entity_id` is a real FK.

**Entity merge (identity confirmation).** Confirming "איציק כהן is the איציק you
already have" is applied by RENAMING, and the merge key does the rest — so the
surviving row keeps its id and the losing entity is never written at all. A
photo attached to an entity that later merges therefore needs no special
handling **on the normal path**: there was only ever one row.

The manual merge run earlier today is the case that DOES need handling — two
rows already existed, mentions were moved, one row was deleted. `ON DELETE
CASCADE` would have destroyed that entity's photos. So:

> **Any merge tool must repoint `media_assets.entity_id` before deleting the
> losing row**, exactly as it repoints `entity_mentions`. Worth writing into
> `scripts/` alongside the merge, not left to be remembered.

**Orphan sweep.** `delete_orphaned_entities` removes an entity no recording
mentions any more. Its photos cascade with it — correct: the person is gone
from the archive, and a photo of nobody is not worth keeping.

**Storage is outside the transaction**, as PROJECT_STATUS already records for
video: *"No transaction spans Postgres and object storage."* A cascaded row
leaves its file behind. Same accepted trade — an orphaned file costs storage,
where a deleted-file-with-live-row costs a broken image.

### ✅ 2.5 Phase 2 built — 2026-08-11

Migration `0026`, `MediaAsset` in `models.py`, and `app/api/v1/media.py`
(`POST /media/presign`, `PUT /media/upload-local/{key}` for dev,
`POST /media`, `GET /media?entity_id=|category=`, `DELETE /media/{id}`).
25 tests. Decisions the plan left open, settled while building:

- **The row-write reads the owner from the STORAGE KEY, never from the
  request body.** The key encodes `entity/{id}` or `period/{category}` at
  presign, so "row claims a different owner than the upload it points at"
  is structurally impossible rather than validated against.
- **`is_primary` landed now, not in Phase 3** (schema shouldn't move
  twice), with two pieces of bookkeeping so an entity with photos never
  renders faceless: the FIRST photo of an entity becomes primary
  automatically, and deleting the primary promotes the earliest survivor.
  Category photos never get one — a period has no face.
- **`GET /media` is included in the foundation** (the doc's Phase 2 named
  only presign/upload/delete): every later phase reads through it, and
  §9.4 already names it as /talk's read shape.
- **Retired-only categories are valid photo owners.** They still render on
  the timeline (FAMILY_TREE_TIMELINE.md §3 correction), so a photo must be
  attachable there. `interview_config.is_valid_category` is the single
  seam, live ∪ retired.
- **The size cap (15MB, `MAX_PHOTO_UPLOAD_BYTES`) is enforced at the
  local PUT and re-checked at the row-write via a HEAD on the object** —
  in R2 mode the browser PUTs straight to storage, so the row-write check
  is the only one that always holds. An oversized object is deleted when
  the row is refused, not left parked in storage.
- **HEIC is rejected with a message that names it** (open decision 2's
  recommended first step) — a generic "unsupported type" on the format
  every iPhone produces would read as the app being broken.
- Deletion removes the row transactionally, then the file best-effort —
  the §2.3 trade, orphaned file over broken image.

No frontend yet — the upload UI is Phase 3's, where the first surface
(extraction panel / recording screen) lands.

---

## 3. E — a photo on an entity

### 3.1 Where it renders

An entity card already exists in four places, and all four read the same shape:

| surface | today | with a photo |
| --- | --- | --- |
| family tree | name + type | a small round portrait on the node |
| timeline sub-bubble | name + mention count | the same, as the bubble's face |
| extraction panel | name, kind, summary | a thumbnail beside the name |
| entity list (`/api/v1/entities`) | name, type, years | a thumbnail |

One `photo_url` on the entity payload feeds all four. It should be resolved
server-side into a presigned URL, not a raw key — the client already never
sees storage keys anywhere else.

> **Note added 2026-08-11 (§9.5):** the "timeline sub-bubble" row above
> describes the pre-§1.8 per-entity chip, which no longer exists on the
> timeline (bubbles are now tag-partitioned, not per-entity — see §1.8/§1.9).
> An entity's photo still applies wherever an entity card itself renders
> (family tree, extraction panel, entity list); on the timeline it surfaces
> through the period gallery (§4), not a per-entity bubble face.

### 3.2 One photo or many

**One primary photo per entity**, with more allowed but one marked primary. The
tree and the timeline need a single face for a node; a gallery needs an order.
Modelled as `media_assets.is_primary boolean`, with a partial unique index —
the same pattern `entities.is_self` already uses for "one per producer":

```sql
CREATE UNIQUE INDEX uq_media_primary_per_entity
    ON media_assets (entity_id) WHERE is_primary;
```

### 3.3 Uploading

From the entity card itself — click the empty portrait, pick a file. The
extraction panel is the natural first home, since it is already the screen for
"what the archive understood about this recording", and it is read-only today
in a way that has already been noted as a gap.

**Also from the per-category recording screen (NEW, see §9.3).** The same
upload flow (click empty portrait / "Add photos") should also be available
directly on the recording/review screen for a category, so a producer can
attach photos in the same session as recording that category's answers, not
only afterward from the timeline or extraction panel.

### ✅ 3.4 Phase 3 built — 2026-08-11

Built against §9.5's corrected wording and §9.6's placement decisions:

- **`photo_url` rides on entity payloads** through one new seam,
  `services/media_store.primary_photo_urls` — ONE bulk query however many
  entities, resolved serving URLs, absent entry = render the placeholder.
  Wired into the tree (`TreePerson.photo_url`) and the extraction panel
  (`ExtractedEntityResponse`, which now also carries `entity_id`: the
  portrait upload needs a row to attach to, and a NAME is not a handle —
  two people can share one).
- **One portrait control** (`components/media/EntityPortrait`): photo or
  initials in a circle, click to upload, shared by the extraction panel
  rows and the tree's person card — §9.6's never-a-second-mechanism rule,
  enforced by there being one component. The tree NODE renders the photo
  inside the existing 16px-radius SVG circle via a shared clipPath; same
  size, same position, initials remain the placeholder.
- **Uploading from the portrait makes the new photo the face**
  (`make_primary` on the row-write, demoting the old primary in the same
  transaction) — a portrait upload that stayed invisible would read as the
  upload failing. First-photo auto-primary is unchanged.
- **The recording screen's zone** (`components/media/CategoryPhotoZone`)
  sits below the recording area, keyed by CATEGORY: thumbnails + add +
  two-click remove, uploading category-owned photos (§2.2). Switching
  questions inside a category neither moves nor reloads it.
- The extraction panel stays read-only about what was SAID — a photo is
  producer-added context, not an edit of the extraction.

Phase 4's remainder is now just the entity list (`/api/v1/entities`)
payload + rendering; the timeline face per §3.1's note arrives with the
Phase 5 gallery work.

---

## 4. F — a gallery on a period

### 4.1 Where it renders

Beside the timeline period, as the plan says: a strip or grid of thumbnails,
opening a lightbox. Photos belong to the **category**, not to a recording, so
they survive re-recording an answer.

**Trigger, updated 2026-08-11 (§9.5) for the tag-bubble model.** The
original wording below (2026-08-10) described hovering an "entity or place
sub-bubble" — that surface no longer exists (§1.8/§1.9 replaced per-entity
chips with tag-partitioned bubbles: משפחה, טבריה, מוסדות לימוד, עוד, etc.).
The trigger is now: **hovering or clicking a period's tag bubble** (or the
period card itself, for the period-wide gallery) surfaces that category's
photo gallery in the side panel, alongside the curated highlight clips the
bubble already shows. Same panel, same data source (`media_assets` filtered
by `category`), no filtering by the bubble's specific tag — a category has
one photo gallery, not one gallery per bubble, since photos attach to the
category as a whole (§2.2), not to a tag or entity within it.

> Original 2026-08-10 wording, superseded by the paragraph above: "Hovering
> or selecting an entity or place sub-bubble within a category should
> surface that category's gallery in the side panel — the same gallery
> described here, triggered by the same hover/select interaction the
> sub-bubble already uses, not a separate control."

### 4.2 The interaction the request describes

> "Clicking a specific entity/place within that category shows its own video or
> photos."

This is the same filter §1.2 introduces, with a second tab.

> **Note added 2026-08-11 (§9.5):** as written, this described the
> per-entity filter from Phase 1, which §1.9 removed in favor of tag
> bubbles. The photo side of this interaction still holds at the category
> level (§4.1) but no longer narrows to an individual entity/place inside a
> bubble, since that filter no longer exists on the timeline. Entity-level
> photo+video browsing (a specific person's own moments and photos) lives
> on the family tree page, consistent with where entity-centric browsing
> already moved in §1.9.

### 4.3 Ordering

`taken_year` when set, `created_at` otherwise — and **no ordering by year on
the timeline itself**, per `timeline.py`'s existing rule: *"Years decorate a
sub-bubble; they never move it."* A gallery ordering its own photos is not the
timeline reordering itself.

### ✅ 4.4 Phase 5 built — 2026-08-11

Against the corrected §4.1 wording, in `TimelinePanel` plus one new shared
component (`components/media/PhotoLightbox`, built for reuse by /talk's
Phase 8 gallery). No backend changes — the gallery reads the existing
`GET /media?category=`, whose ordering already implements §4.3.

- **Trigger, as corrected:** hovering a period CARD activates its gallery
  in the side panel (§9.2 — bubbles sit inside the card, so hovering a
  bubble is hovering the card: same gallery, the one-per-category rule
  made structural); clicking a bubble PINS it. Hover-away does not clear —
  a gallery that vanishes while the pointer travels to the panel can
  never be clicked. Galleries are cached per category for the page's life.
- **The gallery accompanies the player, never replaces it** — a thumbnail
  grid card under the video panel, headed by the period's label.
- **Lightbox:** pinned photo deck (a hover elsewhere cannot swap it under
  the viewer), caption + taken_year (open decision 3's caption rendering),
  arrow-key and button navigation, Esc/backdrop close.
- **Empty states follow §1.7's altitude rule:** a merely-hovered chapter
  with no photos shows NOTHING; a deliberately opened one shows the §5
  "Add photos" entry point (`AddPeriodPhotos`, the same presign→PUT→row
  flow) instead of silence. The gallery card itself also carries a
  compact add control.

---

## 5. The upload UI

The same shape everywhere, because there is one storage path:

1. Click an empty portrait, "Add photos" on a period, or the upload control on
   a category's recording screen (§3.3).
2. Client asks for a presigned PUT (`POST /media/presign` with the owner).
3. Client PUTs the file to storage directly.
4. Client calls `POST /media` with the key, which writes the row.

Constraints worth setting before building rather than after:

- **Accepted types**: `image/jpeg`, `image/png`, `image/webp`. HEIC is what an
  iPhone actually produces and browsers cannot display it — either convert
  server-side or reject it with a message that says so, but do not accept it
  silently and store something nothing can render.
- **Size cap** at upload, enforced server-side when the row is written, not
  only in the browser.
- **A photo is not a question.** It never appears in the confirmation screen,
  never counts toward "N things to check", and never pauses ingestion.

---

## 6. Build order

| Phase | Work | Why here |
| --- | --- | --- |
| **1** ✅ | G — recordings as sub-bubbles, people as a filter — DONE 2026-08-10, see §1.5 (superseded by §1.7–1.9) | No schema, no storage, fixes existing content, and is the surface E and F both hang from |
| **2** ✅ | `media_assets` + presign/upload/delete endpoints — DONE 2026-08-11, see §2.5 | The shared foundation |
| **3** ✅ | E — entity photo, primary only, on the extraction panel, the tree, and the recording screen (§3.3) — DONE 2026-08-11, see §3.4 | Smallest useful slice of the new table |
| **4** ✅ | E everywhere else — CLOSED 2026-08-11 as satisfied by Phase 3, decision by the producer: §3.1's "entity list (`/api/v1/entities`)" surface was never built (no such endpoint, nothing renders an entity list — verified), and the timeline face moved into Phase 5's gallery per §9.5. Every entity card that actually exists carries the photo. If a `GET /api/v1/entities` list is ever built, it carries `photo_url` from day one via `media_store.primary_photo_urls`. | Pure rendering once the payload carries `photo_url` |
| **5** ✅ | F — period galleries, tag-bubble-triggered side panel (§4.1, updated), and the lightbox — DONE 2026-08-11, see §4.4 | Needs 2, benefits from 3's upload UI |
| **6** | Merge-safety: repoint `media_assets` in the merge tooling | Must land before anyone merges entities that have photos |
| **7** | Category year-range attribution (§1.4) | Depends on year-attribution rules being defined first — see §9.1 |
| **8** ✅ | `/talk` photo surfacing (§9.4) — DONE 2026-08-11, see §9.4's build note | Waits on phases 1–6 (producer-side photos must exist before they can surface in chat), not on further sign-off |

Phase 1 is independently useful and worth landing on its own.

## 7. Open decisions

| # | Decision | Recommendation |
| --- | --- | --- |
| 1 | Do periods and entities share one gallery panel, or two? | One — §4.2. Selecting a person narrows the period's content; it does not open a different screen. (Note: the "selecting a person" narrowing no longer exists per §9.5 — see §4.2's 2026-08-11 note. The one-panel answer still holds at the category level.) |
| 2 | HEIC: convert or reject? | Reject with a clear message first; converting needs a codec in the image, which is a deploy change. |
| 3 | Captions on photos? | Yes, optional, one line. A photo of four people is worth naming, and it costs one column already in the schema above. |
| 4 | Can family accounts (`/talk`) see photos? | **APPROVED 2026-08-11 — see §9.4.** Yes: `/talk` shows a photo gallery under the video panel, sourced from the category/categories of the clip(s) answering the current chat turn. Still sequenced as phase 8 — built after phases 1–6 are stable. |
| 5 | Should a photo be attachable to a RECORDING as well? | No. A recording already has video. Entity and period are the two things that lack any imagery. |

## 8. Not in scope

- Video uploads beyond the existing recording path.
- Face detection, or matching a photo to an entity automatically.
- Editing or cropping in the browser.
- Anything about years — see `docs/TIMELINE_YEAR_ATTRIBUTION.md`, except §1.4's
  category-range rule, which is in scope as of this update.
- `/talk` photo surfacing is now in scope (§9.4, approved) but sequenced as
  Phase 8 — not to be started before Phases 1–6 land, since there would be
  no photos yet to surface.

---

## 9. 2026-08-10 amendments

Four points raised after the original plan, folded into the sections above.
Recorded here as a change log so the reasoning isn't lost in the diff.

### 9.1 Category year range must be producer-scoped (§1.4)

New requirement, not in the original plan: category bubbles need an
earliest–latest year range, and the earliest year must be tied to the
producer's own life events, not the earliest year mentioned by anyone in that
category's recordings. This needs a defined way to distinguish "a year about
the producer" from "a year about someone else" before it's built — flagged as
a dependency on `docs/TIMELINE_YEAR_ATTRIBUTION.md`, not yet resolved here.

### 9.2 Gallery on hover/select, not just click (§4.1)

Clarifies that the gallery+filter panel from §4.2 should also appear on
hover, not only on an explicit click/select — same panel, same data, an
additional trigger. (See §9.5 for the 2026-08-11 update to what the trigger
target actually is post-tag-bubbles.)

### 9.3 Upload from the recording screen (§3.3, §5)

Adds a second upload entry point: the per-category recording/review screen,
alongside the entity portrait and period "Add photos" button already in the
original plan. Same presign → PUT → ingest flow; no new storage path.

### 9.4 `/talk` photo surfacing — APPROVED 2026-08-11, this is part of the plan

**This is one of three surfaces this whole document exists to build, and all
three are the plan, not just the producer-side ones:**

1. **Recording screen** (§3.3, §9.3) — a producer can upload photos for a
   category directly while recording/reviewing that category's answers.
2. **Timeline** (§4, §9.5) — those photos show as a gallery beside the
   category on the producer's own timeline.
3. **`/talk` chat** (this section) — while a family member is chatting and a
   clip plays, the video panel also shows a photo gallery underneath it,
   sourced from whichever category(ies) the clip(s) that answered the
   current chat turn belong to. If one answer pulled clips from multiple
   categories (e.g. a chat response that drew on both "ילדות" and
   "צבא/מחתרות"), the gallery shows photos from all of those categories,
   not just one.

This was previously recorded as an unresolved conflict with two earlier
open decisions. Resolving both explicitly, now that this has been
confirmed as the intended plan rather than a late addition:

- **Open decision #4** ("can family accounts see photos?") is now **YES** —
  not deferred. Family sees the same category-level photos a producer
  uploaded, surfaced contextually during chat.
- **Open decision #5** ("photo attach point") is unchanged: photos still
  attach to entities and periods/categories (§2.2), never to an individual
  recording. What's new here is a **read path**, not a new attach point —
  `/talk` needs to know, for the clip(s) currently playing, which
  category(ies) they belong to, then fetch that category's existing
  `media_assets` gallery. No new storage shape.

**What this needs, mechanically:**

- A clip already carries its `category` (it's a recording within a period).
  Given the clip(s) driving the current chat turn, the categories are known
  without new inference — it's a lookup, not a classification.
- A dedicated read endpoint (e.g. `GET /media?category=X` reused across
  producer-timeline and `/talk`, or a small `/talk`-specific wrapper that
  takes the current turn's clip ids and returns the union of their
  categories' galleries) rather than repurposing the producer-side gallery
  response as-is, since `/talk` needs multiple categories unioned per turn,
  not one category per request.
- Family-side access control: confirm `/talk`'s existing auth/session model
  extends cleanly to `media_assets` reads (same producer-scoping question
  video already answers today for family viewing) — not a new access
  model, just confirming the existing one covers photos too.

**Sequencing, unchanged from the original recommendation:** this is still
Phase 8 in §6 — built after phases 1–6 (producer-side upload, storage, and
the timeline gallery) are stable, since `/talk` surfacing has nothing to
show until photos exist to surface. The approval here is about **intent**
(yes, build this), not about **jumping the queue** — Phase 2 (in progress)
and Phases 3–6 still come first.

### ✅ Phase 8 built — 2026-08-11

All three mechanical needs above, resolved as anticipated:

- **The categories are a lookup, made where the clips are chosen.**
  `video_clip_assembler.photo_categories_for_segments` resolves the final
  clips' `raw_segment_id → question_id → category_for_question_id` (live
  or retired), deduped, in the order the answer plays its footage — so the
  gallery leads with the period the clip opens in and can never disagree
  with it. Both modes set `VideoClipResult.photo_categories`; the WS
  `video_clip_response` message carries it. A segment with no question id
  or an unresolvable one contributes nothing — never a guess.
- **The read endpoint is the existing `GET /media?category=`**, called
  once per category by the client and unioned there (deduped by id,
  category order), with a per-category cache for the conversation's life —
  three answers from one period fetch its gallery once.
- **Access control: the existing model, confirmed and extended by one
  read.** A family account's `producer_id` linkage — the same check
  sessions.py runs before a /talk session against that producer's avatar —
  now scopes `GET /media` (list ONLY; every write stays producer-only).
  An unlinked family account gets 403, not an empty list, because an
  empty list would claim the archive has no photos.

UI: `TurnPhotoGallery` renders a thumbnail row under each answer's player
and text in the /talk layout, opening the shared `PhotoLightbox` (§4.4).
While loading, or when the turn's periods hold no photos, it renders
NOTHING — the clip is the answer; photos accompany it when they exist.

**Extended to the producer's own chat screen — producer decision
2026-08-11.** The first live test ran on that screen, where the gallery
was deliberately absent per this section's "/talk" wording — the two
screens share the behaviour hook, not the layout, so `photoCategories`
was arriving and nothing rendered it. Now both layouts mount the ONE
`TurnPhotoGallery` (a `variant` prop keeps the calm theme /talk-only, per
the standing rule): the producer screen shows it under its single video
panel, tracking the clip the panel plays.

**Presentation: an auto-cycling polaroid deck — producer decision
2026-08-11**, replacing the original thumbnail row (after css-tricks'
infinite polaroid slider: cards stacked via grid-area 1/1, thick white
frames, soft shadow). The front card slides out and rejoins the back
every ~3.5s so every photo is seen with no interaction; a caption, when
one exists, sits in the polaroid's bottom margin. Cycling pauses on
hover, while the lightbox is open, and under prefers-reduced-motion; the
reference's pure-CSS per-N keyframes don't fit a dynamic photo count, so
a small timer drives the same motion. Clicking still opens the shared
PhotoLightbox at the photo currently showing. TurnPhotoGallery only —
the timeline period gallery (§4.4) keeps its thumbnail grid unchanged.

Refined the same day, two producer requests: cards are 3:2 LANDSCAPE and
larger (240×160 image area — the classic print proportion; the frame's
shape is the polaroid's, never the file's), and the lightbox shows every
photo inside a FIXED stage (object-contain letterboxing, constant-height
caption band) so stepping through mixed portrait/landscape shots never
moves the nav buttons. The stage fix lives in the shared PhotoLightbox,
so the timeline gallery's viewer gets it too — its thumbnail grid is
untouched.

---

## 9.5 2026-08-11 amendment — §4.1 and §3.1 updated for the tag-bubble timeline

Between the original 2026-08-10 photos plan and this update, Phase 1's
timeline was substantially redesigned (§1.6–§1.10): per-entity chips and the
period-wide "all moments" screen were removed entirely, replaced by
tag-partitioned bubbles (משפחה, טבריה, מוסדות לימוד, עוד, etc.) as the sole
navigation into a period's recordings.

The original photos plan's wording in §3.1 and §4.1 predates that redesign
and referred to "entity or place sub-bubbles," which no longer exist on the
timeline. This amendment corrects those references without changing the
underlying photo architecture (§2's storage/schema plan is unaffected):

- **§3.1**: an entity's `photo_url` still renders on every surface where an
  entity card itself exists (family tree, extraction panel, entity list). It
  no longer has a "timeline sub-bubble" surface to render on, since
  per-entity bubbles don't exist on the timeline anymore.
- **§4.1**: the period gallery's trigger is now hovering/clicking a period's
  tag bubble (or the period card, for the period-wide view) — not an
  entity/place sub-bubble. The gallery itself is unchanged: one gallery per
  category, sourced from `media_assets` filtered by `category`, not
  filtered further by which bubble was hovered (photos attach to the
  category as a whole, per §2.2 — they were never modeled per-entity or
  per-tag).
- **§4.2 / Open decision #1**: the "selecting a person narrows both
  recordings and photos" interaction described here was Phase 1's
  per-entity filter, removed in §1.9. The one-gallery-per-category answer
  to decision #1 still holds; the person-level narrowing does not, and
  person-centric browsing lives on the family tree page instead.

No architectural decisions changed — §2's storage/schema plan is untouched,
and phases 3–8 in §6 keep their order. (§9.4's approval is its own,
separate 2026-08-11 update — an earlier draft of this closing note said
§9.4 "remains unsigned-off", which contradicted the §9.4 header once the
approval landed; corrected.) This is a wording/consistency fix so Phase 3
onward is built against the timeline as it actually exists today, not as
it existed on 2026-08-10.

## 9.6 Phase 3 UI placement — DECIDED 2026-08-11, before building

Two calls the plan had left to "the natural first home" wording, settled
by the producer before Phase 3 was built:

**Recording screen (§3.3/§9.3): one persistent per-CATEGORY photo zone,**
in a fixed area below the video-recording area. Not per-take, not
per-question: whichever question in the category is being answered, the
same zone with the same photos sits underneath the recording UI. The
photos it uploads are CATEGORY-owned (`media_assets.category`, §2.2) —
they belong to the period as a whole, which is exactly why the zone must
not move or reset between questions.

**Family tree (§3.1): the portrait is the EXISTING small circle on the
node** — the one currently showing an initial/placeholder. A primary
photo swaps in at the same size and position; no node redesign, no larger
portrait treatment. Upload is reachable from the entity's box/card on the
tree (clicking the circle or an obvious affordance on the card) and
REUSES the one entity-photo flow and primary model shared with the
extraction panel — never a second upload mechanism.