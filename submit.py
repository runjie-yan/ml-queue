from __future__ import annotations

import argparse
import shlex
import sys
from pathlib import Path

import db


def command_from_remainder(parts: list[str]) -> str:
    if parts and parts[0] == "--":
        parts = parts[1:]
    if not parts:
        raise SystemExit("submit requires a command after --")
    if len(parts) == 1:
        return parts[0]
    return shlex.join(parts)


def render_command(template: str, *, task_id: str, split_id: int | None, split_count: int | None) -> str:
    values = {
        "task_id": task_id,
        "split_id": "" if split_id is None else split_id,
        "split_count": "" if split_count is None else split_count,
    }
    return template.format(**values)


def submit_single(args: argparse.Namespace) -> list[str]:
    task_id = db.make_task_id()
    command = render_command(
        command_from_remainder(args.command),
        task_id=task_id,
        split_id=None,
        split_count=None,
    )
    db.submit_task(
        command,
        queue_dir=args.queue_dir,
        worker_type=args.worker_type,
        cwd=args.cwd,
        task_id=task_id,
    )
    return [task_id]


def submit_splits(args: argparse.Namespace) -> list[str]:
    if args.splits < 1:
        raise SystemExit("--splits must be >= 1")
    template = command_from_remainder(args.command)
    task_ids: list[str] = []
    for split_id in range(args.splits):
        task_id = db.make_task_id()
        command = render_command(
            template,
            task_id=task_id,
            split_id=split_id,
            split_count=args.splits,
        )
        db.submit_task(
            command,
            queue_dir=args.queue_dir,
            worker_type=args.worker_type,
            cwd=args.cwd,
            task_id=task_id,
            split_id=split_id,
            split_count=args.splits,
            params={
                "command_template": template,
                "split_id": split_id,
                "split_count": args.splits,
            },
        )
        task_ids.append(task_id)
    return task_ids


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Submit tasks to the local SQLite queue.")
    parser.add_argument("--queue-dir", default=str(db.default_queue_dir()))
    parser.add_argument("--type", dest="worker_type", default=db.DEFAULT_WORKER_TYPE)
    parser.add_argument("--cwd", default=str(db.default_cwd()))
    parser.add_argument("--splits", type=int, default=None, help="Create N split tasks.")
    parser.add_argument("--resubmit-stale", action="store_true", help="Submit new pending copies of stale running tasks.")
    parser.add_argument("--stale-sec", type=int, default=db.DEFAULT_STALE_SECONDS, help="Heartbeat age threshold for stale running tasks.")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    db.init_db(args.queue_dir)
    if args.resubmit_stale:
        task_ids = db.resubmit_stale_tasks(
            queue_dir=args.queue_dir,
            stale_seconds=args.stale_sec,
            worker_type=args.worker_type,
        )
    else:
        task_ids = submit_splits(args) if args.splits is not None else submit_single(args)
    for task_id in task_ids:
        print(task_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
