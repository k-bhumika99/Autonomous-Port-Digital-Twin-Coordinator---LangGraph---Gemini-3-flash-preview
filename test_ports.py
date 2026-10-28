"""3. valid / invalid port configurations (+ saving through the Port Input page)."""
import copy

from models.port_models import build_config


def test_sample_port_is_valid(sample_port):
    cfg, errors, _warn = build_config(sample_port)
    assert cfg is not None and errors == []


def test_yard_overfilled_is_rejected(sample_port):
    bad = copy.deepcopy(sample_port)
    bad["yard"]["initial_occupied"] = bad["yard"]["capacity"] + 1
    cfg, errors, _ = build_config(bad)
    assert cfg is None and any("capacity" in e for e in errors)


def test_ship_without_any_compatible_crane_is_rejected(sample_port):
    bad = copy.deepcopy(sample_port)
    for c in bad["cranes"]:
        c["status"] = "broken"                      # no operational crane left to serve any ship
    cfg, errors, _ = build_config(bad)
    assert cfg is None and errors


def test_duplicate_ids_and_negative_numbers_rejected(sample_port):
    bad = copy.deepcopy(sample_port)
    bad["ships"][1]["id"] = bad["ships"][0]["id"]
    assert build_config(bad)[0] is None
    bad = copy.deepcopy(sample_port)
    bad["ships"][0]["containers"] = -5
    assert build_config(bad)[0] is None


def test_port_input_page_loads_with_preloaded_sample(logged_in):
    html = logged_in.get("/port-input").get_data(as_text=True)
    assert "MV Aurora" in html
