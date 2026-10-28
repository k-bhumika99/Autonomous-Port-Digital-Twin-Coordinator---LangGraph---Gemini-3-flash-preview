"""Agents as independent modules + candidate plan generation (8) + OR-Tools constraints."""
import pytest

from agents import (crane_agent, planning_agent, ship_agent, truck_agent, validation_agent, yard_agent)
from agents.supervisor import NullRecorder, Deps, default_settings, make_ctx
from config import TestConfig
from models.port_models import PortConfig, Scenario, CandidatePlan
from optimization.scheduler import assign_berths, assign_cranes
from simulation.digital_twin import simulate_port
from simulation.events import resolve_effects


@pytest.fixture()
def world(sample_port, sample_scenarios):
    cfg = PortConfig.model_validate(sample_port)
    scn = Scenario.model_validate(sample_scenarios["crane_breakdown"])
    fx = resolve_effects(cfg, scn).summary()      # agents receive the effects as a plain dict, like in the graph
    base = simulate_port(cfg, scn, None, record_snapshots=False)
    settings = default_settings(TestConfig)
    return cfg, scn, fx, base, make_ctx(Deps(gemini=None, settings=settings), settings), settings


def test_each_analysis_agent_returns_structured_report(world):
    cfg, scn, fx, base, ctx, _ = world
    for mod, key in ((ship_agent, "ship"), (crane_agent, "crane"), (yard_agent, "yard"), (truck_agent, "truck")):
        rep = mod.run(cfg, scn, fx, base, ctx)
        assert rep.agent_key == key and rep.status == "completed"
        assert rep.summary and isinstance(rep.findings, list)
        assert rep.explanation.text is None and rep.explanation.status == "unavailable"   # no key -> never a fabricated AI text


def test_crane_agent_sees_the_breakdown(world):
    cfg, scn, fx, base, ctx, _ = world
    rep = crane_agent.run(cfg, scn, fx, base, ctx)
    assert any("C3" in (f.title + f.detail) for f in rep.findings)


def test_candidate_plans_are_generated_from_the_actual_port(world):
    cfg, scn, fx, base, ctx, settings = world
    reports = {k: m.run(cfg, scn, fx, base, ctx).model_dump() for k, m in
               (("ship", ship_agent), ("crane", crane_agent), ("yard", yard_agent), ("truck", truck_agent))}
    plans, _notes = planning_agent.generate_plans(cfg, fx, reports, 0, [], 3.0)
    labels = [p.label for p in plans]
    assert labels[:2] == ["B", "C"] and len(plans) >= 2      # Plan A (original) is the simulated baseline
    assert all(p.change_count() > 0 for p in plans)          # every alternative really differs from the original
    assert plans[0].solver
    ship_ids = {s.id for s in cfg.ships}
    for p in plans:
        assert set(p.crane_assignment) <= ship_ids and set(p.ship_hold_min) <= ship_ids


def test_ortools_crane_assignment_skips_broken_crane_and_respects_compatibility(world):
    cfg, _scn, fx, *_ = world
    res = assign_cranes(cfg, fx, time_limit_s=2.0)
    assign = res["assignment"] if "assignment" in res else res
    ships = {s.id: s for s in cfg.ships}
    for ship_id, cranes in assign.items():
        for cid in cranes:
            crane = next(c for c in cfg.cranes if c.id == cid)
            assert crane.status == "operational"
            assert set(crane.compatible_berths) & set(ships[ship_id].compatible_berths)


def test_infeasible_plan_is_rejected_by_validation(world):
    cfg, scn, fx, *_ = world
    bad = CandidatePlan(label="Z", name="Impossible berth", berth_assignment={"S1": "B3"})
    res = simulate_port(cfg, scn, bad, record_snapshots=False)
    verdict = validation_agent.validate_plan(cfg, scn, fx, "Z", {"result": res, "plan": bad.model_dump()})
    assert verdict["feasible"] is False and verdict["violations"]


def test_tampered_metrics_are_caught(world):
    cfg, scn, fx, base, *_ = world
    plan = CandidatePlan()
    res = simulate_port(cfg, scn, plan, record_snapshots=False)
    res["metrics"]["total_ship_wait_min"] = 1.0                     # someone "improves" the number by hand
    verdict = validation_agent.validate_plan(cfg, scn, fx, "A", {"result": res, "plan": plan.model_dump()})
    assert verdict["feasible"] is False
    assert any(c["id"] == "metrics_match" and not c["passed"] for c in verdict["checks"])


def test_plan_with_stale_numbers_is_caught(world):
    cfg, scn, fx, *_ = world
    other = simulate_port(cfg, None, CandidatePlan(), record_snapshots=False)    # simulated WITHOUT the disruption
    verdict = validation_agent.validate_plan(cfg, scn, fx, "A", {"result": other, "plan": CandidatePlan().model_dump()})
    assert verdict["feasible"] is False
