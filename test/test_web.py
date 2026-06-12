from __future__ import annotations

import sys
import tempfile
import unittest
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

    def test_only_one_file_placeholder_can_expand_from_folder(self) -> None:
        with self.assertRaises(web.TemplateError):
            web.expand_template(
                "train --config {config:file} --other {other:file}",
                {"config": "configs", "other": "configs"},
                root=self.root,
            )

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
        self.assertNotIn("<label>Worker Type</label>", body)

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

        self.assertIn('<meta http-equiv="refresh" content="5">', body)
        self.assertIn('name="refresh" type="number" min="0" value="5"', body)

    def test_tasks_page_refresh_interval_can_be_changed_or_disabled(self) -> None:
        handler = web.build_handler(str(self.queue_dir), REPO_ROOT)
        handler_instance = object.__new__(handler)

        custom = handler_instance.tasks_page({"refresh": ["2"], "state": ["running"]})
        self.assertIn('<meta http-equiv="refresh" content="2">', custom)
        self.assertIn('name="state" value="running"', custom)

        disabled = handler_instance.tasks_page({"refresh": ["0"]})
        self.assertNotIn('http-equiv="refresh"', disabled)
        self.assertIn('value="0"', disabled)

    def test_task_page_refreshes_and_preserves_task_id(self) -> None:
        task_id = db.submit_task("echo hello", queue_dir=self.queue_dir, cwd=str(REPO_ROOT))
        handler = web.build_handler(str(self.queue_dir), REPO_ROOT)
        handler_instance = object.__new__(handler)
        body = handler_instance.task_page({"id": [task_id]})

        self.assertIn('<meta http-equiv="refresh" content="5">', body)
        self.assertIn(f'name="id" value="{task_id}"', body)


if __name__ == "__main__":
    unittest.main()
