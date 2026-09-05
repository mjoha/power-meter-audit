"""Load power and heart-rate samples from Garmin/Strava-style FIT files."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path

import fitparse


class SkipReason(str, Enum):
    PARSE_ERROR = "parse_error"
    TOO_FEW_PAIRS = "too_few_power_hr_pairs"
    NON_CYCLING = "non_cycling_sport"
    OK = "ok"


@dataclass
class LoadStats:
    path: Path
    total_records: int = 0
    paired_power_hr: int = 0
    records_with_power: int = 0
    records_with_hr: int = 0
    skip_reason: SkipReason = SkipReason.OK


@dataclass
class Activity:
    path: Path
    name: str
    start_time: datetime | None
    sport: str | None
    power: list[float]
    hr: list[float]
    timestamps: list[datetime | None] = field(default_factory=list)
    cadence: list[float | None] = field(default_factory=list)
    speed: list[float | None] = field(default_factory=list)
    # Parallel full-record series for quality checks (includes unpaired samples).
    raw_power: list[float | None] = field(default_factory=list)
    raw_hr: list[float | None] = field(default_factory=list)
    raw_timestamps: list[datetime | None] = field(default_factory=list)
    sub_sport: str | None = None
    indoor: bool = False
    has_gps: bool = False
    power_device_id: str = "unknown"
    power_manufacturer: str | None = None
    power_product: str | None = None
    power_device_label: str = "unknown"
    load_stats: LoadStats | None = None

    @property
    def n_samples(self) -> int:
        return len(self.power)

    @property
    def duration_s(self) -> float:
        valid = [t for t in self.timestamps if t is not None]
        if len(valid) < 2:
            return float(self.n_samples)
        return max(0.0, (valid[-1] - valid[0]).total_seconds())

    @property
    def mean_power(self) -> float | None:
        return sum(self.power) / len(self.power) if self.power else None

    @property
    def mean_hr(self) -> float | None:
        return sum(self.hr) / len(self.hr) if self.hr else None

    @property
    def context_label(self) -> str:
        return "indoor" if self.indoor else "outdoor"


def _field_value(message: fitparse.FitDataMessage, name: str):
    return message.get_value(name)


def _as_float(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_str(value) -> str | None:
    if value is None:
        return None
    return str(value)


CYCLING_SPORTS = {
    "cycling",
    "bike",
    "biking",
    "mountain_biking",
    "gravel_cycling",
    "virtual_activity",
    "indoor_cycling",
    "e_biking",
}

INDOOR_HINTS = {
    "virtual_activity",
    "indoor_cycling",
    "indoor_rowing",
    "indoor",
}

NON_CYCLING = {"running", "walking", "hiking", "swimming", "rowing"}


def _extract_power_device(fitfile: fitparse.FitFile) -> tuple[str, str | None, str | None, str]:
    """Best-effort power meter identity from device_info / file_id."""
    power_candidates: list[tuple[str, str | None, str | None, str]] = []
    fallback: tuple[str, str | None, str | None, str] | None = None

    for msg in fitfile.get_messages("device_info"):
        manufacturer = _as_str(_field_value(msg, "manufacturer"))
        product = _as_str(_field_value(msg, "product")) or _as_str(_field_value(msg, "garmin_product"))
        serial = _field_value(msg, "serial_number")
        device_type = _as_str(_field_value(msg, "device_type"))
        ant_device_type = _field_value(msg, "ant_device_type")
        source_type = _as_str(_field_value(msg, "source_type"))

        serial_s = str(int(serial)) if isinstance(serial, (int, float)) else (_as_str(serial) or "unknown")
        label_parts = [p for p in (manufacturer, product) if p]
        label = " ".join(label_parts) if label_parts else serial_s
        device_id = f"{manufacturer or 'unk'}:{product or 'unk'}:{serial_s}"

        looks_power = False
        if device_type and "power" in device_type.lower():
            looks_power = True
        if ant_device_type is not None:
            try:
                # ANT+ bike power device type is typically 11.
                if int(ant_device_type) == 11:
                    looks_power = True
            except (TypeError, ValueError):
                pass
        if product and any(k in product.lower() for k in ("power", "vector", "stages", "quarq", "assioma", "favero", "powertap", "shimano")):
            looks_power = True

        entry = (device_id, manufacturer, product, label)
        if looks_power:
            power_candidates.append(entry)
        elif fallback is None and source_type not in {None, "local"}:
            fallback = entry
        elif fallback is None and manufacturer:
            fallback = entry

    if power_candidates:
        return power_candidates[0]
    if fallback:
        return fallback

    for msg in fitfile.get_messages("file_id"):
        manufacturer = _as_str(_field_value(msg, "manufacturer"))
        product = _as_str(_field_value(msg, "product")) or _as_str(_field_value(msg, "garmin_product"))
        serial = _field_value(msg, "serial_number")
        serial_s = str(int(serial)) if isinstance(serial, (int, float)) else (_as_str(serial) or "unknown")
        label_parts = [p for p in (manufacturer, product) if p]
        label = " ".join(label_parts) if label_parts else "unknown"
        device_id = f"{manufacturer or 'unk'}:{product or 'unk'}:{serial_s}"
        return device_id, manufacturer, product, label

    return "unknown", None, None, "unknown"


def load_fit(path: Path) -> tuple[Activity | None, LoadStats]:
    """Parse a FIT file. Returns (activity_or_none, load_stats)."""
    stats = LoadStats(path=path)
    try:
        fitfile = fitparse.FitFile(str(path))
    except Exception:
        stats.skip_reason = SkipReason.PARSE_ERROR
        return None, stats

    sport: str | None = None
    sub_sport: str | None = None
    start_time: datetime | None = None
    name = path.stem

    for msg in fitfile.get_messages("session"):
        sport = _as_str(_field_value(msg, "sport")) or sport
        sub_sport = _as_str(_field_value(msg, "sub_sport")) or sub_sport
        start_time = _field_value(msg, "start_time") or start_time

    for msg in fitfile.get_messages("activity"):
        start_time = start_time or _field_value(msg, "timestamp")

    device_id, manufacturer, product, device_label = _extract_power_device(fitfile)

    power: list[float] = []
    hr: list[float] = []
    timestamps: list[datetime | None] = []
    cadence: list[float | None] = []
    speed: list[float | None] = []
    raw_power: list[float | None] = []
    raw_hr: list[float | None] = []
    raw_timestamps: list[datetime | None] = []
    has_gps = False
    gps_fixes = 0

    for msg in fitfile.get_messages("record"):
        stats.total_records += 1
        p = _as_float(_field_value(msg, "power"))
        h = _as_float(_field_value(msg, "heart_rate"))
        t = _field_value(msg, "timestamp")
        c = _as_float(_field_value(msg, "cadence"))
        s = _as_float(_field_value(msg, "speed"))
        lat = _field_value(msg, "position_lat")
        lon = _field_value(msg, "position_long")

        if p is not None:
            stats.records_with_power += 1
        if h is not None:
            stats.records_with_hr += 1
        if lat is not None and lon is not None:
            gps_fixes += 1

        ts = t if isinstance(t, datetime) else None
        raw_power.append(p)
        raw_hr.append(h)
        raw_timestamps.append(ts)

        if p is None or h is None:
            continue
        if p <= 0 or h < 40 or h > 220:
            continue
        stats.paired_power_hr += 1
        power.append(p)
        hr.append(h)
        timestamps.append(ts)
        cadence.append(c)
        speed.append(s)

    has_gps = gps_fixes >= max(10, int(0.05 * max(1, stats.total_records)))

    sport_l = (sport or "").lower()
    sub_l = (sub_sport or "").lower()
    indoor = (
        sport_l in INDOOR_HINTS
        or sub_l in INDOOR_HINTS
        or "indoor" in sub_l
        or "virtual" in sport_l
        or "virtual" in sub_l
        or (not has_gps and stats.paired_power_hr >= 30)
    )

    if stats.paired_power_hr < 30:
        stats.skip_reason = SkipReason.TOO_FEW_PAIRS
        return None, stats

    if sport_l in NON_CYCLING:
        stats.skip_reason = SkipReason.NON_CYCLING
        return None, stats

    activity = Activity(
        path=path,
        name=name,
        start_time=start_time if isinstance(start_time, datetime) else None,
        sport=sport,
        power=power,
        hr=hr,
        timestamps=timestamps,
        cadence=cadence,
        speed=speed,
        raw_power=raw_power,
        raw_hr=raw_hr,
        raw_timestamps=raw_timestamps,
        sub_sport=sub_sport,
        indoor=indoor,
        has_gps=has_gps,
        power_device_id=device_id,
        power_manufacturer=manufacturer,
        power_product=product,
        power_device_label=device_label,
        load_stats=stats,
    )
    return activity, stats


def discover_fit_files(directory: Path, recursive: bool = True) -> list[Path]:
    if recursive:
        files = set(directory.glob("**/*.fit")) | set(directory.glob("**/*.FIT"))
    else:
        files = set(directory.glob("*.fit")) | set(directory.glob("*.FIT"))
    return sorted(files)


def load_directory(
    directory: Path, recursive: bool = True
) -> tuple[list[Activity], list[str], list[LoadStats]]:
    """Load all usable activities. Returns (activities, notes, all_load_stats)."""
    activities: list[Activity] = []
    notes: list[str] = []
    all_stats: list[LoadStats] = []
    for path in discover_fit_files(directory, recursive=recursive):
        activity, stats = load_fit(path)
        all_stats.append(stats)
        if activity is None:
            notes.append(f"skipped ({stats.skip_reason.value}): {path.name}")
            continue
        activities.append(activity)
    return activities, notes, all_stats
