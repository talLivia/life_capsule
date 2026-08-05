"""Synthesise every interview question up front, and fail loudly if it cannot.

Decision 16.D preferred pre-generating all questions per language over
synthesising on demand, for two reasons: no latency on first play, and a
failure that shows up at build time rather than quietly for a storyteller.

The endpoint synthesises on demand and caches, which reaches the same steady
state without a build step or a deploy artifact. This script is the other half
of 16.D — run it and every question is spoken once into the same cache the
endpoint reads, so the first producer to press Read aloud waits for nothing,
and a language TTS cannot handle fails HERE, out loud, with a count.

    python scripts/warm_question_audio.py            # every language
    python scripts/warm_question_audio.py --language he
    python scripts/warm_question_audio.py --force    # re-speak cached ones

Safe to re-run: it skips anything already cached under the current wording, and
the cache is derived data — deleting the directory only costs the next
synthesis.
"""

import argparse
import asyncio
import hashlib
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import interview_config  # noqa: E402
from app.config import settings  # noqa: E402
from app.services.tts import tts_service  # noqa: E402


def cache_path(cache_dir: Path, language: str, question_id: str, text: str) -> Path:
    """Must match the endpoint's key exactly, or warming fills a cache nothing
    reads. Text is in the digest so edited wording misses and is re-spoken."""
    digest = hashlib.sha256(f"{language}:{text}".encode("utf-8")).hexdigest()[:16]
    return cache_dir / f"{question_id}-{digest}.wav"


async def warm(languages: list[str], force: bool) -> int:
    cache_dir = Path(settings.QUESTION_AUDIO_DIR)
    cache_dir.mkdir(parents=True, exist_ok=True)

    failures: list[tuple[str, str, str]] = []
    spoken = skipped = 0

    for language in languages:
        questions = interview_config.get_questions(language)
        print(f"\n{language}: {len(questions)} questions")
        for question in questions:
            qid, text = question["id"], question["text"]
            target = cache_path(cache_dir, language, qid, text)
            if target.exists() and not force:
                skipped += 1
                continue
            try:
                audio = await tts_service.synthesize_bytes(text, language=language)
                target.write_bytes(audio)
                spoken += 1
                print(f"  ok   {qid} ({len(audio):,} bytes)")
            except Exception as exc:  # noqa: BLE001 — reporting every failure is the point
                failures.append((language, qid, str(exc)))
                print(f"  FAIL {qid}: {exc}")

    print(f"\nspoken {spoken}, already cached {skipped}, failed {len(failures)}")
    if failures:
        # Loud, and non-zero. A language TTS cannot speak is a go/no-go
        # finding about read-aloud for that producer, not a warning to scroll
        # past — see §12 on Hebrew quality.
        print("\nFAILURES:")
        for language, qid, err in failures:
            print(f"  {language} {qid}: {err}")
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--language", action="append", dest="languages")
    parser.add_argument("--force", action="store_true", help="re-speak cached questions")
    args = parser.parse_args()

    languages = args.languages or interview_config.available_languages()
    return asyncio.run(warm(languages, args.force))


if __name__ == "__main__":
    raise SystemExit(main())
