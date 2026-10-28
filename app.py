"""Autonomous Port Digital Twin Coordinator - Flask application factory.

Run it with:   python app.py        (or: flask --app app run)
"""
from __future__ import annotations

import logging
import threading

from flask import Flask, g, jsonify, render_template, request, session
from flask_wtf.csrf import CSRFError, CSRFProtect

from config import Config
from database import db
from models.workflow_state import AGENTS
from services.gemini_service import make_gemini
from services.simulation_service import mark_orphans_failed

csrf = CSRFProtect()
log = logging.getLogger("port_twin")

NAV = [
    ("Overview", [("dashboard.dashboard", "Dashboard", "grid"), ("port_input.port_input", "Port Input", "anchor"),
                  ("scenarios.scenario", "Scenario Setup", "bolt")]),
    ("Digital Twin", [("digital_twin.digital_twin", "Digital Twin", "cube")]),
    ("Agents", [("agents.ship", "Ship Agent", "ship"), ("agents.crane", "Crane Agent", "crane"),
                ("agents.yard", "Yard Agent", "box"), ("agents.truck", "Truck Agent", "truck"),
                ("agents.planning", "Planning Agent", "route"), ("agents.validation", "Validation Agent", "shield")]),
    ("Decision", [("results.latest", "Final Results", "flag"), ("results.history", "Simulation History", "clock")]),
]


def create_app(config_class=Config) -> Flask:
    app = Flask(__name__)
    app.config.from_object(config_class)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    db.init_db(app.config["DATABASE"])
    mark_orphans_failed(app.config["DATABASE"])
    csrf.init_app(app)
    app.teardown_appcontext(db.close_db)

    # ---- Gemini: validate configuration on startup (never fabricate answers) --------------
    app.gemini = make_gemini(app.config)
    if getattr(config_class, "SECRET_KEY_IS_EPHEMERAL", False) and not app.config.get("TESTING"):
        log.warning("FLASK_SECRET_KEY is not set: using a random key for this run (sessions reset on restart).")
    if not app.gemini.configured:
        log.warning("GEMINI_API_KEY is not set. AI explanations are OFF; simulation, OR-Tools and validation still work.")
    elif not app.config.get("TESTING"):
        def _probe():
            st = app.gemini.status(check_connection=True)
            (log.info if st["available"] else log.warning)("Gemini: %s", st["message"])
        threading.Thread(target=_probe, daemon=True).start()

    from routes import agents, auth, dashboard, digital_twin, port_input, results, scenarios
    for mod in (auth, dashboard, port_input, scenarios, digital_twin, agents, results):
        app.register_blueprint(mod.bp)

    # ---- request hooks -------------------------------------------------------------------------
    @app.before_request
    def load_user():
        g.user = None
        uid = session.get("user_id")
        if uid is not None:
            row = db.query_one(db.get_db(), "SELECT id, full_name, email FROM users WHERE id = ?", (uid,))
            if row is None:
                session.clear()
            else:
                g.user = row

    @app.context_processor
    def inject():
        return {"NAV": NAV, "AGENTS": AGENTS, "ai": app.gemini.status(), "endpoint": request.endpoint}

    @app.after_request
    def security_headers(resp):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        resp.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com; script-src 'self' 'unsafe-inline'; connect-src 'self'; "
            "frame-ancestors 'none'")
        if request.endpoint and request.endpoint.startswith(("api.", "scenarios.status", "digital_twin.twin_data")):
            resp.headers["Cache-Control"] = "no-store"
        return resp

    # ---- safe error pages (no stack traces, no secrets) -------------------------------------------
    def wants_json():
        return request.path.startswith("/api/") or request.is_json or request.path.endswith("/run")

    @app.errorhandler(CSRFError)
    def csrf_error(_e):
        if wants_json():
            return jsonify(ok=False, errors=["Your session expired. Please reload the page and try again."]), 400
        return render_template("error.html", code=400, title="Session expired",
                               message="The form was submitted with an expired security token. Please go back, reload and try again."), 400

    @app.errorhandler(404)
    def not_found(_e):
        if wants_json():
            return jsonify(ok=False, errors=["Not found."]), 404
        return render_template("error.html", code=404, title="Page not found",
                               message="That page or simulation does not exist (or belongs to another account)."), 404

    @app.errorhandler(500)
    def server_error(_e):
        log.exception("Unhandled error")
        if wants_json():
            return jsonify(ok=False, errors=["Something went wrong on the server."]), 500
        return render_template("error.html", code=500, title="Something went wrong",
                               message="The error has been logged. Please try again."), 500

    @app.route("/api/ai-status")
    def ai_status():
        if g.user is None:
            return jsonify(ok=False), 401
        return jsonify(app.gemini.status(check_connection=True))

    return app


if __name__ == "__main__":
    application = create_app()
    application.run(host="127.0.0.1", port=5000, debug=True, threaded=True)
