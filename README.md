# CIMET QA Gate

Scores a sales call against its retailer's compliance checklist **before** the lead
is submitted to the CRM, and decides one of three things: **HELD**, **QA**, or
**SUBMITTED**. Two checklists ship today: the Retailer 1 energy checklist, and the
NBN-QA pack for internet sales (lead 3613793, the official CIMET artefact).

Local, one command, no Docker, no auth, no database. External services are optional:
the Anthropic API for Type B extraction, and ElevenLabs or Deepgram for turning dialler
recordings into transcripts. It runs end to end without any key.

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
deterministic extractor, the engine status in the top bar says so, and every result
records which extractor produced it. If the LLM is configured but a call fails, the
result records that it fell back and why. Nothing silently pretends to be an LLM.

Other entry points:

```bash
python run.py --selftest          # 93 behavioural assertions, offline, ~1s
python run.py --eval              # accuracy against 12 hand-labelled calls
python run.py --score 3613792     # score one lead in the terminal
python run.py --no-llm            # force the deterministic extractor
```

---

## The console

One page, three columns under a status band.

- **Top bar.** The lead picker is a single button showing the current lead. It opens
  a list in two groups - **Brief leads** (3613790-93) and **Synthetic test calls** -
  with the customer name and a one-line note on what each call tests. **NBN** marks a
  lead scored against the NBN-QA pack; **ASR** marks one scored from a dialler
  recording. Redacted customers show as "Redacted customer". Arrow keys move through
  the list, Escape closes it. To the right: a one-line engine status (speech-to-text
  provider and the Type B extractor, each with a green or amber dot), **Accuracy**
  and **Re-run**. While the dialler is sending a recording, a status badge appears
  there too.
- **Status band.** HELD / QA / SUBMITTED, the queue it was routed to, each reason with
  a clickable timestamp, the counters, the pipeline trace, and **Download audit JSON**.
- **Checklist** (left). Every check with its status, confidence, timestamp and whether
  it blocks the sale. Click a row for the evidence: the quote, the matched phrases,
  the spoken value against the reference, every other mention of the value (including
  figures the agent read off the customer's screen), the guardrails that fired, and
  **Override**.
- **Transcript** (centre). With a recording, a player sits on top; clicking any
  timestamp plays from there, and the line being spoken is highlighted.
- **CRM record, plan sold, override log** (right). Read-only. The panel scrolls as one
  column, so a long plan never squeezes the override log.

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

**Click 3 — pick 3613792 from the lead picker** (the messy-audio call).
Crosstalk, `[inaudible]`, a dropped line. The status is **QA**, not HELD: the DMO
statement, the rate and the email are all **unsure**, each with the guardrail that
downgraded them. Nothing was failed because it could not be heard. The checks that
*were* audible still passed.

Lead **3613791** is the clean control: every critical check passes and it goes
**SUBMITTED**. Lead **3613793** is the official NBN call: **QA**. Open *Total minimum
cost* to see the agent's $42.90 next to the $317 the agent read off the customer's
screen three times.

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
| **G7** possible mishear | An email mismatch that audio cannot settle becomes **unsure**: the two spellings sound the same (rahman / raman), or the CRM holds a real provider and the transcript a non-existent one (bigpond / "bizpoint"). A CRM domain that is a near-miss of a real provider (gmial.com) is still a **fail** - that is a keying error, not a mishear. Both G7 cases were found by running real ElevenLabs Scribe output through the gate. |
| **G8** screen vs spoken conflict | The agent's words disagree with the order screen in a way the call cannot settle (a guarantee a fee will not be charged while the order still includes it). FAIL at 0.7x confidence; the critical floor decides FAIL or UNSURE. |

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
--selftest` asserts exactly that, along with 90 other claims made on this page.

**Card numbers spoken as words.** ASR often writes a card as "four one one one ...".
Redaction counts digit words (including "double" and "triple") the same way it counts
digits, and ASR output is masked before the transcript is written to disk.

---

## Lead 3613793 - the official NBN artefact, and checklist packs

`data/transcripts/3613793.json` is the redacted CIMET internet sales call
(`call-transcript-redacted.pdf`), copied row for row by `tools/build_3613793.py`:
Speaker 2 is the agent, Speaker 1 the customer, and every placeholder tag
(`[EMAIL]`, `[PROVIDER_A]`, ...) is kept verbatim - the self-test asserts that
ingest changes none of them. The source has no audio or timings, so `start_sec` is
estimated from turn order (`timing_estimated: true` on the transcript), and the
source's ASR confidence is unknown (0.9 assumed, recorded on the file).

**Packs.** `data/checks.json` keeps the Retailer 1 energy checklist at the top level
and adds `extra_packs`. A lead is scored against the pack it names in `check_pack`;
leads that name none (3613790-92 and the synthetic calls) are scored exactly as
before - a before/after diff of every verdict on those leads shows zero changes.

**NBN-QA** (12 checks): A - `recording_disclaimer_at_open` (must come within the first
three agent turns), `identity_verified`, `card_on_call` (pass if the recording is
muted and no card number is on the call); B - `promo_price`, `ongoing_price`,
`speed_peak`, `modem_model_and_cost`, `tmc_disclosed`, `development_fee_vs_screen`,
`contract_term`; C - `talk_over` (turns cut off mid-sentence, since there are no real
timings), `customer_confusion`.

**Result: QA.** Disclaimer, identity, card handling, prices, speed and modem pass.
Three criticals route to a human:

| | |
|---|---|
| `tmc_disclosed` | At 06:07 the agent says "the total minimum cost will be forty two dollars and ninety only". Later the agent reads $317 off the customer's screen three times and says to disregard it. $317 - $275 (the development fee the lead marks not applicable) = $42 - the agent's figure is the order total without the fee. **G8** scores it FAIL at 0.7x confidence; at 0.51 it is below the 0.60 floor, so **G5** routes it to QA. |
| `development_fee_vs_screen` | Fee stated correctly ($275) and the lead agrees it does not apply, but the agent "guarantees" it will not be charged while the submitted order still totals $317 including it (and once calls it "two hundred seven dollars"). Same G8 -> G5 path. |
| `contract_term` | The agent says both "month to month" and "one to one contract", and the lead carries no contract term - none is invented, so it is unsure. Add `plan.contract_term_months` to the lead to make it comparable. |

Two generic scoring changes came out of this call: script phrases may list
alternative wordings, and a phrase run together by ASR ("quality assuranceand,
training") still matches - audio markers and turn boundaries block that, so words
either side of an `[inaudible]` can never be glued into a match.

**G8 screen vs spoken conflict.** When the agent's words and the order screen disagree
and the call alone cannot show which the provider will bill, the verdict is FAIL at
70% of normal confidence and the critical floor decides FAIL or UNSURE. With the
deterministic extractor that lands at 0.51 (QA); a more confident extractor could
reach the floor and hold the sale instead - which is the intended behaviour.

---

## Recording ingestion - the dialler webhook

The brief's "build this first": the dialler pushes the recording, keyed on Lead ID,
and nobody touches it after that.

```
dialler --POST /api/dialler/recording--> 202 {job_id}         (answers at once)
            |
            +-- recording saved to runs/audio/<lead>.<ext>    (kept even if ASR fails)
            +-- ASR: speaker separation + word timestamps     (ElevenLabs Scribe or Deepgram)
            +-- agent/customer decided by what each speaker says, recorded with its margin
            +-- card numbers masked, transcript stored against the lead
            +-- scored and gated like any other transcript    -> HELD / QA / SUBMITTED
```

Three ways to send it:

```bash
# raw audio body - what a dialler integration would normally do
curl -X POST "http://127.0.0.1:8787/api/dialler/recording?lead_id=3613802&filename=call.wav" \
     -H "Content-Type: audio/wav" --data-binary @call.wav

# a URL the server fetches
curl -X POST http://127.0.0.1:8787/api/dialler/recording -H "Content-Type: application/json" \
     -d '{"lead_id": "3613802", "recording_url": "https://dialler.example/rec/123.wav"}'

# base64 in JSON: {"lead_id", "audio_base64", "filename"}
```

Poll `GET /api/jobs/<job_id>` (`received -> transcribing -> scoring -> done | error`).
Every transition is appended to `runs/dialler_log.jsonl`. Set `CIMET_DIALLER_TOKEN` and
the dialler must send it as `X-Dialler-Token`. With no ASR key the webhook answers 503:
it never falls back to a fixture and calls that a transcript.

In the console, any timestamp plays the recording from that moment: gate reasons and
the evidence panel play a 20-second clip, and transcript timestamps play on from there.
The transcript follows the audio as it plays.

---

## Test calls and scoring accuracy

`data/synth/` holds 12 synthetic Retailer 1 calls, each with one or two planted defects:
wrong rate, email keyed wrong, disclaimer skipped, DMO paraphrased, authority never
confirmed, card read aloud, dead air and talk-over, NMI mismatch, crosstalk over the
DMO, rate in words, and two criticals on one call. **The labels in
`data/synth/truth.json` were written by hand and the scorer never reads them.**

```bash
python tools/synth_calls.py                 # regenerate scripts, reference transcripts, labels
python run.py --eval                        # agreement on reference transcripts, offline
python tools/make_audio.py --dry-run        # TTS character count (~25k for all 12)
python tools/make_audio.py                  # ElevenLabs -> data/synth/audio/<lead>.wav
python tools/make_audio.py --engine windows # free: Windows built-in voices, no quota
python run.py                               # start the app, then in another terminal:
python tools/dialler_sim.py --all --eval    # push every call through the webhook, score the ASR output
```

The recordings are made to sound like a phone call, not a studio: 300-3400 Hz band,
8 kHz mono, line noise and hum, real overlap between speakers, and real silence where
the script has dead air. The crosstalk call gets far more noise. Clean TTS would make
ASR, and so the accuracy numbers, look better than a real call deserves.

`--eval` reports, per check and overall: agreement where the machine decided,
Cohen's kappa against the labels, the share routed to a human, **critical false
passes**, and at the gate **false submits** and false holds. Unsure is counted as
neither agreement nor error: it is the system declining to guess. On the reference
transcripts, all 12 calls agree with their labels except the crosstalk call, which goes
to QA instead of being passed or failed. Reference transcripts are clean by
construction, so treat that as a floor check. The number that matters is the
`--eval asr` run on real recordings. The **Accuracy** button in the console shows both.

ElevenLabs bills speech-to-text against the same character quota as TTS (about 200
credits for a 3-minute call on the free tier), and `make_audio.py` checks the remaining
quota and refuses up front rather than stopping half way. `--engine windows` renders
every call for free; the robotic voices are enough to exercise ASR, diarisation and
the scorer.

To test against your own voice, record a call on a phone and push it for any lead:
`python tools/dialler_sim.py 3613790 --file my-call.m4a`.

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
decision from it without access to this process. For a run scored from a recording,
the transcript block also carries the recording's SHA-256, the ASR engine and model,
and how the agent was identified.

---

## Layout

```
data/leads.json               4 brief leads with read-only CRM snapshots (3613793 carries its plan inline)
data/plans.json               energy plan catalogue - the reference for rate comparison
data/checks.json              Retailer 1 checklist (default) + extra_packs: NBN-QA; versioned per check
data/transcripts/<id>.json    diarised turns: speaker, text, start_sec, end_sec, asr_confidence
data/synth/                   12 synthetic calls: scripts, reference transcripts, leads, truth.json
app/asr.py                    ElevenLabs / Deepgram speech-to-text + agent/customer assignment
app/evaluate.py               agreement, kappa, false passes against the hand labels
app/textutil.py               normalisation, LCS matching, digit redaction
app/llm.py                    Claude extraction + the leak guard
app/offline_extract.py        deterministic extractor used without a key (energy fields)
app/offline_nbn.py            deterministic extractor for the NBN-QA fields
app/compare.py                the comparators - the only code that sees both sides
app/scoring.py                Type A / B / C scorers
app/gate.py                   the gate and the sample-audit draw
app/pipeline.py               ingest -> score -> gate, and overrides
app/server.py, app/web/       stdlib HTTP server and the single-page console
runs/                         audit artifacts, override log, routing queue, dialler log,
                              recordings (runs/audio) and their ASR transcripts (runs/transcripts)
tools/synth_calls.py          writes data/synth/ from hand-written scenarios
tools/make_audio.py           ElevenLabs TTS -> phone-quality WAV per synthetic call
tools/dialler_sim.py          pushes recordings to the webhook the way a dialler would
tools/build_3613793.py        builds 3613793's transcript from the redacted PDF, row for row
selftest.py                   93 behavioural assertions
```

### API

```
POST /api/ingest   {lead_id}      attach the local transcript (no file picker)
POST /api/score    {ingest_id}    score + gate, returns the full run
POST /api/run      {lead_id}      both of the above
POST /api/override {run_id, check_id, to_status, reason, actor}
GET  /api/bootstrap               leads, checklist, config, preloaded run
GET  /api/runs/<run_id>[/export]  the audit artifact
POST /api/dialler/recording       recording in, keyed on lead_id -> 202 {job_id}
GET  /api/jobs[/<job_id>]         dialler job status
GET  /api/audio/<lead_id>         the recording, with Range support for seeking
GET  /api/leads/<lead_id>/latest  newest run for a lead
GET  /api/eval?source=fixture|asr last accuracy report; POST /api/eval {source} runs one
```

---

## Configuration

All optional, in `.env` or the environment (environment wins). See `.env.example`.

| | |
|---|---|
| `ANTHROPIC_API_KEY` | enables LLM Type B extraction |
| `CIMET_LLM` | `auto` (default) / `on` / `off` |
| `CIMET_MODEL` | default `claude-opus-5`. The extractor sends an `effort` setting; a model that rejects it makes every Type B check fall back to the deterministic extractor (recorded on each result) |
| `CIMET_CONFIDENCE_FLOOR` | critical confidence floor, default `0.6` |
| `CIMET_SAMPLE_AUDIT_RATE` | default `0.05` |
| `CIMET_PORT` | default `8787` |
| `CIMET_PRELOAD_LEAD` | default `3613790` |
| `ELEVENLABS_API_KEY` / `DEEPGRAM_API_KEY` | enables the dialler webhook (ASR) |
| `CIMET_ASR` | `auto` (default) / `elevenlabs` / `deepgram` |
| `CIMET_ASR_MODEL` | default `scribe_v1` or `nova-3` |
| `CIMET_DIALLER_TOKEN` | if set, required as `X-Dialler-Token` |

---

## Known limits

Deliberate, given the scope:

- Mono recordings are separated into speakers by diarisation. A stereo dialler
  recording (agent and customer on separate channels) would give certain speaker
  labels; it is not split by channel yet.
- The dialler job queue lives in memory. Recordings and transcripts are on disk, but
  a job in progress when the process stops has to be pushed again.
- The synthetic calls are short (3-5 minutes) and their scripts follow the checklist
  wording, so they test plumbing and guardrails more than wording drift. The real
  recording handed out on the day is the better test of wording.
- The CRM is a read-only JSON snapshot. Submitting to a real CRM is out of scope;
  the gate decides *whether* you may submit, and stops there.
- The deterministic Type B extractor covers the fields in the two shipped packs. A new
  Type B check needs either a key, or a handler in `app/offline_extract.py` /
  `app/offline_nbn.py`.
- 3613793 comes from a PDF transcript with no audio, so its timestamps are estimated
  from turn order, and there is nothing to play.
- Sampling is seeded per lead so demos replay identically. In production it should be
  seeded per run.
- No auth, binds to `127.0.0.1`. Do not expose it.
