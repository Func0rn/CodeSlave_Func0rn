#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import curses
import html
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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
from urllib.parse import parse_qs, urlparse


TOOL_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = TOOL_ROOT / "workspaces"
DEFAULT_TEMPLATE_DIR = TOOL_ROOT / "templates" / "PhpCodeCheck"
FLOWS_DIR = TOOL_ROOT / "flows"
RUNTIME_DIR = TOOL_ROOT / "runtime"
LOG_DIR = RUNTIME_DIR / "logs"
STATE_PATH = RUNTIME_DIR / "state.json"
SHOWCASE_IMPORTS_PATH = RUNTIME_DIR / "showcase_imports.json"
DEFAULT_FLOW = "php_code_check"
BUILTIN_SKILLS_DIR = TOOL_ROOT / "skills"
BUILTIN_SCA_CLEANER_SCRIPT = (
    BUILTIN_SKILLS_DIR / "sca-report-cleaner" / "scripts" / "normalize_sca_report.py"
)
USER_SCA_CLEANER_SCRIPT = Path("/home/kali/.codex/skills/sca-report-cleaner/scripts/normalize_sca_report.py")
SCA_CLEANER_SCRIPT = (
    BUILTIN_SCA_CLEANER_SCRIPT
    if BUILTIN_SCA_CLEANER_SCRIPT.exists()
    else USER_SCA_CLEANER_SCRIPT
)
STATUS_ALIASES = {
    "success": "succeeded",
    "succeeded": "succeeded",
    "queued": "queued",
    "running": "running",
    "failed": "failed",
    "cancelled": "cancelled",
    "paused": "paused",
}
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
            job = Job(**item)
            if job.status in {"running", "preparing", "paused"} and not self._pid_is_alive(job.pid):
                job.status = "failed"
                job.ended_at = time.time()
                job.pid = None
                job.exit_code = None
                job.error = job.error or "worker was not alive when CodeSlave started"
            jobs.append(job)
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
        repo_name = self._repo_name_from_input(self.current_flow, url)
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
        if job.status in {"running", "paused"}:
            self.cancel_job(job)
            time.sleep(0.2)
        with self.lock:
            self.procs.pop(job.id, None)
            self.jobs = [item for item in self.jobs if item.id != job.id]
            self.selected = min(self.selected, max(len(self.jobs) - 1, 0))
        self._save_state()

    def cancel_job(self, job: Job) -> None:
        proc = self.procs.get(job.id)
        pid = proc.pid if proc else job.pid
        if not pid:
            return
        try:
            os.killpg(pid, signal.SIGTERM)
            if job.status == "paused":
                os.killpg(pid, signal.SIGCONT)
        except ProcessLookupError:
            pass

    def pause_job(self, job: Job) -> bool:
        proc = self.procs.get(job.id)
        pid = proc.pid if proc else job.pid
        if not pid or job.status != "running":
            return False
        try:
            os.killpg(pid, signal.SIGSTOP)
        except ProcessLookupError:
            return False
        with self.lock:
            job.status = "paused"
        self._save_state()
        return True

    def resume_job(self, job: Job) -> bool:
        proc = self.procs.get(job.id)
        pid = proc.pid if proc else job.pid
        if not pid or job.status != "paused":
            return False
        try:
            os.killpg(pid, signal.SIGCONT)
        except ProcessLookupError:
            return False
        with self.lock:
            job.status = "running"
        self._save_state()
        return True

    def bulk_delete_status(self, status: str) -> int:
        target_status = STATUS_ALIASES.get(status, status)
        with self.lock:
            targets = [job for job in self.jobs if job.status == target_status]
        for job in targets:
            self.delete_job(job)
        return len(targets)

    def bulk_pause_running(self) -> int:
        self.auto_start = False
        with self.lock:
            targets = [job for job in self.jobs if job.status == "running"]
        count = sum(1 for job in targets if self.pause_job(job))
        self._save_state()
        return count

    def bulk_resume_paused(self) -> int:
        with self.lock:
            targets = [job for job in self.jobs if job.status == "paused"]
        count = sum(1 for job in targets if self.resume_job(job))
        self._save_state()
        return count

    def job_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        with self.lock:
            for job in self.jobs:
                counts[job.status] = counts.get(job.status, 0) + 1
            counts["total"] = len(self.jobs)
        return counts

    @staticmethod
    def _pid_is_alive(pid: int | None) -> bool:
        if not pid:
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

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
                elif job.status == "paused":
                    job.status = "cancelled"
                    if not job.error:
                        job.error = "paused worker exited"
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
            running = sum(1 for job in self.jobs if job.status in {"preparing", "running", "paused"})
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

    def _repo_name_from_input(self, flow_name: str, raw_input: str) -> str:
        flow_module = self._flow_module(flow_name)
        if hasattr(flow_module, "derive_repo_name"):
            name = str(flow_module.derive_repo_name(raw_input)).strip()
            if not name:
                raise ValueError(f"invalid job input: {raw_input}")
            return self._sanitize_repo_name(name)
        return self._repo_name_from_url(raw_input)

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
        return Overseer._sanitize_repo_name(name)

    @staticmethod
    def _sanitize_repo_name(name: str) -> str:
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
    if flow is not None:
        module = Overseer._load_module_from_path(Path(flow.path))
        if hasattr(module, "expand_inputs"):
            items = [str(item).strip() for item in module.expand_inputs(value)]
            return [item for item in items if item]
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


def read_text_if_exists(path: Path | None, max_chars: int | None = None) -> str:
    if path is None or not path.exists():
        return ""
    text = path.read_text(encoding="utf-8", errors="replace")
    if max_chars is not None and len(text) > max_chars:
        return text[-max_chars:]
    return text


def load_json_file(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8", errors="replace"))


def stable_id(*parts: str) -> str:
    digest = hashlib.sha256("\n".join(parts).encode("utf-8", errors="replace")).hexdigest()
    return digest[:16]


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
        counts: dict[str, int] = {}
        for job in jobs:
            counts[job.status] = counts.get(job.status, 0) + 1
    header = (
        f"CodeSlave | workers={overseer.max_workers} | "
        f"auto={'on' if overseer.auto_start else 'off'} | "
        f"run={counts.get('running', 0)} | pause={counts.get('paused', 0)} | "
        f"queue={counts.get('queued', 0)} | ok={counts.get('succeeded', 0)} | "
        f"fail={counts.get('failed', 0)} | flow={overseer.current_flow}"
    )
    stdscr.addstr(0, 0, header[: width - 1], curses.A_BOLD)
    help_line = (
        "a:add d:del f:flow w:new +/-:workers space:auto r:retry x:cancel "
        "^U:del-ok ^O:del-queued ^P:pause-run ^G:resume enter:logs q:quit"
    )
    stdscr.addstr(1, 0, help_line[: width - 1])

    list_top = 3
    list_height = max((height - list_top) // 2, 5)
    max_top = max(len(jobs) - list_height, 0)
    top = clamp(selected - list_height // 2, 0, max_top)
    visible_jobs = jobs[top : top + list_height]
    column_header = "   status     flow           target                    start     input"
    stdscr.addstr(list_top - 1, 0, column_header[: width - 1], curses.A_DIM)
    for row, job in enumerate(visible_jobs):
        idx = top + row
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
        stdscr.addstr(list_top + row, 0, line[: width - 1], attr)

    if jobs:
        job = jobs[selected]
        info_top = list_top + list_height + 1
        page_line = (
            f"Showing {top + 1}-{top + len(visible_jobs)} of {len(jobs)} | "
            "j/k or arrows move | PageUp/PageDown jump | Home/End first/last"
        )
        stdscr.addstr(info_top - 1, 0, page_line[: width - 1], curses.A_DIM)
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
        elif key == curses.KEY_NPAGE:
            height, _ = stdscr.getmaxyx()
            step = max((height - 3) // 2, 5)
            overseer.selected = min(overseer.selected + step, max(len(overseer.jobs) - 1, 0))
        elif key == curses.KEY_PPAGE:
            height, _ = stdscr.getmaxyx()
            step = max((height - 3) // 2, 5)
            overseer.selected = max(overseer.selected - step, 0)
        elif key == curses.KEY_HOME:
            overseer.selected = 0
        elif key == curses.KEY_END:
            overseer.selected = max(len(overseer.jobs) - 1, 0)
        elif key == ord("r") and overseer.jobs:
            overseer.retry_job(overseer.jobs[overseer.selected])
        elif key == ord("d") and overseer.jobs:
            overseer.delete_job(overseer.jobs[overseer.selected])
        elif key == ord("x") and overseer.jobs:
            job = overseer.jobs[overseer.selected]
            job.status = "cancelled"
            overseer.cancel_job(job)
            overseer._save_state()
        elif key == 21:
            count = overseer.bulk_delete_status("succeeded")
            prompt_notice(stdscr, f"Deleted {count} succeeded jobs. Press any key.")
        elif key == 15:
            count = overseer.bulk_delete_status("queued")
            prompt_notice(stdscr, f"Deleted {count} queued jobs. Press any key.")
        elif key == 16:
            count = overseer.bulk_pause_running()
            prompt_notice(stdscr, f"Paused {count} running jobs and disabled auto scheduling. Press any key.")
        elif key == 7:
            count = overseer.bulk_resume_paused()
            prompt_notice(stdscr, f"Resumed {count} paused jobs. Press any key.")
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


WEB_INDEX = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>CodeSlave</title>
<style>
:root {
  color-scheme: dark;
  --bg: #101418;
  --panel: #171d22;
  --panel-2: #20272d;
  --text: #e7edf2;
  --muted: #98a5af;
  --line: #303a42;
  --accent: #4aa3ff;
  --good: #45c46b;
  --warn: #e5bd4d;
  --bad: #ef6464;
  --pause: #b58cff;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  background: var(--bg);
  color: var(--text);
  font: 14px/1.45 system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}
button, input, select {
  font: inherit;
}
button {
  border: 1px solid var(--line);
  background: var(--panel-2);
  color: var(--text);
  border-radius: 6px;
  padding: 7px 10px;
  cursor: pointer;
}
button:hover { border-color: var(--accent); }
button.danger:hover { border-color: var(--bad); color: #ffdada; }
input, select {
  border: 1px solid var(--line);
  background: #0f1317;
  color: var(--text);
  border-radius: 6px;
  padding: 7px 9px;
  min-width: 0;
}
.shell {
  min-height: 100vh;
  display: grid;
  grid-template-rows: auto auto auto minmax(0, 1fr) auto;
}
.topbar {
  display: grid;
  grid-template-columns: 1fr auto;
  gap: 16px;
  align-items: center;
  padding: 14px 18px;
  border-bottom: 1px solid var(--line);
  background: #12171c;
}
.brand {
  display: flex;
  gap: 12px;
  align-items: baseline;
  min-width: 0;
}
.brand h1 {
  margin: 0;
  font-size: 20px;
  letter-spacing: 0;
}
.brand span { color: var(--muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.metrics {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}
.metric {
  display: inline-flex;
  gap: 6px;
  align-items: baseline;
  border: 1px solid var(--line);
  background: var(--panel);
  border-radius: 6px;
  padding: 5px 8px;
  min-width: 82px;
  justify-content: space-between;
}
.metric strong { font-size: 16px; }
.toolbar {
  display: grid;
  grid-template-columns: minmax(220px, 1fr) auto auto auto;
  gap: 10px;
  align-items: center;
  padding: 12px 18px;
  border-bottom: 1px solid var(--line);
  background: var(--panel);
}
.tabs {
  display: flex;
  gap: 8px;
  padding: 10px 18px 0;
  background: var(--panel);
}
.tab {
  min-width: 112px;
}
.tab.active {
  border-color: var(--accent);
  color: #d8ecff;
}
.hidden { display: none !important; }
.showcase-toolbar {
  display: grid;
  grid-template-columns: minmax(150px, 0.8fr) minmax(220px, 1.2fr) minmax(210px, 1fr) minmax(360px, 1.6fr);
  gap: 10px;
  align-items: center;
  padding: 12px 18px;
  border-bottom: 1px solid var(--line);
  background: var(--panel);
}
.showcase-actions {
  display: grid;
  grid-template-columns: minmax(170px, 1fr) auto auto auto;
  gap: 8px;
  align-items: center;
  min-width: 0;
}
.bulk, .pager, .settings {
  display: flex;
  gap: 8px;
  align-items: center;
  flex-wrap: wrap;
}
.content {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(320px, 38vw);
  min-height: 0;
}
.showcase-content {
  grid-template-columns: minmax(520px, 46vw) minmax(460px, 1fr);
}
.table-wrap {
  overflow: auto;
  border-right: 1px solid var(--line);
}
table {
  width: 100%;
  border-collapse: collapse;
  table-layout: fixed;
}
th, td {
  padding: 9px 10px;
  border-bottom: 1px solid var(--line);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  text-align: left;
}
th {
  position: sticky;
  top: 0;
  background: #141a1f;
  z-index: 1;
  color: var(--muted);
  font-weight: 600;
}
tr { cursor: pointer; }
tr:hover, tr.selected { background: #1f2a32; }
.status {
  display: inline-block;
  border-radius: 999px;
  padding: 2px 7px;
  border: 1px solid var(--line);
  min-width: 54px;
  max-width: 100%;
  text-align: center;
  overflow: hidden;
  text-overflow: ellipsis;
  vertical-align: middle;
}
.succeeded { color: var(--good); }
.queued { color: var(--warn); }
.running { color: var(--accent); }
.failed, .cancelled { color: var(--bad); }
.paused { color: var(--pause); }
.side {
  min-width: 0;
  padding: 14px;
  overflow: auto;
  background: #0f1317;
}
.showcase-side {
  padding: 16px;
}
.detail h2 {
  margin: 0 0 8px;
  font-size: 17px;
  line-height: 1.25;
  overflow-wrap: anywhere;
}
.detail-grid {
  display: grid;
  grid-template-columns: 90px minmax(0, 1fr);
  gap: 6px 10px;
  color: var(--muted);
  margin-bottom: 12px;
}
.detail-grid div:nth-child(even) {
  color: var(--text);
  overflow-wrap: anywhere;
}
.actions {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 12px;
}
pre {
  margin: 0;
  padding: 12px;
  border: 1px solid var(--line);
  background: #07090b;
  border-radius: 6px;
  overflow: auto;
  min-height: 240px;
  max-height: 52vh;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
.showcase-side pre {
  max-height: calc(100vh - 330px);
  min-height: 360px;
  white-space: pre-wrap;
}
.markdown-body {
  max-height: calc(100vh - 330px);
  min-height: 360px;
  overflow: auto;
  padding: 18px 20px;
  border: 1px solid var(--line);
  background: #07090b;
  border-radius: 6px;
  color: var(--text);
  line-height: 1.62;
  overflow-wrap: anywhere;
}
.markdown-body h1,
.markdown-body h2,
.markdown-body h3,
.markdown-body h4 {
  margin: 18px 0 8px;
  line-height: 1.25;
}
.markdown-body h1 { font-size: 22px; border-bottom: 1px solid var(--line); padding-bottom: 8px; }
.markdown-body h2 { font-size: 18px; border-bottom: 1px solid #24303a; padding-bottom: 6px; }
.markdown-body h3 { font-size: 16px; }
.markdown-body h4 { font-size: 14px; color: var(--muted); }
.markdown-body p {
  margin: 8px 0;
}
.markdown-body ul,
.markdown-body ol {
  margin: 8px 0 12px 22px;
  padding: 0;
}
.markdown-body li {
  margin: 4px 0;
}
.markdown-body code {
  background: #161c22;
  border: 1px solid #26313a;
  border-radius: 4px;
  padding: 1px 5px;
  font: 13px/1.45 ui-monospace, SFMono-Regular, Consolas, monospace;
}
.markdown-body pre {
  min-height: 0;
  max-height: none;
  margin: 10px 0;
  white-space: pre;
  overflow: auto;
}
.markdown-body pre code {
  display: block;
  border: 0;
  background: transparent;
  padding: 0;
}
.markdown-body blockquote {
  margin: 10px 0;
  padding: 2px 12px;
  border-left: 3px solid var(--accent);
  color: #c7d2dc;
  background: #10161b;
}
.markdown-body a {
  color: #8bc2ff;
  text-decoration: none;
}
.markdown-body a:hover {
  text-decoration: underline;
}
.markdown-body table {
  table-layout: auto;
  margin: 12px 0;
  border: 1px solid var(--line);
}
.markdown-body th,
.markdown-body td {
  white-space: normal;
  vertical-align: top;
}
textarea {
  width: 100%;
  min-height: 130px;
  resize: vertical;
  border: 1px solid var(--line);
  background: #07090b;
  color: var(--text);
  border-radius: 6px;
  padding: 10px;
  font: 13px/1.4 ui-monospace, SFMono-Regular, Consolas, monospace;
}
.footer {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  padding: 9px 18px;
  border-top: 1px solid var(--line);
  color: var(--muted);
  background: #12171c;
}
.kbd {
  border: 1px solid var(--line);
  border-bottom-color: #111;
  border-radius: 4px;
  padding: 1px 5px;
  color: var(--text);
  background: var(--panel-2);
}
@media (max-width: 980px) {
  .topbar, .toolbar, .showcase-toolbar, .content { grid-template-columns: 1fr; }
  .table-wrap { border-right: 0; }
  .side { border-top: 1px solid var(--line); }
  .showcase-actions { grid-template-columns: 1fr auto auto auto; }
}
@media (max-width: 640px) {
  .showcase-actions { grid-template-columns: 1fr 1fr; }
  .footer { flex-direction: column; }
}
</style>
</head>
<body>
<div class="shell">
  <header class="topbar">
    <div class="brand"><h1>CodeSlave</h1><span id="subtitle">loading...</span></div>
    <div class="metrics" id="metrics"></div>
  </header>
  <nav class="tabs">
    <button class="tab active" id="jobsTab">Jobs</button>
    <button class="tab" id="showcaseTab">Showcase</button>
  </nav>
  <section class="toolbar" id="jobsToolbar">
    <input id="search" placeholder="Search target, URL, id, error">
    <div class="settings">
      <select id="statusFilter">
        <option value="">all status</option>
        <option>queued</option><option>running</option><option>paused</option>
        <option>succeeded</option><option>failed</option><option>cancelled</option>
      </select>
      <select id="flowSelect"></select>
      <input id="workers" type="number" min="1" max="128" title="workers">
      <button id="autoBtn">auto</button>
    </div>
    <div class="bulk">
      <button class="danger" data-bulk="delete-succeeded">Del OK</button>
      <button class="danger" data-bulk="delete-queued">Del Queue</button>
      <button data-bulk="pause-running">Pause Run</button>
      <button data-bulk="resume-paused">Resume</button>
    </div>
    <div class="pager">
      <button id="prev">Prev</button>
      <select id="pageSize"><option>25</option><option selected>50</option><option>100</option><option>200</option></select>
      <button id="next">Next</button>
    </div>
  </section>
  <section class="showcase-toolbar hidden" id="showcaseToolbar">
    <select id="showFlow"><option value="">all flows</option></select>
    <input id="showFolder" placeholder="Match result folder">
    <select id="showClass">
      <option value="">all classifications</option>
      <option>L0_NOT_AFFECTED</option>
      <option>L1_PRESENT_UNREACHABLE</option>
      <option>L2_POTENTIALLY_REACHABLE</option>
      <option>L3_REACHABLE</option>
      <option>L4_EXPLOITABLE_CONFIRMED</option>
    </select>
    <div class="showcase-actions">
      <input id="showSearch" placeholder="Search result metadata">
      <button id="showExport">Export</button>
      <button id="showPersist">Persist</button>
      <button id="showImport">Import</button>
      <input id="showImportFile" type="file" accept="application/json,.json" class="hidden">
    </div>
  </section>
  <main class="content" id="jobsView">
    <div class="table-wrap">
      <table>
        <thead>
          <tr><th style="width:96px">Status</th><th style="width:120px">Flow</th><th style="width:260px">Target</th><th style="width:90px">Start</th><th>Input</th></tr>
        </thead>
        <tbody id="jobs"></tbody>
      </table>
    </div>
    <aside class="side">
      <div class="detail" id="detail"></div>
    </aside>
  </main>
  <main class="content showcase-content hidden" id="showcaseView">
    <div class="table-wrap">
      <table>
        <thead>
          <tr><th style="width:74px">Class</th><th style="width:118px">Flow</th><th style="width:300px">Folder</th><th style="width:145px">Vuln</th><th>Summary</th></tr>
        </thead>
        <tbody id="showRows"></tbody>
      </table>
    </div>
    <aside class="side showcase-side">
      <div class="detail" id="showDetail"></div>
    </aside>
  </main>
  <footer class="footer">
    <span id="pageInfo"></span>
    <span><span class="kbd">Ctrl+U</span> delete OK <span class="kbd">Ctrl+O</span> delete queued <span class="kbd">Ctrl+P</span> pause running <span class="kbd">Ctrl+G</span> resume</span>
  </footer>
</div>
<script>
const state = { view: "jobs", offset: 0, limit: 50, total: 0, selectedId: null, rows: [], last: null };
const show = { offset: 0, limit: 50, total: 0, selectedId: null, rows: [], last: null };
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function inlineMarkdown(text) {
  let out = esc(text);
  out = out.replace(/`([^`]+)`/g, "<code>$1</code>");
  out = out.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  out = out.replace(/\*([^*]+)\*/g, "<em>$1</em>");
  out = out.replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noreferrer">$1</a>');
  out = out.replace(/(https?:\/\/[^\s<]+)/g, '<a href="$1" target="_blank" rel="noreferrer">$1</a>');
  return out;
}
function renderMarkdown(markdown) {
  const lines = String(markdown || "").replace(/\r\n/g, "\n").split("\n");
  const htmlParts = [];
  let paragraph = [];
  let listType = "";
  let inCode = false;
  let codeLines = [];
  const flushParagraph = () => {
    if (!paragraph.length) return;
    htmlParts.push(`<p>${inlineMarkdown(paragraph.join(" "))}</p>`);
    paragraph = [];
  };
  const closeList = () => {
    if (!listType) return;
    htmlParts.push(`</${listType}>`);
    listType = "";
  };
  const flushBlocks = () => {
    flushParagraph();
    closeList();
  };
  for (const rawLine of lines) {
    const line = rawLine.replace(/\s+$/g, "");
    if (line.startsWith("```")) {
      if (inCode) {
        htmlParts.push(`<pre><code>${esc(codeLines.join("\n"))}</code></pre>`);
        codeLines = [];
        inCode = false;
      } else {
        flushBlocks();
        inCode = true;
      }
      continue;
    }
    if (inCode) {
      codeLines.push(rawLine);
      continue;
    }
    if (!line.trim()) {
      flushBlocks();
      continue;
    }
    const heading = line.match(/^(#{1,4})\s+(.+)$/);
    if (heading) {
      flushBlocks();
      const level = heading[1].length;
      htmlParts.push(`<h${level}>${inlineMarkdown(heading[2])}</h${level}>`);
      continue;
    }
    const quote = line.match(/^>\s?(.*)$/);
    if (quote) {
      flushBlocks();
      htmlParts.push(`<blockquote>${inlineMarkdown(quote[1])}</blockquote>`);
      continue;
    }
    const unordered = line.match(/^\s*[-*]\s+(.+)$/);
    const ordered = line.match(/^\s*\d+\.\s+(.+)$/);
    if (unordered || ordered) {
      flushParagraph();
      const target = unordered ? "ul" : "ol";
      if (listType && listType !== target) closeList();
      if (!listType) {
        listType = target;
        htmlParts.push(`<${listType}>`);
      }
      htmlParts.push(`<li>${inlineMarkdown((unordered || ordered)[1])}</li>`);
      continue;
    }
    paragraph.push(line.trim());
  }
  if (inCode) {
    htmlParts.push(`<pre><code>${esc(codeLines.join("\n"))}</code></pre>`);
  }
  flushBlocks();
  return htmlParts.join("\n");
}
async function api(path, opts = {}) {
  const res = await fetch(path, opts);
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}
function started(job) {
  if (!job.started_at) return "--:--:--";
  return new Date(job.started_at * 1000).toLocaleTimeString();
}
function classLabel(value) {
  return {
    L0_NOT_AFFECTED: "L0",
    L1_PRESENT_UNREACHABLE: "L1",
    L2_POTENTIALLY_REACHABLE: "L2",
    L3_REACHABLE: "L3",
    L4_EXPLOITABLE_CONFIRMED: "L4"
  }[value] || (value || "-");
}
function render(data, options = {}) {
  state.last = data;
  state.total = data.total;
  state.rows = data.jobs;
  $("subtitle").textContent = `flow=${data.current_flow} workers=${data.max_workers} auto=${data.auto_start ? "on" : "off"}`;
  $("workers").value = data.max_workers;
  $("autoBtn").textContent = data.auto_start ? "Auto On" : "Auto Off";
  $("flowSelect").innerHTML = data.flows.map(f => `<option ${f.name === data.current_flow ? "selected" : ""}>${esc(f.name)}</option>`).join("");
  const keys = ["total", "running", "paused", "queued", "succeeded", "failed"];
  $("metrics").innerHTML = keys.map(k => `<span class="metric"><span>${k}</span><strong>${data.counts[k] || 0}</strong></span>`).join("");
  const rows = data.jobs;
  if (!state.selectedId && rows[0]) state.selectedId = rows[0].id;
  $("jobs").innerHTML = rows.map(job => `
    <tr class="${job.id === state.selectedId ? "selected" : ""}" data-id="${esc(job.id)}">
      <td><span class="status ${esc(job.status)}">${esc(job.status)}</span></td>
      <td>${esc(job.flow_name)}</td>
      <td title="${esc(job.repo_name)}">${esc(job.repo_name)}</td>
      <td>${started(job)}</td>
      <td title="${esc(job.url)}">${esc(job.url)}</td>
    </tr>`).join("");
  const filtered = data.total === data.unfiltered_total ? "" : ` filtered from ${data.unfiltered_total}`;
  $("pageInfo").textContent = `Showing ${state.total ? state.offset + 1 : 0}-${Math.min(state.offset + data.jobs.length, state.total)} of ${state.total}${filtered}`;
  document.querySelectorAll("tr[data-id]").forEach(row => row.onclick = () => {
    state.selectedId = row.dataset.id;
    renderDetail();
    document.querySelectorAll("tr.selected").forEach(r => r.classList.remove("selected"));
    row.classList.add("selected");
  });
  if (!options.preserveDetail) {
    renderDetail();
  }
}
function renderShowcase(data, options = {}) {
  show.last = data;
  show.total = data.total;
  show.rows = data.items;
  const flowOptions = [`<option value="">all flows</option>`].concat(Object.keys(data.flows).sort().map(name => `<option ${name === $("showFlow").value ? "selected" : ""}>${esc(name)}</option>`));
  $("showFlow").innerHTML = flowOptions.join("");
  if (!show.selectedId && data.items[0]) show.selectedId = data.items[0].id;
  $("showRows").innerHTML = data.items.map(item => `
    <tr class="${item.id === show.selectedId ? "selected" : ""}" data-show-id="${esc(item.id)}">
      <td><span class="status ${esc(item.classification)}" title="${esc(item.classification || "-")}">${esc(classLabel(item.classification))}</span></td>
      <td>${esc(item.flow_name || "-")}</td>
      <td title="${esc(item.folder_name)}">${esc(item.folder_name)}</td>
      <td>${esc(item.vulnerability_id || item.finding_id || "-")}</td>
      <td title="${esc(item.summary || "")}">${esc(item.summary || item.component || "-")}</td>
    </tr>`).join("");
  const filtered = data.total === data.unfiltered_total ? "" : ` filtered from ${data.unfiltered_total}`;
  $("pageInfo").textContent = `Showcase ${data.total ? show.offset + 1 : 0}-${Math.min(show.offset + data.items.length, data.total)} of ${data.total}${filtered}`;
  document.querySelectorAll("tr[data-show-id]").forEach(row => row.onclick = () => {
    show.selectedId = row.dataset.showId;
    renderShowDetail("result");
    document.querySelectorAll("tr.selected").forEach(r => r.classList.remove("selected"));
    row.classList.add("selected");
  });
  if (!options.preserveDetail) {
    renderShowDetail(show.detailKind || "result");
  }
}
async function renderDetail() {
  const job = state.rows.find(j => j.id === state.selectedId);
  if (!job) {
    $("detail").innerHTML = "<h2>No job selected</h2>";
    return;
  }
  const log = await api(`/api/log?id=${encodeURIComponent(job.id)}&lines=160`).catch(() => ({lines:["<log unavailable>"]}));
  $("detail").innerHTML = `
    <h2>${esc(job.repo_name)}</h2>
    <div class="actions">
      <button data-action="retry">Retry</button>
      <button data-action="pause">Pause</button>
      <button data-action="resume">Resume</button>
      <button data-action="cancel">Cancel</button>
      <button class="danger" data-action="delete">Delete</button>
    </div>
    <div class="detail-grid">
      <div>id</div><div>${esc(job.id)}</div>
      <div>status</div><div class="${esc(job.status)}">${esc(job.status)}</div>
      <div>pid</div><div>${esc(job.pid || "-")}</div>
      <div>exit</div><div>${esc(job.exit_code ?? "-")}</div>
      <div>workspace</div><div>${esc(job.workspace)}</div>
      <div>error</div><div>${esc(job.error || "-")}</div>
    </div>
    <pre>${esc(log.lines.join("\n"))}</pre>`;
  document.querySelectorAll("[data-action]").forEach(btn => btn.onclick = () => jobAction(btn.dataset.action, job.id));
}
async function renderShowDetail(kind = "result") {
  show.detailKind = kind;
  const item = show.rows.find(i => i.id === show.selectedId);
  if (!item) {
    $("showDetail").innerHTML = "<h2>No result selected</h2>";
    return;
  }
  const content = await api(`/api/showcase/content?id=${encodeURIComponent(item.id)}&kind=${encodeURIComponent(kind)}`).catch(() => ({text:"<content unavailable>"}));
  const body = kind === "result"
    ? `<div class="markdown-body">${renderMarkdown(content.text || "")}</div>`
    : `<pre>${esc(content.text || "")}</pre>`;
  $("showDetail").innerHTML = `
    <h2>${esc(item.folder_name)}</h2>
    <div class="actions">
      <button data-show-kind="result">result.md</button>
      <button data-show-kind="json">result.json</button>
      <button data-show-kind="log">log</button>
    </div>
    <div class="detail-grid">
      <div>class</div><div class="${esc(item.classification)}">${esc(item.classification || "-")}</div>
      <div>source</div><div>${esc(item.source_type || "-")}${item.in_queue ? " / in queue" : ""}</div>
      <div>flow</div><div>${esc(item.flow_name || "-")}</div>
      <div>finding</div><div>${esc(item.finding_id || "-")}</div>
      <div>component</div><div>${esc(item.component || "-")}</div>
      <div>vuln</div><div>${esc(item.vulnerability_id || "-")}</div>
      <div>workspace</div><div>${esc(item.workspace || "-")}</div>
    </div>
    ${body}`;
  document.querySelectorAll("[data-show-kind]").forEach(btn => btn.onclick = () => renderShowDetail(btn.dataset.showKind));
}
async function load(options = {}) {
  state.limit = Number($("pageSize").value);
  const params = new URLSearchParams({
    offset: String(state.offset),
    limit: String(state.limit),
    q: $("search").value.trim(),
    status: $("statusFilter").value
  });
  const data = await api(`/api/state?${params.toString()}`);
  render(data, options);
}
async function loadShowcase(options = {}) {
  show.limit = Number($("pageSize").value);
  const params = new URLSearchParams({
    offset: String(show.offset),
    limit: String(show.limit),
    flow: $("showFlow").value,
    folder: $("showFolder").value.trim(),
    classification: $("showClass").value,
    q: $("showSearch").value.trim()
  });
  const data = await api(`/api/showcase?${params.toString()}`);
  renderShowcase(data, options);
}
function reloadActive() {
  return state.view === "showcase" ? loadShowcase({preserveDetail: true}) : load({preserveDetail: true});
}
async function post(path, payload) {
  await api(path, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(payload)});
  await load();
}
async function jobAction(action, id) {
  if (action === "delete" && !confirm("Delete selected job?")) return;
  await post("/api/job", {action, id});
}
async function bulk(action) {
  const label = action.replace("-", " ");
  if (!confirm(`Run bulk action: ${label}?`)) return;
  await post("/api/bulk", {action});
}
function setView(view) {
  state.view = view;
  $("jobsView").classList.toggle("hidden", view !== "jobs");
  $("jobsToolbar").classList.toggle("hidden", view !== "jobs");
  $("showcaseView").classList.toggle("hidden", view !== "showcase");
  $("showcaseToolbar").classList.toggle("hidden", view !== "showcase");
  $("jobsTab").classList.toggle("active", view === "jobs");
  $("showcaseTab").classList.toggle("active", view === "showcase");
  reloadActive();
}
async function exportShowcase(persist = false) {
  const params = new URLSearchParams({
    flow: $("showFlow").value,
    folder: $("showFolder").value.trim(),
    classification: $("showClass").value,
    q: $("showSearch").value.trim(),
    include_logs: "1"
  });
  if (persist) {
    const result = await api(`/api/showcase/persist?${params.toString()}`, {method: "POST"});
    alert(`Persisted ${result.count} result items`);
    await loadShowcase();
    return;
  }
  window.location = `/api/showcase/export?${params.toString()}`;
}
async function importShowcase() {
  const file = $("showImportFile").files[0];
  if (!file) return;
  const payload = JSON.parse(await file.text());
  const result = await api("/api/showcase/import", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(payload)});
  alert(`Imported ${result.count} result items`);
  $("showImportFile").value = "";
  await loadShowcase();
}
$("prev").onclick = () => {
  if (state.view === "showcase") { show.offset = Math.max(0, show.offset - show.limit); loadShowcase(); return; }
  state.offset = Math.max(0, state.offset - state.limit); load();
};
$("next").onclick = () => {
  if (state.view === "showcase") { if (show.offset + show.limit < show.total) show.offset += show.limit; loadShowcase(); return; }
  if (state.offset + state.limit < state.total) state.offset += state.limit; load();
};
$("pageSize").onchange = () => { state.offset = 0; show.offset = 0; reloadActive(); };
$("search").oninput = () => { state.offset = 0; load(); };
$("statusFilter").onchange = $("search").oninput;
$("workers").onchange = () => post("/api/settings", {max_workers: Number($("workers").value)});
$("autoBtn").onclick = () => post("/api/settings", {toggle_auto: true});
$("flowSelect").onchange = () => post("/api/settings", {flow: $("flowSelect").value});
document.querySelectorAll("[data-bulk]").forEach(btn => btn.onclick = () => bulk(btn.dataset.bulk));
$("jobsTab").onclick = () => setView("jobs");
$("showcaseTab").onclick = () => setView("showcase");
$("showFlow").onchange = () => { show.offset = 0; loadShowcase(); };
$("showFolder").oninput = () => { show.offset = 0; loadShowcase(); };
$("showClass").onchange = $("showFlow").onchange;
$("showSearch").oninput = $("showFlow").onchange;
$("showExport").onclick = () => exportShowcase(false);
$("showPersist").onclick = () => exportShowcase(true);
$("showImport").onclick = () => $("showImportFile").click();
$("showImportFile").onchange = () => importShowcase();
document.addEventListener("keydown", ev => {
  if (!ev.ctrlKey) return;
  const key = ev.key.toLowerCase();
  if (["u","o","p","g"].includes(key)) ev.preventDefault();
  if (key === "u") bulk("delete-succeeded");
  if (key === "o") bulk("delete-queued");
  if (key === "p") bulk("pause-running");
  if (key === "g") bulk("resume-paused");
});
load();
setInterval(() => reloadActive(), 2500);
</script>
</body>
</html>
"""


def web_json(handler: BaseHTTPRequestHandler, payload: Any, status: int = 200) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def web_download_json(handler: BaseHTTPRequestHandler, payload: Any, filename: str) -> None:
    body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    handler.send_response(200)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Disposition", f'attachment; filename="{filename}"')
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def read_web_json(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    length = int(handler.headers.get("Content-Length", "0") or "0")
    if length <= 0:
        return {}
    raw = handler.rfile.read(length).decode("utf-8")
    return json.loads(raw) if raw else {}


def web_state_payload(
    overseer: Overseer,
    offset: int = 0,
    limit: int = 50,
    status_filter: str = "",
    query: str = "",
) -> dict[str, Any]:
    with overseer.lock:
        all_jobs = list(overseer.jobs)
        jobs = all_jobs
        if status_filter:
            jobs = [job for job in jobs if job.status == status_filter]
        if query:
            lowered = query.lower()
            jobs = [
                job
                for job in jobs
                if lowered
                in " ".join(
                    [
                        job.id,
                        job.repo_name,
                        job.url,
                        job.error,
                        job.flow_name,
                        job.status,
                    ]
                ).lower()
            ]
        offset = clamp(offset, 0, max(len(jobs) - 1, 0)) if jobs else 0
        limit = clamp(limit, 1, 500)
        sliced = jobs[offset : offset + limit]
        counts: dict[str, int] = {}
        for job in all_jobs:
            counts[job.status] = counts.get(job.status, 0) + 1
        counts["total"] = len(all_jobs)
        return {
            "max_workers": overseer.max_workers,
            "auto_start": overseer.auto_start,
            "current_flow": overseer.current_flow,
            "flows": [asdict(flow) for _, flow in sorted(overseer.flows.items())],
            "counts": counts,
            "offset": offset,
            "limit": limit,
            "total": len(jobs),
            "unfiltered_total": len(all_jobs),
            "jobs": [asdict(job) for job in sliced],
        }


def load_showcase_imports() -> list[dict[str, Any]]:
    if not SHOWCASE_IMPORTS_PATH.exists():
        return []
    data = load_json_file(SHOWCASE_IMPORTS_PATH)
    if isinstance(data, dict):
        items = data.get("items", [])
    else:
        items = data
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict)]


def save_showcase_imports(items: list[dict[str, Any]]) -> None:
    SHOWCASE_IMPORTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SHOWCASE_IMPORTS_PATH.write_text(
        json.dumps({"schema": "codeslave-showcase-v1", "items": items}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def extract_classification(result_json: dict[str, Any] | None, result_md: str) -> str:
    if isinstance(result_json, dict):
        value = result_json.get("classification")
        if isinstance(value, str) and value.strip():
            return value.strip()
    for level in [
        "L4_EXPLOITABLE_CONFIRMED",
        "L3_REACHABLE",
        "L2_POTENTIALLY_REACHABLE",
        "L1_PRESENT_UNREACHABLE",
        "L0_NOT_AFFECTED",
    ]:
        if level in result_md:
            return level
    return ""


def normalize_showcase_item(item: dict[str, Any]) -> dict[str, Any]:
    item = dict(item)
    item.setdefault("schema", "codeslave-showcase-item-v1")
    item.setdefault("source_type", "import")
    item.setdefault("flow_name", "")
    item.setdefault("repo_name", item.get("folder_name", "imported-result"))
    item.setdefault("folder_name", item.get("repo_name", "imported-result"))
    item.setdefault("classification", "")
    item.setdefault("confidence", "")
    item.setdefault("finding_id", "")
    item.setdefault("component", "")
    item.setdefault("vulnerability_id", "")
    item.setdefault("summary", "")
    item.setdefault("result_md", "")
    item.setdefault("log_text", "")
    item.setdefault("imported_at", time.time())
    if not item.get("id"):
        item["id"] = stable_id(
            str(item.get("flow_name", "")),
            str(item.get("folder_name", "")),
            str(item.get("finding_id", "")),
            str(item.get("result_md", ""))[:2000],
        )
    return item


def workspace_showcase_items(overseer: Overseer) -> list[dict[str, Any]]:
    with overseer.lock:
        jobs = list(overseer.jobs)
    jobs_by_workspace = {str(Path(job.workspace)): job for job in jobs}
    items: list[dict[str, Any]] = []
    for result_md_path in sorted(overseer.workspace_root.glob("*/*/result.md")):
        workspace = result_md_path.parent
        flow_name = workspace.parent.name
        result_json_path = workspace / "result.json"
        finding_path = workspace / "finding.json"
        result_md = read_text_if_exists(result_md_path)
        result_json: dict[str, Any] | None = None
        finding_json: dict[str, Any] | None = None
        if result_json_path.exists():
            try:
                loaded = load_json_file(result_json_path)
                if isinstance(loaded, dict):
                    result_json = loaded
            except Exception:  # noqa: BLE001
                result_json = None
        if finding_path.exists():
            try:
                loaded = load_json_file(finding_path)
                if isinstance(loaded, dict):
                    finding_json = loaded
            except Exception:  # noqa: BLE001
                finding_json = None
        job = jobs_by_workspace.get(str(workspace))
        component = ""
        vulnerability_id = ""
        if isinstance(result_json, dict):
            raw_component = result_json.get("component")
            if isinstance(raw_component, dict):
                component = "@".join(
                    part
                    for part in [str(raw_component.get("name", "")), str(raw_component.get("version", ""))]
                    if part
                )
            vulnerability_id = str(result_json.get("vulnerability_id", "") or "")
        if not component and isinstance(finding_json, dict):
            raw_component = finding_json.get("component", {})
            if isinstance(raw_component, dict):
                component = "@".join(
                    part
                    for part in [str(raw_component.get("name", "")), str(raw_component.get("version", ""))]
                    if part
                )
            raw_vuln = finding_json.get("vulnerability", {})
            if isinstance(raw_vuln, dict):
                vulnerability_id = str(raw_vuln.get("id", "") or vulnerability_id)
        item = {
            "id": stable_id("workspace", str(workspace.resolve())),
            "schema": "codeslave-showcase-item-v1",
            "source_type": "workspace",
            "flow_name": flow_name,
            "repo_name": job.repo_name if job else workspace.name,
            "folder_name": workspace.name,
            "workspace": str(workspace),
            "result_md_path": str(result_md_path),
            "result_json_path": str(result_json_path) if result_json_path.exists() else "",
            "finding_json_path": str(finding_path) if finding_path.exists() else "",
            "log_path": job.log_path if job else "",
            "job_id": job.id if job else "",
            "job_status": job.status if job else "",
            "in_queue": job is not None,
            "classification": extract_classification(result_json, result_md),
            "confidence": str(result_json.get("confidence", "") if isinstance(result_json, dict) else ""),
            "finding_id": str(result_json.get("finding_id", "") if isinstance(result_json, dict) else ""),
            "component": component,
            "vulnerability_id": vulnerability_id,
            "summary": str(result_json.get("summary", "") if isinstance(result_json, dict) else ""),
            "updated_at": result_md_path.stat().st_mtime,
        }
        items.append(item)
    return items


def all_showcase_items(overseer: Overseer) -> list[dict[str, Any]]:
    workspace_items = workspace_showcase_items(overseer)
    imports = [normalize_showcase_item(item) for item in load_showcase_imports()]
    seen = {item["id"] for item in workspace_items}
    for item in imports:
        if item["id"] not in seen:
            workspace_items.append(item)
            seen.add(item["id"])
    return workspace_items


def filter_showcase_items(
    items: list[dict[str, Any]],
    flow: str = "",
    folder: str = "",
    classification: str = "",
    query: str = "",
) -> list[dict[str, Any]]:
    if flow:
        items = [item for item in items if item.get("flow_name") == flow]
    if folder:
        needle = folder.lower()
        items = [item for item in items if needle in str(item.get("folder_name", "")).lower()]
    if classification:
        items = [item for item in items if item.get("classification") == classification]
    if query:
        lowered = query.lower()
        fields = ["repo_name", "folder_name", "finding_id", "component", "vulnerability_id", "summary", "classification"]
        items = [
            item
            for item in items
            if lowered in " ".join(str(item.get(field, "")) for field in fields).lower()
        ]
    return items


def showcase_payload(
    overseer: Overseer,
    offset: int = 0,
    limit: int = 50,
    flow: str = "",
    folder: str = "",
    classification: str = "",
    query: str = "",
) -> dict[str, Any]:
    all_items = all_showcase_items(overseer)
    filtered = filter_showcase_items(all_items, flow, folder, classification, query)
    offset = clamp(offset, 0, max(len(filtered) - 1, 0)) if filtered else 0
    limit = clamp(limit, 1, 500)
    counts: dict[str, int] = {}
    flows: dict[str, int] = {}
    for item in all_items:
        cls = str(item.get("classification", "") or "<missing>")
        counts[cls] = counts.get(cls, 0) + 1
        flow_name = str(item.get("flow_name", "") or "<none>")
        flows[flow_name] = flows.get(flow_name, 0) + 1
    return {
        "schema": "codeslave-showcase-v1",
        "total": len(filtered),
        "unfiltered_total": len(all_items),
        "offset": offset,
        "limit": limit,
        "counts": counts,
        "flows": flows,
        "items": filtered[offset : offset + limit],
    }


def showcase_item_content(overseer: Overseer, item_id: str, kind: str) -> dict[str, Any] | None:
    item = next((entry for entry in all_showcase_items(overseer) if entry.get("id") == item_id), None)
    if item is None:
        return None
    if item.get("source_type") == "workspace":
        if kind == "result":
            text = read_text_if_exists(Path(str(item.get("result_md_path", ""))))
        elif kind == "log":
            text = read_text_if_exists(Path(str(item.get("log_path", ""))), max_chars=250000)
        elif kind == "json":
            text = read_text_if_exists(Path(str(item.get("result_json_path", ""))))
        else:
            text = ""
    else:
        if kind == "result":
            text = str(item.get("result_md", ""))
        elif kind == "log":
            text = str(item.get("log_text", ""))
        elif kind == "json":
            text = json.dumps(item.get("result_json", {}), ensure_ascii=False, indent=2)
        else:
            text = ""
    return {"item": item, "kind": kind, "text": text}


def export_showcase_items(items: list[dict[str, Any]], include_logs: bool = True) -> dict[str, Any]:
    exported = []
    for item in items:
        entry = normalize_showcase_item(item)
        if item.get("source_type") == "workspace":
            entry["result_md"] = read_text_if_exists(Path(str(item.get("result_md_path", ""))))
            result_json_path = Path(str(item.get("result_json_path", "")))
            entry["result_json"] = load_json_file(result_json_path) if result_json_path.exists() else {}
            if include_logs:
                entry["log_text"] = read_text_if_exists(Path(str(item.get("log_path", ""))), max_chars=1000000)
        exported.append(entry)
    return {
        "schema": "codeslave-showcase-export-v1",
        "exported_at": time.time(),
        "items": exported,
    }


def import_showcase_export(payload: dict[str, Any]) -> int:
    raw_items = payload.get("items", []) if isinstance(payload, dict) else []
    if not isinstance(raw_items, list):
        raise ValueError("showcase import payload must contain items array")
    existing = {item.get("id"): item for item in load_showcase_imports()}
    count = 0
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        item = normalize_showcase_item(raw)
        item["source_type"] = "import"
        item["imported_at"] = time.time()
        existing[item["id"]] = item
        count += 1
    save_showcase_imports(list(existing.values()))
    return count


def make_web_handler(overseer: Overseer) -> type[BaseHTTPRequestHandler]:
    class CodeSlaveWebHandler(BaseHTTPRequestHandler):
        server_version = "CodeSlaveWeb/1.0"

        def log_message(self, fmt: str, *args: Any) -> None:
            return

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/":
                body = WEB_INDEX.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if parsed.path == "/api/state":
                query = parse_qs(parsed.query)
                offset = int(query.get("offset", ["0"])[0])
                limit = int(query.get("limit", ["50"])[0])
                status_filter = STATUS_ALIASES.get(query.get("status", [""])[0], query.get("status", [""])[0])
                search_query = query.get("q", [""])[0].strip()
                web_json(self, web_state_payload(overseer, offset, limit, status_filter, search_query))
                return
            if parsed.path == "/api/log":
                query = parse_qs(parsed.query)
                job_id = query.get("id", [""])[0]
                lines = int(query.get("lines", ["160"])[0])
                with overseer.lock:
                    job = overseer._find_job(job_id)
                if job is None:
                    web_json(self, {"error": "job not found"}, 404)
                    return
                web_json(self, {"lines": tail_text(Path(job.log_path), clamp(lines, 1, 2000))})
                return
            if parsed.path == "/api/showcase":
                query = parse_qs(parsed.query)
                web_json(
                    self,
                    showcase_payload(
                        overseer,
                        offset=int(query.get("offset", ["0"])[0]),
                        limit=int(query.get("limit", ["50"])[0]),
                        flow=query.get("flow", [""])[0],
                        folder=query.get("folder", [""])[0],
                        classification=query.get("classification", [""])[0],
                        query=query.get("q", [""])[0],
                    ),
                )
                return
            if parsed.path == "/api/showcase/content":
                query = parse_qs(parsed.query)
                content = showcase_item_content(
                    overseer,
                    query.get("id", [""])[0],
                    query.get("kind", ["result"])[0],
                )
                if content is None:
                    web_json(self, {"error": "showcase item not found"}, 404)
                    return
                web_json(self, content)
                return
            if parsed.path == "/api/showcase/export":
                query = parse_qs(parsed.query)
                items = filter_showcase_items(
                    all_showcase_items(overseer),
                    flow=query.get("flow", [""])[0],
                    folder=query.get("folder", [""])[0],
                    classification=query.get("classification", [""])[0],
                    query=query.get("q", [""])[0],
                )
                include_logs = query.get("include_logs", ["1"])[0] not in {"0", "false", "False"}
                filename = f"codeslave-showcase-{int(time.time())}.json"
                web_download_json(self, export_showcase_items(items, include_logs=include_logs), filename)
                return
            web_json(self, {"error": "not found"}, 404)

        def do_POST(self) -> None:
            try:
                payload = read_web_json(self)
                if self.path == "/api/job":
                    self.handle_job(payload)
                    return
                if self.path == "/api/bulk":
                    self.handle_bulk(payload)
                    return
                if self.path == "/api/settings":
                    self.handle_settings(payload)
                    return
                if self.path == "/api/showcase/import":
                    count = import_showcase_export(payload)
                    web_json(self, {"ok": True, "count": count})
                    return
                if self.path.startswith("/api/showcase/persist"):
                    self.handle_showcase_persist()
                    return
                web_json(self, {"error": "not found"}, 404)
            except Exception as exc:  # noqa: BLE001
                web_json(self, {"error": str(exc)}, 400)

        def handle_job(self, payload: dict[str, Any]) -> None:
            action = str(payload.get("action", ""))
            job_id = str(payload.get("id", ""))
            with overseer.lock:
                job = overseer._find_job(job_id)
            if job is None:
                web_json(self, {"error": "job not found"}, 404)
                return
            if action == "retry":
                overseer.retry_job(job)
            elif action == "delete":
                overseer.delete_job(job)
            elif action == "cancel":
                job.status = "cancelled"
                overseer.cancel_job(job)
                overseer._save_state()
            elif action == "pause":
                overseer.pause_job(job)
            elif action == "resume":
                overseer.resume_job(job)
            else:
                web_json(self, {"error": f"unknown action: {html.escape(action)}"}, 400)
                return
            web_json(self, {"ok": True})

        def handle_bulk(self, payload: dict[str, Any]) -> None:
            action = str(payload.get("action", ""))
            if action == "delete-succeeded":
                count = overseer.bulk_delete_status("succeeded")
            elif action == "delete-queued":
                count = overseer.bulk_delete_status("queued")
            elif action == "pause-running":
                count = overseer.bulk_pause_running()
            elif action == "resume-paused":
                count = overseer.bulk_resume_paused()
            else:
                web_json(self, {"error": f"unknown bulk action: {html.escape(action)}"}, 400)
                return
            web_json(self, {"ok": True, "count": count})

        def handle_settings(self, payload: dict[str, Any]) -> None:
            if "max_workers" in payload:
                overseer.max_workers = max(int(payload["max_workers"]), 1)
            if payload.get("toggle_auto"):
                overseer.auto_start = not overseer.auto_start
            if "auto_start" in payload:
                overseer.auto_start = bool(payload["auto_start"])
            if "flow" in payload:
                overseer.set_flow(str(payload["flow"]))
            else:
                overseer._save_state()
            web_json(self, {"ok": True})

        def handle_showcase_persist(self) -> None:
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            items = filter_showcase_items(
                all_showcase_items(overseer),
                flow=query.get("flow", [""])[0],
                folder=query.get("folder", [""])[0],
                classification=query.get("classification", [""])[0],
                query=query.get("q", [""])[0],
            )
            include_logs = query.get("include_logs", ["1"])[0] not in {"0", "false", "False"}
            count = import_showcase_export(export_showcase_items(items, include_logs=include_logs))
            web_json(self, {"ok": True, "count": count})

    return CodeSlaveWebHandler


def serve_web(overseer: Overseer, host: str, port: int) -> None:
    server = ThreadingHTTPServer((host, port), make_web_handler(overseer))
    actual_host, actual_port = server.server_address[:2]
    print(f"CodeSlave Web UI: http://{actual_host}:{actual_port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


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

    web = sub.add_parser("web", help="Open the browser-based Web UI.")
    web.add_argument("--host", default="127.0.0.1")
    web.add_argument("--port", type=int, default=8765)

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

    sca_run = sub.add_parser("sca-run", help="Normalize a raw SCA report and queue sca_reachability jobs.")
    sca_run.add_argument("raw_report", help="Raw SCA JSON report")
    sca_run.add_argument("--target", help="Target project/source/artifact path for reachability analysis.")
    sca_run.add_argument("--output", help="Cleaned findings JSON path. Defaults to runtime/sca/<raw>.cleaned.json")
    sca_run.add_argument("--tool", default="auto", help="Scanner name hint for the cleaner.")
    sca_run.add_argument("--max-workers", type=int, default=2)
    sca_run.add_argument("--start", action="store_true", help="Start scheduling immediately and wait until idle.")
    sca_run.add_argument("--include-quick-exit", action="store_true", help="Also queue low-value quick-exit candidates.")

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
    start_scheduler = args.command in (None, "tui", "web", "run")
    overseer = Overseer(
        template_dir=Path(args.template_dir),
        workspace_root=Path(args.workspace_root),
        start_scheduler=start_scheduler,
    )
    try:
        if args.command in (None, "tui"):
            curses.wrapper(lambda stdscr: tui(stdscr, overseer))
            return 0
        if args.command == "web":
            serve_web(overseer, args.host, args.port)
            return 0
        if args.command == "enqueue":
            if args.flow:
                try:
                    overseer.set_flow(args.flow)
                except ValueError as exc:
                    print(str(exc))
                    return 1
            flow = overseer.flows.get(overseer.current_flow)
            for raw in args.urls:
                for url in parse_job_inputs(raw, flow):
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
            flow = overseer.flows.get(overseer.current_flow)
            for raw in args.urls:
                for url in parse_job_inputs(raw, flow):
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
            flow = overseer.flows.get(overseer.current_flow)
            for raw in args.urls:
                for url in parse_job_inputs(raw, flow):
                    overseer.add_job(url)
            overseer._save_state()
            wait_until_idle(overseer)
            failed = [job for job in overseer.jobs if job.status == "failed"]
            return 1 if failed else 0
        if args.command == "sca-run":
            if "sca_reachability" not in overseer.flows:
                print("flow not found: sca_reachability")
                return 1
            raw_report = Path(args.raw_report).expanduser()
            if not raw_report.is_file():
                print(f"raw SCA report not found: {raw_report}")
                return 1
            cleaned_path = (
                Path(args.output).expanduser()
                if args.output
                else RUNTIME_DIR / "sca" / f"{raw_report.stem}.cleaned.json"
            )
            cleaned_path.parent.mkdir(parents=True, exist_ok=True)
            command = [
                "python3",
                str(SCA_CLEANER_SCRIPT),
                str(raw_report),
                "-o",
                str(cleaned_path),
                "--tool",
                args.tool,
            ]
            result = subprocess.run(command, text=True, capture_output=True)
            if result.stdout:
                print(result.stdout.strip())
            if result.returncode != 0:
                if result.stderr:
                    print(result.stderr.strip())
                return result.returncode
            overseer.set_flow("sca_reachability")
            overseer.max_workers = max(args.max_workers, 1)
            if args.start:
                overseer.auto_start = True
            target_suffix = f"::{args.target}" if args.target else ""
            quick_exit_suffix = "::include-quick-exit" if args.include_quick_exit else ""
            flow = overseer.flows.get(overseer.current_flow)
            queued = 0
            for url in parse_job_inputs(f"{cleaned_path}{target_suffix}{quick_exit_suffix}", flow):
                job = overseer.add_job(url)
                queued += 1
                print(f"queued {job.repo_name} ({job.id})")
            print(f"queued {queued} sca_reachability jobs from {cleaned_path}")
            if args.start:
                overseer._save_state()
                wait_until_idle(overseer)
                failed = [job for job in overseer.jobs if job.status == "failed"]
                return 1 if failed else 0
            return 0
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
