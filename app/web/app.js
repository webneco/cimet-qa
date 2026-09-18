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
};

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

function toast(message, bad = false) {
  const el = $("toast");
  el.textContent = message;
  el.className = "toast" + (bad ? " bad" : "");
  el.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { el.hidden = true; }, bad ? 6000 : 3200);
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

function renderLeads() {
  $("leadPicker").innerHTML = state.boot.leads.map((lead) => `
    <button class="leadchip" role="tab" data-lead="${esc(lead.lead_id)}"
            aria-selected="${lead.lead_id === state.leadId}"
            title="${esc(lead.demo_note || "")}">
      <span class="dot ${esc(lead.audio_quality)}"></span>
      ${esc(lead.lead_id)}
      <span style="color:var(--dim)">${esc(lead.account_holder.split(" ")[0])}</span>
    </button>`).join("");

  $("leadPicker").querySelectorAll(".leadchip").forEach((chip) => {
    chip.onclick = () => loadLead(chip.dataset.lead);
  });
}

function renderEngine() {
  const engines = state.boot.extractors;
  const badge = $("extractorBadge");
  badge.textContent = engines.llm_available
    ? `Type B: ${engines.model}`
    : "Type B: deterministic";
  badge.className = "badge " + (engines.llm_available ? "live" : "offline");
  badge.title = engines.llm_available
    ? "Type B values are extracted by Claude, then compared to the CRM by deterministic code."
    : `LLM extraction is off - ${engines.llm_unavailable_reason}. Type B uses the built-in deterministic extractor instead.`;

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
    el.onclick = () => { if (el.dataset.check) selectCheck(el.dataset.check, true); };
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

  if (r.type === "A") {
    body += `<div class="phraselist">${(e.phrases || []).map((p) => `
      <div class="phrase ${p.found ? "ok" : "no"}">
        <span class="mk">${p.found ? "OK" : "--"}</span>
        <span class="txt">"${esc(p.phrase)}"</span>
        <span class="pct">${Math.round(p.ratio * 100)}%</span>
      </div>`).join("")}</div>`;
    body += kv([
      ["method", "deterministic script-span match, no LLM"],
      ["coverage", `<b>${Math.round(e.coverage * 100)}%</b> (pass at ${Math.round(e.pass_threshold * 100)}%, unsure at ${Math.round(e.unsure_threshold * 100)}%)`],
      (e.audio_markers_in_span || []).length ? ["audio markers", `<span class="bad">${esc(e.audio_markers_in_span.join(", "))}</span>`] : null,
      ["asr confidence", `min ${e.min_asr_confidence} / mean ${e.mean_asr_confidence}`],
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
      ["reference", `${esc(e.reference.label)} = <b>${esc(e.reference.value)}</b>
        <span style="color:var(--dim)">from ${esc(e.reference.source)}</span>`],
      c ? ["comparison", `${c.match === true ? '<span class="good">match</span>' : c.match === false ? '<span class="bad">mismatch</span>' : "not comparable"} - ${esc(c.detail)}`] : null,
      ["extractor", `${esc(e.extractor)}${e.effort ? ` (effort ${esc(e.effort)})` : ""}`],
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
  body += `<div class="evactions">
    ${canOverride
      ? `<button class="btn small" data-override="${esc(r.check_id)}">Override to ${r.status === "pass" ? "FAIL" : "PASS"}</button>`
      : '<span class="hint">Type C notes are coaching signal only and cannot be overridden into a blocking state.</span>'}
  </div>`;

  return `<div class="evidence">${body}</div>`;
}

function selectCheck(checkId, keepOpen = false) {
  state.selected = (state.selected === checkId && !keepOpen) ? null : checkId;
  renderChecks();
  highlightTurn();
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
  $("transcriptMeta").textContent =
    `${t.turn_count} turns - ${mmss(t.duration_sec)} - ${t.audio_quality} audio - ${t.asr_engine}`;
  $("redactBar").innerHTML = t.redactions_applied
    ? `${t.redactions_applied} sequence(s) masked at ingest - ${esc(t.redaction_rule)}`
    : `no masking needed - rule active: ${esc(t.redaction_rule)}`;

  const gaps = new Map();
  const deadAir = run.results.find((r) => r.check_id === "dead_air");
  for (const o of (deadAir?.evidence?.occurrences || [])) gaps.set(o.after_turn_idx, o);

  $("turnList").innerHTML = run.turns.map((turn) => {
    const gap = gaps.get(turn.idx);
    return `
      <div class="turn ${esc(turn.speaker)} ${turn.asr_confidence < 0.7 ? "lowasr" : ""}" data-idx="${turn.idx}">
        <span class="ts">${mmss(turn.start_sec)}</span>
        <span>
          <span class="who">${esc(turn.speaker_name || turn.speaker)}${turn.asr_confidence < 0.7
            ? ` - asr ${turn.asr_confidence.toFixed(2)}` : ""}</span>
          <span class="tx">${decorate(turn.text)}</span>
        </span>
      </div>` + (gap
        ? `<div class="gapmark">${gap.duration_sec}s of dead air - ${esc(gap.timestamp)} - coaching note only</div>`
        : "");
  }).join("");
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
    .map(([k, v]) => `<dt>${esc(k)}</dt><dd class="${flagged.has(k) ? "flagged" : ""}">${esc(
      typeof v === "boolean" ? (v ? "yes" : "no") : v)}</dd>`).join("");

  const plan = run.plan_snapshot;
  $("planMeta").textContent = plan.plan_id;
  $("planList").innerHTML = [
    ["plan", plan.plan_name],
    ["peak rate", `${plan.peak_rate_cents} c/kWh`],
    ["daily supply", `${plan.daily_supply_cents} c/day`],
    ["feed-in", `${plan.solar_fit_cents} c/kWh`],
    ["vs DMO", `${plan.dmo_delta_pct}%`],
    ["term", plan.contract_term_months ? `${plan.contract_term_months} months` : "no lock-in"],
  ].map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("");

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

function render() {
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
}

$("rerunBtn").onclick = () => loadLead(state.leadId);
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
  if (e.key === "Escape") $("overrideModal").hidden = true;
});

boot();
