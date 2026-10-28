"""Agent 1 - Ship Operations Agent.

Reads the arrival schedule and the BASELINE simulation of the original schedule, finds
which ships wait and why, detects berth conflicts, proposes a berth sequence (list
scheduling: earliest start, shortest berth occupancy first) and measures that proposal
in the Digital Twin.
"""
from __future__ import annotations

from agents.common import add_explanation, impact_of_fragment, new_report
from models.port_models import PortConfig, Scenario
from models.workflow_state import AgentReport, Finding
from simulation.digital_twin import MAX_HOLD_MIN


def _blockers(ship: dict, baseline: dict, config: PortConfig) -> list[str]:
    """Ships that occupied every compatible berth when this ship arrived."""
    t = ship["arrival_eff"]
    blockers = []
    for iv in baseline["intervals"]["berth"]:
        if iv["ship"] != ship["id"] and iv["berth"] in ship["allowed_berths"] \
                and iv["start"] <= t < iv["end"]:
            blockers.append(iv["ship"])
    return sorted(set(blockers))


def _list_schedule(config: PortConfig, baseline: dict) -> dict:
    """Earliest-start / shortest-occupancy-first berth plan using baseline berth occupancy as duration."""
    ships = baseline["ships"]
    dur = {}
    for s in ships:
        if s["alloc_time"] is not None and s["depart_time"] is not None:
            dur[s["id"]] = s["depart_time"] - s["alloc_time"]
        else:
            dur[s["id"]] = 600.0
    free = {b: 0.0 for b in config.berth_ids()}
    todo = {s["id"]: s for s in ships}
    plan_berth, start_at = {}, {}
    while todo:
        best = None
        for sid, s in todo.items():
            for b in s["allowed_berths"]:
                start = max(free[b], s["arrival_eff"])
                key = (start, dur[sid], s["priority"], sid, b)
                if best is None or key < best[0]:
                    best = (key, sid, b, start)
        _, sid, b, start = best
        plan_berth[sid], start_at[sid] = b, start
        free[b] = start + dur[sid]
        del todo[sid]
    holds = {}
    for s in ships:
        h = int(round(start_at[s["id"]] - s["arrival_eff"]))
        if h > 0:
            holds[s["id"]] = min(h, MAX_HOLD_MIN)
    return {"berth_assignment": plan_berth, "ship_hold_min": holds, "planned_start": start_at}


def run(config: PortConfig, scenario: Scenario, effects: dict, baseline: dict, ctx: dict) -> AgentReport:
    rep = new_report("ship")
    ships = baseline["ships"]
    rep.inputs = {"ships": len(ships), "berths": len(config.berths),
                  "delays": effects.get("ship_delay", {}), "baseline_hash": baseline["input_hash"]}

    rep.tables["arrivals"] = [{
        "ship": s["id"], "name": s["name"], "scheduled_arrival_min": s["arrival_min"],
        "delay_min": s["delay_min"], "effective_arrival_min": s["arrival_eff"],
        "containers": s["containers"], "priority": s["priority"],
        "compatible_berths": ", ".join(s["allowed_berths"])} for s in sorted(ships, key=lambda x: x["arrival_eff"])]
    rep.tables["berth_allocation"] = [{
        "ship": s["id"], "berth": s["berth"] or "-", "berth_allocated_min": s["alloc_time"],
        "unloading_started_min": s["berth_ready"], "unloaded_by_min": s["done_time"],
        "departed_min": s["depart_time"], "berth_wait_min": s["berth_wait_min"],
        "idle_at_berth_min": s["idle_at_berth_min"]} for s in sorted(ships, key=lambda x: x["arrival_eff"])]
    waiting = sorted([s for s in ships if s["waiting_min"] > 0.5], key=lambda s: -s["waiting_min"])
    rep.tables["waiting_ships"] = [{
        "ship": s["id"], "total_waiting_min": s["waiting_min"], "berth_wait_min": s["berth_wait_min"],
        "idle_at_berth_min": s["idle_at_berth_min"],
        "blocked_by": ", ".join(_blockers(s, baseline, config)) or "-"} for s in waiting]
    rep.charts["waiting"] = {
        "type": "bar", "title": "Ship waiting in the original schedule (min)", "stack": True,
        "x": [s["id"] for s in ships],
        "series": [{"name": "Waiting for berth", "values": [s["berth_wait_min"] for s in ships]},
                   {"name": "Idle at berth (no crane working)", "values": [s["idle_at_berth_min"] for s in ships]}]}

    # ---- findings -------------------------------------------------------------
    for s in ships:
        if s["delay_min"]:
            rep.findings.append(Finding(severity="warning", title=f"{s['id']} arrives {s['delay_min']} min late",
                                        detail=f"Scheduled for t={s['arrival_min']}, now expected at t={s['arrival_eff']:.0f}."))
    for s in waiting:
        if s["berth_wait_min"] > 0.5:
            by = ", ".join(_blockers(s, baseline, config)) or "earlier ships"
            rep.findings.append(Finding(
                severity="critical" if s["berth_wait_min"] > 120 else "warning",
                title=f"{s['id']} waits {s['berth_wait_min']:.0f} min for a berth",
                detail=f"All compatible berths ({', '.join(s['allowed_berths'])}) were busy with {by}."))
        if s["idle_at_berth_min"] > 0.5:
            rep.findings.append(Finding(
                severity="warning", title=f"{s['id']} idles {s['idle_at_berth_min']:.0f} min at its berth",
                detail="The ship is moored but no crane is working it (crane shortage, outage or full yard)."))
    only_one = [s["id"] for s in ships if len(s["allowed_berths"]) == 1]
    if only_one:
        rep.findings.append(Finding(severity="info", title="Ships with a single compatible berth",
                                    detail=f"{', '.join(only_one)} can only use one berth, which limits scheduling freedom."))
    if not rep.findings:
        rep.findings.append(Finding(severity="info", title="No berth conflicts detected",
                                    detail="Every ship berthed on arrival and was worked without idle time."))

    # ---- proposal -----------------------------------------------------------------
    sched = _list_schedule(config, baseline)
    fragment = {"berth_assignment": sched["berth_assignment"], "ship_hold_min": sched["ship_hold_min"]}
    rep.proposal = fragment
    rep.proposal_impact = impact_of_fragment(config, scenario, baseline, fragment, ctx, "Ship agent berth sequence")
    rep.tables["proposed_adjustments"] = [{
        "ship": s["id"], "current_berth": s["berth"] or "-", "proposed_berth": sched["berth_assignment"][s["id"]],
        "planned_start_min": round(sched["planned_start"][s["id"]]),
        "hold_before_berthing_min": sched["ship_hold_min"].get(s["id"], 0)} for s in ships]

    imp = rep.proposal_impact
    d = imp["delta_vs_original"]["total_ship_wait_min"]
    verdict = ("reduces" if d is not None and d < -0.5 else "does not reduce")
    rep.summary = (f"{len(waiting)} of {len(ships)} ships waited in the original schedule "
                   f"(total {baseline['metrics']['total_ship_wait_min']:.0f} min). "
                   f"A berth-sequencing proposal {verdict} ship waiting when simulated "
                   f"(change {d:+.0f} min)." if d is not None else "Baseline analysed.")
    rep.log = ["Read arrival schedule", "Analysed baseline berth occupancy",
               "Computed list-schedule proposal", "Simulated proposal in the Digital Twin"]
    facts = {"ships_waiting": len(waiting), "total_ship_wait_min": baseline["metrics"]["total_ship_wait_min"],
             "worst": [{"ship": s["id"], "waiting_min": s["waiting_min"]} for s in waiting[:3]],
             "proposal_change_in_ship_wait_min": d, "proposal_helps": imp["helps"],
             "disruptions": effects.get("labels", [])}
    add_explanation(rep, ctx.get("gemini"), facts,
                    "Explain which ships wait and why, and whether the proposed berth sequence helps.")
    return rep
