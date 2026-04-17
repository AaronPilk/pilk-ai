-- PILK schema v1.
-- Single-writer SQLite, WAL mode. All ids are text (ULID/UUID) unless noted.

CREATE TABLE IF NOT EXISTS schema_version (
    version    INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS plans (
    id             TEXT PRIMARY KEY,
    goal           TEXT NOT NULL,
    status         TEXT NOT NULL,              -- pending|running|paused|completed|failed|cancelled
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    estimated_usd  REAL,
    actual_usd     REAL NOT NULL DEFAULT 0,
    metadata_json  TEXT
);

CREATE TABLE IF NOT EXISTS steps (
    id           TEXT PRIMARY KEY,
    plan_id      TEXT NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
    idx          INTEGER NOT NULL,
    kind         TEXT NOT NULL,                -- tool|llm|agent|approval
    description  TEXT NOT NULL,
    status       TEXT NOT NULL,                -- pending|running|done|failed|skipped|awaiting_approval
    risk_class   TEXT NOT NULL,                -- READ|WRITE_LOCAL|EXEC_LOCAL|NET_READ|NET_WRITE|COMMS|FINANCIAL|IRREVERSIBLE
    input_json   TEXT,
    output_json  TEXT,
    started_at   TEXT,
    finished_at  TEXT,
    cost_usd     REAL NOT NULL DEFAULT 0,
    error        TEXT
);
CREATE INDEX IF NOT EXISTS idx_steps_plan ON steps(plan_id);

CREATE TABLE IF NOT EXISTS agents (
    name          TEXT PRIMARY KEY,
    version       TEXT NOT NULL,
    manifest_path TEXT NOT NULL,
    state         TEXT NOT NULL,               -- registered|ready|running|paused|stopped|errored
    installed_at  TEXT NOT NULL,
    last_run_at   TEXT,
    metadata_json TEXT
);

CREATE TABLE IF NOT EXISTS sandboxes (
    id            TEXT PRIMARY KEY,
    type          TEXT NOT NULL,               -- browser|process|fs|composite
    agent_name    TEXT,                        -- no FK: sandbox lifecycle decoupled
    state         TEXT NOT NULL,               -- creating|ready|running|suspended|destroyed|errored
    created_at    TEXT NOT NULL,
    destroyed_at  TEXT,
    metadata_json TEXT
);

CREATE TABLE IF NOT EXISTS approvals (
    id              TEXT PRIMARY KEY,
    plan_id         TEXT REFERENCES plans(id) ON DELETE CASCADE,
    step_id         TEXT REFERENCES steps(id) ON DELETE CASCADE,
    agent_name      TEXT,
    risk_class      TEXT NOT NULL,
    tool            TEXT NOT NULL,
    args_json       TEXT,
    status          TEXT NOT NULL,             -- pending|approved|rejected|expired
    created_at      TEXT NOT NULL,
    decided_at      TEXT,
    decision_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_approvals_status ON approvals(status);

-- Durable audit of trust rules. The live store is in-memory; this table
-- is written every time a rule is installed so the Approvals tab can
-- show a history across sessions even after rules expire or are revoked.
CREATE TABLE IF NOT EXISTS trust_audit (
    id            TEXT PRIMARY KEY,
    agent_name    TEXT,
    tool_name     TEXT NOT NULL,
    args_json     TEXT,
    ttl_seconds   INTEGER NOT NULL,
    expires_at    TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    created_by    TEXT NOT NULL DEFAULT 'user',
    reason        TEXT,
    approval_id   TEXT REFERENCES approvals(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_trust_audit_created ON trust_audit(created_at);

CREATE TABLE IF NOT EXISTS cost_entries (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id       TEXT,
    step_id       TEXT,
    agent_name    TEXT,
    kind          TEXT NOT NULL,               -- llm|tool|sandbox
    model         TEXT,
    input_tokens  INTEGER,
    output_tokens INTEGER,
    usd           REAL NOT NULL,
    occurred_at   TEXT NOT NULL,
    metadata_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_cost_plan ON cost_entries(plan_id);
CREATE INDEX IF NOT EXISTS idx_cost_occurred ON cost_entries(occurred_at);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    level       TEXT NOT NULL,                 -- debug|info|warn|error
    source      TEXT NOT NULL,
    plan_id     TEXT,
    step_id     TEXT,
    agent_name  TEXT,
    sandbox_id  TEXT,
    message     TEXT NOT NULL,
    data_json   TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);

CREATE TABLE IF NOT EXISTS sessions (
    name          TEXT PRIMARY KEY,
    kind          TEXT NOT NULL,               -- browser|api|other
    vault_ref     TEXT NOT NULL,               -- pointer to encrypted blob
    created_at    TEXT NOT NULL,
    last_used_at  TEXT,
    metadata_json TEXT
);

-- Runtime-mutable governor preferences. Env vars seed defaults at boot;
-- anything the user changes in the Settings UI persists here so it
-- survives pilkd restarts.
CREATE TABLE IF NOT EXISTS governor_prefs (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
