"""4-7, 12: Digital Twin rules (berths, cranes, yard, trucks) and metric maths."""
from models.port_models import CandidatePlan, PortConfig
from simulation.digital_twin import simulate_port
from simulation.metrics import audit_result, compute_metrics, find_conflicts, improvement, objective_value


def run(cfg, scn=None, plan=None):
    return simulate_port(cfg, scn, plan, record_snapshots=False)


def test_baseline_completes_without_violations(sample_port):
    r = run(sample_port)
    assert r["completed"] and r["metrics"]["constraint_violations"] == 0
    assert r["metrics"]["ships_completed"] == r["metrics"]["ships_total"]
    for key in ("completion_time_min", "yard", "truck_trips", "events", "conflicts", "violations", "intervals"):
        assert key in r
    for key in ("total_ship_wait_min", "total_truck_wait_min", "crane_utilization", "throughput_per_hour", "max_yard_occupancy"):
        assert key in r["metrics"]


def test_simulation_is_deterministic(sample_port, sample_scenarios):
    a = run(sample_port, sample_scenarios["crane_breakdown"])
    b = run(sample_port, sample_scenarios["crane_breakdown"])
    assert a["metrics"] == b["metrics"] and a["input_hash"] == b["input_hash"]


def test_no_double_booking_and_berth_exclusive(sample_port, sample_scenarios):
    cfg = PortConfig.model_validate(sample_port)
    for scn in sample_scenarios.values():
        r = run(sample_port, scn)
        assert audit_result(r, cfg) == []
        # independent check: no crane and no berth ever has two overlapping tasks
        for key, field in (("crane", "crane"), ("berth", "berth")):
            rows = sorted(r["intervals"][key], key=lambda i: (i[field], i["start"]))
            for a, b in zip(rows, rows[1:]):
                if a[field] == b[field]:
                    assert a["end"] <= b["start"] + 1e-6, (key, a, b)


def test_crane_breakdown_hurts_the_original_schedule(sample_port, sample_scenarios):
    base, hit = run(sample_port), run(sample_port, sample_scenarios["crane_breakdown"])
    assert hit["metrics"]["total_ship_wait_min"] > base["metrics"]["total_ship_wait_min"]
    assert hit["outage_windows"]
    # the broken crane never works during its outage window
    out = hit["outage_windows"][0]
    for iv in hit["intervals"]["crane"]:
        if iv["crane"] == out["crane_id"]:
            assert iv["end"] <= out["start"] + 1e-6 or iv["start"] >= out["end"] - 1e-6


def test_delayed_ship_arrives_late(sample_port, sample_scenarios):
    r = run(sample_port, sample_scenarios["ship_delay"])
    base = run(sample_port)
    d = sample_scenarios["ship_delay"]["disruptions"][0]
    ship = next(x for x in r["ships"] if x["id"] == d["ship_id"])
    base_ship = next(x for x in base["ships"] if x["id"] == d["ship_id"])
    assert r["completed"]
    assert ship["delay_min"] == d["delay_min"]
    assert ship["arrival_eff"] == base_ship["arrival_eff"] + d["delay_min"]


def test_yard_never_exceeds_capacity_even_when_congested(sample_port):
    scn = {"name": "jam", "kind": "yard_congestion",
           "disruptions": [{"type": "yard_congestion", "target_occupancy_pct": 95, "extra_containers": 0}]}
    r = run(sample_port, scn)
    cap = sample_port["yard"]["capacity"]
    assert r["metrics"]["max_yard_occupancy"] <= cap
    assert all(level <= cap for _t, level, _q in r["yard"]["series"])
    assert r["yard"]["overflow_events"] == 0



def test_truck_surge_creates_or_keeps_queue_and_trips_finish(sample_port):
    scn = {"name": "surge", "kind": "truck_surge",
           "disruptions": [{"type": "truck_surge", "extra_trucks": 15, "window_start_min": 60, "window_end_min": 120}]}
    base, r = run(sample_port), run(sample_port, scn)
    assert r["metrics"]["max_truck_queue"] >= base["metrics"]["max_truck_queue"]
    assert r["metrics"]["total_truck_wait_min"] >= base["metrics"]["total_truck_wait_min"]
    assert r["metrics"]["containers_collected"] > 0


def test_incompatible_plan_is_flagged(sample_port):
    plan = CandidatePlan(label="X", name="bad", berth_assignment={"S1": "B3"})   # S1 only fits B1/B2
    r = run(sample_port, None, plan)
    assert r["plan_issues"]


def test_metrics_recompute_from_records(sample_port, sample_scenarios):
    r = run(sample_port, sample_scenarios["crane_breakdown"])
    again = compute_metrics(r)
    for k in ("total_ship_wait_min", "completion_time_min", "avg_crane_utilization_pct", "containers_unloaded"):
        assert abs(again[k] - r["metrics"][k]) < 0.05


def test_improvement_formula_and_zero_baseline():
    assert improvement(200, 150)["percent"] == 25.0
    assert improvement(100, 120)["percent"] == -20.0 and improvement(100, 120)["label"] == "worse"
    assert improvement(0, 0)["percent"] == 0.0
    assert improvement(0, 5)["percent"] is None
    assert improvement(50, 60, lower_is_better=False)["percent"] == 20.0


def test_objective_value_uses_documented_weights():
    m = {"total_ship_wait_min": 120, "total_truck_wait_min": 60, "completion_time_min": 600}
    w = {"w_ship_wait": 1.0, "w_truck_wait": 0.5, "w_completion": 0.25, "w_changes": 0.5}
    # 1*2h + 0.5*1h + 0.25*10h + 0.5*3 = 6.5
    assert objective_value(m, 3, w)["total"] == 6.5
