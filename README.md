# vibe-coding (MVP)

本仓库提供一个可本地运行的 “vibe coding / 多代理编排” CLI：`vibe`。

## 提供的能力（MVP）

- `.vibe/ledger.jsonl`：事件账本（JSONL 追加写）
- `.vibe/artifacts/`：工件库（内容寻址 sha256 去重）
- `.vibe/checkpoints.json`：检查点（green/restore_steps 等）
- `.vibe/branches/<branch_id>/ledger.jsonl`：分支 ledger stream
- `.vibe/views/<agent_id>/`：每个工种的独立记忆域（结构化文件）
- `.vibe/refstore.sqlite`：轻量 Reference Store（SQLite）

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
- `vibe run [--task <event_id>] [--mock]`（mock 下闭环并产出 green checkpoint）
- `vibe checkpoint list/create/restore`
- `vibe branch create --from <checkpoint_id> [--name <git_branch>]`

## 权限模式（允许 / 每次提示 / 仅聊天）

你可以用三种权限模式控制本地工具（`run_cmd/git/read_file/write_file/search`）：

- `allow_all`：全部允许（默认）
- `prompt`：每次工具动作都会提示是否允许
- `chat_only`：禁止本地工具动作（不会跑命令/改代码）

设置方式三选一（优先级：命令行 > 环境变量 > 配置文件）：

- 命令行：`vibe --policy prompt run`
- 环境变量：`VIBE_POLICY_MODE=prompt`
- 配置文件：`.vibe/vibe.yaml` -> `policy.mode`

## VS Code 扩展（最小 IDE 封装）

扩展源码在 `vscode-vibe/`，提供 Dashboard + 一组 `Vibe:` 命令（调用本机 `vibe` CLI）。

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
