"""Local, append-only audit log for permission decisions and actions.

Stored in SQLite next to the memory database.  Everything written here passes
through :func:`jarvis.security.redaction.redact_dict`, so the audit trail never
contains a password, token or card number - only the fact that an action was
requested, who approved it and what the decision was.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from . import redaction
from .. import paths

log = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL    NOT NULL,
    task_id     TEXT,
    tool        TEXT    NOT NULL,
    category    TEXT,
    risk        TEXT    NOT NULL,
    rule        TEXT,
    decision    TEXT    NOT NULL,
    target      TEXT,
    reason      TEXT,
    args        TEXT,
    actor       TEXT    DEFAULT 'ai',
    duration_ms INTEGER,
    success     INTEGER
);
CREATE INDEX IF NOT EXISTS idx_audit_ts   ON audit_log(ts);
CREATE INDEX IF NOT EXISTS idx_audit_tool ON audit_log(tool);
CREATE INDEX IF NOT EXISTS idx_audit_risk ON audit_log(risk);
"""


@dataclass
class AuditEntry:
    ts: float
    tool: str
    decision: str          # allowed | approved | denied | blocked | error
    risk: str
    rule: str = ""
    category: str = ""
    target: str = ""
    reason: str = ""
    args: Dict[str, Any] = field(default_factory=dict)
    task_id: str = ""
    actor: str = "ai"
    duration_ms: Optional[int] = None
    success: Optional[bool] = None
    id: Optional[int] = None

    def to_row(self) -> Dict[str, Any]:
        d = asdict(self)
        d["args"] = json.dumps(redaction.redact_dict(self.args), ensure_ascii=False)[:4000]
        d["reason"] = redaction.redact_text(self.reason)[:500]
        d["target"] = redaction.redact_text(self.target)[:400]
        return d


class AuditLog:
    def __init__(self, db_path: Optional[os.PathLike] = None,
                 enabled: bool = True, retention_days: int = 30) -> None:
        self.db_path = str(db_path or (paths.data_dir() / "audit.db"))
        self.enabled = enabled
        self.retention_days = max(0, int(retention_days))
        self._lock = threading.RLock()
        self._conn: Optional[sqlite3.Connection] = None
        self._memory: List[AuditEntry] = []
        self._init_db()

    # ------------------------------------------------------------------
    def _init_db(self) -> None:
        if not self.enabled:
            return
        try:
            os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
            self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.executescript(_SCHEMA)
            self._conn.commit()
            self.prune()
        except sqlite3.Error as exc:
            log.error("Audit log unavailable (%s) - continuing in memory only", exc)
            self._conn = None

    def record(self, entry: AuditEntry) -> AuditEntry:
        if not self.enabled:
            return entry
        with self._lock:
            self._memory.append(entry)
            if len(self._memory) > 2000:
                del self._memory[:-2000]
            if self._conn is None:
                return entry
            row = entry.to_row()
            try:
                cur = self._conn.execute(
                    """INSERT INTO audit_log
                       (ts, task_id, tool, category, risk, rule, decision,
                        target, reason, args, actor, duration_ms, success)
                       VALUES (:ts, :task_id, :tool, :category, :risk, :rule,
                               :decision, :target, :reason, :args, :actor,
                               :duration_ms, :success)""",
                    row,
                )
                entry.id = cur.lastrowid
                self._conn.commit()
            except sqlite3.Error:
                log.exception("Could not persist audit entry")
            return entry

    # ------------------------------------------------------------------
    def log_decision(self, tool: str, risk: str, decision: str, rule: str = "",
                     reason: str = "", category: str = "", target: str = "",
                     args: Optional[Dict[str, Any]] = None, task_id: str = "",
                     actor: str = "ai", duration_ms: Optional[int] = None,
                     success: Optional[bool] = None) -> AuditEntry:
        return self.record(AuditEntry(
            ts=time.time(), tool=tool, decision=decision, risk=risk, rule=rule,
            category=category, target=target, reason=reason, args=args or {},
            task_id=task_id, actor=actor, duration_ms=duration_ms, success=success,
        ))

    def recent(self, limit: int = 100, only: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._lock:
            if self._conn is not None:
                try:
                    if only:
                        cur = self._conn.execute(
                            "SELECT * FROM audit_log WHERE risk = ? ORDER BY id DESC LIMIT ?",
                            (only, limit))
                    else:
                        cur = self._conn.execute(
                            "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,))
                    return [dict(r) for r in cur.fetchall()]
                except sqlite3.Error:
                    log.exception("Audit query failed")
            entries = list(reversed(self._memory))
            if only:
                entries = [e for e in entries if e.risk == only]
            return [e.to_row() | {"id": e.id} for e in entries[:limit]]

    def stats(self) -> Dict[str, int]:
        with self._lock:
            out = {"allowed": 0, "approved": 0, "denied": 0, "blocked": 0, "error": 0}
            if self._conn is not None:
                try:
                    for row in self._conn.execute(
                            "SELECT decision, COUNT(*) c FROM audit_log GROUP BY decision"):
                        out[row["decision"]] = row["c"]
                    return out
                except sqlite3.Error:
                    pass
            for e in self._memory:
                out[e.decision] = out.get(e.decision, 0) + 1
            return out

    def prune(self) -> int:
        if not self.retention_days or self._conn is None:
            return 0
        cutoff = time.time() - self.retention_days * 86400
        with self._lock:
            try:
                cur = self._conn.execute("DELETE FROM audit_log WHERE ts < ?", (cutoff,))
                self._conn.commit()
                return cur.rowcount or 0
            except sqlite3.Error:
                log.exception("Audit prune failed")
                return 0

    def clear(self) -> int:
        with self._lock:
            n = len(self._memory)
            self._memory.clear()
            if self._conn is not None:
                try:
                    cur = self._conn.execute("DELETE FROM audit_log")
                    self._conn.commit()
                    n += cur.rowcount or 0
                except sqlite3.Error:
                    log.exception("Audit clear failed")
            return n

    def export_csv(self, dest: os.PathLike) -> str:
        import csv

        rows = self.recent(limit=100000)
        with open(dest, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(
                fh, fieldnames=["id", "ts", "tool", "category", "risk", "rule",
                                "decision", "target", "reason", "actor",
                                "duration_ms", "success", "args"])
            writer.writeheader()
            for r in rows:
                writer.writerow({k: r.get(k, "") for k in writer.fieldnames})
        return str(dest)

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                finally:
                    self._conn = None
