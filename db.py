from __future__ import annotations

import json
import os
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any


DEFAULT_WORKER_TYPE = "BASE"
TASK_STATES = {"pending", "running", "done", "failed", "canceled"}


def default_queue_dir() -> Path:
    return Path(__file__).resolve().parent / "data"


def default_cwd() -> Path:
    return Path(__file__).resolve().parents[1]


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="microseconds")


def make_task_id() -> str:
    prefix = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def resolve_queue_dir(queue_dir: str | os.PathLike[str] | None = None) -> Path:
    return Path(queue_dir).resolve() if queue_dir else default_queue_dir()


def db_path(queue_dir: str | os.PathLike[str] | None = None) -> Path:
    return resolve_queue_dir(queue_dir) / "queue.sqlite"


def log_dir(queue_dir: str | os.PathLike[str] | None = None) -> Path:
    return resolve_queue_dir(queue_dir) / "log"


def ensure_layout(queue_dir: str | os.PathLike[str] | None = None) -> Path:
    root = resolve_queue_dir(queue_dir)
    root.mkdir(parents=True, exist_ok=True)
    log_dir(root).mkdir(parents=True, exist_ok=True)
    return root


def connect(queue_dir: str | os.PathLike[str] | None = None) -> sqlite3.Connection:
    ensure_layout(queue_dir)
    conn = sqlite3.connect(db_path(queue_dir), timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def init_db(queue_dir: str | os.PathLike[str] | None = None) -> None:
    with connect(queue_dir) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                state TEXT NOT NULL,
                worker_type TEXT NOT NULL,
                command TEXT NOT NULL,
                cwd TEXT NOT NULL,
                split_id INTEGER,
                split_count INTEGER,
                params_json TEXT NOT NULL DEFAULT '{}',
                worker_id TEXT,
                started_at TEXT,
                finished_at TEXT,
                return_code INTEGER,
                stdout_path TEXT NOT NULL,
                stderr_path TEXT NOT NULL,
                error TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_tasks_state_type_created
            ON tasks(state, worker_type, created_at ASC)
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS task_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                ts TEXT NOT NULL,
                from_state TEXT,
                to_state TEXT NOT NULL,
                worker_id TEXT,
                message TEXT,
                FOREIGN KEY(task_id) REFERENCES tasks(id)
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_task_events_task_ts
            ON task_events(task_id, ts ASC, event_id ASC)
            """
        )


def record_event(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    to_state: str,
    from_state: str | None = None,
    worker_id: str | None = None,
    message: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO task_events (
            task_id, ts, from_state, to_state, worker_id, message
        )
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (task_id, now_iso(), from_state, to_state, worker_id, message),
    )


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return dict(row)


def submit_task(
    command: str,
    *,
    queue_dir: str | os.PathLike[str] | None = None,
    worker_type: str = DEFAULT_WORKER_TYPE,
    cwd: str | os.PathLike[str] | None = None,
    task_id: str | None = None,
    split_id: int | None = None,
    split_count: int | None = None,
    params: dict[str, Any] | None = None,
) -> str:
    if not command.strip():
        raise ValueError("command must not be empty")

    init_db(queue_dir)
    root = resolve_queue_dir(queue_dir)
    task_id = task_id or make_task_id()
    timestamp = now_iso()
    stdout_path = log_dir(root) / f"{task_id}.out"
    stderr_path = log_dir(root) / f"{task_id}.err"
    task_cwd = Path(cwd).resolve() if cwd else default_cwd()

    with connect(root) as conn:
        conn.execute(
            """
            INSERT INTO tasks (
                id, created_at, updated_at, state, worker_type, command, cwd,
                split_id, split_count, params_json, stdout_path, stderr_path
            )
            VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task_id,
                timestamp,
                timestamp,
                worker_type,
                command,
                str(task_cwd),
                split_id,
                split_count,
                json.dumps(params or {}, sort_keys=True),
                str(stdout_path),
                str(stderr_path),
            ),
        )
        record_event(
            conn,
            task_id=task_id,
            from_state=None,
            to_state="pending",
            message="submitted",
        )
    return task_id


def get_task(task_id: str, *, queue_dir: str | os.PathLike[str] | None = None) -> dict[str, Any] | None:
    init_db(queue_dir)
    with connect(queue_dir) as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return row_to_dict(row)


def list_tasks(
    *,
    queue_dir: str | os.PathLike[str] | None = None,
    state: str | None = None,
    worker_type: str | None = None,
) -> list[dict[str, Any]]:
    init_db(queue_dir)
    clauses = []
    params: list[Any] = []
    if state:
        clauses.append("state = ?")
        params.append(state)
    if worker_type:
        clauses.append("worker_type = ?")
        params.append(worker_type)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    query = f"""
        SELECT * FROM tasks
        {where}
        ORDER BY created_at ASC, split_id ASC, id ASC
    """
    with connect(queue_dir) as conn:
        rows = conn.execute(query, params).fetchall()
    return [dict(row) for row in rows]


def claim_next_task(
    *,
    queue_dir: str | os.PathLike[str] | None = None,
    worker_type: str = DEFAULT_WORKER_TYPE,
    worker_id: str,
) -> dict[str, Any] | None:
    init_db(queue_dir)
    timestamp = now_iso()
    conn = connect(queue_dir)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """
            SELECT id
            FROM tasks
            WHERE state = 'pending'
              AND worker_type = ?
            ORDER BY created_at ASC, id ASC
            LIMIT 1
            """,
            (worker_type,),
        ).fetchone()
        if row is None:
            conn.commit()
            return None

        task_id = row["id"]
        cursor = conn.execute(
            """
            UPDATE tasks
            SET state = 'running',
                worker_id = ?,
                started_at = ?,
                updated_at = ?
            WHERE id = ?
              AND state = 'pending'
            """,
            (worker_id, timestamp, timestamp, task_id),
        )
        if cursor.rowcount != 1:
            conn.rollback()
            return None

        record_event(
            conn,
            task_id=task_id,
            from_state="pending",
            to_state="running",
            worker_id=worker_id,
            message="claimed",
        )
        task = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        conn.commit()
        return row_to_dict(task)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def finish_task(
    task_id: str,
    *,
    queue_dir: str | os.PathLike[str] | None = None,
    state: str,
    return_code: int | None,
    error: str | None = None,
) -> None:
    if state not in {"done", "failed"}:
        raise ValueError("finish_task state must be 'done' or 'failed'")
    timestamp = now_iso()
    with connect(queue_dir) as conn:
        conn.execute(
            """
            UPDATE tasks
            SET state = ?,
                updated_at = ?,
                finished_at = ?,
                return_code = ?,
                error = ?
            WHERE id = ?
            """,
            (state, timestamp, timestamp, return_code, error, task_id),
        )
        record_event(
            conn,
            task_id=task_id,
            from_state="running",
            to_state=state,
            message=error or f"return_code={return_code}",
        )


def cancel_task(task_id: str, *, queue_dir: str | os.PathLike[str] | None = None) -> bool:
    init_db(queue_dir)
    timestamp = now_iso()
    with connect(queue_dir) as conn:
        cursor = conn.execute(
            """
            UPDATE tasks
            SET state = 'canceled',
                updated_at = ?
            WHERE id = ?
              AND state = 'pending'
            """,
            (timestamp, task_id),
        )
        if cursor.rowcount == 1:
            record_event(
                conn,
                task_id=task_id,
                from_state="pending",
                to_state="canceled",
                message="canceled",
            )
    return cursor.rowcount == 1


def list_events(
    task_id: str,
    *,
    queue_dir: str | os.PathLike[str] | None = None,
) -> list[dict[str, Any]]:
    init_db(queue_dir)
    with connect(queue_dir) as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM task_events
            WHERE task_id = ?
            ORDER BY ts ASC, event_id ASC
            """,
            (task_id,),
        ).fetchall()
    return [dict(row) for row in rows]
