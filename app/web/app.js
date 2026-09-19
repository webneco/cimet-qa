/* CIMET QA Gate - single page console.
   The UI never writes to the CRM. The only mutating call it can make is
   POST /api/override, which requires a written reason. */

const $ = (id) => document.getElementById(id);

const state = {
  boot: null,
  run: null,
  leadId: null,
  selected: null,
  busy: false,
  pending: null, // check awaiting an override decision
  stopAt: null,  // seconds - a clip played from evidence stops here
  jobs: new Map(), // dialler job_id -> last seen status
  jobsPrimed: false,
  evalSource: "fixture",
};

const CLIP_SEC = 20; // "clicks the timestamp, hears twenty seconds"

const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const mmss = (sec) => {
  if (sec === null || sec === undefined) return "--:--";
  const s = Math.round(Number(sec));
  return `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
};

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

/* ------------------------------------------------------------------ header */

const leadName = (lead) => (/^\[/.test(lead.account_holder) ? "Redacted customer" : lead.account_holder);

function leadTags(lead) {
  return [
    lead.vertical === "nbn" ? '<span class="tagx nbn" title="NBN-QA checklist">NBN</span>' : "",
    lead.has_asr_transcript ? '<span class="tagx asr" title="scored from a dialler recording">ASR</span>' : "",
  ].join("");
}

function renderLeads() {
  const current = state.boot.leads.find((l) => l.lead_id === state.leadId) || state.boot.leads[0];
  $("leadBtn").innerHTML = `
    <span class="dot ${esc(current.audio_quality)}"></span>
    <span class="lid">${esc(current.lead_id)}</span>
    <span class="lname">${esc(leadName(current))}</span>
    ${leadTags(current)}
    <span class="chev" aria-hidden="true">&#9662;</span>`;

  const item = (lead) => `
    <button type="button" class="leaditem" role="option" data-lead="${esc(lead.lead_id)}"
            aria-selected="${lead.lead_id === state.leadId}">
      <span class="dot ${esc(lead.audio_quality)}"></span>
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

  const c = state.boot.checklist;
  $("brandSub").textContent = `${state.boot.retailer} - checklist ${c.checklist_version}`;
  $("checklistMeta").textContent = `${c.checklist_id} - ${c.checks.length} checks - in force from ${c.effective_from}`;
  $("typeLegend").innerHTML = ["A", "B", "C"]
    .map((t) => `<b>TYPE ${t}</b> ${esc(c.type_legend[t])}`).join("<br>");
}

/* --------------------------------------------------------------- gate band */

function renderGate() {
  const run = state.run;
  const gate = run.gate;
  const chip = $("statusChip");
  chip.className = `statuschip ${gate.status.toLowerCase()} flash`;
  setTimeout(() => chip.classList.remove("flash"), 650);
  $("statusText").textContent = gate.status;
  document.querySelector(".gateband").style.setProperty("--band-glow",
    { HELD: "rgba(255,96,112,.14)", QA: "rgba(255,180,58,.13)", SUBMITTED: "rgba(53,217,153,.11)" }[gate.status]);

  $("chipSub").innerHTML =
    (gate.queue ? `queued &rarr; ${esc(gate.queue.queue)}` : "no queue") +
    (gate.sample_audit ? `<span class="sample">SAMPLE AUDIT</span>` : "");

  $("gateReasons").innerHTML = gate.reasons.map((r) => `
    <div class="reason">
      ${r.start_sec !== null && r.start_sec !== undefined
        ? `<span class="rt" data-jump="${r.start_sec}" data-check="${esc(r.check_id || "")}">${esc(r.timestamp)}</span>`
        : `<span class="rr">${esc(r.rule)}</span>`}
      <span>${esc(r.detail)}</span>
    </div>`).join("");
  $("gateReasons").querySelectorAll("[data-jump]").forEach((el) => {
    el.onclick = () => {
      if (el.dataset.check) selectCheck(el.dataset.check, true);
      playAt(Number(el.dataset.jump), CLIP_SEC);
    };
  });

  const k = gate.counters;
  $("gateCounters").innerHTML = [
    ["critical pass", k.critical_pass, "good"],
    ["critical fail", k.critical_fail, k.critical_fail ? "bad" : ""],
    ["critical unsure", k.critical_unsure, k.critical_unsure ? "warn" : ""],
    ["confidence floor", gate.policy.confidence_floor_critical.toFixed(2), ""],
    ["weighted score", `${k.weighted_score}%`, ""],
    ["evidence coverage", `${k.evidence_coverage_pct}%`, ""],
    ["coaching notes", k.coaching_notes, ""],
  ].map(([label, value, tone]) =>
    `<span class="counter ${tone}">${esc(label)} <b>${esc(value)}</b></span>`).join("");

  $("pipelineTrace").innerHTML = run.pipeline_trace.map((step) => `
    <div class="tracerow"><b>${esc(step.stage)}</b><span>${esc(step.detail)}</span>
      <span class="ms">${step.ms}ms</span></div>`).join("");

  const exportBtn = $("exportBtn");
  exportBtn.href = `/api/runs/${run.run_id}/export`;
  exportBtn.textContent = `Download audit JSON`;
  exportBtn.title = run.run_id;
}

/* ------------------------------------------------------------------ checks */

function confClass(value) { return value >= 0.8 ? "high" : value >= 0.6 ? "" : "low"; }

function renderChecks() {
  const run = state.run;
  let html = "";
  let section = null;
  for (const r of run.results) {
    if (r.section !== section) {
      section = r.section;
      html += `<div class="sectionhead">${esc(section)}</div>`;
    }
    const label = r.outcome_label.toLowerCase();
    html += `
      <div class="checkrow ${state.selected === r.check_id ? "selected" : ""} ${r.blocks_sale ? "blocking" : ""}"
           data-check="${esc(r.check_id)}">
        <div class="main">
          <span class="pill ${label === "note" ? "note" : r.status}">${esc(r.outcome_label)}</span>
          <span class="cname">
            <span class="n">${esc(r.name)}</span>
            <span class="m">
              <span class="tag">TYPE ${esc(r.type)}</span>
              ${r.critical ? '<span class="tag crit">CRITICAL</span>' : '<span class="tag">non-blocking</span>'}
              <span class="tag">v${esc(r.check_version)}</span>
              ${r.overridden ? '<span class="tag ov">OVERRIDDEN</span>' : ""}
            </span>
          </span>
          <span class="conf ${confClass(r.confidence)}">
            <span class="v">${r.confidence.toFixed(2)}</span>
            <span class="bar"><i style="width:${Math.round(r.confidence * 100)}%"></i></span>
          </span>
          <span class="at">${esc(r.timestamp)}</span>
          <span class="blockcell ${r.blocks_sale ? "yes" : ""}">${r.blocks_sale ? "YES" : "no"}</span>
        </div>
        ${state.selected === r.check_id ? renderEvidence(r) : ""}
      </div>`;
  }
  $("checkList").innerHTML = html;
  $("checkList").querySelectorAll(".checkrow .main").forEach((el) => {
    el.onclick = () => selectCheck(el.parentElement.dataset.check);
  });
  const overrideBtn = $("checkList").querySelector("[data-override]");
  if (overrideBtn) overrideBtn.onclick = () => openOverride(overrideBtn.dataset.override);
  const playBtn = $("checkList").querySelector("[data-play]");
  if (playBtn) playBtn.onclick = () => playAt(Number(playBtn.dataset.play), CLIP_SEC);
  $("checkList").querySelectorAll("[data-seek-evidence]").forEach((el) => {
    el.onclick = () => {
      const sec = Number(el.dataset.seekEvidence);
      const turn = state.run.turns.find((t) => t.start_sec === sec);
      if (turn) {
        document.querySelectorAll(".turn.hit").forEach((t) => t.classList.remove("hit"));
        const node = document.querySelector(`.turn[data-idx="${turn.idx}"]`);
        if (node) { node.classList.add("hit"); node.scrollIntoView({ block: "center", behavior: "smooth" }); }
      }
      playAt(sec, CLIP_SEC);
    };
  });
}

function fmtValue(v, key = "") {
  if (v === null || v === undefined) return "not supplied";
  if (typeof v === "boolean") return v ? "yes" : "no";
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

function renderEvidence(r) {
  const e = r.evidence || {};
  let body = "";

  if (r.quote) {
    body += `<blockquote class="quote">
      <span class="qmeta">${esc(r.timestamp)} - ${esc(r.speaker || "unknown")} - turn ${esc(r.turn_idx)}
        ${e.quote_role === "closest_matching_span" ? " - closest matching span, not proof of compliance" : ""}</span>
      ${decorate(r.quote)}</blockquote>`;
  }
  if (r.note) body += `<p class="hint" style="margin:-4px 0 10px">${esc(r.note)}</p>`;

  for (const g of r.guardrails || []) {
    const code = g.slice(0, g.indexOf(" ") > 0 ? g.indexOf(" ") : 2);
    body += `<div class="guard"><b>${esc(code)}</b> ${esc(g.slice(code.length).trim())}</div>`;
  }

  if (r.override) {
    body += `<div class="ovbox"><b>Overridden by ${esc(r.override.actor)}</b> -
      ${esc(r.override.machine_status)} &rarr; ${esc(r.override.to_status)}<br>
      ${esc(r.override.reason)}<br>
      <span style="color:var(--dim);font-size:11px">machine verdict kept:
      ${esc(r.override.machine_status)} at ${r.override.machine_confidence.toFixed(2)} confidence</span></div>`;
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
        <span class="mk">${p.found ? "OK" : "--"}</span>
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
        ? `<b>${esc(x.value)}</b> <span style="color:var(--dim)">(extraction confidence ${x.confidence})</span>`
        : `<span class="bad">not established</span> - ${esc(x.reason || "")}`],
      ["reference", `${esc(e.reference.label)} = <b>${esc(fmtValue(e.reference.value, e.reference.path || ""))}</b>
        <span style="color:var(--dim)">from ${esc(e.reference.source)}</span>`],
      (x.other_mentions || []).length ? ["other mentions", x.other_mentions.map((m) =>
        `<span class="mention"><span class="rt" data-seek-evidence="${m.start_sec}">${esc(mmss(m.start_sec))}</span> ${esc(m.value)} <span style="color:var(--dim)">(${esc(m.kind)})</span></span>`).join("")] : null,
      c ? ["comparison", `${c.match === true ? '<span class="good">match</span>' : c.match === false ? '<span class="bad">mismatch</span>' : "not comparable"} - ${esc(c.detail)}`] : null,
      ["extractor", `${esc(e.extractor)}${e.effort ? ` (effort ${esc(e.effort)})` : ""}${e.degraded_from
        ? ` <span class="bad" title="${esc(e.degrade_reason || "")}">- fell back from ${esc(e.degraded_from)}</span>` : ""}`],
      ["leak guard", `the extractor never saw ${esc(e.reference.label)} - ${esc(e.leak_guard)}`],
      ["grounding", e.grounding
        ? `quote ${e.grounding.quote_grounded_in_transcript ? "found in" : "<span class='bad'>not found in</span>"} the transcript (${Math.round((e.grounding.match_ratio || 0) * 100)}% match)`
        : "-"],
      e.prompt_sha256 ? ["prompt sha256", `<span style="font-family:var(--mono);font-size:10.5px">${esc(e.prompt_sha256.slice(0, 32))}...</span>`] : null,
      ["check version", `${esc(r.check_version)} (from ${esc(r.effective_from)})`],
      ["basis", esc(r.regulatory_basis || "-")],
    ]);
  } else {
    body += kv([
      ["metric", esc(e.metric)],
      ["observed", `<b>${esc(e.observed)}</b> against a threshold of ${esc(e.threshold)}`],
      ["occurrences", (e.occurrences || []).length
        ? e.occurrences.map((o) => `${esc(o.timestamp)}${o.duration_sec ? ` (${o.duration_sec}s)` : ""}`).join(", ")
        : "none"],
      ["blocks sale", '<span class="good">never</span>'],
      ["policy", esc(e.policy)],
    ]);
  }

  const canOverride = r.type !== "C";
  const canPlay = state.run.audio_url && r.start_sec !== null && r.start_sec !== undefined && r.start_sec >= 0;
  body += `<div class="evactions">
    ${canPlay ? `<button class="btn small" data-play="${r.start_sec}">&#9654; Play ${CLIP_SEC}s from ${esc(r.timestamp)}</button>` : ""}
    ${canOverride
      ? `<button class="btn small" data-override="${esc(r.check_id)}">Override to ${r.status === "pass" ? "FAIL" : "PASS"}</button>`
      : '<span class="hint">Type C notes are coaching signal only and cannot be overridden into a blocking state.</span>'}
  </div>`;

  return `<div class="evidence">${body}</div>`;
}

function selectCheck(checkId, keepOpen = false) {
  state.selected = (state.selected === checkId && !keepOpen) ? null : checkId;
  renderChecks();
  revealSelectedRow();
  highlightTurn();
}

function revealSelectedRow() {
  if (!state.selected) return;
  const box = $("checkList");
  const row = box.querySelector(`.checkrow[data-check="${state.selected}"]`);
  if (row) box.scrollTo({ top: Math.max(0, row.offsetTop - 6), behavior: "smooth" });
}

function highlightTurn() {
  document.querySelectorAll(".turn.hit").forEach((el) => el.classList.remove("hit"));
  if (!state.selected) return;
  const result = state.run.results.find((r) => r.check_id === state.selected);
  if (!result || result.turn_idx === null || result.turn_idx === undefined) return;
  const el = document.querySelector(`.turn[data-idx="${result.turn_idx}"]`);
  if (!el) return;
  el.classList.add("hit");
  el.scrollIntoView({ block: "center", behavior: "smooth" });
}

/* -------------------------------------------------------------- transcript */

function renderTranscript() {
  const run = state.run;
  const t = run.transcript;
  const fromAudio = t.source === "dialler_recording_asr";
  $("transcriptMeta").textContent =
    `${t.turn_count} turns - ${mmss(t.duration_sec)} - ${fromAudio ? "from dialler recording" : t.audio_quality + " audio"} - ${t.asr_engine}`;
  $("transcriptMeta").title = fromAudio && t.speaker_mapping
    ? `agent = ${t.speaker_mapping.agent_speaker} by ${t.speaker_mapping.method} (margin ${t.speaker_mapping.margin}); audio sha256 ${t.audio?.sha256 || "-"}`
    : `source: ${t.source} (${t.transcript_path})`;
  $("redactBar").innerHTML = t.redactions_applied
    ? `${t.redactions_applied} sequence(s) masked at ingest - rule: ${esc(t.redaction_rule)}`
    : `nothing to mask on this call - rule: ${esc(t.redaction_rule)}`;

  const gaps = new Map();
  const deadAir = run.results.find((r) => r.check_id === "dead_air");
  for (const o of (deadAir?.evidence?.occurrences || [])) gaps.set(o.after_turn_idx, o);

  $("turnList").innerHTML = run.turns.map((turn) => {
    const gap = gaps.get(turn.idx);
    return `
      <div class="turn ${esc(turn.speaker)} ${turn.asr_confidence < 0.7 ? "lowasr" : ""}" data-idx="${turn.idx}">
        <span class="ts ${run.audio_url ? "play" : ""}" ${run.audio_url ? `data-seek="${turn.start_sec}" title="play from here"` : ""}>${mmss(turn.start_sec)}</span>
        <span>
          <span class="who">${esc(turn.speaker_name || turn.speaker)}${turn.asr_confidence < 0.7
            ? ` - asr ${turn.asr_confidence.toFixed(2)}` : ""}</span>
          <span class="tx">${decorate(turn.text)}</span>
        </span>
      </div>` + (gap
        ? `<div class="gapmark">${gap.duration_sec}s of dead air - ${esc(gap.timestamp)} - coaching note only</div>`
        : "");
  }).join("");
  $("turnList").querySelectorAll("[data-seek]").forEach((el) => {
    el.onclick = () => playAt(Number(el.dataset.seek), null);
  });
}

/* ------------------------------------------------------------------- audio */

function setupPlayer() {
  const run = state.run;
  const player = $("player");
  const src = run.audio_url ? `${run.audio_url}?v=${encodeURIComponent(run.run_id)}` : "";
  $("playerBar").hidden = !src;
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
  $("playerMeta").textContent = a ? `${a.filename} - sha256 ${a.sha256.slice(0, 10)}...` : "synthetic recording";
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
    next.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }
}

/* -------------------------------------------------------------- side panel */

function renderSide() {
  const run = state.run;
  const flagged = new Set(run.results
    .filter((r) => r.status === "fail" && r.evidence?.reference?.source === "crm")
    .map((r) => r.evidence.reference.path || "email"));

  const crm = run.crm_snapshot;
  $("crmList").innerHTML = Object.entries(crm)
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

  const log = run.override_log || [];
  $("overrideMeta").textContent = `${log.length} entr${log.length === 1 ? "y" : "ies"}`;
  $("overrideLog").innerHTML = log.length
    ? log.slice().reverse().map((entry) => `
      <div class="ovrow">
        <div class="h">
          <span class="id">${esc(entry.check_id)}</span>
          <span class="fromto"><span class="f">${esc(entry.from_status)}</span> &rarr;
            <span class="t">${esc(entry.to_status)}</span></span>
          <span style="color:var(--dim);margin-left:auto">${esc(entry.actor)}</span>
        </div>
        <p class="r">${esc(entry.reason)}</p>
        <p class="w">gate ${esc(entry.gate_before)} &rarr; ${esc(entry.gate_after)} -
          machine said ${esc(entry.machine_status)} - crm_written=${entry.crm_written} - ${esc(entry.at)}</p>
      </div>`).join("")
    : `<p class="empty">No overrides on this run. Every machine verdict stands as scored.</p>`;
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
  $("ovActor").value = $("ovActor").value || "";
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

/* ------------------------------------------------------------------- flow */

function setBusy(busy) {
  state.busy = busy;
  document.body.classList.toggle("busy", busy);
  $("rerunBtn").disabled = busy;
}

function renderChecklistMeta() {
  const c = state.run.checklist;
  $("checklistMeta").textContent =
    `${c.checklist_id} ${c.checklist_version} - ${state.run.results.length} checks - in force from ${c.effective_from}`;
  $("brandSub").textContent = `${c.retailer} - checklist ${c.checklist_version}`;
}

function render() {
  renderChecklistMeta();
  setupPlayer();
  renderGate();
  renderChecks();
  renderTranscript();
  renderSide();
  highlightTurn();
}

async function loadLead(leadId) {
  if (state.busy) return;
  state.leadId = leadId;
  state.selected = null;
  renderLeads();
  setBusy(true);
  $("statusChip").className = "statuschip loading";
  $("statusText").textContent = "INGEST";
  $("chipSub").textContent = `attaching local transcript for ${leadId}`;
  try {
    const ingest = await api("/api/ingest", { lead_id: leadId });
    $("statusText").textContent = "SCORING";
    $("chipSub").textContent = `${ingest.turn_count} turns attached - scoring against the checklist`;
    state.run = await api("/api/score", { ingest_id: ingest.ingest_id });
    render();
  } catch (err) {
    $("statusChip").className = "statuschip loading";
    $("statusText").textContent = "ERROR";
    $("chipSub").textContent = err.message;
    toast(err.message, true);
  } finally {
    setBusy(false);
  }
}

/* ---------------------------------------------------------------- dialler */

async function pollJobs() {
  let jobs;
  try { jobs = (await api("/api/jobs")).jobs; } catch { return; }
  const active = jobs.filter((j) => !["done", "error"].includes(j.status));
  const badge = $("diallerBadge");
  badge.hidden = !active.length;
  badge.className = "badge busy-dialler";
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
}

/* ------------------------------------------------------------------- eval */

function openEval() {
  $("evalModal").hidden = false;
  loadEval(false);
}

async function loadEval(run) {
  const source = state.evalSource;
  $("evSource").querySelectorAll("button").forEach((b) =>
    b.setAttribute("aria-pressed", String(b.dataset.source === source)));
  $("evBody").innerHTML = `<p class="evnote">${run ? "Scoring every labelled call..." : "Loading..."}</p>`;
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
  const pct = (v) => (v === null || v === undefined ? "-" : `${v}%`);
  $("evSub").textContent = `${s.calls} hand-labelled calls - ${r.transcript_source === "asr"
    ? "transcripts produced by ASR from audio" : "reference transcripts"} - Type B: ${r.extractor} - ${r.generated_at}`;
  let html = `<div class="evstats">
    ${stat("critical false passes", s.critical_false_pass, s.critical_false_pass ? "bad" : "good")}
    ${stat("sales wrongly submitted", s.gate_false_submit, s.gate_false_submit ? "bad" : "good")}
    ${stat("sales wrongly held", s.gate_false_hold, s.gate_false_hold ? "bad" : "good")}
    ${stat("agreement where the machine decided", pct(s.agreement_pct_decided))}
    ${stat("Cohen's kappa vs labels", s.kappa ?? "-")}
    ${stat("verdicts routed to a human", pct(s.abstain_pct))}
  </div>
  <p class="evnote">Unsure counts as neither agreement nor error: the system declined to guess and routed the call to QA.
    Labels live in data/synth/truth.json and the scorer never reads them.</p>
  <table class="evtable"><thead><tr><th>Check</th><th>Critical</th><th class="num">N</th><th class="num">Agree</th>
    <th class="num">Unsure</th><th class="num">False pass</th><th class="num">False fail</th><th class="num">Agree %</th><th class="num">Kappa</th></tr></thead><tbody>
    ${r.per_check.map((b) => `<tr><td>${esc(b.check_id)}</td><td>${b.critical ? "yes" : "-"}</td>
      <td class="num">${b.n}</td><td class="num">${b.agree}</td><td class="num">${b.abstain}</td>
      <td class="num ${b.false_pass ? "FALSE_PASS" : ""}">${b.false_pass}</td>
      <td class="num ${b.false_fail ? "false_fail" : ""}">${b.false_fail}</td>
      <td class="num">${pct(b.agreement_pct)}</td><td class="num">${b.kappa ?? "-"}</td></tr>`).join("")}
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

async function boot() {
  try {
    state.boot = await api("/api/bootstrap");
  } catch (err) {
    $("statusText").textContent = "ERROR";
    $("chipSub").textContent = err.message;
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

$("rerunBtn").onclick = () => loadLead(state.leadId);
$("leadBtn").onclick = () => ($("leadMenu").hidden ? openLeadMenu() : closeLeadMenu(true));
document.addEventListener("click", (e) => { if (!e.target.closest(".leadsel")) closeLeadMenu(); });
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
