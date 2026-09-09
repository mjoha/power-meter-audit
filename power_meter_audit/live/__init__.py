"""Live dual-power-source comparison: drive an ERG protocol, record both meters."""

from power_meter_audit.live.analysis import (
    CellResult,
    ComparisonReport,
    Thresholds,
    Verdict,
    analyse_session,
    format_comparison_report,
)
from power_meter_audit.live.harness import build_runner, run_simulated_session
from power_meter_audit.live.protocol import (
    PRESETS,
    CadenceSegment,
    Protocol,
    Step,
    TimelineSegment,
    quick_protocol,
    standard_protocol,
)
from power_meter_audit.live.runner import (
    ControlWarning,
    LiveStatus,
    SegmentFinished,
    SegmentStarted,
    SessionFinished,
    SessionRunner,
    SessionStarted,
)
from power_meter_audit.live.session import SessionLog
from power_meter_audit.live.simulator import MeterModel, RiderModel, SimulatedRig
from power_meter_audit.live.sources import (
    PEDALS,
    TRAINER,
    PowerSource,
    RealClock,
    Sample,
    TrainerSource,
    VirtualClock,
)

__all__ = [
    "CellResult",
    "ComparisonReport",
    "Thresholds",
    "Verdict",
    "analyse_session",
    "format_comparison_report",
    "build_runner",
    "run_simulated_session",
    "PRESETS",
    "CadenceSegment",
    "Protocol",
    "Step",
    "TimelineSegment",
    "quick_protocol",
    "standard_protocol",
    "ControlWarning",
    "LiveStatus",
    "SegmentFinished",
    "SegmentStarted",
    "SessionFinished",
    "SessionRunner",
    "SessionStarted",
    "SessionLog",
    "MeterModel",
    "RiderModel",
    "SimulatedRig",
    "PEDALS",
    "TRAINER",
    "PowerSource",
    "RealClock",
    "Sample",
    "TrainerSource",
    "VirtualClock",
]
