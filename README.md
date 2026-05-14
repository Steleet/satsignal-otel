# satsignal-otel

OpenTelemetry `SpanProcessor` that anchors selected GenAI spans to BSV
via [Satsignal](https://satsignal.cloud). One integration covers any
observability stack already speaking OTel — Langfuse, LangSmith,
Arize, Datadog, Honeycomb — and adds a tamper-evident receipt for the
spans that matter.

> **Your observability stack shows the run. Satsignal proves the run
> record hasn't been edited since.**

```bash
pip install satsignal-otel
```

## What it does

Drop the processor into your `TracerProvider`. Only spans carrying the
attribute `satsignal.anchor=true` are anchored — everything else flows
through to your existing exporters untouched. Matching spans are
batched and posted as a single manifest-mode anchor on BSV, binding
all leaves under one Merkle root. The on-chain anchor is the receipt
your auditor uses to prove the span record has not been edited since
that block was mined.

By default the SDK is **opt-in per span**, **batched** (1-minute
window), **sha-only** (your span bytes never leave the process), and
**fail-open** (anchor failures log + drop; your app keeps running).

## Failed-eval auto-anchor (headline)

Wire one line into your eval pipeline. When a scorer drops below
threshold, mark the span — Satsignal anchors the failure with a per-
span receipt so the timing claim ("we knew at 14:32 UTC") is provable.

```python
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from satsignal_otel import SatsignalSpanProcessor, auto_anchor_on_eval_fail

provider = TracerProvider()
provider.add_span_processor(SatsignalSpanProcessor(
    api_key=os.environ["SATSIGNAL_API_KEY"],
    matter_slug="otel-evals",
))
trace.set_tracer_provider(provider)

tracer = trace.get_tracer(__name__)

with tracer.start_as_current_span("eval.scorer") as span:
    score = run_scorer(prompt, response)
    span.set_attribute("gen_ai.eval.score", score)
    auto_anchor_on_eval_fail(span, threshold=0.7)
```

The helper sets `satsignal.anchor=true` only when `score < threshold`;
under threshold spans are anchored individually (`mode=single`) so the
receipt timing is per-event, not amortized across a batch.

## Release-gate anchor (secondary)

At deploy time, mark the release-manifest span. One receipt per
release — what shipped, with the eval evidence around it.

```python
from satsignal_otel import mark_for_anchor

with tracer.start_as_current_span("release.gate") as span:
    span.set_attribute("gen_ai.system", "anthropic")
    span.set_attribute("gen_ai.model", "claude-opus-4-7")
    span.set_attribute("prompt.version", PROMPT_VERSION)
    span.set_attribute("eval.pass_rate", PASS_RATE)
    span.set_attribute("config.hash", CONFIG_HASH)
    mark_for_anchor(span, mode="single", label=f"release-{GIT_SHA}")
```

## Configuration

```python
SatsignalSpanProcessor(
    api_key,                    # required: SATSIGNAL_API_KEY
    matter_slug,                # required: workspace matter for receipts
    base_url="https://app.satsignal.cloud",
    flush_interval=60.0,        # seconds between manifest flushes
    max_batch_size=500,         # force-flush when queue hits this size
    daily_anchor_cap=1000,      # client-side guard against runaway anchoring
    fail_open=True,             # log + drop on anchor failure
    transport=None,             # inject a callable for tests (see below)
)
```

### Attributes the processor reads

| Attribute | Type | Effect |
| --------- | ---- | ------ |
| `satsignal.anchor` | bool | **Required to anchor.** `True` → span is queued. |
| `satsignal.anchor.mode` | str | `"manifest"` (default, batched) or `"single"` (per-span anchor). |
| `satsignal.anchor.label` | str | Optional display label on the receipt (truncated at 256 chars). |
| `satsignal.anchor.force_new` | bool | `True` bypasses server-side dedup (single mode only). |

## What gets anchored

The sha256 of a deterministic canonical-JSON encoding of the span's
`{name, kind, status, start_time, end_time, attributes, events, links,
resource}`. The bytes never leave your process — only the hash. The
OTel `trace_id` and `span_id` ride along as an off-chain `session_id`
so a verifier can correlate the receipt back to your existing trace
data.

This is **chain-of-custody** for the span record, not the underlying
prompt + completion bytes. Your trace store still owns the bytes; the
anchor proves they have not been edited since.

## Threat model

- **Span attributes are attacker-controllable** when prompts include
  user-provided strings. The label is treated as untrusted display
  text server-side (length-capped, harness-string-rejected). The
  sha256 is bytes-are-bytes.
- **An anchor proves anchorer-knowledge, not world-existence.** The
  on-chain receipt commits the anchorer's knowledge of the canonical
  bytes at the anchored time; it does NOT prove the span existed
  before then. For end-to-end provenance pair this with a commit-
  reveal flow over the underlying prompt + completion.
- **Semantic-convention churn.** The GenAI OTel semantic conventions
  are still in development. `auto_anchor_on_eval_fail` reads
  `gen_ai.eval.score`, `gen_ai.evaluation.score`, and `eval.score` —
  pass `score_attribute=` if your stack names it something else, or
  pass `score=` directly.

## Sister packages

- [`satsignal-cli`](https://github.com/Steleet/satsignal-cli) — verify
  receipts and bundles on disk.
- [`satsignal-mcp`](https://github.com/Steleet/satsignal-mcp) —
  agent-callable anchoring + lookup tools via Model Context Protocol.
- [`langchain-satsignal`](https://github.com/Steleet/langchain-satsignal)
  — LangChain callback that anchors policy snapshots + decisions +
  evidence manifests.
- [`Steleet/satsignal-action`](https://github.com/Steleet/satsignal-action)
  — GitHub Action that anchors build artifacts as a workflow step.

## Testing without the network

The processor accepts a `transport=` callable matching urllib's shape:

```python
def fake(method, url, headers, body, timeout):
    return 200, b'{"bundle_id": "abc", "txid": "deadbeef", ...}'

processor = SatsignalSpanProcessor(
    api_key="sk_test", matter_slug="m", transport=fake,
)
```

The full test suite exercises the processor against a mock transport;
see `tests/test_processor.py` for canned-response patterns.

## License

MIT.
