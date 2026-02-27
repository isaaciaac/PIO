# Vibe Coding (VS Code Extension)

本目录提供一个最小 VS Code 扩展，用于在 IDE 内调用本机的 `vibe` CLI（本仓库的多代理编排/工作流工具）。

## 你将得到什么

- 左侧 Activity Bar 的 `Vibe` 视图（侧边栏 Chat UI）：像“助手”一样对话（一个 `发送` 按钮 + 模式下拉框）
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

## 打包成 VSIX（离线安装）

本项目已在 `devDependencies` 中固定了可在 Node 18 下工作的 `vsce` 版本，避免你直接 `npx @vscode/vsce ...` 时出现 `ReferenceError: File is not defined`。

```bash
cd vscode-vibe
npm install
npm run compile
npm run package:vsix
```

会生成：`vscode-vibe/vibe-coding-<版本号>.vsix`（每次打包会自动把版本号 +1）

安装：

- VS Code → Extensions → `...` → `Install from VSIX...`
- 或命令行：`code --install-extension vibe-coding-<版本号>.vsix`

## 使用

- 打开 Command Palette（`Ctrl+Shift+P`），搜索 `Vibe:` 命令
- 或点击左侧 Activity Bar 的 `Vibe` 图标打开面板

### 侧边栏 Chat UI（推荐）

- 在输入框里输入内容
- 用下拉框选择模式（决定是否写项目 + 是否弹窗确认权限）：
  - `聊天（PM）`：自然语言对话（调用 `vibe chat`，不会运行工作流/改代码）
  - `确认权限（写项目）`：执行工作流（`task add + run`），每次工具动作都会弹窗询问是否允许
  - `完全授权（写项目）`：执行工作流（`task add + run`），不弹窗，全部允许
- 聊天模式下可选择“角色”，会将对话历史分别保存在 `.vibe/views/<agent_id>/chat.jsonl`
- 写项目模式下可选择“路由等级”（`auto/L0..L4`），会在运行时传给 `vibe run --route ...`
- 勾选 `Mock` 可无 key 运行闭环
- 点击 `发送`（或 `Ctrl+Enter` 发送）

当模式为 `确认权限（写项目）/ 完全授权（写项目）` 时，每次发送会执行：

1. 若未初始化则自动 `vibe init`
2. `vibe task add "<你的输入>"`
3. `vibe run --task <task_id>`（Mock 勾选时会追加 `--mock`）

当模式为 `聊天（PM）` 时，每次发送会执行：

1. 若未初始化则自动 `vibe init`
2. `vibe chat "<你的输入>" --json`（Mock 勾选时会追加 `--mock`）

聊天模式会显示 PM 的自然语言回复；写项目模式会显示 checkpoint id、green 状态，以及本次新增的 ledger events 摘要。

## 权限模式（像“工具审批”一样）

扩展支持三种权限模式（Settings -> 搜索 `Vibe: Permission Mode`）。  
说明：侧边栏面板的“模式下拉框”会在每次点击 `发送` 时**按当前选择覆盖**权限模式；而 Command Palette 的 `Vibe:` 命令仍使用该 Setting。

- `allow_all`：不提示，全部允许（默认建议）
- `prompt`：每次工具动作（如 `run_cmd/git/read_file/write_file/search`）都会弹窗询问是否允许
- `chat_only`：禁止本地工具动作（只做“聊天/产出结构化结果”，不会跑命令/改代码）
- `config`：不覆盖 CLI 参数，使用工作区 `.vibe/vibe.yaml` 里的 `policy.mode`

当选择 `prompt` 时，扩展会通过 `.vibe/approvals/` 与 CLI 交互：

- CLI 写入：`.vibe/approvals/requests/*.json`
- 扩展弹窗询问后写回：`.vibe/approvals/responses/*.json`

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

扩展支持把 key 存到 VS Code SecretStorage（推荐），并在每次调用 `vibe` CLI 时自动注入到子进程环境变量里；无需 PowerShell/终端里反复 `set`。

用 Command Palette（`Ctrl+Shift+P`）执行：

- `Vibe：设置 DeepSeek 密钥`
- `Vibe：设置 DashScope 密钥`
- `Vibe：查看密钥状态`
- `Vibe：清除已保存密钥`

注入的环境变量名仍是：

- DeepSeek：`DEEPSEEK_API_KEY`
- DashScope：`DASHSCOPE_API_KEY`

你也可以继续用系统环境变量方式设置它们（不通过扩展保存）。但注意：扩展进程只会继承“启动 VS Code 时”的环境变量；在集成终端里后设置的变量，通常不会自动同步到扩展进程。
