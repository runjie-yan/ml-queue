# Queue Quick Start

This folder is a lightweight SQLite command queue. Use it when you want to submit any shell commands from one machine and let background workers run them later.

Longer design notes: [doc/QUEUE.md](doc/QUEUE.md).

Default runtime files:

```text
queue/data/queue.sqlite
queue/data/log/
```

## Installation

Use it as a standalone repo:

```bash
git clone git@github.com:<USER_OR_ORG>/ml-queue.git
cd ml-queue
python submit.py -- echo hello
python worker.py --once
```

Use it as a submodule inside another ML project:

```bash
cd /path/to/your/ml-project
git submodule add git@github.com:<USER_OR_ORG>/ml-queue.git queue
git commit -m "Add ml-queue submodule"
```

Clone the ML project later with:

```bash
git clone --recurse-submodules git@github.com:<USER_OR_ORG>/<ML_PROJECT>.git
```

For an existing clone:

```bash
git submodule update --init --recursive
```

When changing queue code from inside the parent project, commit and push inside `queue/` first, then commit the updated submodule pointer in the parent repo.

## Submit One Command

```bash
python queue/submit.py -- echo hello
python queue/worker.py --once
```

The submitted task ID is printed by `submit.py`. stdout and stderr are written to `queue/data/log/<task_id>.out` and `queue/data/log/<task_id>.err`.

## Submit Split Commands

```bash
python queue/submit.py --type PREPROCESS --splits 3 -- \
  echo split={split_id}/{split_count} task={task_id}
```

Run workers:

```bash
python queue/worker.py --type PREPROCESS --once
python queue/worker.py --type PREPROCESS --once
python queue/worker.py --type PREPROCESS --once
```

For a real preprocessing script, use the same placeholders:

```bash
python queue/submit.py --type PREPROCESS --splits 100 -- \
  bash scripts/my_preprocess.sh --task-id {split_id} --num-tasks {split_count}
```

## Run Tests

```bash
python -m unittest discover -s queue/test -v
```

## Web Interface

Start a local browser UI:

```bash
python queue/web.py
```

Open:

```text
http://127.0.0.1:8765
```

The web UI can generate arbitrary shell commands from templates, copy them, submit them directly to the SQLite queue, and view the task table and logs. Queue status pages refresh every 5 seconds by default, with an interval field that can be changed or set to 0 to disable refresh. Template selection opens a popup with hard-coded submit/worker examples first, then JSON templates from `queue/data/template/`.

Template placeholders:

- `{config:file}`: select one file, or select a folder to recursively generate one command per file.
- `{data:folder}`: select one folder and use that folder path as-is.
- `{name:text}` or `{worker type:name}`: plain text value.

Only one `file` placeholder may expand recursively from a selected folder. Multiple placeholders can be used in one template.

The first two built-in templates are queue examples:

```bash
/usr/bin/python queue/submit.py --queue-dir {queue-dir:folder} --type {worker type:name} -- {command:name}
/usr/bin/python queue/worker.py --queue-dir {queue-dir:folder} --type {worker type:name}
```

The right panel shows the exact command lines that will be submitted. If one file placeholder points to a folder, the preview expands it into one command per recursive file. Saving opens a popup with a name field and cancel option.

Saved templates are readable JSON files:

```text
queue/data/template/<name>.json
```

## API Reference

### `submit.py`

Submit one shell command:

```bash
python queue/submit.py [--queue-dir queue/data] [--type BASE] [--cwd PATH] -- <command>
```

Submit split commands:

```bash
python queue/submit.py [--queue-dir queue/data] [--type TYPE] [--cwd PATH] --splits N -- <command-template>
```

Supported placeholders in split command templates:

- `{split_id}`
- `{split_count}`
- `{task_id}`

### `worker.py`

Run a worker:

```bash
python queue/worker.py [--queue-dir queue/data] [--type BASE] [--poll-sec 5]
```

Run at most one task, useful for tests and manual execution:

```bash
python queue/worker.py --once
```

### `db.py`

Important internal helpers:

- `init_db(queue_dir=None)`: create the SQLite database and log directory.
- `submit_task(command, ...)`: insert one pending task.
- `claim_next_task(worker_type, worker_id, ...)`: atomically claim one pending task.
- `finish_task(task_id, state, return_code, ...)`: mark a running task as `done` or `failed`.
- `cancel_task(task_id, ...)`: cancel a pending task.
- `get_task(task_id, ...)`: read one task row.
- `list_tasks(...)`: list task rows.
- `list_events(task_id, ...)`: list state-history events for one task.
