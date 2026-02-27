# Vibe Coding (VS Code Extension)

本目录提供一个最小 VS Code 扩展，用于在 IDE 内调用本机的 `vibe` CLI（本仓库的多代理编排/工作流工具）。

## 你将得到什么

- Explorer 面板的 `Vibe` 视图（Dashboard）：一键 `init / add task / run / run mock / 打开配置与 ledger / checkpoint list`
- Command Palette 的 `Vibe:` 命令：和 Dashboard 同功能
- 输出统一写到 `Output -> Vibe`，便于复制/排障

## 前置条件

1. VS Code
2. Node.js（用于编译扩展）
3. Python（用于安装/运行 `vibe` CLI）
4. （可选）git：需要用到 `Vibe: Branch Create From Checkpoint` 时必须有 git 仓库

## 第一步：确保 `vibe` CLI 可用

扩展本质上是“调用 CLI”，所以必须先让 VS Code 里能直接执行 `vibe`。

在本仓库根目录执行：

```bash
pip install -e .
vibe --help
```

如果你用虚拟环境，请确保你启动 VS Code 的那个终端/环境里 `vibe` 在 `PATH` 中。

### 如果 VS Code 找不到 `vibe`

报错通常类似：`Cannot find vibe CLI ... set VS Code setting 'vibe.cliPath'`

解决办法二选一：

1. 从已激活 venv 的终端启动 VS Code（让 PATH 继承 venv Scripts/bin）
2. 打开 VS Code Settings，搜索 `Vibe: Cli Path`，填入 `vibe` 的绝对路径（Windows 常见是 `<python>/Scripts/vibe.exe`）

## 第二步：编译并运行扩展（开发态）

1. 安装依赖

```bash
cd vscode-vibe
npm install
```

2. 编译

```bash
npm run compile
```

3. 在 VS Code 中打开本仓库，按 `F5` 运行 Extension Development Host（会打开一个新的 VS Code 窗口）。

> 你也可以只打开 `vscode-vibe/` 目录再按 `F5`；但通常建议直接打开仓库根目录，便于扩展在真实工作区里跑。

## 使用

- 打开 Command Palette（`Ctrl+Shift+P`），搜索 `Vibe:` 命令
- 或在 Explorer 面板底部找到 `Vibe` 视图（Dashboard）直接点按钮

### 命令与行为对应

- `Vibe: Init`
  - 运行：`vibe init --path <workspace>`
  - 生成：`.vibe/`、`.vibe/vibe.yaml`、`.vibe/ledger.jsonl`、manifests、views、refstore 等
- `Vibe: Add Task`
  - 运行：`vibe task add "<text>" --path <workspace>`
  - 结果：向 ledger 追加 `REQ_CREATED` 事件
- `Vibe: Run`
  - 运行：`vibe run --path <workspace>`
  - 结果：执行 orchestrator 工作流；通过会创建 green checkpoint
- `Vibe: Run (Mock)`
  - 运行：`vibe run --mock --path <workspace>`
  - 结果：强制 `VIBE_MOCK_MODE=1`，无需 key 也能闭环产出 green checkpoint（适合先验收链路）
- `Vibe: Open Config (vibe.yaml)`
  - 打开：`.vibe/vibe.yaml`
- `Vibe: Open Ledger (ledger.jsonl)`
  - 打开：`.vibe/ledger.jsonl`
- `Vibe: Checkpoint List`
  - 运行：`vibe checkpoint list --path <workspace>`（输出在 Output -> Vibe）
- `Vibe: Checkpoint Restore`
  - 选择一个 checkpoint id 后执行：`vibe checkpoint restore <id> --path <workspace>`
- `Vibe: Branch Create From Checkpoint`
  - 选择一个 checkpoint id，可选输入分支名，然后执行：`vibe branch create --from <id> [--name <branch>] --path <workspace>`

### 输出与产物在哪里看

- VS Code：`View -> Output`，右上角下拉选择 `Vibe`
- 工作区文件：
  - `.vibe/ledger.jsonl`：每次运行的事件记录
  - `.vibe/checkpoints.json`：检查点
  - `.vibe/artifacts/sha256/...`：stdout/stderr/patch/报告等（内容寻址）

## 使用真实模型（DeepSeek / DashScope）

扩展不直接保存 key；仍由 `vibe` CLI 从环境变量读取：

- DeepSeek：`DEEPSEEK_API_KEY`
- DashScope：`DASHSCOPE_API_KEY`

建议在“启动 Extension Development Host 之前”的终端里设置环境变量，再启动 VS Code，这样扩展运行时能继承到。

