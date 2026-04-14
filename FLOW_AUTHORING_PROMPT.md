# Flow Authoring Prompt

把下面这段提示词发给另一个 Codex agent，即可让它在 `CodeSlave` 里新增或修改流程脚本。

```text
你在维护一个本地 TUI 工具：`CodeSlave`。

目标：
新增或修改一个 flow 脚本，让主控器可以切换不同任务流程，而不是把流程写死在主控器里。

你必须遵守以下约束：
1. 不要把具体业务流程写进 `scripts/codeslave.py`，流程逻辑只允许放在 `flows/*.py`
2. 每个 flow 文件都必须导出：
   - `FLOW_NAME`
   - `FLOW_DESCRIPTION`
   - `prepare_workspace(context)`
   - `build_prompt(context)`
3. `prepare_workspace(context)` 负责：
   - 准备工作目录
   - 复制模板或初始化文件
   - 注入 URL / Task / Agent 等上下文
4. `build_prompt(context)` 负责：
   - 给内层 Codex 生成最终 prompt
   - 明确执行要求、输入文件和输出位置
5. 主控器只负责：
   - 选择 flow
   - 管理并发
   - 启动/取消 worker
   - 查看日志

上下文结构：
- `context["job"]`：当前 job 的字典
- `context["workspace"]`：当前工作目录
- `context["workspace_root"]`：所有项目根目录
- `context["template_dir"]`：默认模板目录
- `context["tool_root"]`：工具根目录

要求你完成：
1. 先阅读现有 `flows/*.py`
2. 按现有风格实现新 flow，不破坏已有 flow
3. 如需改 README，只补充 flow 使用说明
4. 不要删除现有默认 flow，除非任务明确要求
5. 代码改完后，运行 `python3 -m py_compile` 做语法检查

输出要求：
- 直接修改代码，不只给方案
- 最终说明新增/修改了哪个 flow 文件
- 简短说明这个 flow 适合什么场景
```
