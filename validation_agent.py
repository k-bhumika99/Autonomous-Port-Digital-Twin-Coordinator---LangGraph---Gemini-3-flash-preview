"""Agent 6 - Validation & Decision Agent.

Deterministic gatekeeper. For every candidate plan it
  1. re-derives the hard-constraint audit from the recorded simulation facts,
  2. re-computes the headline metrics and compares them with the reported ones,
  3. checks that the simulation really finished and that no data is missing,
  4. rejects infeasible plans, scores the feasible ones with the transparent objective
     function and selects the lowest score.

Gemini (if available) only EXPLAINS the outcome afterwards; it can never change a verdict.

OBJECTIVE FUNCTION (lower is better) - see simulation/metrics.py::objective_value
    J = w_ship  * total_ship_waiting_hours
      + w_truck * total_truck_waiting_hours
      + w_comp  * completion_time_hours
      + w_chg   * number_of_changes_to_the_original_schedule
Default weights: ship 1.0, truck 0.5, completion 0.25, changes 0.5 (configurable per scenario).
"""
from __future__ import annotations

from agents.common import add_explanation, new_report
from models.port_models import CandidatePlan, PortConfig, Scenario
from models.workflow_state import AgentReport, Finding
from simulation import metrics as M
from simulation.digital_twin import plan_issues, sim_input_hash
from simulation.metrics import objective_value

REQUIRED_METRICS = ["total_ship_wait_min", "total_truck_wait_min", "completion_time_min",
                    "max_yard_occupancy_pct", "avg_crane_utilization_pct", "containers_unloaded"]
GROUPS = {
    "crane_double_booking": "A crane never serves two ships at once",
    "berth_double_occupancy": "A berth never hosts two ships at once",
    "berth_incompatible": "Ships only use compatible berths",
    "crane_incompatible": "Cranes only work compatible berths",
    "crane_worked_while_down": "Broken-down cranes never work",
    "yard_overflow": "Yard never exceeds its capacity",
}


def _check(cid: str, title: str, passed: bool, detail: str) -> dict:
    return {"id": cid, "title": title, "passed": bool(passed), "detail": detail}


def _metrics_match(reported: dict, recomputed: dict) -> list[str]:
    diffs = []
    for k, v in recomputed.items():
        r = reported.get(k)
        if isinstance(v, (int, float)) and isinstance(r, (int, float)):
            if abs(v - r) > 1e-6:
                diffs.append(f"{k}: reported {r} vs recomputed {v}")
        elif isinstance(v, (int, float)) != isinstance(r, (int, float)) and v is not None and r is not None:
            diffs.append(f"{k}: type mismatch")
    return diffs


def validate_plan(config: PortConfig, scenario: Scenario, effects: dict, label: str, cand: dict) -> dict:
    """Run every deterministic check for one candidate. Returns checks + violations."""
    res = cand["result"]
    plan = CandidatePlan.model_validate(cand["plan"])
    checks: list[dict] = []

    # 1. Simulation finished?
    checks.append(_check("sim_completed", "Simulation ran to completion", bool(res.get("completed")),
                         "All ships departed and all truck trips finished." if res.get("completed")
                         else f"Not finished: {res.get('incomplete_reason') or 'unknown reason'}."))
    # 2. numbers belong to this plan/config/scenario
    expected = sim_input_hash(config, scenario, plan)
    checks.append(_check("input_hash", "Results belong to this plan, port and scenario",
                         res.get("input_hash") == expected,
                         f"Fingerprint {res.get('input_hash')} {'matches' if res.get('input_hash') == expected else 'differs from ' + expected}."))
    # 3. plan structure
    extra_ids = {t["id"] for t in effects.get("extra_trucks", [])}
    issues = plan_issues(config, plan, extra_ids)
    checks.append(_check("plan_structure", "Plan only refers to existing, compatible resources", not issues,
                         "No structural problems." if not issues else "; ".join(issues[:3])))
    # 4. hard constraints re-derived from the recorded intervals
    audit = M.audit_result(res, config)
    by_code: dict[str, list[dict]] = {}
    for v in audit:
        by_code.setdefault(v["code"], []).append(v)
    for code, title in GROUPS.items():
        v = by_code.get(code, [])
        checks.append(_check(code, title, not v, "Respected throughout the run." if not v else v[0]["message"]))
    # 5. metrics reproducible from the simulation facts
    recomputed = M.compute_metrics(res)
    diffs = _metrics_match(res.get("metrics", {}), recomputed)
    checks.append(_check("metrics_match", "Reported metrics equal metrics recomputed from the simulation records",
                         not diffs, "All headline metrics reproduce exactly." if not diffs else "; ".join(diffs[:3])))
    checks.append(_check("violation_count", "Reported violation count equals the independent audit",
                         res.get("metrics", {}).get("constraint_violations") == len(audit),
                         f"Reported {res.get('metrics', {}).get('constraint_violations')} vs audited {len(audit)}."))
    # 6. conservation + completeness
    total = sum(s["containers"] for s in res["ships"])
    unloaded = sum(s["unloaded"] for s in res["ships"])
    checks.append(_check("containers_conserved", "Every container was unloaded", unloaded == total,
                         f"{unloaded} of {total} containers unloaded."))
    missing = [k for k in REQUIRED_METRICS if res.get("metrics", {}).get(k) is None]
    checks.append(_check("data_complete", "No required metric is missing", not missing,
                         "All required metrics present." if not missing else "Missing: " + ", ".join(missing)))
    # 7. same disruption applied to every plan
    same = res.get("effects") == effects
    checks.append(_check("disruption_applied", "The selected disruption was applied identically", same,
                         "Disruption effects equal those of the baseline." if same else "Disruption effects differ."))

    feasible = all(c["passed"] for c in checks)
    return {"label": label, "name": plan.name, "feasible": feasible, "checks": checks,
            "violations": [c["detail"] for c in checks if not c["passed"]],
            "changes": plan.change_count()}


def run(config: PortConfig, scenario: Scenario, effects: dict, candidates: dict, attempt: int,
        failed_agents: list, ctx: dict) -> tuple[AgentReport, dict]:
    rep = new_report("validation")
    weights = ctx["weights"]
    rep.inputs = {"plans_received": len(candidates), "weights": weights, "attempt": attempt}

    plans: dict[str, dict] = {}
    for label, cand in candidates.items():
        try:
            plans[label] = validate_plan(config, scenario, effects, label, cand)
        except Exception as exc:  # noqa: BLE001 - a plan we cannot verify is rejected, never trusted
            plans[label] = {"label": label, "name": cand["plan"].get("name", label), "feasible": False,
                            "checks": [_check("validator_error", "Plan could be validated", False,
                                              f"Validation error: {type(exc).__name__}")],
                            "violations": [f"Validation error: {type(exc).__name__}"],
                            "changes": 0}
    feasible = [l for l, p in plans.items() if p["feasible"]]

    # ---- objective ----------------------------------------------------------------------------
    for label, p in plans.items():
        if p["feasible"]:
            m = candidates[label]["result"]["metrics"]
            p["objective"] = objective_value(m, p["changes"], weights)
        else:
            p["objective"] = None
    ranking = sorted(feasible, key=lambda l: (plans[l]["objective"]["total"], plans[l]["changes"], l))
    selected = ranking[0] if ranking else None
    for rank, l in enumerate(ranking, 1):
        plans[l]["rank"] = rank

    # ---- reasons ---------------------------------------------------------------------------------
    base_j = plans["A"]["objective"]["total"] if "A" in plans and plans["A"]["feasible"] else None
    for label, p in plans.items():
        if not p["feasible"]:
            p["verdict"] = "rejected"
            p["reason"] = "Rejected: " + "; ".join(p["violations"][:3])
        elif label == selected:
            if label == "A":
                p["reason"] = (f"Selected: the original schedule has the lowest objective ({p['objective']['total']}) "
                               f"among the {len(feasible)} feasible plan(s) tested - no candidate beat it.")
            else:
                gain = "" if base_j is None else f" (original schedule: {base_j})"
                p["reason"] = (f"Selected: lowest objective {p['objective']['total']}{gain} among "
                               f"{len(feasible)} feasible plan(s).")
            p["verdict"] = "selected"
        else:
            sel_j = plans[selected]["objective"]["total"]
            p["reason"] = (f"Feasible but objective {p['objective']['total']} is higher than the selected plan's {sel_j}.")
            p["verdict"] = "feasible, not selected"

    validation = {
        "plans": plans, "ranking": ranking, "selected": selected, "tested": len(plans),
        "feasible_count": len(feasible), "weights": weights, "attempt": attempt,
        "status": "ok" if selected else "no_feasible_plan",
        "objective_formula": "J = w_ship*ship_wait_h + w_truck*truck_wait_h + w_completion*completion_h + w_changes*changes",
        "optimality_claim_allowed": False,
        "optimality_note": ("Plans are the best AMONG THE TESTED PLANS under this objective. OR-Tools solves small "
                            "estimate models to optimality, but the Digital Twin measures the real effect, so no "
                            "global optimality is claimed."),
        "feedback": [{"label": l, "reasons": p["violations"]} for l, p in plans.items() if not p["feasible"]],
        "incomplete_inputs": list(failed_agents),
    }

    # ---- report tables -------------------------------------------------------------------------------
    rep.tables["checklist"] = [{"plan": l, "check": c["title"], "result": "PASS" if c["passed"] else "FAIL",
                                "detail": c["detail"]} for l, p in plans.items() for c in p["checks"]]
    rep.tables["plan_feasibility"] = [{
        "plan": l, "name": p["name"], "feasible": "yes" if p["feasible"] else "no",
        "checks_passed": f"{sum(c['passed'] for c in p['checks'])}/{len(p['checks'])}",
        "verdict": p["verdict"], "reason": p["reason"]} for l, p in plans.items()]
    rep.tables["violations"] = [{"plan": l, "violation": v} for l, p in plans.items() for v in p["violations"]] \
        or [{"plan": "-", "violation": "No constraint violations in any plan."}]
    rep.tables["objective_breakdown"] = [{
        "plan": l, "ship_wait_h": p["objective"]["terms"]["ship_wait"]["value"],
        "truck_wait_h": p["objective"]["terms"]["truck_wait"]["value"],
        "completion_h": p["objective"]["terms"]["completion"]["value"],
        "changes": p["objective"]["terms"]["plan_changes"]["value"],
        "objective_J": p["objective"]["total"], "rank": p.get("rank")}
        for l, p in plans.items() if p["feasible"]]
    rep.tables["weights"] = [{"term": k, "weight": v} for k, v in weights.items()]
    rep.charts["objective"] = {"type": "bar", "title": "Objective J per feasible plan (lower is better)",
                               "x": [r["plan"] for r in rep.tables["objective_breakdown"]],
                               "series": [{"name": "J", "values": [r["objective_J"] for r in rep.tables["objective_breakdown"]]}]}
    rep.metrics = {"tested": len(plans), "feasible": len(feasible), "rejected": len(plans) - len(feasible),
                   "selected": selected, "selected_objective": plans[selected]["objective"]["total"] if selected else None}

    # ---- findings / status -------------------------------------------------------------------------------
    if selected:
        rep.findings.append(Finding(severity="info", title=f"Plan {selected} selected: {plans[selected]['name']}",
                                    detail=plans[selected]["reason"]))
    else:
        rep.status = "blocked"
        rep.findings.append(Finding(severity="critical", title="No feasible solution found",
                                    detail="Every candidate plan failed at least one hard-constraint or integrity check. "
                                           "No recommendation is produced."))
    for l, p in plans.items():
        if not p["feasible"]:
            rep.findings.append(Finding(severity="warning", title=f"Plan {l} rejected", detail=p["reason"]))
    if failed_agents:
        rep.warnings.append("Incomplete inputs: agent(s) failed - " + ", ".join(failed_agents))
    rep.summary = (f"Validated {len(plans)} plan(s): {len(feasible)} feasible, {len(plans) - len(feasible)} rejected. "
                   + (f"Selected plan {selected} (J = {plans[selected]['objective']['total']})." if selected
                      else "No feasible plan exists - no recommendation was made."))
    rep.log = [f"Re-audited {len(plans)} simulation result(s)", "Recomputed metrics from simulation records",
               f"Scored {len(feasible)} feasible plan(s) with the objective function",
               "Selected plan " + selected if selected else "No feasible plan: validation blocked the workflow"]
    facts = {"tested": len(plans), "feasible": len(feasible), "selected": selected,
             "plans": [{"plan": l, "feasible": p["feasible"],
                        "objective": p["objective"]["total"] if p["objective"] else None,
                        "violations": p["violations"][:2]} for l, p in plans.items()],
             "optimality_claim_allowed": False}
    add_explanation(rep, ctx.get("gemini"), facts,
                    "Explain why the selected plan was accepted and the others accepted or rejected. "
                    "Do not claim optimality.")
    return rep, validation
