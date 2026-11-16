"""SQLite persistence (standard library): runs, per-step checkpoints, traces.

* A run is recorded as 'running' when it starts and updated when it ends, so crashes are visible.
* Checkpoints make failed runs resumable: every worker reports back to the supervisor, so re-entering
  the graph at the supervisor with the last saved state is always valid.
* WAL mode + a short-lived connection per operation keeps it safe for the threaded HTTP server.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY,
  incident_id TEXT NOT NULL,
  config TEXT NOT NULL,
  status TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 1,
  cost_usd REAL NOT NULL DEFAULT 0,
  tokens_in INTEGER NOT NULL DEFAULT 0,
  tokens_out INTEGER NOT NULL DEFAULT 0,
  error TEXT,
  incident_json TEXT NOT NULL,
  state_json TEXT,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS checkpoints (
  run_id TEXT NOT NULL, attempt INTEGER NOT NULL, step INTEGER NOT NULL,
  node TEXT NOT NULL, state_json TEXT NOT NULL,
  PRIMARY KEY (run_id, attempt, step)
);
CREATE TABLE IF NOT EXISTS traces (
  run_id TEXT NOT NULL, attempt INTEGER NOT NULL,
  summary_json TEXT NOT NULL, trace_json TEXT NOT NULL,
  PRIMARY KEY (run_id, attempt)
);
CREATE INDEX IF NOT EXISTS idx_runs_created ON runs(created_at);
"""


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self._write_lock = threading.Lock()
        with self._tx() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def _tx(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def ping(self) -> bool:
        with self._tx() as c:
            return c.execute("SELECT 1").fetchone()[0] == 1

    # ---- writes ----
    def create_run(self, run_id: str, incident: dict, config: str) -> None:
        now = time.time()
        with self._write_lock, self._tx() as c:
            c.execute(
                "INSERT INTO runs (id, incident_id, config, status, incident_json, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                (run_id, incident["id"], config, "running", json.dumps(incident), now, now))

    def begin_attempt(self, run_id: str) -> int:
        with self._write_lock, self._tx() as c:
            c.execute("UPDATE runs SET attempts = attempts + 1, status = 'running', error = NULL, updated_at = ? WHERE id = ?",
                      (time.time(), run_id))
            return c.execute("SELECT attempts FROM runs WHERE id = ?", (run_id,)).fetchone()[0]

    def save_checkpoint(self, run_id: str, attempt: int, step: int, node: str, state: dict) -> None:
        with self._write_lock, self._tx() as c:
            c.execute("INSERT OR REPLACE INTO checkpoints VALUES (?,?,?,?,?)",
                      (run_id, attempt, step, node, json.dumps(state, default=str)))

    def finish_run(self, run_id: str, attempt: int, status: str, state: dict, summary: dict,
                   trace: dict, error: str | None) -> None:
        with self._write_lock, self._tx() as c:
            c.execute(
                "UPDATE runs SET status=?, error=?, state_json=?, cost_usd = cost_usd + ?, tokens_in = tokens_in + ?, "
                "tokens_out = tokens_out + ?, updated_at=? WHERE id=?",
                (status, error, json.dumps(state, default=str), summary["cost_usd"], summary["tokens_in"],
                 summary["tokens_out"], time.time(), run_id))
            c.execute("INSERT OR REPLACE INTO traces VALUES (?,?,?,?)",
                      (run_id, attempt, json.dumps(summary, default=str), json.dumps(trace, default=str)))

    # ---- reads ----
    def get_run(self, run_id: str) -> dict | None:
        with self._tx() as c:
            r = c.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if r is None:
            return None
        d = dict(r)
        d["incident"] = json.loads(d.pop("incident_json"))
        raw = d.pop("state_json")
        d["state"] = json.loads(raw) if raw else None
        return d

    def list_runs(self, limit: int = 20, status: str | None = None) -> list[dict]:
        q = "SELECT id, incident_id, config, status, attempts, cost_usd, tokens_in, tokens_out, error, created_at, updated_at FROM runs"
        args: list = []
        if status:
            q += " WHERE status = ?"
            args.append(status)
        q += " ORDER BY created_at DESC LIMIT ?"
        args.append(max(1, min(limit, 200)))
        with self._tx() as c:
            return [dict(r) for r in c.execute(q, args).fetchall()]

    def last_checkpoint(self, run_id: str) -> dict | None:
        with self._tx() as c:
            r = c.execute("SELECT attempt, step, node, state_json FROM checkpoints WHERE run_id = ? "
                          "ORDER BY attempt DESC, step DESC LIMIT 1", (run_id,)).fetchone()
        return None if r is None else {"attempt": r["attempt"], "step": r["step"], "node": r["node"], "state": json.loads(r["state_json"])}

    def get_trace(self, run_id: str, attempt: int | None = None) -> dict | None:
        with self._tx() as c:
            if attempt is None:
                r = c.execute("SELECT trace_json FROM traces WHERE run_id = ? ORDER BY attempt DESC LIMIT 1", (run_id,)).fetchone()
            else:
                r = c.execute("SELECT trace_json FROM traces WHERE run_id = ? AND attempt = ?", (run_id, attempt)).fetchone()
        return None if r is None else json.loads(r["trace_json"])


class StoreCheckpointer:
    """Adapter so the graph runtime can persist checkpoints (it only needs `.append(entry)`)."""

    def __init__(self, store: Store, run_id: str, attempt: int):
        self.store, self.run_id, self.attempt = store, run_id, attempt

    def append(self, entry: dict) -> None:
        self.store.save_checkpoint(self.run_id, self.attempt, entry["step"], entry["node"], entry["state"])
