"""
Tests for gpu_client.py (Prompt 9) — the Fly.io/Runpod network-split
toggle. Verifies the LOCAL branch (GPU_SERVICE_URL unset, the default)
calls the in-process services unchanged, and the REMOTE branch serializes/
deserializes correctly against a mocked HTTP transport (no real network).
"""

import base64
from unittest.mock import AsyncMock

import httpx
import pytest

from app.services import gpu_client
from app.services.tts import SynthResult

pytestmark = pytest.mark.asyncio


# ── local branch (GPU_SERVICE_URL unset) ────────────────────────────────────


async def test_transcribe_local_calls_stt_service_directly(monkeypatch):
    monkeypatch.setattr(gpu_client.settings, "GPU_SERVICE_URL", None)
    mock = AsyncMock(return_value="hello")
    monkeypatch.setattr(gpu_client.stt_service, "transcribe", mock)

    result = await gpu_client.transcribe(b"audio-bytes", language="en")

    assert result == "hello"
    mock.assert_awaited_once_with(b"audio-bytes", language="en")


async def test_synthesize_local_calls_tts_service_directly(monkeypatch, tmp_path):
    monkeypatch.setattr(gpu_client.settings, "GPU_SERVICE_URL", None)
    expected = SynthResult(output_path="x", engine="chatterbox", fallback=False, voice_cloned=True)
    mock = AsyncMock(return_value=expected)
    monkeypatch.setattr(gpu_client.tts_service, "synthesize", mock)

    result = await gpu_client.synthesize("hi", str(tmp_path / "out.wav"), language="en")

    assert result is expected
    mock.assert_awaited_once()


async def test_animate_local_calls_avatar_animator_directly(monkeypatch, tmp_path):
    monkeypatch.setattr(gpu_client.settings, "GPU_SERVICE_URL", None)
    mock = AsyncMock()
    monkeypatch.setattr(gpu_client.avatar_animator, "animate", mock)

    await gpu_client.animate("img.jpg", "audio.wav", str(tmp_path / "out.mp4"))

    mock.assert_awaited_once_with(
        avatar_image_path="img.jpg", audio_path="audio.wav", output_path=str(tmp_path / "out.mp4")
    )


# ── remote branch (GPU_SERVICE_URL set) ─────────────────────────────────────

# The REAL httpx.AsyncClient, captured before any monkeypatching — the
# patched factory below must build clients from this, not from
# `httpx.AsyncClient` again, or it recurses into itself infinitely once
# that name is patched.
_RealAsyncClient = httpx.AsyncClient


def _patch_async_client(monkeypatch, handler) -> None:
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        gpu_client.httpx, "AsyncClient", lambda **kw: _RealAsyncClient(transport=transport)
    )


async def test_transcribe_remote_posts_base64_and_secret(monkeypatch):
    monkeypatch.setattr(gpu_client.settings, "GPU_SERVICE_URL", "http://gpu-pod.internal")
    monkeypatch.setattr(gpu_client.settings, "GPU_SERVICE_SHARED_SECRET", "s3cr3t")

    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["secret"] = request.headers.get("x-gpu-service-secret")
        import json

        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"text": "transcribed!"})

    _patch_async_client(monkeypatch, handler)

    result = await gpu_client.transcribe(b"raw-audio", language="he")

    assert result == "transcribed!"
    assert captured["url"] == "http://gpu-pod.internal/internal/gpu/transcribe"
    assert captured["secret"] == "s3cr3t"
    assert base64.b64decode(captured["body"]["audio_b64"]) == b"raw-audio"
    assert captured["body"]["language"] == "he"


async def test_synthesize_remote_round_trips_audio(monkeypatch, tmp_path):
    monkeypatch.setattr(gpu_client.settings, "GPU_SERVICE_URL", "http://gpu-pod.internal")
    monkeypatch.setattr(gpu_client.settings, "GPU_SERVICE_SHARED_SECRET", "s3cr3t")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "audio_b64": base64.b64encode(b"wav-bytes").decode("ascii"),
                "engine": "chatterbox",
                "fallback": False,
                "voice_cloned": True,
            },
        )

    _patch_async_client(monkeypatch, handler)

    output_path = str(tmp_path / "out.wav")
    result = await gpu_client.synthesize("hello", output_path, language="en")

    assert result.engine == "chatterbox"
    assert result.voice_cloned is True
    assert (tmp_path / "out.wav").read_bytes() == b"wav-bytes"


async def test_animate_remote_round_trips_video(monkeypatch, tmp_path):
    monkeypatch.setattr(gpu_client.settings, "GPU_SERVICE_URL", "http://gpu-pod.internal")
    monkeypatch.setattr(gpu_client.settings, "GPU_SERVICE_SHARED_SECRET", "s3cr3t")

    image_path = tmp_path / "img.jpg"
    audio_path = tmp_path / "in.wav"
    image_path.write_bytes(b"img-bytes")
    audio_path.write_bytes(b"audio-bytes")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"video_b64": base64.b64encode(b"mp4-bytes").decode("ascii")}
        )

    _patch_async_client(monkeypatch, handler)

    output_path = str(tmp_path / "out.mp4")
    await gpu_client.animate(str(image_path), str(audio_path), output_path)

    assert (tmp_path / "out.mp4").read_bytes() == b"mp4-bytes"


async def test_remote_raises_on_http_error(monkeypatch):
    monkeypatch.setattr(gpu_client.settings, "GPU_SERVICE_URL", "http://gpu-pod.internal")
    monkeypatch.setattr(gpu_client.settings, "GPU_SERVICE_SHARED_SECRET", "wrong")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "Invalid or missing GPU service secret"})

    _patch_async_client(monkeypatch, handler)

    with pytest.raises(httpx.HTTPStatusError):
        await gpu_client.transcribe(b"audio", language="en")
