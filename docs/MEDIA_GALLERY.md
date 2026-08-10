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