"""
The fixed template banks and fallback constants for verbatim answers.

⚠️ HISTORY: this module WAS Prompt 8's `assemble_response` — the final
step of the avatar mode's multi-step retrieval pipeline (retrieve →
score_candidates → verbatim transcripts joined by bridge phrases). That
pipeline was retired on 2026-08-19 per docs/AVATAR_SHARED_ENGINE_PLAN.md
§5: the shared engine (full_archive_retrieval.select_units) + the two
renderers (video_clip_assembler, spoken_answer) do the whole job for both
modes. See the pre-retirement history of this file for the deleted
implementation.

The banks and constants STAY HERE, names and home frozen (decided in the
plan's re-verification pass): `video_clip_assembler` and `spoken_answer`
import them, and nothing moving means no import is ever disturbed. The
never-invent rule they encode is unchanged: {entity} is the only thing
ever injected, and only after validation against the archive.
"""

# Fixed answer when no primary segment matches the question's topic at
# all — never an LLM-generated apology/filler, exactly per the project plan.
NO_STORY_FALLBACK = "אין לי סיפור על זה"

# WHAT TO SAY WHEN THE LOOKUP ITSELF FAILED, which is NOT the same thing and
# must never again be said with the same words.
#
# The archive read is fail-soft by design: a family member should get a
# sentence, not a stack trace. But it returned the SAME empty result for "the
# model found nothing" and "the API was down", so both came out as
# NO_STORY_FALLBACK — telling someone their relative has no story about a
# person the archive has twelve units about. That is a false statement about
# somebody's life, produced by an outage.
#
# It also cost three misdiagnoses in one day: two measurements read as clean
# results, and one live report that could not be explained until every other
# cause had been eliminated. PROJECT_STATUS has carried this warning about the
# eval since 2026-07-29; this is the production half of it.
TRANSIENT_FAILURE_FALLBACK = "לא הצלחתי להביא את הסיפור כרגע. אפשר לנסות שוב?"

# Fixed bridge-phrase bank (Hebrew) — {entity} is the only thing ever
# injected; the phrase itself never varies with what actually happened in
# the related segment.
BRIDGE_PHRASE_TEMPLATES = [
    "זה מזכיר לי גם את {entity}...",
    "אגב, יש עוד סיפור על {entity}...",
    "וזה גורם לי לחשוב גם על {entity}...",
]


def _pick_bridge_phrase(index: int) -> str:
    return BRIDGE_PHRASE_TEMPLATES[index % len(BRIDGE_PHRASE_TEMPLATES)]


# "I don't have a story about that" is true but sounds like the system failed
# to understand the question. Asked "what else did you do together?" about a
# specific person, it reads as a shrug — when the honest and much warmer answer
# is "זה כל מה שיש לי על אמנון".
#
# SAME RULE AS THE BRIDGE PHRASES ABOVE, and for the same reason: {entity} is
# the only thing ever injected, and it is a name the archive really holds,
# validated before it gets here. The sentence itself never varies with what
# happened in any recording, so nothing is generated on the storyteller's
# behalf — the constraint NO_STORY_FALLBACK exists to enforce.
#
# ONE BANK, AND ONLY THE "NOTHING MORE" ONE. There was briefly a second bank
# for "I have nothing about X at all", and it was unreachable and dangerous in
# the same breath: the caller now refuses to name a subject while ANY of that
# person's units are still unplayed (see _no_story_line), because an empty
# selection means "nothing answers THAT question", never "the archive is out
# of stories about this person". Every entity in the map has units, so
# "nothing at all about X" can only be said when everything about X has
# already been played — which is precisely what this bank says.
#
# That happened live: with five of אמנון's twelve units played, the archive
# told the listener there was nothing more while the entire army story sat
# unplayed. A specific falsehood is worse than a vague truth — it tells
# someone to stop asking about a person the archive still has stories about.
NO_MORE_STORY_ABOUT_TEMPLATES = [
    "אין לי עוד סיפור על {entity}",
    "זה כל מה שיש לי על {entity}",
    "לא נשאר לי עוד מה לספר על {entity}",
]


def no_story_about(entity: str, *, variant: int = 0) -> str:
    """"That's all I have about אמנון" — said only when it is true.

    `variant` picks from the bank and is expected to be a STABLE function of
    the question, so the same question always produces the same wording.
    Random selection would make the retrieval eval flaky for a reason that has
    nothing to do with retrieval.
    """
    return NO_MORE_STORY_ABOUT_TEMPLATES[
        variant % len(NO_MORE_STORY_ABOUT_TEMPLATES)
    ].format(entity=entity)

