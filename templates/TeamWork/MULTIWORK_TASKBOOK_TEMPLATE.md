# Agent.md

## 共同任务

在这里写所有 Codex worker 共享的总目标、范围、成功标准和协作约束。

## 共同规范

1. 先阅读本文件，再执行各自的 `Task-*.md`
2. 每个 worker 只负责自己的子任务，不吞并其他任务
3. 产出要可汇总，优先写结构化结果
4. 明确记录已完成项、未完成项和阻塞点

## 统一输入

- 在这里列出所有 worker 都默认可用的输入

## 统一输出

- `findings/`
- `notes/`
- `artifacts/`

## 协作接口

每个 worker 都需要回答：

1. 我负责哪一部分
2. 我的输入是什么
3. 我的输出如何被其他 worker 消费
4. 我失败时要保留哪些半成品


# Task-01-Example.md

## 角色

这里写 Task 1 的角色。

## 任务目标

这里写 Task 1 的明确目标。

## 输入

- 这里写 Task 1 依赖的输入

## 工作步骤

1. 步骤一
2. 步骤二
3. 步骤三

## 输出

1. `findings/task_01.json`
2. `notes/task_01.md`
3. `artifacts/task_01/`

## 对其他任务的接口

- 这里写这个任务的结果怎么交给其他任务使用


# Task-02-Example.md

## 角色

这里写 Task 2 的角色。

## 任务目标

这里写 Task 2 的明确目标。

## 输入

- 这里写 Task 2 依赖的输入

## 工作步骤

1. 步骤一
2. 步骤二
3. 步骤三

## 输出

1. `findings/task_02.json`
2. `notes/task_02.md`
3. `artifacts/task_02/`

## 对其他任务的接口

- 这里写这个任务的结果怎么交给其他任务使用


# Task-03-Example.md

## 角色

这里继续追加更多 `# Task-xx-*.md` 分段。`mutiwork` 会把每个 `Task-*.md` 分段拆成一个独立 job，并复用同一份 `Agent.md`。
