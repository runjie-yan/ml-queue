from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
QUEUE_DIR = REPO_ROOT / "queue"
SUBMIT = QUEUE_DIR / "submit.py"
WORKER = QUEUE_DIR / "worker.py"

sys.path.insert(0, str(QUEUE_DIR))
import db  # noqa: E402


class QueueSmokeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.queue_dir = Path(self.tmp.name) / "queue-data"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def run_submit(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SUBMIT), "--queue-dir", str(self.queue_dir), *args],
            cwd=REPO_ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )

    def run_worker(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(WORKER), "--queue-dir", str(self.queue_dir), "--once", *args],
            cwd=REPO_ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )

    def submitted_id(self, result: subprocess.CompletedProcess[str]) -> str:
        lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        self.assertTrue(lines, msg=result.stderr)
        return lines[0]

    def test_init_creates_database_and_log_dir(self) -> None:
        db.init_db(self.queue_dir)
        self.assertTrue((self.queue_dir / "queue.sqlite").exists())
        self.assertTrue((self.queue_dir / "log").is_dir())

    def test_echo_task_runs_to_done_and_writes_stdout(self) -> None:
        task_id = self.submitted_id(self.run_submit("--", "echo", "hello"))
        self.run_worker()

        task = db.get_task(task_id, queue_dir=self.queue_dir)
        self.assertIsNotNone(task)
        self.assertEqual(task["state"], "done")
        self.assertEqual(task["return_code"], 0)
        self.assertEqual(Path(task["stdout_path"]).read_text().strip(), "hello")
        events = db.list_events(task_id, queue_dir=self.queue_dir)
        self.assertEqual([event["to_state"] for event in events], ["pending", "running", "done"])

    def test_touch_task_creates_file(self) -> None:
        target = Path(self.tmp.name) / "touched"
        task_id = self.submitted_id(self.run_submit("--", "touch", str(target)))
        self.run_worker()

        task = db.get_task(task_id, queue_dir=self.queue_dir)
        self.assertEqual(task["state"], "done")
        self.assertTrue(target.exists())

    def test_failing_task_records_failed(self) -> None:
        task_id = self.submitted_id(
            self.run_submit("--", sys.executable, "-c", "import sys; sys.exit(7)")
        )
        self.run_worker()

        task = db.get_task(task_id, queue_dir=self.queue_dir)
        self.assertEqual(task["state"], "failed")
        self.assertEqual(task["return_code"], 7)

    def test_cancel_pending_task(self) -> None:
        task_id = self.submitted_id(self.run_submit("--", "echo", "cancel-me"))
        self.assertTrue(db.cancel_task(task_id, queue_dir=self.queue_dir))

        task = db.get_task(task_id, queue_dir=self.queue_dir)
        self.assertEqual(task["state"], "canceled")
        events = db.list_events(task_id, queue_dir=self.queue_dir)
        self.assertEqual([event["to_state"] for event in events], ["pending", "canceled"])
        self.run_worker()
        self.assertEqual(db.get_task(task_id, queue_dir=self.queue_dir)["state"], "canceled")

    def test_second_claim_cannot_claim_same_task(self) -> None:
        task_id = self.submitted_id(self.run_submit("--", "echo", "claimed-once"))
        first = db.claim_next_task(
            queue_dir=self.queue_dir,
            worker_type=db.DEFAULT_WORKER_TYPE,
            worker_id="worker-a",
        )
        second = db.claim_next_task(
            queue_dir=self.queue_dir,
            worker_type=db.DEFAULT_WORKER_TYPE,
            worker_id="worker-b",
        )

        self.assertIsNotNone(first)
        self.assertEqual(first["id"], task_id)
        self.assertIsNone(second)

    def test_submit_splits_creates_split_tasks(self) -> None:
        result = self.run_submit(
            "--splits",
            "3",
            "--",
            "echo",
            "split={split_id}/{split_count}",
            "task={task_id}",
        )
        task_ids = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        self.assertEqual(len(task_ids), 3)

        tasks = db.list_tasks(queue_dir=self.queue_dir)
        self.assertEqual([task["split_id"] for task in tasks], [0, 1, 2])
        self.assertEqual([task["split_count"] for task in tasks], [3, 3, 3])
        self.assertIn(task_ids[0], tasks[0]["command"])

    def test_worker_type_filtering(self) -> None:
        base_id = self.submitted_id(self.run_submit("--", "echo", "base"))
        other_id = self.submitted_id(self.run_submit("--type", "PREPROCESS", "--", "echo", "pre"))

        self.run_worker("--type", "PREPROCESS")

        self.assertEqual(db.get_task(base_id, queue_dir=self.queue_dir)["state"], "pending")
        self.assertEqual(db.get_task(other_id, queue_dir=self.queue_dir)["state"], "done")


if __name__ == "__main__":
    unittest.main()
