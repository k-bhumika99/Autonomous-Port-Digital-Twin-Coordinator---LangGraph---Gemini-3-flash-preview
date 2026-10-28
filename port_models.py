"""Typed data models for the virtual port.

These Pydantic models are the single source of truth for what a "port
configuration", a "disruption scenario" and a "candidate plan" look like.
Everything that crosses a module boundary (routes -> simulation -> agents ->
database) uses these shapes, so a mistake is caught early with a readable
message instead of a mysterious crash deep inside the simulator.

UNITS (used everywhere in the project)
    time ............ minutes since simulation start (t = 0)
    containers ...... whole container units (a scaled, synthetic TEU-like unit)
    crane rate ...... containers handled per hour
    truck capacity .. containers collected per trip
"""
from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError, field_validator

ID_PATTERN = re.compile(r"^[A-Za-z0-9_\-]{1,16}$")

CraneStatus = Literal["operational", "maintenance"]
DisruptionType = Literal["crane_breakdown", "ship_delay", "yard_congestion", "truck_surge"]
ScenarioKind = Literal[
    "crane_breakdown", "ship_delay", "yard_congestion", "truck_surge", "multiple"
]
CranePolicy = Literal["fixed_home", "assigned", "pooled"]


def _as_id_list(value: Any) -> list[str]:
    """Accept ['B1','B2'] or 'B1, B2' (from an HTML text box) and return a clean list."""
    if value is None or value == "":
        return []
    if isinstance(value, str):
        value = [part for part in re.split(r"[,\s;]+", value) if part]
    return [str(v).strip() for v in value if str(v).strip()]


def _check_id(value: str, what: str) -> str:
    value = str(value).strip()
    if not ID_PATTERN.match(value):
        raise ValueError(f"{what} must be 1-16 letters, digits, '-' or '_' (got {value!r})")
    return value


# --------------------------------------------------------------------------
# Port resources
# --------------------------------------------------------------------------
class Berth(BaseModel):
    id: str
    name: str = ""

    @field_validator("id")
    @classmethod
    def _id(cls, v):
        return _check_id(v, "Berth ID")


class Ship(BaseModel):
    id: str
    name: str = ""
    arrival_min: int = Field(ge=0, le=100000, description="minutes after simulation start")
    containers: int = Field(gt=0, le=100000, description="containers to unload")
    priority: int = Field(default=2, ge=1, le=3, description="1 = highest, 3 = lowest")
    compatible_berths: list[str] = Field(default_factory=list, description="empty = any berth")

    @field_validator("id")
    @classmethod
    def _id(cls, v):
        return _check_id(v, "Ship ID")

    @field_validator("compatible_berths", mode="before")
    @classmethod
    def _compat(cls, v):
        return _as_id_list(v)


class Crane(BaseModel):
    id: str
    status: CraneStatus = "operational"
    rate_per_hour: float = Field(gt=0, le=500, description="containers handled per hour")
    compatible_berths: list[str] = Field(default_factory=list, description="empty = any berth")

    @field_validator("id")
    @classmethod
    def _id(cls, v):
        return _check_id(v, "Crane ID")

    @field_validator("compatible_berths", mode="before")
    @classmethod
    def _compat(cls, v):
        return _as_id_list(v)


class Yard(BaseModel):
    capacity: int = Field(gt=0, le=1000000, description="storage spaces")
    initial_occupied: int = Field(default=0, ge=0)
    zones: int = Field(default=4, ge=1, le=8)


class Truck(BaseModel):
    id: str
    arrival_min: int = Field(ge=0, le=100000, description="first arrival at the gate")
    capacity: int = Field(gt=0, le=1000, description="containers collected per trip")
    trips: int = Field(default=1, ge=1, le=50, description="number of collection trips")
    round_trip_min: int = Field(default=75, ge=10, le=1440, description="minutes between trips")
    available: bool = True

    @field_validator("id")
    @classmethod
    def _id(cls, v):
        return _check_id(v, "Truck ID")


class PortConfig(BaseModel):
    name: str = "My Port"
    gates: int = Field(default=2, ge=1, le=20, description="truck loading lanes")
    berths: list[Berth]
    ships: list[Ship]
    cranes: list[Crane]
    yard: Yard
    trucks: list[Truck]

    # ---- convenience lookups ------------------------------------------------
    def ship(self, ship_id: str) -> Ship | None:
        return next((s for s in self.ships if s.id == ship_id), None)

    def crane(self, crane_id: str) -> Crane | None:
        return next((c for c in self.cranes if c.id == crane_id), None)

    def berth_ids(self) -> list[str]:
        return [b.id for b in self.berths]

    def allowed_berths(self, ship: Ship) -> list[str]:
        """Berths a ship may use (empty compatibility list means 'any')."""
        return list(ship.compatible_berths) or self.berth_ids()

    def crane_berths(self, crane: Crane) -> list[str]:
        return list(crane.compatible_berths) or self.berth_ids()

    def home_berth(self, crane: Crane) -> str:
        """The crane's 'home' berth in the ORIGINAL schedule = first compatible berth."""
        return self.crane_berths(crane)[0]


# --------------------------------------------------------------------------
# Disruptions / scenarios
# --------------------------------------------------------------------------
class Disruption(BaseModel):
    type: DisruptionType
    # crane breakdown
    crane_id: str | None = None
    start_min: int | None = Field(default=None, ge=0, le=100000)
    duration_min: int | None = Field(default=None, gt=0, le=100000)
    # delayed ship
    ship_id: str | None = None
    delay_min: int | None = Field(default=None, gt=0, le=100000)
    # yard congestion
    target_occupancy_pct: float | None = Field(default=None, ge=0, le=100)
    extra_containers: int = Field(default=0, ge=0, le=1000000)
    # truck surge
    extra_trucks: int | None = Field(default=None, gt=0, le=200)
    window_start_min: int | None = Field(default=None, ge=0, le=100000)
    window_end_min: int | None = Field(default=None, ge=0, le=100000)

    def label(self) -> str:
        if self.type == "crane_breakdown":
            return (f"Crane {self.crane_id} breakdown at t={self.start_min} min "
                    f"for {self.duration_min} min")
        if self.type == "ship_delay":
            return f"Ship {self.ship_id} delayed by {self.delay_min} min"
        if self.type == "yard_congestion":
            parts = []
            if self.target_occupancy_pct is not None:
                parts.append(f"yard pre-filled to {self.target_occupancy_pct:g}%")
            if self.extra_containers:
                parts.append(f"+{self.extra_containers} containers already in yard")
            return "Yard congestion: " + (", ".join(parts) or "no change")
        return (f"Truck surge: +{self.extra_trucks} trucks arriving between "
                f"t={self.window_start_min} and t={self.window_end_min} min")


class Scenario(BaseModel):
    name: str = "Scenario"
    kind: ScenarioKind = "crane_breakdown"
    disruptions: list[Disruption] = Field(default_factory=list)

    def labels(self) -> list[str]:
        return [d.label() for d in self.disruptions]


# --------------------------------------------------------------------------
# Candidate plans
# --------------------------------------------------------------------------
class CandidatePlan(BaseModel):
    """A set of operational instructions the Digital Twin can simulate.

    An EMPTY plan with crane_policy='fixed_home' is the ORIGINAL schedule (Plan A).
    """

    label: str = "A"
    name: str = "Original schedule"
    description: str = ""
    crane_policy: CranePolicy = "fixed_home"
    crane_assignment: dict[str, list[str]] = Field(default_factory=dict)  # ship -> preferred cranes
    berth_assignment: dict[str, str] = Field(default_factory=dict)        # ship -> berth
    ship_hold_min: dict[str, int] = Field(default_factory=dict)           # ship -> hold before berth request
    truck_shift_min: dict[str, int] = Field(default_factory=dict)         # truck -> later first arrival
    rationale: list[str] = Field(default_factory=list)
    solver: dict[str, Any] = Field(default_factory=dict)                  # OR-Tools status info

    def change_count(self) -> int:
        """How many instructions deviate from the original schedule (used in the objective)."""
        return (
            (1 if self.crane_policy != "fixed_home" else 0)
            + len(self.crane_assignment)
            + len(self.berth_assignment)
            + sum(1 for v in self.ship_hold_min.values() if v > 0)
            + sum(1 for v in self.truck_shift_min.values() if v > 0)
        )


# --------------------------------------------------------------------------
# Validation helpers
# --------------------------------------------------------------------------
def _fmt_pydantic_errors(exc: ValidationError) -> list[str]:
    out = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err["loc"])
        out.append(f"{loc}: {err['msg']}")
    return out


def check_config(config: PortConfig) -> tuple[list[str], list[str]]:
    """Cross-field consistency checks. Returns (errors, warnings)."""
    errors: list[str] = []
    warnings: list[str] = []

    def dupes(items, what):
        seen = set()
        for it in items:
            if it in seen:
                errors.append(f"Duplicate {what} ID: {it}")
            seen.add(it)

    dupes([b.id for b in config.berths], "berth")
    dupes([s.id for s in config.ships], "ship")
    dupes([c.id for c in config.cranes], "crane")
    dupes([t.id for t in config.trucks], "truck")

    if not config.berths:
        errors.append("At least one berth is required.")
    if not config.ships:
        errors.append("At least one ship is required.")
    if not config.cranes:
        errors.append("At least one crane is required.")
    if not config.trucks:
        warnings.append("No trucks configured: containers will never leave the yard.")

    berth_ids = set(config.berth_ids())
    for s in config.ships:
        for b in s.compatible_berths:
            if b not in berth_ids:
                errors.append(f"Ship {s.id} lists unknown berth {b}.")
    for c in config.cranes:
        for b in c.compatible_berths:
            if b not in berth_ids:
                errors.append(f"Crane {c.id} lists unknown berth {b}.")

    if config.yard.initial_occupied > config.yard.capacity:
        errors.append(
            f"Yard initially holds {config.yard.initial_occupied} containers but its "
            f"capacity is only {config.yard.capacity}."
        )
    if config.yard.zones > config.yard.capacity:
        errors.append("Yard has more zones than storage spaces.")

    # Can every ship be served by at least one operational crane?
    if not errors:
        for s in config.ships:
            ship_berths = set(config.allowed_berths(s))
            ok = any(
                c.status == "operational" and ship_berths & set(config.crane_berths(c))
                for c in config.cranes
            )
            if not ok:
                errors.append(
                    f"Ship {s.id} cannot be served: no operational crane is compatible "
                    f"with its berths ({', '.join(sorted(ship_berths))})."
                )
        total = sum(s.containers for s in config.ships)
        room = config.yard.capacity - config.yard.initial_occupied
        biggest = max((s.containers for s in config.ships), default=0)
        collectable = sum(t.capacity * t.trips for t in config.trucks if t.available)
        if total > room + collectable:
            warnings.append(
                f"Ships bring {total} containers but only {room} yard spaces are free and "
                f"trucks can collect at most {collectable}: ships may be blocked by a full yard."
            )
        if biggest > config.yard.capacity:
            warnings.append("A single ship carries more containers than the whole yard can hold.")
    return errors, warnings


def build_config(data: dict) -> tuple[PortConfig | None, list[str], list[str]]:
    """Parse + validate a dict (from JSON or a form). Returns (config, errors, warnings)."""
    try:
        config = PortConfig.model_validate(data)
    except ValidationError as exc:
        return None, _fmt_pydantic_errors(exc), []
    errors, warnings = check_config(config)
    return (config if not errors else None), errors, warnings


def check_scenario(config: PortConfig, scenario: Scenario) -> list[str]:
    """Make sure a scenario refers to things that really exist in the port."""
    errors: list[str] = []
    if not scenario.disruptions:
        errors.append("A scenario needs at least one disruption.")
    for i, d in enumerate(scenario.disruptions, 1):
        tag = f"Disruption {i} ({d.type})"
        if d.type == "crane_breakdown":
            if not d.crane_id or config.crane(d.crane_id) is None:
                errors.append(f"{tag}: choose an existing crane.")
            if d.start_min is None or d.duration_min is None:
                errors.append(f"{tag}: start time and duration are required.")
        elif d.type == "ship_delay":
            if not d.ship_id or config.ship(d.ship_id) is None:
                errors.append(f"{tag}: choose an existing ship.")
            if d.delay_min is None:
                errors.append(f"{tag}: delay duration is required.")
        elif d.type == "yard_congestion":
            if d.target_occupancy_pct is None and not d.extra_containers:
                errors.append(f"{tag}: set a target occupancy or extra containers.")
            base = config.yard.initial_occupied + d.extra_containers
            target = int(round((d.target_occupancy_pct or 0) / 100 * config.yard.capacity))
            if max(base, target) > config.yard.capacity:
                errors.append(f"{tag}: resulting occupancy would exceed yard capacity.")
        elif d.type == "truck_surge":
            if not d.extra_trucks:
                errors.append(f"{tag}: number of extra trucks is required.")
            if d.window_start_min is None or d.window_end_min is None:
                errors.append(f"{tag}: arrival window is required.")
            elif d.window_end_min < d.window_start_min:
                errors.append(f"{tag}: window end must not be before window start.")
    return errors


def build_scenario(config: PortConfig, data: dict) -> tuple[Scenario | None, list[str]]:
    try:
        scenario = Scenario.model_validate(data)
    except ValidationError as exc:
        return None, _fmt_pydantic_errors(exc)
    errors = check_scenario(config, scenario)
    return (scenario if not errors else None), errors
