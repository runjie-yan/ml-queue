from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime
from datetime import timedelta
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
QUEUE_DIR = REPO_ROOT / "queue"

sys.path.insert(0, str(QUEUE_DIR))
import db  # noqa: E402
import web  # noqa: E402


class TemplateExpansionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "configs" / "nested").mkdir(parents=True)
        (self.root / "configs" / "a.yaml").write_text("a: 1\n")
        (self.root / "configs" / "nested" / "b.yml").write_text("b: 1\n")
        (self.root / "configs" / "ignore.txt").write_text("ignore\n")
        (self.root / "data").mkdir()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_file_placeholder_accepts_one_file(self) -> None:
        result = web.expand_template(
            "train --config {config:file} --data {data:folder} --name {name:text}",
            {
                "config": "configs/a.yaml",
                "data": "data",
                "name": "exp1",
            },
            root=self.root,
        )

        self.assertEqual(len(result), 1)
        self.assertEqual(
            result[0]["command"],
            "train --config configs/a.yaml --data data --name exp1",
        )

    def test_file_placeholder_expands_folder_recursively_for_all_files(self) -> None:
        result = web.expand_template(
            "train --config {config:file}",
            {"config": "configs"},
            root=self.root,
        )

        self.assertEqual([item["values"]["config"] for item in result], [
            "configs/a.yaml",
            "configs/ignore.txt",
            "configs/nested/b.yml",
        ])
        self.assertEqual([item["command"] for item in result], [
            "train --config configs/a.yaml",
            "train --config configs/ignore.txt",
            "train --config configs/nested/b.yml",
        ])

    def test_folder_placeholder_does_not_expand_files(self) -> None:
        result = web.expand_template(
            "train --config-dir {config_dir:folder}",
            {"config_dir": "configs"},
            root=self.root,
        )

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["command"], "train --config-dir configs")

    def test_file_placeholder_folder_expansion_can_combine_with_other_files(self) -> None:
        result = web.expand_template(
            "train --config {config:file} --other {other:file}",
            {"config": "configs", "other": "configs/a.yaml"},
            root=self.root,
        )

        self.assertEqual(len(result), 3)
        self.assertEqual(result[0]["command"], "train --config configs/a.yaml --other configs/a.yaml")

    def test_multiple_placeholders_can_use_same_expanded_file(self) -> None:
        result = web.expand_template(
            "train --config {config:file} --tag {config:file}",
            {"config": "configs"},
            root=self.root,
        )

        self.assertEqual(len(result), 3)
        self.assertEqual(result[0]["command"], "train --config configs/a.yaml --tag configs/a.yaml")

    def test_rejects_path_outside_root(self) -> None:
        with self.assertRaises(web.TemplateError):
            web.expand_template(
                "train --config {config:file}",
                {"config": "../outside.yaml"},
                root=self.root,
            )

    def test_name_placeholder_allows_spaces_and_dashes(self) -> None:
        result = web.expand_template(
            "/usr/bin/python queue/submit.py --queue-dir {queue-dir:folder} --type {worker type:name} -- {command:name}",
            {
                "queue-dir": "data",
                "worker type": "BASE",
                "command": "echo hello",
            },
            root=self.root,
        )

        self.assertEqual(len(result), 1)
        self.assertEqual(
            result[0]["command"],
            "/usr/bin/python queue/submit.py --queue-dir data --type BASE -- echo hello",
        )

    def test_text_placeholder_expands_one_value_per_line(self) -> None:
        result = web.expand_template(
            "echo {item:name}",
            {"item": "one\ntwo\nthree"},
            root=self.root,
        )

        self.assertEqual([item["command"] for item in result], ["echo one", "echo two", "echo three"])

    def test_multiple_placeholder_lists_expand_as_cartesian_product(self) -> None:
        result = web.expand_template(
            "echo {left:name} {right:name}",
            {"left": "a\nb", "right": "1\n2"},
            root=self.root,
        )

        self.assertEqual([item["command"] for item in result], [
            "echo a 1",
            "echo a 2",
            "echo b 1",
            "echo b 2",
        ])

    def test_file_placeholder_accepts_multiline_paths_and_folder_expansion(self) -> None:
        result = web.expand_template(
            "train --config {config:file}",
            {"config": "configs/a.yaml\nconfigs/nested"},
            root=self.root,
        )

        self.assertEqual([item["command"] for item in result], [
            "train --config configs/a.yaml",
            "train --config configs/nested/b.yml",
        ])


class WebSubmissionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.queue_dir = Path(self.tmp.name) / "queue-data"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_submit_generated_tasks_records_template_metadata(self) -> None:
        generated = [
            {
                "command": "echo config/a.yaml",
                "values": {"config": "config/a.yaml"},
                "expanded_placeholder": "config",
            }
        ]
        task_ids = web.submit_generated_tasks(
            generated,
            queue_dir=str(self.queue_dir),
            worker_type="TRAIN",
            cwd=str(REPO_ROOT),
            template="echo {config:file}",
        )

        self.assertEqual(len(task_ids), 1)
        task = db.get_task(task_ids[0], queue_dir=self.queue_dir)
        self.assertEqual(task["worker_type"], "TRAIN")
        self.assertEqual(task["command"], "echo config/a.yaml")
        self.assertIn("web-template", task["params_json"])

    def test_tail_text_reads_last_bytes(self) -> None:
        path = Path(self.tmp.name) / "log.txt"
        path.write_text("0123456789")
        self.assertEqual(web.tail_text(str(path), limit=4), "6789")

    def test_saved_template_round_trips_as_json_file(self) -> None:
        path = web.save_template(
            queue_dir=self.queue_dir,
            name="train-default",
            template="python train.py --config {config:file} --tag {tag:text}",
            worker_type="TRAIN",
            cwd=str(REPO_ROOT),
            values={"config": "config/experiment", "tag": "smoke"},
        )

        self.assertEqual(path.parent, self.queue_dir / "template")
        self.assertEqual(path.name, "train-default.json")
        loaded = web.load_template(self.queue_dir, "train-default")
        self.assertEqual(loaded["template"], "python train.py --config {config:file} --tag {tag:text}")
        self.assertEqual(loaded["worker_type"], "TRAIN")
        self.assertEqual(loaded["values"]["config"], "config/experiment")

    def test_template_summary_starts_with_hardcoded_examples(self) -> None:
        web.save_template(
            queue_dir=self.queue_dir,
            name="b-template",
            template="echo b {config:file}",
            worker_type="TRAIN",
            cwd=str(REPO_ROOT),
            values={"config": "config/b.yaml"},
        )
        web.save_template(
            queue_dir=self.queue_dir,
            name="a-template",
            template="echo a {config:file}",
            worker_type="TRAIN",
            cwd=str(REPO_ROOT),
            values={"config": "config/a.yaml"},
        )

        summaries = web.template_summary(self.queue_dir)
        self.assertEqual(summaries[0]["name"], "example-submit-task")
        self.assertEqual(summaries[1]["name"], "example-worker")
        self.assertEqual(summaries[2]["name"], "a-template")
        self.assertIn("echo a {config:file}", summaries[2]["template"])

    def test_saved_template_rejects_unsafe_name(self) -> None:
        with self.assertRaises(web.TemplateError):
            web.save_template(
                queue_dir=self.queue_dir,
                name="../bad",
                template="echo bad",
                worker_type="TRAIN",
                cwd=str(REPO_ROOT),
                values={},
            )

    def test_submit_form_builtin_default_is_generic_shell_command(self) -> None:
        handler = web.build_handler(str(self.queue_dir), REPO_ROOT)
        handler_instance = object.__new__(handler)
        body = handler_instance.submit_form()
        self.assertIn("/usr/bin/python queue/submit.py --queue-dir {queue-dir:folder}", body)
        self.assertIn("Exact Command Preview", body)
        self.assertIn("save-modal", body)
        self.assertIn("example-submit-task", body)
        self.assertIn("example-worker", body)
        self.assertIn('textarea class="placeholder-value"', body)
        self.assertNotIn("<label>Worker Type</label>", body)

        full_page = web.page("Queue Submit", body).decode("utf-8")
        self.assertIn("templateText?.addEventListener('keydown'", full_page)
        self.assertIn("#template-text { overflow: auto; white-space: pre;", full_page)

    def test_template_cards_show_name_and_template_snippet(self) -> None:
        web.save_template(
            queue_dir=self.queue_dir,
            name="train-long",
            template="python train.py --config {config:file} --extra " + ("x" * 160),
            worker_type="TRAIN",
            cwd=str(REPO_ROOT),
            values={"config": "config/experiment"},
        )
        handler = web.build_handler(str(self.queue_dir), REPO_ROOT)
        handler_instance = object.__new__(handler)
        body = handler_instance.submit_form()
        self.assertIn("train-long", body)
        self.assertIn("python train.py --config", body)

    def test_generated_preview_has_copy_all_commands(self) -> None:
        handler = web.build_handler(str(self.queue_dir), REPO_ROOT)
        handler_instance = object.__new__(handler)
        body = handler_instance.generated_preview_html(
            [{"command": "echo hello", "values": {"command": "hello"}, "expanded_placeholder": None}],
            ["task-1"],
        )
        self.assertIn("all-shell-commands", body)
        self.assertIn("Copy All Shell Commands", body)

    def test_tasks_page_refreshes_every_five_seconds_by_default(self) -> None:
        handler = web.build_handler(str(self.queue_dir), REPO_ROOT)
        handler_instance = object.__new__(handler)
        body = handler_instance.tasks_page({})

        self.assertNotIn('http-equiv="refresh"', body)
        self.assertIn('name="refresh" type="number" min="0" value="5" data-refresh-seconds', body)
        self.assertIn('id="task-list-data"', body)
        self.assertIn("startPartialRefresh", web.page("Queue Tasks", body).decode("utf-8"))

    def test_tasks_page_refresh_interval_can_be_changed_or_disabled(self) -> None:
        handler = web.build_handler(str(self.queue_dir), REPO_ROOT)
        handler_instance = object.__new__(handler)

        custom = handler_instance.tasks_page({"refresh": ["2"], "state": ["running"]})
        self.assertNotIn('http-equiv="refresh"', custom)
        self.assertIn('data-refresh-seconds', custom)
        self.assertIn('name="state" value="running"', custom)

        disabled = handler_instance.tasks_page({"refresh": ["0"]})
        self.assertNotIn('http-equiv="refresh"', disabled)
        self.assertIn('value="0"', disabled)

    def test_task_page_refreshes_and_preserves_task_id(self) -> None:
        task_id = db.submit_task("echo hello", queue_dir=self.queue_dir, cwd=str(REPO_ROOT))
        handler = web.build_handler(str(self.queue_dir), REPO_ROOT)
        handler_instance = object.__new__(handler)
        body = handler_instance.task_page({"id": [task_id]})

        self.assertNotIn('http-equiv="refresh"', body)
        self.assertIn(f'name="id" value="{task_id}"', body)
        self.assertIn('id="task-detail-data"', body)

    def test_tasks_page_headers_are_sort_links_and_toggle_order(self) -> None:
        db.submit_task("echo b", queue_dir=self.queue_dir, worker_type="B", cwd=str(REPO_ROOT))
        db.submit_task("echo a", queue_dir=self.queue_dir, worker_type="A", cwd=str(REPO_ROOT))
        handler = web.build_handler(str(self.queue_dir), REPO_ROOT)
        handler_instance = object.__new__(handler)

        body = handler_instance.tasks_page({"sort": ["type"], "order": ["asc"], "refresh": ["0"]})

        self.assertIn("sort=type&amp;order=desc", body)
        self.assertIn("Type ^", body)
        self.assertIn("sort=state&amp;order=asc", body)
        self.assertLess(body.index(">A<"), body.index(">B<"))

    def test_task_list_api_fragment_uses_same_sorted_table(self) -> None:
        db.submit_task("echo b", queue_dir=self.queue_dir, worker_type="B", cwd=str(REPO_ROOT))
        db.submit_task("echo a", queue_dir=self.queue_dir, worker_type="A", cwd=str(REPO_ROOT))
        handler = web.build_handler(str(self.queue_dir), REPO_ROOT)
        handler_instance = object.__new__(handler)

        body = handler_instance.tasks_data_html({"sort": ["command"], "order": ["asc"]})

        self.assertIn('action="/delete-tasks"', body)
        self.assertIn("Command ^", body)
        self.assertLess(body.index("echo a"), body.index("echo b"))

    def test_tasks_page_marks_stale_running_and_resubmits_by_button(self) -> None:
        task_id = db.submit_task("echo stale-web", queue_dir=self.queue_dir, cwd=str(REPO_ROOT))
        claimed = db.claim_next_task(
            queue_dir=self.queue_dir,
            worker_type=db.DEFAULT_WORKER_TYPE,
            worker_id="dead-worker",
        )
        self.assertEqual(claimed["id"], task_id)
        old = (datetime.now().astimezone() - timedelta(seconds=10)).isoformat(timespec="microseconds")
        with db.connect(self.queue_dir) as conn:
            conn.execute(
                "UPDATE tasks SET heartbeat_at = ?, updated_at = ? WHERE id = ?",
                (old, old, task_id),
            )
        handler = web.build_handler(str(self.queue_dir), REPO_ROOT)
        handler_instance = object.__new__(handler)

        body = handler_instance.tasks_page({"stale_sec": ["1"], "refresh": ["0"]})
        self.assertIn("running(stale)", body)
        self.assertIn('action="/resubmit-stale"', body)
        self.assertIn("Resubmit Stale Running", body)

        submitted = handler_instance.resubmit_stale_page({"stale_sec": ["1"], "refresh": ["0"]})
        tasks = db.list_tasks(queue_dir=self.queue_dir)
        pending = [task for task in tasks if task["state"] == "pending"]
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["command"], "echo stale-web")
        self.assertIn("Resubmitted 1 stale running task", submitted)

    def test_tasks_page_has_multi_select_delete_form(self) -> None:
        task_id = db.submit_task("echo delete-me", queue_dir=self.queue_dir, cwd=str(REPO_ROOT))
        handler = web.build_handler(str(self.queue_dir), REPO_ROOT)
        handler_instance = object.__new__(handler)
        body = handler_instance.tasks_page({})

        self.assertIn('action="/delete-tasks"', body)
        self.assertIn('id="select-all-tasks"', body)
        self.assertIn(f'name="task_id" value="{task_id}"', body)
        self.assertIn("Delete Selected", body)
        full_page = web.page("Queue Tasks", body).decode("utf-8")
        self.assertIn("toggleAllTasks", full_page)
        self.assertIn("syncSelectAll", full_page)

    def test_running_tasks_are_not_selected_by_select_all(self) -> None:
        running_id = db.submit_task("echo running", queue_dir=self.queue_dir, cwd=str(REPO_ROOT))
        pending_id = db.submit_task("echo pending", queue_dir=self.queue_dir, cwd=str(REPO_ROOT))
        claimed = db.claim_next_task(
            queue_dir=self.queue_dir,
            worker_type=db.DEFAULT_WORKER_TYPE,
            worker_id="test-worker",
        )
        self.assertEqual(claimed["id"], running_id)
        handler = web.build_handler(str(self.queue_dir), REPO_ROOT)
        handler_instance = object.__new__(handler)

        body = handler_instance.tasks_data_html({})

        self.assertIn(f'name="task_id" value="{running_id}" disabled', body)
        self.assertIn(f'name="task_id" value="{pending_id}"', body)

    def test_delete_tasks_page_deletes_selected_tasks_and_preserves_filters(self) -> None:
        task_id = db.submit_task("echo delete-me", queue_dir=self.queue_dir, worker_type="TRAIN", cwd=str(REPO_ROOT))
        handler = web.build_handler(str(self.queue_dir), REPO_ROOT)
        handler_instance = object.__new__(handler)
        body = handler_instance.delete_tasks_page({
            "task_id": [task_id],
            "type": ["TRAIN"],
            "refresh": ["0"],
        })

        self.assertIsNone(db.get_task(task_id, queue_dir=self.queue_dir))
        self.assertIn("Deleted 1 of 1 selected task", body)
        self.assertIn('name="type" value="TRAIN"', body)
        self.assertIn('name="refresh" type="number" min="0" value="0"', body)


if __name__ == "__main__":
    unittest.main()
