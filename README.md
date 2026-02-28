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

## CLI 一览

- `vibe init`
- `vibe config show`
- `vibe task add "..."`（写入 `REQ_CREATED` 到 ledger）
- `vibe chat "..."`（PM 自然语言对话；可用 `--json` 输出结构化结果）
- `vibe run [--task <event_id>] [--mock] [--route auto|L0|L1|L2|L3|L4]`（mock 下闭环并产出 checkpoint）
- `vibe checkpoint list/create/restore`
- `vibe branch create --from <checkpoint_id> [--name <git_branch>]`

## 路由等级（L0–L4）

`vibe run` 支持按风险/范围选择不同的门禁等级：

- `--route auto`（默认）：由 `RouteDecider` 硬逻辑选择（低风险默认走 `L1`）
- `--route L0`：极速路径（仅 smoke 验证；检查点一定是 `green=false` / draft）
- `--route L1`：标准路径（PM→Router→Coder→QA；通过才允许 `green=true`）

> 说明：`L2+` 的门禁流程会在后续 Phase 补齐；当前选择 `L2/L3/L4` 会报 “not implemented yet”。

## 权限模式（允许 / 每次提示 / 仅聊天）

你可以用三种权限模式控制本地工具（`run_cmd/git/read_file/write_file/search`）：

- `allow_all`：全部允许（默认）
- `prompt`：每次工具动作都会提示是否允许
- `chat_only`：禁止本地工具动作（不会跑命令/改代码）

设置方式三选一（优先级：命令行 > 环境变量 > 配置文件）：

- 命令行：`vibe --policy prompt run`
- 环境变量：`VIBE_POLICY_MODE=prompt`
- 配置文件：`.vibe/vibe.yaml` -> `policy.mode`

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
