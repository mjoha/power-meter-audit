"use strict";

const $ = (id) => document.getElementById(id);
let state = null;
let draft = null;          // locally edited protocol, applied on demand
let draftDirty = false;

/* ---------------- transport ---------------- */

function connectSocket() {
  const socket = new WebSocket(`ws://${location.host}/ws`);
  socket.onmessage = (event) => {
    const message = JSON.parse(event.data);
    if (message.type === "state") {
      state = message.state;
      if (!draftDirty) draft = clone(state.protocol);
      render();
    }
  };
  socket.onclose = () => setTimeout(connectSocket, 1500);
}

async function post(path, body) {
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.error || response.statusText);
  return payload;
}

const clone = (value) => JSON.parse(JSON.stringify(value));

function banner(message) {
  const element = $("banner");
  element.textContent = message || "";
  element.classList.toggle("hidden", !message);
}

/* ---------------- tabs ---------------- */

document.querySelectorAll(".tab").forEach((tab) => {
  tab.onclick = () => {
    document.querySelectorAll(".tab").forEach((t) => t.classList.remove("active"));
    tab.classList.add("active");
    ["devices", "protocol", "run"].forEach((name) => {
      $(`tab-${name}`).classList.toggle("hidden", name !== tab.dataset.tab);
    });
  };
});

function showTab(name) {
  document.querySelector(`.tab[data-tab="${name}"]`).click();
}

/* ---------------- devices ---------------- */

document.querySelectorAll('input[name="mode"]').forEach((radio) => {
  radio.onchange = () => {
    const hardware = radio.value === "hardware" && radio.checked;
    $("panel-hardware").classList.toggle("hidden", !hardware);
    $("panel-simulate").classList.toggle("hidden", hardware);
  };
});

function connectPayload() {
  const mode = document.querySelector('input[name="mode"]:checked').value;
  if (mode === "simulate") {
    return {
      mode,
      speed: Number($("sim-speed").value) || 1,
      pedal_scale: Number($("sim-scale").value) || 1,
      pedal_torque_gain: Number($("sim-torque").value) || 0,
      left_fraction: Number($("sim-left").value) || 0.5,
    };
  }
  return {
    mode,
    trainer_address: $("trainer-address").value.trim(),
    ant_device_id: Number($("ant-id").value) || 0,
    pedals_ble: $("pedals-ble").value.trim() || null,
  };
}

$("btn-connect").onclick = async () => {
  banner("");
  $("btn-connect").disabled = true;
  try {
    await post("/api/connect", connectPayload());
  } catch (error) {
    banner(`Could not connect: ${error.message}`);
  } finally {
    $("btn-connect").disabled = false;
  }
};

$("btn-disconnect").onclick = () => post("/api/disconnect").catch((e) => banner(e.message));

$("btn-scan").onclick = async () => {
  $("scan-status").textContent = "scanning…";
  $("btn-scan").disabled = true;
  try {
    const result = await post("/api/scan");
    const list = $("scan-results");
    list.innerHTML = "";
    (result.devices || []).forEach((device) => {
      const item = document.createElement("li");
      const tags = [];
      if (device.trainer) tags.push('<i class="tag trainer">trainer</i>');
      if (device.power) tags.push('<i class="tag power">power</i>');
      item.innerHTML =
        `<b>${device.name}</b> ${tags.join(" ")}<span class="sub"> ${device.address}` +
        `${device.rssi == null ? "" : ` · ${device.rssi} dBm`}</span>`;
      item.onclick = () => {
        $(device.power && !device.trainer ? "pedals-ble" : "trainer-address").value = device.address;
      };
      list.appendChild(item);
    });
    $("scan-status").textContent = result.error
      ? result.error
      : `${(result.devices || []).length} device(s) — click the one marked "trainer"`;
  } catch (error) {
    $("scan-status").textContent = error.message;
  } finally {
    $("btn-scan").disabled = false;
  }
};

/* ---------------- protocol ---------------- */

function readGlobals() {
  draft.warmup_s = Math.max(0, Number($("p-warmup").value) || 0) * 60;
  draft.cadence_tolerance_rpm = Number($("p-tolerance").value) || 5;
  const seconds = Number($("p-seconds").value) || 120;
  const cadences = $("p-cadences").value
    .split(",")
    .map((value) => parseInt(value.trim(), 10))
    .filter((value) => !Number.isNaN(value));
  draft.steps.forEach((step) => {
    step.seconds_per_cadence = seconds;
    step.cadences = cadences.length ? cadences : [70, 90];
  });
  draft.name = "custom";
}

function renderSteps() {
  if (!draft) return;
  const body = $("steps-body");
  body.innerHTML = "";
  draft.steps.forEach((step, index) => {
    const row = document.createElement("tr");
    row.innerHTML = `
      <td>${index + 1}</td>
      <td><input type="number" step="10" value="${step.watts}" data-index="${index}" data-field="watts"/></td>
      <td><input type="text" value="${step.label || ""}" data-index="${index}" data-field="label"/></td>
      <td><button class="secondary" data-remove="${index}">Remove</button></td>`;
    body.appendChild(row);
  });

  body.querySelectorAll("input").forEach((input) => {
    input.oninput = () => {
      const step = draft.steps[Number(input.dataset.index)];
      step[input.dataset.field] =
        input.dataset.field === "watts" ? Number(input.value) : input.value;
      draftDirty = true;
      updateSummary();
    };
  });
  body.querySelectorAll("button[data-remove]").forEach((button) => {
    button.onclick = () => {
      draft.steps.splice(Number(button.dataset.remove), 1);
      draftDirty = true;
      renderSteps();
      updateSummary();
    };
  });
}

function updateSummary() {
  if (!draft) return;
  readGlobals();
  const cells = draft.steps.reduce((total, step) => total + (step.cadences || []).length, 0);
  const seconds = draft.steps.reduce(
    (total, step) => total + (step.cadences || []).length * (step.seconds_per_cadence || 0),
    0
  );
  const minutes = (draft.warmup_s + seconds) / 60;
  $("protocol-summary").textContent =
    `${draft.steps.length} steps, ${cells} cells, ${minutes.toFixed(0)} min total`;
}

["p-warmup", "p-seconds", "p-cadences", "p-tolerance"].forEach((id) => {
  $(id).oninput = () => { draftDirty = true; updateSummary(); };
});

$("btn-add-step").onclick = () => {
  const last = draft.steps[draft.steps.length - 1];
  const watts = last ? last.watts + 50 : 200;
  draft.steps.push({
    watts,
    label: `${watts}W`,
    cadences: last ? last.cadences : [70, 90],
    seconds_per_cadence: last ? last.seconds_per_cadence : 120,
  });
  draftDirty = true;
  renderSteps();
  updateSummary();
};

document.querySelectorAll(".preset").forEach((button) => {
  button.onclick = async () => {
    try {
      draftDirty = false;
      await post("/api/protocol", { preset: button.dataset.preset });
      banner("");
    } catch (error) {
      banner(error.message);
    }
  };
});

$("btn-apply").onclick = async () => {
  readGlobals();
  try {
    await post("/api/protocol", draft);
    draftDirty = false;
    banner("");
    showTab("run");
  } catch (error) {
    banner(error.message);
  }
};

/* ---------------- run ---------------- */

$("btn-start").onclick = async () => {
  try {
    await post("/api/start");
    banner("");
  } catch (error) {
    banner(error.message);
  }
};
$("btn-stop").onclick = () => post("/api/stop").catch((e) => banner(e.message));

/* ---------------- rendering ---------------- */

function render() {
  if (!state) return;
  renderPhase();
  renderSources();
  renderProtocol();
  renderRun();
  renderResults();
  banner(state.error || "");
}

function offlineSources() {
  return Object.values(state.sources)
    .filter((source) => source.error)
    .map((source) => source.label);
}

function renderPhase() {
  const pill = $("phase-pill");
  const labels = {
    idle: "disconnected",
    partial: `${offlineSources().join(" & ").toLowerCase()} offline`,
    connected: "connected",
    running: "running",
    finished: "finished",
  };
  pill.textContent = labels[state.phase] || state.phase;
  pill.className =
    "pill" +
    (state.phase === "running" || state.phase === "partial"
      ? " busy"
      : state.phase === "idle"
      ? ""
      : " live");

  const speed = $("speed-pill");
  const accelerated = state.mode === "simulate" && state.speed > 1 && state.phase !== "idle";
  speed.classList.toggle("hidden", !accelerated);
  speed.textContent = `time ×${Math.round(state.speed)}`;

  // Connect stays available while partly connected so a fixed radio can be retried.
  $("btn-connect").disabled = state.phase === "connected" || state.phase === "running";
  $("btn-disconnect").disabled = state.phase === "idle";
  $("btn-start").disabled = !(state.phase === "connected" || state.phase === "finished");
  $("btn-stop").disabled = state.phase !== "running";
}

function renderSources() {
  [["trainer", "src-trainer"], ["pedals", "src-pedals"]].forEach(([key, id]) => {
    const source = state.sources[key] || {};
    const card = $(id);
    card.classList.toggle("alive", !!source.alive);
    card.querySelector(".watts").textContent =
      source.watts == null ? "—" : Math.round(source.watts);
    card.querySelector(".cadence").textContent =
      source.cadence == null ? "—" : Math.round(source.cadence);
    card.querySelector(".rate").textContent = source.hz ? `${source.hz.toFixed(1)} Hz` : "no data";
    card.classList.toggle("failed", !!source.error);
    let note = card.querySelector(".source-error");
    if (source.error && !note) {
      note = document.createElement("div");
      note.className = "source-error";
      card.appendChild(note);
    }
    if (note) {
      note.textContent = source.error || "";
      note.classList.toggle("hidden", !source.error);
    }
    const detail = card.querySelector(".source-detail");
    detail.textContent = source.detail ? `reading from ${source.detail}` : "";
    detail.classList.toggle("hidden", !source.detail);
  });
}

function renderProtocol() {
  if (!draft) return;
  if (!draftDirty) {
    $("p-warmup").value = Math.round(draft.warmup_s / 60);
    $("p-tolerance").value = draft.cadence_tolerance_rpm;
    const first = draft.steps[0];
    if (first) {
      $("p-seconds").value = first.seconds_per_cadence;
      $("p-cadences").value = (first.cadences || []).join(", ");
    }
    renderSteps();
  }
  updateSummary();

  const notes = $("protocol-notes");
  notes.innerHTML = "";
  (state.protocol.notes || []).forEach((note) => {
    const item = document.createElement("li");
    item.textContent = note;
    notes.appendChild(item);
  });
}

function renderRun() {
  const segment = state.segment;
  $("progress-bar").style.width = `${(state.progress * 100).toFixed(1)}%`;

  if (!segment) {
    $("segment-label").textContent = state.phase === "finished" ? "Session complete" : "Not started";
    if (state.phase === "finished") {
      $("segment-sub").textContent = "Review the grid below, or download the raw samples.";
    } else if (state.phase === "partial") {
      // Saying "connect the devices" here would be wrong: one of them is connected.
      $("segment-sub").textContent =
        `Comparing needs both sources. ${offlineSources().join(" and ")} did not connect —` +
        ` see the Devices tab.`;
    } else {
      $("segment-sub").textContent = "Connect the devices, then start the protocol.";
    }
  } else {
    $("segment-label").textContent = `${segment.label} — ${segment.target_watts} W`;
    const phase = segment.measuring ? "measuring" : "settling";
    $("segment-sub").textContent =
      `${phase} · ${formatClock(segment.remaining_s)} left in this cell`;
  }

  const warnings = $("warnings");
  warnings.innerHTML = "";
  (state.warnings || []).forEach((text) => {
    const item = document.createElement("li");
    item.textContent = text;
    warnings.appendChild(item);
  });
  warnings.classList.toggle("hidden", !(state.warnings || []).length);

  renderCadence(segment);
  renderLive();
  drawTrace();
}

function renderCadence(segment) {
  const trainer = state.sources.trainer || {};
  const current = trainer.cadence;
  const target = segment ? segment.target_rpm : null;
  const tolerance = state.cadence_tolerance_rpm || 5;

  $("cadence-value").textContent = current == null ? "—" : Math.round(current);
  $("cadence-target").textContent = target == null ? "—" : `${target} rpm`;

  const cue = $("cadence-cue");
  if (target == null) {
    cue.textContent = segment ? "free cadence" : "—";
    cue.className = "cue idle";
  } else if (current == null) {
    cue.textContent = "waiting for cadence";
    cue.className = "cue off";
  } else if (current < target - tolerance) {
    cue.textContent = `spin up to ${target}`;
    cue.className = "cue off";
  } else if (current > target + tolerance) {
    cue.textContent = `slow down to ${target}`;
    cue.className = "cue off";
  } else {
    cue.textContent = "on target";
    cue.className = "cue ok";
  }

  // Meter spans the target plus/minus 20 rpm so the tolerance band is legible.
  const centre = target != null ? target : current != null ? Math.round(current) : 85;
  const lo = centre - 20;
  const hi = centre + 20;
  $("meter-lo").textContent = `${lo} rpm`;
  $("meter-hi").textContent = `${hi} rpm`;

  const band = $("meter-band");
  if (target == null) {
    band.style.display = "none";
  } else {
    band.style.display = "block";
    band.style.left = `${pct(target - tolerance, lo, hi)}%`;
    band.style.width = `${(2 * tolerance / (hi - lo)) * 100}%`;
  }
  $("meter-marker").style.left =
    current == null ? "-10px" : `${pct(current, lo, hi)}%`;
}

const pct = (value, lo, hi) => Math.max(0, Math.min(100, ((value - lo) / (hi - lo)) * 100));

function renderLive() {
  const trainer = state.sources.trainer || {};
  const pedals = state.sources.pedals || {};
  $("live-trainer").textContent = trainer.watts == null ? "—" : Math.round(trainer.watts);
  $("live-pedals").textContent = pedals.watts == null ? "—" : Math.round(pedals.watts);
  const ratio =
    trainer.watts && pedals.watts && trainer.watts > 20 ? pedals.watts / trainer.watts : null;
  $("live-ratio").textContent = ratio == null ? "—" : ratio.toFixed(2);
}

function formatClock(seconds) {
  const total = Math.max(0, Math.round(seconds));
  return `${String(Math.floor(total / 60)).padStart(2, "0")}:${String(total % 60).padStart(2, "0")}`;
}

/* ---------------- trace chart ---------------- */

function drawTrace() {
  const canvas = $("trace");
  const trace = state.trace || [];
  const ratio = window.devicePixelRatio || 1;
  const width = canvas.clientWidth;
  const height = canvas.height;
  if (canvas.width !== width * ratio) {
    canvas.width = width * ratio;
    canvas.style.height = `${height}px`;
  }
  const ctx = canvas.getContext("2d");
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  ctx.clearRect(0, 0, width, height);

  if (trace.length < 2) {
    ctx.fillStyle = "#5c574e";
    ctx.font = '13px "Segoe UI", system-ui, sans-serif';
    ctx.fillText("Waiting for data…", 12, 24);
    return;
  }

  const pad = { left: 42, right: 10, top: 12, bottom: 22 };
  const values = [];
  trace.forEach((point) => {
    [point.trainer, point.pedals, point.target_watts].forEach((v) => {
      if (v != null) values.push(v);
    });
  });
  const maxValue = Math.max(50, Math.ceil(Math.max(...values) / 50) * 50 + 25);
  const t0 = trace[0].t;
  const t1 = Math.max(trace[trace.length - 1].t, t0 + 1);

  const x = (t) => pad.left + ((t - t0) / (t1 - t0)) * (width - pad.left - pad.right);
  const y = (v) => height - pad.bottom - (v / maxValue) * (height - pad.top - pad.bottom);

  ctx.strokeStyle = "#e6ded0";
  ctx.fillStyle = "#5c574e";
  ctx.font = '11px "Segoe UI", system-ui, sans-serif';
  ctx.lineWidth = 1;
  for (let step = 0; step <= 4; step++) {
    const value = (maxValue / 4) * step;
    const yy = y(value);
    ctx.beginPath();
    ctx.moveTo(pad.left, yy);
    ctx.lineTo(width - pad.right, yy);
    ctx.stroke();
    ctx.fillText(`${Math.round(value)}`, 6, yy + 3);
  }

  const line = (key, colour, dashed) => {
    ctx.beginPath();
    ctx.strokeStyle = colour;
    ctx.lineWidth = dashed ? 1.5 : 2;
    ctx.setLineDash(dashed ? [4, 4] : []);
    let started = false;
    trace.forEach((point) => {
      const value = point[key];
      if (value == null) return;
      const px = x(point.t);
      const py = y(value);
      if (started) ctx.lineTo(px, py);
      else { ctx.moveTo(px, py); started = true; }
    });
    ctx.stroke();
    ctx.setLineDash([]);
  };

  line("target_watts", "#c9c0b0", true);
  line("trainer", "#2f5d8c", false);
  line("pedals", "#9b5d2c", false);
}

window.addEventListener("resize", () => { if (state) drawTrace(); });

/* ---------------- results ---------------- */

function renderResults() {
  const card = $("results-card");
  const report = state.report;
  card.classList.toggle("hidden", !report);
  if (!report) return;

  setVerdict("verdict-consistency", report.consistency,
    report.ratio_spread == null ? "—" : `${(report.ratio_spread * 100).toFixed(1)} point spread`);
  setVerdict("verdict-offset", report.offset,
    report.mean_ratio == null ? "—" : `mean ratio ${report.mean_ratio.toFixed(3)}`);

  const fit = $("verdict-fit");
  fit.querySelector("b").textContent =
    report.slope == null
      ? "—"
      : `${report.slope.toFixed(3)}× ${report.intercept >= 0 ? "+" : "−"}${Math.abs(report.intercept).toFixed(0)} W`;

  const body = $("results-body");
  body.innerHTML = "";
  report.cells.forEach((cell) => {
    const row = document.createElement("tr");
    row.className = cell.usable ? cell.verdict : "excluded";
    const fmt = (v, digits = 0) => (v == null ? "—" : v.toFixed(digits));
    row.innerHTML = `
      <td>${cell.label}</td>
      <td class="num">${fmt(cell.trainer_mean_w)} W</td>
      <td class="num">${fmt(cell.pedal_mean_w)} W</td>
      <td class="num">${cell.ratio == null ? "—" : cell.ratio.toFixed(3)}</td>
      <td class="num">${cell.diff_pct == null ? "—" : `${cell.diff_pct >= 0 ? "+" : ""}${cell.diff_pct.toFixed(1)}%`}</td>
      <td class="num">${(cell.time_in_zone * 100).toFixed(0)}%</td>
      <td class="num">${cell.trainer_n}/${cell.pedal_n}</td>`;
    body.appendChild(row);
    if (!cell.usable && cell.reason) {
      const note = document.createElement("tr");
      note.className = "excluded";
      note.innerHTML = `<td colspan="7" class="sub">excluded — ${cell.reason}</td>`;
      body.appendChild(note);
    }
  });

  const notes = $("result-notes");
  notes.innerHTML = "";
  (report.notes || []).forEach((text) => {
    const item = document.createElement("li");
    item.textContent = text;
    notes.appendChild(item);
  });
}

function setVerdict(id, verdict, subtitle) {
  const element = $(id);
  element.className = `verdict ${verdict}`;
  element.querySelector("b").textContent = verdict;
  element.querySelectorAll(".sub")[1].textContent = subtitle;
}

connectSocket();
