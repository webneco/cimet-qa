# CIMET QA Gate

Scores an Energy sales call against the Retailer 1 compliance checklist **before**
the lead is submitted to the CRM, and decides one of three things: **HELD**, **QA**,
or **SUBMITTED**.

Local, one command, no Docker, no auth, no database. The only external service it
touches is the Anthropic API, and it runs end to end without a key.

---

## Run

```bash
python run.py
```

That is the whole setup. Python 3.10+, standard library only — the server, the
scoring engine and the UI have no third-party dependencies. It opens
<http://127.0.0.1:8787> and lead **3613790** is already scored when the page paints.

For LLM-backed Type B extraction, put a key in `.env` first:

```bash
cp .env.example .env        # then set ANTHROPIC_API_KEY
python run.py               # installs `anthropic` on first run if the key is set
```

Without a key the app still runs every check — Type B falls back to a built-in
deterministic extractor, the badge in the top right says so, and every result
records which extractor produced it. Nothing silently pretends to be an LLM.

Other entry points:

```bash
python run.py --selftest          # 68 behavioural assertions, offline, ~1s
python run.py --score 3613792     # score one lead in the terminal
python run.py --no-llm            # force the deterministic extractor
```

---

## Three-click demo

The first screen is already the worked example from the brief: lead 3613790,
status **HELD**.

**Click 1 — the `rates_and_charges` row.**
The transcript jumps to **14:02** and highlights the line. The evidence panel shows
the agent said **28.6 c/kWh**, the plan sold is **31.9 c/kWh**, a 3.3c gap. Under it,
`email_captured` failed at **22:10** — the customer said `j.smith@gmail.com`, the CRM
holds `j.smith@gmial.com`. Neither of those is a judgement call you have to trust:
both carry a quote, a timestamp, the reference value, and the comparison that produced
the verdict.

**Click 2 — Override to PASS on that row.**
Saving is refused until you write a reason, in the UI and again at the API. Save it
and the gate re-runs live:
the status chip stays **HELD**, because the email failure is still open. Clear that
one too and it flips to **SUBMITTED**. Both entries land in the append-only override
log on the right with who, why, the machine's original verdict, and the gate
transition. The CRM email is still `j.smith@gmial.com` — this app never writes to
the CRM.

**Click 3 — lead 3613792** (the messy-audio call).
Crosstalk, `[inaudible]`, a dropped line. The status is **QA**, not HELD: the DMO
statement, the rate and the email are all **unsure**, each with the guardrail that
downgraded them. Nothing was failed because it could not be heard. The checks that
*were* audible still passed.

Lead **3613791** is the clean control: every critical check passes and it goes
**SUBMITTED**.

---

## The gate

```
any critical check  status = fail                      ->  HELD       queued to a team leader
any critical check  status = unsure                    ->  QA         never SUBMITTED
any critical check  confidence < 0.60                  ->  QA         never SUBMITTED
otherwise                                              ->  SUBMITTED
5% of SUBMITTED runs                                   ->  flagged SAMPLE_AUDIT
```

Rules are evaluated in that order and the first match wins. Type C results are
removed from the list *before* any of it is evaluated, so a coaching note cannot
influence the outcome even if someone flips it.

HELD and QA both write a routing record to `runs/queue.jsonl`, so "queued to a TL"
is an actual queue entry, not a label on a screen.

Sampling is a stable hash of `salt:lead_id`, so a demo replays identically. Set
`CIMET_SAMPLE_AUDIT_RATE=1.0` to see the **SAMPLE AUDIT** badge on 3613791.

---

## How each check is scored

| Type | Method | LLM involved |
|---|---|---|
| **A** — script span | Token-level longest-common-subsequence match of each required phrase against a sliding window of turns, then the window is tightened to the smallest span that still carries the wording, so the timestamp is the moment it was said. | **No.** Type A has no hallucination surface at all. |
| **B** — extract and compare | The LLM reads the transcript and reports **what was said**. Deterministic code then compares that to the CRM or plan value. | Extraction only. The comparison is code. |
| **C** — observation | Arithmetic over turn timings: longest silence, count of talk-overs. | No. |

Type A thresholds and required phrases live in `data/checks.json`, versioned per
check (`check_version`, `effective_from`) and stamped onto every result.

Confidence is the machine's probability that its **verdict** is right, not how loud
the audio was. The exact formula is recorded in `evidence.confidence_formula` on
every result.

---

## Guardrails

**The LLM never sees the answer.** A Type B prompt is built from the check definition
and the transcript only — CRM and plan data are not in scope of the function that
builds it. Before the request is sent, `assert_no_reference_leak()` refuses any prompt
containing a CRM or plan value that is not itself spoken on the call. If it trips, the
request is never made and the check reports unsure. The model cannot supply the value
it is being measured against, so a "match" means the agent actually said it.

**Named, recorded downgrades.** Each one is attached to the result that it changed,
visible in the UI and in the audit JSON:

| | |
|---|---|
| **G1** audio-quality abstain | A Type A span that would fail, but the window contains `[inaudible]`/`[crosstalk]` or ASR confidence below 0.75, becomes **unsure**. Unheard is not the same as unsaid. |
| **G2** abstention honoured | The extractor reporting "I could not establish this" becomes **unsure**, never a fail. |
| **G3** ungrounded quote | A quote that does not appear in the transcript is discarded and the check reports unsure. Hallucinated evidence cannot reach a verdict. |
| **G4** not comparable | A spoken value that will not parse into comparable form reports unsure rather than guessing. |
| **G5** low-confidence critical fail | A critical fail below the 0.60 floor is downgraded to unsure, so an uncertain machine routes to QA instead of holding a customer's sale. |
| **G6** timestamp corrected | If the model's reported `start_sec` disagrees with the turn its quote came from, the transcript wins and the correction is logged. |

**Redaction.** Any run of 13 or more digits is masked at ingest, before scoring,
before the prompt is built, before anything is stored. Card numbers never reach the
model, the run artifact, or the screen. NMIs (10–11 digits) and phone numbers
deliberately fall below the threshold and survive intact.

**Overrides are bounded.** A written reason of at least 10 characters is required;
the machine verdict is preserved next to the human one; the gate is re-run and both
the before and after status are logged; entries are appended to
`runs/override_log.jsonl` and never rewritten. Type C checks cannot be overridden at
all — there is nothing to override, because they never affect the gate. No endpoint
in this app writes to the CRM.

**Silence is never evidence.** Lead 3613792 exists to prove it. Its DMO span scores
44% coverage, below the 55% unsure threshold, so the raw thresholds would fail it;
G1 converts it to unsure because the span is full of audio markers. `python run.py
--selftest` asserts exactly that, along with 67 other claims made on this page.

---

## Traceability

Every result carries: `check_id`, `check_version`, `effective_from`, `type`,
`critical`, `weight`, `status`, `confidence`, `quote`, `start_sec`, `turn_idx`,
`speaker`, `blocks_sale`, the guardrails that fired, and a type-specific `evidence`
block — for Type B that includes the extractor, the model, the prompt SHA-256, the
reference value and its source, the comparison, and whether the quote was grounded.

Each run writes a complete audit artifact to `runs/<run_id>.json`: the checklist
version in force, the transcript SHA-256, the CRM and plan snapshots, every result,
the gate decision with its reasons, the gate history, and the override log.
**Download audit JSON** in the UI is that file. A team leader can reconstruct any
decision from it without access to this process.

---

## Layout

```
data/leads.json               3 leads with read-only CRM snapshots
data/plans.json               plan catalogue - the reference for rate comparison
data/checks.json              Retailer 1 checklist, versioned per check
data/transcripts/<id>.json    diarised turns: speaker, text, start_sec, end_sec, asr_confidence
app/textutil.py               normalisation, LCS matching, digit redaction
app/llm.py                    Claude extraction + the leak guard
app/offline_extract.py        deterministic extractor used without a key
app/compare.py                the comparators - the only code that sees both sides
app/scoring.py                Type A / B / C scorers
app/gate.py                   the gate and the sample-audit draw
app/pipeline.py               ingest -> score -> gate, and overrides
app/server.py, app/web/       stdlib HTTP server and the single-page console
runs/                         audit artifacts, override log, routing queue
selftest.py                   68 behavioural assertions
```

### API

```
POST /api/ingest   {lead_id}      attach the local transcript (no file picker)
POST /api/score    {ingest_id}    score + gate, returns the full run
POST /api/run      {lead_id}      both of the above
POST /api/override {run_id, check_id, to_status, reason, actor}
GET  /api/bootstrap               leads, checklist, config, preloaded run
GET  /api/runs/<run_id>[/export]  the audit artifact
```

---

## Configuration

All optional, in `.env` or the environment (environment wins). See `.env.example`.

| | |
|---|---|
| `ANTHROPIC_API_KEY` | enables LLM Type B extraction |
| `CIMET_LLM` | `auto` (default) / `on` / `off` |
| `CIMET_MODEL` | default `claude-opus-5` |
| `CIMET_CONFIDENCE_FLOOR` | critical confidence floor, default `0.6` |
| `CIMET_SAMPLE_AUDIT_RATE` | default `0.05` |
| `CIMET_PORT` | default `8787` |
| `CIMET_PRELOAD_LEAD` | default `3613790` |

---

## Known limits

Deliberate, given the scope:

- Three fixture leads with local transcripts. There is no ASR step and no call-recording
  integration — ingest attaches a transcript that already exists.
- The CRM is a read-only JSON snapshot. Submitting to a real CRM is out of scope;
  the gate decides *whether* you may submit, and stops there.
- The deterministic Type B extractor covers the four fields in this checklist. A new
  Type B check needs either a key, or a handler in `app/offline_extract.py`.
- Sampling is seeded per lead so demos replay identically. In production it should be
  seeded per run.
- No auth, binds to `127.0.0.1`. Do not expose it.
