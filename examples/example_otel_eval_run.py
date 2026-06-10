#!/usr/bin/env python3
"""example_otel_eval_run.py — OTel dogfood companion to example_eval_run.py.

Same five math-grading submissions as ``example_eval_run.py``; same
deterministic grader. The difference is the receipt path: instead of
calling ``/api/v1/anchors`` directly, this script emits OTel spans and
lets ``satsignal-otel``'s ``SatsignalSpanProcessor`` decide what to
anchor.

The point of the example: **anchoring is opt-in per span**. We mark
failing grades with ``satsignal.anchor=true`` via
``auto_anchor_on_eval_fail``; passing grades flow through to your
existing exporters untouched. The processor batches the marked spans
into one manifest receipt at shutdown.

Usage:

    pip install satsignal-otel opentelemetry-api opentelemetry-sdk

    # Dry run — nothing leaves the machine; uses a mock transport.
    python3 example_otel_eval_run.py

    # Real anchor — broadcasts ONE manifest receipt to BSV.
    SATSIGNAL_API_KEY=sk_... python3 example_otel_eval_run.py

Environment:
    SATSIGNAL_API_KEY    bearer token; if unset, runs in dry-run mode
                         and uses a mock transport (no broadcast).
    SATSIGNAL_FOLDER     workspace folder slug (default: otel-spans)
    SATSIGNAL_MATTER     deprecated legacy alias of SATSIGNAL_FOLDER;
                         still honored — error if both are set to
                         different values.
    SATSIGNAL_API_BASE   API host (default: https://app.satsignal.cloud)

Cost: one manifest BSV transaction covers all failed leaves
(~120 sats ≈ $0.0001 at $60/BSV).
"""
from __future__ import annotations

import json
import os
import sys

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.resources import Resource

from satsignal_otel import (
    SatsignalSpanProcessor,
    auto_anchor_on_eval_fail,
)
from satsignal_otel._anchor import resolve_folder_alias


def _folder_from_env(default: str = "otel-spans") -> str:
    """SATSIGNAL_FOLDER is canonical; SATSIGNAL_MATTER is the
    deprecated legacy fallback. Both set + different -> ValueError
    (same rule as the constructor kwargs)."""
    slug = resolve_folder_alias(
        os.environ.get("SATSIGNAL_FOLDER", "").strip(),
        os.environ.get("SATSIGNAL_MATTER", "").strip(),
        source="SATSIGNAL_FOLDER/SATSIGNAL_MATTER",
    )
    return slug or default


# ---- the eval (same SUBMISSIONS as example_eval_run.py) -------------

SUBMISSIONS = [
    ("Q1", "12", "12"),
    ("Q2", "24", "21"),     # incorrect — will be anchored
    ("Q3", "7",  "7"),
    ("Q4", "84", "12 * 7 = 84"),
    ("Q5", "10", "9"),      # incorrect — will be anchored
]

GRADER_THRESHOLD = 0.5  # below 0.5 → anchor


def grade(item_id: str, expected: str, student_answer: str) -> dict:
    """Same deterministic grader as example_eval_run.py — substring
    match on extracted digits."""
    digits_e = "".join(c for c in expected if c.isdigit())
    digits_a = "".join(c for c in student_answer if c.isdigit())
    correct = bool(digits_e) and digits_e in digits_a
    return {
        "item_id": item_id,
        "verdict": "correct" if correct else "incorrect",
        "score": 1.0 if correct else 0.0,
        "expected": expected,
        "student_answer": student_answer,
    }


# ---- mock transport for the no-key dry-run --------------------------

class _MockTransport:
    """Captures every POST and returns a canned 200. Used when no
    SATSIGNAL_API_KEY is set so the script is hermetic in CI."""

    def __init__(self):
        self.calls: list = []

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append((method, url, body))
        # Canonical response keys only — matches the live API
        # (vocabulary sunset; legacy bundle_id/receipt_url/matter_slug
        # keys are gone from 2xx responses).
        canned = {
            "proof_id": "DRYRUN_PROOF_ID",
            "txid": "DRYRUN_TXID",
            "mode": "manifest",
            "folder_slug": _folder_from_env(),
            "proof_url":
                "https://app.satsignal.cloud/w/<workspace>/m/otel-spans/r/DRYRUN",
            "bundle_url": None,
            "duplicate": False,
            "leaf_count": 0,
            "root": "0" * 64,
        }
        return 200, json.dumps(canned).encode("utf-8")


# ---- run -----------------------------------------------------------

def main() -> int:
    api_key = os.environ.get("SATSIGNAL_API_KEY", "").strip()
    folder_slug = _folder_from_env()
    base_url = (
        os.environ.get("SATSIGNAL_API_BASE", "https://app.satsignal.cloud")
        .rstrip("/")
    )

    is_dry_run = not api_key
    mock = _MockTransport() if is_dry_run else None

    provider = TracerProvider(
        resource=Resource.create({
            "service.name": "satsignal-otel-dogfood",
            "service.version": "0.1.0",
            "deployment.environment": "example",
        }),
    )
    processor = SatsignalSpanProcessor(
        api_key=api_key or "sk_dryrun",
        folder_slug=folder_slug,
        base_url=base_url,
        flush_interval=2.0,    # short window so the example flushes fast
        daily_anchor_cap=10,   # conservative cap for dogfood
        transport=mock,
    )
    provider.add_span_processor(processor)
    trace.set_tracer_provider(provider)
    tracer = trace.get_tracer("satsignal-otel-dogfood")

    print(f"# satsignal-otel dogfood eval")
    print(f"# folder_slug    = {folder_slug}")
    print(f"# base_url       = {base_url}")
    print(f"# dry_run        = {is_dry_run}")
    print()

    results = []
    for item_id, expected, student in SUBMISSIONS:
        with tracer.start_as_current_span(f"eval.grade.{item_id}") as span:
            r = grade(item_id, expected, student)
            results.append(r)
            span.set_attribute("eval.item_id", item_id)
            span.set_attribute("eval.expected", expected)
            span.set_attribute("eval.student_answer", student)
            span.set_attribute("eval.verdict", r["verdict"])
            span.set_attribute("gen_ai.eval.score", r["score"])
            # mode="manifest" batches all failed leaves into ONE receipt
            # (cost-efficient default for the example). The helper's own
            # default is mode="single" — appropriate when each failure
            # warrants its own per-event timing claim.
            marked = auto_anchor_on_eval_fail(
                span, threshold=GRADER_THRESHOLD, mode="manifest",
            )
            tag = "ANCHOR" if marked else "skip"
            print(
                f"  [{tag}] {item_id} verdict={r['verdict']} "
                f"score={r['score']} expected={expected!r} student={student!r}",
            )

    print()
    print("# flushing processor (waits for the worker thread to drain)...")
    processor.shutdown()

    if is_dry_run:
        print()
        print("# DRY RUN — what would have been broadcast:")
        if not mock.calls:  # type: ignore[union-attr]
            print("  (no anchor calls — every span passed the threshold)")
        else:
            for _, url, body in mock.calls:  # type: ignore[union-attr]
                decoded = json.loads(body.decode("utf-8"))
                print(f"  POST {url}")
                print("  body:")
                print("    " + json.dumps(decoded, indent=2).replace(
                    "\n", "\n    ",
                ))
        print()
        print("# To broadcast a real receipt, re-run with:")
        print("#   SATSIGNAL_API_KEY=sk_... python3 example_otel_eval_run.py")
    else:
        print()
        print(
            "# Broadcast complete. Check the folder activity feed at:",
        )
        print(
            f"#   https://app.satsignal.cloud/w/<workspace>/m/{folder_slug}/activity",
        )
        print(
            "# The receipt binds all failed leaves under one Merkle root;",
        )
        print(
            "# the worker thread logs warnings to stderr on API failures.",
        )

    n_failed = sum(1 for r in results if r["score"] < GRADER_THRESHOLD)
    print()
    print(f"# done. {n_failed}/{len(results)} submissions anchored.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
