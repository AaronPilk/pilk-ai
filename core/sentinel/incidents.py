"""Persistent incident store + ``incidents.jsonl`` mirror.

Two writers for one truth:

* SQLite ``sentinel_incidents`` — queryable, what the UI + tools read.
* ``<home>/sentinel/incidents.jsonl`` — append-only, operator-friendly
  (`tail -f`, `grep`, external log shippers). Lossy mirror: if the
  filesystem is out of space the SQLite write still succeeds and the
  jsonl append is skipped with a warning.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path

from core.logging import get_logger
from core.sentinel.contracts import (
    Category,
    Finding,
    Incident,
    Severity,
    TriageResult,
)

log = get_logger("pilkd.sentinel.incidents")


class IncidentStore:
    def __init__(self, db_path: Path, jsonl_path: Path | None = None) -> None:
        self._db_path = db_path
        self._jsonl_path = jsonl_path

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def create(
        self,
        *,
        finding: Finding,
        triage: TriageResult | None,
        category: Category,
        severity: Severity,
        remediation: str | None = None,
        outcome: str | None = None,
    ) -> Incident:
        incident = Incident(
            id=f"inc-{uuid.uuid4().hex[:12]}",
            agent_name=finding.agent_name,
            category=category,
            severity=severity,
            finding_kind=finding.kind,
            summary=finding.summary,
            details=dict(finding.details),
            triage=triage,
            remediation=remediation,
            outcome=outcome,
            acknowledged_at=None,
            created_at=datetime.now(UTC).isoformat(),
        )
        self._write_sql(incident)
        self._write_jsonl(incident)
        return incident

    def update_outcome(
        self, incident_id: str, *, remediation: str | None, outcome: str
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                """UPDATE sentinel_incidents
                   SET remediation = ?, outcome = ?
                   WHERE id = ?""",
                (remediation, outcome, incident_id),
            )
            conn.commit()

    def acknowledge(self, incident_id: str) -> bool:
        now = datetime.now(UTC).isoformat()
        with self._conn() as conn:
            cur = conn.execute(
                """UPDATE sentinel_incidents
                   SET acknowledged_at = ?
                   WHERE id = ? AND acknowledged_at IS NULL""",
                (now, incident_id),
            )
            conn.commit()
            return cur.rowcount > 0

    def recent(
        self,
        *,
        limit: int = 50,
        agent_name: str | None = None,
        min_severity: Severity | None = None,
        only_unacked: bool = False,
    ) -> list[Incident]:
        where: list[str] = []
        params: list[object] = []
        if agent_name:
            where.append("agent_name = ?")
            params.append(agent_name)
        if min_severity is not None:
            allowed = [s.value for s in Severity if s.rank() >= min_severity.rank()]
            where.append(
                "severity IN (" + ",".join("?" * len(allowed)) + ")"
            )
            params.extend(allowed)
        if only_unacked:
            where.append("acknowledged_at IS NULL")
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        with self._conn() as conn:
            rows = conn.execute(
                f"""SELECT id, agent_name, category, severity, finding_kind,
                           summary, details_json, triage_json, remediation,
                           outcome, acknowledged_at, created_at
                    FROM sentinel_incidents{clause}
                    ORDER BY created_at DESC
                    LIMIT ?""",
                (*params, int(limit)),
            ).fetchall()
        return [_row(r) for r in rows]

    # ── private ──

    def _write_sql(self, inc: Incident) -> None:
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO sentinel_incidents(
                       id, agent_name, category, severity, finding_kind,
                       summary, details_json, triage_json, remediation,
                       outcome, acknowledged_at, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    inc.id,
                    inc.agent_name,
                    inc.category.value,
                    inc.severity.value,
                    inc.finding_kind,
                    inc.summary,
                    json.dumps(inc.details, default=str),
                    json.dumps(_triage_dict(inc.triage)) if inc.triage else None,
                    inc.remediation,
                    inc.outcome,
                    inc.acknowledged_at,
                    inc.created_at,
                ),
            )
            conn.commit()

    def _write_jsonl(self, inc: Incident) -> None:
        if self._jsonl_path is None:
            return
        try:
            self._jsonl_path.parent.mkdir(parents=True, exist_ok=True)
            with self._jsonl_path.open("a", encoding="utf-8") as f:
                f.write(
                    json.dumps(
                        {
                            "id": inc.id,
                            "at": inc.created_at,
                            "agent": inc.agent_name,
                            "category": inc.category.value,
                            "severity": inc.severity.value,
                            "kind": inc.finding_kind,
                            "summary": inc.summary,
                            "triage": _triage_dict(inc.triage) if inc.triage else None,
                            "remediation": inc.remediation,
                            "outcome": inc.outcome,
                        },
                        default=str,
                    )
                    + "\n"
                )
        except OSError as e:
            # Don't let a full disk drop the incident — SQLite already
            # has it. Just log and move on.
            log.warning("incidents_jsonl_write_failed", error=str(e))


def _row(row: tuple) -> Incident:
    details = json.loads(row[6]) if row[6] else {}
    triage = _triage_from_json(row[7]) if row[7] else None
    return Incident(
        id=row[0],
        agent_name=row[1],
        category=Category.parse(row[2]),
        severity=Severity.parse(row[3]),
        finding_kind=row[4],
        summary=row[5],
        details=details,
        triage=triage,
        remediation=row[8],
        outcome=row[9],
        acknowledged_at=row[10],
        created_at=row[11],
    )


def _triage_dict(t: TriageResult) -> dict[str, object]:
    return {
        "severity": t.severity.value,
        "category": t.category.value,
        "likely_cause": t.likely_cause,
        "recommended_action": t.recommended_action,
        "confidence": t.confidence,
    }


def _triage_from_json(raw: str) -> TriageResult:
    d = json.loads(raw)
    return TriageResult(
        severity=Severity.parse(d.get("severity", "med")),
        category=Category.parse(d.get("category", "unknown")),
        likely_cause=str(d.get("likely_cause", "")),
        recommended_action=str(d.get("recommended_action", "")),
        confidence=float(d.get("confidence", 1.0)),
    )


__all__ = ["IncidentStore"]
