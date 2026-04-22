# CodeSlave

```text

    __/\
   /,   \
  /  / \ \
 /__/   \ )
       / /
      /_/
```

## 目录结构

- `scripts/codeslave.py`
  主控 TUI 和 CLI
- `templates/PhpCodeCheck/`
  内置模板
- `flows/`
  可切换流程脚本
- `skills/`
  CodeSlave 内置 Codex 格式 skills。当前内置 `sca-report-cleaner` 与 `sca-reachability-analysis`，避免 SCA flow 依赖用户级 `~/.codex/skills`
- `workspaces/`
  每个目标仓库的独立工作目录
- `runtime/`
  运行状态和日志

## 启动

```bash
codeslave tui
```

Web UI：

```bash
codeslave web --host 127.0.0.1 --port 8765
```

快捷键：

- `a`：新增当前 flow 所需输入
- `d`：删除当前任务
- `f`：打开 flow 选择面板
- `w`：新建 flow 向导
- `+` / `-`：调整并发 worker 数量
- `space`：切换自动调度
- `PageUp` / `PageDown`：任务列表翻页
- `Home` / `End`：跳到第一个/最后一个任务
- `r`：重试当前任务
- `x`：取消当前运行中的任务
- `Ctrl+U`：删除所有 `succeeded` 任务
- `Ctrl+O`：删除所有 `queued` 任务
- `Ctrl+P`：暂停所有 `running` 任务，并关闭自动调度
- `Ctrl+G`：恢复所有 `paused` 任务
- `enter`：查看当前任务日志
- `q`：退出

Web UI 支持：

- 分页任务表格，适合几百到上千个任务
- 状态计数、状态过滤、关键词搜索
- 单任务重试/暂停/恢复/取消/删除
- 批量删除成功任务、删除排队任务、暂停运行任务、恢复暂停任务
- 同样支持 `Ctrl+U` / `Ctrl+O` / `Ctrl+P` / `Ctrl+G` 组合键，执行前会弹出浏览器确认框
- Showcase 展示台：扫描所有 flow workspace 中的 `result.md` / `result.json`
- Showcase 可按 flow、结果目录字符串、classification 和关键词筛选，例如筛选 `sca_reachability` 的 `L2_POTENTIALLY_REACHABLE`
- Showcase 可直接查看 `result.md`、`result.json` 和关联 worker log
- Showcase 支持导出当前筛选结果为结构化 JSON，导出内容包含 result 和 log，适合长期归档
- Showcase 支持重新导入导出的 JSON，导入项会写入 `runtime/showcase_imports.json`，即使原任务不在队列中也会继续显示

自定义 flow 如果希望进入 Showcase，建议在每个 workspace 输出：

- `result.md`：人工阅读结果
- `result.json`：机器可筛选元数据，建议包含 `classification`、`confidence`、`summary`

对于 `sca_reachability`，Showcase 会优先读取 `result.json.classification`；如果缺失，会从 `result.md` 中匹配 `L0_NOT_AFFECTED`、`L1_PRESENT_UNREACHABLE`、`L2_POTENTIALLY_REACHABLE`、`L3_REACHABLE`、`L4_EXPLOITABLE_CONFIRMED`。

日志页：

- `Left` / `Right` 或 `h` / `l`：横向滚动
- `PageUp` / `PageDown`：快速纵向翻页
- `/`：进入实时搜索
- `n`：下一个匹配
- `p`：上一个匹配

Flow 选择页：

- `Up` / `Down` 或 `j` / `k`：移动选择
- `Enter`：切换到选中的 flow
- `PageUp` / `PageDown`：快速翻页
- `q` / `Esc`：取消

批量导入：

- 如果当前 flow 支持文件导入，`a` 输入框会显示 `or file path`
- 输入一个本地文件路径后，会按行读取内容并批量入队
- 空行和以 `#` 开头的行会被忽略

## 常用命令

```bash
codeslave flows
codeslave prepare https://github.com/yesilmen-vm/YesilCMS
codeslave run --flow php_code_check --max-workers 3 https://github.com/yesilmen-vm/YesilCMS
codeslave sca-run raw-sca.json --target /path/to/project --max-workers 8
codeslave status --json
```

`sca-run` 是 SCA 可达性分析快捷方式：它会先调用 `sca-report-cleaner` 清洗原始报告，再自动使用 `sca_reachability` flow 为每条 pending finding 创建一个独立 worker。加 `--start` 会立即开始调度并等待结束；不加时只入队，适合先进入 TUI 检查任务数和并发。

## 内置 Skills

CodeSlave 内置少量 Codex 格式 skills，放在 `skills/<skill-name>/` 下，每个 skill 保留标准 `SKILL.md`，可选 `agents/` 和 `scripts/`：

```text
skills/
├── sca-report-cleaner/
│   ├── SKILL.md
│   ├── agents/openai.yaml
│   └── scripts/normalize_sca_report.py
└── sca-reachability-analysis/
    ├── SKILL.md
    └── agents/openai.yaml
```

当前内置 skills 只支持 Codex skill 格式，不兼容其他 agent 的 skill/plugin 结构。

- `sca-report-cleaner`
  用于把大型 SCA、SBOM、依赖扫描 JSON 清洗成 `sca-clean-v1` cleaned findings。`codeslave sca-run` 会优先调用项目内置脚本 `skills/sca-report-cleaner/scripts/normalize_sca_report.py`；只有内置脚本缺失时才回退到用户级 `~/.codex/skills` 路径。
- `sca-reachability-analysis`
  用于约束每个 SCA finding worker 的可达性分析：漏洞签名解析、证据 ID、quick-exit、L0-L4 分级、SAST/DAST 线索和输出格式。`sca_reachability` flow 会把项目内置 `skills/sca-reachability-analysis/SKILL.md` 注入 worker prompt，因此 worker 不再依赖用户目录中是否安装同名 skill。

这两个内置 skill 的目标是让 CodeSlave 的 SCA 流程可迁移、可复现：把仓库复制到新机器后，只要 CodeSlave 本体和 Codex 可用，`sca-run` 与 `sca_reachability` 的核心约束仍然存在。

## Flow

Flow 文件放在 `flows/*.py` 下，启动时会自动发现，不需要额外注册。

当前内置 flow：

- `php_code_check`
  路径：`flows/php_code_check.py`
  输入：`GitHub URL`
  批量导入：支持，本地文件中每行一个 GitHub URL
  行为：复制 `templates/PhpCodeCheck/`，改写 `Task` 第一行，然后把 `Agent.md` 和 `Task` 一起交给 Codex
- `hello-world`
  路径：`flows/hello-world.py`
  输入：`Task Input`
  行为：创建最小工作区，写入 `Task` 和占位 `Agent.md`，然后把内容交给 Codex
- `mutiwork`
  路径：`flows/mutiwork.py`
  输入：`Multiwork Path`
  行为：支持两种输入
  1. 一个目录：读取共享 `Agent.md` 和多个任务文件，把每个任务文件展开成一个独立 job
  2. 一个 taskbook Markdown：解析顶层 `# Agent.md` 和多个 `# Task-*.md` 分段，再展开成多个独立 job
  每个 worker 都会拿到同一份 `Agent.md` 和自己专属的 `Task.md`
- `sca_reachability`
  路径：`flows/sca_reachability.py`
  输入：`Cleaned JSON`
  行为：读取内置 `sca-report-cleaner` 生成的 `sca-clean-v1` JSON，把每个 pending finding 展开成一个独立 worker。每个 worker 只拿自己的 `finding.json`，并注入内置 `sca-reachability-analysis` skill 约束，输出 `result.md` 与 `result.json`，避免把大型 SCA 报告整体塞进单次提示词。
  可选输入格式：`/path/cleaned-findings.json::/path/to/target-project`，用于把目标项目源码路径传给每个 worker。

`mutiwork` 目录约定示例：

```text
/path/to/mutiwork/
├── Agent.md
├── Task-frontend.md
├── Task-backend.md
└── Task-review.md
```

你也可以使用别的文件名；只要目录里存在 `Agent.md`，其余文件会被当作候选任务文件，优先选择名字以 `Task` 开头的文件。

`mutiwork` 也支持单文件 taskbook，示例：

```markdown
# Agent.md

共享任务说明

# Task-01-Example.md

第一个子任务

# Task-02-Example.md

第二个子任务
```

如果输入的是 taskbook 文件，`mutiwork` 会把每个 `# Task-*.md` 分段拆成一个独立 workspace。可直接参考 [MULTIWORK_TASKBOOK_TEMPLATE.md](/home/kali/Desktop/CodeSlave/MULTIWORK_TASKBOOK_TEMPLATE.md)。

如果你要让另一个 Codex 快速帮你生成新 flow，可以直接使用：

- [FLOW_AUTHORING_PROMPT.md](/home/kali/Desktop/CodeSlave/FLOW_AUTHORING_PROMPT.md)

## 新建 Flow 向导

TUI 按 `w` 后会进入分步向导：

- 先选 flow 脚手架类型
  - `prompt-only`：最小任务流，只创建 `Task` 和占位 `Agent.md`
  - `template-copy`：复用 `PhpCodeCheck` 模板，适合仓库导入类任务
- flow 名称
- flow 描述
- 输入提示文案
- worker 指令

工具会生成 `flows/<flow-name>.py` 并切换到新 flow。
这个向导生成的是“脚手架”，不是完整 flow 设计器；复杂 flow 仍然应该继续手改生成的 Python 文件。
生成后的 flow 只要保留在 `flows/` 目录并满足接口，就会在下次刷新/启动时自动出现。

CLI 也支持：

```bash
codeslave new-flow --mode prompt-only my-flow "My custom flow" "Read Agent.md first. Then execute Task."
codeslave new-flow --mode template-copy --input-label "GitHub URL" repo-audit "Repo audit flow" "Read Agent.md first. Then execute Task."
```

## 行为边界

- CodeSlave 负责模板复制、Task 注入、worker 并发和日志管理
- 具体业务流程放在 `flows/*.py`
- worker 本体仍然是 `codex exec`
- `php_code_check` 这类模板流如果目标目录已存在，不会覆盖整个目录，只会继续在该目录上执行 flow 逻辑
- `prompt-only` 这类最小流会直接写入当前 flow 自己的 `Task` / `Agent.md`
