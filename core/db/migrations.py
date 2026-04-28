"""Applies `schema.sql` and versioned migrations to the PILK SQLite database.

The base `schema.sql` is idempotent — re-running it on startup is safe.
When a backward-incompatible change is needed, add an entry to `MIGRATIONS`
keyed by the new version number and a list of SQL statements. Everything
runs in one transaction per version bump.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

CURRENT_VERSION = 13
SCHEMA_FILE = Path(__file__).parent / "schema.sql"


MIGRATIONS: dict[int, list[str]] = {
    # v2: drop the agent_name FK on sandboxes. A sandbox's lifecycle is
    # decoupled from agent registration — a sandbox can outlive or precede
    # the agent row. SQLite can't drop a column constraint, so we recreate
    # the table. The table is only used transiently across sessions; data
    # loss here is expected and safe.
    2: [
        "DROP TABLE IF EXISTS sandboxes",
        """CREATE TABLE sandboxes (
            id            TEXT PRIMARY KEY,
            type          TEXT NOT NULL,
            agent_name    TEXT,
            state         TEXT NOT NULL,
            created_at    TEXT NOT NULL,
            destroyed_at  TEXT,
            metadata_json TEXT
        )""",
    ],
    # v3: trust_audit table for the approval/trust layer (batch 3). The
    # live trust store is in-memory; this table is the historical mirror.
    3: [
        """CREATE TABLE IF NOT EXISTS trust_audit (
            id            TEXT PRIMARY KEY,
            agent_name    TEXT,
            tool_name     TEXT NOT NULL,
            args_json     TEXT,
            ttl_seconds   INTEGER NOT NULL,
            expires_at    TEXT NOT NULL,
            created_at    TEXT NOT NULL,
            created_by    TEXT NOT NULL DEFAULT 'user',
            reason        TEXT,
            approval_id   TEXT
        )""",
        "CREATE INDEX IF NOT EXISTS idx_trust_audit_created ON trust_audit(created_at)",
    ],
    # v4: structured memory. What PILK is currently retaining — explicit
    # preferences, standing instructions, remembered facts, observed
    # patterns. Entries are user-curated in this phase; auto-extraction
    # and vector recall are deferred.
    4: [
        """CREATE TABLE IF NOT EXISTS memory_entries (
            id          TEXT PRIMARY KEY,
            kind        TEXT NOT NULL,
            title       TEXT NOT NULL,
            body        TEXT NOT NULL DEFAULT '',
            source      TEXT NOT NULL DEFAULT 'user',
            plan_id     TEXT,
            created_at  TEXT NOT NULL,
            updated_at  TEXT NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS idx_memory_kind ON memory_entries(kind)",
        "CREATE INDEX IF NOT EXISTS idx_memory_created ON memory_entries(created_at)",
    ],
    # v5: per-agent autonomy profile. Persisted so the gate can widen
    # the auto-allow set for trusted agents across restarts.
    5: [
        """CREATE TABLE IF NOT EXISTS agent_policies (
            agent_name TEXT PRIMARY KEY,
            profile    TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""",
    ],
    # v6: user-managed API keys for external integrations (HubSpot,
    # Hunter.io, Google APIs, etc.). One row per logical secret name;
    # values are plaintext under 0600 OS perms on the SQLite file
    # (same security boundary as OAuth tokens in accounts/secrets/).
    # Phase 2 moves this table (and the OAuth blob) onto Supabase with
    # per-user scoping; single-tenant v1 keeps it alongside the daemon.
    6: [
        """CREATE TABLE IF NOT EXISTS integration_secrets (
            name       TEXT PRIMARY KEY,
            value      TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""",
    ],
    # v7: runtime settings for the XAUUSD execution agent. A tiny
    # key/value table — today it only holds `execution_mode`
    # (approve | autonomous), but future non-secret toggles
    # (cooldown length, countertrend enable, etc.) land here too.
    # Separate from integration_secrets because these aren't secrets
    # and the UI treats them differently (toggles, not password inputs).
    7: [
        """CREATE TABLE IF NOT EXISTS xauusd_settings (
            name       TEXT PRIMARY KEY,
            value      TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""",
    ],
    # v8: Sentinel supervisor tables. Heartbeats are written by every
    # long-running agent via `sentinel_heartbeat`; Sentinel's stale-
    # heartbeat rule reads from this table on its 30s scan.
    # Incidents are the source-of-truth for every finding Sentinel
    # has surfaced — mirrored to ``<home>/sentinel/incidents.jsonl``
    # for tail-based operator tools.
    8: [
        """CREATE TABLE IF NOT EXISTS agent_heartbeats (
            agent_name             TEXT PRIMARY KEY,
            status                 TEXT NOT NULL,
            progress               TEXT,
            active_task_id         TEXT,
            last_at                TEXT NOT NULL,
            interval_seconds       INTEGER NOT NULL DEFAULT 60,
            stuck_task_timeout_s   INTEGER NOT NULL DEFAULT 900
        )""",
        "CREATE INDEX IF NOT EXISTS idx_heartbeats_last_at ON agent_heartbeats(last_at)",
        """CREATE TABLE IF NOT EXISTS sentinel_incidents (
            id              TEXT PRIMARY KEY,
            agent_name      TEXT,
            category        TEXT NOT NULL,
            severity        TEXT NOT NULL,
            finding_kind    TEXT NOT NULL,
            summary         TEXT NOT NULL,
            details_json    TEXT,
            triage_json     TEXT,
            remediation     TEXT,
            outcome         TEXT,
            acknowledged_at TEXT,
            created_at      TEXT NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS idx_incidents_created ON sentinel_incidents(created_at)",
        "CREATE INDEX IF NOT EXISTS idx_incidents_agent ON sentinel_incidents(agent_name)",
        "CREATE INDEX IF NOT EXISTS idx_incidents_severity ON sentinel_incidents(severity)",
    ],
    # v9: Intelligence Engine — Batch 1 foundation. Strictly additive:
    # four new tables for sources, watchlist topics, fetched items,
    # and per-fetch run metadata. No existing tables touched, no
    # columns added to existing tables, no data moved or rewritten.
    # Safe to apply without affecting memory, brain, plans, or
    # cost_entries.
    9: [
        """CREATE TABLE IF NOT EXISTS intel_sources (
            id                    TEXT PRIMARY KEY,
            slug                  TEXT NOT NULL UNIQUE,
            kind                  TEXT NOT NULL,
            label                 TEXT NOT NULL,
            url                   TEXT NOT NULL,
            config_json           TEXT,
            enabled               INTEGER NOT NULL DEFAULT 1,
            default_priority      TEXT NOT NULL DEFAULT 'medium',
            project_slug          TEXT,
            poll_interval_seconds INTEGER NOT NULL DEFAULT 3600,
            last_checked_at       TEXT,
            last_status           TEXT,
            consecutive_failures  INTEGER NOT NULL DEFAULT 0,
            etag                  TEXT,
            last_modified         TEXT,
            mute_until            TEXT,
            created_at            TEXT NOT NULL,
            updated_at            TEXT NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS idx_intel_sources_kind ON intel_sources(kind)",
        "CREATE INDEX IF NOT EXISTS idx_intel_sources_enabled ON intel_sources(enabled)",
        "CREATE INDEX IF NOT EXISTS idx_intel_sources_project ON intel_sources(project_slug)",

        """CREATE TABLE IF NOT EXISTS intel_topics (
            id            TEXT PRIMARY KEY,
            slug          TEXT NOT NULL UNIQUE,
            label         TEXT NOT NULL,
            description   TEXT,
            priority      TEXT NOT NULL DEFAULT 'medium',
            project_slug  TEXT,
            keywords_json TEXT NOT NULL DEFAULT '[]',
            mute_until    TEXT,
            created_at    TEXT NOT NULL,
            updated_at    TEXT NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS idx_intel_topics_priority ON intel_topics(priority)",
        "CREATE INDEX IF NOT EXISTS idx_intel_topics_project ON intel_topics(project_slug)",

        """CREATE TABLE IF NOT EXISTS intel_items (
            id                    TEXT PRIMARY KEY,
            source_id             TEXT NOT NULL,
            external_id           TEXT,
            title                 TEXT NOT NULL,
            url                   TEXT NOT NULL,
            canonical_url         TEXT,
            published_at          TEXT,
            fetched_at            TEXT NOT NULL,
            content_hash          TEXT NOT NULL,
            raw_json              TEXT,
            summary               TEXT,
            status                TEXT NOT NULL DEFAULT 'new',
            score                 INTEGER,
            score_reason          TEXT,
            score_dimensions_json TEXT,
            brain_path            TEXT,
            FOREIGN KEY (source_id) REFERENCES intel_sources(id) ON DELETE CASCADE
        )""",
        "CREATE INDEX IF NOT EXISTS idx_intel_items_source ON intel_items(source_id)",
        "CREATE INDEX IF NOT EXISTS idx_intel_items_status ON intel_items(status)",
        "CREATE INDEX IF NOT EXISTS idx_intel_items_fetched ON intel_items(fetched_at)",
        "CREATE INDEX IF NOT EXISTS idx_intel_items_hash ON intel_items(content_hash)",
        "CREATE INDEX IF NOT EXISTS idx_intel_items_canonical ON intel_items(canonical_url)",

        """CREATE TABLE IF NOT EXISTS intel_fetch_runs (
            id            TEXT PRIMARY KEY,
            source_id     TEXT NOT NULL,
            started_at    TEXT NOT NULL,
            finished_at   TEXT,
            status        TEXT NOT NULL,
            items_seen    INTEGER NOT NULL DEFAULT 0,
            items_new     INTEGER NOT NULL DEFAULT 0,
            items_dup     INTEGER NOT NULL DEFAULT 0,
            error         TEXT,
            FOREIGN KEY (source_id) REFERENCES intel_sources(id) ON DELETE CASCADE
        )""",
        "CREATE INDEX IF NOT EXISTS idx_intel_runs_source ON intel_fetch_runs(source_id)",
        "CREATE INDEX IF NOT EXISTS idx_intel_runs_started ON intel_fetch_runs(started_at)",
    ],
    # v10: Vector brain — semantic retrieval layer over the markdown
    # vault. Strictly additive. The markdown files at ~/PILK-brain
    # remain the source of truth; these tables hold a derived index
    # that can be rebuilt from the vault at any time.
    #
    #   brain_chunks      — one row per heading-aware chunk of a note,
    #                       with content + provenance metadata
    #                       (brain_path, project_slug, source_type,
    #                       file_mtime/hash for incremental re-index).
    #   brain_embeddings  — packed float32 vector per chunk. Kept in
    #                       a separate table so a future swap to
    #                       sqlite-vec only touches embeddings.
    #
    # No mutation paths are added to existing brain tools. The only
    # new write surface is the indexer, which re-derives chunks from
    # the markdown vault.
    10: [
        """CREATE TABLE IF NOT EXISTS brain_chunks (
            id              TEXT PRIMARY KEY,
            brain_path      TEXT NOT NULL,
            chunk_idx       INTEGER NOT NULL,
            heading         TEXT,
            content         TEXT NOT NULL,
            project_slug    TEXT,
            source_type     TEXT NOT NULL,
            file_mtime      REAL NOT NULL,
            file_hash       TEXT NOT NULL,
            indexed_at      TEXT NOT NULL,
            embedding_model TEXT NOT NULL,
            UNIQUE(brain_path, chunk_idx)
        )""",
        "CREATE INDEX IF NOT EXISTS idx_brain_chunks_path ON brain_chunks(brain_path)",
        "CREATE INDEX IF NOT EXISTS idx_brain_chunks_project ON brain_chunks(project_slug)",
        "CREATE INDEX IF NOT EXISTS idx_brain_chunks_source ON brain_chunks(source_type)",
        "CREATE INDEX IF NOT EXISTS idx_brain_chunks_hash ON brain_chunks(file_hash)",
        """CREATE TABLE IF NOT EXISTS brain_embeddings (
            chunk_id        TEXT PRIMARY KEY,
            vector          BLOB NOT NULL,
            dim             INTEGER NOT NULL,
            FOREIGN KEY (chunk_id) REFERENCES brain_chunks(id) ON DELETE CASCADE
        )""",
    ],
    # v11: Proactive alerts foundation. Tiny, additive — three new
    # tables and zero changes to existing rows.
    #
    #   alert_settings_kv  — operator-tunable knobs as a key/value
    #                        store (max-per-day, min-score, quiet
    #                        hours override, digest-only mode,
    #                        telegram enable, scheduled briefs).
    #                        Singleton-style: one row per key.
    #   alerts             — event log of every alert that was
    #                        considered, with the delivery decision
    #                        (silent, digest, telegram, dashboard)
    #                        and the dedupe fingerprint that
    #                        suppressed duplicates.
    #   alert_topic_overrides — per-topic delivery mode + mute.
    #
    # ALL alert delivery defaults are conservative: Telegram and
    # scheduled briefs are OFF by default, digest-only mode is ON.
    # The first time an operator wants a Telegram ping they have to
    # explicitly enable it via settings — there is no path that
    # silently turns alerts on.
    11: [
        """CREATE TABLE IF NOT EXISTS alert_settings_kv (
            key        TEXT PRIMARY KEY,
            value      TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS alerts (
            id              TEXT PRIMARY KEY,
            kind            TEXT NOT NULL,
            severity        TEXT NOT NULL DEFAULT 'info',
            title           TEXT NOT NULL,
            body            TEXT,
            project_slug    TEXT,
            topic_slug      TEXT,
            source_slug     TEXT,
            score           INTEGER,
            dedupe_key      TEXT NOT NULL,
            delivery        TEXT NOT NULL,
            delivered_at    TEXT,
            metadata_json   TEXT,
            created_at      TEXT NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS idx_alerts_created ON alerts(created_at)",
        "CREATE INDEX IF NOT EXISTS idx_alerts_dedupe ON alerts(dedupe_key)",
        "CREATE INDEX IF NOT EXISTS idx_alerts_kind ON alerts(kind)",
        "CREATE INDEX IF NOT EXISTS idx_alerts_delivery ON alerts(delivery)",
        """CREATE TABLE IF NOT EXISTS alert_topic_overrides (
            topic_slug    TEXT PRIMARY KEY,
            mode          TEXT NOT NULL DEFAULT 'digest',
            mute_until    TEXT,
            updated_at    TEXT NOT NULL
        )""",
    ],
    # v12: File ingestion registry. One row per file the operator
    # drops into ``~/PILK/inbox/`` (or uploads via the API). Tracks
    # extraction status, the resulting brain note path, and the
    # content hash for dedup. Strictly additive.
    12: [
        """CREATE TABLE IF NOT EXISTS ingested_files (
            id                  TEXT PRIMARY KEY,
            original_path       TEXT NOT NULL,
            stored_path         TEXT,
            file_type           TEXT NOT NULL,
            project_slug        TEXT,
            content_hash        TEXT NOT NULL,
            byte_size           INTEGER NOT NULL,
            status              TEXT NOT NULL DEFAULT 'pending',
            extracted_text_path TEXT,
            brain_note_path     TEXT,
            summary             TEXT,
            error               TEXT,
            metadata_json       TEXT,
            created_at          TEXT NOT NULL,
            updated_at          TEXT NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS idx_ingest_status ON ingested_files(status)",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_ingest_hash ON ingested_files(content_hash)",
        "CREATE INDEX IF NOT EXISTS idx_ingest_project ON ingested_files(project_slug)",
        "CREATE INDEX IF NOT EXISTS idx_ingest_created ON ingested_files(created_at)",
    ],
    # v13: Workflow engine — turn repeated operations into named
    # multi-step recipes the operator can run on demand. Strictly
    # additive. Workflow definitions live in YAML files on disk
    # (loaded at boot like agent manifests); the DB only tracks
    # runs + steps + checkpoints so pause/resume/cancel works.
    13: [
        """CREATE TABLE IF NOT EXISTS workflow_runs (
            id              TEXT PRIMARY KEY,
            workflow_name   TEXT NOT NULL,
            status          TEXT NOT NULL DEFAULT 'pending',
            inputs_json     TEXT,
            current_step    INTEGER NOT NULL DEFAULT 0,
            checkpoint_json TEXT,
            error           TEXT,
            created_at      TEXT NOT NULL,
            updated_at      TEXT NOT NULL,
            finished_at     TEXT
        )""",
        "CREATE INDEX IF NOT EXISTS idx_workflow_runs_name ON workflow_runs(workflow_name)",
        "CREATE INDEX IF NOT EXISTS idx_workflow_runs_status ON workflow_runs(status)",
        "CREATE INDEX IF NOT EXISTS idx_workflow_runs_created ON workflow_runs(created_at)",
        """CREATE TABLE IF NOT EXISTS workflow_steps (
            id            TEXT PRIMARY KEY,
            run_id        TEXT NOT NULL,
            idx           INTEGER NOT NULL,
            name          TEXT NOT NULL,
            kind          TEXT NOT NULL,
            status        TEXT NOT NULL DEFAULT 'pending',
            input_json    TEXT,
            output_json   TEXT,
            error         TEXT,
            started_at    TEXT,
            finished_at   TEXT,
            FOREIGN KEY (run_id) REFERENCES workflow_runs(id) ON DELETE CASCADE
        )""",
        "CREATE INDEX IF NOT EXISTS idx_workflow_steps_run ON workflow_steps(run_id)",
    ],
}


def ensure_schema(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(SCHEMA_FILE.read_text(encoding="utf-8"))
        row = conn.execute(
            "SELECT MAX(version) FROM schema_version"
        ).fetchone()
        current = row[0] if row and row[0] is not None else 0
        for version in sorted(MIGRATIONS):
            if version <= current:
                continue
            for stmt in MIGRATIONS[version]:
                conn.execute(stmt)
            conn.execute(
                "INSERT INTO schema_version(version, applied_at) VALUES (?, ?)",
                (version, datetime.now(UTC).isoformat()),
            )
        if current < 1:
            conn.execute(
                "INSERT INTO schema_version(version, applied_at) VALUES (?, ?)",
                (1, datetime.now(UTC).isoformat()),
            )
        conn.commit()
    finally:
        conn.close()
