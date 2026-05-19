"""satsignal-otel — anchor selected OpenTelemetry GenAI spans to BSV.

Drop ``SatsignalSpanProcessor`` into your OTel ``TracerProvider`` and
mark spans you want a tamper-evident receipt for with the attribute
``satsignal.anchor=true``. Marked spans are batched and anchored on BSV
via Satsignal; everything else flows through untouched.

This package does not replace your observability stack — it sits
alongside it. Langfuse / LangSmith / Datadog / Honeycomb show the run;
Satsignal proves the run record was not edited after the fact.
"""
from __future__ import annotations

__version__ = "0.2.0"

from ._anchor import APIError
from .processor import SatsignalSpanProcessor
from .helpers import auto_anchor_on_eval_fail, mark_for_anchor

__all__ = [
    "SatsignalSpanProcessor",
    "APIError",
    "auto_anchor_on_eval_fail",
    "mark_for_anchor",
    "__version__",
]
