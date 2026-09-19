/* CIMET QA Gate - single page console.
   The UI never writes to the CRM. The only mutating call it can make is
   POST /api/override, which requires a written reason.

   Layout is review-first: the verdict in plain words, then the checks that need a
   human as cards (said vs expected, play, override), passes and coaching notes
   folded away, and the call as a timeline you can click. */

const $ = (id) => document.getElementById(id);

const state = {
  boot: null,
  run: null,
  leadId: null,
  selected: null,
  busy: false,
  pending: null,     // check awaiting an override decision
  stopAt: null,      // seconds - a clip played from evidence stops here
  jobs: new Map(),   // dialler job_id -> last seen status
  jobsPrimed: false,
  evalSource: "fixture",
  tab: "transcript",
  open: { passed: false, notes: false },
};

const CLIP_SEC = 20; // "clicks the timestamp, hears twenty seconds"

const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const mmss = (sec) => {
  if (sec === null || sec === undefined) return "--:--";
  const s = Math.round(Number(sec));
  return `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
};

const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
const hasTime = (r) => r.start_sec !== null && r.start_sec !== undefined && r.start_sec >= 0;

const decorate = (text) => esc(text)
  .replace(/\[(inaudible|crosstalk|silence|unintelligible|indistinct)\]/gi, "<mark>[$1]</mark>")
  .replace(/\[REDACTED ([^\]]+)\]/g, '<span class="redacted">[REDACTED $1]</span>');

function toast(message, bad = false, onClick = null) {
  const el = $("toast");
  el.textContent = message;
  el.className = "toast" + (bad ? " bad" : "") + (onClick ? " link" : "");
  el.onclick = onClick ? () => { el.hidden = true; onClick(); } : null;
  el.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { el.hidden = true; }, bad || onClick ? 8000 : 3200);
}

async function api(path, body) {
  const options = body
    ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }
    : {};
  const response = await fetch(path, options);
  const data = await response.json().catch(() => ({ error: `${response.status} ${response.statusText}` }));
  if (!response.ok) throw new Error(data.error || `request failed (${response.status})`);
  return data;
}

/* ------------------------------------------------------------------ top bar */

const leadName = (lead) => (/^\[/.test(lead.account_holder) ? "Redacted customer" : lead.account_holder);

/* The dot is the lead's verdict from its latest scoring, not its audio quality. */
const GATE_LABEL = { HELD: "held", QA: "qa", SUBMITTED: "submitted" };
function gateDot(lead) {
  const g = lead.last_gate;
  const title = g ? `Last scored: ${g}` : "Not scored yet";
  return `<span class="dot gate-${GATE_LABEL[g] || "none"}" title="${esc(title)}"></span>`;
}

function leadTags(lead) {
  return [
    lead.audio_quality === "degraded" ? '<span class="tagx noisy" title="Poor audio / crosstalk">NOISY</span>' : "",
    lead.vertical === "nbn" ? '<span class="tagx nbn" title="NBN-QA checklist">NBN</span>' : "",
    lead.has_asr_transcript ? '<span class="tagx asr" title="scored from a dialler recording">ASR</span>' : "",
  ].join("");
}

function renderLeads() {
  const current = state.boot.leads.find((l) => l.lead_id === state.leadId) || state.boot.leads[0];
  $("leadBtn").innerHTML = `
    ${gateDot(current)}
    <span class="lid">${esc(current.lead_id)}</span>
    <span class="lname">${esc(leadName(current))}</span>
    ${leadTags(current)}
    <span class="chev" aria-hidden="true">&#9662;</span>`;

  const item = (lead) => `
    <button type="button" class="leaditem" role="option" data-lead="${esc(lead.lead_id)}"
            aria-selected="${lead.lead_id === state.leadId}">
      ${gateDot(lead)}
      <span class="lid">${esc(lead.lead_id)}</span>
      <span class="lname">${esc(leadName(lead))}</span>
      <span class="tags">${leadTags(lead)}</span>
      <span class="lnote">${esc((lead.demo_note || "").replace(/^SYNTHETIC - /, ""))}</span>
    </button>`;
  const brief = state.boot.leads.filter((l) => !l.synthetic);
  const synth = state.boot.leads.filter((l) => l.synthetic);
  $("leadMenu").innerHTML =
    `<div class="leadgroup">Brief leads</div>${brief.map(item).join("")}` +
    (synth.length ? `<div class="leadgroup">Synthetic test calls</div>${synth.map(item).join("")}` : "");
  $("leadMenu").querySelectorAll(".leaditem").forEach((el) => {
    el.onclick = () => { closeLeadMenu(); loadLead(el.dataset.lead); };
  });
}

function openLeadMenu() {
  $("leadMenu").hidden = false;
  $("leadBtn").setAttribute("aria-expanded", "true");
  const sel = $("leadMenu").querySelector('[aria-selected="true"]') || $("leadMenu").querySelector(".leaditem");
  if (sel) { sel.scrollIntoView({ block: "nearest" }); sel.focus(); }
}

function closeLeadMenu(refocus = false) {
  if ($("leadMenu").hidden) return;
  $("leadMenu").hidden = true;
  $("leadBtn").setAttribute("aria-expanded", "false");
  if (refocus) $("leadBtn").focus();
}

function renderEngine() {
  const engines = state.boot.extractors;
  const badge = $("extractorBadge");
  badge.innerHTML = engines.llm_available
    ? `Type B <b>${esc(engines.model)}</b>`
    : "Type B <b>deterministic</b>";
  badge.className = "eng " + (engines.llm_available ? "live" : "offline");
  badge.title = engines.llm_available
    ? "Type B values are extracted by Claude, then compared to the CRM by deterministic code."
    : `LLM extraction is off - ${engines.llm_unavailable_reason}. Type B uses the built-in deterministic extractor instead.`;

  const asr = state.boot.asr || {};
  const asrBadge = $("asrBadge");
  asrBadge.innerHTML = `ASR <b>${esc(asr.provider || "off")}</b>`;
  asrBadge.className = "eng " + (asr.provider ? "live" : "offline");
  asrBadge.title = asr.provider
    ? "Dialler recordings posted to /api/dialler/recording are transcribed with speaker separation and timestamps."
    : `Dialler webhook is disabled - ${asr.unavailable_reason}. Reference transcripts still score.`;
}

/* ------------------------------------------------------------------ verdict */

const queueLabel = (q) => (q ? q.replace(/_/g, " ").toLowerCase().replace(/\bqa\b/, "QA") : "");

function renderVerdict() {
  const run = state.run;
  const gate = run.gate;
  const scored = run.results.filter((r) => r.type !== "C");
  const critical = scored.filter((r) => r.critical);
  const failed = critical.filter((r) => r.status === "fail");
  const unsure = critical.filter((r) => r.status === "unsure");
  const names = (list) => list.map((r) => r.name).join(" · ");

  const verdict = $("verdict");
  verdict.className = `verdict ${gate.status.toLowerCase()}`;
  const chip = $("statusChip");
  chip.className = "statuschip flash";
  setTimeout(() => chip.classList.remove("flash"), 550);
  $("statusText").textContent = gate.status;

  let title, sub;
  if (gate.status === "HELD") {
    title = `${plural(failed.length, "critical check")} failed`;
    sub = `${names(failed)}. Held and routed to ${queueLabel(gate.queue?.queue) || "a team leader"}.`;
  } else if (gate.status === "QA") {
    title = `${plural(unsure.length, "critical check")} need${unsure.length === 1 ? "s" : ""} a human`;
    sub = `${names(unsure)}. Routed to ${queueLabel(gate.queue?.queue) || "QA review"} - nothing uncertain auto-passes.`;
  } else {
    title = "All critical checks passed";
    sub = "Cleared to submit to the CRM.";
  }
  $("verdictTitle").textContent = title;
  const scoredAt = new Date(run.completed_at);
  const when = isNaN(scoredAt) ? "" : scoredAt.toLocaleString([], { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
  $("chipSub").innerHTML = esc(sub)
    + (gate.sample_audit ? '<span class="sample">Sampled for human audit</span>' : "")
    + (when ? `<span class="scored" title="Opening a lead shows its saved result. Re-run scores it again.">Scored ${esc(when)} · ${esc(run.extractors?.type_b_active || "")}</span>` : "");

  const passed = scored.filter((r) => r.status === "pass").length;
  const fails = scored.filter((r) => r.status === "fail").length;
  const unsures = scored.filter((r) => r.status === "unsure").length;
  $("gateCounters").innerHTML = [
    ["Passed", passed, "good", ""],
    ["Failed", fails, fails ? "bad" : "", ""],
    ["Unsure", unsures, unsures ? "warn" : "", ""],
    ["Score", `${gate.counters.weighted_score}%`, "", "opt"],
  ].map(([label, value, tone, opt]) =>
    `<div class="stat ${tone} ${opt}"><b>${esc(value)}</b><span>${esc(label)}</span></div>`).join("");
}

/* ------------------------------------------------------------------- checks */

function fmtValue(v, key = "") {
  if (v === null || v === undefined) return "not supplied";
  if (typeof v === "boolean") return v ? "yes" : "no";
  // Energy rates are cents per unit (31.9c/kWh); every other *_cents field is money.
  if (typeof v === "number" && /(rate|supply|fit)_cents$/.test(key)) return `${v}c`;
  if (typeof v === "number" && /_cents$/.test(key)) return `$${(v / 100).toFixed(2)}`;
  if (typeof v === "object") {
    return Object.entries(v).map(([k, x]) => `${k.replace(/^plan\./, "")} = ${fmtValue(x, k)}`).join("; ");
  }
  return String(v);
}

function kv(rows) {
  return `<dl class="kv">${rows.filter(Boolean)
    .map(([k, v]) => `<dt>${esc(k)}</dt><dd>${v}</dd>`).join("")}</dl>`;
}

/* The one-glance summary on a card: what was said against what it had to be. */
function cardSummary(r) {
  const e = r.evidence || {};
  if (r.type === "B") {
    const x = e.extraction || {};
    const c = e.comparison;
    const said = x.found ? (c?.observed_normalised || x.value) : "not established";
    const expected = c?.expected_normalised ?? fmtValue(e.reference?.value, e.reference?.path || "");
    const op = c?.match === true ? "=" : c?.match === false ? "≠" : "?";
    return `<div class="diff">
      <div class="side said"><div class="lbl">Said</div><div class="val">${esc(said)}</div></div>
      <div class="op">${op}</div>
      <div class="side"><div class="lbl">Expected</div><div class="val">${esc(expected)}</div></div>
    </div>`;
  }
  if (r.type === "A" && e.phrases) {
    const missing = e.phrases.filter((p) => !p.found).map((p) => `"${p.phrase}"`);
    return missing.length
      ? `<p class="why"><b>Missing:</b> ${esc(missing.join(", "))} <span class="conf">(${Math.round(e.coverage * 100)}% of the wording)</span></p>`
      : "";
  }
  return "";
}

/* Why the machine decided this, in one sentence. */
function cardWhy(r) {
  const guard = (r.guardrails || [])[0];
  if (guard) {
    const text = guard.replace(/^G\d+\s+[^:]+:\s*/, "");
    return text.split(/(?<=\.)\s/)[0];
  }
  if (r.note) return r.note;
  return r.evidence?.comparison?.detail || "";
}

function renderCard(r) {
  const tone = r.status === "fail" ? "fail" : "unsure";
  const selected = state.selected === r.check_id;
  const why = cardWhy(r);
  return `
    <article class="card ${tone} ${selected ? "selected" : ""}" data-check="${esc(r.check_id)}">
      <div class="cardtop">
        <span class="status-ico" aria-hidden="true">${tone === "fail" ? "✕" : "?"}</span>
        <div class="cardtitle">
          <div class="n">${esc(r.name)}</div>
          <div class="sub">${r.status === "fail" ? "Failed" : "Unsure - needs a human"} · ${esc(r.section.replace(/^\d+\.\s*/, ""))}</div>
        </div>
        <button class="tchip" data-jump="${hasTime(r) ? r.start_sec : ""}" data-check="${esc(r.check_id)}"
                ${hasTime(r) ? "" : "disabled"} title="Jump to this moment">${esc(r.timestamp)}</button>
      </div>
      ${cardSummary(r)}
      ${why && r.type !== "A" ? `<p class="why">${esc(why)}</p>` : ""}
      <div class="cardfoot">
        ${r.critical ? '<span class="chip crit">CRITICAL</span>' : '<span class="chip">non-blocking</span>'}
        <span class="chip">TYPE ${esc(r.type)}</span>
        ${r.overridden ? '<span class="chip ov">OVERRIDDEN</span>' : ""}
        <span class="conf" title="machine confidence">conf ${r.confidence.toFixed(2)}</span>
        <span class="spacer"></span>
        ${state.run.audio_url && hasTime(r) ? `<button class="btn small" data-play="${r.start_sec}">&#9654; Play ${CLIP_SEC}s</button>` : ""}
        <button class="btn small" data-override="${esc(r.check_id)}">Override</button>
      </div>
      ${selected ? renderEvidence(r) : ""}
    </article>`;
}

function renderRow(r) {
  const selected = state.selected === r.check_id;
  const note = r.type === "C";
  return `
    <div class="row ${note ? "note" : ""} ${selected ? "selected" : ""}" data-check="${esc(r.check_id)}">
      <span class="status-ico" aria-hidden="true">${note ? "•" : "✓"}</span>
      <span class="rn">${esc(r.name)}${note && r.note ? `<small>${esc(r.note)}</small>` : ""}
        ${r.overridden ? '<span class="chip ov">OVERRIDDEN</span>' : ""}</span>
      <span class="rt">${esc(r.timestamp)}</span>
    </div>
    ${selected ? `<div class="rowevidence">${renderEvidence(r)}</div>` : ""}`;
}

function renderChecks() {
  const results = state.run.results;
  const attention = results
    .filter((r) => r.type !== "C" && r.status !== "pass")
    .sort((a, b) => (b.critical - a.critical) || ((a.status === "fail" ? 0 : 1) - (b.status === "fail" ? 0 : 1))
      || ((a.start_sec ?? 1e9) - (b.start_sec ?? 1e9)));
  const passed = results.filter((r) => r.type !== "C" && r.status === "pass");
  const notes = results.filter((r) => r.type === "C");

  // Keep a group open if the selected check lives in it.
  if (passed.some((r) => r.check_id === state.selected)) state.open.passed = true;
  if (notes.some((r) => r.check_id === state.selected)) state.open.notes = true;

  $("checkList").innerHTML = `
    <section class="group">
      <div class="grouphead">Needs attention <span class="n">${attention.length}</span></div>
      ${attention.length ? attention.map(renderCard).join("")
        : '<div class="allclear">✓ Nothing needs attention - every check that can block this sale passed.</div>'}
    </section>
    <details class="group" data-group="passed" ${state.open.passed ? "open" : ""}>
      <summary class="grouphead">Passed <span class="n">${passed.length}</span></summary>
      ${passed.map(renderRow).join("")}
    </details>
    <details class="group" data-group="notes" ${state.open.notes ? "open" : ""}>
      <summary class="grouphead">Coaching notes <span class="n">${notes.length}</span></summary>
      ${notes.map(renderRow).join("")}
    </details>`;

  const list = $("checkList");
  list.querySelectorAll("details[data-group]").forEach((d) => {
    d.addEventListener("toggle", () => { state.open[d.dataset.group] = d.open; });
  });
  list.querySelectorAll(".card, .row").forEach((el) => {
    el.onclick = (ev) => {
      if (ev.target.closest("button, a, .evidence, .seek, .mention")) return;
      selectCheck(el.dataset.check);
    };
  });
  list.querySelectorAll("[data-override]").forEach((b) => { b.onclick = () => openOverride(b.dataset.override); });
  list.querySelectorAll("[data-play]").forEach((b) => { b.onclick = () => playAt(Number(b.dataset.play), CLIP_SEC); });
  list.querySelectorAll(".tchip[data-jump]").forEach((b) => {
    b.onclick = () => {
      if (b.dataset.jump === "") return;
      selectCheck(b.dataset.check, true);
      playAt(Number(b.dataset.jump), CLIP_SEC);
    };
  });
  list.querySelectorAll("[data-seek-evidence]").forEach((el) => {
    el.onclick = () => jumpTo(Number(el.dataset.seekEvidence), CLIP_SEC);
  });
}

function renderEvidence(r) {
  const e = r.evidence || {};
  let body = "";

  if (r.quote) {
    body += `<blockquote class="quote">
      <span class="qmeta">${esc(r.timestamp)} · ${esc(r.speaker || "unknown")} · turn ${esc(r.turn_idx)}
        ${e.quote_role === "closest_matching_span" ? " · closest matching span, not proof of compliance" : ""}</span>
      ${decorate(r.quote)}</blockquote>`;
  }
  if (r.note && r.type !== "C") body += `<p class="hint" style="margin:-4px 0 10px">${esc(r.note)}</p>`;

  for (const g of r.guardrails || []) {
    const code = g.slice(0, g.indexOf(" ") > 0 ? g.indexOf(" ") : 2);
    body += `<div class="guard"><b>${esc(code)}</b>${esc(g.slice(code.length).trim())}</div>`;
  }

  if (r.override) {
    body += `<div class="ovbox"><b>Overridden by ${esc(r.override.actor)}</b> -
      ${esc(r.override.machine_status)} &rarr; ${esc(r.override.to_status)}<br>
      ${esc(r.override.reason)}<br>
      <span class="hint">machine verdict kept: ${esc(r.override.machine_status)} at
      ${r.override.machine_confidence.toFixed(2)} confidence</span></div>`;
  }

  if (r.type === "A" && e.method === "card_handling") {
    body += kv([
      ["method", "deterministic rule, no LLM"],
      ["card number on call", e.pan_turn_idx.length ? `<span class="bad">yes - turn ${e.pan_turn_idx.join(", ")}</span>` : '<span class="good">none</span>'],
      ["recording muted", e.mute_turn_idx.length ? `<span class="good">turn ${e.mute_turn_idx.join(", ")}</span>` : "not mentioned"],
      ["payment discussed", e.payment_turn_idx.length ? `turn ${e.payment_turn_idx.join(", ")}` : "no"],
      ["rule", esc(e.rule)],
      ["check version", `${esc(r.check_version)} (from ${esc(r.effective_from)})`],
      ["basis", esc(r.regulatory_basis || "-")],
    ]);
  } else if (r.type === "A") {
    body += `<div class="phraselist">${(e.phrases || []).map((p) => `
      <div class="phrase ${p.found ? "ok" : "no"}">
        <span class="mk">${p.found ? "✓" : "✕"}</span>
        <span class="txt">"${esc(p.phrase)}"${p.alternatives ? ` <span class="alt">or ${p.alternatives.filter((a) => a !== p.phrase).map((a) => `"${esc(a)}"`).join(", ")}</span>` : ""}${p.matched_run_together ? ' <span class="alt">(run-together in transcript)</span>' : ""}</span>
        <span class="pct">${Math.round(p.ratio * 100)}%</span>
      </div>`).join("")}</div>`;
    body += kv([
      ["method", "deterministic script-span match, no LLM"],
      ["coverage", `<b>${Math.round(e.coverage * 100)}%</b> (pass at ${Math.round(e.pass_threshold * 100)}%, unsure at ${Math.round(e.unsure_threshold * 100)}%)`],
      (e.audio_markers_in_span || []).length ? ["audio markers", `<span class="bad">${esc(e.audio_markers_in_span.join(", "))}</span>`] : null,
      ["asr confidence", `min ${e.min_asr_confidence} / mean ${e.mean_asr_confidence}`],
      e.agent_turn_ordinal ? ["at the open", `agent turn ${e.agent_turn_ordinal} (must be within the first ${e.max_agent_turn})`] : null,
      ["check version", `${esc(r.check_version)} (from ${esc(r.effective_from)})`],
      ["basis", esc(r.regulatory_basis || "-")],
    ]);
  } else if (r.type === "B") {
    const x = e.extraction || {};
    const c = e.comparison;
    body += kv([
      ["spoken value", x.found
        ? `<b>${esc(x.value)}</b> <span class="hint">(extraction confidence ${x.confidence})</span>`
        : `<span class="bad">not established</span> - ${esc(x.reason || "")}`],
      ["reference", `${esc(e.reference.label)} = <b>${esc(fmtValue(e.reference.value, e.reference.path || ""))}</b>
        <span class="hint">from ${esc(e.reference.source)}</span>`],
      (x.other_mentions || []).length ? ["other mentions", x.other_mentions.map((m) =>
        `<span class="mention"><span class="rt" data-seek-evidence="${m.start_sec}">${esc(mmss(m.start_sec))}</span> ${esc(m.value)} <span class="hint">(${esc(m.kind)})</span></span>`).join("")] : null,
      c ? ["comparison", `${c.match === true ? '<span class="good">match</span>' : c.match === false ? '<span class="bad">mismatch</span>' : "not comparable"} - ${esc(c.detail)}`] : null,
      ["extractor", `${esc(e.extractor)}${e.effort ? ` (effort ${esc(e.effort)})` : ""}${e.degraded_from
        ? ` <span class="bad" title="${esc(e.degrade_reason || "")}">- fell back from ${esc(e.degraded_from)}</span>` : ""}`],
      ["leak guard", `the extractor never saw ${esc(e.reference.label)}`],
      ["grounding", e.grounding
        ? `quote ${e.grounding.quote_grounded_in_transcript ? "found in" : "<span class='bad'>not found in</span>"} the transcript (${Math.round((e.grounding.match_ratio || 0) * 100)}% match)`
        : "-"],
      e.prompt_sha256 ? ["prompt sha256", `<span class="mono">${esc(e.prompt_sha256.slice(0, 24))}…</span>`] : null,
      ["check version", `${esc(r.check_version)} (from ${esc(r.effective_from)})`],
      ["basis", esc(r.regulatory_basis || "-")],
    ]);
  } else {
    body += kv([
      ["metric", esc(e.metric)],
      ["observed", `<b>${esc(e.observed)}</b> against a threshold of ${esc(e.threshold)}`],
      ["occurrences", (e.occurrences || []).length
        ? e.occurrences.map((o) => `<span class="seek" data-seek-evidence="${o.start_sec}">${esc(o.timestamp)}</span>${o.duration_sec ? ` (${o.duration_sec}s)` : ""}`).join(" ")
        : "none"],
      ["blocks sale", '<span class="good">never - coaching only</span>'],
    ]);
  }

  if (r.type === "C") body += '<p class="hint">Coaching notes never affect the gate and cannot be overridden.</p>';
  return `<div class="evidence">${body}</div>`;
}

/* Scroll an element into view inside its own panel. scrollIntoView() is not used:
   two smooth scrolls in different panels at once cancel each other in Chrome. */
function scrollWithin(el, mode = "center") {
  const box = el?.closest(".colscroll");
  if (!box) return;
  const r = el.getBoundingClientRect();
  const b = box.getBoundingClientRect();
  if (mode === "nearest" && r.top >= b.top && r.bottom <= b.bottom) return;
  const offset = mode === "top" ? 8 : mode === "nearest" && r.top < b.top ? 8
    : mode === "nearest" ? box.clientHeight - r.height - 8 : (box.clientHeight - r.height) / 2;
  // Hidden tabs never run smooth-scroll animations, so jump instead of waiting forever.
  box.scrollTo({ top: box.scrollTop + (r.top - b.top) - offset,
                 behavior: document.visibilityState === "visible" ? "smooth" : "auto" });
}

function selectCheck(checkId, keepOpen = false) {
  state.selected = (state.selected === checkId && !keepOpen) ? null : checkId;
  renderChecks();
  renderTimelineMarks();
  if (state.selected) scrollWithin($("checkList").querySelector(`[data-check="${state.selected}"]`), "top");
  highlightTurn();
}

function highlightTurn() {
  document.querySelectorAll(".turn.hit").forEach((el) => el.classList.remove("hit"));
  if (!state.selected) return;
  const result = state.run.results.find((r) => r.check_id === state.selected);
  if (!result || result.turn_idx === null || result.turn_idx === undefined) return;
  setTab("transcript");
  const el = document.querySelector(`.turn[data-idx="${result.turn_idx}"]`);
  if (!el) return;
  el.classList.add("hit");
  scrollWithin(el, "center");
}

/* Jump the transcript to a moment, and play it when there is a recording. */
function jumpTo(sec, clip = null) {
  let turn = null;
  for (const t of state.run.turns) { if (t.start_sec <= sec + 0.05) turn = t; else break; }
  turn = turn || state.run.turns[0];
  setTab("transcript");
  document.querySelectorAll(".turn.hit").forEach((el) => el.classList.remove("hit"));
  const node = turn && document.querySelector(`.turn[data-idx="${turn.idx}"]`);
  if (node) { node.classList.add("hit"); scrollWithin(node, "center"); }
  playAt(sec, clip);
}

/* ----------------------------------------------------------------- timeline */

function callDuration() {
  const run = state.run;
  const last = run.turns[run.turns.length - 1];
  return Math.max(1, run.transcript.duration_sec || 0, last ? last.end_sec : 0);
}

const pct = (sec) => `${Math.min(100, Math.max(0, (sec / callDuration()) * 100)).toFixed(3)}%`;

function renderTimeline() {
  const run = state.run;
  const t = run.transcript;
  const dur = callDuration();
  const fromAudio = t.source === "dialler_recording_asr";
  $("transcriptMeta").textContent = [
    mmss(dur),
    plural(t.turn_count, "turn"),
    fromAudio ? "from dialler recording" : run.audio_url ? "synthetic recording" : "no recording",
    t.asr_engine,
  ].filter(Boolean).join(" · ");
  $("transcriptMeta").title = fromAudio && t.speaker_mapping
    ? `agent = ${t.speaker_mapping.agent_speaker} by ${t.speaker_mapping.method} (margin ${t.speaker_mapping.margin})`
    : `source: ${t.source} (${t.transcript_path})`;

  const seg = (turn) => `<div class="seg" style="left:${pct(turn.start_sec)};width:${pct(Math.max(0.5, turn.end_sec - turn.start_sec))}"></div>`;
  $("laneAgent").innerHTML = run.turns.filter((x) => x.speaker === "agent").map(seg).join("");
  $("laneCustomer").innerHTML = run.turns.filter((x) => x.speaker !== "agent").map(seg).join("");

  const ticks = 5;
  $("tlAxis").innerHTML = Array.from({ length: ticks }, (_, i) => {
    const sec = (dur * i) / (ticks - 1);
    return `<span style="left:${pct(sec)}">${mmss(sec)}</span>`;
  }).join("");
  renderTimelineMarks();
}

function renderTimelineMarks() {
  const run = state.run;
  const deadAir = run.results.find((r) => r.evidence?.metric === "max_silence_gap_sec");
  const gaps = (deadAir?.evidence?.occurrences || []).map((o) =>
    `<div class="gapzone" style="left:${pct(o.start_sec)};width:${pct(o.duration_sec || 0)}" title="${esc(o.duration_sec)}s of dead air at ${esc(o.timestamp)}"></div>`);
  // Passes first so problems draw on top of them.
  const order = { pass: 0, unsure: 1, fail: 2 };
  const pins = run.results
    .filter((r) => r.type !== "C" && hasTime(r))
    .sort((a, b) => order[a.status] - order[b.status])
    .map((r) => `<button class="pin ${esc(r.status)} ${state.selected === r.check_id ? "selected" : ""}"
        style="left:${pct(r.start_sec)}" data-check="${esc(r.check_id)}" data-sec="${r.start_sec}"
        title="${esc(r.outcome_label)} · ${esc(r.name)} · ${esc(r.timestamp)}"
        aria-label="${esc(r.name)} ${esc(r.outcome_label)} at ${esc(r.timestamp)}"></button>`);
  $("tlMarks").innerHTML = gaps.join("") + pins.join("");
  $("tlMarks").querySelectorAll(".pin").forEach((p) => {
    p.onclick = (ev) => {
      ev.stopPropagation();
      selectCheck(p.dataset.check, true);
      playAt(Number(p.dataset.sec), CLIP_SEC);
    };
  });
}

$("tlTrack").addEventListener("click", (ev) => {
  if (!state.run || ev.target.closest(".pin")) return;
  const box = $("tlTrack").getBoundingClientRect();
  const sec = ((ev.clientX - box.left) / box.width) * callDuration();
  jumpTo(sec, null);
});

/* --------------------------------------------------------------- transcript */

function renderTranscript() {
  const run = state.run;
  const t = run.transcript;
  $("redactBar").className = "redactbar" + (t.redactions_applied ? " has" : "");
  $("redactBar").textContent = t.redactions_applied
    ? `${plural(t.redactions_applied, "card-length number")} masked before anything was stored or scored.`
    : "";

  const gaps = new Map();
  const deadAir = run.results.find((r) => r.evidence?.metric === "max_silence_gap_sec");
  for (const o of (deadAir?.evidence?.occurrences || [])) gaps.set(o.after_turn_idx, o);

  $("turnList").innerHTML = run.turns.map((turn) => {
    const gap = gaps.get(turn.idx);
    return `
      <div class="turn ${esc(turn.speaker)} ${turn.asr_confidence < 0.7 ? "lowasr" : ""}" data-idx="${turn.idx}">
        <span class="ts ${run.audio_url ? "play" : ""}" ${run.audio_url ? `data-seek="${turn.start_sec}" title="play from here"` : ""}>${mmss(turn.start_sec)}</span>
        <span>
          <span class="who">${esc(turn.speaker_name || turn.speaker)}${turn.asr_confidence < 0.7
            ? ` · asr ${turn.asr_confidence.toFixed(2)}` : ""}</span>
          <span class="tx">${decorate(turn.text)}</span>
        </span>
      </div>` + (gap
        ? `<div class="gapmark">${gap.duration_sec}s of dead air at ${esc(gap.timestamp)} · coaching note only</div>`
        : "");
  }).join("");
  $("turnList").querySelectorAll("[data-seek]").forEach((el) => {
    el.onclick = () => playAt(Number(el.dataset.seek), null);
  });
}

/* -------------------------------------------------------------------- tabs */

function setTab(name) {
  state.tab = name;
  document.querySelectorAll(".tab").forEach((t) => t.setAttribute("aria-selected", String(t.dataset.tab === name)));
  document.querySelectorAll(".tabpanel").forEach((p) => { p.hidden = p.id !== `tab-${name}`; });
}

document.querySelectorAll(".tab").forEach((t) => { t.onclick = () => setTab(t.dataset.tab); });

/* ------------------------------------------------------------------- audio */

function setupPlayer() {
  const run = state.run;
  const player = $("player");
  const src = run.audio_url ? `${run.audio_url}?v=${encodeURIComponent(run.run_id)}` : "";
  $("playerBar").hidden = !src;
  $("playhead").hidden = !src;
  if (!src) {
    player.pause();
    player.removeAttribute("src");
    player.dataset.src = "";
    return;
  }
  if (player.dataset.src !== src) {
    player.dataset.src = src;
    player.src = src;
  }
  const a = run.transcript.audio;
  $("playerMeta").textContent = a ? `${a.filename} · sha256 ${a.sha256.slice(0, 10)}…` : "synthetic recording";
  $("playhead").style.left = "0%";
}

function playAt(sec, clip) {
  const player = $("player");
  if (!state.run?.audio_url || Number.isNaN(sec)) return;
  const start = Math.max(0, sec - 1);
  state.stopAt = clip ? start + clip : null;
  const go = () => { player.currentTime = start; player.play().catch(() => {}); };
  if (player.readyState >= 1) { go(); return; }
  player.addEventListener("loadedmetadata", go, { once: true });
  // Clicked before the recording arrived, or the first fetch was abandoned: ask again.
  if (player.networkState !== HTMLMediaElement.NETWORK_LOADING) player.load();
}

function followPlayback() {
  const player = $("player");
  const t = player.currentTime;
  if (state.stopAt !== null && t >= state.stopAt) {
    player.pause();
    state.stopAt = null;
  }
  if (!state.run) return;
  $("playhead").style.left = pct(t);
  let current = null;
  for (const turn of state.run.turns) {
    if (turn.start_sec <= t + 0.05) current = turn; else break;
  }
  const prev = document.querySelector(".turn.playing");
  const next = current && !player.paused ? document.querySelector(`.turn[data-idx="${current.idx}"]`) : null;
  if (prev === next) return;
  if (prev) prev.classList.remove("playing");
  if (next) {
    next.classList.add("playing");
    scrollWithin(next, "nearest");
  }
}

/* ------------------------------------------------------- record, audit, log */

function renderRecord() {
  const run = state.run;
  const flagged = new Set(run.results
    .filter((r) => r.status === "fail" && r.evidence?.reference?.source === "crm")
    .map((r) => r.evidence.reference.path || "email"));

  $("crmList").innerHTML = Object.entries(run.crm_snapshot)
    .filter(([k]) => !k.startsWith("_"))
    .map(([k, v]) => `<dt>${esc(k.replace(/_/g, " "))}</dt><dd class="${flagged.has(k) ? "flagged" : ""}">${esc(
      typeof v === "boolean" ? (v ? "yes" : "no") : v)}</dd>`).join("");

  const plan = run.plan_snapshot;
  $("planMeta").textContent = plan.plan_id;
  const planRows = plan.peak_rate_cents !== undefined ? [
    ["plan", plan.plan_name],
    ["peak rate", `${plan.peak_rate_cents} c/kWh`],
    ["daily supply", `${plan.daily_supply_cents} c/day`],
    ["feed-in", `${plan.solar_fit_cents} c/kWh`],
    ["vs DMO", `${plan.dmo_delta_pct}%`],
    ["term", plan.contract_term_months ? `${plan.contract_term_months} months` : "no lock-in"],
  ] : Object.entries({ ...plan, ...(run.lead.commercials || {}) })
    .filter(([k]) => !["plan_id", "retailer"].includes(k))
    .map(([k, v]) => [k.replace(/_cents$/, "").replace(/_/g, " "), fmtValue(v, k)]);
  $("planList").innerHTML = planRows.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("");
}

function renderAudit() {
  const run = state.run;
  const gate = run.gate;
  const k = gate.counters;
  const stat = (label, value) => `<div class="stat"><b>${esc(value)}</b><span>${esc(label)}</span></div>`;
  $("auditPanel").innerHTML = [
    stat("run", run.run_id),
    stat("checklist", `${run.checklist.checklist_id} ${run.checklist.checklist_version}`),
    stat("Type B extractor", run.extractors?.type_b_active || "-"),
    stat("critical confidence floor", gate.policy.confidence_floor_critical.toFixed(2)),
    stat("weighted score", `${k.weighted_score}%`),
    stat("evidence coverage", `${k.evidence_coverage_pct}%`),
    stat("queue", gate.queue ? gate.queue.queue : "none"),
    stat("transcript sha256", `${(run.transcript.transcript_sha256 || "").slice(0, 16)}…`),
  ].join("");
  $("pipelineTrace").innerHTML = run.pipeline_trace.map((step) => `
    <div class="tracerow"><b>${esc(step.stage)}</b><span>${esc(step.detail)}</span>
      <span class="ms">${step.ms}ms</span></div>`).join("");
  $("exportBtn").href = `/api/runs/${run.run_id}/export`;
  $("exportBtn").title = run.run_id;
}

function renderLog() {
  const log = state.run.override_log || [];
  $("overrideMeta").textContent = log.length;
  $("overrideLog").innerHTML = log.length
    ? log.slice().reverse().map((entry) => `
      <div class="ovrow">
        <div class="h">
          <span class="id">${esc(entry.check_id)}</span>
          <span><span class="f">${esc(entry.from_status)}</span> &rarr; <span class="t">${esc(entry.to_status)}</span></span>
          <span class="meta" style="margin-left:auto">${esc(entry.actor)}</span>
        </div>
        <p class="r">${esc(entry.reason)}</p>
        <p class="w">gate ${esc(entry.gate_before)} &rarr; ${esc(entry.gate_after)} · machine said
          ${esc(entry.machine_status)} · crm_written=${entry.crm_written} · ${esc(entry.at)}</p>
      </div>`).join("")
    : '<p class="empty">No overrides on this run. Every machine verdict stands as scored.</p>';
}

/* ---------------------------------------------------------------- override */

function openOverride(checkId) {
  const r = state.run.results.find((x) => x.check_id === checkId);
  if (!r) return;
  state.pending = { checkId, to: r.status === "pass" ? "fail" : "pass" };
  $("ovCheckName").textContent = `${r.name} (${r.check_id} v${r.check_version})`;
  $("ovMachine").textContent =
    `machine verdict: ${r.machine_status.toUpperCase()} at ${r.machine_confidence.toFixed(2)} confidence, ${r.timestamp}`;
  $("ovReason").value = "";
  $("ovHint").className = "hint";
  $("ovHint").textContent = "At least 10 characters. The machine verdict is kept alongside your decision.";
  syncToggle();
  $("overrideModal").hidden = false;
  $("ovReason").focus();
}

function syncToggle() {
  $("ovToggle").querySelectorAll("button").forEach((b) => {
    b.setAttribute("aria-pressed", String(b.dataset.status === state.pending.to));
  });
}

async function submitOverride() {
  const reason = $("ovReason").value.trim();
  if (reason.length < 10) {
    $("ovHint").className = "hint bad";
    $("ovHint").textContent = `A reason of at least 10 characters is required (${reason.length} so far).`;
    $("ovReason").focus();
    return;
  }
  setBusy(true);
  try {
    const run = await api("/api/override", {
      run_id: state.run.run_id,
      check_id: state.pending.checkId,
      to_status: state.pending.to,
      reason,
      actor: $("ovActor").value.trim() || "unattributed",
    });
    $("overrideModal").hidden = true;
    const previous = state.run.gate.display_status;
    state.run = run;
    render();
    toast(previous === run.gate.display_status
      ? `Override recorded. Gate stays ${run.gate.display_status}.`
      : `Override recorded. Gate ${previous} -> ${run.gate.display_status}.`);
  } catch (err) {
    $("ovHint").className = "hint bad";
    $("ovHint").textContent = err.message;
  } finally {
    setBusy(false);
  }
}

/* -------------------------------------------------------------------- flow */

function setBusy(busy) {
  state.busy = busy;
  document.body.classList.toggle("busy", busy);
  $("rerunBtn").disabled = busy;
}

function renderChecklistMeta() {
  const c = state.run.checklist;
  $("checklistMeta").textContent = `${c.checklist_id} ${c.checklist_version} · ${state.run.results.length} checks`;
  $("checklistMeta").title = `in force from ${c.effective_from}`;
  $("brandSub").textContent = `${c.retailer} · checklist ${c.checklist_version}`;
  const legend = state.run.checklist.pack_id && state.run.checklist.pack_id !== "default"
    ? null : state.boot.checklist.type_legend;
  $("typeLegend").innerHTML = legend
    ? ["A", "B", "C"].map((t) => `<b>Type ${t}</b> ${esc(legend[t])}`).join("<br>")
    : "<b>Type A</b> script wording, deterministic. <b>Type B</b> the spoken value against the lead and plan. <b>Type C</b> coaching only, never blocks.";
}

/* A new run starts at the top of every panel; an override re-render keeps its place. */
function resetView() {
  setTab("transcript");
  for (const id of ["checkList", "tab-transcript", "tab-record", "tab-audit", "tab-log"]) $(id).scrollTop = 0;
}

function render() {
  const lead = state.boot.leads.find((l) => l.lead_id === state.run.lead_id);
  if (lead && lead.last_gate !== state.run.gate.status) {
    lead.last_gate = state.run.gate.status;
    renderLeads();
  }
  renderChecklistMeta();
  setupPlayer();
  renderVerdict();
  renderChecks();
  renderTimeline();
  renderTranscript();
  renderRecord();
  renderAudit();
  renderLog();
  highlightTurn();
}

function showLoading(label, detail) {
  $("verdict").className = "verdict";
  $("statusChip").className = "statuschip";
  $("statusText").textContent = label;
  $("verdictTitle").textContent = detail;
  $("chipSub").textContent = "";
}

/* Opening a lead shows its latest saved result - no scoring, no LLM cost. Scoring
   happens only when a lead has never been scored, or when Re-run is pressed. */
async function loadLead(leadId, rescore = false) {
  if (state.busy) return;
  state.leadId = leadId;
  state.selected = null;
  state.open = { passed: false, notes: false };
  renderLeads();
  setBusy(true);
  try {
    if (!rescore) {
      const { run } = await api(`/api/leads/${leadId}/latest`);
      if (run) {
        state.run = run;
        render();
        resetView();
        return;
      }
    }
    showLoading("INGEST", `Attaching the transcript for ${leadId}…`);
    const ingest = await api("/api/ingest", { lead_id: leadId });
    showLoading("SCORING", `Scoring ${plural(ingest.turn_count, "turn")} against the checklist…`);
    state.run = await api("/api/score", { ingest_id: ingest.ingest_id });
    render();
    resetView();
  } catch (err) {
    showLoading("ERROR", err.message);
    toast(err.message, true);
  } finally {
    setBusy(false);
  }
}

/* ---------------------------------------------------------------- dialler */

/* Leads scored in the background (or by another tab) get their dot filled in. */
async function pollLeadStatus() {
  if (!state.boot.leads.some((l) => !l.last_gate)) return;
  let status;
  try { status = (await api("/api/leads/status")).leads; } catch { return; }
  let changed = false;
  for (const lead of state.boot.leads) {
    const gate = status[lead.lead_id];
    if (gate && gate !== lead.last_gate) { lead.last_gate = gate; changed = true; }
  }
  if (changed) renderLeads();
}

async function pollJobs() {
  pollLeadStatus();
  let jobs;
  try { jobs = (await api("/api/jobs")).jobs; } catch { return; }
  const active = jobs.filter((j) => !["done", "error"].includes(j.status));
  const badge = $("diallerBadge");
  badge.hidden = !active.length;
  badge.textContent = active.length
    ? `Dialler: ${active[0].lead_id} ${active[0].status}${active.length > 1 ? ` +${active.length - 1}` : ""}`
    : "";

  for (const job of jobs.slice().reverse()) {
    const seen = state.jobs.get(job.job_id);
    state.jobs.set(job.job_id, job.status);
    // The first poll only learns what already happened; it announces nothing.
    if (!state.jobsPrimed || seen === job.status) continue;
    if (job.status === "done") {
      const lead = state.boot.leads.find((l) => l.lead_id === job.lead_id);
      if (lead) { lead.has_asr_transcript = true; lead.has_audio = true; renderLeads(); }
      if (job.lead_id === state.leadId && !state.busy) {
        await showRun(job.run_id);
        toast(`Recording for ${job.lead_id} transcribed and scored: ${job.gate}`);
      } else {
        toast(`Recording for ${job.lead_id} scored: ${job.gate} - click to open`, false, () => showRun(job.run_id));
      }
    } else if (job.status === "error") {
      toast(`Dialler recording for ${job.lead_id} failed: ${job.error}`, true);
    }
  }
  state.jobsPrimed = true;
}

async function showRun(runId) {
  state.run = await api(`/api/runs/${runId}`);
  state.leadId = state.run.lead_id;
  state.selected = null;
  renderLeads();
  render();
  resetView();
}

/* ------------------------------------------------------------------- eval */

function openEval() {
  // Running the evaluation re-scores all 12 labelled calls; with Claude on, that is
  // roughly four API calls per call. Say so on the button rather than surprise anyone.
  const llm = state.boot.extractors.llm_available;
  $("evRun").textContent = llm ? "Run evaluation (~48 Claude calls)" : "Run evaluation";
  $("evRun").title = llm ? "Re-scores all 12 labelled calls with Claude. The last saved report is shown for free." : "";
  $("evalModal").hidden = false;
  loadEval(false);
}

async function loadEval(run) {
  const source = state.evalSource;
  $("evSource").querySelectorAll("button").forEach((b) =>
    b.setAttribute("aria-pressed", String(b.dataset.source === source)));
  $("evBody").innerHTML = `<p class="evnote">${run ? "Scoring every labelled call…" : "Loading…"}</p>`;
  $("evRun").disabled = true;
  try {
    const report = run ? await api("/api/eval", { source }) : await api(`/api/eval?source=${source}`);
    renderEval(report);
  } catch (err) {
    $("evBody").innerHTML = `<p class="evnote">${esc(err.message)}. ${source === "asr"
      ? "Push recordings first with <code>python tools/dialler_sim.py --all</code>, then" : ""} press <b>Run evaluation</b>.</p>`;
  } finally {
    $("evRun").disabled = false;
  }
}

function renderEval(r) {
  const s = r.summary;
  const stat = (label, value, tone = "") =>
    `<div class="evstat ${tone}"><b>${esc(value)}</b><span>${esc(label)}</span></div>`;
  const pctv = (v) => (v === null || v === undefined ? "-" : `${v}%`);
  $("evSub").textContent = `${s.calls} hand-labelled calls · ${r.transcript_source === "asr"
    ? "transcripts produced by ASR from audio" : "reference transcripts"} · Type B: ${r.extractor} · ${r.generated_at}`;
  let html = `<div class="evstats">
    ${stat("critical false passes", s.critical_false_pass, s.critical_false_pass ? "bad" : "good")}
    ${stat("sales wrongly submitted", s.gate_false_submit, s.gate_false_submit ? "bad" : "good")}
    ${stat("sales wrongly held", s.gate_false_hold, s.gate_false_hold ? "bad" : "good")}
    ${stat("agreement where the machine decided", pctv(s.agreement_pct_decided))}
    ${stat("Cohen's kappa vs labels", s.kappa ?? "-")}
    ${stat("verdicts routed to a human", pctv(s.abstain_pct))}
  </div>
  <p class="evnote">Unsure counts as neither agreement nor error: the system declined to guess and routed the call to QA.
    Labels live in data/synth/truth.json and the scorer never reads them.</p>
  <table class="evtable"><thead><tr><th>Check</th><th>Critical</th><th class="num">N</th><th class="num">Agree</th>
    <th class="num">Unsure</th><th class="num">False pass</th><th class="num">False fail</th><th class="num">Agree %</th><th class="num">Kappa</th></tr></thead><tbody>
    ${r.per_check.map((b) => `<tr><td>${esc(b.check_id)}</td><td>${b.critical ? "yes" : "-"}</td>
      <td class="num">${b.n}</td><td class="num">${b.agree}</td><td class="num">${b.abstain}</td>
      <td class="num ${b.false_pass ? "FALSE_PASS" : ""}">${b.false_pass}</td>
      <td class="num ${b.false_fail ? "false_fail" : ""}">${b.false_fail}</td>
      <td class="num">${pctv(b.agreement_pct)}</td><td class="num">${b.kappa ?? "-"}</td></tr>`).join("")}
  </tbody></table>
  <table class="evtable"><thead><tr><th>Lead</th><th>Scenario</th><th>Label</th><th>Machine</th><th>Outcome</th></tr></thead><tbody>
    ${r.gates.map((g) => `<tr class="clickable" data-lead="${esc(g.lead_id)}"><td>${esc(g.lead_id)}</td><td>${esc(g.title)}</td>
      <td>${esc(g.label)}</td><td>${esc(g.machine)}</td><td class="${esc(g.outcome)}">${esc(g.outcome.replace("_", " "))}</td></tr>`).join("")}
  </tbody></table>`;
  if (r.disagreements.length) {
    html += `<table class="evtable"><thead><tr><th>Lead</th><th>Check</th><th>Label</th><th>Machine</th><th class="num">Conf</th><th>At</th><th>Outcome</th></tr></thead><tbody>
      ${r.disagreements.map((d) => `<tr class="clickable" data-lead="${esc(d.lead_id)}"><td>${esc(d.lead_id)}</td><td>${esc(d.check_id)}</td>
        <td>${esc(d.label)}</td><td>${esc(d.machine)}</td><td class="num">${d.confidence.toFixed(2)}</td>
        <td>${esc(d.timestamp)}</td><td class="${esc(d.outcome)}">${esc(d.outcome.replace("_", " "))}</td></tr>`).join("")}
    </tbody></table>`;
  }
  if ((r.skipped_no_asr_transcript || []).length) {
    html += `<p class="evnote">Not yet received from the dialler: ${r.skipped_no_asr_transcript.map(esc).join(", ")}</p>`;
  }
  $("evBody").innerHTML = html;
  $("evBody").querySelectorAll("tr[data-lead]").forEach((row) => {
    row.onclick = () => { $("evalModal").hidden = true; loadLead(row.dataset.lead); };
  });
}

/* ------------------------------------------------------------------- boot */

async function boot() {
  try {
    state.boot = await api("/api/bootstrap");
  } catch (err) {
    showLoading("ERROR", err.message);
    return;
  }
  state.leadId = state.boot.preload_lead_id;
  renderEngine();
  renderLeads();

  if (state.boot.run) {
    state.run = state.boot.run;
    state.leadId = state.run.lead_id;
    renderLeads();
    render();
  } else {
    await loadLead(state.leadId);
  }
  for (const warning of state.boot.warnings || []) toast(warning, true);
  pollJobs();
  setInterval(pollJobs, 3000);
}

$("rerunBtn").onclick = () => loadLead(state.leadId, true);
$("leadBtn").onclick = () => ($("leadMenu").hidden ? openLeadMenu() : closeLeadMenu(true));
// Close the lead list on any press outside it. pointerdown in the capture phase fires
// before any element can stop it, and also for scrollbars and the audio controls,
// which never produce a click.
document.addEventListener("pointerdown", (e) => {
  if (!e.target.closest?.(".leadsel")) closeLeadMenu();
}, true);
window.addEventListener("blur", () => closeLeadMenu());
$("leadMenu").addEventListener("keydown", (e) => {
  const items = [...$("leadMenu").querySelectorAll(".leaditem")];
  const i = items.indexOf(document.activeElement);
  if (e.key === "ArrowDown") { e.preventDefault(); items[Math.min(items.length - 1, i + 1)]?.focus(); }
  if (e.key === "ArrowUp") { e.preventDefault(); items[Math.max(0, i - 1)]?.focus(); }
  if (e.key === "Escape") closeLeadMenu(true);
});
$("player").addEventListener("timeupdate", followPlayback);
$("evalBtn").onclick = openEval;
$("evClose").onclick = () => { $("evalModal").hidden = true; };
$("evRun").onclick = () => loadEval(true);
$("evSource").onclick = (e) => {
  const btn = e.target.closest("button[data-source]");
  if (!btn) return;
  state.evalSource = btn.dataset.source;
  loadEval(false);
};
$("evalModal").onclick = (e) => { if (e.target === $("evalModal")) $("evalModal").hidden = true; };
$("ovCancel").onclick = () => { $("overrideModal").hidden = true; };
$("ovSubmit").onclick = submitOverride;
$("ovToggle").onclick = (e) => {
  const btn = e.target.closest("button[data-status]");
  if (!btn || !state.pending) return;
  state.pending.to = btn.dataset.status;
  syncToggle();
};
$("overrideModal").onclick = (e) => { if (e.target === $("overrideModal")) $("overrideModal").hidden = true; };
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") { $("overrideModal").hidden = true; $("evalModal").hidden = true; }
});

boot();
