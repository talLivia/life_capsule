"""
Tracing must be a zero-cost no-op in the default build (OTEL disabled, no
opentelemetry packages installed). These tests lock that contract so the
hot-path `span(...)` calls never raise or require the optional deps.
"""

from app.telemetry import init_telemetry, is_enabled, span


def test_span_is_noop_when_disabled():
    # Default config has OTEL_ENABLED=false, so no tracer is configured.
    assert is_enabled() is False
    with span("tts.synthesize", chars=42, lang="en") as s:
        assert s is None  # null context yields None


def test_span_accepts_attributes_without_tracer():
    # Must not raise even with many attributes when tracing is off.
    with span("chat.turn", input_chars=10, history_len=3, cloned=True):
        pass


def test_init_telemetry_noop_when_disabled():
    class _DummyApp:
        pass

    # Should return cleanly without touching any opentelemetry import.
    init_telemetry(_DummyApp())
    assert is_enabled() is False
