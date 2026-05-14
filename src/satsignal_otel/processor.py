"""Satsignal ``SpanProcessor`` for the OpenTelemetry Python SDK.

Drop into your ``TracerProvider``::

    from opentelemetry.sdk.trace import TracerProvider
    from satsignal_otel import SatsignalSpanProcessor

    provider = TracerProvider()
    provider.add_span_processor(
        SatsignalSpanProcessor(
            api_key=os.environ["SATSIGNAL_API_KEY"],
            matter_slug="otel-evals",
        ),
    )

Only spans carrying the boolean attribute ``satsignal.anchor=true`` are
anchored — everything else flows through untouched. Matching spans are
queued; a daemon worker thread flushes the queue every
``flush_interval`` seconds (or sooner if ``max_batch_size`` is reached)
and posts a single manifest-mode anchor that binds all leaves with a
Merkle root.

Spans tagged with ``satsignal.anchor.mode=single`` are posted as their
own standard-mode anchor instead of being batched. Right for low-rate
high-value events that deserve a clean per-event receipt.
"""
from __future__ import annotations

import logging
import queue
import sys
import threading
import time
from dataclasses import dataclass
from datetime import date
from typing import Any, Optional

from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanProcessor

from . import _anchor
from ._anchor import APIError, AnchorResult, SatsignalApi
from .canonical import span_sha256, trace_span_session_id


# Attribute names. Public surface — don't rename without a major.
ATTR_ANCHOR = "satsignal.anchor"
ATTR_MODE = "satsignal.anchor.mode"           # "single" opts out of batching
ATTR_LABEL = "satsignal.anchor.label"         # optional display label
ATTR_FORCE_NEW = "satsignal.anchor.force_new" # bypass server dedup for the span

_log = logging.getLogger("satsignal_otel")
_SHUTDOWN_SENTINEL: Any = object()


@dataclass
class _QueuedSpan:
    sha256_hex: str
    label: str
    session_id: Optional[str]
    mode: str           # "single" or "manifest"
    force_new: bool


class SatsignalSpanProcessor(SpanProcessor):
    """Anchor selected OTel spans to BSV via Satsignal.

    Parameters
    ----------
    api_key
        Bearer key from your Satsignal workspace. Required.
    matter_slug
        Workspace matter the receipts file under (e.g. ``"otel-evals"``).
        Required.
    base_url
        Override the Satsignal API host. Defaults to the production
        host. Note: ``app.satsignal.cloud`` is the customer-API host;
        ``proof.satsignal.cloud`` is the verifier surface and 404s on
        POST /api/v1/anchors.
    flush_interval
        Seconds between worker-thread flushes (default 60.0). The
        plan calls for a 1-5 min window; 60s is the lower bound so a
        burst of failed-eval spans still ships within a minute.
    max_batch_size
        Force a flush when the queue hits this many spans (default
        500). Caps the per-request item count well below the server's
        10000-leaf limit so a single bad batch never wedges the whole
        flush.
    daily_anchor_cap
        Soft client-side cap to prevent a misconfigured filter from
        runaway-anchoring (default 1000). Counted by UTC date; reset
        at midnight. Exceeded spans drop with a warning.
    fail_open
        If True (default), API errors are logged + dropped. If False,
        ``shutdown()`` re-raises the last error. The on-end path is
        never blocking — fail_open=False only affects shutdown
        semantics.
    transport
        Optional ``(method, url, headers, body, timeout) -> (status,
        bytes)`` callable for tests. Default uses ``urllib``.
    """

    def __init__(
        self,
        *,
        api_key: str,
        matter_slug: str,
        base_url: str = _anchor.DEFAULT_API_BASE,
        flush_interval: float = 60.0,
        max_batch_size: int = 500,
        daily_anchor_cap: int = 1000,
        fail_open: bool = True,
        transport: Optional[Any] = None,
    ):
        if not api_key:
            raise ValueError("api_key is required")
        if not matter_slug:
            raise ValueError("matter_slug is required")
        if flush_interval <= 0:
            raise ValueError("flush_interval must be > 0")
        if max_batch_size <= 0:
            raise ValueError("max_batch_size must be > 0")

        self._api = SatsignalApi(
            api_base=base_url, api_key=api_key, transport=transport,
        )
        self._matter_slug = matter_slug
        self._flush_interval = float(flush_interval)
        self._max_batch_size = int(max_batch_size)
        self._daily_anchor_cap = int(daily_anchor_cap)
        self._fail_open = bool(fail_open)

        self._queue: "queue.Queue[Any]" = queue.Queue()
        self._stopped = threading.Event()
        self._last_error: Optional[Exception] = None

        # Quota counting: spans we've added to the queue today. Reset
        # by date so 30-day uptime doesn't slowly tighten the gate.
        self._quota_lock = threading.Lock()
        self._quota_date: date = date.today()
        self._quota_used: int = 0

        self._worker = threading.Thread(
            target=self._run_worker,
            name="satsignal-otel-worker",
            daemon=True,
        )
        self._worker.start()

    # ---- SpanProcessor contract --------------------------------------

    def on_start(self, span, parent_context=None) -> None:
        # No-op. We can't read the anchor flag yet because attributes
        # may be set after the span starts.
        return None

    def on_end(self, span: ReadableSpan) -> None:
        if self._stopped.is_set():
            return
        attrs = span.attributes or {}
        if not bool(attrs.get(ATTR_ANCHOR)):
            return
        if not self._reserve_quota():
            _log.warning(
                "satsignal-otel daily anchor cap (%d) hit — span %r dropped",
                self._daily_anchor_cap, getattr(span, "name", None),
            )
            return

        try:
            sha, _bytes = span_sha256(span)
        except Exception as e:  # noqa: BLE001 — defensive: never crash on_end
            _log.warning("satsignal-otel canonicalization failed: %s", e)
            return

        label = attrs.get(ATTR_LABEL) or getattr(span, "name", "") or ""
        mode = (attrs.get(ATTR_MODE) or "manifest").strip().lower()
        if mode not in ("single", "manifest"):
            mode = "manifest"
        force_new = bool(attrs.get(ATTR_FORCE_NEW))
        session_id = trace_span_session_id(span)

        self._queue.put(_QueuedSpan(
            sha256_hex=sha,
            label=str(label),
            session_id=session_id,
            mode=mode,
            force_new=force_new,
        ))

    def shutdown(self) -> None:
        if self._stopped.is_set():
            return
        self._stopped.set()
        self._queue.put(_SHUTDOWN_SENTINEL)
        self._worker.join(timeout=30.0)
        if self._last_error is not None and not self._fail_open:
            raise self._last_error

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        """Wait until the worker has drained the queue or ``timeout``
        expires. Returns True if the queue is empty by the deadline.
        """
        deadline = time.monotonic() + (timeout_millis / 1000.0)
        while time.monotonic() < deadline:
            if self._queue.unfinished_tasks == 0:
                return True
            time.sleep(0.05)
        return self._queue.unfinished_tasks == 0

    # ---- worker thread ------------------------------------------------

    def _run_worker(self) -> None:
        # Drain spans into a per-flush buffer. We block on .get() up to
        # `flush_interval`; if anything arrives we keep accumulating
        # until the timer expires or the buffer fills up.
        while not self._stopped.is_set():
            buf: list[_QueuedSpan] = []
            deadline = time.monotonic() + self._flush_interval
            while True:
                remaining = max(0.0, deadline - time.monotonic())
                try:
                    item = self._queue.get(timeout=remaining or 0.001)
                except queue.Empty:
                    break
                if item is _SHUTDOWN_SENTINEL:
                    self._queue.task_done()
                    self._flush(buf)
                    return
                buf.append(item)
                self._queue.task_done()
                if len(buf) >= self._max_batch_size:
                    break
            if buf:
                self._flush(buf)
        # Drain anything that snuck in between the shutdown signal and
        # the loop exit.
        rest: list[_QueuedSpan] = []
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            if item is _SHUTDOWN_SENTINEL:
                self._queue.task_done()
                continue
            rest.append(item)
            self._queue.task_done()
        if rest:
            self._flush(rest)

    def _flush(self, items: list[_QueuedSpan]) -> None:
        if not items:
            return
        singles = [x for x in items if x.mode == "single"]
        batched = [x for x in items if x.mode != "single"]

        for x in singles:
            self._post_single(x)
        if batched:
            self._post_manifest(batched)

    def _post_single(self, x: _QueuedSpan) -> None:
        def fn() -> AnchorResult:
            return self._api.anchor_standard(
                matter_slug=self._matter_slug,
                sha256_hex=x.sha256_hex,
                label=x.label or None,
                session_id=x.session_id,
                force_new=x.force_new,
            )
        try:
            _anchor.call_with_retry(fn)
        except APIError as e:
            self._handle_api_error(e, n_spans=1, mode="single")

    def _post_manifest(self, batch: list[_QueuedSpan]) -> None:
        # Manifest items[] is bounded server-side to MAX_MANIFEST_LEAVES.
        # max_batch_size keeps us under that by construction.
        items = [
            {"label": x.label or "", "sha256_hex": x.sha256_hex}
            for x in batch
        ]
        session_id = next(
            (x.session_id for x in batch if x.session_id), None,
        )

        def fn() -> AnchorResult:
            return self._api.anchor_manifest(
                matter_slug=self._matter_slug,
                items=items,
                session_id=session_id,
            )
        try:
            _anchor.call_with_retry(fn)
        except APIError as e:
            self._handle_api_error(e, n_spans=len(batch), mode="manifest")

    def _handle_api_error(self, exc: APIError, *, n_spans: int, mode: str) -> None:
        self._last_error = exc
        _log.warning(
            "satsignal-otel %s anchor failed (n=%d): %s [%s]",
            mode, n_spans, exc.message or exc.code, exc.status,
        )
        if not self._fail_open:
            # On-end path doesn't raise — we surface the error at
            # shutdown(). This matches BatchSpanProcessor's behavior
            # under transient backend failures.
            sys.stderr.write(
                f"[satsignal-otel] anchor failed: {exc}\n",
            )

    # ---- quota --------------------------------------------------------

    def _reserve_quota(self) -> bool:
        with self._quota_lock:
            today = date.today()
            if today != self._quota_date:
                self._quota_date = today
                self._quota_used = 0
            if self._quota_used >= self._daily_anchor_cap:
                return False
            self._quota_used += 1
            return True
