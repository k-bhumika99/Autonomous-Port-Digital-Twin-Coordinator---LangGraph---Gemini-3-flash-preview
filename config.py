"""Application configuration.

All secrets come from environment variables (loaded from a local `.env` file).
Nothing secret is hard-coded here.
"""
import os
import secrets
from datetime import timedelta
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

# Objective-function weights used by the Validation Agent (documented on the Validation page).
#   J = w_ship_wait * total_ship_wait_h
#     + w_truck_wait * total_truck_wait_h
#     + w_completion * completion_time_h
#     + w_changes * number_of_plan_changes
DEFAULT_OBJECTIVE_WEIGHTS = {
    "w_ship_wait": 1.0,
    "w_truck_wait": 0.5,
    "w_completion": 0.25,
    "w_changes": 0.5,
}


class Config:
    # --- Flask / security ---------------------------------------------------
    _env_secret = os.getenv("FLASK_SECRET_KEY", "")
    SECRET_KEY_IS_EPHEMERAL = (not _env_secret) or _env_secret == "replace_with_a_random_secret"
    # If no real secret is configured we generate a random one for this run only
    # (sessions will reset on restart). We never ship a default secret.
    SECRET_KEY = secrets.token_hex(32) if SECRET_KEY_IS_EPHEMERAL else _env_secret
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = os.getenv("SESSION_COOKIE_SECURE", "0") == "1"
    PERMANENT_SESSION_LIFETIME = timedelta(hours=8)
    WTF_CSRF_TIME_LIMIT = 8 * 3600
    TEMPLATES_AUTO_RELOAD = True

    # --- Database -----------------------------------------------------------
    DATABASE = os.getenv("DATABASE_PATH", str(BASE_DIR / "instance" / "port_twin.sqlite3"))

    # --- Gemini -------------------------------------------------------------
    GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
    GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3-flash-preview")
    GEMINI_TIMEOUT_SECONDS = int(os.getenv("GEMINI_TIMEOUT_SECONDS", "25"))
    GEMINI_MAX_RETRIES = int(os.getenv("GEMINI_MAX_RETRIES", "2"))

    # --- Workflow -----------------------------------------------------------
    WORKFLOW_ASYNC = True                 # run LangGraph in a background thread
    MAX_REPLAN_RETRIES = int(os.getenv("MAX_REPLAN_RETRIES", "2"))
    AGENT_RETRY_LIMIT = int(os.getenv("AGENT_RETRY_LIMIT", "1"))
    SIM_HORIZON_HOURS = float(os.getenv("SIM_HORIZON_HOURS", "300"))
    OBJECTIVE_WEIGHTS = dict(DEFAULT_OBJECTIVE_WEIGHTS)


class TestConfig(Config):
    TESTING = True
    WTF_CSRF_ENABLED = False
    WORKFLOW_ASYNC = False
    GEMINI_API_KEY = ""          # tests never call the live API
    SECRET_KEY = "test-secret-key"
    SECRET_KEY_IS_EPHEMERAL = False
