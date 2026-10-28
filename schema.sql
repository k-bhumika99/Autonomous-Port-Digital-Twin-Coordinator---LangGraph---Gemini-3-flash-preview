-- SQLite schema for the Autonomous Port Digital Twin Coordinator.
-- Queryable fields are real columns; bulky structured data is stored as JSON text.

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    full_name     TEXT NOT NULL,
    email         TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

-- One row per saved version of a user's port; only one is "active".
CREATE TABLE IF NOT EXISTS port_configurations (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name       TEXT NOT NULL,
    gates      INTEGER NOT NULL DEFAULT 2,
    is_active  INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_portcfg_user ON port_configurations(user_id, is_active);

CREATE TABLE IF NOT EXISTS berths (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    config_id INTEGER NOT NULL REFERENCES port_configurations(id) ON DELETE CASCADE,
    code      TEXT NOT NULL,
    name      TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_berths_cfg ON berths(config_id);

CREATE TABLE IF NOT EXISTS ships (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    config_id         INTEGER NOT NULL REFERENCES port_configurations(id) ON DELETE CASCADE,
    code              TEXT NOT NULL,
    name              TEXT NOT NULL DEFAULT '',
    arrival_min       INTEGER NOT NULL,
    containers        INTEGER NOT NULL,
    priority          INTEGER NOT NULL DEFAULT 2,
    compatible_berths TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_ships_cfg ON ships(config_id);

CREATE TABLE IF NOT EXISTS cranes (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    config_id         INTEGER NOT NULL REFERENCES port_configurations(id) ON DELETE CASCADE,
    code              TEXT NOT NULL,
    status            TEXT NOT NULL DEFAULT 'operational',
    rate_per_hour     REAL NOT NULL,
    compatible_berths TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_cranes_cfg ON cranes(config_id);

CREATE TABLE IF NOT EXISTS trucks (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    config_id      INTEGER NOT NULL REFERENCES port_configurations(id) ON DELETE CASCADE,
    code           TEXT NOT NULL,
    arrival_min    INTEGER NOT NULL,
    capacity       INTEGER NOT NULL,
    trips          INTEGER NOT NULL DEFAULT 1,
    round_trip_min INTEGER NOT NULL DEFAULT 75,
    available      INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_trucks_cfg ON trucks(config_id);

CREATE TABLE IF NOT EXISTS yard_state (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    config_id        INTEGER NOT NULL UNIQUE REFERENCES port_configurations(id) ON DELETE CASCADE,
    capacity         INTEGER NOT NULL,
    initial_occupied INTEGER NOT NULL DEFAULT 0,
    zones            INTEGER NOT NULL DEFAULT 4
);

CREATE TABLE IF NOT EXISTS scenarios (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    config_id   INTEGER NOT NULL REFERENCES port_configurations(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    kind        TEXT NOT NULL,
    params_json TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_scenarios_user ON scenarios(user_id);

CREATE TABLE IF NOT EXISTS simulations (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id          INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    config_id        INTEGER NOT NULL REFERENCES port_configurations(id) ON DELETE CASCADE,
    scenario_id      INTEGER NOT NULL REFERENCES scenarios(id) ON DELETE CASCADE,
    status           TEXT NOT NULL DEFAULT 'queued',  -- queued|running|completed|no_feasible_plan|failed
    stage            TEXT NOT NULL DEFAULT 'Queued',
    progress         INTEGER NOT NULL DEFAULT 0,       -- 0..100
    recommended_plan TEXT,
    objective_value  REAL,
    decision_json    TEXT,                             -- final decision (selected plan, reasons ...)
    summary_json     TEXT,                             -- scenario summary, improvement table, trust block
    approval_status  TEXT NOT NULL DEFAULT 'pending',  -- pending|approved|rejected
    ai_status        TEXT,
    replan_attempts  INTEGER NOT NULL DEFAULT 0,
    config_json      TEXT NOT NULL,                    -- snapshot, so old runs can be reopened
    scenario_json    TEXT NOT NULL,
    error            TEXT,
    started_at       TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_sim_user ON simulations(user_id, id DESC);

CREATE TABLE IF NOT EXISTS agent_runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    simulation_id INTEGER NOT NULL REFERENCES simulations(id) ON DELETE CASCADE,
    agent_key     TEXT NOT NULL,                      -- ship|crane|yard|truck|planning|validation
    status        TEXT NOT NULL DEFAULT 'pending',    -- pending|running|completed|failed|blocked
    attempts      INTEGER NOT NULL DEFAULT 0,
    started_at    TEXT,
    finished_at   TEXT,
    ai_status     TEXT,
    report_json   TEXT,
    error         TEXT,
    UNIQUE(simulation_id, agent_key)
);
CREATE INDEX IF NOT EXISTS idx_agent_runs_sim ON agent_runs(simulation_id);

CREATE TABLE IF NOT EXISTS candidate_plans (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    simulation_id  INTEGER NOT NULL REFERENCES simulations(id) ON DELETE CASCADE,
    label          TEXT NOT NULL,
    name           TEXT NOT NULL,
    attempt        INTEGER NOT NULL DEFAULT 0,
    plan_json      TEXT NOT NULL,
    result_json    TEXT,                              -- metrics + audit (no events/snapshots)
    snapshots_json TEXT,                              -- Digital Twin playback frames
    sim_completed  INTEGER NOT NULL DEFAULT 0,
    feasible       INTEGER,
    objective      REAL,
    UNIQUE(simulation_id, label)
);
CREATE INDEX IF NOT EXISTS idx_plans_sim ON candidate_plans(simulation_id);

CREATE TABLE IF NOT EXISTS validation_results (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    simulation_id        INTEGER NOT NULL REFERENCES simulations(id) ON DELETE CASCADE,
    plan_label           TEXT NOT NULL,
    feasible             INTEGER NOT NULL,
    checks_json          TEXT NOT NULL,
    violations_json      TEXT NOT NULL,
    objective            REAL,
    objective_breakdown  TEXT,
    selected             INTEGER NOT NULL DEFAULT 0,
    reason               TEXT,
    UNIQUE(simulation_id, plan_label)
);
CREATE INDEX IF NOT EXISTS idx_val_sim ON validation_results(simulation_id);

CREATE TABLE IF NOT EXISTS simulation_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    simulation_id INTEGER NOT NULL REFERENCES simulations(id) ON DELETE CASCADE,
    plan_label    TEXT NOT NULL,
    t             REAL NOT NULL,
    type          TEXT NOT NULL,
    entity        TEXT,
    detail_json   TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_sim ON simulation_events(simulation_id, plan_label, t);
