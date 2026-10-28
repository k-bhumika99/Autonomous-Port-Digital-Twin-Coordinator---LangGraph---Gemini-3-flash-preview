"""10. LangGraph workflow, 11. retry limits / failed agents, 9. no feasible plan, 13. persistence, sample scenarios."""
import pytest

from agents import crane_agent, supervisor
from agents.supervisor import Deps, default_settings, guarded, should_replan
from config import TestConfig
from models.workflow_state import AGENT_ORDER
from orchestration.graph import run_workflow


def deps_and_settings(**over):
    s = default_settings(TestConfig)
    s["solver_limit_s"] = 2.0
    s.update(over)
    return Deps(gemini=None, settings=s), s


@pytest.mark.parametrize("kind", ["crane_breakdown", "ship_delay", "multiple"])
def test_workflow_runs_all_six_agents_for_each_sample_scenario(sample_port, sample_scenarios, kind):
    deps, s = deps_and_settings()
    out = run_workflow(sample_port, sample_scenarios[kind], deps, 0, s)
    assert set(out["reports"]) == set(AGENT_ORDER)
    assert all(r["status"] == "completed" for r in out["reports"].values())
    final = out["final"]
    assert final["status"] == "completed" and final["selected_label"] in out["candidates"]
    assert len(out["candidates"]) >= 3                                   # A, B, C at minimum
    # the recommendation really is the best FEASIBLE plan under the objective
    v = out["validation"]["plans"]
    feasible = {k: p for k, p in v.items() if p["feasible"]}
    assert final["selected_label"] in feasible
    assert final["objective"]["total"] == min(p["objective"]["total"] for p in feasible.values())


def test_recommendation_is_not_worse_than_original(sample_port, sample_scenarios):
    deps, s = deps_and_settings()
    out = run_workflow(sample_port, sample_scenarios["crane_breakdown"], deps, 0, s)
    plans = out["validation"]["plans"]
    assert plans[out["final"]["selected_label"]]["objective"]["total"] <= plans["A"]["objective"]["total"]


def test_failed_agent_is_isolated_and_reported(sample_port, sample_scenarios, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("simulated crash")
    monkeypatch.setattr(crane_agent, "run", boom)
    deps, s = deps_and_settings(agent_retry_limit=1)
    out = run_workflow(sample_port, sample_scenarios["crane_breakdown"], deps, 0, s)
    assert out["reports"]["crane"]["status"] == "failed"
    assert "crane" in out["failed_agents"]
    assert out["reports"]["ship"]["status"] == "completed"               # the others still ran
    assert "Traceback" not in (out["reports"]["crane"]["error"] or "")


def test_guarded_retries_exactly_the_configured_number_of_times():
    calls = []

    def always_fails():
        calls.append(1)
        raise ValueError("nope")
    deps, s = deps_and_settings(agent_retry_limit=2)
    rep, failed = guarded(deps, 0, "ship", s, always_fails)
    assert failed and rep.status == "failed" and len(calls) == 3         # 1 try + 2 retries


def test_replan_stops_at_retry_limit():
    s = {"max_replan_retries": 2}
    assert should_replan({"validation": {"feasible_count": 0}, "attempt": 0}, s)
    assert should_replan({"validation": {"feasible_count": 0}, "attempt": 1}, s)
    assert not should_replan({"validation": {"feasible_count": 0}, "attempt": 2}, s)       # limit reached
    assert not should_replan({"validation": {"feasible_count": 2}, "attempt": 0}, s)       # a plan passed
    assert not should_replan({"validation": {"feasible_count": 0}, "attempt": 0, "status": "failed"}, s)


def test_no_feasible_plan_means_explicit_failure_not_a_fake_recommendation(sample_port, sample_scenarios, monkeypatch):
    from agents import validation_agent
    real = validation_agent.validate_plan

    def reject_all(*a, **k):
        v = real(*a, **k)
        v["feasible"] = False
        v["violations"] = ["forced rejection for this test"]
        return v
    monkeypatch.setattr(validation_agent, "validate_plan", reject_all)
    deps, s = deps_and_settings(max_replan_retries=1)
    out = run_workflow(sample_port, sample_scenarios["crane_breakdown"], deps, 0, s)
    assert out["final"]["status"] == "no_feasible_plan"
    assert out["final"]["selected_label"] is None
    assert out["attempt"] <= 1 + 1                                       # bounded re-planning


def test_ai_explanations_unavailable_without_key(sample_port, sample_scenarios):
    deps, s = deps_and_settings()
    out = run_workflow(sample_port, sample_scenarios["crane_breakdown"], deps, 0, s)
    assert all(r["explanation"]["text"] is None for r in out["reports"].values())
    assert out["final"]["status"] == "completed"                         # numbers unaffected by missing AI


# ---------------------------------------------------------------- persistence through the web app
def _run(client, name="t"):
    body = {"scenario": {"name": name, "kind": "crane_breakdown",
                         "disruptions": [{"type": "crane_breakdown", "crane_id": "C1", "start_min": 60, "duration_min": 240}]}}
    r = client.post("/scenario/run", json=body)
    assert r.status_code == 200 and r.json["ok"]
    return r.json


def test_run_scenario_saves_everything_and_can_be_reopened(logged_in, app):
    info = _run(logged_in)
    sid = info["simulation_id"]
    st = logged_in.get(info["status_url"]).json
    assert st["status"] == "completed" and st["finished"] and set(st["agents"]) == set(AGENT_ORDER)
    from database.db import connect
    db = connect(app.config["DATABASE"])
    q = lambda sql: db.execute(sql, (sid,)).fetchone()[0]
    assert q("SELECT COUNT(*) FROM agent_runs WHERE simulation_id=?") == 6
    assert q("SELECT COUNT(*) FROM candidate_plans WHERE simulation_id=?") >= 3
    assert q("SELECT COUNT(*) FROM validation_results WHERE simulation_id=?") >= 3
    assert q("SELECT COUNT(*) FROM simulation_events WHERE simulation_id=?") > 0
    # reopen later: pages are served from the database, nothing is re-simulated
    for url in (info["results_url"], info["next_url"], f"/api/twin?sim={sid}", "/history", "/dashboard",
                "/agents/validation", "/agents/planning"):
        assert logged_in.get(url).status_code == 200, url
    csv = logged_in.get(f"/results/{sid}/csv")
    assert csv.status_code == 200 and csv.mimetype == "text/csv" and len(csv.data) > 100


def test_twin_feed_is_built_from_recorded_frames(logged_in):
    sid = _run(logged_in)["simulation_id"]
    d = logged_in.get(f"/api/twin?sim={sid}").json
    assert d["ok"] and d["frames"] and d["events"] and d["cranes"] and d["ships"]
    assert d["frames"][0]["t"] <= d["frames"][-1]["t"]


def test_invalid_scenario_is_rejected_with_message(logged_in):
    r = logged_in.post("/scenario/run", json={"scenario": {"name": "x", "kind": "crane_breakdown",
                       "disruptions": [{"type": "crane_breakdown", "crane_id": "NOPE", "start_min": 0, "duration_min": 10}]}})
    assert r.status_code == 422 and r.json["errors"]
