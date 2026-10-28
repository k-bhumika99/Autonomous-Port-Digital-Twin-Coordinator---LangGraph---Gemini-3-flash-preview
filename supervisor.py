"""Supervisor logic for the LangGraph workflow.

The supervisor is NOT an LLM. It is deterministic Python that
  * runs each agent with error handling and a retry limit,
  * tracks which agents completed or failed,
  * decides whether to trigger re-planning after a failed validation,
  * compiles the final recommendation (improvement analysis, trust block, decision),
  * returns an explicit failure status when no valid plan exists.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from agents.planning_agent import describe_changes
from models.port_models import CandidatePlan, PortConfig, Scenario
from models.workflow_state import AGENTS, AgentReport, Explanation
from simulation.digital_twin import ASSUMPTIONS
from simulation.metrics import improvement_table

log = logging.getLogger(__name__)


class NullRecorder:
    """Does nothing; used by unit tests that do not need a database."""

    def __getattr__(self, _name):
        return lambda *a, **k: None


@dataclass
class Deps:
    gemini: Any = None
    recorder: Any = field(default_factory=NullRecorder)
    settings: dict = field(default_factory=dict)


def default_settings(config_cls) -> dict:
    return {
        "weights": dict(config_cls.OBJECTIVE_WEIGHTS),
        "max_replan_retries": int(config_cls.MAX_REPLAN_RETRIES),
        "agent_retry_limit": int(config_cls.AGENT_RETRY_LIMIT),
        "horizon_min": float(config_cls.SIM_HORIZON_HOURS) * 60.0,
        "solver_limit_s": 4.0,
    }


def make_ctx(deps: Deps, settings: dict) -> dict:
    return {"gemini": deps.gemini, "weights": settings["weights"], "horizon_min": settings["horizon_min"],
            "solver_limit_s": settings.get("solver_limit_s", 4.0)}


# ---------------------------------------------------------------------------
# running one agent safely
# ---------------------------------------------------------------------------
def guarded(deps: Deps, sim_id: int, key: str, settings: dict, fn: Callable[[], Any]):
    """Run `fn` (returns an AgentReport or (AgentReport, extra)); retry, then degrade to a failed report.

    Returns (result, failed_flag). The error text stored for the UI never contains a stack trace.
    """
    limit = int(settings.get("agent_retry_limit", 1))
    attempts = 0
    last_error = ""
    while attempts <= limit:
        attempts += 1
        deps.recorder.agent_update(sim_id, key, "running", attempts=attempts)
        try:
            result = fn()
            rep = result[0] if isinstance(result, tuple) else result
            deps.recorder.agent_update(sim_id, key, rep.status if rep.status != "pending" else "completed",
                                       attempts=attempts, report=rep.model_dump())
            return result, False
        except Exception as exc:  # noqa: BLE001 - one agent must never crash the whole workflow
            last_error = f"{type(exc).__name__}: {str(exc)[:200]}"
            log.exception("Agent %s failed (attempt %s)", key, attempts)
    meta = AGENTS[key]
    failed = AgentReport(agent_key=key, agent_name=meta["name"], role=meta["role"], status="failed",
                         summary=f"This agent failed after {attempts} attempt(s): {last_error}",
                         error=last_error, warnings=["Results from this agent are missing."],
                         explanation=Explanation(status="skipped", reason="Agent failed."))
    deps.recorder.agent_update(sim_id, key, "failed", attempts=attempts, report=failed.model_dump(), error=last_error)
    return failed, True


# ---------------------------------------------------------------------------
# routing
# ---------------------------------------------------------------------------
def should_replan(state: dict, settings: dict) -> bool:
    v = state.get("validation") or {}
    return (state.get("status") != "failed" and v.get("feasible_count", 0) == 0
            and state.get("attempt", 0) < int(settings.get("max_replan_retries", 2)))


def ai_status_of(reports: dict, final_expl: Explanation | None = None) -> str:
    statuses = [(r.get("explanation") or {}).get("status", "skipped") for r in reports.values()]
    if final_expl is not None:
        statuses.append(final_expl.status)
    if not statuses:
        return "unavailable"
    if all(s == "ok" for s in statuses):
        return "ok"
    if any(s == "ok" for s in statuses):
        return "partial"
    return "unavailable"


# ---------------------------------------------------------------------------
# final compilation
# ---------------------------------------------------------------------------
def compile_final(state: dict, deps: Deps, ctx: dict) -> tuple[dict, dict, str]:
    """Build the final decision, improvement analysis and trust block."""
    config = PortConfig.model_validate(state["config"])
    scenario = Scenario.model_validate(state["scenario"])
    validation = state.get("validation") or {}
    baseline = state["baseline"]
    candidates = state.get("candidates", {})
    reports = state.get("reports", {})
    selected = validation.get("selected")
    failed_agents = list(state.get("failed_agents", []))
    bm = baseline["metrics"]

    timeline = baseline.get("disruption_timeline", [])
    starts = [d["start"] for d in timeline if d.get("start") is not None]
    scenario_summary = {
        "port_name": config.name,
        "counts": {"ships": len(config.ships), "berths": len(config.berths), "cranes": len(config.cranes),
                   "trucks": len(config.trucks), "gates": config.gates,
                   "containers": sum(s.containers for s in config.ships),
                   "yard_capacity": config.yard.capacity, "yard_initial": config.yard.initial_occupied},
        "scenario_name": scenario.name, "scenario_kind": scenario.kind,
        "disruptions": scenario.labels(),
        "start_min": min(starts) if starts else 0,
        "baseline_completion_min": bm.get("completion_time_min"),
        "baseline_duration_note": ("Disruption starts at t=%s min; the original schedule finishes at t=%s min."
                                   % (min(starts) if starts else 0, bm.get("completion_time_min"))),
    }

    trust = {
        "plans_tested": validation.get("tested", 0), "plans_feasible": validation.get("feasible_count", 0),
        "simulations_completed": sum(1 for c in candidates.values() if c["result"]["completed"]),
        "simulations_total": len(candidates),
        "all_simulations_completed": all(c["result"]["completed"] for c in candidates.values()) if candidates else False,
        "checks": {l: {"passed": sum(c["passed"] for c in p["checks"]), "total": len(p["checks"]),
                       "feasible": p["feasible"]} for l, p in (validation.get("plans") or {}).items()},
        "replan_attempts": state.get("attempt", 0),
        "failed_agents": failed_agents,
        "incomplete_notes": [], "assumptions": ASSUMPTIONS,
        "disclaimer": ("All figures come from a SIMULATED port built from synthetic input data. They are not "
                       "verified real-port performance."),
        "optimality_claim_allowed": False,
    }
    if failed_agents:
        trust["incomplete_notes"].append("Agents that failed: " + ", ".join(failed_agents)
                                         + ". Planning continued without their proposals.")
    if not trust["all_simulations_completed"]:
        trust["incomplete_notes"].append("At least one candidate simulation did not run to completion.")
    for w in config_warnings(config):
        trust["incomplete_notes"].append(w)

    final: dict[str, Any] = {"scenario_summary": scenario_summary, "approval_status": "pending",
                             "trust": trust, "weights": validation.get("weights"),
                             "optimality_note": validation.get("optimality_note"), "warnings": [],
                             "attempts": state.get("attempt", 0)}
    summary: dict[str, Any] = {"scenario": scenario_summary, "trust": trust}

    if not selected:
        final.update({
            "status": "no_feasible_plan", "selected_label": None, "selected_name": None, "objective": None,
            "reasons": ["No candidate plan passed validation after %d planning attempt(s)." % (state.get("attempt", 0) + 1)]
                       + [f"Plan {f['label']}: {'; '.join(f['reasons'][:2])}" for f in validation.get("feedback", [])[:6]],
            "tradeoffs": [], "improvement": [], "recommended_changes": {},
        })
        final["warnings"].append("No feasible solution was found. No recommendation is made.")
        explanation = Explanation(status="skipped", reason="No recommendation to explain.")
        final["explanation"] = explanation.model_dump()
        summary["improvement"] = []
        return final, summary, ai_status_of(reports)

    sel = candidates[selected]
    plan = CandidatePlan.model_validate(sel["plan"])
    sm = sel["result"]["metrics"]
    imp = improvement_table(bm, sm)
    changes = describe_changes(plan)
    yard_prop = ((reports.get("yard") or {}).get("proposal") or {}).get("ship_hold_min", {})
    yard_changes = [f"Hold ship {sid} for {plan.ship_hold_min[sid]} min so trucks can clear the yard"
                    for sid in yard_prop if plan.ship_hold_min.get(sid, 0) > 0]
    yard_changes.append(f"Peak yard occupancy {bm['max_yard_occupancy_pct']}% (original) -> "
                        f"{sm['max_yard_occupancy_pct']}% (recommended)")
    recommended = {
        "label": selected, "name": plan.name, "description": plan.description,
        "resources_reassigned": changes["resources"] or ["No crane reassignment (original crane-to-berth assignment)."],
        "ship_scheduling": [c for c in changes["ships"]] or ["No ship schedule changes."],
        "truck_adjustments": changes["trucks"] or ["No truck schedule changes."],
        "yard_adjustments": yard_changes,
        "plan": plan.model_dump(),
    }
    vp = validation["plans"][selected]
    reasons = [vp["reason"]]
    if selected != "A":
        for row in imp:
            if row["metric"] in ("total_ship_wait_min", "total_truck_wait_min", "completion_time_min") \
                    and row["improvement_percent"] is not None:
                reasons.append(f"{row['label']}: {row['baseline']} -> {row['candidate']} "
                               f"({row['improvement_percent']:+.1f}%, {row['verdict']}).")
    reasons.append(f"All {len(vp['checks'])} validation checks passed for plan {selected}.")
    tradeoffs = []
    for row in imp:
        if row["improvement_percent"] is not None and row["improvement_percent"] < -0.05 and selected != "A":
            tradeoffs.append(f"{row['label']} is worse than the original: {row['baseline']} -> {row['candidate']} "
                             f"({row['improvement_percent']:+.1f}%).")
    if plan.change_count():
        tradeoffs.append(f"The plan requires {plan.change_count()} change(s) to the original schedule "
                         f"(each costs {validation['weights']['w_changes']} objective points).")
    if not tradeoffs:
        tradeoffs.append("No metric is worse than the original schedule.")
    warnings = []
    if failed_agents:
        warnings.append("Some agents failed: " + ", ".join(failed_agents) + ".")
    if sm["max_yard_occupancy_pct"] >= 95:
        warnings.append(f"Yard reaches {sm['max_yard_occupancy_pct']}% of capacity in the recommended plan.")
    if sm["max_truck_queue"] >= 5:
        warnings.append(f"Truck queue still peaks at {sm['max_truck_queue']} trucks.")
    warnings.append("Human approval is required before acting on this simulated recommendation.")

    final.update({
        "status": "completed", "selected_label": selected, "selected_name": plan.name,
        "objective": vp["objective"], "reasons": reasons, "tradeoffs": tradeoffs,
        "improvement": imp, "recommended_changes": recommended, "warnings": warnings,
        "baseline_objective": (validation["plans"].get("A") or {}).get("objective"),
    })
    summary["improvement"] = imp

    facts = {"selected_plan": selected, "plan_name": plan.name, "objective": vp["objective"]["total"],
             "plans_tested": validation["tested"], "plans_feasible": validation["feasible_count"],
             "improvement": [{"metric": r["label"], "baseline": r["baseline"], "candidate": r["candidate"],
                              "percent": r["improvement_percent"]} for r in imp
                             if r["metric"] in ("total_ship_wait_min", "total_truck_wait_min", "completion_time_min")],
             "optimality_claim_allowed": False, "results_are_simulated": True}
    explanation = (deps.gemini.explain("Final recommendation", facts,
                                       "Explain the final recommendation and its trade-offs. Say 'best among the "
                                       "tested plans', never 'optimal'. Mention it is simulated.")
                   if deps.gemini is not None else Explanation(status="unavailable", reason="AI service not configured."))
    final["explanation"] = explanation.model_dump()
    return final, summary, ai_status_of(reports, explanation)


def config_warnings(config: PortConfig) -> list[str]:
    from models.port_models import check_config
    return check_config(config)[1]
