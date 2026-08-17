"""Bulk-upload the presenter videos to storage, keyed by CONVENTION:
presenter/{question_id}.mp4 (plus presenter/intro.mp4).

There is deliberately NO mapping file anywhere (docs/PRESENTER_VIDEOS_PLAN.md
§2) — this script is where "every question has a video" is enforced, because
the runtime derives keys blindly from the frozen question ids.

Two modes:

  ID-NAMED (default): files are named by frozen question id —
  childhood_q01.mp4, military_service_q03.mp4, intro.mp4. This is the
  MANUAL-CORRECTION flow: to replace one question's video, drop ONE
  correctly-named file in a directory and run with --allow-partial; only
  that key is overwritten.

      python scripts/upload_presenter_videos.py path/to/dir --allow-partial

  ORDERED (--ordered-dir): files carry arbitrary (e.g. camera) names whose
  ASCENDING SORT ORDER equals the interview's document order — file #1 →
  question #1, file #2 → question #2, through gate branches in place. The
  count must equal the question count EXACTLY (no --allow-partial here: a
  partial ordered set has no defensible alignment). The full mapping table
  is printed before upload and written next to this script's output.

      python scripts/upload_presenter_videos.py "C:/New folder" --ordered-dir

Validation always runs BEFORE any byte is uploaded:
  * id-named: unknown filenames (typos) abort; without --allow-partial,
    missing question ids abort. A missing intro is a warning, never fatal —
    it ships separately.
  * ordered: a count mismatch aborts.
"""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from app import interview_config  # noqa: E402
from app.services.storage import storage_service  # noqa: E402

VIDEO_EXTS = {".mp4"}
PREFIX = "presenter/"
INTRO_ID = "intro"


def _video_files(directory: Path) -> list[Path]:
    files = sorted(
        (p for p in directory.iterdir() if p.suffix.lower() in VIDEO_EXTS),
        key=lambda p: p.name,
    )
    strays = [p.name for p in directory.iterdir() if p.is_file() and p.suffix.lower() not in VIDEO_EXTS]
    if strays:
        print(f"NOTE: ignoring {len(strays)} non-video file(s): {strays[:5]}")
    return files


def _plan_id_named(files: list[Path], ids: list[str], allow_partial: bool) -> list[tuple[Path, str]]:
    known = set(ids) | {INTRO_ID}
    unknown = [p.name for p in files if p.stem not in known]
    if unknown:
        sys.exit(f"ABORT — filenames that match no question id (typos?): {unknown}")
    present = {p.stem for p in files}
    missing = [i for i in ids if i not in present]
    if missing and not allow_partial:
        sys.exit(
            f"ABORT — {len(missing)} question(s) have no video (use --allow-partial "
            f"for a replacement run): {missing[:10]}{'…' if len(missing) > 10 else ''}"
        )
    if INTRO_ID not in present:
        print("WARNING: no intro.mp4 in this batch — the intro ships separately.")
    return [(p, p.stem) for p in files]


def _plan_ordered(files: list[Path], ids: list[str]) -> list[tuple[Path, str]]:
    if len(files) != len(ids):
        sys.exit(
            f"ABORT — ordered mode needs EXACTLY {len(ids)} files (one per "
            f"question, document order); found {len(files)}. A partial ordered "
            f"set has no defensible alignment."
        )
    return list(zip(files, ids))


async def _upload(plan: list[tuple[Path, str]], dry_run: bool) -> None:
    total = sum(p.stat().st_size for p, _ in plan)
    print(f"\n{len(plan)} file(s), {total / 1e9:.2f} GB total → {PREFIX}<id>.mp4\n")
    for n, (path, qid) in enumerate(plan, 1):
        key = f"{PREFIX}{qid}.mp4"
        size_mb = path.stat().st_size / 1e6
        if dry_run:
            print(f"[{n:3d}/{len(plan)}] DRY RUN  {path.name} -> {key}  ({size_mb:.1f} MB)")
            continue
        await storage_service.upload_file(path.read_bytes(), key, content_type="video/mp4")
        print(f"[{n:3d}/{len(plan)}] uploaded {path.name} -> {key}  ({size_mb:.1f} MB)", flush=True)


def main() -> None:
    # Windows consoles default to cp1252 — Hebrew/arrows in output must not
    # crash an upload run.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("directory", type=Path)
    ap.add_argument("--ordered-dir", action="store_true",
                    help="filenames are arbitrary; ascending sort order = document question order")
    ap.add_argument("--allow-partial", action="store_true",
                    help="id-named mode only: skip the completeness check (single-file replacement)")
    ap.add_argument("--language", default="he")
    ap.add_argument("--dry-run", action="store_true", help="print the plan, upload nothing")
    ap.add_argument("--mapping-out", type=Path, default=None,
                    help="ordered mode: also write the mapping table to this file")
    args = ap.parse_args()

    if args.ordered_dir and args.allow_partial:
        sys.exit("ABORT — --allow-partial is meaningless with --ordered-dir "
                 "(a partial ordered set has no defensible alignment).")
    if not args.directory.is_dir():
        sys.exit(f"ABORT — not a directory: {args.directory}")

    ids = interview_config.all_question_ids(args.language)
    files = _video_files(args.directory)
    if args.ordered_dir:
        plan = _plan_ordered(files, ids)
        lines = [f"#{i+1:3d}  {p.name}  ->  {qid}" for i, (p, qid) in enumerate(plan)]
        print("MAPPING (document order):")
        print("\n".join(lines))
        if args.mapping_out:
            args.mapping_out.write_text("\n".join(lines) + "\n", encoding="utf-8")
            print(f"\nmapping table written to {args.mapping_out}")
    else:
        plan = _plan_id_named(files, ids, args.allow_partial)

    asyncio.run(_upload(plan, args.dry_run))
    print("\nDone." if not args.dry_run else "\nDry run complete — nothing uploaded.")


if __name__ == "__main__":
    main()
