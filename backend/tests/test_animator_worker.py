"""
Regression test for issue #5 — missing musetalk_worker.py.

musetalk_worker.py is our custom persistent-worker driver, not part of the
upstream MuseTalk repo that setup_musetalk.sh clones. It's gitignored under
models/, so a fresh clone never had it and MuseTalk silently fell back to the
static-image engine for every user. We now ship a tracked copy at
backend/musetalk_worker.py and resolve to it when the clone lacks the script.
"""

from pathlib import Path

from app.services.animator import AvatarAnimator


def test_tracked_worker_script_is_shipped():
    """The tracked worker must exist in the repo (outside gitignored models/)."""
    tracked = Path(__file__).resolve().parent.parent / "musetalk_worker.py"
    assert tracked.is_file(), "backend/musetalk_worker.py is missing — MuseTalk will not start"


def test_resolve_worker_prefers_clone_then_falls_back(tmp_path):
    animator = AvatarAnimator()

    # Clone has its own copy → use it.
    clone = tmp_path / "MuseTalk"
    (clone / "scripts").mkdir(parents=True)
    in_clone = clone / "scripts" / "musetalk_worker.py"
    in_clone.write_text("# worker")
    assert animator._resolve_worker_script(clone) == in_clone

    # Clone lacks the script → fall back to the tracked backend copy.
    empty_clone = tmp_path / "EmptyClone"
    (empty_clone / "scripts").mkdir(parents=True)
    resolved = animator._resolve_worker_script(empty_clone)
    assert resolved.name == "musetalk_worker.py"
    assert resolved.is_file()
    # It's the tracked backend copy, not anything under the empty clone.
    assert "EmptyClone" not in str(resolved)
