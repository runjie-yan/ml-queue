from __future__ import annotations

import argparse
import html
import json
import re
from dataclasses import dataclass
from itertools import product
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import db


REPO_ROOT = Path(__file__).resolve().parents[1]
PLACEHOLDER_RE = re.compile(r"(?<!\{)\{([A-Za-z0-9_][A-Za-z0-9_ -]*?)(?::(file|folder|text|name))?\}(?!\})")
TEMPLATE_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
LOG_TAIL_BYTES = 64 * 1024
DEFAULT_REFRESH_SEC = 5
TASK_SORT_KEYS = {
    "id": "ID",
    "type": "Type",
    "state": "State",
    "rc": "RC",
    "command": "Command",
}

BUILTIN_TEMPLATES = [
    {
        "version": 1,
        "name": "example-submit-task",
        "template": "/usr/bin/python queue/submit.py --queue-dir {queue-dir:folder} --type {worker type:name} -- {command:name}",
        "worker_type": "BASE",
        "cwd": str(REPO_ROOT),
        "values": {"queue-dir": "queue/data", "worker type": "BASE", "command": "echo hello"},
    },
    {
        "version": 1,
        "name": "example-worker",
        "template": "/usr/bin/python queue/worker.py --queue-dir {queue-dir:folder} --type {worker type:name}",
        "worker_type": "BASE",
        "cwd": str(REPO_ROOT),
        "values": {"queue-dir": "queue/data", "worker type": "BASE"},
    },
]
BUILTIN_TEMPLATE_BY_NAME = {str(item["name"]): item for item in BUILTIN_TEMPLATES}


@dataclass(frozen=True)
class Placeholder:
    name: str
    kind: str


class TemplateError(ValueError):
    pass


def templates_dir(queue_dir: str | Path) -> Path:
    path = Path(queue_dir).resolve() / "template"
    path.mkdir(parents=True, exist_ok=True)
    return path


def template_path(queue_dir: str | Path, name: str) -> Path:
    clean = name.strip()
    if clean.endswith(".json"):
        clean = clean[:-5]
    if not clean:
        raise TemplateError("template name must not be empty")
    if not TEMPLATE_NAME_RE.match(clean):
        raise TemplateError("template name can only contain letters, numbers, underscore, dash, and dot")
    return templates_dir(queue_dir) / f"{clean}.json"


def save_template(
    *,
    queue_dir: str | Path,
    name: str,
    template: str,
    worker_type: str,
    cwd: str,
    values: dict[str, str],
) -> Path:
    path = template_path(queue_dir, name)
    payload = {
        "version": 1,
        "name": path.stem,
        "template": template,
        "worker_type": worker_type,
        "cwd": cwd,
        "values": values,
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return path


def list_saved_templates(queue_dir: str | Path) -> list[str]:
    return sorted(path.stem for path in templates_dir(queue_dir).glob("*.json") if path.is_file())


def load_template(queue_dir: str | Path, name: str) -> dict[str, Any]:
    path = template_path(queue_dir, name)
    if not path.exists():
        raise TemplateError(f"template not found: {name}")
    data = json.loads(path.read_text())
    if data.get("version") != 1:
        raise TemplateError(f"unsupported template version in {name}")
    if not isinstance(data.get("template"), str):
        raise TemplateError(f"template file is missing template text: {name}")
    if not isinstance(data.get("values", {}), dict):
        raise TemplateError(f"template values must be a JSON object: {name}")
    return data


def load_builtin_template(name: str) -> dict[str, Any] | None:
    item = BUILTIN_TEMPLATE_BY_NAME.get(name)
    return dict(item) if item else None


def load_named_template(queue_dir: str | Path, name: str) -> dict[str, Any]:
    builtin = load_builtin_template(name)
    return builtin if builtin else load_template(queue_dir, name)


def template_summary(queue_dir: str | Path, *, max_len: int = 120) -> list[dict[str, str]]:
    summaries: list[dict[str, str]] = []
    for item in BUILTIN_TEMPLATES:
        template = str(item["template"])
        summaries.append({"name": str(item["name"]), "template": shorten(template, max_len)})
    for name in list_saved_templates(queue_dir):
        if name in BUILTIN_TEMPLATE_BY_NAME:
            continue
        try:
            summaries.append({"name": name, "template": shorten(str(load_template(queue_dir, name)["template"]), max_len)})
        except TemplateError as exc:
            summaries.append({"name": name, "template": f"ERROR: {exc}"})
    return summaries


def shorten(text: str, max_len: int) -> str:
    return text if len(text) <= max_len else text[: max_len - 3] + "..."


def parse_placeholders(template: str) -> list[Placeholder]:
    seen: dict[str, str] = {}
    placeholders: list[Placeholder] = []
    for match in PLACEHOLDER_RE.finditer(template):
        name = match.group(1).strip()
        kind = match.group(2) or "name"
        previous = seen.get(name)
        if previous and previous != kind:
            raise TemplateError(f"placeholder {{{name}}} is used as both {previous} and {kind}")
        if previous is None:
            seen[name] = kind
            placeholders.append(Placeholder(name=name, kind=kind))
    return placeholders


def resolve_under_root(path_text: str, *, root: Path = REPO_ROOT) -> Path:
    if not path_text.strip():
        raise TemplateError("placeholder path must not be empty")
    raw = Path(path_text).expanduser()
    path = raw if raw.is_absolute() else root / raw
    resolved = path.resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as exc:
        raise TemplateError(f"path is outside repo root: {path_text}") from exc
    return resolved


def display_path(path: Path, *, root: Path = REPO_ROOT) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(path)


def recursive_files(folder: Path) -> list[Path]:
    return sorted([path for path in folder.rglob("*") if path.is_file()], key=lambda path: path.as_posix())


def render_template(template: str, values: dict[str, str]) -> str:
    return PLACEHOLDER_RE.sub(lambda match: values[match.group(1).strip()], template)


def value_lines(value: str) -> list[str]:
    return [line.strip() for line in value.splitlines() if line.strip()]


def resolve_placeholder_values(placeholder: Placeholder, raw_value: str, *, root: Path) -> list[str]:
    lines = value_lines(raw_value)
    if not lines:
        raise TemplateError(f"placeholder {{{placeholder.name}:{placeholder.kind}}} must not be empty")

    if placeholder.kind in {"name", "text"}:
        return lines

    resolved: list[str] = []
    for line in lines:
        selected = resolve_under_root(line, root=root)
        if placeholder.kind == "folder":
            if not selected.is_dir():
                raise TemplateError(f"folder placeholder {{{placeholder.name}:folder}} requires a folder: {line}")
            resolved.append(display_path(selected, root=root))
            continue

        if selected.is_file():
            resolved.append(display_path(selected, root=root))
        elif selected.is_dir():
            files = recursive_files(selected)
            if not files:
                raise TemplateError(f"folder has no recursive files: {line}")
            resolved.extend(display_path(path, root=root) for path in files)
        else:
            raise TemplateError(f"file placeholder {{{placeholder.name}:file}} path does not exist: {line}")
    return resolved


def expand_template(template: str, values: dict[str, str], *, root: Path = REPO_ROOT) -> list[dict[str, Any]]:
    placeholders = parse_placeholders(template)
    resolved_options: dict[str, list[str]] = {}
    expanded_names: list[str] = []

    for placeholder in placeholders:
        if placeholder.name not in values:
            raise TemplateError(f"missing value for placeholder {{{placeholder.name}:{placeholder.kind}}}")
        options = resolve_placeholder_values(placeholder, values[placeholder.name], root=root)
        resolved_options[placeholder.name] = options
        if len(options) > 1:
            expanded_names.append(placeholder.name)

    results: list[dict[str, Any]] = []
    names = [placeholder.name for placeholder in placeholders]
    for combo in product(*(resolved_options[name] for name in names)):
        item_values = dict(zip(names, combo))
        results.append({
            "command": render_template(template, item_values),
            "values": item_values,
            "expanded_placeholder": ",".join(expanded_names) if expanded_names else None,
        })
    return results


def form_values(params: dict[str, list[str]]) -> dict[str, str]:
    return {key[4:]: value[0] if value else "" for key, value in params.items() if key.startswith("ph__")}


def placeholder_inputs(template: str, values: dict[str, str]) -> str:
    fields = []
    for placeholder in parse_placeholders(template):
        value = values.get(placeholder.name, "")
        fields.append(
            f'<div><label>{html.escape(placeholder.name)} <span class="muted">{html.escape(placeholder.kind)}</span></label>'
            f'<textarea class="placeholder-value" data-placeholder="{html.escape(placeholder.name)}" name="ph__{html.escape(placeholder.name)}" rows="2">{html.escape(value)}</textarea></div>'
        )
    return "".join(fields) if fields else '<p class="muted">No placeholders found.</p>'


def refresh_seconds(params: dict[str, list[str]]) -> int:
    raw = params.get("refresh", [str(DEFAULT_REFRESH_SEC)])[0]
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_REFRESH_SEC
    return max(0, value)


def refresh_controls(path: str, params: dict[str, list[str]]) -> str:
    refresh = refresh_seconds(params)
    hidden_inputs = []
    for key, values in params.items():
        if key == "refresh":
            continue
        for value in values:
            hidden_inputs.append(f'<input type="hidden" name="{html.escape(key)}" value="{html.escape(value)}">')
    return f"""
<form method="get" action="{html.escape(path)}" class="surface refresh-control">
  {''.join(hidden_inputs)}
  <label>Refresh Interval Seconds</label>
  <input name="refresh" type="number" min="0" value="{html.escape(str(refresh))}" data-refresh-seconds>
  <button type="submit">Apply</button>
  <span class="muted">Set to 0 to disable auto refresh.</span>
</form>
"""


def first_param(params: dict[str, list[str]], key: str, default: str = "") -> str:
    values = params.get(key)
    return values[0] if values else default


def sort_params(params: dict[str, list[str]]) -> tuple[str, str]:
    key = first_param(params, "sort", "id")
    if key not in TASK_SORT_KEYS:
        key = "id"
    order = first_param(params, "order", "asc").lower()
    if order not in {"asc", "desc"}:
        order = "asc"
    return key, order


def sort_tasks(tasks: list[dict[str, Any]], *, sort_key: str, order: str) -> list[dict[str, Any]]:
    def value(task: dict[str, Any]) -> Any:
        if sort_key == "type":
            return str(task["worker_type"])
        if sort_key == "state":
            return str(task["state"])
        if sort_key == "rc":
            return (task["return_code"] is None, task["return_code"] if task["return_code"] is not None else 0)
        return str(task[sort_key])

    return sorted(tasks, key=value, reverse=order == "desc")


def task_sort_link(params: dict[str, list[str]], *, key: str, label: str, current_key: str, current_order: str) -> str:
    next_order = "desc" if key == current_key and current_order == "asc" else "asc"
    query: dict[str, str] = {}
    for name in ("state", "type", "refresh"):
        value = first_param(params, name)
        if value:
            query[name] = value
    query["sort"] = key
    query["order"] = next_order
    marker = " ^" if key == current_key and current_order == "asc" else " v" if key == current_key else ""
    return f'<a href="/tasks?{html.escape(urlencode(query), quote=True)}">{html.escape(label + marker)}</a>'


def submit_generated_tasks(
    generated: list[dict[str, Any]],
    *,
    queue_dir: str,
    worker_type: str,
    cwd: str,
    template: str,
) -> list[str]:
    task_ids: list[str] = []
    for item in generated:
        task_ids.append(
            db.submit_task(
                item["command"],
                queue_dir=queue_dir,
                worker_type=worker_type,
                cwd=cwd,
                params={
                    "source": "web-template",
                    "template": template,
                    "template_values": item["values"],
                    "expanded_placeholder": item["expanded_placeholder"],
                },
            )
        )
    return task_ids


def tail_text(path_text: str | None, *, limit: int = LOG_TAIL_BYTES) -> str:
    if not path_text:
        return ""
    path = Path(path_text)
    if not path.is_file():
        return ""
    with path.open("rb") as handle:
        handle.seek(0, 2)
        size = handle.tell()
        handle.seek(max(0, size - limit), 0)
        data = handle.read()
    return data.decode("utf-8", errors="replace")


def json_response(handler: BaseHTTPRequestHandler, payload: dict[str, Any], status: int = 200) -> None:
    data = json.dumps(payload).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def page(title: str, body: str) -> bytes:
    return f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>{html.escape(title)}</title>
<style>
body {{ margin: 0; background: #f7fafc; color: #1f2933; font-family: system-ui, -apple-system, Segoe UI, sans-serif; }}
.topbar {{ background: #fff; border-bottom: 1px solid #d9e2ec; padding: 14px 24px; display: flex; justify-content: space-between; }}
.brand {{ font-weight: 750; }}
nav {{ display: flex; gap: 8px; }}
nav a {{ color: #334e68; text-decoration: none; padding: 6px 10px; border-radius: 6px; }}
nav a:hover {{ background: #e6f6ff; color: #075985; }}
.page {{ max-width: 1440px; margin: 0 auto; padding: 24px; }}
h1 {{ margin: 0 0 12px; font-size: 24px; }}
h2 {{ margin: 0 0 10px; font-size: 16px; }}
textarea, input {{ box-sizing: border-box; width: 100%; border: 1px solid #bcccdc; border-radius: 6px; padding: 8px 9px; font: inherit; }}
textarea {{ min-height: 132px; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }}
#template-text {{ overflow: auto; white-space: pre; word-wrap: normal; resize: vertical; tab-size: 4; }}
.placeholder-value {{ min-height: 70px; resize: vertical; white-space: pre; overflow: auto; line-height: 1.45; }}
label {{ display: block; font-weight: 650; margin-top: 12px; margin-bottom: 4px; }}
button {{ border: 1px solid #0ea5e9; border-radius: 6px; background: #0284c7; color: white; padding: 8px 12px; margin-top: 12px; margin-right: 8px; cursor: pointer; font-weight: 650; }}
button.secondary {{ background: #fff; color: #075985; }}
button.danger {{ background: #fff; color: #b42318; border-color: #fca5a5; }}
pre {{ background: #102a43; color: #f0f4f8; border-radius: 6px; padding: 12px; overflow: auto; white-space: pre-wrap; }}
table {{ border-collapse: collapse; width: 100%; margin-top: 12px; background: #fff; }}
th, td {{ border-bottom: 1px solid #d9e2ec; padding: 7px 8px; text-align: left; vertical-align: top; }}
th {{ background: #f0f4f8; }}
.workspace {{ display: grid; grid-template-columns: minmax(0, 1fr) minmax(360px, .58fr); gap: 18px; align-items: start; }}
.surface {{ background: #fff; border: 1px solid #d9e2ec; border-radius: 8px; padding: 16px; }}
.grid {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; }}
.muted {{ color: #627d98; }}
.error {{ color: #b42318; font-weight: 650; }}
.actions {{ display: flex; gap: 8px; flex-wrap: wrap; }}
.cmd {{ background: #f0f4f8; color: #102a43; }}
.modal {{ position: fixed; inset: 0; background: rgba(15, 23, 42, .38); display: none; align-items: center; justify-content: center; padding: 24px; }}
.modal.open {{ display: flex; }}
.modal-panel {{ width: min(760px, 100%); max-height: 80vh; overflow: auto; background: #fff; border-radius: 8px; padding: 16px; box-shadow: 0 18px 60px rgba(15, 23, 42, .25); }}
.template-card {{ display: block; border: 1px solid #d9e2ec; border-radius: 6px; padding: 10px; margin-top: 8px; text-decoration: none; color: inherit; background: #fff; }}
.template-card.selected {{ border-color: #075985; background: #e0f2fe; }}
.template-name {{ font-weight: 700; margin-bottom: 6px; }}
.template-snippet {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace; color: #486581; overflow-wrap: anywhere; }}
@media (max-width: 900px) {{ .workspace, .grid {{ grid-template-columns: 1fr; }} .topbar {{ flex-direction: column; gap: 10px; }} }}
</style>
<script>
const PH_RE = /(?<!\\{{)\\{{([A-Za-z0-9_][A-Za-z0-9_ -]*?)(?::(file|folder|text|name))?\\}}(?!\\}})/g;
function copyText(id) {{ navigator.clipboard.writeText(document.getElementById(id).textContent); }}
function esc(s) {{ return s.replace(/[&<>"']/g, c => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c])); }}
function values() {{
  const out = {{}};
  document.querySelectorAll('[data-placeholder]').forEach(i => out[i.dataset.placeholder] = i.value);
  return out;
}}
function refreshFields() {{
  const template = document.getElementById('template-text').value;
  const old = values();
  const seen = new Set();
  const parts = [];
  let m;
  while ((m = PH_RE.exec(template)) !== null) {{
    const name = m[1].trim();
    const kind = m[2] || 'name';
    if (seen.has(name)) continue;
    seen.add(name);
    parts.push(`<div><label>${{esc(name)}} <span class="muted">${{esc(kind)}}</span></label><textarea class="placeholder-value" data-placeholder="${{esc(name)}}" name="ph__${{esc(name)}}" rows="2">${{esc(old[name] || '')}}</textarea></div>`);
  }}
  document.getElementById('placeholder-fields').innerHTML = parts.length ? parts.join('') : '<p class="muted">No placeholders found.</p>';
  updatePreview();
}}
async function updatePreview() {{
  const payload = {{
    template: document.getElementById('template-text').value,
    values: values()
  }};
  const box = document.getElementById('live-preview');
  try {{
    const res = await fetch('/api/preview', {{method: 'POST', body: JSON.stringify(payload)}});
    const data = await res.json();
    box.textContent = data.ok ? data.commands.join('\\n') : data.error;
  }} catch (err) {{
    box.textContent = String(err);
  }}
}}
function openModal(id) {{ document.getElementById(id).classList.add('open'); }}
function closeModal(id) {{ document.getElementById(id).classList.remove('open'); }}
async function saveTemplate() {{
  const err = document.getElementById('save-error');
  err.textContent = '';
  const payload = {{
    name: document.getElementById('save-name').value,
    template: document.getElementById('template-text').value,
    cwd: document.getElementById('cwd').value,
    worker_type: values()['worker type'] || document.getElementById('submit-worker-type').value,
    values: values()
  }};
  const res = await fetch('/api/save-template', {{method: 'POST', body: JSON.stringify(payload)}});
  const data = await res.json();
  if (!data.ok) {{ err.textContent = data.error; return; }}
  window.location = '/?template=' + encodeURIComponent(data.name);
}}
function refreshIntervalSeconds() {{
  const input = document.querySelector('[data-refresh-seconds]');
  if (!input) return 0;
  const value = Number.parseInt(input.value || '0', 10);
  return Number.isFinite(value) && value > 0 ? value : 0;
}}
function apiUrl(path) {{
  const url = new URL(path, window.location.origin);
  document.querySelectorAll('.refresh-control input[name]').forEach(input => {{
    if (input.name !== 'refresh' && input.value) url.searchParams.append(input.name, input.value);
  }});
  return url.toString();
}}
async function refreshTaskList() {{
  const target = document.getElementById('task-list-data');
  if (!target) return;
  const res = await fetch(apiUrl('/api/tasks'));
  const data = await res.json();
  if (data.ok) target.innerHTML = data.html;
}}
function syncSelectAll() {{
  const selectAll = document.getElementById('select-all-tasks');
  if (!selectAll) return;
  const boxes = [...document.querySelectorAll('input[name="task_id"]:not(:disabled)')];
  const checked = boxes.filter(box => box.checked).length;
  selectAll.checked = boxes.length > 0 && checked === boxes.length;
  selectAll.indeterminate = checked > 0 && checked < boxes.length;
}}
function toggleAllTasks(checked) {{
  document.querySelectorAll('input[name="task_id"]:not(:disabled)').forEach(box => box.checked = checked);
  syncSelectAll();
}}
async function refreshTaskDetail() {{
  const target = document.getElementById('task-detail-data');
  if (!target) return;
  const res = await fetch(apiUrl('/api/task'));
  const data = await res.json();
  if (data.ok) target.innerHTML = data.html;
}}
function startPartialRefresh() {{
  const seconds = refreshIntervalSeconds();
  if (!seconds) return;
  const refresh = document.getElementById('task-list-data') ? refreshTaskList : refreshTaskDetail;
  window.setInterval(() => refresh().catch(err => console.error(err)), seconds * 1000);
}}
window.addEventListener('DOMContentLoaded', () => {{
  const templateText = document.getElementById('template-text');
  templateText?.addEventListener('input', refreshFields);
  templateText?.addEventListener('keydown', event => {{
    if (event.key !== 'Tab') return;
    event.preventDefault();
    const start = templateText.selectionStart;
    const end = templateText.selectionEnd;
    templateText.value = templateText.value.slice(0, start) + '\\t' + templateText.value.slice(end);
    templateText.selectionStart = templateText.selectionEnd = start + 1;
    refreshFields();
  }});
  document.getElementById('placeholder-fields')?.addEventListener('input', updatePreview);
  document.getElementById('cwd')?.addEventListener('input', updatePreview);
  document.addEventListener('change', event => {{
    if (event.target?.id === 'select-all-tasks') toggleAllTasks(event.target.checked);
    if (event.target?.name === 'task_id') syncSelectAll();
  }});
  updatePreview();
  startPartialRefresh();
  syncSelectAll();
}});
</script>
</head>
<body>
<header class="topbar"><div class="brand">Queue</div><nav><a href="/">Submit</a><a href="/tasks">Tasks</a></nav></header>
<main class="page">{body}</main>
</body>
</html>""".encode("utf-8")


class QueueWebHandler(BaseHTTPRequestHandler):
    queue_dir: str
    repo_root: Path

    def write_html(self, title: str, body: str, status: int = 200) -> None:
        data = page(title, body)
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def read_post(self) -> dict[str, list[str]]:
        length = int(self.headers.get("Content-Length", "0"))
        return parse_qs(self.rfile.read(length).decode("utf-8"), keep_blank_values=True)

    def read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(length).decode("utf-8") or "{}")

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        if parsed.path == "/":
            self.write_html("Queue Submit", self.submit_form(selected=params.get("template", ["example-submit-task"])[0]))
        elif parsed.path == "/tasks":
            self.write_html("Queue Tasks", self.tasks_page(params))
        elif parsed.path == "/task":
            self.write_html("Queue Task", self.task_page(params))
        elif parsed.path == "/api/tasks":
            self.api_tasks(params)
        elif parsed.path == "/api/task":
            self.api_task(params)
        else:
            self.write_html("Not Found", "<h1>Not Found</h1>", status=404)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/submit":
            self.write_html("Submitted Commands", self.submit_page(self.read_post()))
        elif parsed.path == "/delete-tasks":
            self.write_html("Queue Tasks", self.delete_tasks_page(self.read_post()))
        elif parsed.path == "/api/preview":
            self.api_preview()
        elif parsed.path == "/api/save-template":
            self.api_save_template()
        else:
            self.write_html("Not Found", "<h1>Not Found</h1>", status=404)

    def selected_template(self, name: str) -> dict[str, Any]:
        try:
            return load_named_template(self.queue_dir, name)
        except TemplateError:
            return dict(BUILTIN_TEMPLATES[0])

    def submit_form(self, message: str = "", selected: str = "example-submit-task", form_state: dict[str, Any] | None = None) -> str:
        loaded = self.selected_template(selected)
        template = str(loaded.get("template", ""))
        cwd = str(loaded.get("cwd", self.repo_root))
        worker_type = str(loaded.get("worker_type", "BASE"))
        values = {str(k): str(v) for k, v in loaded.get("values", {}).items()}
        selected_name = str(loaded.get("name", selected))
        if form_state:
            template = str(form_state.get("template", template))
            cwd = str(form_state.get("cwd", cwd))
            worker_type = str(form_state.get("worker_type", worker_type))
            values = {str(k): str(v) for k, v in form_state.get("values", values).items()}
            selected_name = str(form_state.get("selected_template", selected_name))

        placeholders = placeholder_inputs(template, values)
        return f"""
<h1>Command Template</h1>
{message}
<div class="workspace">
  <section class="surface">
    <div class="actions"><button type="button" onclick="openModal('template-modal')">Select Template</button><button type="button" class="secondary" onclick="openModal('save-modal')">Save Template</button></div>
    <p class="muted">Template: <code>{html.escape(selected_name)}</code></p>
    <form id="template-form" method="post" action="/submit">
      <input type="hidden" name="selected_template" value="{html.escape(selected_name)}">
      <input id="submit-worker-type" type="hidden" name="submit_worker_type" value="{html.escape(worker_type)}">
      <label>Working Directory</label>
      <input id="cwd" name="cwd" value="{html.escape(cwd)}">
      <label>Command Template</label>
      <textarea id="template-text" name="template">{html.escape(template)}</textarea>
      <label>Placeholder Values</label>
      <div id="placeholder-fields" class="grid">{placeholders}</div>
    </form>
  </section>
  <aside class="surface">
    <h2>Exact Command Preview</h2>
    <pre id="live-preview" class="cmd"></pre>
    <div class="actions"><button onclick="copyText('live-preview')">Copy</button><button form="template-form" type="submit">Submit To Queue</button></div>
    <p class="muted">Each placeholder line is one expansion value. File placeholders also expand folders into one value per recursive file.</p>
  </aside>
</div>
{self.template_modal(selected_name)}
{self.save_modal()}
"""

    def template_modal(self, selected: str) -> str:
        cards = []
        for item in template_summary(self.queue_dir):
            name = item["name"]
            cls = "template-card selected" if name == selected else "template-card"
            cards.append(
                f'<a class="{cls}" href="/?{urlencode({"template": name})}"><div class="template-name">{html.escape(name)}</div><div class="template-snippet">{html.escape(item["template"])}</div></a>'
            )
        return f"""
<div id="template-modal" class="modal"><div class="modal-panel">
  <h2>Select Template</h2>
  {''.join(cards)}
  <button type="button" class="secondary" onclick="closeModal('template-modal')">Cancel</button>
</div></div>
"""

    def save_modal(self) -> str:
        return """
<div id="save-modal" class="modal"><div class="modal-panel">
  <h2>Save Template</h2>
  <label>Template Name</label>
  <input id="save-name" placeholder="train-default">
  <p id="save-error" class="error"></p>
  <div class="actions"><button type="button" onclick="saveTemplate()">Save</button><button type="button" class="secondary" onclick="closeModal('save-modal')">Cancel</button></div>
</div></div>
"""

    def api_preview(self) -> None:
        payload = self.read_json()
        try:
            generated = expand_template(str(payload.get("template", "")), {str(k): str(v) for k, v in payload.get("values", {}).items()}, root=self.repo_root)
        except TemplateError as exc:
            json_response(self, {"ok": False, "error": str(exc)})
            return
        json_response(self, {"ok": True, "commands": [item["command"] for item in generated]})

    def api_save_template(self) -> None:
        payload = self.read_json()
        try:
            path = save_template(
                queue_dir=self.queue_dir,
                name=str(payload.get("name", "")),
                template=str(payload.get("template", "")),
                worker_type=str(payload.get("worker_type", "BASE")),
                cwd=str(payload.get("cwd", self.repo_root)),
                values={str(k): str(v) for k, v in payload.get("values", {}).items()},
            )
        except (TemplateError, OSError) as exc:
            json_response(self, {"ok": False, "error": str(exc)})
            return
        json_response(self, {"ok": True, "name": path.stem})

    def api_tasks(self, params: dict[str, list[str]]) -> None:
        json_response(self, {"ok": True, "html": self.tasks_data_html(params)})

    def api_task(self, params: dict[str, list[str]]) -> None:
        task_id = params.get("id", [""])[0]
        task = db.get_task(task_id, queue_dir=self.queue_dir)
        if task is None:
            json_response(self, {"ok": False, "error": "task not found"}, status=404)
            return
        json_response(self, {"ok": True, "html": self.task_detail_html(task_id)})

    def submit_page(self, params: dict[str, list[str]]) -> str:
        template = params.get("template", [""])[0]
        cwd = params.get("cwd", [str(self.repo_root)])[0] or str(self.repo_root)
        values = form_values(params)
        worker_type = values.get("worker type") or params.get("submit_worker_type", ["BASE"])[0] or "BASE"
        form_state = {
            "template": template,
            "worker_type": worker_type,
            "cwd": cwd,
            "selected_template": params.get("selected_template", ["example-submit-task"])[0],
            "values": values,
        }
        try:
            generated = expand_template(template, values, root=self.repo_root)
        except TemplateError as exc:
            return self.submit_form(f'<p class="error">{html.escape(str(exc))}</p>', form_state=form_state)
        task_ids = submit_generated_tasks(generated, queue_dir=self.queue_dir, worker_type=worker_type, cwd=cwd, template=template)
        return self.submit_form(
            f"<p>Submitted {len(task_ids)} task(s) as worker type <code>{html.escape(worker_type)}</code>.</p>{self.submitted_commands(generated, task_ids)}",
            form_state=form_state,
        )

    def submitted_commands(self, generated: list[dict[str, Any]], task_ids: list[str]) -> str:
        rows = []
        for index, item in enumerate(generated):
            rows.append(f"<tr><td>{index}</td><td>{html.escape(task_ids[index])}</td><td>{html.escape(item['command'])}</td></tr>")
        return f"<section class='surface'><h2>Submitted Commands</h2><table><thead><tr><th>#</th><th>Task ID</th><th>Command</th></tr></thead><tbody>{''.join(rows)}</tbody></table></section>"

    def generated_preview_html(self, generated: list[dict[str, Any]], task_ids: list[str]) -> str:
        commands = "\n".join(str(item["command"]) for item in generated)
        return f'<h2>Submitted Commands</h2><pre id="all-shell-commands">{html.escape(commands)}</pre><button onclick="copyText(\'all-shell-commands\')">Copy All Shell Commands</button>'

    def tasks_data_html(self, params: dict[str, list[str]]) -> str:
        state = params.get("state", [""])[0] or None
        worker_type = params.get("type", [""])[0] or None
        sort_key, order = sort_params(params)
        rows = []
        tasks = db.list_tasks(queue_dir=self.queue_dir, state=state, worker_type=worker_type)
        for task in sort_tasks(tasks, sort_key=sort_key, order=order):
            command = shorten(str(task["command"]), 120)
            disabled = " disabled" if task["state"] == "running" else ""
            rows.append(
                f'<tr><td><input type="checkbox" name="task_id" value="{html.escape(task["id"])}"{disabled}></td><td><a href="/task?{urlencode({"id": task["id"]})}">{html.escape(task["id"])}</a></td><td>{html.escape(task["state"])}</td><td>{html.escape(task["worker_type"])}</td><td>{html.escape(str(task["return_code"])) if task["return_code"] is not None else ""}</td><td>{html.escape(command)}</td></tr>'
            )
        headers = [
            '<th><input id="select-all-tasks" type="checkbox" title="Select all visible tasks"></th>',
            f"<th>{task_sort_link(params, key='id', label='ID', current_key=sort_key, current_order=order)}</th>",
            f"<th>{task_sort_link(params, key='state', label='State', current_key=sort_key, current_order=order)}</th>",
            f"<th>{task_sort_link(params, key='type', label='Type', current_key=sort_key, current_order=order)}</th>",
            f"<th>{task_sort_link(params, key='rc', label='RC', current_key=sort_key, current_order=order)}</th>",
            f"<th>{task_sort_link(params, key='command', label='Command', current_key=sort_key, current_order=order)}</th>",
        ]
        hidden_filters = "".join(
            f'<input type="hidden" name="{html.escape(key)}" value="{html.escape(value)}">'
            for key in ("state", "type", "refresh", "sort", "order")
            for value in params.get(key, [])
            if value
        )
        return f"""
<form method="post" action="/delete-tasks">
{hidden_filters}
<div class="actions"><button type="submit" class="danger">Delete Selected</button><span class="muted">Running tasks cannot be deleted from this view.</span></div>
<table><thead><tr>{''.join(headers)}</tr></thead><tbody>{''.join(rows)}</tbody></table>
</form>
"""

    def tasks_page(self, params: dict[str, list[str]]) -> str:
        message = params.get("message", [""])[0]
        state = params.get("state", [""])[0] or None
        worker_type = params.get("type", [""])[0] or None
        sort_key, order = sort_params(params)
        refresh = refresh_seconds(params)
        message_html = f'<p>{html.escape(message)}</p>' if message else ""
        return f"""
<h1>Tasks</h1>
{message_html}
{refresh_controls("/tasks", params)}
<form method="get" action="/tasks" class="surface"><label>State</label><input name="state" value="{html.escape(state or "")}"><label>Worker Type</label><input name="type" value="{html.escape(worker_type or "")}"><input type="hidden" name="refresh" value="{html.escape(str(refresh))}"><input type="hidden" name="sort" value="{html.escape(sort_key)}"><input type="hidden" name="order" value="{html.escape(order)}"><button type="submit">Filter</button></form>
<div id="task-list-data">
{self.tasks_data_html(params)}
</div>
"""

    def delete_tasks_page(self, params: dict[str, list[str]]) -> str:
        result = db.delete_tasks(params.get("task_id", []), queue_dir=self.queue_dir)
        parts = [f"Deleted {result['deleted']} of {result['requested']} selected task(s)."]
        if result["skipped_running"]:
            parts.append(f"Skipped {result['skipped_running']} running task(s).")
        if result["missing"]:
            parts.append(f"{result['missing']} task(s) were already missing.")
        next_params = {
            key: values
            for key, values in params.items()
            if key in {"state", "type", "refresh", "sort", "order"}
        }
        next_params["message"] = [" ".join(parts)]
        return self.tasks_page(next_params)

    def task_detail_html(self, task_id: str) -> str:
        task = db.get_task(task_id, queue_dir=self.queue_dir)
        if task is None:
            return "<h1>Task Not Found</h1>"
        events = db.list_events(task_id, queue_dir=self.queue_dir)
        event_rows = "".join(
            f"<tr><td>{html.escape(str(event['ts']))}</td><td>{html.escape(str(event['from_state']))}</td><td>{html.escape(str(event['to_state']))}</td><td>{html.escape(str(event['message']))}</td></tr>"
            for event in events
        )
        return f"""
<h2>Metadata</h2><pre>{html.escape(json.dumps(task, indent=2, sort_keys=True))}</pre>
<h2>Events</h2><table><thead><tr><th>Time</th><th>From</th><th>To</th><th>Message</th></tr></thead><tbody>{event_rows}</tbody></table>
<h2>stdout</h2><pre>{html.escape(tail_text(task.get("stdout_path")))}</pre>
<h2>stderr</h2><pre>{html.escape(tail_text(task.get("stderr_path")))}</pre>
"""

    def task_page(self, params: dict[str, list[str]]) -> str:
        task_id = params.get("id", [""])[0]
        if db.get_task(task_id, queue_dir=self.queue_dir) is None:
            return "<h1>Task Not Found</h1>"
        return f"""
<h1>Task {html.escape(task_id)}</h1>
{refresh_controls("/task", params)}
<div id="task-detail-data">
{self.task_detail_html(task_id)}
</div>
"""


def build_handler(queue_dir: str, repo_root: Path) -> type[QueueWebHandler]:
    class ConfiguredQueueWebHandler(QueueWebHandler):
        pass

    ConfiguredQueueWebHandler.queue_dir = queue_dir
    ConfiguredQueueWebHandler.repo_root = repo_root.resolve()
    return ConfiguredQueueWebHandler


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Local web UI for the lightweight queue.")
    parser.add_argument("--queue-dir", default=str(db.default_queue_dir()))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--repo-root", default=str(REPO_ROOT))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    db.init_db(args.queue_dir)
    server = ThreadingHTTPServer(
        (args.host, args.port),
        build_handler(args.queue_dir, Path(args.repo_root).resolve()),
    )
    print(f"Queue web UI: http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
