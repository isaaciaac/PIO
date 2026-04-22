# Vibe Coding

`Vibe Coding` 是一个本地优先的多代理软件交付 CLI。它不把模型当成“会自己完成一切的工程师”，而是把模型放进一个带事件账本、检查点、工件库、权限策略和 fix-loop 的运行时里。

这个仓库现在已经不只是一个聊天脚本，也不是单纯的“多 agent prompt chaining”。它的核心形态更接近：

- 一个命令行入口：`vibe`
- 一个编排器：负责 route、plan、实现、验证、修复、恢复
- 一组结构化对象：`RequirementPack`、`Plan`、`ImplementationBlueprint`、`CodeChange`、`IncidentPack`、`FixPlanPack`
- 一套工作区状态目录：`.vibe/`
- 一个可选的 VS Code 封装：`vscode-vibe/`

如果你是第一次接手这个项目，建议先看：

- [docs/system-map.md](/D:/R/HongyouCoding/docs/system-map.md)
- [docs/prompt-index.md](/D:/R/HongyouCoding/docs/prompt-index.md)
- [docs/maintenance-guide.md](/D:/R/HongyouCoding/docs/maintenance-guide.md)

## 项目现状

当前版本已经具备这些骨架能力：

- `Route -> Plan -> ImplementationBlueprint -> Execute -> Verify -> Fix-loop -> Checkpoint`
- `.vibe/ledger.jsonl` 事件账本
- `.vibe/artifacts/` 内容寻址工件库
- `.vibe/checkpoints.json` 检查点恢复
- per-agent memory / lessons
- CLI 和 VS Code 扩展两套入口

当前正在进行的治理重点：

- 拆分 [orchestrator.py](/D:/R/HongyouCoding/vibe/orchestrator.py)，让它更像调度器，而不是把所有规则层塞进 `run()`
- 把“计划任务”和“修复任务”统一成工单语义
- 让 fix-loop 更少靠临时补丁，多靠显式规则和结构化对象

## 设计取向

这个项目的设计目标不是“模拟一个会聊天的工程团队”，而是“把模型纳入一个可以审计、恢复、继续运行的工程运行时”。因此它的设计优先级大致是：

1. 真实副作用可追踪
2. 失败后可恢复
3. 模型行为有边界
4. 结构化对象比自然语言总结更可信
5. prompt 只是实现细节，运行时约束才是骨架

## 快速开始

### 1. 安装 CLI

推荐开发模式安装：

```bash
pip install -e .
```

`-e` 模式下修改 `vibe/` 里的 Python 代码后，不需要重新打包，重新运行命令即可生效。

### 2. 初始化工作区

```bash
vibe init
```

这会创建 `.vibe/` 目录和默认配置。

### 3. 添加任务

```bash
vibe task add "从 0 创建一个最小可运行的 Python 项目"
```

### 4. 运行工作流

```bash
vibe run --mock
```

如果你已经配置了模型密钥，也可以直接：

```bash
vibe run
```

### 5. 查看检查点

```bash
vibe checkpoint list
```

## Mock 模式

如果你只是想验证“工作流骨架能不能跑通”，推荐先用 mock：

```bash
set VIBE_MOCK_MODE=1
vibe init
vibe task add "hello"
vibe run
```

或者：

```bash
vibe run --mock --mock-writes
```

适合场景：

- 验证 `.vibe/` 工作区能否正常初始化
- 验证 CLI / checkpoint / ledger 是否通
- 做回归测试而不依赖真实模型

## 工作区目录

运行过程中，绝大多数“真相”都在 `.vibe/` 里，而不在聊天总结里。

关键目录和文件：

- `.vibe/ledger.jsonl`
  - 主事件账本
  - 记录 route、plan、写文件、测试失败、incident、checkpoint 等

- `.vibe/artifacts/`
  - 保存 stdout/stderr/json 报告/patch/chat archive/fix plan
  - 内容寻址，适合回放和审计

- `.vibe/checkpoints.json`
  - 记录恢复点
  - resume / restore / replan 都依赖它

- `.vibe/views/<agent_id>/`
  - agent 的 memory / digest / lessons

- `.vibe/branches/<branch_id>/`
  - 分支级 ledger stream

- `.vibe/refstore.sqlite`
  - 轻量 reference store

## 运行时主链路

一次 `vibe run` 的主过程大致是：

1. 找任务和 hints
2. 构造 `ContextPacket`
3. 决定 route level
4. 生成 requirement / intent / usecases / decision / contract / plan
5. 生成 `ImplementationBlueprint`
6. 把 `PlanTask` 转成执行工单并逐个落地
7. 运行 QA / verify
8. 若失败，生成 `IncidentPack` 并进入 fix-loop
9. 必要时 refresh blueprint / replan / restore
10. 产出 checkpoint

你可以把它理解成一个状态驱动的交付流程，而不是一次性的 prompt-response。

## 核心结构化对象

这些对象是整个系统的主语：

- `RequirementPack`
- `IntentExpansionPack`
- `Plan`
- `PlanTask`
- `ImplementationBlueprint`
- `ExecutionWorkOrder`
- `CodeChange`
- `TestReport`
- `IncidentPack`
- `FixPlanPack`

它们的定义主要在 [packs.py](/D:/R/HongyouCoding/vibe/schemas/packs.py) 以及最近新增的运行时工单层 [work_orders.py](/D:/R/HongyouCoding/vibe/orchestration/work_orders.py)。

### ExecutionWorkOrder

最近的重构重点之一，是把计划任务和 fix-loop 工单统一成运行时工单对象。现在系统不再只是“拿到任务就直接交给某个 agent”，而是先把任务翻译成带边界的工单，再让执行器消费它。

工单现在承载：

- `owner`
- `summary`
- `reason`
- `allowed_write_globs`
- `denied_write_globs`
- `commands`
- `verify_commands`
- `notes`
- `invariants`

这一步的目的，是把“哪些文件能改、为什么改、该怎么验证”从 prompt 文本里抬成正式结构。

## 路由等级

`vibe run` 支持 `L0` 到 `L4` 的 route level：

- `L0`
  - 快速草稿验证
  - 不追求 green

- `L1`
  - 简单 MVP
  - 默认档位

- `L2`
  - 多模块 / 契约 / 跨边界项目

- `L3`
  - 可发布交付
  - 引入更多工程 gate

- `L4`
  - 生产级
  - 包含 perf / compliance / rollback / runbook 等

命令行示例：

```bash
vibe run --route auto
vibe run --route L2
vibe run --route L3 --style detailed
```

## 权限模式

本地工具动作有 3 种权限模式：

- `allow_all`
- `prompt`
- `chat_only`

设置方式：

```bash
vibe --policy prompt run
```

或者：

```bash
set VIBE_POLICY_MODE=prompt
```

或者在 `.vibe/vibe.yaml` 中设置：

```yaml
policy:
  mode: prompt
```

## 风格模式

可通过 `behavior.style` 或命令行控制代理的“自由发挥程度”：

- `free`
- `balanced`
- `detailed`

示例：

```bash
vibe chat "帮我总结当前工程问题" --style free
vibe run --style detailed
```

## CLI 命令总览

常用命令：

- `vibe init`
- `vibe config show`
- `vibe task add "..." `
- `vibe hint add "..." `
- `vibe chat "..."`
- `vibe run`
- `vibe checkpoint list`
- `vibe checkpoint restore <id>`
- `vibe branch create --from <checkpoint_id>`

推荐工作流：

```bash
vibe init
vibe task add "实现一个最小可运行的 FastAPI 服务"
vibe run --route L2
vibe checkpoint list
```

## 用户 Hint 管道

当系统卡住时，可以通过 hint 注入高优先级约束：

```bash
vibe hint add "不要引入不存在的依赖；优先使用仓库已有脚本；必要时补齐 pyproject.toml。"
vibe run
```

它会影响后续 `ContextPacket.constraints`，但不会覆盖 repo 事实。

## Incident 与 Fix-loop

当验证失败时，系统会生成 `IncidentPack` 和相关 artifact，然后进入 fix-loop。这里的目标不是“再猜一次”，而是：

1. 把失败压缩成结构化诊断
2. 选一个主根因
3. 决定是否需要 `env_engineer / ops_engineer / implementation_lead / coder / integration_engineer`
4. 在尽量小的 write scope 内修复
5. 分层验证

fix-loop 的“工单化”和“委派命令执行”逻辑最近已经开始从 [orchestrator.py](/D:/R/HongyouCoding/vibe/orchestrator.py) 往 [fix_runtime.py](/D:/R/HongyouCoding/vibe/orchestration/fix_runtime.py) 抽离。

## 当前主要代码入口

如果你要读代码，建议顺序如下：

1. [vibe/cli.py](/D:/R/HongyouCoding/vibe/cli.py)
2. [vibe/orchestrator.py](/D:/R/HongyouCoding/vibe/orchestrator.py)
3. [vibe/orchestration/planning.py](/D:/R/HongyouCoding/vibe/orchestration/planning.py)
4. [vibe/orchestration/fix_runtime.py](/D:/R/HongyouCoding/vibe/orchestration/fix_runtime.py)
5. [vibe/orchestration/work_orders.py](/D:/R/HongyouCoding/vibe/orchestration/work_orders.py)
6. [vibe/routes.py](/D:/R/HongyouCoding/vibe/routes.py)
7. [vibe/policy.py](/D:/R/HongyouCoding/vibe/policy.py)
8. [vibe/storage/](/D:/R/HongyouCoding/vibe/storage)
9. [vibe/schemas/packs.py](/D:/R/HongyouCoding/vibe/schemas/packs.py)

## 最近的结构治理

为了让这个项目不再继续长成一整坨，最近做了两件基础治理：

### 1. 引入统一执行工单

新增：

- [vibe/orchestration/work_orders.py](/D:/R/HongyouCoding/vibe/orchestration/work_orders.py)

作用：

- 把 `PlanTask` 和 `FixWorkOrder` 统一成 `ExecutionWorkOrder`
- 让 write scope / commands / verification 从 prompt 里抬成正式运行时对象

### 2. 把 planning 和 fix runtime 规则层从 orchestrator 拔出来

新增：

- [vibe/orchestration/planning.py](/D:/R/HongyouCoding/vibe/orchestration/planning.py)
- [vibe/orchestration/fix_runtime.py](/D:/R/HongyouCoding/vibe/orchestration/fix_runtime.py)

作用：

- 让 `orchestrator` 更像调度器
- 让 blueprint 清洗、prompt 生成、fix-loop 委派执行变成独立规则层
- 为后续继续拆 `run()` 留出稳定边界

## 安装和打包

### 开发模式

推荐：

```bash
pip install -e .
```

好处：

- 修改 Python 代码后无需重新构建 wheel
- 适合日常开发和本地调试

### 重新安装 wheel

如果你不是 `-e` 安装，而是普通安装，则修改代码后需要重新安装：

```bash
python -m build
pip install dist/vibe_coding-*.whl --force-reinstall
```

### VS Code 扩展

如果你改了 `vscode-vibe/` 下的 TypeScript，需要重新编译：

```bash
cd vscode-vibe
npm install
npm run compile
```

如果你要生成 `.vsix`：

```bash
npm run package:vsix
```

## 配置模型

当前主要支持：

- DeepSeek
- DashScope
- OpenAI-compatible provider

常见环境变量：

- `DEEPSEEK_API_KEY`
- `DASHSCOPE_API_KEY`

provider/model 的默认配置见：

- [vibe/config.py](/D:/R/HongyouCoding/vibe/config.py)

## 测试

完整回归：

```bash
pytest -q
```

开发时建议先跑聚焦测试：

```bash
pytest -q tests/test_execution_work_orders.py
pytest -q tests/test_planning_runtime.py
pytest -q tests/test_fix_runtime.py
pytest -q tests/test_fix_loop_budget.py
pytest -q tests/test_resume.py
```

这些测试覆盖了最近的核心治理方向：

- 工单抽象
- planning runtime
- delegated fix runtime
- fix-loop 预算
- resume / checkpoint

## 仓库结构

```text
HongyouCoding/
├─ vibe/
│  ├─ cli.py
│  ├─ orchestrator.py
│  ├─ orchestration/
│  │  ├─ planning.py
│  │  ├─ fix_runtime.py
│  │  ├─ work_orders.py
│  │  ├─ contracts.py
│  │  └─ diagnostics.py
│  ├─ schemas/
│  ├─ storage/
│  ├─ providers/
│  ├─ tools/
│  └─ agents/
├─ tests/
├─ docs/
├─ vscode-vibe/
└─ README.md
```

## VS Code 扩展

扩展位于 [vscode-vibe/](/D:/R/HongyouCoding/vscode-vibe)。

它做的事情很克制：

- 提供 Dashboard / Webview
- 调用本地 `vibe` CLI
- 用 SecretStorage 存密钥
- 提供一些快捷命令

也就是说：**真正的核心逻辑在 Python CLI，不在扩展里。**

## 维护建议

如果你要继续改这个项目，建议遵守这几个原则：

- 先改结构化对象，再改 prompt
- 先补规则层，再补 UI 层
- 先缩小边界，再放大能力
- 先看 artifact 和 ledger，再看自然语言总结
- 先做聚焦测试，再做大范围重构

### 不建议的做法

- 看到一个现象就直接改 `orchestrator.py` 某一段 prompt
- 再继续往 `run()` 里塞一层判断
- 遇到 fix-loop 卡住就优先补新 agent
- 让模块既负责判断、又负责执行、又负责恢复、又负责写日志

### 更建议的做法

- 把规则层继续往 `vibe/orchestration/` 拆
- 让失败语义变成显式对象，而不是临时变量
- 用结构化测试保护边界
- 把 README / system-map / prompt-index 同步维护

## 已知局限

当前系统仍然存在这些现实问题：

- [orchestrator.py](/D:/R/HongyouCoding/vibe/orchestrator.py) 仍然过大
- fix-loop 还没完全模板化
- 仍有一部分逻辑过度依赖 prompt 收敛
- 文档和代码之间偶尔会有滞后
- 更复杂的 replan / restore / gate 拆分还没有彻底完成

## 路线建议

如果后面继续治理，我建议按这个顺序推进：

1. 继续拆 fix-loop 的 diagnose / repair / verify 子域
2. 给 `ExecutionWorkOrder` 和失败语义增加更多直接测试
3. 让 `orchestrator` 只保留编排主线
4. 逐步把关键 prompt 收敛成规则层 + 辅助提示

## License

Apache License 2.0. See [LICENSE](/D:/R/HongyouCoding/LICENSE).
