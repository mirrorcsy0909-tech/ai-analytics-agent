# New-scenario acceptance — 2026-10-18

The new suite passed **19 scenarios / 25 turns** on its first live run. Every
turn met its deterministic evidence or expected-failure checks and received a
separate, answer-hash-bound Codex review. No production workflow changes were
needed for this run. This is development acceptance authored and reviewed by
Codex, not an independent human study or a production accuracy estimate.

## Coverage

| Area | Scenarios | What was checked |
| --- | ---: | --- |
| Complex SQL | 7 | Monthly refunds, cumulative totals, revenue shares, distinct invoices, returned units, ranking within countries, missing customers |
| Instruction-like data | 1 | A product description containing a write instruction stays data; no write is attempted |
| Conversation | 2 | Product query followed by a chart; missing experiment counts supplied in a later turn |
| Visualization | 1 | Country chart uses verified net revenue including refunds |
| A/B testing | 3 | Significant decline, prespecified smaller hypothesis, sparse two-sided Fisher test and confidence level |
| Input recovery | 1 | Impossible conversion counts are rejected, then corrected counts are used |
| Write refusal | 1 | Refusal followed by a valid read of the unchanged database |
| Tool failure recovery | 2 | One injected SQL failure recovers; persistent failures stop, then a later turn succeeds after restoration |
| Checkpoint resume | 1 | One injected pre-response timeout leaves a checkpoint; resume completes the original question |

The fixture has **24 invented transactions**, five year-months, missing customer
IDs, duplicate invoice lines, refunds and a zero-quantity transaction. Net
revenue is 503 with no declared currency. It differs from the original eight-row
regression fixture. Table oracles are explicit SQL executed once when authoring
the suite. A/B probability references use pooled-z tail calculations and direct
hypergeometric enumeration, independently of the production A/B tool.

The suite was frozen before the first live run. Production tools, prompts and
workflow sources stayed unchanged throughout the run. Intentional failures count
as passed acceptance behavior only when they fail explicitly within bounds; they
are not successful analyses. The injected timeout is simulated before a real
model response, not a measurement of provider outage recovery.

## Results and limits

- Configured model: GPT-5.6, medium reasoning, Responses API.
- 25/25 turns passed deterministic checks and Codex answer review.
- 61 model calls, 37 tool calls, 96,480 reported tokens, 194.84 seconds total.
- Remote tracing was disabled for this acceptance run. The real retail database
  was never accessed or uploaded.
- The original twelve-task regression suite and its historical results remain
  unchanged. Durations from different suites are not an overhead comparison.
- New scenarios do not establish exhaustive SQL correctness, immunity to every
  prompt injection, all tie-breaking cases, or semantic accuracy on arbitrary
  business questions. A second independent human review is still useful.

Artifacts:

- [Frozen scenarios and expected results](../eval/acceptance_v1.json)
- [First live run, messages and evidence](../eval/results/acceptance_v1_first_run.json)
- [Separate answer reviews](../eval/results/acceptance_v1_reviews.json)
- [Recomputed score](../eval/results/acceptance_v1_score.json)
- [Architecture](architecture.md)
- [Portable recorded-output replay](demo/index.html), [animated preview](demo/preview.gif)

## Reproduce

Recompute archived checks and verify review fingerprints without credentials,
network access or model calls:

```bash
python eval/acceptance_score.py \
  --report eval/results/acceptance_v1_first_run.json \
  --reviews eval/results/acceptance_v1_reviews.json
```

Run a new live acceptance with the configured model and save to a new path:

```bash
python eval/acceptance.py --run --output outputs/acceptance_new_run.json
```

The runner refuses to overwrite an existing report. New answers need new reviews;
the old review file cannot certify a different recording. For a targeted rerun,
add `--only transient_sql_failure model_interrupt_resume`. Partial coverage never
produces a full-suite success rate.

Rebuild and view the recorded-output demo:

```bash
python docs/demo/build_demo.py
python -m http.server 8765 --bind 127.0.0.1 --directory docs
```

Open `http://127.0.0.1:8765/demo/`. The HTML and GIF replay actual archived results;
they are not screen recordings or live Agent sessions. The chart asset is copied
unchanged from the recorded tool output. CI verifies archived acceptance offline;
it does not spend model tokens or use account keys.
