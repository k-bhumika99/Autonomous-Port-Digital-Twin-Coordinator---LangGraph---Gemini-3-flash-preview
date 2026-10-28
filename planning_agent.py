"""Agent 5 - Planning & Optimization Agent.

Receives the findings/proposals of the Ship, Crane, Yard and Truck agents and builds several
CANDIDATE PLANS from the real port configuration and the selected disruption:

    A  Original schedule                      (the baseline simulation, nothing changed)
    B  Resource reassignment                  (crane preferences solved with OR-Tools CP-SAT)
    C  Reassignment + coordinated scheduling  (B + OR-Tools berth sequence + OR-Tools truck stagger
                                               + the yard agent's hold)
    D  Agent consensus                        (all four agent proposals merged)
    E  Pooled cranes                          (any compatible crane serves any berthed ship)

Every candidate is then RUN THROUGH THE DIGITAL TWIN. The numbers shown on the page are the
simulation's numbers; OR-Tools only decides which instructions to try.

When the Validation Agent rejects every plan, the supervisor calls this agent again with
`feedback`; on attempt > 0 it builds conservative "recovery" plans instead.
"""
from __future__ import annotations

from agents.common import add_explanation, new_report
from models.port_models import CandidatePlan, PortConfig, Scenario
from models.workflow_state import AgentReport, Finding
from optimization import scheduler
from simulation.digital_twin import MAX_HOLD_MIN, MAX_TRUCK_SHIFT_MIN, simulate_port, sim_input_hash
from simulation.metrics import improvement, objective_value


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def merge_fragments(*fragments: dict) -> dict:
    """Combine several plan fragments (later ones win for berth/crane choices, max() for delays)."""
    out = {"crane_policy": "fixed_home", "crane_assignment": {}, "berth_assignment": {},
           "ship_hold_min": {}, "truck_shift_min": {}}
    for f in fragments:
        if not f:
            continue
        if f.get("crane_policy") and f["crane_policy"] != "fixed_home":
            out["crane_policy"] = f["crane_policy"]
        out["crane_assignment"].update(f.get("crane_assignment", {}))
        out["berth_assignment"].update(f.get("berth_assignment", {}))
        for k, v in f.get("ship_hold_min", {}).items():
            out["ship_hold_min"][k] = min(MAX_HOLD_MIN, max(out["ship_hold_min"].get(k, 0), int(v)))
        for k, v in f.get("truck_shift_min", {}).items():
            out["truck_shift_min"][k] = min(MAX_TRUCK_SHIFT_MIN, max(out["truck_shift_min"].get(k, 0), int(v)))
    return out


def describe_changes(plan: CandidatePlan) -> dict[str, list[str]]:
    """Human-readable list of how a plan differs from the original schedule."""
    res, ships, trucks = [], [], []
    if plan.crane_policy == "assigned":
        res.append("Cranes may be reassigned across berths; the listed cranes are preferred per ship.")
    elif plan.crane_policy == "pooled":
        res.append("Cranes are pooled: any compatible idle crane helps any berthed ship.")
    for sid, cranes in sorted(plan.crane_assignment.items()):
        res.append(f"Ship {sid}: preferred cranes {', '.join(cranes)}")
    for sid, b in sorted(plan.berth_assignment.items()):
        ships.append(f"Ship {sid}: use berth {b}")
    for sid, h in sorted(plan.ship_hold_min.items()):
        if h > 0:
            ships.append(f"Ship {sid}: hold {h} min before requesting a berth")
    for tid, s in sorted(plan.truck_shift_min.items()):
        if s > 0:
            trucks.append(f"Truck {tid}: arrive {s} min later")
    return {"resources": res, "ships": ships, "trucks": trucks}


def _new_plan(label: str, name: str, desc: str, fragment: dict, rationale: list[str], solver: dict) -> CandidatePlan:
    return CandidatePlan(label=label, name=name, description=desc, rationale=rationale, solver=solver,
                         crane_policy=fragment.get("crane_policy", "fixed_home"),
                         crane_assignment=fragment.get("crane_assignment", {}),
                         berth_assignment=fragment.get("berth_assignment", {}),
                         ship_hold_min=fragment.get("ship_hold_min", {}),
                         truck_shift_min=fragment.get("truck_shift_min", {}))


def _solver_note(res: dict) -> dict:
    return {"status": res.get("status"), "optimal_for_estimate_model": res.get("optimal_for_estimate_model"),
            "wall_time_s": res.get("wall_time_s")}


def _proposal(reports: dict, key: str) -> dict:
    r = reports.get(key) or {}
    return r.get("proposal") or {}


# ---------------------------------------------------------------------------
# plan generation
# ---------------------------------------------------------------------------
def generate_plans(config: PortConfig, effects: dict, reports: dict, attempt: int, feedback: list,
                   solver_limit_s: float = 4.0) -> tuple[list[CandidatePlan], list[str]]:
    notes: list[str] = []
    plans: list[CandidatePlan] = []

    if attempt == 0:
        # ---- B: OR-Tools crane assignment ----------------------------------
        crane_sol = scheduler.assign_cranes(config, effects, solver_limit_s)
        notes.append(f"OR-Tools crane assignment: {crane_sol['status']}")
        crane_frag = {}
        if crane_sol["assignment"]:
            crane_frag = {"crane_policy": "assigned",
                          "crane_assignment": {s: c for s, c in crane_sol["assignment"].items() if c}}
        else:
            fallback = _proposal(reports, "crane")
            if fallback:
                crane_frag = fallback
                notes.append("Crane solver found no assignment; using the Crane agent's greedy proposal.")
        if crane_frag:
            plans.append(_new_plan(
                "B", "Resource reassignment",
                "Preferred cranes per ship chosen by OR-Tools; cranes may work any compatible berth.",
                crane_frag,
                [f"Crane assignment solver status: {crane_sol['status']} "
                 f"({'optimal for the estimate model' if crane_sol['optimal_for_estimate_model'] else 'feasible'})."],
                {"crane": _solver_note(crane_sol)}))

        # ---- C: B + coordinated scheduling -----------------------------------
        est = crane_sol.get("est_service_min") or {s.id: 300 for s in config.ships}
        berth_sol = scheduler.assign_berths(config, effects, est, solver_limit_s)
        notes.append(f"OR-Tools berth sequencing: {berth_sol['status']}")
        truck_min = _proposal(reports, "truck").get("truck_shift_min", {})
        truck_sol = scheduler.stagger_trucks(config, effects, truck_min, solver_limit_s)
        notes.append(f"OR-Tools truck staggering: {truck_sol['status']}")
        yard_frag = _proposal(reports, "yard")
        # The solver also returns planned start times ("holds"). The Digital Twin already queues ships
        # first-come-first-served, so holding a ship back on top of that only adds waiting; we therefore
        # use the solver's BERTH CHOICE and leave the timing to the simulation.
        sched_frag = {"berth_assignment": berth_sol["assignment"],
                      "truck_shift_min": truck_sol["shift_min"]}
        c_frag = merge_fragments(crane_frag, sched_frag, {"ship_hold_min": yard_frag.get("ship_hold_min", {})})
        if berth_sol["assignment"] or truck_sol["shift_min"] or yard_frag:
            plans.append(_new_plan(
                "C", "Reassignment + coordinated scheduling",
                "Plan B plus OR-Tools berth choices, staggered truck arrivals and the yard agent's hold (if any).",
                c_frag,
                [f"Berth sequencing solver status: {berth_sol['status']}.",
                 f"Truck staggering solver status: {truck_sol['status']}."],
                {"crane": _solver_note(crane_sol), "berth": _solver_note(berth_sol),
                 "truck": _solver_note(truck_sol)}))

        # ---- D: all agent proposals merged -----------------------------------
        frags = [_proposal(reports, k) for k in ("ship", "crane", "yard", "truck")]
        if any(frags):
            plans.append(_new_plan(
                "D", "Agent consensus",
                "The Ship, Crane, Yard and Truck agents' own proposals merged into one plan.",
                merge_fragments(*frags),
                ["Merged proposals from: " + ", ".join(k for k, f in zip(("ship", "crane", "yard", "truck"), frags) if f)],
                {}))
        # ---- E: pooled cranes ---------------------------------------------------
        plans.append(_new_plan("E", "Pooled cranes",
                               "Every idle compatible crane helps any berthed ship (no preferences).",
                               {"crane_policy": "pooled"},
                               ["Simple flexible policy used as a safe comparison point."], {}))
    else:
        # ---- recovery plans after a failed validation --------------------------
        why = sorted({r for f in feedback for r in f.get("reasons", [])})
        notes.append("Re-planning after validation feedback: " + ("; ".join(why)[:300] or "no reasons recorded"))
        tag = f"R{attempt}"
        plans.append(_new_plan(f"{tag}a", "Recovery: pooled cranes, no delays",
                               "Conservative plan: pool cranes, no holds or truck shifts.",
                               {"crane_policy": "pooled"},
                               ["Built after validation rejected earlier plans."] + why[:3], {}))
        crane_sol = scheduler.assign_cranes(config, effects, solver_limit_s)
        if crane_sol["assignment"]:
            plans.append(_new_plan(
                f"{tag}b", "Recovery: crane preferences only",
                "Only OR-Tools crane preferences; no berth, hold or truck changes.",
                {"crane_policy": "assigned",
                 "crane_assignment": {s: c for s, c in crane_sol["assignment"].items() if c}},
                ["Built after validation rejected earlier plans."], {"crane": _solver_note(crane_sol)}))
    return plans, notes


# ---------------------------------------------------------------------------
# the agent
# ---------------------------------------------------------------------------
def run(config: PortConfig, scenario: Scenario, effects: dict, baseline: dict, reports: dict,
        existing: dict, attempt: int, feedback: list, ctx: dict) -> tuple[AgentReport, dict]:
    """Returns (report, new_candidates). `existing` holds candidates from earlier attempts."""
    rep = new_report("planning")
    missing = [k for k in ("ship", "crane", "yard", "truck")
               if not reports.get(k) or reports[k].get("status") == "failed"]
    rep.inputs = {"attempt": attempt, "agents_received": [k for k in ("ship", "crane", "yard", "truck") if k not in missing],
                  "agents_missing": missing, "baseline_hash": baseline["input_hash"],
                  "feedback_items": len(feedback)}
    if missing:
        rep.warnings.append("Planning ran without findings from: " + ", ".join(missing)
                            + ". Proposals from those agents are not part of the candidate plans.")

    candidates: dict[str, dict] = {}
    if "A" not in existing:
        candidates["A"] = {"plan": CandidatePlan().model_dump(), "result": baseline, "attempt": 0}

    plans, notes = generate_plans(config, effects, reports, attempt, feedback, ctx.get("solver_limit_s", 4.0))
    known_hashes = {sim_input_hash(config, scenario, CandidatePlan.model_validate(c["plan"]))
                    for c in {**existing, **candidates}.values()}
    skipped = []
    for plan in plans:
        h = sim_input_hash(config, scenario, plan)
        if h in known_hashes:
            skipped.append(plan.label)
            continue
        known_hashes.add(h)
        result = simulate_port(config, scenario, plan, horizon_min=ctx["horizon_min"], record_snapshots=True)
        candidates[plan.label] = {"plan": plan.model_dump(), "result": result, "attempt": attempt}
    if skipped:
        notes.append("Skipped duplicate plans: " + ", ".join(skipped))

    # ---- tables -------------------------------------------------------------------------------
    all_c = {**existing, **candidates}
    base_m = baseline["metrics"]
    w = ctx["weights"]
    rows, changes_rows, trade = [], [], []
    for label in sorted(all_c, key=lambda l: (l != "A", l)):
        c = all_c[label]
        plan = CandidatePlan.model_validate(c["plan"])
        res = c["result"]
        m = res["metrics"]
        n_ch = plan.change_count()
        prelim = bool(res["completed"] and m["constraint_violations"] == 0)
        rows.append({
            "plan": label, "name": plan.name, "changes": n_ch,
            "ship_wait_min": m["total_ship_wait_min"], "truck_wait_min": m["total_truck_wait_min"],
            "avg_crane_util_pct": m["avg_crane_utilization_pct"], "max_yard_pct": m["max_yard_occupancy_pct"],
            "completion_min": m["completion_time_min"], "violations": m["constraint_violations"],
            "simulation": "completed" if res["completed"] else "incomplete",
            "feasibility": "feasible (pending validation)" if prelim else "infeasible",
            "objective": objective_value(m, n_ch, w)["total"] if prelim else None})
        ch = describe_changes(plan)
        changes_rows.append({"plan": label, "name": plan.name,
                             "resource_changes": "; ".join(ch["resources"][:4]) or "none",
                             "ship_changes": "; ".join(ch["ships"][:6]) or "none",
                             "truck_changes": "; ".join(ch["trucks"][:6]) or "none",
                             "solver": ", ".join(f"{k}: {v['status']}" for k, v in plan.solver.items()) or "-"})
        if label != "A" and prelim:
            sw = improvement(base_m["total_ship_wait_min"], m["total_ship_wait_min"], True)
            tw = improvement(base_m["total_truck_wait_min"], m["total_truck_wait_min"], True)
            ct = improvement(base_m["completion_time_min"], m["completion_time_min"], True)
            trade.append(f"Plan {label} ({plan.name}): ship waiting {_pct(sw)}, truck waiting {_pct(tw)}, "
                         f"completion {_pct(ct)}, using {n_ch} change(s).")
        elif label != "A":
            trade.append(f"Plan {label} ({plan.name}) is infeasible: "
                         + (res.get("incomplete_reason") or f"{m['constraint_violations']} constraint violation(s)") + ".")
    rep.tables["candidate_plans"] = rows
    rep.tables["changes_vs_original"] = changes_rows
    rep.tables["tradeoffs"] = [{"trade_off": t} for t in trade]
    rep.charts["ship_wait"] = {"type": "bar", "title": "Total ship waiting per plan (min, simulated)",
                               "x": [r["plan"] for r in rows],
                               "series": [{"name": "Ship waiting (min)", "values": [r["ship_wait_min"] for r in rows]}]}
    rep.charts["truck_wait"] = {"type": "bar", "title": "Total truck waiting per plan (min, simulated)",
                                "x": [r["plan"] for r in rows],
                                "series": [{"name": "Truck waiting (min)", "values": [r["truck_wait_min"] for r in rows]}]}
    rep.metrics = {"plans_generated": len(all_c), "plans_this_attempt": len(candidates),
                   "attempt": attempt, "all_simulations_completed": all(c["result"]["completed"] for c in all_c.values())}

    for n in notes:
        rep.log.append(n)
    rep.log.append(f"Simulated {len(candidates)} plan(s) in the Digital Twin")
    for r in rows:
        if r["feasibility"] == "infeasible":
            rep.findings.append(Finding(severity="warning", title=f"Plan {r['plan']} is infeasible",
                                        detail=f"{r['violations']} violation(s); simulation {r['simulation']}."))
    best = min((r for r in rows if r["objective"] is not None), key=lambda r: r["objective"], default=None)
    if best:
        rep.findings.append(Finding(severity="info", title=f"Lowest simulated objective so far: plan {best['plan']}",
                                    detail=f"Objective {best['objective']} (lower is better). Validation makes the final call."))
    rep.summary = (f"Generated and simulated {len(candidates)} new plan(s) ({len(all_c)} in total) from the "
                   f"real configuration and disruption using OR-Tools. "
                   + (f"Best simulated objective so far: plan {best['plan']} = {best['objective']}." if best
                      else "No plan is feasible yet."))
    facts = {"plans": [{k: r[k] for k in ("plan", "name", "ship_wait_min", "truck_wait_min", "completion_min",
                                          "violations", "objective")} for r in rows],
             "attempt": attempt, "optimality_claim_allowed": False}
    add_explanation(rep, ctx.get("gemini"), facts,
                    "Explain how the candidate plans differ and what trade-offs the simulation shows. "
                    "Do not declare any plan optimal.")
    return rep, candidates


def _pct(imp: dict) -> str:
    if imp["percent"] is None:
        return imp["label"]
    return f"{imp['percent']:+.1f}% ({imp['label']})"
