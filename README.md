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

# Dual power source comparison

Heart rate is a noisy bridge between two power meters. `power-meter-compare` removes it: it drives a
trainer through an ERG ladder while recording a second power meter at the same time, on one clock.

```powershell
power-meter-ui                                    # browser UI (recommended)
power-meter-compare --scan                        # find BLE devices
power-meter-compare --trainer-address AA:BB:CC:DD:EE:FF --out runs\test1
power-meter-compare --simulate --sim-pedal-torque-gain 0.003   # no hardware needed
power-meter-compare --analyse runs\test1.json     # re-analyse a saved session
```

## Browser UI

`power-meter-ui` serves a local page with three steps: connect and verify both sources, edit the
protocol table, then run it. Everything runs in the Python process; the browser is only the view.

The run screen exists mainly for the cadence guide. Samples taken outside the cadence band are
excluded from a cell rather than averaged in, so you need to see whether you are inside it *now* —
the page shows the target, the current cadence against a banded meter, a spin-up or slow-down cue,
and both live power traces so a dead link is obvious before a session is wasted.

Pick "Simulated rig" to rehearse the whole flow without a bike. It can inject a known fault (scale
error, torque-proportional gain, left-leg fraction) and accelerate time, so a 34-minute protocol
plays through in well under a minute.

## The protocol

Each ERG step is split into two cadence halves. At a fixed power, 70 rpm loads the cranks about 29%
harder than 90 rpm, so one step yields two crank torques and the grid separates a torque-dependent
error from a power-dependent one. The runner guides you to the target cadence, discards a settling
window after every power *and* cadence change, and only measures samples where you were actually in
the band.

| Preset | Ladder | Per cadence | Warm-up |
|--------|--------|-------------|---------|
| `standard` | 150–350 W in five steps, plus a repeat of the first | 2 min | 10 min |
| `quick` | 150 / 250 / 350 W | 90 s | 5 min |

Zero-offset the pedals and run a trainer spindown at the end of the warm-up, before the first step.
The repeated opening step measures drift across the session rather than calibration.

## How to interpret the result

The primary verdict is **consistency, not absolute offset**. A left-only pedal read against a
direct-drive trainer *should* sit a few percent high, because of drivetrain loss plus whatever the
rider's left/right imbalance is — and neither of those varies with power or cadence. So:

- A ratio that stays **flat** across the grid means the two sources are realistically aligned,
  whatever its value. This verdict needs no assumptions.
- A ratio that **drifts** across the grid cannot be explained by imbalance or drivetrain loss, and
  points at a real fault: a strain-gauge nonlinearity, or progressive slip in the trainer.
- `ratio by cadence` isolates torque: if the two cadence halves disagree at matched power, the error
  tracks crank torque rather than power.
- `cadence bias` matters because pedal power is derived from angular velocity, so a cadence error
  feeds straight into power.

The secondary offset light compares the mean ratio against an expected band (default 1.02–1.09) and
is a convention, not a measurement — without a single-leg block you can measure the disagreement but
not attribute it.

## Hardware

```powershell
.\.venv\Scripts\activate   # the venv matters here, see below
pip install -e .[app]      # or .[hardware] / .[ui] separately
```

Install into the virtual environment, not a system-wide Python. `fitparse` ships no wheel, so pip
falls back to a legacy `setup.py install` that writes a `fitdump` script next to the interpreter;
against a system Python such as `C:\Python310` that needs admin rights and fails with
`could not create '...\Scripts\fitdump': Permission denied`, taking the whole install down with it.
A venv you own has no such problem.

The trainer is driven over Bluetooth FTMS. Control (ERG target) always goes over the
Fitness Machine Control Point; readings come from Indoor Bike Data when the trainer
serves it, and from the Cycling Power Service otherwise. Wahoo exposes both, and a
firmware missing Indoor Bike Data used to abort the handshake before control was
requested, so every target write was ignored and the trainer freewheeled. Close Zwift
first — BLE trainer control is exclusive.

The pedals are read over ANT+ by default, which supports unlimited concurrent listeners
so your head unit can keep recording the same ride. `--pedals-ble` is available as a
fallback but consumes one of the pedals' few BLE connection slots.

### ANT+ on Windows

`openant` reaches the USB stick through pyusb, which needs two things Windows does not provide by
default. Both failures are silent about their cause, so they are translated into instructions in the
app itself.

1. **libusb.** Absent, pyusb fails with `No backend available`. The `[hardware]` extra pulls
   `libusb-package` on Windows to supply it, and the DLL is registered with pyusb at startup —
   bundled inside site-packages, it is somewhere pyusb would never look on its own.
2. **A libusb-compatible driver bound to the stick.** Garmin's own ANT USB driver will not let
   libusb claim the device, so pyusb finds no stick and `openant` raises `DriverNotFound`. Use
   [Zadig](https://zadig.akeo.ie) to replace the driver with **WinUSB**: *Options → List All
   Devices*, select the ANT USB stick, choose WinUSB, *Replace Driver*. Garmin Express and any ANT
   Agent must not be running, as they hold the stick open.

Prefer BLE for the pedals if you would rather not rebind the driver: pick "Pedals over BLE instead"
in the UI, or pass `--pedals-ble <address>`. Cadence is derived from the crank counters in that mode
because the BLE power profile carries no cadence field.

# Tests

```powershell
python -m pytest -q
```
