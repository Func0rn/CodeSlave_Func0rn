#!/usr/bin/env python3
from __future__ import annotations

import argparse
import curses
import importlib.util
import json
import os
import signal
import subprocess
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


TOOL_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = TOOL_ROOT / "workspaces"
DEFAULT_TEMPLATE_DIR = TOOL_ROOT / "templates" / "PhpCodeCheck"
FLOWS_DIR = TOOL_ROOT / "flows"
RUNTIME_DIR = TOOL_ROOT / "runtime"
LOG_DIR = RUNTIME_DIR / "logs"
STATE_PATH = RUNTIME_DIR / "state.json"
DEFAULT_FLOW = "php_code_check"
FLOW_MIGRATIONS = {
    "task_only": "php_code_check",
    "demo_project": "php_code_check",
}
FLOW_MODES = {
    "prompt-only": {
        "description": "Minimal workspace with Task + Agent.md placeholder.",
        "input_label": "Task Input",
        "worker_instructions": (
            "先读取当前目录中的 `Task`。\n"
            "如果存在 `Agent.md`，一并参考。\n"
            "直接根据任务内容执行，不要假设这是仓库导入流程。"
        ),
    },
    "template-copy": {
        "description": "Copy the default template, then rewrite Task first line.",
        "input_label": "GitHub URL",
        "worker_instructions": (
            "你当前工作目录中已经准备好了 `Agent.md` 和 `Task`。\n\n"
            "执行要求：\n"
            "1. 先阅读并遵循 `Agent.md`\n"
            "2. 再严格完成 `Task`\n"
            "3. 尽量持续推进，不要停在等待人工确认\n"
            "4. 所有产出都保留在当前工作目录"
        ),
    },
}


@dataclass
class Job:
    id: str
    url: str
    repo_name: str
    workspace: str
    status: str = "queued"
    created_at: float = 0.0
    started_at: float | None = None
    ended_at: float | None = None
    pid: int | None = None
    exit_code: int | None = None
    log_path: str = ""
    error: str = ""
    flow_name: str = DEFAULT_FLOW


@dataclass
class FlowDefinition:
    name: str
    path: str
    description: str
    input_label: str
    allow_batch_file: bool


class Overseer:
    def __init__(
        self,
        template_dir: Path = DEFAULT_TEMPLATE_DIR,
        workspace_root: Path = WORKSPACE_ROOT,
        max_workers: int = 2,
        auto_start: bool = True,
        start_scheduler: bool = True,
    ) -> None:
        self.template_dir = template_dir
        self.workspace_root = workspace_root
        self.max_workers = max_workers
        self.auto_start = auto_start
        self.lock = threading.RLock()
        self.jobs: list[Job] = []
        self.procs: dict[str, subprocess.Popen[str]] = {}
        self.stop_event = threading.Event()
        self.selected = 0
        self.scheduler: threading.Thread | None = None
        self.current_flow = DEFAULT_FLOW
        self.flows = self._load_flows()
        if not self.flows:
            raise RuntimeError(f"no flows found in {FLOWS_DIR}")
        if self.current_flow not in self.flows:
            self.current_flow = sorted(self.flows)[0]
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        self._load_state()
        if start_scheduler:
            self.scheduler = threading.Thread(target=self._scheduler_loop, daemon=True)
            self.scheduler.start()

    def _load_state(self) -> None:
        if not STATE_PATH.exists():
            return
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        self.max_workers = int(data.get("max_workers", self.max_workers))
        self.auto_start = bool(data.get("auto_start", self.auto_start))
        self.current_flow = self._normalize_flow_name(data.get("current_flow", self.current_flow))
        if self.current_flow not in self.flows:
            self.current_flow = sorted(self.flows)[0]
        jobs: list[Job] = []
        for item in data.get("jobs", []):
            item["flow_name"] = self._normalize_flow_name(item.get("flow_name", self.current_flow))
            jobs.append(Job(**item))
        self.jobs = jobs

    def _save_state(self) -> None:
        with self.lock:
            payload = {
                "max_workers": self.max_workers,
                "auto_start": self.auto_start,
                "current_flow": self.current_flow,
                "jobs": [asdict(job) for job in self.jobs],
            }
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def shutdown(self) -> None:
        self.stop_event.set()
        if self.scheduler is not None:
            self.scheduler.join(timeout=1.0)
        self._save_state()

    def add_job(self, url: str) -> Job:
        repo_name = self._repo_name_from_url(url)
        workspace = self.workspace_root / self.current_flow / repo_name
        job = Job(
            id=uuid.uuid4().hex[:8],
            url=url.strip(),
            repo_name=repo_name,
            workspace=str(workspace),
            created_at=time.time(),
            log_path=str(LOG_DIR / f"{repo_name}-{int(time.time())}.log"),
            flow_name=self.current_flow,
        )
        with self.lock:
            self.jobs.append(job)
            self.selected = len(self.jobs) - 1
        self._save_state()
        return job

    def set_flow(self, flow_name: str) -> None:
        flow_name = self._normalize_flow_name(flow_name)
        if flow_name not in self.flows:
            raise ValueError(f"unknown flow: {flow_name}")
        self.current_flow = flow_name
        self._save_state()

    @staticmethod
    def _normalize_flow_name(flow_name: str) -> str:
        return FLOW_MIGRATIONS.get(flow_name, flow_name)

    def retry_job(self, job: Job) -> None:
        with self.lock:
            if job.status == "running":
                return
            job.status = "queued"
            job.started_at = None
            job.ended_at = None
            job.pid = None
            job.exit_code = None
            job.error = ""
            job.log_path = str(LOG_DIR / f"{job.repo_name}-{int(time.time())}.log")
        self._save_state()

    def delete_job(self, job: Job) -> None:
        if job.status == "running":
            self.cancel_job(job)
            time.sleep(0.2)
        with self.lock:
            self.procs.pop(job.id, None)
            self.jobs = [item for item in self.jobs if item.id != job.id]
            self.selected = min(self.selected, max(len(self.jobs) - 1, 0))
        self._save_state()

    def cancel_job(self, job: Job) -> None:
        proc = self.procs.get(job.id)
        if not proc:
            return
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass

    def _scheduler_loop(self) -> None:
        while not self.stop_event.is_set():
            self._poll_processes()
            if self.auto_start:
                self._start_queued_jobs()
            time.sleep(1.0)

    def _poll_processes(self) -> None:
        for job_id, proc in list(self.procs.items()):
            code = proc.poll()
            if code is None:
                continue
            with self.lock:
                job = self._find_job(job_id)
                if job is None:
                    self.procs.pop(job_id, None)
                    continue
                job.exit_code = code
                job.ended_at = time.time()
                if job.status == "cancelled":
                    pass
                elif code == 0:
                    job.status = "succeeded"
                else:
                    job.status = "failed"
                    if not job.error:
                        job.error = f"codex exited with code {code}"
                job.pid = None
                self.procs.pop(job_id, None)
            self._save_state()

    def _start_queued_jobs(self) -> None:
        with self.lock:
            running = sum(1 for job in self.jobs if job.status == "running")
            capacity = max(self.max_workers - running, 0)
            queued = [job for job in self.jobs if job.status == "queued"][:capacity]
        for job in queued:
            self._launch_job(job)

    def _launch_job(self, job: Job) -> None:
        with self.lock:
            if job.status != "queued":
                return
            job.status = "preparing"
            job.error = ""
        self._save_state()

        try:
            self._prepare_workspace(job)
            prompt = self._build_prompt(job)
            log_path = Path(job.log_path)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_file = log_path.open("w", encoding="utf-8")
            proc = subprocess.Popen(
                [
                    "codex",
                    "exec",
                    "--skip-git-repo-check",
                    "--dangerously-bypass-approvals-and-sandbox",
                    "-C",
                    job.workspace,
                    "-",
                ],
                cwd=job.workspace,
                stdin=subprocess.PIPE,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
            )
            assert proc.stdin is not None
            proc.stdin.write(prompt)
            proc.stdin.close()
            with self.lock:
                job.status = "running"
                job.started_at = time.time()
                job.pid = proc.pid
                self.procs[job.id] = proc
        except Exception as exc:  # noqa: BLE001
            with self.lock:
                job.status = "failed"
                job.error = str(exc)
                job.ended_at = time.time()
        self._save_state()

    def _prepare_workspace(self, job: Job) -> None:
        flow_module = self._flow_module(job.flow_name)
        flow_module.prepare_workspace(
            {
                "job": asdict(job),
                "workspace": job.workspace,
                "workspace_root": str(self.workspace_root),
                "template_dir": str(self.template_dir),
                "tool_root": str(TOOL_ROOT),
            }
        )

    def _build_prompt(self, job: Job) -> str:
        flow_module = self._flow_module(job.flow_name)
        prompt = flow_module.build_prompt(
            {
                "job": asdict(job),
                "workspace": job.workspace,
                "workspace_root": str(self.workspace_root),
                "template_dir": str(self.template_dir),
                "tool_root": str(TOOL_ROOT),
            }
        )
        return prompt.rstrip() + "\n"

    def _find_job(self, job_id: str) -> Job | None:
        for job in self.jobs:
            if job.id == job_id:
                return job
        return None

    def _load_flows(self) -> dict[str, FlowDefinition]:
        flows: dict[str, FlowDefinition] = {}
        for path in sorted(FLOWS_DIR.glob("*.py")):
            if path.name.startswith("_"):
                continue
            module = self._load_module_from_path(path)
            name = getattr(module, "FLOW_NAME", path.stem)
            description = getattr(module, "FLOW_DESCRIPTION", "")
            input_label = getattr(module, "FLOW_INPUT_LABEL", "Task Input")
            allow_batch_file = bool(getattr(module, "FLOW_ALLOW_BATCH_FILE", False))
            flows[name] = FlowDefinition(
                name=name,
                path=str(path),
                description=description,
                input_label=input_label,
                allow_batch_file=allow_batch_file,
            )
        return flows

    def _flow_module(self, flow_name: str) -> Any:
        flow = self.flows.get(flow_name)
        if flow is None:
            raise ValueError(f"unknown flow: {flow_name}")
        return self._load_module_from_path(Path(flow.path))

    @staticmethod
    def _load_module_from_path(path: Path) -> Any:
        spec = importlib.util.spec_from_file_location(path.stem, path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"cannot load flow module: {path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    @staticmethod
    def _repo_name_from_url(url: str) -> str:
        parsed = urlparse(url.strip())
        path = parsed.path.rstrip("/")
        if not path:
            raise ValueError(f"invalid repository url: {url}")
        name = path.rsplit("/", 1)[-1]
        if name.endswith(".git"):
            name = name[:-4]
        safe = "".join(ch if ch.isalnum() or ch in "-._" else "-" for ch in name)
        return safe.strip("-") or "repo"


def prompt_input(stdscr: Any, label: str) -> str:
    stdscr.timeout(-1)
    curses.echo()
    curses.curs_set(1)
    height, width = stdscr.getmaxyx()
    prompt = label[: max(width - 1, 1)]
    stdscr.move(height - 1, 0)
    stdscr.clrtoeol()
    stdscr.addstr(height - 1, 0, prompt)
    stdscr.move(height - 1, min(len(prompt), width - 1))
    stdscr.refresh()
    raw = stdscr.getstr(height - 1, min(len(prompt), width - 1), max(width - len(prompt) - 1, 1))
    curses.noecho()
    curses.curs_set(0)
    stdscr.move(height - 1, 0)
    stdscr.clrtoeol()
    stdscr.timeout(500)
    return raw.decode("utf-8").strip()


def slugify_flow_name(raw: str) -> str:
    chars = []
    previous_dash = False
    for ch in raw.strip().lower():
        if ch.isalnum():
            chars.append(ch)
            previous_dash = False
        elif ch in {" ", "-", "_", "."}:
            if not previous_dash:
                chars.append("-")
                previous_dash = True
    return "".join(chars).strip("-")


def build_flow_source(
    flow_name: str,
    description: str,
    worker_instructions: str,
    *,
    input_label: str = "Task Input",
    mode: str = "prompt-only",
) -> str:
    safe_description = description.replace('"', '\\"')
    safe_input_label = input_label.replace('"', '\\"')
    indented = "\n".join(f"        {line}" if line else "" for line in worker_instructions.strip().splitlines())
    if mode == "template-copy":
        return f'''from __future__ import annotations

import shutil
import textwrap
from pathlib import Path


FLOW_NAME = "{flow_name}"
FLOW_DESCRIPTION = "{safe_description}"
FLOW_INPUT_LABEL = "{safe_input_label}"


def prepare_workspace(context: dict) -> None:
    template_dir = Path(context["template_dir"])
    workspace = Path(context["workspace"])
    job = context["job"]

    if not template_dir.exists():
        raise FileNotFoundError(f"template not found: {{template_dir}}")

    if not workspace.exists():
        shutil.copytree(template_dir, workspace)

    task_path = workspace / "Task"
    if not task_path.exists():
        raise FileNotFoundError(f"Task not found: {{task_path}}")

    lines = task_path.read_text(encoding="utf-8").splitlines()
    if lines:
        lines[0] = job["url"]
    else:
        lines = [job["url"]]
    task_path.write_text("\\n".join(lines) + "\\n", encoding="utf-8")


def build_prompt(context: dict) -> str:
    workspace = Path(context["workspace"])
    task_path = workspace / "Task"
    agent_path = workspace / "Agent.md"
    task_text = task_path.read_text(encoding="utf-8")
    agent_text = (
        agent_path.read_text(encoding="utf-8")
        if agent_path.exists()
        else "Agent.md not found."
    )
    return textwrap.dedent(
        f"""
{indented}

        [Agent.md]
        {{agent_text}}

        [Task]
        {{task_text}}
        """
    ).strip()
'''

    return f'''from __future__ import annotations

import textwrap
from pathlib import Path


FLOW_NAME = "{flow_name}"
FLOW_DESCRIPTION = "{safe_description}"
FLOW_INPUT_LABEL = "{safe_input_label}"


def prepare_workspace(context: dict) -> None:
    workspace = Path(context["workspace"])
    job = context["job"]
    workspace.mkdir(parents=True, exist_ok=True)

    task_path = workspace / "Task"
    task_path.write_text(job["url"].rstrip() + "\\n", encoding="utf-8")

    agent_path = workspace / "Agent.md"
    agent_path.write_text("# Agent.md\\n\\nNo agent instructions provided.\\n", encoding="utf-8")


def build_prompt(context: dict) -> str:
    workspace = Path(context["workspace"])
    task_path = workspace / "Task"
    agent_path = workspace / "Agent.md"
    task_text = task_path.read_text(encoding="utf-8")
    agent_text = (
        agent_path.read_text(encoding="utf-8")
        if agent_path.exists()
        else "Agent.md not found."
    )
    return textwrap.dedent(
        f"""
{indented}

        [Agent.md]
        {{agent_text}}

        [Task]
        {{task_text}}
        """
    ).strip()
'''


def create_flow_file(
    flow_name: str,
    description: str,
    worker_instructions: str,
    *,
    input_label: str = "Task Input",
    mode: str = "prompt-only",
) -> Path:
    FLOWS_DIR.mkdir(parents=True, exist_ok=True)
    path = FLOWS_DIR / f"{flow_name}.py"
    if path.exists():
        raise FileExistsError(f"flow already exists: {path}")
    source = build_flow_source(
        flow_name,
        description,
        worker_instructions,
        input_label=input_label,
        mode=mode,
    )
    path.write_text(source, encoding="utf-8")
    return path


def parse_job_inputs(raw: str, flow: FlowDefinition | None) -> list[str]:
    value = raw.strip()
    if not value:
        return []
    if flow is not None and flow.allow_batch_file:
        path = Path(value).expanduser()
        if path.is_file():
            items: list[str] = []
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                entry = line.strip()
                if not entry or entry.startswith("#"):
                    continue
                items.append(entry)
            return items
    return [value]


def prompt_notice(stdscr: Any, message: str) -> None:
    stdscr.timeout(-1)
    curses.curs_set(0)
    height, width = stdscr.getmaxyx()
    line = message[: max(width - 1, 1)]
    stdscr.move(height - 1, 0)
    stdscr.clrtoeol()
    stdscr.addstr(height - 1, 0, line)
    stdscr.refresh()
    stdscr.getch()
    stdscr.move(height - 1, 0)
    stdscr.clrtoeol()
    stdscr.timeout(500)


def clamp(value: int, minimum: int, maximum: int) -> int:
    return max(minimum, min(value, maximum))


def tail_text(path: Path, max_lines: int) -> list[str]:
    if not path.exists():
        return ["<no log yet>"]
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return lines[-max_lines:] or ["<empty log>"]


def find_matches(lines: list[str], needle: str) -> list[int]:
    if not needle:
        return []
    lowered = needle.lower()
    return [idx for idx, line in enumerate(lines) if lowered in line.lower()]


def match_ranges(line: str, needle: str) -> list[tuple[int, int]]:
    if not needle:
        return []
    ranges: list[tuple[int, int]] = []
    lower_line = line.lower()
    lower_needle = needle.lower()
    start = 0
    while True:
        idx = lower_line.find(lower_needle, start)
        if idx == -1:
            return ranges
        ranges.append((idx, idx + len(needle)))
        start = idx + max(len(needle), 1)


def render_highlighted_line(
    stdscr: Any,
    y: int,
    width: int,
    line: str,
    search_term: str,
    horizontal_offset: int,
    current_line: bool = False,
    base_attr: int = curses.A_NORMAL,
) -> None:
    if width <= 0:
        return
    visible_start = horizontal_offset
    visible_end = horizontal_offset + width
    rendered = line[visible_start:visible_end]
    if not search_term:
        stdscr.addstr(y, 0, rendered[:width], base_attr)
        return

    ranges = match_ranges(line, search_term)
    if not ranges:
        stdscr.addstr(y, 0, rendered[:width], base_attr)
        return

    highlight_mask = [False] * len(rendered)
    for start, end in ranges:
        if end <= visible_start:
            continue
        if start >= visible_end:
            break
        local_start = max(start, visible_start) - visible_start
        local_end = min(end, visible_end) - visible_start
        for idx in range(local_start, local_end):
            if 0 <= idx < len(highlight_mask):
                highlight_mask[idx] = True

    chunk_start = 0
    matched_attr = base_attr | (curses.A_REVERSE | curses.A_BOLD if current_line else curses.A_BOLD)
    current_attr = matched_attr if highlight_mask and highlight_mask[0] else base_attr
    for idx in range(1, len(rendered) + 1):
        next_attr = None
        if idx < len(rendered):
            next_attr = matched_attr if highlight_mask[idx] else base_attr
        if idx == len(rendered) or next_attr != current_attr:
            chunk = rendered[chunk_start:idx]
            if chunk:
                stdscr.addstr(y, chunk_start, chunk, current_attr)
            chunk_start = idx
            if idx < len(rendered):
                current_attr = next_attr


def begin_search(stdscr: Any, initial: str = "") -> str:
    curses.curs_set(1)
    curses.noecho()
    stdscr.keypad(True)
    return initial


def handle_search_key(key: Any, current: str) -> tuple[str, bool]:
    if key == "\x1b" or key == 27:
        return current, True
    if key in ("\n", "\r", 10, 13):
        return current, True
    if key in (curses.KEY_BACKSPACE, "\b", "\x7f", 127, 8):
        return current[:-1], False
    if isinstance(key, str) and key.isprintable():
        return current + key, False
    if isinstance(key, int) and 32 <= key <= 126:
        return current + chr(key), False
    return current, False


def choose_flow(stdscr: Any, overseer: Overseer) -> str | None:
    entries = sorted(overseer.flows.items())
    if not entries:
        return None

    current_index = next(
        (idx for idx, (name, _) in enumerate(entries) if name == overseer.current_flow),
        0,
    )
    selected = current_index
    top = 0
    stdscr.keypad(True)

    while True:
        stdscr.erase()
        height, width = stdscr.getmaxyx()
        title = "Choose Flow  (enter select, j/k or arrows move, q/esc cancel)"
        stdscr.addstr(0, 0, title[: width - 1], curses.A_BOLD)

        visible_height = max(height - 3, 1)
        max_top = max(len(entries) - visible_height, 0)
        selected = clamp(selected, 0, len(entries) - 1)
        if selected < top:
            top = selected
        elif selected >= top + visible_height:
            top = selected - visible_height + 1
        top = clamp(top, 0, max_top)

        for row, (name, flow) in enumerate(entries[top : top + visible_height], start=1):
            absolute_index = top + row - 1
            marker = "*" if absolute_index == current_index else " "
            batch_marker = " file" if flow.allow_batch_file else ""
            line = f"{marker} {name:<20.20} {flow.input_label:<12.12}{batch_marker:<5.5} {flow.description or '-'}"
            attr = curses.A_REVERSE if absolute_index == selected else curses.A_NORMAL
            stdscr.addstr(row, 0, line[: width - 1], attr)

        footer = "Current flow marked with *"
        stdscr.addstr(height - 1, 0, footer[: width - 1])
        stdscr.refresh()

        key = stdscr.getch()
        if key in (ord("q"), 27):
            return None
        if key in (10, 13):
            return entries[selected][0]
        if key in (curses.KEY_UP, ord("k")):
            selected = max(selected - 1, 0)
        elif key in (curses.KEY_DOWN, ord("j")):
            selected = min(selected + 1, len(entries) - 1)
        elif key == curses.KEY_PPAGE:
            selected = max(selected - visible_height, 0)
        elif key == curses.KEY_NPAGE:
            selected = min(selected + visible_height, len(entries) - 1)


def choose_flow_mode(stdscr: Any) -> str | None:
    entries = list(FLOW_MODES.items())
    selected = 0
    stdscr.keypad(True)

    while True:
        stdscr.erase()
        height, width = stdscr.getmaxyx()
        stdscr.addstr(0, 0, "New Flow Type  (enter select, q/esc cancel)", curses.A_BOLD)
        for row, (mode, meta) in enumerate(entries, start=1):
            line = f"{mode:<14.14} {meta['input_label']:<12.12} {meta['description']}"
            attr = curses.A_REVERSE if row - 1 == selected else curses.A_NORMAL
            stdscr.addstr(row, 0, line[: width - 1], attr)
        footer = "prompt-only: generic task flow | template-copy: PhpCodeCheck-style template flow"
        stdscr.addstr(height - 1, 0, footer[: width - 1])
        stdscr.refresh()

        key = stdscr.getch()
        if key in (ord("q"), 27):
            return None
        if key in (10, 13):
            return entries[selected][0]
        if key in (curses.KEY_UP, ord("k")):
            selected = max(selected - 1, 0)
        elif key in (curses.KEY_DOWN, ord("j")):
            selected = min(selected + 1, len(entries) - 1)


def run_flow_wizard(stdscr: Any, overseer: Overseer) -> None:
    prompt_notice(
        stdscr,
        "New flow creates a scaffold only. Choose mode first, then adjust the generated file if needed. Press any key.",
    )
    mode = choose_flow_mode(stdscr)
    if not mode:
        return
    mode_meta = FLOW_MODES[mode]

    raw_name = prompt_input(stdscr, "Flow name: ")
    if not raw_name:
        return
    flow_name = slugify_flow_name(raw_name)
    if not flow_name:
        prompt_notice(stdscr, "Invalid flow name. Press any key.")
        return
    if flow_name in overseer.flows:
        prompt_notice(stdscr, f"Flow already exists: {flow_name}. Press any key.")
        return

    description = prompt_input(stdscr, "Description: ")
    if not description:
        description = f"Custom flow {flow_name} ({mode})"

    input_label = prompt_input(stdscr, f"Input label [{mode_meta['input_label']}]: ")
    if not input_label:
        input_label = mode_meta["input_label"]

    prompt_notice(
        stdscr,
        "Next, enter worker instructions. Use \\n for line breaks. Leave empty to use the mode default. Press any key.",
    )
    raw_instructions = prompt_input(stdscr, "Worker prompt: ")
    worker_instructions = raw_instructions.replace("\\n", "\n").strip()
    if not worker_instructions:
        worker_instructions = mode_meta["worker_instructions"]

    try:
        path = create_flow_file(
            flow_name,
            description,
            worker_instructions,
            input_label=input_label,
            mode=mode,
        )
        overseer.flows = overseer._load_flows()
        overseer.set_flow(flow_name)
    except Exception as exc:  # noqa: BLE001
        prompt_notice(stdscr, f"Create flow failed: {exc}. Press any key.")
        return

    prompt_notice(
        stdscr,
        f"Created flow: {flow_name} [{mode}] -> {path}. Review the generated file before relying on it. Press any key.",
    )


def draw(stdscr: Any, overseer: Overseer) -> None:
    stdscr.erase()
    height, width = stdscr.getmaxyx()
    with overseer.lock:
        jobs = list(overseer.jobs)
        selected = max(0, min(overseer.selected, max(len(jobs) - 1, 0)))
        overseer.selected = selected
        running = sum(1 for job in jobs if job.status == "running")
        queued = sum(1 for job in jobs if job.status == "queued")
    header = (
        f"CodeSlave | workers={overseer.max_workers} | "
        f"auto={'on' if overseer.auto_start else 'off'} | "
        f"running={running} | queued={queued} | flow={overseer.current_flow}"
    )
    stdscr.addstr(0, 0, header[: width - 1], curses.A_BOLD)
    help_line = "a:add  d:delete  f:flow  w:new-flow  +/-:workers  space:auto  r:retry  x:cancel  enter:logs  q:quit"
    stdscr.addstr(1, 0, help_line[: width - 1])

    list_top = 3
    list_height = max((height - list_top) // 2, 5)
    for idx, job in enumerate(jobs[: list_height]):
        marker = ">" if idx == selected else " "
        started = (
            time.strftime("%H:%M:%S", time.localtime(job.started_at))
            if job.started_at
            else "--:--:--"
        )
        line = (
            f"{marker} {job.status:<10} {job.flow_name:<14.14} {job.repo_name:<24.24} "
            f"{started}  {job.url}"
        )
        attr = curses.A_REVERSE if idx == selected else curses.A_NORMAL
        stdscr.addstr(list_top + idx, 0, line[: width - 1], attr)

    if jobs:
        job = jobs[selected]
        info_top = list_top + list_height + 1
        info = (
            f"id={job.id} pid={job.pid or '-'} exit={job.exit_code if job.exit_code is not None else '-'} "
            f"workspace={job.workspace}"
        )
        stdscr.addstr(info_top, 0, info[: width - 1], curses.A_BOLD)
        err_line = f"error={job.error or '-'}"
        stdscr.addstr(info_top + 1, 0, err_line[: width - 1])
        flow = overseer.flows.get(job.flow_name)
        flow_line = f"flow={job.flow_name} script={flow.path if flow else '-'}"
        stdscr.addstr(info_top + 2, 0, flow_line[: width - 1])
        log_lines = tail_text(Path(job.log_path), max(height - info_top - 5, 3))
        for idx, line in enumerate(log_lines):
            stdscr.addstr(info_top + 4 + idx, 0, line[: width - 1])
    else:
        flow = overseer.flows.get(overseer.current_flow)
        input_label = flow.input_label if flow else "Task Input"
        stdscr.addstr(list_top, 0, f"No jobs yet. Press 'a' to add {input_label}.")

    stdscr.refresh()


def tui(stdscr: Any, overseer: Overseer) -> None:
    curses.curs_set(0)
    stdscr.timeout(500)
    while True:
        draw(stdscr, overseer)
        key = stdscr.getch()
        if key == -1:
            continue
        if key in (ord("q"), 27):
            return
        if key == ord("a"):
            flow = overseer.flows.get(overseer.current_flow)
            input_label = flow.input_label if flow else "Task Input"
            suffix = " or file path" if flow and flow.allow_batch_file else ""
            raw_value = prompt_input(stdscr, f"{input_label}{suffix}: ")
            items = parse_job_inputs(raw_value, flow)
            if not items:
                continue
            try:
                for item in items:
                    overseer.add_job(item)
            except ValueError as exc:
                prompt_notice(stdscr, f"Add job failed: {exc}. Press any key.")
                continue
            if len(items) > 1:
                prompt_notice(stdscr, f"Imported {len(items)} jobs. Press any key.")
        elif key == ord("f"):
            flow_name = choose_flow(stdscr, overseer)
            if flow_name:
                overseer.set_flow(flow_name)
        elif key == ord("w"):
            run_flow_wizard(stdscr, overseer)
        elif key in (ord("+"), ord("=")):
            overseer.max_workers += 1
            overseer._save_state()
        elif key == ord("-"):
            overseer.max_workers = max(overseer.max_workers - 1, 1)
            overseer._save_state()
        elif key == ord(" "):
            overseer.auto_start = not overseer.auto_start
            overseer._save_state()
        elif key == ord("j"):
            overseer.selected = min(overseer.selected + 1, max(len(overseer.jobs) - 1, 0))
        elif key == ord("k"):
            overseer.selected = max(overseer.selected - 1, 0)
        elif key == curses.KEY_DOWN:
            overseer.selected = min(overseer.selected + 1, max(len(overseer.jobs) - 1, 0))
        elif key == curses.KEY_UP:
            overseer.selected = max(overseer.selected - 1, 0)
        elif key == ord("r") and overseer.jobs:
            overseer.retry_job(overseer.jobs[overseer.selected])
        elif key == ord("d") and overseer.jobs:
            overseer.delete_job(overseer.jobs[overseer.selected])
        elif key == ord("x") and overseer.jobs:
            job = overseer.jobs[overseer.selected]
            job.status = "cancelled"
            overseer.cancel_job(job)
            overseer._save_state()
        elif key in (10, 13) and overseer.jobs:
            view_logs(stdscr, overseer.jobs[overseer.selected])


def view_logs(stdscr: Any, job: Job) -> None:
    stdscr.timeout(-1)
    stdscr.keypad(True)
    vertical_offset = 0
    horizontal_offset = 0
    search_term = ""
    match_indices: list[int] = []
    match_cursor = -1
    search_mode = False
    while True:
        stdscr.erase()
        height, width = stdscr.getmaxyx()
        lines = tail_text(Path(job.log_path), 500)
        body_height = max(height - 3, 1)
        max_vertical_offset = max(len(lines) - body_height, 0)
        longest_line = max((len(line) for line in lines), default=0)
        max_horizontal_offset = max(longest_line - max(width - 1, 1), 0)
        vertical_offset = clamp(vertical_offset, 0, max_vertical_offset)
        horizontal_offset = clamp(horizontal_offset, 0, max_horizontal_offset)
        start = max(len(lines) - body_height - vertical_offset, 0)
        end = len(lines) - vertical_offset or None
        visible = lines[start:end]
        title = (
            f"Logs: {job.repo_name}  "
            "(up/down scroll, left/right pan, / search, n next, p previous, q back)"
        )
        stdscr.addstr(0, 0, title[: width - 1], curses.A_BOLD)
        for idx, line in enumerate(visible[:body_height]):
            line_number = start + idx
            render_highlighted_line(
                stdscr,
                1 + idx,
                max(width - 1, 1),
                line,
                search_term,
                horizontal_offset,
                current_line=(
                    bool(match_indices)
                    and 0 <= match_cursor < len(match_indices)
                    and line_number == match_indices[match_cursor]
                ),
                base_attr=curses.A_NORMAL,
            )
        status = (
            f"Search: {search_term or '-'} | "
            f"H:{horizontal_offset} V:{vertical_offset} | "
            f"Matches: {len(match_indices)}"
        )
        if match_indices and 0 <= match_cursor < len(match_indices):
            status += f" | Current: {match_cursor + 1}/{len(match_indices)} @ line {match_indices[match_cursor] + 1}"
        stdscr.addstr(height - 2, 0, status[: width - 1])
        prompt = "Search: " + search_term if search_mode or search_term else "Search: "
        stdscr.addstr(height - 1, 0, prompt[: width - 1])
        if search_mode:
            stdscr.move(height - 1, min(len(prompt), width - 1))
        stdscr.refresh()
        key = stdscr.get_wch() if search_mode else stdscr.getch()
        if search_mode:
            search_term, done = handle_search_key(key, search_term)
            match_indices = find_matches(lines, search_term)
            if match_indices:
                if match_cursor < 0:
                    match_cursor = 0
                else:
                    match_cursor = clamp(match_cursor, 0, len(match_indices) - 1)
                target_line = match_indices[match_cursor]
                vertical_offset = max(len(lines) - body_height - target_line, 0)
            else:
                match_cursor = -1
            if done:
                search_mode = False
                curses.curs_set(0)
            continue
        if key == ord("q"):
            stdscr.timeout(500)
            curses.curs_set(0)
            return
        if key in (curses.KEY_UP, ord("k")):
            vertical_offset = min(vertical_offset + 1, max_vertical_offset)
        elif key in (curses.KEY_DOWN, ord("j")):
            vertical_offset = max(vertical_offset - 1, 0)
        elif key in (curses.KEY_LEFT, ord("h"), curses.KEY_SLEFT):
            horizontal_offset = max(horizontal_offset - 4, 0)
        elif key in (curses.KEY_RIGHT, ord("l"), curses.KEY_SRIGHT):
            horizontal_offset = min(horizontal_offset + 4, max_horizontal_offset)
        elif key == curses.KEY_PPAGE:
            vertical_offset = min(vertical_offset + body_height, max_vertical_offset)
        elif key == curses.KEY_NPAGE:
            vertical_offset = max(vertical_offset - body_height, 0)
        elif key == ord("/"):
            search_mode = True
            search_term = begin_search(stdscr, search_term)
            match_indices = find_matches(lines, search_term)
            if match_indices:
                match_cursor = 0
                target_line = match_indices[match_cursor]
                vertical_offset = max(len(lines) - body_height - target_line, 0)
            else:
                match_cursor = -1
        elif key == ord("n") and match_indices:
            match_cursor = (match_cursor + 1) % len(match_indices)
            target_line = match_indices[match_cursor]
            vertical_offset = max(len(lines) - body_height - target_line, 0)
        elif key == ord("p") and match_indices:
            match_cursor = (match_cursor - 1) % len(match_indices)
            target_line = match_indices[match_cursor]
            vertical_offset = max(len(lines) - body_height - target_line, 0)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Manage parallel Codex workers from a local template.")
    parser.add_argument(
        "--template-dir",
        default=str(DEFAULT_TEMPLATE_DIR),
        help="Path to the project template directory.",
    )
    parser.add_argument(
        "--workspace-root",
        default=str(WORKSPACE_ROOT),
        help="Root directory where per-repository workspaces are created.",
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("tui", help="Open the curses TUI.")

    enqueue = sub.add_parser("enqueue", help="Queue one or more repository URLs.")
    enqueue.add_argument("--flow", help="Flow name to use for the queued jobs.")
    enqueue.add_argument("urls", nargs="+")

    prepare = sub.add_parser("prepare", help="Prepare workspaces without starting Codex.")
    prepare.add_argument("--flow", help="Flow name to use while preparing workspaces.")
    prepare.add_argument("urls", nargs="+")

    run = sub.add_parser("run", help="Queue URLs and keep scheduling until all complete.")
    run.add_argument("--flow", help="Flow name to use for this run.")
    run.add_argument("urls", nargs="+")
    run.add_argument("--max-workers", type=int, default=2)

    status = sub.add_parser("status", help="Print queued jobs as JSON.")
    status.add_argument("--json", action="store_true")

    sub.add_parser("flows", help="List available flow scripts.")
    new_flow = sub.add_parser("new-flow", help="Create a new flow scaffold.")
    new_flow.add_argument("--mode", choices=sorted(FLOW_MODES), default="prompt-only")
    new_flow.add_argument("--input-label")
    new_flow.add_argument("name")
    new_flow.add_argument("description")
    new_flow.add_argument("worker_instructions")

    return parser.parse_args()


def wait_until_idle(overseer: Overseer) -> None:
    while True:
        with overseer.lock:
            active = any(job.status in {"queued", "preparing", "running"} for job in overseer.jobs)
        if not active:
            return
        time.sleep(1.0)


def main() -> int:
    args = parse_args()
    start_scheduler = args.command in (None, "tui", "run")
    overseer = Overseer(
        template_dir=Path(args.template_dir),
        workspace_root=Path(args.workspace_root),
        start_scheduler=start_scheduler,
    )
    try:
        if args.command in (None, "tui"):
            curses.wrapper(lambda stdscr: tui(stdscr, overseer))
            return 0
        if args.command == "enqueue":
            if args.flow:
                try:
                    overseer.set_flow(args.flow)
                except ValueError as exc:
                    print(str(exc))
                    return 1
            for url in args.urls:
                job = overseer.add_job(url)
                print(f"queued {job.repo_name} ({job.id})")
            return 0
        if args.command == "prepare":
            if args.flow:
                try:
                    overseer.set_flow(args.flow)
                except ValueError as exc:
                    print(str(exc))
                    return 1
            for url in args.urls:
                job = overseer.add_job(url)
                overseer._prepare_workspace(job)
                print(f"prepared {job.repo_name} -> {job.workspace}")
            return 0
        if args.command == "run":
            if args.flow:
                try:
                    overseer.set_flow(args.flow)
                except ValueError as exc:
                    print(str(exc))
                    return 1
            overseer.max_workers = max(args.max_workers, 1)
            overseer.auto_start = True
            for url in args.urls:
                overseer.add_job(url)
            overseer._save_state()
            wait_until_idle(overseer)
            failed = [job for job in overseer.jobs if job.status == "failed"]
            return 1 if failed else 0
        if args.command == "status":
            payload = [asdict(job) for job in overseer.jobs]
            if args.json:
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            else:
                for job in overseer.jobs:
                    print(f"{job.status:<10} {job.repo_name:<24} {job.url}")
            return 0
        if args.command == "flows":
            for name, flow in sorted(overseer.flows.items()):
                print(f"{name}\t{flow.path}\t{flow.description}")
            return 0
        if args.command == "new-flow":
            flow_name = slugify_flow_name(args.name)
            input_label = args.input_label or FLOW_MODES[args.mode]["input_label"]
            worker_instructions = args.worker_instructions or FLOW_MODES[args.mode]["worker_instructions"]
            path = create_flow_file(
                flow_name,
                args.description,
                worker_instructions,
                input_label=input_label,
                mode=args.mode,
            )
            overseer.flows = overseer._load_flows()
            overseer.set_flow(flow_name)
            print(path)
            return 0
        return 0
    finally:
        overseer.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
