"""Append-only run trace: JSONL for humans/streaming, SQLite for querying.

Every thought, tool call, observation, screenshot, approval and error lands here.
Nothing is ever rewritten — the replay view and the eval harness both read this,
so it has to be a faithful record rather than a pretty one.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from common.logging_setup import get_logger

log = get_logger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id        TEXT PRIMARY KEY,
    goal          TEXT NOT NULL,
    status        TEXT NOT NULL,
    provider      TEXT NOT NULL DEFAULT '',
    model         TEXT NOT NULL DEFAULT '',
    started_at    REAL NOT NULL,
    ended_at      REAL,
    steps         INTEGER NOT NULL DEFAULT 0,
    retries       INTEGER NOT NULL DEFAULT 0,
    input_tokens  INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    repairs       INTEGER NOT NULL DEFAULT 0,
    report_json   TEXT,
    criteria_json TEXT,
    config_json   TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id     TEXT NOT NULL,
    seq        INTEGER NOT NULL,
    ts         REAL NOT NULL,
    kind       TEXT NOT NULL,
    step       INTEGER NOT NULL DEFAULT 0,
    tool       TEXT,
    args_json  TEXT,
    obs_json   TEXT,
    text       TEXT,
    duration_ms INTEGER,
    evidence_json TEXT,
    error      TEXT
);

CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id, seq);
CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status);
"""

OBSERVATION_TRUNCATE = 2000


@dataclass
class TraceEvent:
    kind: str
    step: int = 0
    tool: str | None = None
    args: dict[str, Any] | None = None
    observation: Any = None
    text: str | None = None
    duration_ms: int | None = None
    evidence: list[str] = field(default_factory=list)
    error: str | None = None
    payload: dict[str, Any] | None = None


class Tracer:
    """One per run. Owns a JSONL file plus rows in the shared SQLite trace db."""

    def __init__(
        self,
        *,
        run_id: str,
        goal: str,
        run_dir: Path,
        db_path: Path,
        provider: str = "",
        model: str = "",
        config: dict[str, Any] | None = None,
    ) -> None:
        self.run_id = run_id
        self.goal = goal
        self.run_dir = Path(run_dir)
        self.db_path = Path(db_path)
        self.provider = provider
        self.model = model
        self.config = config or {}
        self.run_dir.mkdir(parents=True, exist_ok=True)
        (self.run_dir / "screenshots").mkdir(parents=True, exist_ok=True)
        (self.run_dir / "downloads").mkdir(parents=True, exist_ok=True)

        self._jsonl_path = self.run_dir / "trace.jsonl"
        self._jsonl = self._jsonl_path.open("a", encoding="utf-8")
        self._lock = threading.Lock()
        self._seq = 0
        self.started_at = time.time()
        self.input_tokens = 0
        self.output_tokens = 0
        self.retries = 0

        self._init_db()
        self._insert_run_row()

    # -- sqlite -----------------------------------------------------------
    def _init_db(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.executescript(SCHEMA)
        conn.commit()
        conn.close()

    def _insert_run_row(self) -> None:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.execute(
            "INSERT OR REPLACE INTO runs (run_id, goal, status, provider, model, started_at, config_json)"
            " VALUES (?,?,?,?,?,?,?)",
            (self.run_id, self.goal, "running", self.provider, self.model,
             self.started_at, json.dumps(self.config, default=str)),
        )
        conn.commit()
        conn.close()

    # -- writing ----------------------------------------------------------
    def emit(self, event: TraceEvent, sink: Any = None) -> int:
        with self._lock:
            self._seq += 1
            seq = self._seq
            record = {
                "run_id": self.run_id,
                "seq": seq,
                "ts": time.time(),
                "step": event.step,
                "kind": event.kind,
                "tool": event.tool,
                "args": event.args,
                "observation": _truncate_obj(event.observation, OBSERVATION_TRUNCATE),
                "text": event.text,
                "duration_ms": event.duration_ms,
                "evidence": event.evidence,
                "error": event.error,
            }
            self._jsonl.write(json.dumps(record, default=str) + "\n")
            self._jsonl.flush()

            conn = sqlite3.connect(self.db_path, check_same_thread=False)
            conn.execute(
                "INSERT INTO events (run_id, seq, ts, kind, step, tool, args_json, obs_json,"
                " text, duration_ms, evidence_json, error)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    self.run_id, seq, record["ts"], event.kind, event.step, event.tool,
                    json.dumps(event.args, default=str) if event.args is not None else None,
                    json.dumps(record["observation"], default=str) if event.observation is not None else None,
                    event.text, event.duration_ms,
                    json.dumps(event.evidence) if event.evidence else None, event.error,
                ),
            )
            conn.commit()
            conn.close()

        if sink is not None:
            try:
                sink(record)
            except Exception as exc:  # pragma: no cover - UI must not break the run
                log.warning("trace sink error: %s", exc)
        return seq

    def add_tokens(self, input_tokens: int, output_tokens: int) -> None:
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens

    def bump_retry(self) -> None:
        self.retries += 1

    def finish(
        self,
        *,
        status: str,
        steps: int,
        repairs: int = 0,
        report: dict[str, Any] | None = None,
        criteria: list[dict[str, Any]] | None = None,
    ) -> None:
        self._jsonl.flush()
        self._jsonl.close()
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.execute(
            "UPDATE runs SET status=?, ended_at=?, steps=?, retries=?, input_tokens=?,"
            " output_tokens=?, repairs=?, report_json=?, criteria_json=? WHERE run_id=?",
            (
                status, time.time(), steps, self.retries, self.input_tokens,
                self.output_tokens, repairs,
                json.dumps(report, default=str) if report else None,
                json.dumps(criteria, default=str) if criteria else None,
                self.run_id,
            ),
        )
        conn.commit()
        conn.close()

    # -- reading ----------------------------------------------------------
    def events(self) -> list[dict[str, Any]]:
        return list(read_run_events(self.db_path, self.run_id))

    def jsonl_path(self) -> Path:
        return self._jsonl_path


def _truncate_obj(obj: Any, limit: int) -> Any:
    """Keep traces readable: clip long strings but preserve shape."""
    if isinstance(obj, str):
        return obj if len(obj) <= limit else obj[:limit] + f"\u2026[{len(obj)} chars]"
    if isinstance(obj, dict):
        return {k: _truncate_obj(v, limit) for k, v in obj.items()}
    if isinstance(obj, list):
        clipped = [_truncate_obj(v, limit) for v in obj[:50]]
        if len(obj) > 50:
            clipped.append(f"\u2026[{len(obj) - 50} more items]")
        return clipped
    return obj


def read_run_events(db_path: Path, run_id: str) -> Iterator[dict[str, Any]]:
    if not db_path.exists():
        return iter(())
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT * FROM events WHERE run_id = ? ORDER BY seq", (run_id,)
        ).fetchall()
    finally:
        conn.close()
    for row in rows:
        yield {
            "seq": row["seq"],
            "ts": row["ts"],
            "kind": row["kind"],
            "step": row["step"],
            "tool": row["tool"],
            "args": json.loads(row["args_json"]) if row["args_json"] else None,
            "observation": json.loads(row["obs_json"]) if row["obs_json"] else None,
            "text": row["text"],
            "duration_ms": row["duration_ms"],
            "evidence": json.loads(row["evidence_json"]) if row["evidence_json"] else [],
            "error": row["error"],
        }


def read_run(db_path: Path, run_id: str) -> dict[str, Any] | None:
    if not db_path.exists():
        return None
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    record = dict(row)
    for key in ("report_json", "criteria_json", "config_json"):
        raw = record.pop(key, None)
        record[key.replace("_json", "")] = json.loads(raw) if raw else None
    return record


def list_runs(db_path: Path, limit: int = 50) -> list[dict[str, Any]]:
    if not db_path.exists():
        return []
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT run_id, goal, status, provider, model, started_at, ended_at, steps,"
            " retries, input_tokens, output_tokens, repairs FROM runs"
            " ORDER BY started_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]