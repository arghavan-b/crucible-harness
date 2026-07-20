"""Trace Recorder (design §6.5).

Everything observable, structured and timestamped: commands, exit codes, full
stdout/stderr, per-step filesystem deltas, package-manager events, GPU/memory
telemetry, network requests, and ALL LLM calls with prompts and outputs (the
planner is part of the experiment record). This IS the observability system.

Stage-0 slice: a single SQLite file (events table + traces table). The interface
is backend-agnostic so this swaps to Postgres + S3 (design §15) without touching
the executor. Every run is recorded from the very first command.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from typing import Any, Protocol


class TraceRecorder(Protocol):
    def start(self, experiment_id: str) -> str: ...
    def record(self, trace_id: str, kind: str, payload: dict[str, Any]) -> None: ...
    def record_llm_call(self, trace_id: str, role: str, prompt: Any, output: Any) -> None: ...
    def events(self, trace_id: str) -> list[dict[str, Any]]: ...


class SQLiteTraceRecorder:
    def __init__(self, db_path: str = "traces/crucible.sqlite") -> None:
        import os

        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self._conn = sqlite3.connect(db_path)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS traces ("
            "trace_id TEXT PRIMARY KEY, experiment_id TEXT, started_at REAL)"
        )
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS events ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, trace_id TEXT, ts REAL, "
            "kind TEXT, payload TEXT)"
        )
        self._conn.commit()

    def start(self, experiment_id: str) -> str:
        trace_id = f"trace_{uuid.uuid4().hex[:10]}"
        self._conn.execute(
            "INSERT INTO traces (trace_id, experiment_id, started_at) VALUES (?, ?, ?)",
            (trace_id, experiment_id, time.time()),
        )
        self._conn.commit()
        return trace_id

    def record(self, trace_id: str, kind: str, payload: dict[str, Any]) -> None:
        self._conn.execute(
            "INSERT INTO events (trace_id, ts, kind, payload) VALUES (?, ?, ?, ?)",
            (trace_id, time.time(), kind, json.dumps(payload, default=str)),
        )
        self._conn.commit()

    def record_llm_call(self, trace_id: str, role: str, prompt: Any, output: Any) -> None:
        # The planner/diagnoser are part of the experiment record (design §6.5).
        self.record(trace_id, "llm_call", {"role": role, "prompt": prompt, "output": output})

    def events(self, trace_id: str) -> list[dict[str, Any]]:
        cur = self._conn.execute(
            "SELECT ts, kind, payload FROM events WHERE trace_id = ? ORDER BY id",
            (trace_id,),
        )
        return [
            {"ts": ts, "kind": kind, "payload": json.loads(payload)}
            for ts, kind, payload in cur.fetchall()
        ]

    def close(self) -> None:
        self._conn.close()


def replay(trace_id: str) -> None:
    """Reproduce a run to a byte-comparable result, or emit a documented list of
    nondeterminism sources. Backs `crucible replay`. (Stage 0, weeks 1-2.)"""
    raise NotImplementedError("Replay from certificate: weeks 1-2.")
