"""Helpers shared by all six agents."""
from __future__ import annotations

from models.port_models import CandidatePlan, PortConfig, Scenario
from models.workflow_state import AGENTS, AgentReport, Explanation
from simulation.digital_twin import simulate_port
from simulation.metrics import objective_value

KEY_METRICS = ["total_ship_wait_min", "total_truck_wait_min", "completion_time_min",
               "max_yard_occupancy_pct", "avg_crane_utilization_pct", "constraint_violations"]


def new_report(key: str) -> AgentReport:
    meta = AGENTS[key]
    return AgentReport(agent_key=key, agent_name=meta["name"], role=meta["role"], status="completed")


def quick_sim(config: PortConfig, scenario: Scenario, plan: CandidatePlan, horizon_min: float) -> dict:
    """A fast what-if run (no playback frames)."""
    return simulate_port(config, scenario, plan, horizon_min=horizon_min, record_snapshots=False)


def impact_of_fragment(config: PortConfig, scenario: Scenario, baseline: dict, fragment: dict,
                       ctx: dict, name: str) -> dict:
    """Simulate an agent's proposal on its own and report the measured effect vs the baseline."""
    plan = CandidatePlan(label="what-if", name=name, **fragment)
    res = quick_sim(config, scenario, plan, ctx["horizon_min"])
    bm, rm = baseline["metrics"], res["metrics"]
    w = ctx["weights"]
    j0 = objective_value(bm, 0, w)["total"]
    j1 = objective_value(rm, plan.change_count(), w)["total"]
    delta = {k: (None if rm.get(k) is None or bm.get(k) is None else round(rm[k] - bm[k], 2))
             for k in KEY_METRICS}
    return {
        "fragment": fragment, "changes": plan.change_count(),
        "simulation_completed": res["completed"],
        "constraint_violations": rm["constraint_violations"],
        "metrics": {k: rm.get(k) for k in KEY_METRICS},
        "delta_vs_original": delta,
        "objective_original": j0, "objective_with_proposal": j1,
        "helps": bool(res["completed"] and rm["constraint_violations"] == 0 and j1 < j0 - 1e-6),
    }


def add_explanation(report: AgentReport, gemini, facts: dict, task: str) -> None:
    """Ask Gemini to explain the (already computed) findings; never fabricate if unavailable."""
    if gemini is None:
        report.explanation = Explanation(status="unavailable", reason="AI service not configured.")
        return
    report.explanation = gemini.explain(report.agent_name, facts, task)


def downsample(rows: list, n: int = 90) -> list:
    if len(rows) <= n:
        return rows
    step = len(rows) / n
    out = [rows[int(i * step)] for i in range(n)]
    if out[-1] is not rows[-1]:
        out.append(rows[-1])
    return out


def fmt_min(x) -> str:
    if x is None:
        return "n/a"
    return f"{x:.0f} min"
