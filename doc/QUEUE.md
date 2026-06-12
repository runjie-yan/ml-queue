# Lightweight SQLite Worker Queue

## Current Goal

The queue system is a standalone folder under `queue/`. It is intentionally separate from the main training code so it can later become its own repository or submodule.

Primary use cases:

1. Submit any shell command from a development machine.
2. Submit split preprocessing commands with `{split_id}`, `{split_count}`, and `{task_id}` placeholders.
3. Run one or more background workers that claim tasks from SQLite and execute them.
4. Generate arbitrary shell commands interactively from saved shell-command templates.
5. Inspect the task database and logs from a read-only browser page.

The implementation follows a `tsp`-style model: the queue accepts shell commands as the basic unit of work.

## Files

```text
queue/
  README.md
  db.py
  submit.py
  worker.py
  web.py
  test/
    test_queue.py
    test_web.py
  data/
    queue.sqlite
    log/
    template/
```

Runtime files under `queue/data/` are ignored by `queue/.gitignore`.

## Runtime Layout

Default runtime paths:

```text
queue/data/queue.sqlite
queue/data/log/<task_id>.out
queue/data/log/<task_id>.err
queue/data/template/<template_name>.json
```

SQLite stores task metadata and state history. Log files store stdout/stderr. Template files are portable readable JSON.

## Command Submission

Submit one arbitrary shell command:

```bash
python queue/submit.py -- echo hello
python queue/submit.py --type TRAIN -- bash scripts/train.sh
```

Submit split commands:

```bash
python queue/submit.py --type PREPROCESS --splits 100 -- \
  bash scripts/my_preprocess.sh --task-id {split_id} --num-tasks {split_count}
```

Split placeholders:

- `{split_id}`: zero-based split index.
- `{split_count}`: total number of splits.
- `{task_id}`: unique queue task ID.

## Worker

Run one task and exit:

```bash
python queue/worker.py --once
python queue/worker.py --type PREPROCESS --once
```

Run continuously:

```bash
python queue/worker.py --type PREPROCESS --poll-sec 2
```

Worker behavior:

1. Claim one pending task for the requested worker type.
2. Commit the SQLite transaction.
3. Run the shell command in the task working directory.
4. Write stdout/stderr to `queue/data/log/`.
5. Mark the task `done` or `failed`.

Workers never hold a SQLite transaction while running a shell command.

## Task States

Current states:

```text
pending
running
done
failed
canceled
```

Current transitions:

```text
pending -> running
running -> done
running -> failed
pending -> canceled
```

There is no automatic retry yet. Rerun failed work by submitting a new command.

## Database Tables

`tasks` stores the current task state and command metadata.

Important fields:

- `id`
- `state`
- `worker_type`
- `command`
- `cwd`
- `split_id`
- `split_count`
- `params_json`
- `worker_id`
- `started_at`
- `finished_at`
- `return_code`
- `stdout_path`
- `stderr_path`
- `error`

`task_events` stores state-history events. The implementation records:

- submitted
- claimed
- finished with return code
- canceled

This keeps failures and command lifecycle inspectable without adding a heavy workflow engine.

## Web Interface

Start the local browser UI:

```bash
python queue/web.py
```

Open:

```text
http://127.0.0.1:8765
```

The web interface supports:

- arbitrary shell-command template editing
- exact command preview while typing, including recursive file expansion
- generated shell command preview
- copy one generated command or all generated commands at once
- direct submission to SQLite
- hard-coded queue submit and worker examples as the first templates
- saved templates read from `queue/data/template/`
- saved template writing
- saved template names with command snippets
- read-only task table
- read-only task detail
- read-only stdout/stderr log viewing

The database viewer does not edit existing tasks.

## Template Placeholders

Command templates are arbitrary shell commands with typed placeholders:

```bash
/usr/bin/python queue/submit.py --queue-dir {queue-dir:folder} --type {worker type:name} -- {command:name}
```

Supported placeholder types:

- `{name:file}`: one file path. If the selected value is a folder, it recursively expands over every file in the folder and generates one command per file.
- `{name:folder}`: one folder path used as-is. It does not recursively expand files.
- `{name:text}`, `{name:name}`, or `{name}`: plain text.

Rules:

- Multiple placeholders can be used in one template.
- Placeholder names may contain spaces or dashes, such as `{worker type:name}` and `{queue-dir:folder}`.
- The same placeholder can be reused multiple times.
- Only one `file` placeholder may recursively expand from a selected folder in a single generation.
- Paths are resolved under the repository root; paths outside the repo are rejected.
- Folder expansion includes every regular file found recursively.

Example:

```bash
/usr/bin/python queue/submit.py --queue-dir {queue-dir:folder} --type {worker type:name} -- {command:name}
```

The browser form shows the exact command lines immediately as the template or placeholder values change. The right panel can copy the commands or submit them directly to the SQLite queue. If one file placeholder points to a folder, preview and submit expand it into one command per recursive file. Template selection and saving use popup dialogs; save-name validation stays inside the save dialog. Queue status and task-detail pages refresh every 5 seconds by default; set the refresh interval to 0 to disable auto refresh.

If a file placeholder is set to `config/experiment/`, submission generates one task per file under that folder.

## Saved Templates

Templates are stored as JSON:

```text
queue/data/template/train-default.json
```

Example:

```json
{
  "version": 1,
  "name": "train-default",
  "template": "/usr/bin/python queue/submit.py --queue-dir {queue-dir:folder} --type {worker type:name} -- {command:name}",
  "worker_type": "BASE",
  "cwd": "/path/to/repo",
  "values": {
    "queue-dir": "queue/data",
    "worker type": "BASE",
    "command": "echo hello"
  }
}
```

The file is portable and human-readable. It can be copied with the `queue/` folder or edited manually.

The template list starts with hard-coded queue submit and worker examples, then lists JSON templates from `queue/data/template/`. There is no separate manual template-loading control.

## Current Scope

Implemented:

- SQLite queue database.
- Arbitrary shell command submission.
- Split task submission.
- Worker type filtering.
- One-task-at-a-time workers.
- stdout/stderr log files.
- Task state history.
- Basic pending-task cancel helper in `db.py`.
- Web command-template UI.
- Saved template JSON files.
- Read-only task/log viewer.
- Smoke tests.

Not implemented yet:

- automatic retries
- task dependencies
- priorities
- timeout management
- GPU/resource scheduling
- authentication
- remote broker
- DAG engine
- SLURM state synchronization

## Testing

Run all queue tests after any queue code update:

```bash
python -m unittest discover -s queue/test -v
```

Current tests cover:

- database initialization
- simple `echo` command
- `touch` command
- failed command handling
- pending task cancel
- duplicate claim prevention
- split task submission
- worker type filtering
- task event history
- template placeholder expansion
- file-placeholder recursive file expansion
- folder-placeholder non-expansion
- path safety
- saved template round trip
- generated web-template task submission
- log tailing

## Design Notes

The queue borrows the useful parts of mature systems without adopting their operational weight:

- From `tsp`: arbitrary shell command submission as the main workflow.
- From Celery/RQ: explicit task states and worker loops.
- From Airflow/Prefect: visible run records, logs, and state history.
- From SLURM: split jobs as a normal ML workflow.
- From SQLite: short transactions for simple local task claiming.

The system should stay small. Add features only when the daily workflow clearly needs them.
