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
- `workspaces/`
  每个目标仓库的独立工作目录
- `runtime/`
  运行状态和日志

## 启动

```bash
python3 scripts/codeslave.py tui
```

快捷键：

- `a`：新增当前 flow 所需输入
- `d`：删除当前任务
- `f`：打开 flow 选择面板
- `w`：新建 flow 向导
- `+` / `-`：调整并发 worker 数量
- `space`：切换自动调度
- `r`：重试当前任务
- `x`：取消当前运行中的任务
- `enter`：查看当前任务日志
- `q`：退出

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
python3 scripts/codeslave.py flows
python3 scripts/codeslave.py prepare https://github.com/yesilmen-vm/YesilCMS
python3 scripts/codeslave.py run --flow php_code_check --max-workers 3 https://github.com/yesilmen-vm/YesilCMS
python3 scripts/codeslave.py status --json
```

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
python3 scripts/codeslave.py new-flow --mode prompt-only my-flow "My custom flow" "Read Agent.md first. Then execute Task."
python3 scripts/codeslave.py new-flow --mode template-copy --input-label "GitHub URL" repo-audit "Repo audit flow" "Read Agent.md first. Then execute Task."
```

## 行为边界

- CodeSlave 负责模板复制、Task 注入、worker 并发和日志管理
- 具体业务流程放在 `flows/*.py`
- worker 本体仍然是 `codex exec`
- `php_code_check` 这类模板流如果目标目录已存在，不会覆盖整个目录，只会继续在该目录上执行 flow 逻辑
- `prompt-only` 这类最小流会直接写入当前 flow 自己的 `Task` / `Agent.md`
