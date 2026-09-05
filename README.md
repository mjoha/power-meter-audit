# Power Meter Audit

Analyze a directory of cycling `.fit` files to find activities whose power readings look inconsistent relative to heart rate — useful for spotting power-meter calibration or hardware issues.

## Setup

```powershell
cd ~\Projects\power-meter-audit
python -m venv .venv
.\.venv\Scripts\activate
pip install -e .
pip install pytest   # optional, for tests
```

## Usage

```powershell
python -m power_meter_audit path\to\fit\files
python -m power_meter_audit path\to\fit\files --json report.json --html report.html
python -m power_meter_audit path\to\fit\files --verbose --top 100
```

### Useful options

| Flag | Default | Meaning |
|------|---------|---------|
| `--hr-bin` | `5` | Heart-rate bin width (bpm) |
| `--min-bin-samples` | `20` | Min samples in a bin within one ride |
| `--min-overlap-bins` | `3` | Min shared HR bins vs baseline |
| `--min-ratio-shift` | `0.15` | Min \|observed/expected − 1\| for watch/suspect |
| `--z-threshold` | `1.5` | Score z-score for suspect severity |
| `--no-steady-state` | off | Disable cadence 60–110 rpm filtering |
| `--json FILE` | — | Full machine-readable report |
| `--html FILE` | — | Self-contained HTML with charts |
| `--verbose` | off | Print every HR-bin for each ride |
| `--no-recursive` | off | Only the top-level directory |

Exit code: `0` if any **suspect** ride or **severe** quality finding; `1` if clean; `2` on bad input.

## What you get

**CLI**

- Summary counts (loaded / scored / suspect / watch / quality / devices)
- Ranking table of **every** scored ride (ratio, ΔW, z, severity, quality flags)
- Suspect detail with per-HR-bin examples
- Device grouping and cross-device watts at shared HR
- Signal-quality section
- Global baseline curve (HR → typical W)

**JSON** — full `rides[]`, `baseline`, `devices[]`, `device_comparisons[]`, `quality_findings[]`, `params`, `notes`.

**HTML** — charts for baseline vs suspects, ratio over time, and mean ratio by device, plus the same tables.

## How to interpret

- A **stable scale offset** across many HR bins (e.g. always ~1.5×) is a stronger power-meter signal than one hard interval day.
- Fitness / freshness changes HR for a given wattage; look for the inverse pattern (same HR, different watts) plus device/quality context.
- Indoor vs outdoor are matched for baselines when enough rides exist in the same context.
- Quality flags (dropouts, stuck power, spikes, zero bursts) often explain weird ratios without implying a calibration offset.

## Tests

```powershell
python -m pytest -q
```
