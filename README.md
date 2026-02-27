# vibe-coding (MVP)

本仓库提供一个可本地运行的 “vibe coding / 多代理编排” CLI：`vibe`。

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

## 国内模型接入（可选）

- DeepSeek：设置 `DEEPSEEK_API_KEY`
- DashScope：设置 `DASHSCOPE_API_KEY`

默认使用 OpenAI-compatible 接口（`base_url` 在 `.vibe/vibe.yaml` 中可见）。

