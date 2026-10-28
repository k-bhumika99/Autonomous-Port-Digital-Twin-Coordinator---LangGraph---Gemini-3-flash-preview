"""1. signup/login, 2. authentication protection, 14. user isolation."""
from tests.conftest import register_and_login

PROTECTED = ["/dashboard", "/port-input", "/scenario", "/digital-twin", "/agents/ship", "/agents/crane", "/agents/yard",
             "/agents/truck", "/agents/planning", "/agents/validation", "/results/latest", "/history", "/api/twin"]


def test_public_pages_open(client):
    for p in ("/", "/signin", "/signup"):
        assert client.get(p).status_code == 200


def test_signup_then_signin_redirects_to_dashboard(client):
    r = client.post("/signup", data={"full_name": "A B", "email": "x@example.com", "password": "Passw0rd!x", "confirm_password": "Passw0rd!x"})
    assert r.status_code == 302 and r.headers["Location"].endswith("/signin")
    r = client.post("/signin", data={"email": "x@example.com", "password": "Passw0rd!x"})
    assert r.status_code == 302 and r.headers["Location"].endswith("/dashboard")


def test_password_is_hashed(app, client):
    register_and_login(client, "h@example.com")
    from database.db import connect
    row = connect(app.config["DATABASE"]).execute("SELECT password_hash FROM users WHERE email='h@example.com'").fetchone()
    assert row[0] != "Passw0rd!x" and ("pbkdf2" in row[0] or "scrypt" in row[0])


def test_duplicate_email_and_weak_password_rejected(client):
    ok = {"full_name": "A B", "email": "d@example.com", "password": "Passw0rd!x", "confirm_password": "Passw0rd!x"}
    client.post("/signup", data=ok)
    assert client.post("/signup", data=ok).status_code == 400            # form shown again with an error
    weak = dict(ok, email="w@example.com", password="abc", confirm_password="abc")
    assert client.post("/signup", data=weak).status_code == 400
    mismatch = dict(ok, email="m@example.com", confirm_password="different1!")
    assert client.post("/signup", data=mismatch).status_code == 400


def test_wrong_password_shows_error_and_no_session(client):
    register_and_login(client, "p@example.com")
    client.get("/logout")
    r = client.post("/signin", data={"email": "p@example.com", "password": "nope-nope"})
    assert r.status_code == 401
    assert client.get("/dashboard").status_code == 302


def test_protected_pages_redirect_when_logged_out(client):
    for p in PROTECTED:
        r = client.get(p)
        assert r.status_code in (302, 401), p
        if r.status_code == 302:
            assert "/signin" in r.headers["Location"], p


def test_logout_ends_session(logged_in):
    assert logged_in.get("/dashboard").status_code == 200
    logged_in.get("/logout")
    assert logged_in.get("/dashboard").status_code == 302


def test_every_protected_page_loads_when_logged_in(logged_in):
    for p in PROTECTED:
        assert logged_in.get(p, follow_redirects=True).status_code == 200, p


def test_users_cannot_see_each_others_simulations(app):
    a, b = app.test_client(), app.test_client()
    register_and_login(a, "a@example.com")
    register_and_login(b, "b@example.com")
    sc = {"scenario": {"name": "t", "kind": "crane_breakdown",
                       "disruptions": [{"type": "crane_breakdown", "crane_id": "C1", "start_min": 60, "duration_min": 120}]}}
    sid = a.post("/scenario/run", json=sc).json["simulation_id"]
    assert a.get(f"/results/{sid}").status_code == 200
    assert b.get(f"/results/{sid}").status_code == 404
    assert b.get(f"/results/{sid}/csv").status_code == 404
    assert b.get(f"/api/simulations/{sid}/status").status_code == 404
    assert b.get(f"/api/twin?sim={sid}").status_code == 404
