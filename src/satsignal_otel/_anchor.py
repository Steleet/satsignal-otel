"""Stdlib HTTP client for ``POST /api/v1/anchors``.

No third-party HTTP dep (mirrors ``langchain-satsignal``). A
``transport=`` hook lets tests inject canned responses so CI does not
burn chain fees.

Two anchor shapes:

- **single** — one span per request, body carries ``sha256_hex``.
- **manifest** — N spans per request, body carries
  ``items: [{label, sha256_hex}, ...]``. The server stamps a Merkle
  root + on-chain anchor that binds the set; each leaf's inclusion
  proof is recoverable from the bundle.

Both shapes accept ``session_id`` as an off-chain correlation field
(we pack the OTel ``trace_id:span_id`` into it). ``label`` is
attacker-controllable display text — the server enforces length caps
and rejects LLM-harness control substrings.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Optional


DEFAULT_API_BASE = "https://app.satsignal.cloud"

# Mirrors notary/manifest.MAX_LEAVES. A request with > 10000 items
# 400s server-side, so the SpanProcessor chunks before reaching here.
MAX_MANIFEST_LEAVES = 10000

# Hard-cap on label length the server accepts. Mirrors
# notary.manifest.MAX_LABEL_LEN; keeping it client-side lets us trim
# noisy span names before they 400 the whole batch.
MAX_LABEL_LEN = 256


class APIError(RuntimeError):
    """Non-2xx response from /api/v1/anchors.

    ``status`` is the HTTP status; ``code`` and ``message`` come from
    the server's ``{"error": {"code", "message"}}`` body when
    available. ``body`` is the parsed payload for callers that want
    to render the full response.
    """

    def __init__(self, status: int, code: str, message: str,
                 *, body: Optional[dict] = None):
        super().__init__(f"satsignal API {status} {code}: {message}")
        self.status = status
        self.code = code
        self.message = message
        self.body = body or {}


@dataclass
class AnchorResult:
    bundle_id: str
    txid: Optional[str]
    mode: str
    matter_slug: str
    receipt_url: str
    bundle_url: Optional[str]
    duplicate: bool
    leaf_count: Optional[int] = None
    root: Optional[str] = None
    session_id: Optional[str] = None
    raw: dict = field(default_factory=dict)


# Transport hook signature:
#   (method, url, headers, body_bytes, timeout) -> (status, response_bytes)
TransportFn = Callable[[str, str, dict, bytes, float], "tuple[int, bytes]"]


def _urllib_transport(
    method: str, url: str, headers: dict, body: bytes, timeout: float,
) -> "tuple[int, bytes]":
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.getcode(), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, (e.read() or b"")


def _parse_api_error(status: int, body_bytes: bytes) -> APIError:
    text = body_bytes.decode("utf-8", errors="replace")
    try:
        body = json.loads(text)
    except (ValueError, json.JSONDecodeError):
        return APIError(status, "non_json_response",
                        text[:200] or f"HTTP {status}")
    err = body.get("error") if isinstance(body, dict) else None
    if isinstance(err, dict):
        return APIError(
            status,
            str(err.get("code") or "unknown_error"),
            str(err.get("message") or ""),
            body=body,
        )
    if isinstance(body, dict) and isinstance(body.get("error"), str):
        return APIError(status, body["error"], "", body=body)
    return APIError(status, "unknown_error", str(body)[:200], body=body)


def _safe_label(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    s = str(value).strip()
    if not s:
        return None
    if len(s) > MAX_LABEL_LEN:
        s = s[:MAX_LABEL_LEN]
    return s


class SatsignalApi:
    """Synchronous HTTP client. One instance per ``SpanProcessor``.

    The worker thread holds this client and posts batches serially —
    OTel's SpanProcessor contract doesn't promise an event loop, so
    sync is the right shape here (we run async only in the MCP server
    where stdio_server requires it).
    """

    def __init__(
        self,
        *,
        api_base: str,
        api_key: str,
        timeout: float = 30.0,
        transport: Optional[TransportFn] = None,
        user_agent: str = "satsignal-otel/0.1.1",
    ):
        self.api_base = api_base.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self._transport: TransportFn = transport or _urllib_transport
        self._user_agent = user_agent

    # ---- POST /api/v1/anchors -----------------------------------------

    def anchor_standard(
        self,
        *,
        matter_slug: str,
        sha256_hex: str,
        file_size: Optional[int] = None,
        label: Optional[str] = None,
        session_id: Optional[str] = None,
        force_new: bool = False,
    ) -> AnchorResult:
        body: dict[str, Any] = {
            "matter_slug": matter_slug,
            "sha256_hex": sha256_hex.lower().strip(),
        }
        if file_size is not None:
            body["file_size"] = int(file_size)
        label = _safe_label(label)
        if label:
            body["label"] = label
        if session_id:
            body["session_id"] = session_id
        if force_new:
            body["force_new"] = True
        return self._post_anchor(body, default_mode="standard",
                                  matter_slug=matter_slug,
                                  session_id=session_id)

    def anchor_manifest(
        self,
        *,
        matter_slug: str,
        items: list,             # [{label, sha256_hex}, ...]
        label: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> AnchorResult:
        if not items:
            raise APIError(
                400, "empty_items",
                "items[] must not be empty (manifest batch needs >=1 leaf)",
            )
        if len(items) > MAX_MANIFEST_LEAVES:
            raise APIError(
                400, "too_many_items",
                f"items[] has {len(items)} > {MAX_MANIFEST_LEAVES} leaves; "
                "split into smaller batches",
            )
        clean: list[dict] = []
        for i, it in enumerate(items):
            sha = (it.get("sha256_hex") or "").lower().strip()
            if len(sha) != 64:
                raise APIError(
                    400, "bad_sha",
                    f"items[{i}].sha256_hex must be 64 hex chars",
                )
            leaf_label = _safe_label(it.get("label")) or ""
            clean.append({"label": leaf_label, "sha256_hex": sha})
        body: dict[str, Any] = {
            "matter_slug": matter_slug,
            "items": clean,
        }
        label = _safe_label(label)
        if label:
            body["label"] = label
        if session_id:
            body["session_id"] = session_id
        return self._post_anchor(body, default_mode="manifest",
                                  matter_slug=matter_slug,
                                  session_id=session_id)

    # ---- internals ----------------------------------------------------

    def _post_anchor(
        self,
        body: dict,
        *,
        default_mode: str,
        matter_slug: str,
        session_id: Optional[str],
    ) -> AnchorResult:
        url = f"{self.api_base}/api/v1/anchors"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": self._user_agent,
        }
        raw_body = json.dumps(body, separators=(",", ":")).encode("utf-8")
        status, resp_bytes = self._transport(
            "POST", url, headers, raw_body, self.timeout,
        )
        if status >= 400:
            raise _parse_api_error(status, resp_bytes)
        try:
            data = json.loads(resp_bytes.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as e:
            raise APIError(status, "bad_response",
                           f"non-JSON 2xx body: {e}")
        return AnchorResult(
            bundle_id=str(data.get("bundle_id") or ""),
            txid=data.get("txid"),
            mode=str(data.get("mode") or default_mode),
            matter_slug=str(data.get("matter_slug") or matter_slug),
            receipt_url=str(data.get("receipt_url") or ""),
            bundle_url=data.get("bundle_url"),
            duplicate=bool(data.get("duplicate", False)),
            leaf_count=data.get("leaf_count"),
            root=data.get("root"),
            session_id=data.get("session_id") or session_id,
            raw=data,
        )


# ---- retry wrapper ---------------------------------------------------

# Retries on transient API errors. 3 tries total, exponential backoff:
# attempt 1 (immediate), attempt 2 after 1s, attempt 3 after 4s. After
# exhaustion the wrapper re-raises so the caller (worker thread) can
# log + drop the batch. Matches the plan: "3 tries + log warning +
# drop." 4xx errors are NOT retried — they're our fault, not a
# transient.

_TRANSIENT_STATUSES = frozenset({0, 408, 429, 500, 502, 503, 504})


def _is_transient(exc: APIError) -> bool:
    return exc.status in _TRANSIENT_STATUSES


def call_with_retry(
    fn: Callable[[], AnchorResult],
    *,
    attempts: int = 3,
    base_delay: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
) -> AnchorResult:
    last_exc: Optional[Exception] = None
    for i in range(attempts):
        try:
            return fn()
        except APIError as e:
            last_exc = e
            if not _is_transient(e) or i == attempts - 1:
                raise
            sleep(base_delay * (4 ** i))
    # Unreachable in practice — kept to satisfy type-checkers.
    raise last_exc  # type: ignore[misc]
