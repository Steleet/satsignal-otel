"""Additive folder/proof vocabulary alias — compat + conflict tests.

Per coordinator policy:
  * legacy ``matter_slug=`` (ctor + api) keeps working byte-identically
  * the new ``folder_slug=`` surface works
  * ctor ``matter_slug`` is no longer hard-required: EITHER
    ``folder_slug`` OR ``matter_slug`` satisfies it (raise if NEITHER)
  * conflict rule: both supplied, different non-empty -> ValueError
  * WIRE-TOKEN POLICY: the HTTP body still sends ``matter_slug``
No on-chain / live calls (transport mocked).
"""
from __future__ import annotations

import json

import pytest

from satsignal_otel import SatsignalSpanProcessor
from satsignal_otel._anchor import SatsignalApi, resolve_folder_alias


# ───────────────────── resolve_folder_alias core ─────────────────────

def test_resolve_neither():
    assert resolve_folder_alias(None, None) is None
    assert resolve_folder_alias("", "") is None


def test_resolve_legacy_only_unchanged():
    assert resolve_folder_alias(None, "otel-evals") == "otel-evals"


def test_resolve_new_only():
    assert resolve_folder_alias("f", None) == "f"


def test_resolve_both_equal_ok():
    assert resolve_folder_alias("x", "x") == "x"


def test_resolve_both_differ_raises():
    with pytest.raises(ValueError) as ei:
        resolve_folder_alias("fA", "mB")
    m = str(ei.value)
    assert "aliases" in m and "use folder" in m


# ───────────────────── api wire body ─────────────────────

class _Cap:
    def __init__(self, body=None):
        self.calls = []
        self.body = body or {
            "bundle_id": "b" * 16, "txid": "dead", "mode": "standard",
            "matter_slug": "srv", "receipt_url": "https://r",
            "bundle_url": None, "duplicate": False,
        }

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append(json.loads(body.decode("utf-8")) if body else None)
        return 200, json.dumps(self.body).encode("utf-8")


def test_anchor_standard_legacy_wire_body():
    t = _Cap()
    api = SatsignalApi(api_base="https://app", api_key="sk", transport=t)
    res = api.anchor_standard(matter_slug="legacy", sha256_hex="a" * 64)
    assert t.calls[0]["matter_slug"] == "legacy"
    assert "folder_slug" not in t.calls[0]
    assert res.matter_slug == res.folder_slug  # read alias


def test_anchor_standard_new_kwarg_folds_into_matter_slug():
    t = _Cap()
    api = SatsignalApi(api_base="https://app", api_key="sk", transport=t)
    api.anchor_standard(folder_slug="newf", sha256_hex="a" * 64)
    assert t.calls[0]["matter_slug"] == "newf"
    assert "folder_slug" not in t.calls[0]


def test_anchor_standard_conflict_raises():
    t = _Cap()
    api = SatsignalApi(api_base="https://app", api_key="sk", transport=t)
    with pytest.raises(ValueError):
        api.anchor_standard(folder_slug="f", matter_slug="m",
                            sha256_hex="a" * 64)
    assert t.calls == []


def test_anchor_manifest_legacy_and_new():
    t = _Cap(body={
        "bundle_id": "b", "txid": "x", "mode": "manifest",
        "matter_slug": "srv", "receipt_url": "https://r",
        "bundle_url": None, "duplicate": False,
    })
    api = SatsignalApi(api_base="https://app", api_key="sk", transport=t)
    api.anchor_manifest(matter_slug="legacy",
                        items=[{"label": "a", "sha256_hex": "a" * 64}])
    assert t.calls[0]["matter_slug"] == "legacy"
    api.anchor_manifest(folder_slug="newf",
                        items=[{"label": "a", "sha256_hex": "a" * 64}])
    assert t.calls[1]["matter_slug"] == "newf"
    assert "folder_slug" not in t.calls[1]


def test_anchor_reads_new_response_keys():
    t = _Cap(body={
        "proof_id": "p1", "txid": "x", "mode": "standard",
        "folder_slug": "ff", "proof_url": "https://new",
        "bundle_id": "OLD", "matter_slug": "OLDM",
        "receipt_url": "https://old", "bundle_url": None,
        "duplicate": False,
    })
    api = SatsignalApi(api_base="https://app", api_key="sk", transport=t)
    res = api.anchor_standard(folder_slug="ff", sha256_hex="a" * 64)
    assert res.bundle_id == "p1"
    assert res.matter_slug == "ff"
    assert res.receipt_url == "https://new"


# ───────────────────── processor ctor either-of ─────────────────────

def test_ctor_legacy_matter_slug_unchanged():
    p = SatsignalSpanProcessor(api_key="sk", matter_slug="legacy",
                               transport=_Cap())
    try:
        assert p.matter_slug == "legacy"
        assert p.folder_slug == "legacy"
        assert p._matter_slug == "legacy"  # legacy private attr
    finally:
        p.shutdown()


def test_ctor_new_folder_slug():
    p = SatsignalSpanProcessor(api_key="sk", folder_slug="newf",
                               transport=_Cap())
    try:
        assert p.folder_slug == "newf"
        assert p.matter_slug == "newf"
    finally:
        p.shutdown()


def test_ctor_neither_raises():
    with pytest.raises(ValueError) as ei:
        SatsignalSpanProcessor(api_key="sk", transport=_Cap())
    assert "required" in str(ei.value)


def test_ctor_conflict_raises():
    with pytest.raises(ValueError) as ei:
        SatsignalSpanProcessor(api_key="sk", folder_slug="A",
                               matter_slug="B", transport=_Cap())
    assert "aliases" in str(ei.value)


def test_ctor_both_equal_ok():
    p = SatsignalSpanProcessor(api_key="sk", folder_slug="same",
                               matter_slug="same", transport=_Cap())
    try:
        assert p.folder_slug == "same"
    finally:
        p.shutdown()
