# Vibe Coding (VS Code Extension)

本目录提供一个最小 VS Code 扩展，用于在 IDE 内调用 `vibe` CLI。

## 开发运行

1. 安装依赖

```bash
cd vscode-vibe
npm install
```

2. 编译

```bash
npm run compile
```

3. 在 VS Code 中打开本仓库，进入 `vscode-vibe/`，按 `F5` 运行 Extension Development Host。

## 使用

- Command Palette 中搜索 `Vibe:` 命令
- Explorer 面板下会出现 `Vibe` 视图（Dashboard）

> 需要先在系统环境中可执行 `vibe`（例如 `pip install -e .`），或在 VS Code Settings 中设置 `vibe.cliPath`。

