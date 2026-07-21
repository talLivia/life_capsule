"""
Thin client for the GPU-inference network split (Prompt 9). When
settings.GPU_SERVICE_URL is set, STT/TTS/animate calls proxy over HTTP to a
separate GPU-backed deployment of this same codebase (a persistent Runpod
pod, per the project plan) via its /internal/gpu/* endpoints
(app/api/v1/gpu_internal.py). Left unset (the default), these functions
call the same in-process services websocket.py always has, unchanged.

websocket.py calls THESE three functions instead of stt_service/
tts_service/avatar_animator directly, so the Fly.io/Runpod split is a
config toggle, not a code fork — local dev and a CPU-only deployment need
no changes at all.
"""

from __future__ import annotations

import base64
import logging
from pathlib import Path
from typing import Optional, Union

import httpx

from app.config import settings
from app.services.animator import avatar_animator
from app.services.stt import stt_service
from app.services.tts import SynthResult, tts_service

logger = logging.getLogger(__name__)


def _headers() -> dict:
    return {"X-GPU-Service-Secret": settings.GPU_SERVICE_SHARED_SECRET}


async def transcribe(audio_data: Union[bytes, str], language: str = "en") -> str:
    """`audio_data` may be raw bytes or a local file path (matching
    stt_service.transcribe's own signature) — a path is only meaningful
    on THIS machine, so the remote branch reads it into bytes before
    sending; the local branch passes either straight through unchanged."""
    if not settings.GPU_SERVICE_URL:
        return await stt_service.transcribe(audio_data, language=language)

    raw = audio_data if isinstance(audio_data, bytes) else Path(audio_data).read_bytes()

    async with httpx.AsyncClient(timeout=settings.GPU_SERVICE_TIMEOUT_SECONDS) as client:
        resp = await client.post(
            f"{settings.GPU_SERVICE_URL}/internal/gpu/transcribe",
            json={
                "audio_b64": base64.b64encode(raw).decode("ascii"),
                "language": language,
            },
            headers=_headers(),
        )
        resp.raise_for_status()
        return resp.json()["text"]


async def synthesize(
    text: str,
    output_path: str,
    speaker_wav: Optional[str] = None,
    language: str = "en",
) -> SynthResult:
    if not settings.GPU_SERVICE_URL:
        return await tts_service.synthesize(
            text=text, output_path=output_path, speaker_wav=speaker_wav, language=language
        )

    payload: dict = {"text": text, "language": language}
    if speaker_wav:
        payload["speaker_wav_b64"] = base64.b64encode(Path(speaker_wav).read_bytes()).decode(
            "ascii"
        )

    async with httpx.AsyncClient(timeout=settings.GPU_SERVICE_TIMEOUT_SECONDS) as client:
        resp = await client.post(
            f"{settings.GPU_SERVICE_URL}/internal/gpu/synthesize",
            json=payload,
            headers=_headers(),
        )
        resp.raise_for_status()
        data = resp.json()

    Path(output_path).write_bytes(base64.b64decode(data["audio_b64"]))
    return SynthResult(
        output_path=output_path,
        engine=data["engine"],
        fallback=data["fallback"],
        voice_cloned=data["voice_cloned"],
    )


async def animate(avatar_image_path: str, audio_path: str, output_path: str) -> None:
    if not settings.GPU_SERVICE_URL:
        await avatar_animator.animate(
            avatar_image_path=avatar_image_path, audio_path=audio_path, output_path=output_path
        )
        return

    payload = {
        "avatar_image_b64": base64.b64encode(Path(avatar_image_path).read_bytes()).decode(
            "ascii"
        ),
        "audio_b64": base64.b64encode(Path(audio_path).read_bytes()).decode("ascii"),
    }
    async with httpx.AsyncClient(timeout=settings.GPU_SERVICE_TIMEOUT_SECONDS) as client:
        resp = await client.post(
            f"{settings.GPU_SERVICE_URL}/internal/gpu/animate",
            json=payload,
            headers=_headers(),
        )
        resp.raise_for_status()
        video_b64 = resp.json()["video_b64"]
    Path(output_path).write_bytes(base64.b64decode(video_b64))
