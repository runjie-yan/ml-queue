from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from typing import Any


DEFAULT_WORKER_TYPE = "BASE"
TASK_STATES = {"pending", "running", "done", "failed", "canceled"}
SQLITE_BUSY_TIMEOUT_MS = 30000
LOCK_RETRY_SECONDS = 60.0
DEFAULT_STALE_SECONDS = 3600
_INITIALIZED_DB_PATHS: set[Path] = set()


def default_queue_dir() -> Path:
    return Path(__file__).resolve().parent / "data"


def default_cwd() -> Path:
    return Path(__file__).resolve().parents[1]


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="microseconds")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


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
    conn = sqlite3.connect(db_path(queue_dir), timeout=SQLITE_BUSY_TIMEOUT_MS / 1000)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")
    return conn


def is_locked_error(exc: sqlite3.OperationalError) -> bool:
    message = str(exc).lower()
    return "database is locked" in message or "database is busy" in message


def retry_locked(operation: Any, *, seconds: float = LOCK_RETRY_SECONDS) -> Any:
    deadline = time.monotonic() + seconds
    delay = 0.05
    while True:
        try:
            return operation()
        except sqlite3.OperationalError as exc:
            if not is_locked_error(exc) or time.monotonic() >= deadline:
                raise
            time.sleep(delay)
            delay = min(delay * 1.5, 1.0)


def init_db(queue_dir: str | os.PathLike[str] | None = None) -> None:
    root = resolve_queue_dir(queue_dir)

    def operation() -> None:
        with connect(root) as conn:
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
                    heartbeat_at TEXT,
                    error TEXT
                )
                """
            )
            ensure_column(conn, "tasks", "heartbeat_at", "TEXT")
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

    retry_locked(operation)
    _INITIALIZED_DB_PATHS.add(db_path(root))


def init_db_if_missing(queue_dir: str | os.PathLike[str] | None = None) -> None:
    root = ensure_layout(queue_dir)
    path = db_path(root)
    if path not in _INITIALIZED_DB_PATHS:
        init_db(root)


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


def ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    if column not in {row["name"] for row in rows}:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return dict(row)


def is_task_stale(task: dict[str, Any], *, stale_seconds: int = DEFAULT_STALE_SECONDS) -> bool:
    if task.get("state") != "running":
        return False
    heartbeat = parse_iso(str(task.get("heartbeat_at") or task.get("updated_at") or ""))
    if heartbeat is None:
        return True
    return datetime.now().astimezone() - heartbeat > timedelta(seconds=stale_seconds)


def display_state(task: dict[str, Any], *, stale_seconds: int = DEFAULT_STALE_SECONDS) -> str:
    return "running(stale)" if is_task_stale(task, stale_seconds=stale_seconds) else str(task["state"])


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

    init_db_if_missing(queue_dir)
    root = resolve_queue_dir(queue_dir)
    task_id = task_id or make_task_id()
    timestamp = now_iso()
    stdout_path = log_dir(root) / f"{task_id}.out"
    stderr_path = log_dir(root) / f"{task_id}.err"
    task_cwd = Path(cwd).resolve() if cwd else default_cwd()

    def operation() -> None:
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

    retry_locked(operation)
    return task_id


def get_task(task_id: str, *, queue_dir: str | os.PathLike[str] | None = None) -> dict[str, Any] | None:
    init_db_if_missing(queue_dir)

    def operation() -> sqlite3.Row | None:
        with connect(queue_dir) as conn:
            return conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()

    row = retry_locked(operation)
    return row_to_dict(row)


def list_tasks(
    *,
    queue_dir: str | os.PathLike[str] | None = None,
    state: str | None = None,
    worker_type: str | None = None,
) -> list[dict[str, Any]]:
    init_db_if_missing(queue_dir)
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
    def operation() -> list[sqlite3.Row]:
        with connect(queue_dir) as conn:
            return conn.execute(query, params).fetchall()

    rows = retry_locked(operation)
    return [dict(row) for row in rows]


def count_active_tasks(
    *,
    queue_dir: str | os.PathLike[str] | None = None,
    worker_type: str = DEFAULT_WORKER_TYPE,
) -> int:
    init_db_if_missing(queue_dir)

    def operation() -> sqlite3.Row:
        with connect(queue_dir) as conn:
            return conn.execute(
                """
                SELECT COUNT(*) AS count
                FROM tasks
                WHERE worker_type = ?
                  AND state IN ('pending', 'running')
                """,
                (worker_type,),
            ).fetchone()

    row = retry_locked(operation)
    return int(row["count"])


def claim_next_task(
    *,
    queue_dir: str | os.PathLike[str] | None = None,
    worker_type: str = DEFAULT_WORKER_TYPE,
    worker_id: str,
) -> dict[str, Any] | None:
    init_db_if_missing(queue_dir)

    def operation() -> dict[str, Any] | None:
        timestamp = now_iso()
        conn = connect(queue_dir)
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT id
                FROM tasks NOT INDEXED
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
                    updated_at = ?,
                    heartbeat_at = ?
                WHERE id = ?
                  AND state = 'pending'
                """,
                (worker_id, timestamp, timestamp, timestamp, task_id),
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

    return retry_locked(operation)


def heartbeat_task(
    task_id: str,
    *,
    queue_dir: str | os.PathLike[str] | None = None,
    worker_id: str | None = None,
) -> bool:
    init_db_if_missing(queue_dir)

    def operation() -> bool:
        timestamp = now_iso()
        params: list[Any] = [timestamp, timestamp, task_id]
        worker_clause = ""
        if worker_id is not None:
            worker_clause = " AND worker_id = ?"
            params.append(worker_id)
        with connect(queue_dir) as conn:
            cursor = conn.execute(
                f"""
                UPDATE tasks
                SET heartbeat_at = ?,
                    updated_at = ?
                WHERE id = ?
                  AND state = 'running'
                  {worker_clause}
                """,
                params,
            )
            return cursor.rowcount == 1

    return retry_locked(operation)


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
    init_db_if_missing(queue_dir)

    def operation() -> None:
        timestamp = now_iso()
        with connect(queue_dir) as conn:
            conn.execute(
                """
                UPDATE tasks
                SET state = ?,
                    updated_at = ?,
                    finished_at = ?,
                    return_code = ?,
                    error = ?,
                    heartbeat_at = ?
                WHERE id = ?
                """,
                (state, timestamp, timestamp, return_code, error, timestamp, task_id),
            )
            record_event(
                conn,
                task_id=task_id,
                from_state="running",
                to_state=state,
                message=error or f"return_code={return_code}",
            )

    retry_locked(operation)


def cancel_task(task_id: str, *, queue_dir: str | os.PathLike[str] | None = None) -> bool:
    init_db_if_missing(queue_dir)

    def operation() -> bool:
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

    return retry_locked(operation)


def stale_running_tasks(
    *,
    queue_dir: str | os.PathLike[str] | None = None,
    stale_seconds: int = DEFAULT_STALE_SECONDS,
    worker_type: str | None = None,
) -> list[dict[str, Any]]:
    tasks = list_tasks(queue_dir=queue_dir, state="running", worker_type=worker_type)
    return [task for task in tasks if is_task_stale(task, stale_seconds=stale_seconds)]


def resubmit_stale_tasks(
    *,
    queue_dir: str | os.PathLike[str] | None = None,
    stale_seconds: int = DEFAULT_STALE_SECONDS,
    worker_type: str | None = None,
) -> list[str]:
    original_tasks = stale_running_tasks(
        queue_dir=queue_dir,
        stale_seconds=stale_seconds,
        worker_type=worker_type,
    )
    new_ids: list[str] = []
    for task in original_tasks:
        params = json.loads(str(task.get("params_json") or "{}"))
        params["resubmitted_from"] = task["id"]
        params["resubmitted_reason"] = f"stale_after_{stale_seconds}s"
        new_id = submit_task(
            str(task["command"]),
            queue_dir=queue_dir,
            worker_type=str(task["worker_type"]),
            cwd=str(task["cwd"]),
            split_id=task["split_id"],
            split_count=task["split_count"],
            params=params,
        )
        new_ids.append(new_id)
        def operation(task_id: str = str(task["id"]), created_id: str = new_id) -> None:
            with connect(queue_dir) as conn:
                record_event(
                    conn,
                    task_id=task_id,
                    from_state="running",
                    to_state="running",
                    message=f"stale task resubmitted as {created_id}",
                )

        retry_locked(operation)
    return new_ids


def delete_tasks(
    task_ids: list[str],
    *,
    queue_dir: str | os.PathLike[str] | None = None,
    allow_running: bool = False,
) -> dict[str, int]:
    init_db_if_missing(queue_dir)
    requested = len([task_id for task_id in task_ids if task_id])
    if requested == 0:
        return {"requested": 0, "deleted": 0, "skipped_running": 0, "missing": 0}

    def operation() -> dict[str, int]:
        deleted = 0
        skipped_running = 0
        missing = 0
        with connect(queue_dir) as conn:
            for task_id in task_ids:
                if not task_id:
                    continue
                row = conn.execute("SELECT state FROM tasks WHERE id = ?", (task_id,)).fetchone()
                if row is None:
                    missing += 1
                    continue
                if row["state"] == "running" and not allow_running:
                    skipped_running += 1
                    continue
                conn.execute("DELETE FROM task_events WHERE task_id = ?", (task_id,))
                cursor = conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
                deleted += cursor.rowcount
        return {
            "requested": requested,
            "deleted": deleted,
            "skipped_running": skipped_running,
            "missing": missing,
        }

    return retry_locked(operation)


def list_events(
    task_id: str,
    *,
    queue_dir: str | os.PathLike[str] | None = None,
) -> list[dict[str, Any]]:
    init_db_if_missing(queue_dir)

    def operation() -> list[sqlite3.Row]:
        with connect(queue_dir) as conn:
            return conn.execute(
                """
                SELECT *
                FROM task_events
                WHERE task_id = ?
                ORDER BY ts ASC, event_id ASC
                """,
                (task_id,),
            ).fetchall()

    rows = retry_locked(operation)
    return [dict(row) for row in rows]
