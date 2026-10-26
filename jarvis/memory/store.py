"""JARVIS's long-term memory.

One SQLite database in the user's data directory holding four kinds of
information:

``conversations``  every message, so the Conversation History page is real and
                   context can be rebuilt across restarts.
``facts``          durable things JARVIS learned about the user ("favourite
                   album is Ye", "keeps projects in D:\\code"), with a category,
                   a source and a use counter.
``tasks``          what JARVIS was asked to do, what it did, whether it worked
                   and how long it took - which is also what makes "do that
                   again" possible.
``shortcuts``      user-defined phrase -> plan mappings ("morning setup" ->
                   a list of steps).

Everything written here is passed through the redaction filter first: a memory
is never allowed to become a password store.  Nothing here ever leaves the
machine.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .. import paths
from ..security.redaction import contains_secret, redact_text

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL,
    ts          REAL NOT NULL,
    role        TEXT NOT NULL,
    content     TEXT NOT NULL,
    task_id     TEXT,
    meta        TEXT
);
CREATE INDEX IF NOT EXISTS idx_conv_session ON conversations(session_id, ts);

CREATE TABLE IF NOT EXISTS facts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL NOT NULL,
    category    TEXT NOT NULL,
    key         TEXT NOT NULL,
    value       TEXT NOT NULL,
    source      TEXT,
    confidence  REAL DEFAULT 0.8,
    uses        INTEGER DEFAULT 0,
    last_used   REAL DEFAULT 0,
    pinned      INTEGER DEFAULT 0,
    disabled    INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_facts_cat ON facts(category);
CREATE UNIQUE INDEX IF NOT EXISTS idx_facts_key ON facts(category, key);

CREATE TABLE IF NOT EXISTS tasks (
    id          TEXT PRIMARY KEY,
    ts          REAL NOT NULL,
    finished    REAL,
    request     TEXT NOT NULL,
    status      TEXT NOT NULL,
    summary     TEXT,
    steps       TEXT,
    duration_ms REAL,
    model       TEXT
);
CREATE INDEX IF NOT EXISTS idx_tasks_ts ON tasks(ts);

CREATE TABLE IF NOT EXISTS shortcuts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    phrase      TEXT NOT NULL UNIQUE,
    action      TEXT NOT NULL,
    ts          REAL NOT NULL,
    uses        INTEGER DEFAULT 0
);
"""

FACT_CATEGORIES = ("preference", "app", "location", "music", "person", "routine",
                   "device", "workflow", "other")


@dataclass
class Fact:
    category: str
    key: str
    value: str
    source: str = "conversation"
    confidence: float = 0.8
    uses: int = 0
    last_used: float = 0.0
    ts: float = field(default_factory=time.time)
    id: Optional[int] = None
    pinned: bool = False
    #: The user can switch a memory off without deleting it.  Disabled facts are
    #: never offered to the model.
    disabled: bool = False

    def as_dict(self) -> Dict[str, Any]:
        return {"id": self.id, "category": self.category, "key": self.key,
                "value": self.value, "source": self.source,
                "confidence": round(float(self.confidence), 2), "uses": self.uses,
                "last_used": self.last_used, "ts": self.ts, "pinned": self.pinned,
                "disabled": self.disabled}


@dataclass
class TaskRecord:
    id: str
    ts: float
    request: str
    status: str = "running"
    summary: str = ""
    steps: List[Dict[str, Any]] = field(default_factory=list)
    duration_ms: float = 0.0
    finished: Optional[float] = None
    model: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {"id": self.id, "ts": self.ts, "finished": self.finished,
                "request": self.request, "status": self.status,
                "summary": self.summary, "steps": self.steps,
                "duration_ms": round(self.duration_ms, 1), "model": self.model}


class MemoryStore:
    def __init__(self, db_path: Optional[os.PathLike] = None,
                 settings_provider: Optional[Any] = None) -> None:
        self.db_path = str(db_path or paths.memory_db())
        self._settings_provider = settings_provider
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(SCHEMA)
        self._conn.commit()
        self._migrate()
        self._apply_retention()

    # ------------------------------------------------------------------
    def _migrate(self) -> None:
        """Add columns introduced after a user's database already existed."""
        wanted = {"facts": {"disabled": "INTEGER DEFAULT 0"}}
        for table, columns in wanted.items():
            try:
                existing = {row[1] for row in
                            self._conn.execute(f"PRAGMA table_info({table})")}
            except sqlite3.Error:
                continue
            for column, definition in columns.items():
                if column in existing:
                    continue
                try:
                    self._conn.execute(
                        f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
                    self._conn.commit()
                    log.info("Migrated %s: added column %s", table, column)
                except sqlite3.Error as exc:
                    log.warning("Could not add %s.%s: %s", table, column, exc)

    # ------------------------------------------------------------------
    @property
    def settings(self) -> Any:
        if self._settings_provider is None:
            return None
        try:
            return self._settings_provider()
        except Exception:  # noqa: BLE001
            return None

    @property
    def enabled(self) -> bool:
        s = self.settings
        if s is None:
            return True
        return bool(getattr(s.memory, "enabled", True))

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass

    # ------------------------------------------------------------------
    # conversations
    # ------------------------------------------------------------------
    def add_message(self, session_id: str, role: str, content: str,
                    task_id: str = "", meta: Optional[Dict[str, Any]] = None) -> int:
        if not self.enabled or not self.settings_conversations_enabled():
            return -1
        safe = self._safe(content)
        if safe is None:
            log.info("Refusing to store a message that looks like it contains a "
                     "credential.")
            safe = "[redacted: message contained a credential]"
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO conversations (session_id, ts, role, content, task_id, meta)"
                " VALUES (?,?,?,?,?,?)",
                (session_id, time.time(), role, safe, task_id,
                 json.dumps(meta or {}, ensure_ascii=False)[:2000]))
            self._conn.commit()
            return int(cur.lastrowid or -1)

    def settings_conversations_enabled(self) -> bool:
        s = self.settings
        if s is None:
            return True
        return bool(getattr(s.memory, "remember_conversations", True))

    def history(self, session_id: Optional[str] = None, limit: int = 50,
                since: Optional[float] = None) -> List[Dict[str, Any]]:
        with self._lock:
            if session_id and since:
                cur = self._conn.execute(
                    "SELECT * FROM conversations WHERE session_id=? AND ts>=? "
                    "ORDER BY ts DESC LIMIT ?", (session_id, since, limit))
            elif session_id:
                cur = self._conn.execute(
                    "SELECT * FROM conversations WHERE session_id=? ORDER BY ts DESC "
                    "LIMIT ?", (session_id, limit))
            else:
                cur = self._conn.execute(
                    "SELECT * FROM conversations ORDER BY ts DESC LIMIT ?", (limit,))
            rows = [dict(r) for r in cur.fetchall()]
        rows.reverse()
        for r in rows:
            try:
                r["meta"] = json.loads(r.get("meta") or "{}")
            except json.JSONDecodeError:
                r["meta"] = {}
        return rows

    def sessions(self, limit: int = 60) -> List[Dict[str, Any]]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT session_id, COUNT(*) messages, MIN(ts) started, MAX(ts) ended,"
                " (SELECT content FROM conversations c2 WHERE c2.session_id=c.session_id"
                "  AND c2.role='user' ORDER BY ts LIMIT 1) first_user_message"
                " FROM conversations c GROUP BY session_id ORDER BY ended DESC LIMIT ?",
                (limit,))
            return [dict(r) for r in cur.fetchall()]

    def clear_history(self, session_id: Optional[str] = None) -> int:
        with self._lock:
            if session_id:
                cur = self._conn.execute(
                    "DELETE FROM conversations WHERE session_id=?", (session_id,))
            else:
                cur = self._conn.execute("DELETE FROM conversations")
            self._conn.commit()
            return cur.rowcount or 0

    # ------------------------------------------------------------------
    # facts
    # ------------------------------------------------------------------
    def add_fact(self, category: str, key: str, value: str, source: str = "conversation",
                 confidence: float = 0.8, pinned: bool = False) -> Optional[Fact]:
        if not self.enabled:
            return None
        value = self._safe(value)
        key = self._safe(key) or ""
        if value is None:
            log.info("Refusing to remember a value that looks like a credential.")
            return None
        category = (category or "other").lower()
        if category not in FACT_CATEGORIES:
            category = "other"
        key = (key or value)[:160]
        with self._lock:
            self._conn.execute(
                """INSERT INTO facts (ts, category, key, value, source, confidence,
                                      uses, last_used, pinned)
                   VALUES (?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(category, key) DO UPDATE SET
                     value=excluded.value, source=excluded.source,
                     confidence=excluded.confidence, ts=excluded.ts,
                     pinned=MAX(pinned, excluded.pinned)""",
                (time.time(), category, key, value, source, float(confidence),
                 1, time.time(), 1 if pinned else 0))
            self._conn.commit()
            cur = self._conn.execute(
                "SELECT * FROM facts WHERE category=? AND key=?", (category, key))
            row = cur.fetchone()
        return self._row_to_fact(row) if row else None

    def _row_to_fact(self, row: sqlite3.Row) -> Fact:
        d = dict(row)
        return Fact(id=d.get("id"), category=d.get("category", "other"),
                    key=d.get("key", ""), value=d.get("value", ""),
                    source=d.get("source", ""), confidence=float(d.get("confidence", 0.8)),
                    uses=int(d.get("uses", 0)), last_used=float(d.get("last_used", 0)),
                    ts=float(d.get("ts", 0)), pinned=bool(d.get("pinned")),
                    disabled=bool(d.get("disabled")))

    def list_facts(self, category: str = "", limit: int = 200,
                   include_disabled: bool = False) -> List[Fact]:
        """Facts the model may use.  Disabled ones are hidden unless asked for
        (the Memory page shows everything so the user can switch a memory back on)."""
        where = []
        params: List[Any] = []
        if category:
            where.append("category=?")
            params.append(category)
        if not include_disabled:
            where.append("COALESCE(disabled, 0)=0")
        sql = "SELECT * FROM facts"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY pinned DESC, last_used DESC, ts DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            cur = self._conn.execute(sql, tuple(params))
            return [self._row_to_fact(r) for r in cur.fetchall()]

    def set_fact_enabled(self, fact_id: int, enabled: bool) -> bool:
        """Switch one memory on or off without deleting it."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE facts SET disabled=? WHERE id=?",
                (0 if enabled else 1, int(fact_id)))
            self._conn.commit()
            return (cur.rowcount or 0) > 0

    def search_facts(self, query: str, limit: int = 8) -> List[Fact]:
        q = (query or "").strip().lower()
        facts = self.list_facts(limit=500)
        if not q:
            return facts[:limit]
        scored: List[Tuple[float, Fact]] = []
        tokens = [t for t in re.split(r"[^a-z0-9]+", q) if t]
        for f in facts:
            hay = f"{f.key} {f.value} {f.category}".lower()
            score = 0.0
            if q in hay:
                score += 2.0
            for t in tokens:
                if t and t in hay:
                    score += 1.0
            if score:
                score += min(1.0, f.uses / 10.0) + (0.5 if f.pinned else 0.0)
                scored.append((score, f))
        scored.sort(key=lambda t: -t[0])
        out = [f for _s, f in scored[:limit]]
        for f in out:
            self.touch_fact(f.id)
        return out

    def touch_fact(self, fact_id: Optional[int]) -> None:
        if not fact_id:
            return
        with self._lock:
            self._conn.execute(
                "UPDATE facts SET uses = uses + 1, last_used = ? WHERE id = ?",
                (time.time(), int(fact_id)))
            self._conn.commit()

    def update_fact(self, fact_id: int, value: Optional[str] = None,
                    category: Optional[str] = None, key: Optional[str] = None,
                    pinned: Optional[bool] = None) -> bool:
        safe_value = self._safe(value) if value is not None else None
        if value is not None and safe_value is None:
            return False
        sets, params = [], []
        if safe_value is not None:
            sets.append("value=?"); params.append(safe_value)
        if category:
            sets.append("category=?"); params.append(category)
        if key:
            sets.append("key=?"); params.append(self._safe(key) or key)
        if pinned is not None:
            sets.append("pinned=?"); params.append(1 if pinned else 0)
        if not sets:
            return False
        params.append(int(fact_id))
        with self._lock:
            cur = self._conn.execute(
                f"UPDATE facts SET {', '.join(sets)} WHERE id=?", params)
            self._conn.commit()
            return (cur.rowcount or 0) > 0

    def delete_fact(self, fact_id: int) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM facts WHERE id=?", (int(fact_id),))
            self._conn.commit()
            return (cur.rowcount or 0) > 0

    def clear_facts(self, category: str = "") -> int:
        with self._lock:
            if category:
                cur = self._conn.execute("DELETE FROM facts WHERE category=?",
                                         (category,))
            else:
                cur = self._conn.execute("DELETE FROM facts")
            self._conn.commit()
            return cur.rowcount or 0

    # ------------------------------------------------------------------
    # tasks
    # ------------------------------------------------------------------
    def start_task(self, request: str, model: str = "") -> TaskRecord:
        rec = TaskRecord(id=uuid.uuid4().hex[:12], ts=time.time(),
                         request=self._safe(request) or request, status="running",
                         model=model)
        with self._lock:
            self._conn.execute(
                "INSERT INTO tasks (id, ts, request, status, summary, steps,"
                " duration_ms, model) VALUES (?,?,?,?,?,?,?,?)",
                (rec.id, rec.ts, rec.request, rec.status, "", "[]", 0.0, model))
            self._conn.commit()
        return rec

    def finish_task(self, task_id: str, status: str, summary: str = "",
                    steps: Optional[Sequence[Dict[str, Any]]] = None,
                    duration_ms: float = 0.0) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE tasks SET finished=?, status=?, summary=?, steps=?,"
                " duration_ms=? WHERE id=?",
                (time.time(), status, self._safe(summary) or summary,
                 json.dumps(list(steps or []), ensure_ascii=False)[:200000],
                 float(duration_ms), task_id))
            self._conn.commit()
            return (cur.rowcount or 0) > 0

    def list_tasks(self, limit: int = 50, status: str = "") -> List[TaskRecord]:
        with self._lock:
            if status:
                cur = self._conn.execute(
                    "SELECT * FROM tasks WHERE status=? ORDER BY ts DESC LIMIT ?",
                    (status, limit))
            else:
                cur = self._conn.execute(
                    "SELECT * FROM tasks ORDER BY ts DESC LIMIT ?", (limit,))
            rows = cur.fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                steps = json.loads(d.get("steps") or "[]")
            except json.JSONDecodeError:
                steps = []
            out.append(TaskRecord(id=d["id"], ts=d["ts"], finished=d.get("finished"),
                                  request=d.get("request", ""),
                                  status=d.get("status", ""),
                                  summary=d.get("summary", ""), steps=steps,
                                  duration_ms=float(d.get("duration_ms") or 0.0),
                                  model=d.get("model", "")))
        return out

    def get_task(self, task_id: str) -> Optional[TaskRecord]:
        for t in self.list_tasks(limit=1000):
            if t.id == task_id:
                return t
        return None

    def similar_tasks(self, request: str, limit: int = 3) -> List[TaskRecord]:
        """Find previous tasks with a similar request ('do that again')."""
        q = re.sub(r"[^a-z0-9 ]+", "", (request or "").lower())
        q_tokens = {t for t in q.split() if len(t) > 2}
        if not q_tokens:
            return []
        scored = []
        for t in self.list_tasks(limit=200):
            hay = re.sub(r"[^a-z0-9 ]+", "", (t.request or "").lower())
            tokens = {w for w in hay.split() if len(w) > 2}
            if not tokens:
                continue
            overlap = len(q_tokens & tokens) / max(1, len(q_tokens | tokens))
            if overlap >= 0.45:
                scored.append((overlap, t))
        scored.sort(key=lambda x: -x[0])
        return [t for _s, t in scored[:limit]]

    # ------------------------------------------------------------------
    # shortcuts
    # ------------------------------------------------------------------
    def add_shortcut(self, phrase: str, action: Any) -> bool:
        if not self.enabled:
            return False
        phrase = (phrase or "").strip().lower()
        if not phrase:
            return False
        payload = action if isinstance(action, str) else json.dumps(action,
                                                                    ensure_ascii=False)
        with self._lock:
            self._conn.execute(
                "INSERT INTO shortcuts (phrase, action, ts, uses) VALUES (?,?,?,0)"
                " ON CONFLICT(phrase) DO UPDATE SET action=excluded.action,"
                " ts=excluded.ts", (phrase, payload, time.time()))
            self._conn.commit()
        return True

    def shortcuts(self) -> List[Dict[str, Any]]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM shortcuts ORDER BY uses DESC, phrase LIMIT 200")
            out = []
            for r in cur.fetchall():
                d = dict(r)
                try:
                    action = json.loads(d["action"])
                except (json.JSONDecodeError, TypeError):
                    action = d["action"]
                out.append({"id": d["id"], "phrase": d["phrase"], "action": action,
                            "uses": d["uses"]})
            return out

    def match_shortcut(self, text: str) -> Optional[Dict[str, Any]]:
        low = re.sub(r"[^a-z0-9 ]+", "", (text or "").lower()).strip()
        if not low:
            return None
        best = None
        for sc in self.shortcuts():
            phrase = re.sub(r"[^a-z0-9 ]+", "", sc["phrase"].lower()).strip()
            if not phrase:
                continue
            if low == phrase or low.startswith(phrase + " ") or low.endswith(" " + phrase):
                best = sc
                break
            if phrase in low and len(phrase) >= 6:
                best = best or sc
        if best is not None:
            with self._lock:
                self._conn.execute(
                    "UPDATE shortcuts SET uses = uses + 1 WHERE id=?", (best["id"],))
                self._conn.commit()
        return best

    def delete_shortcut(self, shortcut_id: int) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM shortcuts WHERE id=?",
                                     (int(shortcut_id),))
            self._conn.commit()
            return (cur.rowcount or 0) > 0

    # ------------------------------------------------------------------
    # prompt context
    # ------------------------------------------------------------------
    def context_block(self, session_id: str, query: str = "",
                      max_messages: int = 24, max_chars: int = 24000,
                      max_facts: int = 12) -> Dict[str, Any]:
        """Build the memory slice that goes into the model prompt."""
        if not self.enabled:
            return {"messages": [], "facts_text": "", "tasks_text": "",
                    "shortcuts_text": "", "disabled": True}
        messages = [
            {"role": m["role"], "content": m["content"], "ts": m["ts"]}
            for m in self.history(session_id=session_id, limit=max_messages)
        ]
        facts = self.search_facts(query, limit=max_facts) if query else \
            self.list_facts(limit=max_facts)
        facts_text = ""
        if facts:
            facts_text = "\n".join(
                f"- [{f.category}] {f.key}: {f.value[:180]}" for f in facts)
        tasks = self.similar_tasks(query, limit=3) if query else []
        tasks_text = ""
        if tasks:
            tasks_text = "\n".join(
                f"- \"{t.request}\" -> {t.status}"
                + (f": {t.summary[:160]}" if t.summary else "") for t in tasks)
        sc = self.match_shortcut(query) if query else None
        shortcuts_text = ""
        if sc is not None:
            action = sc["action"]
            shortcuts_text = (f"User-defined shortcut '{sc['phrase']}' = "
                              f"{json.dumps(action, ensure_ascii=False)[:600]}")
        total = sum(len(m["content"]) for m in messages)
        while total > max_chars and messages:
            removed = messages.pop(0)
            total -= len(removed["content"])
        return {"messages": messages, "facts_text": facts_text,
                "tasks_text": tasks_text, "shortcuts_text": shortcuts_text,
                "disabled": False}

    # ------------------------------------------------------------------
    def stats(self) -> Dict[str, Any]:
        with self._lock:
            def count(sql: str, args: Tuple = ()) -> int:
                try:
                    return int(self._conn.execute(sql, args).fetchone()[0])
                except (sqlite3.Error, TypeError, IndexError):
                    return 0

            size = 0
            try:
                size = os.path.getsize(self.db_path)
            except OSError:
                pass
            return {
                "enabled": self.enabled,
                "db_path": self.db_path,
                "db_size_kb": round(size / 1024.0, 1),
                "messages": count("SELECT COUNT(*) FROM conversations"),
                "sessions": count("SELECT COUNT(DISTINCT session_id) FROM conversations"),
                "facts": count("SELECT COUNT(*) FROM facts WHERE COALESCE(disabled,0)=0"),
                "facts_disabled": count(
                    "SELECT COUNT(*) FROM facts WHERE COALESCE(disabled,0)=1"),
                "tasks": count("SELECT COUNT(*) FROM tasks"),
                "shortcuts": count("SELECT COUNT(*) FROM shortcuts"),
            }

    def clear_all(self, keep_facts: bool = False) -> Dict[str, int]:
        out = {}
        with self._lock:
            out["messages"] = self.clear_history()
            if not keep_facts:
                out["facts"] = self.clear_facts()
            cur = self._conn.execute("DELETE FROM tasks")
            out["tasks"] = cur.rowcount or 0
            cur = self._conn.execute("DELETE FROM shortcuts")
            out["shortcuts"] = cur.rowcount or 0
            self._conn.commit()
        return out

    def export_json(self, dest: Optional[os.PathLike] = None) -> str:
        payload = {
            "exported_at": time.time(),
            "facts": [f.as_dict() for f in self.list_facts(limit=5000)],
            "shortcuts": self.shortcuts(),
            "tasks": [t.as_dict() for t in self.list_tasks(limit=1000)],
            "conversations": self.history(limit=5000),
            "note": "Local-only export. No credentials are stored here by design.",
        }
        path = str(dest or (paths.data_dir() / "memory-export.json"))
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=False)
        return path

    def _apply_retention(self) -> None:
        s = self.settings
        days = int(getattr(getattr(s, "memory", None), "retention_days", 0) or 0) \
            if s is not None else 0
        if days <= 0:
            return
        cutoff = time.time() - days * 86400
        with self._lock:
            try:
                self._conn.execute("DELETE FROM conversations WHERE ts < ?", (cutoff,))
                self._conn.execute(
                    "DELETE FROM tasks WHERE ts < ? AND status != 'running'", (cutoff,))
                self._conn.commit()
            except sqlite3.Error:
                log.debug("retention prune failed", exc_info=True)

    # ------------------------------------------------------------------
    def _safe(self, text: Any) -> Optional[str]:
        """Redact secrets, or refuse the value outright when it *is* a secret.

        Short values that still look credential-shaped after redaction are
        dropped completely - JARVIS would rather remember nothing than become a
        password store.  Longer text keeps its useful parts with the secrets
        replaced by ``[REDACTED]``.
        """
        if text is None:
            return None
        raw = str(text)
        try:
            redact = bool(self.settings.memory.redact_secrets)
        except Exception:  # noqa: BLE001
            redact = True          # no settings: redact, never the other way
        if not redact:
            # The user turned redaction off, but JARVIS still refuses to *become*
            # a password store: an obvious credential is dropped, not stored raw.
            return None if contains_secret(raw) and len(raw) < 200 else raw
        redacted = redact_text(raw)
        if contains_secret(redacted) and len(redacted) < 200:
            return None
        return redacted
