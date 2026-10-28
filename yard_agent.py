"""Agent 3 - Container Yard Agent.

Measures yard occupancy, free space, overflow/blocking risk and the in/out container flow in
the baseline simulation, and proposes holding low-priority ships when the yard is the bottleneck.
"""
from __future__ import annotations

from agents.common import add_explanation, downsample, impact_of_fragment, new_report
from models.port_models import PortConfig, Scenario
from models.workflow_state import AgentReport, Finding
from simulation.digital_twin import MAX_HOLD_MIN

WARN_PCT = 85.0


def _hourly_flows(baseline: dict) -> list[dict]:
    end = baseline["end_time_min"]
    hours = int(end // 60) + 1
    inflow = [0] * hours
    outflow = [0] * hours
    for e in baseline["events"]:
        h = min(int(e["t"] // 60), hours - 1)
        if e["type"] == "crane_work_end":
            inflow[h] += e["detail"].get("containers", 0)
        elif e["type"] == "truck_loading":
            outflow[h] += e["detail"].get("containers", 0)
    return [{"hour": h, "containers_in": inflow[h], "containers_out": outflow[h],
             "net": inflow[h] - outflow[h]} for h in range(hours)]


def run(config: PortConfig, scenario: Scenario, effects: dict, baseline: dict, ctx: dict) -> AgentReport:
    rep = new_report("yard")
    yard = baseline["yard"]
    cap = yard["capacity"]
    m = baseline["metrics"]
    start_level = effects.get("yard_initial", config.yard.initial_occupied)
    rep.inputs = {"capacity": cap, "initial_occupied": start_level, "zones": config.yard.zones,
                  "baseline_hash": baseline["input_hash"]}

    series = yard["series"]
    peak_row = max(series, key=lambda r: r[1] + r[2])
    rep.metrics = {"capacity": cap, "initial_occupied": start_level,
                   "initial_occupancy_pct": round(100 * start_level / cap, 1),
                   "free_spaces_at_start": cap - start_level,
                   "peak_committed": peak_row[1] + peak_row[2], "peak_time_min": peak_row[0],
                   "peak_occupancy_pct": m["max_yard_occupancy_pct"],
                   "blocked_crane_minutes": m["crane_blocked_minutes"], "final_level": m["final_yard_level"]}
    rep.tables["capacity"] = [{"measure": k.replace("_", " "), "value": v} for k, v in rep.metrics.items()]
    flows = _hourly_flows(baseline)
    rep.tables["hourly_flows"] = flows
    rep.tables["zones"] = [{"zone": f"Z{i + 1}", "capacity": c} for i, c in enumerate(yard["zone_caps"])]
    trend = downsample(series, 100)
    rep.charts["occupancy"] = {
        "type": "line", "title": "Yard occupancy over time (original schedule)", "x_title": "hours",
        "x": [round(r[0] / 60, 2) for r in trend],
        "series": [{"name": "Containers stored", "values": [r[1] for r in trend]},
                   {"name": "Capacity", "values": [cap] * len(trend), "dash": True}]}

    total_in = sum(s.containers for s in config.ships)
    out_cap = sum(t.capacity * t.trips for t in config.trucks if t.available) + \
        sum(t["capacity"] * t["trips"] for t in effects.get("extra_trucks", []))
    rep.metrics["total_inflow"] = total_in
    rep.metrics["max_collectable_by_trucks"] = out_cap

    if m["crane_blocked_minutes"] > 0.5:
        rep.findings.append(Finding(severity="critical", title="Yard fullness blocked crane work",
                                    detail=f"Cranes were blocked {m['crane_blocked_minutes']:.0f} min because no yard space was free."))
    if m["max_yard_occupancy_pct"] >= WARN_PCT:
        rep.findings.append(Finding(severity="warning" if m["max_yard_occupancy_pct"] < 100 else "critical",
                                    title=f"Potential overflow: yard reaches {m['max_yard_occupancy_pct']:.0f}% of capacity",
                                    detail=f"Peak around t={peak_row[0]:.0f} min. Capacity is {cap}, free at start {cap - start_level}."))
    if total_in > (cap - start_level) + out_cap:
        rep.findings.append(Finding(severity="critical", title="Inflow exceeds free space plus truck collection",
                                    detail=f"{total_in} containers arrive but only {cap - start_level} spaces are free "
                                           f"and trucks can remove at most {out_cap}."))
    if not rep.findings:
        rep.findings.append(Finding(severity="info", title="Yard capacity is comfortable",
                                    detail=f"Peak occupancy is {m['max_yard_occupancy_pct']:.0f}% of capacity."))

    # ---- proposal: hold the lowest-priority ship that berths near the occupancy peak -------------
    fragment: dict = {}
    notes = []
    risky = m["max_yard_occupancy_pct"] >= 90 or m["crane_blocked_minutes"] > 0.5
    if risky:
        t_peak = peak_row[0]
        near = [s for s in baseline["ships"] if s["berth_ready"] is not None
                and s["berth_ready"] <= t_peak and (s["done_time"] or t_peak) >= t_peak - 30]
        near.sort(key=lambda s: (-s["priority"], -s["containers"]))
        if near:
            target = near[0]
            hold = int(min(MAX_HOLD_MIN, max(30, round(m["crane_blocked_minutes"] or 60))))
            fragment = {"ship_hold_min": {target["id"]: hold}}
            notes.append(f"Hold {target['id']} (priority {target['priority']}) {hold} min before berthing so "
                         f"trucks can clear the yard first.")
    notes.append("Prioritise truck collection from the fullest zone first (the Digital Twin already does this).")
    rep.proposal = fragment
    if fragment:
        rep.proposal_impact = impact_of_fragment(config, scenario, baseline, fragment, ctx, "Yard agent hold")
    else:
        rep.proposal_impact = {"fragment": {}, "helps": False, "changes": 0,
                               "note": "No yard bottleneck: no schedule change proposed."}
    rep.tables["proposed_adjustments"] = [{"adjustment": n} for n in notes]

    rep.summary = (f"Peak yard occupancy {m['max_yard_occupancy_pct']:.0f}% of {cap} spaces; "
                   f"cranes were blocked {m['crane_blocked_minutes']:.0f} min by a full yard. "
                   + ("A ship hold was proposed." if fragment else "No schedule change needed for the yard."))
    rep.log = ["Read yard state", "Measured occupancy and flows from baseline", "Checked overflow risk",
               "Simulated proposal" if fragment else "No proposal needed"]
    facts = {"capacity": cap, "peak_occupancy_pct": m["max_yard_occupancy_pct"],
             "blocked_crane_minutes": m["crane_blocked_minutes"], "initial_occupied": start_level,
             "proposal": fragment, "proposal_helps": rep.proposal_impact.get("helps")}
    add_explanation(rep, ctx.get("gemini"), facts, "Explain yard occupancy, overflow risk and any proposed adjustment.")
    return rep
