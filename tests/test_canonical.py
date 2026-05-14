"""Span canonicalization tests — duck-typed FakeSpan, no OTel SDK needed."""
from __future__ import annotations

from satsignal_otel.canonical import (
    canonical_fields,
    canonical_span_doc,
    canonicalize,
    sha256_hex,
    span_sha256,
    trace_span_session_id,
)


class _FakeKind:
    def __init__(self, name: str):
        self.name = name


class _FakeStatus:
    def __init__(self, code_name: str = "OK", description=None):
        class _Code:
            name = code_name
        self.status_code = _Code()
        self.description = description


class _FakeContext:
    def __init__(self, trace_id=0, span_id=0):
        self.trace_id = trace_id
        self.span_id = span_id


class _FakeResource:
    def __init__(self, attrs):
        self.attributes = attrs


class _FakeSpan:
    def __init__(
        self,
        *,
        name="span-1",
        kind="INTERNAL",
        attrs=None,
        start=1_700_000_000_000_000_000,
        end=1_700_000_000_500_000_000,
        events=None,
        links=None,
        resource_attrs=None,
        status_code="OK",
        trace_id=0xa1b2c3,
        span_id=0xdeadbeef,
    ):
        self.name = name
        self.kind = _FakeKind(kind)
        self.attributes = attrs or {}
        self.start_time = start
        self.end_time = end
        self.events = events or []
        self.links = links or []
        self.resource = _FakeResource(resource_attrs or {})
        self.status = _FakeStatus(status_code)
        self.context = _FakeContext(trace_id, span_id)


def test_canonical_fields_is_stable():
    assert canonical_fields() == (
        "name", "kind", "status", "start_time", "end_time",
        "attributes", "events", "links", "resource",
    )


def test_canonical_doc_extracts_documented_fields():
    span = _FakeSpan(attrs={"gen_ai.system": "anthropic", "score": 0.9})
    doc = canonical_span_doc(span)
    assert doc["name"] == "span-1"
    assert doc["kind"] == "INTERNAL"
    assert doc["status"] == {"code": "OK", "description": None}
    assert doc["attributes"] == {"gen_ai.system": "anthropic", "score": 0.9}


def test_canonicalize_is_deterministic_for_reordered_dicts():
    s1 = _FakeSpan(attrs={"b": 2, "a": 1})
    s2 = _FakeSpan(attrs={"a": 1, "b": 2})
    assert span_sha256(s1) == span_sha256(s2)


def test_canonicalize_changes_when_a_field_changes():
    s1 = _FakeSpan(attrs={"a": 1})
    s2 = _FakeSpan(attrs={"a": 2})
    assert span_sha256(s1)[0] != span_sha256(s2)[0]


def test_canonicalize_output_matches_known_sha():
    # Lock the wire shape: changing this expected hash means we changed
    # the canonical doc schema, which would silently invalidate every
    # customer's prior anchor. If you really mean to change it, bump a
    # major version.
    span = _FakeSpan(
        name="fixed",
        kind="CLIENT",
        attrs={"k": "v"},
        start=1,
        end=2,
        events=[],
        links=[],
        resource_attrs={"service.name": "demo"},
        status_code="OK",
        trace_id=0x1,
        span_id=0x2,
    )
    sha, raw = span_sha256(span)
    assert sha == sha256_hex(raw)
    assert sha == (
        "06898bfaa12c57e555bb5f82a126d5225cfb5e125d1bf8d068c82015b5705113"
    )


def test_session_id_packs_trace_and_span():
    span = _FakeSpan(trace_id=0x1, span_id=0x2)
    sid = trace_span_session_id(span)
    # trace_id is 32-hex, span_id is 16-hex.
    assert sid == "00000000000000000000000000000001:0000000000000002"


def test_canonicalize_rejects_nan():
    import math
    import pytest
    span = _FakeSpan(attrs={"x": math.nan})
    with pytest.raises(Exception):
        span_sha256(span)
