"""
Regression tests for AvatarAnimator._animate_simple's ffmpeg invocation.

asyncio.create_subprocess_exec's subprocess transport is not implemented
for WindowsSelectorEventLoopPolicy — only for ProactorEventLoop — and
raises a bare NotImplementedError when attempted. main.py pins
WindowsSelectorEventLoopPolicy on Windows for psycopg3's async mode
(LangGraph's checkpointer), which conversely doesn't work under Proactor,
so the two requirements are mutually exclusive under one global policy.
_animate_simple must shell out to ffmpeg via the plain synchronous
subprocess module (in a worker thread), sidestepping asyncio's subprocess
transport entirely — confirmed live: this is exactly what broke
end-to-end /talk testing on Windows before this fix.
"""

import subprocess

import pytest

from app.services.animator import AvatarAnimator

pytestmark = pytest.mark.asyncio


async def test_animate_simple_uses_synchronous_subprocess(monkeypatch, tmp_path):
    animator = AvatarAnimator()
    output_path = str(tmp_path / "out.mp4")
    captured: dict = {}

    def fake_run(cmd, capture_output=True):
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, returncode=0, stdout=b"", stderr=b"")

    # Patching subprocess.run (not asyncio.create_subprocess_exec) is the
    # actual assertion here — if _animate_simple ever regresses back to
    # asyncio's subprocess API, this mock simply wouldn't be hit and the
    # real (Windows-broken) call would raise NotImplementedError instead.
    monkeypatch.setattr(subprocess, "run", fake_run)

    result = await animator._animate_simple("avatar.jpg", "audio.wav", output_path)

    assert result == output_path
    assert captured["cmd"][0] == "ffmpeg"
    assert "audio.wav" in captured["cmd"]
    assert output_path in captured["cmd"]


async def test_animate_simple_raises_with_ffmpeg_stderr_on_failure(monkeypatch, tmp_path):
    animator = AvatarAnimator()

    def fake_run(cmd, capture_output=True):
        return subprocess.CompletedProcess(cmd, returncode=1, stdout=b"", stderr=b"ffmpeg exploded")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="Simple animation"):
        await animator._animate_simple("avatar.jpg", "audio.wav", str(tmp_path / "out.mp4"))
