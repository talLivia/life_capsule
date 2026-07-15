"""
Optional OpenTelemetry tracing.

Tracing is off by default and the `opentelemetry-*` packages are NOT a hard
dependency — `span(...)` returns a null context and `init_telemetry()` is a
no-op unless BOTH `settings.OTEL_ENABLED` is true AND the packages import
cleanly. This keeps the hot path (LLM → TTS → animation) zero-cost in the
default build while letting operators flip on full distributed tracing with
a config change + `pip install -r requirements-otel.txt`.

Instrumented spans (when enabled):
  chat.turn          — one per user turn (the trace root for a conversation turn)
  llm.stream         — the streaming LLM call
  tts.synthesize     — per-sentence speech synthesis
  avatar.animate     — per-sentence lip-sync render
  storage.upload     — per-chunk video upload
Plus FastAPI request spans and SQLAlchemy query spans via auto-instrumentation.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Any, Iterator, Optional

from app.config import settings

logger = logging.getLogger(__name__)

# Set to a real tracer by init_telemetry() when tracing is active.
_tracer: Optional[Any] = None


def init_telemetry(app) -> None:
    """
    Wire up OpenTelemetry if enabled and the packages are present.
    Safe to call unconditionally at startup; degrades to a no-op otherwise.
    """
    global _tracer

    if not settings.OTEL_ENABLED:
        logger.info("OpenTelemetry disabled (OTEL_ENABLED=false)")
        return

    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
        from opentelemetry.sdk.resources import SERVICE_NAME, Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError as e:
        logger.warning(
            "OTEL_ENABLED=true but opentelemetry packages are missing (%s). "
            "Install requirements-otel.txt. Tracing stays disabled.",
            e,
        )
        return

    try:
        resource = Resource.create({SERVICE_NAME: settings.OTEL_SERVICE_NAME})
        provider = TracerProvider(resource=resource)
        exporter = OTLPSpanExporter(endpoint=settings.OTEL_EXPORTER_OTLP_ENDPOINT)
        provider.add_span_processor(BatchSpanProcessor(exporter))
        trace.set_tracer_provider(provider)

        # Auto-instrument the web + DB layers. FastAPI spans cover REST
        # requests; SQLAlchemy spans cover every query (including the ones
        # the WS pipeline issues outside a request context).
        FastAPIInstrumentor.instrument_app(app)
        try:
            from app.database import engine

            SQLAlchemyInstrumentor().instrument(engine=engine.sync_engine)
        except Exception as e:  # pragma: no cover - best effort
            logger.warning("SQLAlchemy instrumentation skipped: %s", e)

        _tracer = trace.get_tracer(settings.OTEL_SERVICE_NAME)
        logger.info(
            "OpenTelemetry enabled (service=%s, endpoint=%s)",
            settings.OTEL_SERVICE_NAME,
            settings.OTEL_EXPORTER_OTLP_ENDPOINT,
        )
    except Exception as e:  # pragma: no cover - never let tracing break boot
        logger.warning("OpenTelemetry init failed, continuing without tracing: %s", e)
        _tracer = None


@contextmanager
def span(name: str, **attributes: Any) -> Iterator[Optional[Any]]:
    """
    Start a span with the given attributes, or a zero-cost null context if
    tracing is disabled. Usable from sync or async code:

        with span("tts.synthesize", chars=len(text), lang=lang):
            await tts_service.synthesize(...)
    """
    if _tracer is None:
        yield None
        return

    with _tracer.start_as_current_span(name) as current:
        for key, value in attributes.items():
            try:
                current.set_attribute(key, value)
            except Exception:
                pass
        yield current


def is_enabled() -> bool:
    return _tracer is not None
