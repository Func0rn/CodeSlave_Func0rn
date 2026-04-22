from __future__ import annotations

import json
import textwrap
from pathlib import Path
from typing import Any


FLOW_NAME = "sca_reachability"
FLOW_DESCRIPTION = "Read cleaned SCA findings JSON and fan out one reachability-analysis worker per finding."
FLOW_INPUT_LABEL = "Cleaned JSON"
TOOL_ROOT = Path(__file__).resolve().parents[1]
BUILTIN_REACHABILITY_SKILL = TOOL_ROOT / "skills" / "sca-reachability-analysis" / "SKILL.md"


def expand_inputs(raw: str) -> list[str]:
    source, target, include_quick_exit = _parse_input_spec(raw)
    if not source.is_file():
        return [raw.strip()]
    data = _load_cleaned_json(source)
    findings = data.get("findings", [])
    if not isinstance(findings, list):
        raise ValueError("cleaned JSON must contain findings array")
    jobs = []
    for finding in findings:
        if not isinstance(finding, dict):
            continue
        if finding.get("status") in {"analyzed", "deduplicated", "skipped"}:
            continue
        triage = finding.get("triage", {})
        if (
            isinstance(triage, dict)
            and triage.get("quick_exit")
            and not include_quick_exit
        ):
            continue
        finding_id = str(finding.get("finding_id", "")).strip()
        if not finding_id:
            raise ValueError("finding missing finding_id")
        jobs.append(_encode_finding_job(source.resolve(), finding_id, target))
    if not jobs:
        raise ValueError(f"no pending findings found in: {source}")
    return jobs


def derive_repo_name(raw: str) -> str:
    job = _decode_finding_job(raw)
    if job is None:
        source = Path(raw).expanduser()
        return source.stem or "sca-reachability"
    finding = _find_finding(Path(job["cleaned_json"]), job["finding_id"])
    component = finding.get("component", {})
    vulnerability = finding.get("vulnerability", {})
    name = "-".join(
        part
        for part in [
            Path(job["cleaned_json"]).stem,
            str(component.get("name", "component")),
            str(vulnerability.get("id", "vuln")),
            job["finding_id"][:8],
        ]
        if part
    )
    return name or f"finding-{job['finding_id'][:8]}"


def prepare_workspace(context: dict) -> None:
    workspace = Path(context["workspace"])
    job = _decode_finding_job(context["job"]["url"])
    if job is None:
        raise ValueError("sca_reachability flow expects a cleaned finding job payload")

    cleaned_path = Path(job["cleaned_json"]).expanduser()
    finding = _find_finding(cleaned_path, job["finding_id"])
    target_path = job.get("target_path", "")

    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "finding.json").write_text(
        json.dumps(finding, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (workspace / "cleaned.source.txt").write_text(str(cleaned_path.resolve()) + "\n", encoding="utf-8")
    if target_path:
        (workspace / "target.source.txt").write_text(str(Path(target_path).expanduser().resolve()) + "\n", encoding="utf-8")
    (workspace / "Agent.md").write_text(_agent_text(), encoding="utf-8")
    (workspace / "Task.md").write_text(_task_text(finding, target_path), encoding="utf-8")


def build_prompt(context: dict) -> str:
    workspace = Path(context["workspace"])
    agent_text = (workspace / "Agent.md").read_text(encoding="utf-8")
    task_text = (workspace / "Task.md").read_text(encoding="utf-8")
    finding_text = (workspace / "finding.json").read_text(encoding="utf-8")
    skill_text = _builtin_skill_text()
    return textwrap.dedent(
        f"""
        这是 SCA 漏洞可达性并发分析任务。

        当前 worker 只负责 `finding.json` 中这一条 finding。不要分析 cleaned JSON 中的其他 finding，也不要声称完成全量报告。

        执行要求：
        1. 使用下方 `[Built-in skill: sca-reachability-analysis]` 的分级、证据 ID、quick-exit 和输出约束；不要依赖用户目录中的同名 skill。
        2. 如果 `finding.json.triage.quick_exit=true`，先执行 quick-exit 短路径：核查 quick_exit_reason 是否成立，输出 L1/L2、缺失证据和复核条件；除非发现远程/Web/高影响例外信号，不要进入完整长任务。
        3. 非 quick-exit 项必须主动通过 Web 检索理解目标漏洞本身，优先查厂商公告、GHSA/OSV/NVD、补丁 diff、回归测试、官方 issue、可信漏洞分析文章和 PoC 说明。记录 URL 和你从中提取的触发条件；不要只依赖 scanner 描述。
        4. 非 quick-exit 项采用长任务工作方式：先写 `Prompt.md`、`Plan.md`、`Implement.md`、`Documentation.md` 作为持久化项目记忆；按 milestone 执行“计划 -> 检索/分析 -> 本地验证 -> 修正 -> 记录状态”的循环。
        5. 先解析漏洞签名，再核查组件运行面、调用链、配置链、数据流和入口暴露。
        6. 如果当前 workspace 没有目标项目源码或构建产物，只能输出缺失证据和下一步探针，不得判 L3/L4。
        7. 对非 quick-exit 项尽可能深入分析，不要满足于摘要判断；只在证据链、缺失证据和验证记录完整后结束。
        8. 输出写入当前目录 `result.json` 和 `result.md`。
        9. `result.json` 必须包含 finding_id、classification、confidence、evidence_ids、missing_evidence、web_references、analysis_scope、quick_exit、summary。

        [Built-in skill: sca-reachability-analysis]
        {skill_text}

        [Agent.md]
        {agent_text}

        [Task.md]
        {task_text}

        [finding.json]
        {finding_text}
        """
    ).strip()


def _builtin_skill_text() -> str:
    if not BUILTIN_REACHABILITY_SKILL.exists():
        raise FileNotFoundError(f"built-in skill not found: {BUILTIN_REACHABILITY_SKILL}")
    return BUILTIN_REACHABILITY_SKILL.read_text(encoding="utf-8", errors="replace")


def _load_cleaned_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    if not isinstance(data, dict):
        raise ValueError("cleaned JSON root must be an object")
    if data.get("schema_version") != "sca-clean-v1":
        raise ValueError("cleaned JSON schema_version must be sca-clean-v1")
    return data


def _find_finding(cleaned_path: Path, finding_id: str) -> dict[str, Any]:
    data = _load_cleaned_json(cleaned_path)
    for finding in data.get("findings", []):
        if isinstance(finding, dict) and str(finding.get("finding_id")) == finding_id:
            return finding
    raise FileNotFoundError(f"finding_id not found: {finding_id}")


def _encode_finding_job(cleaned_path: Path, finding_id: str, target_path: str = "") -> str:
    return json.dumps(
        {
            "kind": "sca-reachability-finding",
            "cleaned_json": str(cleaned_path),
            "finding_id": finding_id,
            "target_path": target_path,
        },
        ensure_ascii=False,
    )


def _decode_finding_job(raw: str) -> dict[str, str] | None:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict) or payload.get("kind") != "sca-reachability-finding":
        return None
    cleaned_json = payload.get("cleaned_json")
    finding_id = payload.get("finding_id")
    target_path = payload.get("target_path", "")
    if not isinstance(cleaned_json, str) or not isinstance(finding_id, str):
        raise ValueError("invalid sca reachability job payload")
    if not isinstance(target_path, str):
        raise ValueError("invalid sca reachability target_path")
    return {"cleaned_json": cleaned_json, "finding_id": finding_id, "target_path": target_path}


def _parse_input_spec(raw: str) -> tuple[Path, str, bool]:
    value = raw.strip()
    include_quick_exit = False
    if "::include-quick-exit" in value:
        value = value.replace("::include-quick-exit", "")
        include_quick_exit = True
    if "::" not in value:
        return Path(value).expanduser(), "", include_quick_exit
    cleaned, target = value.split("::", 1)
    return Path(cleaned.strip()).expanduser(), target.strip(), include_quick_exit


def _agent_text() -> str:
    return """# Agent.md

你是 SCA 漏洞可达性分析 worker。你的职责是对单条 finding 做证据约束的可达性分析。

工作方式：
- 这是长任务，不是一轮摘要。使用持久化 markdown 文件管理状态，避免漂移。
- 以 CodeSlave 仓库内置的 `skills/sca-reachability-analysis/SKILL.md` 为准，不依赖用户级 skill 安装状态。
- 但如果 `triage.quick_exit=true` 且没有例外信号，执行短路径即可，不要浪费长任务预算。
- 先建立 `Prompt.md` 固化目标、边界、输入、产出和 done 条件。
- 再建立 `Plan.md`，拆成可验证 milestone：漏洞情报、签名解析、组件存在性、静态可达性、配置/guard、结论/QC。
- 再建立 `Implement.md`，记录执行规则：每个 milestone 完成后必须验证、失败要修复或降级，不得跳过缺失证据。
- 持续更新 `Documentation.md`，记录已完成项、证据 URL、本地证据、决策和后续缺口。

Web 研究要求：
- 必须主动查找漏洞本身的资料，而不是只看 `finding.json` 的描述。
- 资料优先级：厂商公告 > GHSA/OSV/NVD > fix commit/patch diff > regression tests > 官方 issue/release note > 可信安全文章 > PoC。
- 记录每个来源的 URL、可信度、提取出的触发条件和补丁语义。
- 如果不同来源冲突，优先相信厂商公告和补丁 diff，并在结果中列出冲突。

边界：
- 只分析当前 `finding.json`。
- 不要声明全量报告完成。
- 没有源码、构建产物或运行配置时，优先输出缺失证据和下一步探针。
- 不允许凭组件版本命中直接判可达。
- 不允许无 PoC 效果判 L4。
"""


def _task_text(finding: dict[str, Any], target_path: str = "") -> str:
    component = finding.get("component", {})
    vulnerability = finding.get("vulnerability", {})
    return textwrap.dedent(
        f"""
        # Task.md

        分析这一条 SCA finding 的漏洞可达性。

        - finding_id: {finding.get("finding_id", "")}
        - component: {component.get("name", "")}@{component.get("version", "")}
        - ecosystem: {component.get("ecosystem", "")}
        - vulnerability: {vulnerability.get("id", "")}
        - severity: {vulnerability.get("severity", "")}
        - target_path: {target_path or "未提供"}

        产出要求：
        1. 写 `result.md`：中文说明结论、证据链、缺失证据、SAST/DAST 线索和修复建议。
        2. 写 `result.json`：机器可汇总字段。
        3. `classification` 只能是 L0_NOT_AFFECTED、L1_PRESENT_UNREACHABLE、L2_POTENTIALLY_REACHABLE、L3_REACHABLE、L4_EXPLOITABLE_CONFIRMED。
        4. 如果提供了 `target_path`，优先从该路径读取源码、依赖清单、构建产物和配置证据。
        5. 如果没有目标源码或运行产物证据，默认最高只能到 L2，并说明缺什么。
        6. 必须生成并维护 `Prompt.md`、`Plan.md`、`Implement.md`、`Documentation.md`，把长任务状态、Web 资料、验证步骤和结论依据写清楚。
        7. 必须尽力进行 Web 检索，增强对漏洞触发条件、补丁语义和真实利用前置条件的理解。
        8. `result.json` 至少包含：`finding_id`、`component`、`vulnerability_id`、`classification`、`confidence`、`evidence_ids`、`missing_evidence`、`web_references`、`analysis_scope`、`sast_hints`、`dast_hints`、`summary`。
        9. 如果 `triage.quick_exit=true`，`result.json` 还必须包含 `quick_exit=true`、`quick_exit_reason` 和 `recheck_triggers`。
        """
    ).strip() + "\n"
