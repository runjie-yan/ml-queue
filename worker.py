from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import time

import db


def make_worker_id() -> str:
    return f"{socket.gethostname()}-{os.getpid()}-{db.make_task_id()}"


def run_task(task: dict[str, object], *, queue_dir: str) -> int:
    stdout_path = str(task["stdout_path"])
    stderr_path = str(task["stderr_path"])
    command = str(task["command"])
    cwd = str(task["cwd"])

    try:
        with open(stdout_path, "ab") as stdout_file, open(stderr_path, "ab") as stderr_file:
            process = subprocess.run(
                command,
                cwd=cwd,
                shell=True,
                stdout=stdout_file,
                stderr=stderr_file,
                check=False,
            )
        state = "done" if process.returncode == 0 else "failed"
        db.finish_task(
            str(task["id"]),
            queue_dir=queue_dir,
            state=state,
            return_code=process.returncode,
        )
        return process.returncode
    except Exception as exc:
        db.finish_task(
            str(task["id"]),
            queue_dir=queue_dir,
            state="failed",
            return_code=None,
            error=str(exc),
        )
        return 1


def run_once(*, queue_dir: str, worker_type: str, worker_id: str | None = None) -> bool:
    worker_id = worker_id or make_worker_id()
    task = db.claim_next_task(queue_dir=queue_dir, worker_type=worker_type, worker_id=worker_id)
    if task is None:
        return False
    run_task(task, queue_dir=queue_dir)
    return True


def worker_loop(args: argparse.Namespace) -> int:
    db.init_db(args.queue_dir)
    worker_id = args.worker_id or make_worker_id()

    while True:
        claimed = run_once(
            queue_dir=args.queue_dir,
            worker_type=args.worker_type,
            worker_id=worker_id,
        )
        if args.once:
            return 0
        if not args.forever and not claimed and db.count_active_tasks(queue_dir=args.queue_dir, worker_type=args.worker_type) == 0:
            return 0
        if not claimed:
            time.sleep(args.poll_sec)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run tasks from the local SQLite queue.")
    parser.add_argument("--queue-dir", default=str(db.default_queue_dir()))
    parser.add_argument("--type", dest="worker_type", default=db.DEFAULT_WORKER_TYPE)
    parser.add_argument("--poll-sec", type=float, default=5.0)
    parser.add_argument("--once", action="store_true", help="Try at most one task, then exit.")
    parser.add_argument("--forever", action="store_true", help="Keep polling forever, even when this worker type has no active jobs.")
    parser.add_argument("--worker-id", default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    return worker_loop(build_parser().parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
