"""Deterministic canonicalization of an OpenTelemetry ``ReadableSpan``.

Anchoring a span means committing to a specific byte sequence — the
sha256 we put on-chain is only meaningful if any future holder of the
same span can reproduce the same bytes. ``ReadableSpan.to_json`` is
deliberately not used because its output is unstable across SDK
versions (key order, formatting, optional fields). We pull a fixed
field set into a plain dict and emit canonical JSON ourselves.

The canonicalization is JSON sort_keys + compact separators + UTF-8
(matches ``satsignal-mcp`` and ``langchain-satsignal``). Not full
RFC 8785 — sufficient for the OTel value shapes that show up in
practice (str / int / float / bool / None / list / dict).
"""
from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Mapping


# Fields lifted off the ReadableSpan into the canonical doc. Order in
# this tuple is irrelevant (sort_keys handles it) but the SET is
# load-bearing: adding a field later changes every customer's
# downstream sha256. Treated as part of the wire contract.
_CANONICAL_FIELDS = (
    "name",
    "kind",
    "status",
    "start_time",
    "end_time",
    "attributes",
    "events",
    "links",
    "resource",
)


class CanonicalizationError(ValueError):
    pass


def _safe_value(value: Any) -> Any:
    """Coerce OTel attribute values into JSON-safe primitives.

    OTel allows str / bool / int / float / sequences of those. Tuples
    become lists; bytes become hex (rare but possible via custom
    instrumentation). Anything else is repr'd — the sha covers SOME
    bytes, just not necessarily ones the customer reproduces, so the
    canonical doc is the source of truth.
    """
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise CanonicalizationError(
                f"non-finite float in span: {value!r}",
            )
        return value
    if isinstance(value, (list, tuple)):
        return [_safe_value(v) for v in value]
    if isinstance(value, Mapping):
        return {str(k): _safe_value(v) for k, v in value.items()}
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).hex()
    return repr(value)


def _attrs_dict(attrs) -> dict:
    if attrs is None:
        return {}
    # BoundedAttributes / dict / Mapping all support .items().
    return {str(k): _safe_value(v) for k, v in attrs.items()}


def _event_dict(event) -> dict:
    return {
        "name": getattr(event, "name", None),
        "timestamp": getattr(event, "timestamp", None),
        "attributes": _attrs_dict(getattr(event, "attributes", None)),
    }


def _link_dict(link) -> dict:
    ctx = getattr(link, "context", None)
    trace_id = getattr(ctx, "trace_id", None) if ctx is not None else None
    span_id = getattr(ctx, "span_id", None) if ctx is not None else None
    return {
        "trace_id": _hex(trace_id, 32) if trace_id is not None else None,
        "span_id": _hex(span_id, 16) if span_id is not None else None,
        "attributes": _attrs_dict(getattr(link, "attributes", None)),
    }


def _status_dict(status) -> dict:
    if status is None:
        return {"code": "UNSET", "description": None}
    code = getattr(status, "status_code", None)
    name = getattr(code, "name", None) if code is not None else None
    return {
        "code": name or "UNSET",
        "description": getattr(status, "description", None),
    }


def _hex(value: int, width: int) -> str:
    return format(value, "0{}x".format(width))


def canonical_span_doc(span) -> dict:
    """Extract the canonical document for a ReadableSpan-like object.

    The argument is duck-typed: anything with ``name``, ``kind``,
    ``start_time``, ``end_time``, ``attributes``, ``status``,
    ``events``, ``links``, ``resource`` works. This keeps tests free
    of the OTel SDK import.
    """
    kind = getattr(span, "kind", None)
    kind_name = getattr(kind, "name", None) if kind is not None else None
    resource = getattr(span, "resource", None)
    resource_attrs = getattr(resource, "attributes", None) if resource else None
    return {
        "name": getattr(span, "name", None),
        "kind": kind_name or "INTERNAL",
        "status": _status_dict(getattr(span, "status", None)),
        "start_time": getattr(span, "start_time", None),
        "end_time": getattr(span, "end_time", None),
        "attributes": _attrs_dict(getattr(span, "attributes", None)),
        "events": [_event_dict(e) for e in (getattr(span, "events", None) or [])],
        "links": [_link_dict(l) for l in (getattr(span, "links", None) or [])],
        "resource": _attrs_dict(resource_attrs),
    }


def canonicalize(doc: Any) -> bytes:
    """Return canonical JSON bytes for ``doc``. UTF-8 encoded."""
    return json.dumps(
        doc,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def span_sha256(span) -> tuple[str, bytes]:
    """Return ``(sha256_hex, canonical_bytes)`` for the given span.

    Both values are returned so the caller can persist the bytes
    alongside the receipt — verification later requires reproducing
    the same bytes locally. ``canonical_fields()`` documents the
    schema.
    """
    doc = canonical_span_doc(span)
    raw = canonicalize(doc)
    return sha256_hex(raw), raw


def canonical_fields() -> tuple[str, ...]:
    """Public view of the canonicalized field set (wire contract)."""
    return _CANONICAL_FIELDS


def trace_span_session_id(span) -> str | None:
    """Build ``trace_id:span_id`` off-chain session_id from a span's
    SpanContext. Returns None if neither id is available. Both ids
    are rendered as their canonical hex (32-char trace, 16-char span).
    """
    ctx = getattr(span, "context", None) or getattr(span, "get_span_context",
                                                     lambda: None)()
    if ctx is None:
        return None
    trace_id = getattr(ctx, "trace_id", None)
    span_id = getattr(ctx, "span_id", None)
    if not trace_id and not span_id:
        return None
    parts = []
    if trace_id:
        parts.append(_hex(trace_id, 32))
    if span_id:
        parts.append(_hex(span_id, 16))
    return ":".join(parts)
