"""SpanProcessor behaviour tests — uses a mock transport instead of
hitting the network. The processor is exercised via its public
contract (on_end + force_flush + shutdown).
"""
from __future__ import annotations

import json
import threading
import time
from typing import Any, List, Tuple

import pytest

from satsignal_otel import (
    SatsignalSpanProcessor,
    auto_anchor_on_eval_fail,
    mark_for_anchor,
)
from satsignal_otel._anchor import (
    APIError,
    call_with_retry,
)


# ---- minimal fake OTel surface ---------------------------------------

class _FakeKind:
    def __init__(self, name="INTERNAL"):
        self.name = name


class _FakeStatus:
    def __init__(self):
        class _C:
            name = "OK"
        self.status_code = _C()
        self.description = None


class _FakeCtx:
    def __init__(self, t=0x1, s=0x2):
        self.trace_id = t
        self.span_id = s


class _FakeResource:
    def __init__(self):
        self.attributes = {"service.name": "test"}


class _FakeSpan:
    def __init__(self, *, name="span", attrs=None):
        self.name = name
        self.kind = _FakeKind()
        self.status = _FakeStatus()
        self.start_time = 1
        self.end_time = 2
        self.attributes = dict(attrs or {})
        self.events: list = []
        self.links: list = []
        self.resource = _FakeResource()
        self.context = _FakeCtx()

    def set_attribute(self, k, v):
        self.attributes[k] = v


# ---- mock transport --------------------------------------------------

class MockTransport:
    """Captures every POST and returns canned 200 responses by default."""

    def __init__(self, *, status: int = 200, body: dict | None = None):
        self.calls: List[Tuple[str, str, dict, bytes]] = []
        self.status = status
        # Canonical-keys-only — matches the live server's 2xx shape.
        self.body = body if body is not None else {
            "proof_id": "b" * 16,
            "txid": "deadbeef",
            "mode": "manifest",
            "folder_slug": "otel-evals",
            "proof_url": "https://app.satsignal.cloud/r/x",
            "bundle_url": "https://app.satsignal.cloud/bundle/x.mbnt",
            "duplicate": False,
        }

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append((method, url, dict(headers), body))
        return self.status, json.dumps(self.body).encode("utf-8")


# ---- tests -----------------------------------------------------------

def _new_processor(transport, **kwargs):
    # Canonical kwarg; the deprecated matter_slug= ctor path is covered
    # in test_folder_alias.py.
    return SatsignalSpanProcessor(
        api_key="sk_test",
        folder_slug="otel-evals",
        flush_interval=kwargs.pop("flush_interval", 0.2),
        max_batch_size=kwargs.pop("max_batch_size", 100),
        transport=transport,
        **kwargs,
    )


def test_unmarked_spans_do_not_anchor():
    t = MockTransport()
    p = _new_processor(t)
    try:
        p.on_end(_FakeSpan(name="unmarked"))
        p.force_flush(timeout_millis=1000)
        assert t.calls == []
    finally:
        p.shutdown()


def test_marked_span_anchors_as_manifest_batch():
    t = MockTransport()
    p = _new_processor(t)
    try:
        s = _FakeSpan(attrs={"satsignal.anchor": True})
        p.on_end(s)
        # Force a flush by waiting just over the flush interval.
        time.sleep(0.4)
        assert p.force_flush(timeout_millis=1000)
        assert len(t.calls) == 1
        _method, url, _headers, body = t.calls[0]
        assert url.endswith("/api/v1/anchors")
        decoded = json.loads(body.decode("utf-8"))
        assert decoded["folder_slug"] == "otel-evals"   # canonical wire key
        assert "matter_slug" not in decoded
        assert "items" in decoded   # manifest mode
        assert len(decoded["items"]) == 1
        assert decoded["items"][0]["sha256_hex"]
    finally:
        p.shutdown()


def test_single_mode_span_anchors_standalone():
    t = MockTransport()
    t.body = {**t.body, "mode": "standard"}
    p = _new_processor(t)
    try:
        s = _FakeSpan(attrs={
            "satsignal.anchor": True,
            "satsignal.anchor.mode": "single",
            "satsignal.anchor.label": "important",
        })
        p.on_end(s)
        time.sleep(0.4)
        assert p.force_flush(timeout_millis=1000)
        assert len(t.calls) == 1
        decoded = json.loads(t.calls[0][3].decode("utf-8"))
        assert "items" not in decoded            # single mode
        assert decoded["sha256_hex"]
        assert decoded["label"] == "important"
    finally:
        p.shutdown()


def test_batch_groups_multiple_spans():
    t = MockTransport()
    p = _new_processor(t)
    try:
        for i in range(5):
            p.on_end(_FakeSpan(name=f"s{i}",
                               attrs={"satsignal.anchor": True}))
        time.sleep(0.4)
        assert p.force_flush(timeout_millis=1000)
        # One manifest POST should cover all 5 spans.
        assert len(t.calls) == 1
        decoded = json.loads(t.calls[0][3].decode("utf-8"))
        assert len(decoded["items"]) == 5
    finally:
        p.shutdown()


def test_max_batch_size_forces_flush():
    t = MockTransport()
    # Huge flush_interval but small max_batch_size — flush should be
    # driven by the batch size hitting the threshold.
    p = _new_processor(t, flush_interval=60.0, max_batch_size=3)
    try:
        for i in range(3):
            p.on_end(_FakeSpan(name=f"s{i}",
                               attrs={"satsignal.anchor": True}))
        # Worker wakes up because batch hit max_batch_size.
        for _ in range(50):
            if t.calls:
                break
            time.sleep(0.05)
        assert len(t.calls) == 1
        decoded = json.loads(t.calls[0][3].decode("utf-8"))
        assert len(decoded["items"]) == 3
    finally:
        p.shutdown()


def test_daily_anchor_cap_drops_overflow():
    t = MockTransport()
    p = _new_processor(t, daily_anchor_cap=2)
    try:
        for _ in range(5):
            p.on_end(_FakeSpan(attrs={"satsignal.anchor": True}))
        time.sleep(0.4)
        assert p.force_flush(timeout_millis=1000)
        assert len(t.calls) == 1
        decoded = json.loads(t.calls[0][3].decode("utf-8"))
        assert len(decoded["items"]) == 2
    finally:
        p.shutdown()


def test_shutdown_drains_pending_spans():
    t = MockTransport()
    p = _new_processor(t, flush_interval=60.0, max_batch_size=1000)
    p.on_end(_FakeSpan(attrs={"satsignal.anchor": True}))
    p.on_end(_FakeSpan(attrs={"satsignal.anchor": True}))
    # shutdown() should drain — no time.sleep needed.
    p.shutdown()
    assert len(t.calls) == 1
    decoded = json.loads(t.calls[0][3].decode("utf-8"))
    assert len(decoded["items"]) == 2


def test_fail_open_swallows_api_error():
    t = MockTransport(status=500, body={
        "error": {"code": "server_error", "message": "boom"},
    })
    p = _new_processor(t, fail_open=True)
    try:
        p.on_end(_FakeSpan(attrs={"satsignal.anchor": True}))
        time.sleep(0.4)
        # Retry will hammer 3 times; we just need it not to raise.
        p.force_flush(timeout_millis=15_000)
    finally:
        p.shutdown()  # would re-raise if fail_open=False


def test_fail_closed_reraises_on_shutdown():
    t = MockTransport(status=400, body={
        "error": {"code": "bad_request", "message": "no"},
    })
    p = _new_processor(t, fail_open=False)
    p.on_end(_FakeSpan(attrs={"satsignal.anchor": True}))
    time.sleep(0.4)
    p.force_flush(timeout_millis=1000)
    with pytest.raises(APIError):
        p.shutdown()


# ---- helper tests ----------------------------------------------------

def test_mark_for_anchor_sets_attributes():
    s = _FakeSpan()
    mark_for_anchor(s, label="foo", mode="single", force_new=True)
    assert s.attributes["satsignal.anchor"] is True
    assert s.attributes["satsignal.anchor.mode"] == "single"
    assert s.attributes["satsignal.anchor.label"] == "foo"
    assert s.attributes["satsignal.anchor.force_new"] is True


def test_auto_anchor_marks_when_below_threshold():
    s = _FakeSpan(attrs={"gen_ai.eval.score": 0.5})
    marked = auto_anchor_on_eval_fail(s, threshold=0.7)
    assert marked is True
    assert s.attributes["satsignal.anchor"] is True
    assert s.attributes["satsignal.anchor.mode"] == "single"


def test_auto_anchor_skips_when_above_threshold():
    s = _FakeSpan(attrs={"gen_ai.eval.score": 0.9})
    marked = auto_anchor_on_eval_fail(s, threshold=0.7)
    assert marked is False
    assert "satsignal.anchor" not in s.attributes


def test_auto_anchor_skips_when_no_score():
    s = _FakeSpan()
    marked = auto_anchor_on_eval_fail(s, threshold=0.7)
    assert marked is False


# ---- retry behaviour -------------------------------------------------

def test_retry_succeeds_on_second_attempt():
    attempts = {"n": 0}

    def fn():
        attempts["n"] += 1
        if attempts["n"] < 2:
            raise APIError(503, "transient", "later")
        from satsignal_otel._anchor import AnchorResult
        return AnchorResult(
            bundle_id="b", txid="t", mode="standard",
            matter_slug="m", receipt_url="r", bundle_url=None,
            duplicate=False,
        )

    result = call_with_retry(
        fn,
        sleep=lambda _: None,  # don't actually sleep in tests
    )
    assert attempts["n"] == 2
    assert result.bundle_id == "b"


def test_retry_does_not_retry_on_4xx():
    attempts = {"n": 0}

    def fn():
        attempts["n"] += 1
        raise APIError(400, "bad", "you sent a bad thing")

    with pytest.raises(APIError):
        call_with_retry(fn, sleep=lambda _: None)
    assert attempts["n"] == 1


def test_retry_gives_up_after_attempts_exhausted():
    attempts = {"n": 0}

    def fn():
        attempts["n"] += 1
        raise APIError(503, "transient", "later")

    with pytest.raises(APIError):
        call_with_retry(fn, attempts=3, sleep=lambda _: None)
    assert attempts["n"] == 3
