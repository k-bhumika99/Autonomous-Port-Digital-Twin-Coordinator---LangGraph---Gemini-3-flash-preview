"""Agent 2 - Crane & Equipment Agent.

Inspects crane status, breakdowns and workload in the baseline simulation, flags idle or
overloaded cranes and proposes a compatible, work-conserving crane assignment (greedy
balancing by priority and workload). The proposal is measured in the Digital Twin.
"""
from __future__ import annotations

from agents.common import add_explanation, impact_of_fragment, new_report
from models.port_models import PortConfig, Scenario
from models.workflow_state import AgentReport, Finding
from simulation.digital_twin import MAX_CRANES_PER_SHIP

OVERLOADED_PCT = 80.0
IDLE_PCT = 40.0


def _greedy_assignment(config: PortConfig, effects: dict) -> dict[str, list[str]]:
    """Give every ship its most-needy compatible cranes, never exceeding MAX_CRANES_PER_SHIP."""
    ships = sorted(config.ships, key=lambda s: (s.priority, s.arrival_min))
    cranes = [c for c in config.cranes if c.status == "operational"]
    assigned: dict[str, list[str]] = {s.id: [] for s in ships}
    rate_of = {s.id: 0.0 for s in ships}
    load = {c.id: 0 for c in cranes}
    for c in sorted(cranes, key=lambda c: -c.rate_per_hour):
        options = [s for s in ships
                   if set(config.crane_berths(c)) & set(config.allowed_berths(s))
                   and len(assigned[s.id]) < MAX_CRANES_PER_SHIP]
        if not options:
            continue
        target = max(options, key=lambda s: (s.containers / (rate_of[s.id] + 1.0), -s.priority))
        assigned[target.id].append(c.id)
        rate_of[target.id] += c.rate_per_hour
        load[c.id] += 1
    # second pass: every ship should have at least one crane if any compatible one exists
    for s in ships:
        if not assigned[s.id]:
            cand = [c for c in cranes if set(config.crane_berths(c)) & set(config.allowed_berths(s))]
            if cand:
                assigned[s.id].append(min(cand, key=lambda c: load[c.id]).id)
    return {sid: cids for sid, cids in assigned.items() if cids}


def run(config: PortConfig, scenario: Scenario, effects: dict, baseline: dict, ctx: dict) -> AgentReport:
    rep = new_report("crane")
    cranes = baseline["cranes"]
    outages = effects.get("crane_outages", [])
    rep.inputs = {"cranes": len(cranes), "outages": outages, "baseline_hash": baseline["input_hash"]}

    rep.tables["crane_status"] = [{
        "crane": c["id"], "status": ("broken down (scenario)" if any(o["crane_id"] == c["id"] for o in outages)
                                      else c["status"]),
        "rate_per_hour": c["rate"], "home_berth": c["home"], "compatible_berths": ", ".join(c["compatible"]),
        "utilization_pct": c["utilization_pct"], "busy_min": c["busy_min"], "down_min": c["down_min"],
        "containers_handled": c["containers_handled"]} for c in cranes]
    rep.tables["breakdowns"] = [{
        "crane": o["crane_id"], "starts_min": o["start"], "ends_min": o["end"],
        "duration_min": o["end"] - o["start"]} for o in outages]
    rep.charts["workload"] = {
        "type": "bar", "title": "Crane workload (containers handled)", "x": [c["id"] for c in cranes],
        "series": [{"name": "Containers handled", "values": [c["containers_handled"] for c in cranes]}]}
    rep.charts["utilization"] = {
        "type": "bar", "title": "Crane utilization (%)", "x": [c["id"] for c in cranes],
        "series": [{"name": "Utilization %", "values": [c["utilization_pct"] for c in cranes]}]}

    total_wait = baseline["metrics"]["total_ship_wait_min"]
    for c in cranes:
        if c["status"] != "operational":
            rep.findings.append(Finding(severity="warning", title=f"Crane {c['id']} is out of service",
                                        detail="Status 'maintenance': it cannot be assigned to any ship."))
            continue
        if c["down_min"] > 0:
            rep.findings.append(Finding(severity="critical", title=f"Crane {c['id']} down for {c['down_min']:.0f} min",
                                        detail="Ships relying on this crane stall unless other cranes are reassigned."))
        if c["utilization_pct"] >= OVERLOADED_PCT:
            rep.findings.append(Finding(severity="warning", title=f"Crane {c['id']} is overloaded ({c['utilization_pct']:.0f}% busy)",
                                        detail="It works almost continuously while the original schedule fixes it to its home berth."))
        elif c["utilization_pct"] <= IDLE_PCT and total_wait > 0:
            rep.findings.append(Finding(severity="warning", title=f"Crane {c['id']} is under-used ({c['utilization_pct']:.0f}% busy)",
                                        detail=f"Ships waited {total_wait:.0f} min in total while this crane had spare capacity."))
    if not any(f.severity != "info" for f in rep.findings):
        rep.findings.append(Finding(severity="info", title="Cranes are balanced",
                                    detail="No overloaded, idle or broken cranes were detected."))

    assignment = _greedy_assignment(config, effects)
    fragment = {"crane_policy": "assigned", "crane_assignment": assignment}
    rep.proposal = fragment
    rep.proposal_impact = impact_of_fragment(config, scenario, baseline, fragment, ctx, "Crane agent reassignment")
    rate = {c.id: c.rate_per_hour for c in config.cranes}
    rep.tables["alternative_assignments"] = [{
        "ship": s.id, "containers": s.containers, "preferred_cranes": ", ".join(assignment.get(s.id, [])) or "-",
        "combined_rate_per_hour": sum(rate[c] for c in assignment.get(s.id, [])),
        "original_cranes": ", ".join(c.id for c in config.cranes
                                      if c.status == "operational" and config.home_berth(c) in config.allowed_berths(s))}
        for s in config.ships]

    d = rep.proposal_impact["delta_vs_original"]["total_ship_wait_min"]
    rep.summary = (f"{sum(1 for c in cranes if c['utilization_pct'] >= OVERLOADED_PCT)} overloaded and "
                   f"{sum(1 for c in cranes if c['status']=='operational' and c['utilization_pct'] <= IDLE_PCT)} under-used cranes. "
                   f"Pooling cranes across berths changes total ship waiting by {d:+.0f} min in simulation.")
    rep.log = ["Read crane status and outages", "Computed utilization from baseline intervals",
               "Built compatible assignment", "Simulated reassignment in the Digital Twin"]
    facts = {"cranes": [{"id": c["id"], "utilization_pct": c["utilization_pct"], "down_min": c["down_min"]} for c in cranes],
             "outages": outages, "proposal_change_in_ship_wait_min": d,
             "proposal_helps": rep.proposal_impact["helps"]}
    add_explanation(rep, ctx.get("gemini"), facts,
                    "Explain crane workload imbalances/breakdowns and whether reassigning cranes helps.")
    return rep
