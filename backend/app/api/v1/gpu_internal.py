"""
Internal GPU-inference endpoints (Prompt 9) — exposed by every deployment
of this codebase, but only actually called when another instance of this
same app runs with GPU_SERVICE_URL pointed here. This is the Runpod-pod
half of the Fly.io/Runpod split the project plan calls for: the CPU-tier
Fly.io deployment proxies STT/TTS/animate calls to whichever deployment
serves these routes, over HTTP, via app/services/gpu_client.py.

Shared-secret auth (X-GPU-Service-Secret header) — never reachable by the
public frontend, and never wired into any frontend-facing router.
"""

import base64
import logging
import shutil
import tempfile
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, status

from app.config import settings
from app.schemas import (
    GpuAnimateRequest,
    GpuAnimateResponse,
    GpuSynthesizeRequest,
    GpuSynthesizeResponse,
    GpuTranscribeRequest,
    GpuTranscribeResponse,
)
from app.services.animator import avatar_animator
from app.services.stt import stt_service
from app.services.tts import tts_service

logger = logging.getLogger(__name__)
router = APIRouter()


def _check_secret(x_gpu_service_secret: Optional[str]) -> None:
    if (
        not settings.GPU_SERVICE_SHARED_SECRET
        or x_gpu_service_secret != settings.GPU_SERVICE_SHARED_SECRET
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing GPU service secret",
        )


@router.post("/transcribe", response_model=GpuTranscribeResponse)
async def internal_transcribe(
    payload: GpuTranscribeRequest,
    x_gpu_service_secret: Optional[str] = Header(default=None),
):
    _check_secret(x_gpu_service_secret)
    audio_bytes = base64.b64decode(payload.audio_b64)
    text = await stt_service.transcribe(audio_bytes, language=payload.language)
    return GpuTranscribeResponse(text=text)


@router.post("/synthesize", response_model=GpuSynthesizeResponse)
async def internal_synthesize(
    payload: GpuSynthesizeRequest,
    x_gpu_service_secret: Optional[str] = Header(default=None),
):
    _check_secret(x_gpu_service_secret)
    tmpdir = Path(tempfile.mkdtemp(prefix="gpu-synth-"))
    try:
        output_path = tmpdir / "out.wav"
        speaker_wav_path: Optional[Path] = None
        if payload.speaker_wav_b64:
            speaker_wav_path = tmpdir / "speaker.wav"
            speaker_wav_path.write_bytes(base64.b64decode(payload.speaker_wav_b64))

        result = await tts_service.synthesize(
            text=payload.text,
            output_path=str(output_path),
            speaker_wav=str(speaker_wav_path) if speaker_wav_path else None,
            language=payload.language,
        )
        audio_b64 = base64.b64encode(output_path.read_bytes()).decode("ascii")
        return GpuSynthesizeResponse(
            audio_b64=audio_b64,
            engine=result.engine,
            fallback=result.fallback,
            voice_cloned=result.voice_cloned,
        )
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


@router.post("/animate", response_model=GpuAnimateResponse)
async def internal_animate(
    payload: GpuAnimateRequest,
    x_gpu_service_secret: Optional[str] = Header(default=None),
):
    _check_secret(x_gpu_service_secret)
    tmpdir = Path(tempfile.mkdtemp(prefix="gpu-animate-"))
    try:
        image_path = tmpdir / "avatar.jpg"
        audio_path = tmpdir / "in.wav"
        output_path = tmpdir / "out.mp4"
        image_path.write_bytes(base64.b64decode(payload.avatar_image_b64))
        audio_path.write_bytes(base64.b64decode(payload.audio_b64))

        await avatar_animator.animate(
            avatar_image_path=str(image_path),
            audio_path=str(audio_path),
            output_path=str(output_path),
        )
        video_b64 = base64.b64encode(output_path.read_bytes()).decode("ascii")
        return GpuAnimateResponse(video_b64=video_b64)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
