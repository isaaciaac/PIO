# vibe-coding (MVP)

本仓库提供一个可本地运行的 “vibe coding / 多代理编排” CLI：`vibe`。

## 提供的能力（MVP）

- `.vibe/ledger.jsonl`：事件账本（JSONL 追加写）
- `.vibe/artifacts/`：工件库（内容寻址 sha256 去重）
- `.vibe/checkpoints.json`：检查点（green/restore_steps 等）
- `.vibe/branches/<branch_id>/ledger.jsonl`：分支 ledger stream
- `.vibe/views/<agent_id>/`：每个工种的独立记忆域（结构化文件）
- `.vibe/refstore.sqlite`：轻量 Reference Store（SQLite）

## 上下文压缩（长对话防爆）

当你在 VS Code 侧边栏或命令行反复 `vibe chat`，历史对话可能会逐渐变长。当前实现提供一个**简单但可审计**的压缩策略：

- 触发：当“将要发送给模型的对话尾部 + 本轮用户消息”接近预算（按字符数估算）
- 动作：把较早的对话归档到 `.vibe/artifacts/sha256/...*.chat.txt`（可按块拆分），并把结构化摘要写入 `.vibe/views/<agent_id>/memory.jsonl`
- 保留：chat 里会插入一条 `system` 摘要（含 pointers + pinned 要点），并仅保留最后 N 条消息

你可以在 `.vibe/vibe.yaml` 里按 agent 覆盖默认预算：

```yaml
context:
  defaults:
    max_chars: 16000
    compress_trigger_ratio: 0.85
    keep_last_messages: 16
    keep_last_digests: 3
    pinned_max_items: 8
    archive_chunk_chars: 20000
  agents:
    pm:
      max_chars: 24000
```

## 安装

```bash
pip install -e .
```

## 自检

```bash
pytest -q
```

## 快速开始（mock 模式）

```bash
set VIBE_MOCK_MODE=1
vibe init
vibe task add "hello"
vibe run
vibe checkpoint list
```

## 从 0 开始搭建（空目录）

在一个空目录里（可以没有 git）：

```bash
vibe init
vibe task add "从 0 创建一个最小可运行的 Python 项目：包含 main.py、README、unittest 测试"
vibe run
```

如果你还没配置 DeepSeek/DashScope key，可以用 mock 先验证“会写文件、能闭环”的能力：

```bash
vibe init
vibe task add "从 0 创建一个最小 Python 项目"
vibe run --mock --mock-writes
```

## CLI 一览

- `vibe init`
- `vibe config show`
- `vibe task add "..."`（写入 `REQ_CREATED` 到 ledger）
- `vibe hint add "..."`（写入 `USER_HINT_ADDED`；下一次 `vibe run` 会注入到 ContextPacket.constraints）
- `vibe chat "..."`（PM 自然语言对话；可用 `--json` 输出结构化结果）
- `vibe run [--task <event_id>] [--mock] [--route auto|L0|L1|L2|L3|L4]`（mock 下闭环并产出 checkpoint）
- `vibe checkpoint list/create/restore`
- `vibe branch create --from <checkpoint_id> [--name <git_branch>]`

## 路由等级（L0–L4）

`vibe run` 支持按风险/范围选择不同的**最小门禁档位**：

- `--route auto`（默认）：由 `RouteDecider` 硬逻辑选择（低风险默认走 `L1`）
- `--route L0`：快速（草稿验证；仅 smoke；检查点一定是 `green=false` / draft）
- `--route L1`：简单 MVP（默认；PM→Router→Coder→QA(unit+lint)；通过才允许 `green=true`；如未检测到可跑命令，会按需调用 `env_engineer` 生成最小可运行命令并继续）
- `--route L2`：多模块 MVP（跨模块/契约/鉴权等风险域；加 ADR-lite/契约确认/代码审查；QA 升级为 full/integration；通过才允许 `green=true`）
- `--route L3`：可发布（交付/可复现；加 env/security/doc/release 门禁）
- `--route L4`：生产级（高风险；含 perf/compliance/runbook/迁移回滚等门禁）

说明：门禁是硬约束；但**启用哪些 agent 是按需的**（到某个 gate 才激活该 gate 需要的 agent，并写入 `AGENTS_ACTIVATED` 事件，便于审计与回放）。
没有密钥时可用 `--mock` 先跑通闭环。

## UserHint（用户提示管道）

当系统卡住时，你可以把“你希望它遵守的规则/修复方向”写入 ledger，下一次 `vibe run` 会把它注入 `ContextPacket.constraints`，从而影响 PM/Router/Coder 的决策：

```bash
vibe hint add "不要引入不存在的库；优先用仓库已有 scripts；必要时补齐依赖声明。"
vibe run
```

> 说明：hint 会被作为“高优先级约束”注入（并用 artifacts 指针留痕），不会替代 repo facts；repo 事实仍以 pointers/artifacts/git 为准。

## Incident 胶囊（失败诊断）

当 QA/build/lint/test 失败进入 fix-loop 时，编排器会生成一个**确定性的 IncidentPack**（结构化诊断胶囊），并写入：

- ledger 事件：`INCIDENT_CREATED`
- 工件：`.vibe/artifacts/sha256/...*.incident.json`

IncidentPack 会把噪声日志收敛成“可执行简报”（category/summary/evidence/next_steps/required_capabilities），并注入下一轮修复提示，减少反复猜错。

## 权限模式（允许 / 每次提示 / 仅聊天）

你可以用三种权限模式控制本地工具（`run_cmd/git/read_file/write_file/search`）：

- `allow_all`：全部允许（默认）
- `prompt`：每次工具动作都会提示是否允许
- `chat_only`：禁止本地工具动作（不会跑命令/改代码）

设置方式三选一（优先级：命令行 > 环境变量 > 配置文件）：

- 命令行：`vibe --policy prompt run`
- 环境变量：`VIBE_POLICY_MODE=prompt`
- 配置文件：`.vibe/vibe.yaml` -> `policy.mode`

## “自由发挥 / 细致程度”调节

你可以控制代理是“少追问、先给方案”，还是“更严谨、更细化验收”。

- 配置文件：`.vibe/vibe.yaml` -> `behavior.style`（`free|balanced|detailed`）
- 命令行覆盖：
  - `vibe chat "..." --style free`
  - `vibe run --style detailed`
- VS Code 扩展：侧边栏有 `风格` 下拉框，会把选择作为 `--style` 传给 CLI

## 默认模型分工（建议）

- **DeepSeek `deepseek-reasoner`**：需求/架构/审计/安全/合规/性能/契约确认（更偏推理、把关）
- **DashScope `qwen-plus/qwen-flash`**：路由编排、日志压缩、文案/文档整理（更偏通用与速度）
- **DashScope `qwen3-coder-*`**：所有“写代码/写脚本/写 CI”的工种（更偏代码生成与修复）

## VS Code 扩展（最小 IDE 封装）

扩展源码在 `vscode-vibe/`，提供 Dashboard + 一组 `Vibe:` 命令（调用本机 `vibe` CLI）。
扩展支持把 DeepSeek/DashScope key 存到 VS Code SecretStorage，并在运行时自动注入给 CLI（详见 `vscode-vibe/README.md`）。

```bash
cd vscode-vibe
npm install
npm run compile
```

然后在 VS Code 中打开仓库，按 `F5` 启动 Extension Development Host。

## 国内模型接入（可选）

- DeepSeek：设置 `DEEPSEEK_API_KEY`
- DashScope：设置 `DASHSCOPE_API_KEY`

默认使用 OpenAI-compatible 接口（`base_url` 在 `.vibe/vibe.yaml` 中可见）。
