"""Agent 4 - Truck & Logistics Agent.

Analyses gate queues and cargo-readiness in the baseline simulation, identifies bottlenecks and
proposes a schedule shift: each truck arrives when a loading lane is free and containers are ready.
"""
from __future__ import annotations

import math

from agents.common import add_explanation, impact_of_fragment, new_report
from models.port_models import PortConfig, Scenario
from models.workflow_state import AgentReport, Finding
from simulation.digital_twin import GATE_FIXED_MIN, GATE_PER_CONTAINER_MIN, MAX_TRUCK_SHIFT_MIN


def _propose_shifts(config: PortConfig, effects: dict, baseline: dict) -> dict[str, int]:
    """First-trip shifts: wait for cargo readiness, then spread arrivals over the free lanes."""
    trips = baseline["truck_trips"]
    first = {t["truck"]: t for t in trips if t["trip"] == 1}
    shifts: dict[str, int] = {}
    for tid, rec in first.items():
        if rec["cargo_wait_min"] > 0.5:
            shifts[tid] = min(MAX_TRUCK_SHIFT_MIN, int(math.ceil(rec["cargo_wait_min"])))
    trucks = [(t.id, t.arrival_min, t.capacity) for t in config.trucks if t.available] + \
             [(d["id"], d["arrival_min"], d["capacity"]) for d in effects.get("extra_trucks", [])]
    lanes = [0.0] * config.gates
    for tid, arr, cap in sorted(trucks, key=lambda x: (x[1], x[0])):
        arr_eff = arr + shifts.get(tid, 0)
        i = min(range(len(lanes)), key=lambda k: lanes[k])
        start = max(arr_eff, lanes[i])
        if start > arr_eff + 0.5:
            shifts[tid] = min(MAX_TRUCK_SHIFT_MIN, shifts.get(tid, 0) + int(math.ceil(start - arr_eff)))
            start = arr + shifts[tid]
        lanes[i] = start + GATE_FIXED_MIN + GATE_PER_CONTAINER_MIN * cap
    return {k: v for k, v in shifts.items() if v > 0}


def run(config: PortConfig, scenario: Scenario, effects: dict, baseline: dict, ctx: dict) -> AgentReport:
    rep = new_report("truck")
    trips = baseline["truck_trips"]
    m = baseline["metrics"]
    extra = effects.get("extra_trucks", [])
    rep.inputs = {"trucks": len([t for t in config.trucks if t.available]) + len(extra),
                  "surge_trucks": len(extra), "gate_lanes": config.gates, "baseline_hash": baseline["input_hash"]}

    per_truck: dict[str, dict] = {}
    for t in trips:
        r = per_truck.setdefault(t["truck"], {"truck": t["truck"], "trips_done": 0, "containers_collected": 0,
                                              "total_wait_min": 0.0, "max_wait_min": 0.0, "empty_trips": 0})
        r["trips_done"] += 1
        r["containers_collected"] += t["loaded"]
        r["total_wait_min"] = round(r["total_wait_min"] + t["wait_min"], 2)
        r["max_wait_min"] = max(r["max_wait_min"], t["wait_min"])
        r["empty_trips"] += 1 if t["loaded"] == 0 else 0
    rep.tables["arrival_and_collection"] = [{
        "truck": t.id, "first_arrival_min": t.arrival_min, "capacity_per_trip": t.capacity, "trips": t.trips,
        "round_trip_min": t.round_trip_min, "available": "yes" if t.available else "no",
        **{k: v for k, v in per_truck.get(t.id, {}).items() if k not in ("truck",)}} for t in config.trucks] + \
        [{"truck": d["id"], "first_arrival_min": d["arrival_min"], "capacity_per_trip": d["capacity"], "trips": 1,
          "round_trip_min": d["round_trip_min"], "available": "yes (surge)",
          **{k: v for k, v in per_truck.get(d["id"], {}).items() if k != "truck"}} for d in extra]
    queued = sorted([t for t in trips if t["gate_wait_min"] > 0.5], key=lambda t: -t["gate_wait_min"])
    rep.tables["truck_queue"] = [{"truck": t["truck"], "trip": t["trip"], "arrived_min": t["arrival"],
                                  "gate_wait_min": t["gate_wait_min"], "cargo_wait_min": t["cargo_wait_min"],
                                  "total_wait_min": t["wait_min"]} for t in queued[:25]]
    hours = {}
    for t in trips:
        h = int(t["arrival"] // 60)
        hours.setdefault(h, []).append(t["wait_min"])
    rep.tables["estimated_waiting_by_hour"] = [{
        "hour": h, "trips": len(v), "avg_wait_min": round(sum(v) / len(v), 1), "max_wait_min": round(max(v), 1)}
        for h, v in sorted(hours.items())]
    rep.charts["waiting"] = {
        "type": "bar", "title": "Average truck waiting by arrival hour (min)",
        "x": [f"h{h}" for h, _ in sorted(hours.items())],
        "series": [{"name": "Average wait (min)", "values": [round(sum(v) / len(v), 1) for _, v in sorted(hours.items())]}]}

    gate_service = GATE_FIXED_MIN + GATE_PER_CONTAINER_MIN * (sum(t["capacity"] for t in trips) / len(trips) if trips else 0)
    rep.metrics = {"trips_total": m["truck_trips_total"], "max_queue": m["max_truck_queue"],
                   "avg_wait_min": m["avg_truck_wait_min"], "max_wait_min": m["max_truck_wait_min"],
                   "empty_trips": m["truck_trips_empty"], "gate_lanes": config.gates,
                   "gate_throughput_per_hour": round(config.gates * 60 / gate_service, 1) if gate_service else None}

    if m["max_truck_queue"] >= 3:
        rep.findings.append(Finding(severity="critical" if m["max_truck_queue"] >= 6 else "warning",
                                    title=f"Gate bottleneck: queue peaks at {m['max_truck_queue']} trucks",
                                    detail=f"Average wait {m['avg_truck_wait_min']:.1f} min, worst {m['max_truck_wait_min']:.0f} min "
                                           f"with {config.gates} lanes."))
    cargo_wait = [t for t in trips if t["cargo_wait_min"] > 0.5]
    if cargo_wait:
        rep.findings.append(Finding(severity="warning", title=f"{len(cargo_wait)} trips waited for containers to be ready",
                                    detail="Trucks arrived before enough containers were stored; arrival should follow readiness."))
    if m["truck_trips_empty"]:
        rep.findings.append(Finding(severity="critical", title=f"{m['truck_trips_empty']} trips left empty",
                                    detail="No containers were ready within the waiting limit."))
    if extra:
        rep.findings.append(Finding(severity="warning", title=f"Truck surge: {len(extra)} extra trucks",
                                    detail="Extra trucks add load on the gate lanes in a short window."))
    if not rep.findings:
        rep.findings.append(Finding(severity="info", title="No truck bottleneck",
                                    detail="Trucks loaded without noticeable queues."))

    shifts = _propose_shifts(config, effects, baseline)
    fragment = {"truck_shift_min": shifts} if shifts else {}
    rep.proposal = fragment
    if fragment:
        rep.proposal_impact = impact_of_fragment(config, scenario, baseline, fragment, ctx, "Truck agent schedule shift")
    else:
        rep.proposal_impact = {"fragment": {}, "helps": False, "changes": 0,
                               "note": "No truck bottleneck: no schedule change proposed."}
    rep.tables["suggested_changes"] = [{"truck": k, "shift_first_arrival_by_min": v} for k, v in shifts.items()] or \
        [{"truck": "-", "shift_first_arrival_by_min": 0}]

    rep.summary = (f"{m['truck_trips_total']} truck trips, average wait {m['avg_truck_wait_min']:.1f} min, "
                   f"queue peak {m['max_truck_queue']}. "
                   + (f"Proposed shifting {len(shifts)} trucks." if shifts else "No schedule change needed."))
    rep.log = ["Read truck schedules", "Measured queues and cargo readiness from baseline",
               "Computed lane-aware arrival shifts" if shifts else "No shifts needed"]
    facts = {"trips": m["truck_trips_total"], "avg_wait_min": m["avg_truck_wait_min"], "max_queue": m["max_truck_queue"],
             "surge_trucks": len(extra), "shifted_trucks": len(shifts),
             "proposal_change_in_truck_wait_min": rep.proposal_impact.get("delta_vs_original", {}).get("total_truck_wait_min"),
             "proposal_helps": rep.proposal_impact.get("helps")}
    add_explanation(rep, ctx.get("gemini"), facts, "Explain truck queues/bottlenecks and any proposed schedule shifts.")
    return rep
