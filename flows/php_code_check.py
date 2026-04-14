from __future__ import annotations

import shutil
import textwrap
from pathlib import Path


FLOW_NAME = "php_code_check"
FLOW_DESCRIPTION = "Copy PhpCodeCheck, rewrite Task first line, then run Codex on Agent.md + Task."
FLOW_INPUT_LABEL = "GitHub URL"
FLOW_ALLOW_BATCH_FILE = True


def prepare_workspace(context: dict) -> None:
    template_dir = Path(context["template_dir"])
    workspace = Path(context["workspace"])
    job = context["job"]

    if not template_dir.exists():
        raise FileNotFoundError(f"template not found: {template_dir}")

    if not workspace.exists():
        shutil.copytree(template_dir, workspace)

    task_path = workspace / "Task"
    if not task_path.exists():
        raise FileNotFoundError(f"Task not found: {task_path}")

    lines = task_path.read_text(encoding="utf-8").splitlines()
    if lines:
        lines[0] = job["url"]
    else:
        lines = [job["url"]]
    task_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


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
        你当前工作目录中已经准备好了 `Agent.md` 和 `Task`。

        执行要求：
        1. 先阅读并遵循 `Agent.md`
        2. 再严格完成 `Task`
        3. 尽量持续推进，不要停在等待人工确认
        4. 所有产出都保留在当前工作目录

        [Agent.md]
        {agent_text}

        [Task]
        {task_text}
        """
    ).strip()
