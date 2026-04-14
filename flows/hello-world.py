from __future__ import annotations

import textwrap
from pathlib import Path


FLOW_NAME = "hello-world"
FLOW_DESCRIPTION = "输入hello_world"
FLOW_INPUT_LABEL = "Task Input"


def prepare_workspace(context: dict) -> None:
    workspace = Path(context["workspace"])
    job = context["job"]
    workspace.mkdir(parents=True, exist_ok=True)

    task_path = workspace / "Task"
    task_path.write_text(job["url"].rstrip() + "\n", encoding="utf-8")

    agent_path = workspace / "Agent.md"
    agent_path.write_text("# Agent.md\n\nNo agent instructions provided.\n", encoding="utf-8")


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
        简单的hello_world

        [Agent.md]
        {agent_text}

        [Task]
        {task_text}
        """
    ).strip()
