"""In-process shared state bridging the HTTP handler (agent/service.py) and
the MCP tool implementations (agent/tools.py).

Both live in the same Python process (the long-running agent service), so a
simple module-level object is enough -- no IPC needed here. The EnergyPlus
plugin, which runs in a *separate* process (EnergyPlus's embedded
interpreter), talks to this service over HTTP; see plugin/eco_loop_plugin.py
and agent/service.py.
"""

from dataclasses import dataclass, field


@dataclass
class ZoneReading:
    temp_c: float
    pmv: float


@dataclass
class SimSnapshot:
    sim_time: str
    zones: dict[str, ZoneReading]
    facility_demand_w: float
    cooling_setpoint_c: float
    heating_setpoint_c: float
    carbon_intensity: float  # synthetic 0-1 scale, higher = dirtier grid


@dataclass
class PendingDecision:
    cooling_setpoint_c: float | None = None
    heating_setpoint_c: float | None = None
    reasoning: str = ""
    committed: bool = False  # True once set_hvac_setpoints tool has been called


@dataclass
class PendingPatch:
    out_path: str | None = None
    committed: bool = False  # True once patch_idf_field tool has succeeded


@dataclass
class SharedState:
    snapshot: SimSnapshot | None = None
    pending: PendingDecision = field(default_factory=PendingDecision)
    pending_patch: PendingPatch = field(default_factory=PendingPatch)

    def reset_pending(self) -> None:
        self.pending = PendingDecision()

    def reset_pending_patch(self) -> None:
        self.pending_patch = PendingPatch()


state = SharedState()
