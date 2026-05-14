"""Small ergonomic helpers for marking spans to anchor.

These are *thin* — they just set OTel attributes the SpanProcessor
already reads. They exist so the canonical examples in the README are
two lines instead of five, not because the underlying mechanism needs
them.
"""
from __future__ import annotations

from typing import Any, Optional

from .processor import (
    ATTR_ANCHOR,
    ATTR_FORCE_NEW,
    ATTR_LABEL,
    ATTR_MODE,
)


def mark_for_anchor(
    span: Any,
    *,
    label: Optional[str] = None,
    mode: str = "manifest",
    force_new: bool = False,
) -> None:
    """Mark a span so the ``SatsignalSpanProcessor`` will anchor it.

    Equivalent to ``span.set_attribute("satsignal.anchor", True)``
    with optional companions. ``mode`` must be ``"manifest"`` (batched,
    default) or ``"single"`` (per-span anchor — appropriate for low-
    rate high-value events).
    """
    if mode not in ("manifest", "single"):
        raise ValueError(f"mode must be 'manifest' or 'single', got {mode!r}")
    span.set_attribute(ATTR_ANCHOR, True)
    span.set_attribute(ATTR_MODE, mode)
    if label is not None:
        span.set_attribute(ATTR_LABEL, str(label))
    if force_new:
        span.set_attribute(ATTR_FORCE_NEW, True)


# Default attribute names. We read whichever is set; a customer running
# a stack that names the score differently can pass ``score_attribute=``.
# The GenAI semantic conventions are still in development — exact field
# names shift between spec versions, so we lean permissive.
_DEFAULT_SCORE_ATTRS = (
    "gen_ai.eval.score",
    "gen_ai.evaluation.score",
    "eval.score",
)


def auto_anchor_on_eval_fail(
    span: Any,
    *,
    threshold: float,
    score: Optional[float] = None,
    score_attribute: Optional[str] = None,
    mode: str = "single",
) -> bool:
    """Anchor this span iff its eval score is *below* ``threshold``.

    Returns True when the span was marked for anchoring. The score is
    read from (in order):

    1. The explicit ``score`` keyword argument.
    2. The attribute named by ``score_attribute``.
    3. Any of the default GenAI score attribute names.

    ``mode="single"`` is the default because a failed-eval span is
    typically what the auditor wants a per-event receipt for — batching
    obscures the timing claim. Set ``mode="manifest"`` if you want
    failed evals batched with other anchored spans.
    """
    if score is None:
        attrs = getattr(span, "attributes", None) or {}
        candidates = (score_attribute,) if score_attribute else _DEFAULT_SCORE_ATTRS
        for key in candidates:
            if key and key in attrs:
                try:
                    score = float(attrs[key])
                    break
                except (TypeError, ValueError):
                    continue
    if score is None:
        return False
    if score >= threshold:
        return False
    mark_for_anchor(
        span,
        label=f"eval_fail score={score:.4f} threshold={threshold:.4f}",
        mode=mode,
    )
    return True
