from __future__ import annotations

import json
import shutil
import textwrap
from pathlib import Path


FLOW_NAME = "mutiwork"
FLOW_DESCRIPTION = "Import a shared Agent.md plus multiple task files, then fan them out to separate Codex workers."
FLOW_INPUT_LABEL = "Multiwork Path"


def expand_inputs(raw: str) -> list[str]:
    source = Path(raw).expanduser()
    if source.is_file():
        taskbook = _try_parse_taskbook(source)
        if taskbook is not None:
            return [
                _encode_taskbook_job(source.resolve(), task_name)
                for task_name, _task_text in taskbook["tasks"]
            ]
        return [str(source.resolve())]
    if source.is_dir():
        return [str(task.resolve()) for task in _list_task_files(source)]
    return [raw.strip()]


def derive_repo_name(raw: str) -> str:
    taskbook_job = _decode_taskbook_job(raw)
    if taskbook_job is not None:
        book_path = Path(taskbook_job["taskbook"])
        task_name = Path(taskbook_job["task_name"]).stem
        return f"{book_path.stem}-{task_name}"
    source = Path(raw).expanduser()
    if source.is_file():
        return f"{source.parent.name}-{source.stem}"
    return source.name or "multiwork"


def prepare_workspace(context: dict) -> None:
    workspace = Path(context["workspace"])
    raw_input = context["job"]["url"]
    taskbook_job = _decode_taskbook_job(raw_input)

    if taskbook_job is not None:
        _prepare_taskbook_workspace(workspace, taskbook_job)
        return

    source_task = Path(raw_input).expanduser()

    if not source_task.exists() or not source_task.is_file():
        raise FileNotFoundError(f"task file not found: {source_task}")

    source_dir = source_task.parent
    agent_source = source_dir / "Agent.md"
    if not agent_source.exists():
        raise FileNotFoundError(f"shared Agent.md not found: {agent_source}")

    workspace.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(agent_source, workspace / "Agent.md")
    shutil.copyfile(source_task, workspace / "Task.md")
    (workspace / "Task.source.txt").write_text(str(source_task.resolve()) + "\n", encoding="utf-8")


def build_prompt(context: dict) -> str:
    workspace = Path(context["workspace"])
    task_path = workspace / "Task.md"
    agent_path = workspace / "Agent.md"
    task_text = task_path.read_text(encoding="utf-8")
    agent_text = agent_path.read_text(encoding="utf-8")

    return textwrap.dedent(
        f"""
        这是一个多人分工任务流。

        你和其他 Codex worker 共享同一份 `Agent.md`，它定义共同目标、边界和产出规范。
        你当前只负责本工作目录中的 `Task.md`，不要擅自扩展成整个项目总包任务。

        执行要求：
        1. 先完整阅读并遵循 `Agent.md`
        2. 再只完成当前 `Task.md` 指定的子任务
        3. 输出尽量落在当前工作目录，保持结果可交接、可汇总
        4. 如果需要写说明，明确写出你完成了哪一部分，以及遗留项

        [Agent.md]
        {agent_text}

        [Task.md]
        {task_text}
        """
    ).strip()


def _list_task_files(source_dir: Path) -> list[Path]:
    candidates = [
        path
        for path in sorted(source_dir.iterdir())
        if path.is_file() and path.name != "Agent.md" and not path.name.startswith(".")
    ]
    preferred = [
        path
        for path in candidates
        if path.stem.lower().startswith("task") or path.name.lower().startswith("task")
    ]
    tasks = preferred or candidates
    if not tasks:
        raise FileNotFoundError(f"no task files found in: {source_dir}")
    return tasks


def _prepare_taskbook_workspace(workspace: Path, taskbook_job: dict[str, str]) -> None:
    taskbook_path = Path(taskbook_job["taskbook"]).expanduser()
    parsed = _parse_taskbook(taskbook_path)
    task_name = taskbook_job["task_name"]
    task_map = {name: text for name, text in parsed["tasks"]}
    task_text = task_map.get(task_name)
    if task_text is None:
        raise FileNotFoundError(f"task section not found in taskbook: {task_name}")

    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "Agent.md").write_text(parsed["agent"], encoding="utf-8")
    (workspace / "Task.md").write_text(task_text, encoding="utf-8")
    (workspace / "Task.source.txt").write_text(
        f"{taskbook_path.resolve()}#{task_name}\n",
        encoding="utf-8",
    )


def _try_parse_taskbook(path: Path) -> dict[str, object] | None:
    try:
        return _parse_taskbook(path)
    except ValueError:
        return None


def _parse_taskbook(path: Path) -> dict[str, object]:
    text = path.read_text(encoding="utf-8")
    sections = _split_taskbook_sections(text)
    agent_text = sections.get("Agent.md", "").strip()
    tasks = [
        (name, body.strip() + "\n")
        for name, body in sections.items()
        if name.startswith("Task-") and name.endswith(".md")
    ]
    if not agent_text:
        raise ValueError(f"taskbook missing Agent.md section: {path}")
    if not tasks:
        raise ValueError(f"taskbook missing Task-*.md sections: {path}")
    return {"agent": agent_text + "\n", "tasks": tasks}


def _split_taskbook_sections(text: str) -> dict[str, str]:
    sections: dict[str, list[str]] = {}
    current_name: str | None = None
    current_lines: list[str] = []

    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("# "):
            if current_name is not None:
                sections[current_name] = current_lines
            current_name = stripped[2:].strip()
            current_lines = []
            continue
        if current_name is not None:
            current_lines.append(line)

    if current_name is not None:
        sections[current_name] = current_lines

    return {name: "\n".join(lines).strip() for name, lines in sections.items()}


def _encode_taskbook_job(taskbook_path: Path, task_name: str) -> str:
    return json.dumps(
        {"kind": "mutiwork-taskbook", "taskbook": str(taskbook_path), "task_name": task_name},
        ensure_ascii=False,
    )


def _decode_taskbook_job(raw: str) -> dict[str, str] | None:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("kind") != "mutiwork-taskbook":
        return None
    taskbook = payload.get("taskbook")
    task_name = payload.get("task_name")
    if not isinstance(taskbook, str) or not isinstance(task_name, str):
        raise ValueError("invalid mutiwork taskbook job payload")
    return {"taskbook": taskbook, "task_name": task_name}
