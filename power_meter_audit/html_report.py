"""Self-contained HTML audit report with Chart.js charts."""

from __future__ import annotations

import html
import json
from pathlib import Path

from power_meter_audit.analyze import AuditResult
from power_meter_audit.report import result_to_dict


def write_html_report(result: AuditResult, path: Path) -> None:
    payload = result_to_dict(result)
    data_json = json.dumps(payload, indent=None)
    # Escape for embedding inside <script> as JSON assignment.
    data_json_safe = data_json.replace("<", "\\u003c")

    n_suspect = len(payload["suspects"])
    n_watch = len(payload["watch"])
    n_quality = len(payload["quality_findings"])

    ride_rows = []
    for i, r in enumerate(payload["rides"], start=1):
        flags = ",".join(r.get("quality", {}).get("flags", []) or []) if r.get("quality") else "—"
        if not flags:
            flags = "—"
        ride_rows.append(
            "<tr>"
            f"<td>{i}</td>"
            f"<td>{html.escape(r['name'])}</td>"
            f"<td>{html.escape((r['start_time'] or '—')[:10])}</td>"
            f"<td>{html.escape(r['context'])}</td>"
            f"<td>{html.escape(r['device_label'] or r['device_id'])}</td>"
            f"<td>{r['mean_ratio']:.2f}</td>"
            f"<td>{r['mean_delta_w']:+.0f}</td>"
            f"<td>{r['z_score']:.1f}</td>"
            f"<td class='sev-{html.escape(r['severity'])}'>{html.escape(r['severity'])}</td>"
            f"<td>{html.escape(flags)}</td>"
            "</tr>"
        )

    quality_rows = []
    for f in payload["quality_findings"]:
        quality_rows.append(
            "<tr>"
            f"<td class='sev-{html.escape(f['severity'])}'>{html.escape(f['severity'])}</td>"
            f"<td>{html.escape(f['activity_name'])}</td>"
            f"<td>{html.escape(f['kind'])}</td>"
            f"<td>{html.escape(f['description'])}</td>"
            "</tr>"
        )

    device_rows = []
    for g in payload["devices"]:
        device_rows.append(
            "<tr>"
            f"<td>{html.escape(g['label'])}</td>"
            f"<td><code>{html.escape(g['device_id'])}</code></td>"
            f"<td>{g['n_rides']}</td>"
            f"<td>{g['median_ratio']:.2f}</td>"
            f"<td>{g['mean_ratio']:.2f}</td>"
            "</tr>"
        )

    doc = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Power meter audit</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>
  :root {{
    --bg: #f6f4ef;
    --ink: #1c1a16;
    --muted: #5c574e;
    --line: #d9d2c5;
    --card: #fffdf8;
    --suspect: #9b2c2c;
    --watch: #9a6700;
    --ok: #1a7f37;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    font-family: "Segoe UI", system-ui, sans-serif;
    color: var(--ink);
    background: linear-gradient(180deg, #efe8da 0%, var(--bg) 240px);
    line-height: 1.45;
  }}
  main {{ max-width: 1100px; margin: 0 auto; padding: 2rem 1.25rem 4rem; }}
  h1 {{ font-size: 1.75rem; margin: 0 0 0.35rem; letter-spacing: -0.02em; }}
  h2 {{ font-size: 1.15rem; margin: 2rem 0 0.75rem; }}
  .sub {{ color: var(--muted); margin-bottom: 1.25rem; }}
  .stats {{
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(120px, 1fr));
    gap: 0.75rem;
    margin-bottom: 1.5rem;
  }}
  .stat {{
    background: var(--card);
    border: 1px solid var(--line);
    border-radius: 10px;
    padding: 0.85rem 1rem;
  }}
  .stat b {{ display: block; font-size: 1.35rem; }}
  .stat span {{ color: var(--muted); font-size: 0.85rem; }}
  .charts {{
    display: grid;
    grid-template-columns: 1fr;
    gap: 1rem;
  }}
  @media (min-width: 900px) {{
    .charts-2 {{ grid-template-columns: 1fr 1fr; }}
  }}
  .chart-card {{
    background: var(--card);
    border: 1px solid var(--line);
    border-radius: 12px;
    padding: 1rem;
  }}
  .chart-card h3 {{ margin: 0 0 0.75rem; font-size: 0.95rem; }}
  canvas {{ max-height: 320px; }}
  table {{
    width: 100%;
    border-collapse: collapse;
    background: var(--card);
    border: 1px solid var(--line);
    border-radius: 12px;
    overflow: hidden;
    font-size: 0.9rem;
  }}
  th, td {{
    text-align: left;
    padding: 0.55rem 0.65rem;
    border-bottom: 1px solid var(--line);
    vertical-align: top;
  }}
  th {{ background: #f0ebe1; font-weight: 600; }}
  tr:last-child td {{ border-bottom: 0; }}
  .sev-suspect {{ color: var(--suspect); font-weight: 600; }}
  .sev-watch {{ color: var(--watch); font-weight: 600; }}
  .sev-ok, .sev-info {{ color: var(--ok); }}
  .sev-severe {{ color: var(--suspect); font-weight: 600; }}
  code {{ font-size: 0.8rem; }}
  .notes {{ color: var(--muted); font-size: 0.9rem; }}
</style>
</head>
<body>
<main>
  <h1>Power meter audit</h1>
  <p class="sub">Power vs heart-rate consistency across cycling FIT activities</p>
  <div class="stats">
    <div class="stat"><b>{payload["activity_count"]}</b><span>loaded</span></div>
    <div class="stat"><b>{payload["scored_count"]}</b><span>scored</span></div>
    <div class="stat"><b>{n_suspect}</b><span>suspect</span></div>
    <div class="stat"><b>{n_watch}</b><span>watch</span></div>
    <div class="stat"><b>{n_quality}</b><span>quality findings</span></div>
    <div class="stat"><b>{len(payload["devices"])}</b><span>devices</span></div>
  </div>

  <div class="charts">
    <div class="chart-card">
      <h3>Power vs HR — baseline and suspect rides</h3>
      <canvas id="chartProfile"></canvas>
    </div>
    <div class="charts charts-2">
      <div class="chart-card">
        <h3>Ride mean ratio over time</h3>
        <canvas id="chartTimeline"></canvas>
      </div>
      <div class="chart-card">
        <h3>Mean ratio by device</h3>
        <canvas id="chartDevices"></canvas>
      </div>
    </div>
  </div>

  <h2>All rides</h2>
  <table>
    <thead>
      <tr>
        <th>#</th><th>Name</th><th>Date</th><th>Ctx</th><th>Device</th>
        <th>Ratio</th><th>ΔW</th><th>z</th><th>Severity</th><th>Flags</th>
      </tr>
    </thead>
    <tbody>
      {"".join(ride_rows) or "<tr><td colspan='10'>No scored rides</td></tr>"}
    </tbody>
  </table>

  <h2>Devices</h2>
  <table>
    <thead><tr><th>Label</th><th>ID</th><th>Rides</th><th>Median ratio</th><th>Mean ratio</th></tr></thead>
    <tbody>
      {"".join(device_rows) or "<tr><td colspan='5'>No devices</td></tr>"}
    </tbody>
  </table>

  <h2>Signal quality</h2>
  <table>
    <thead><tr><th>Severity</th><th>Activity</th><th>Kind</th><th>Description</th></tr></thead>
    <tbody>
      {"".join(quality_rows) or "<tr><td colspan='4'>No quality issues flagged</td></tr>"}
    </tbody>
  </table>

  <h2>Notes</h2>
  <ul class="notes">
    {"".join(f"<li>{html.escape(n)}</li>" for n in payload["notes"] if not n.startswith("skipped")) or "<li>None</li>"}
  </ul>
</main>
<script>
const DATA = {data_json_safe};

function buildProfileChart() {{
  const baseline = DATA.baseline || {{}};
  const hrs = Object.keys(baseline).map(Number).sort((a,b)=>a-b);
  const baselineY = hrs.map(h => baseline[String(h)].median_watts);
  const datasets = [{{
    label: "Baseline",
    data: hrs.map((h,i) => ({{x: h, y: baselineY[i]}})),
    borderColor: "#1c1a16",
    backgroundColor: "#1c1a16",
    tension: 0.2,
    pointRadius: 3,
  }}];
  const suspects = new Set(DATA.suspects || []);
  const palette = ["#9b2c2c", "#9a6700", "#0550ae", "#8250df", "#1a7f37"];
  let pi = 0;
  for (const ride of DATA.rides || []) {{
    if (!suspects.has(ride.name)) continue;
    const bins = ride.profile_bins || {{}};
    const xs = Object.keys(bins).map(Number).sort((a,b)=>a-b);
    datasets.push({{
      label: ride.name,
      data: xs.map(h => ({{x: h, y: bins[String(h)]}})),
      borderColor: palette[pi % palette.length],
      backgroundColor: palette[pi % palette.length],
      tension: 0.2,
      pointRadius: 2,
      borderDash: [4, 3],
    }});
    pi++;
  }}
  new Chart(document.getElementById("chartProfile"), {{
    type: "line",
    data: {{ datasets }},
    options: {{
      parsing: false,
      scales: {{
        x: {{ type: "linear", title: {{ display: true, text: "HR (bpm)" }} }},
        y: {{ title: {{ display: true, text: "Power (W)" }} }},
      }},
      plugins: {{ legend: {{ position: "bottom" }} }},
    }},
  }});
}}

function buildTimelineChart() {{
  const rides = (DATA.rides || []).slice().filter(r => r.start_time).sort((a,b) => a.start_time.localeCompare(b.start_time));
  const labels = rides.map(r => (r.start_time || "").slice(0, 10));
  const ratios = rides.map(r => r.mean_ratio);
  const colors = rides.map(r => r.severity === "suspect" ? "#9b2c2c" : (r.severity === "watch" ? "#9a6700" : "#1a7f37"));
  new Chart(document.getElementById("chartTimeline"), {{
    type: "line",
    data: {{
      labels,
      datasets: [{{
        label: "Mean ratio",
        data: ratios,
        borderColor: "#5c574e",
        pointBackgroundColor: colors,
        pointBorderColor: colors,
        pointRadius: 4,
        tension: 0.15,
      }}],
    }},
    options: {{
      scales: {{
        y: {{ title: {{ display: true, text: "Power / expected" }}, suggestedMin: 0.6, suggestedMax: 1.4 }},
      }},
      plugins: {{
        annotation: undefined,
        legend: {{ display: false }},
      }},
    }},
  }});
}}

function buildDeviceChart() {{
  const devices = DATA.devices || [];
  new Chart(document.getElementById("chartDevices"), {{
    type: "bar",
    data: {{
      labels: devices.map(d => d.label || d.device_id),
      datasets: [{{
        label: "Mean ratio",
        data: devices.map(d => d.mean_ratio),
        backgroundColor: "#3d6b8c",
      }}],
    }},
    options: {{
      scales: {{
        y: {{ title: {{ display: true, text: "Mean ratio" }}, suggestedMin: 0.6, suggestedMax: 1.4 }},
      }},
      plugins: {{ legend: {{ display: false }} }},
    }},
  }});
}}

buildProfileChart();
buildTimelineChart();
buildDeviceChart();
</script>
</body>
</html>
"""
    path.write_text(doc, encoding="utf-8")
