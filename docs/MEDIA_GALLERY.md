# Photos on entities and periods, and what an entity-less period shows

**Written 2026-08-06. Updated 2026-08-10 — see §9.** Three related gaps in what a timeline
period or a tree entity can show beyond a name:

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
- **Moment titles** (migration `0023`, `raw_segments.moment_title` +
  language): a recording's only rendered name everywhere, including the
  player — generated once per recording by the same lazy/batched/stored
  seam as §1.6's summaries (batches of 20; unparseable reply leaves NULL
  and retries; untranscribed segments wait for their transcript). The
  question text is model INPUT (it names unnamed referents — CLAUDE.md) but
  never renders. `question_asked` stays in the payload as data.

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

---

## 4. F — a gallery on a period

### 4.1 Where it renders

Beside the timeline period, as the plan says: a strip or grid of thumbnails,
opening a lightbox. Photos belong to the **category**, not to a recording, so
they survive re-recording an answer.

**Hover/select trigger (NEW, see §9.2).** Hovering or selecting an entity or
place sub-bubble within a category should surface that category's gallery
in the side panel — the same gallery described here, triggered by the same
hover/select interaction the sub-bubble already uses, not a separate control.

### 4.2 The interaction the request describes

> "Clicking a specific entity/place within that category shows its own video or
> photos."

This is the same filter §1.2 introduces, with a second tab. Selecting a person
inside a period shows:

- **their recordings** — the moments in this period that mention them, which is
  the §1.2 filter;
- **their photos** — `media_assets` for that entity.

So E and F converge on one panel rather than two: a period shows recordings and
photos; selecting a person narrows both to that person. Nothing new is needed
for the second half once E exists.

### 4.3 Ordering

`taken_year` when set, `created_at` otherwise — and **no ordering by year on
the timeline itself**, per `timeline.py`'s existing rule: *"Years decorate a
sub-bubble; they never move it."* A gallery ordering its own photos is not the
timeline reordering itself.

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
| **1** ✅ | G — recordings as sub-bubbles, people as a filter — DONE 2026-08-10, see §1.5 | No schema, no storage, fixes existing content, and is the surface E and F both hang from |
| **2** | `media_assets` + presign/upload/delete endpoints | The shared foundation |
| **3** | E — entity photo, primary only, on the extraction panel, the tree, and the recording screen (§3.3) | Smallest useful slice of the new table |
| **4** | E everywhere else — timeline bubbles, entity list | Pure rendering once the payload carries `photo_url` |
| **5** | F — period galleries, hover-triggered side panel (§4.1), and the lightbox | Needs 2, benefits from 3's upload UI |
| **6** | Merge-safety: repoint `media_assets` in the merge tooling | Must land before anyone merges entities that have photos |
| **7** | Category year-range attribution (§1.4) | Depends on year-attribution rules being defined first — see §9.1 |
| **8** | `/talk` photo surfacing (§9.4) | Blocked on an explicit scope decision; do not start before sign-off |

Phase 1 is independently useful and worth landing on its own.

## 7. Open decisions

| # | Decision | Recommendation |
| --- | --- | --- |
| 1 | Do periods and entities share one gallery panel, or two? | One — §4.2. Selecting a person narrows the period's content; it does not open a different screen. |
| 2 | HEIC: convert or reject? | Reject with a clear message first; converting needs a codec in the image, which is a deploy change. |
| 3 | Captions on photos? | Yes, optional, one line. A photo of four people is worth naming, and it costs one column already in the schema above. |
| 4 | Can family accounts (`/talk`) see photos? | **Revisited in §9.4 — no longer purely deferred.** The 2026-08-10 request asks for this explicitly; needs sign-off before phase 8, see §9.4. |
| 5 | Should a photo be attachable to a RECORDING as well? | No. A recording already has video. Entity and period are the two things that lack any imagery. |

## 8. Not in scope

- Video uploads beyond the existing recording path.
- Face detection, or matching a photo to an entity automatically.
- Editing or cropping in the browser.
- Anything about years — see `docs/TIMELINE_YEAR_ATTRIBUTION.md`, except §1.4's
  category-range rule, which is in scope as of this update.
- `/talk` photo surfacing, unless and until §9.4 is signed off.

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
additional trigger.

### 9.3 Upload from the recording screen (§3.3, §5)

Adds a second upload entry point: the per-category recording/review screen,
alongside the entity portrait and period "Add photos" button already in the
original plan. Same presign → PUT → ingest flow; no new storage path.

### 9.4 `/talk` photo surfacing — scope decision needed, not yet approved

While a family member is chatting in `/talk` and a clip plays, the request
asks for the bottom video panel to also show photos from whichever
categories the currently-playing clips belong to.

**This conflicts with two things this document already decided**, and should
not be built until explicitly resolved:

- **Open decision #4** (original table) deferred family/`/talk` photo
  visibility entirely, calling it *"a separate call about what the archive
  exposes."* This request effectively answers that decision as "yes" — worth
  making that an explicit, conscious sign-off rather than something that
  slips in as a side effect of building phase 8.
- **Open decision #5** confirmed photos attach to entities and periods, not
  individual recordings. Surfacing "photos for the categories behind the
  clips currently playing" is a new read path (category → photos, filtered
  by what's currently on screen) that doesn't exist yet in the API and isn't
  the same as the producer-side gallery in §4.

Before building: decide (a) whether family/`/talk` should see photos at all,
(b) if yes, whether this needs a dedicated endpoint (e.g. "photos for the
categories behind the currently-playing clip(s)") rather than reusing the
producer-side gallery response, and (c) whether this should wait until
phases 1–6 (producer-side photos) are stable, since it's a new
consumer-facing surface built on top of a feature that doesn't exist yet.

Tracked as phase 8 in §6, gated on this sign-off.