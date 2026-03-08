# Vibe Prompt 索引（维护版）

> 这份文档只回答一个问题：**“某个 agent / 某个行为的实际 prompt 在哪里？”**

---

## 1. 先说最重要的事实

### 1.1 `config.py` 不是主要 prompt 来源

虽然 `vibe/config.py:40` 有 `prompt_template`，但当前系统里大多数 agent 的 `prompt_template` 还是空的。

**真实生效的 prompt 主要在：**

- `vibe/orchestrator.py`
- `vibe/cli.py`
- `vibe/providers/base.py`
- `vibe/style.py`

### 1.2 prompt 的拼接方式

大多数 runtime prompt 都是下面这个模式：

- 固定 system prompt
- 追加 `workflow_hint`
- 拼接 ContextPacket / RequirementPack / TestReport / Incident / Blueprint
- 再通过 `_messages_with_memory(...)` 加入 agent 自己的 memory / lessons

相关入口：

- `vibe/orchestrator.py:999` `_messages_with_memory`
- `vibe/style.py:75` `style_workflow_hint`

---

## 2. Prompt 文件分布

### 2.1 编排器里的 prompt

主文件：

- `vibe/orchestrator.py`

这类 prompt 决定：

- agent 在 workflow 里的职责
- 输出 schema
- 当前 gate 的硬约束
- 是否允许自动推进

### 2.2 CLI / chat 里的 prompt

主文件：

- `vibe/cli.py`

这类 prompt 决定：

- 侧边栏 chat 时 PM/角色如何说话
- chat 压缩器如何总结
- 图片/OCR 如何解释

### 2.3 provider repair prompt

主文件：

- `vibe/providers/base.py`

这类 prompt 不对业务负责，只对：

- JSON 修复
- 结构化输出纠偏

---

## 3. Orchestrator Prompt 索引

下面按阶段列。

---

## 4. 需求与前置分析阶段

### 4.1 PM（RequirementPack）

- 位置：`vibe/orchestrator.py:5240`
- 触发：`gate:requirements`
- 输出：`RequirementPack`
- 用途：
  - 生成需求摘要
  - 生成 AC / constraints / non-goals
  - 为后续路线和计划提供正式输入

### 4.2 Intent Expander

- 位置：`vibe/orchestrator.py:5300`
- 触发：`gate:intent_expansion`
- 输出：`IntentExpansionPack`
- 用途：
  - 在用户没说全时，把“常见产品应该具备的东西”向前补充
  - 受 route/style 影响

### 4.3 Requirements Analyst

- 位置：`vibe/orchestrator.py:5363`
- 触发：`gate:usecases`
- 输出：`UseCasePack`
- 用途：
  - 边界条件
  - 错误场景
  - 正/反向用例

### 4.4 Architect（DecisionPack）

- 位置：`vibe/orchestrator.py:5397`
- 触发：`gate:architecture`
- 输出：`DecisionPack`
- 用途：
  - ADR-lite
  - 模块边界
  - 设计选择

### 4.5 Web Info

- 位置：`vibe/orchestrator.py:5477`
- 触发：`gate:web_info`
- 输出：`WebInfoPack`
- 用途：
  - 联网查证外部事实
  - 只做 fact-check，不做主实现

### 4.6 API Confirm

- 位置：`vibe/orchestrator.py:5526`
- 触发：`gate:contract`
- 输出：`ContractPack`
- 用途：
  - 合同/Schema/OpenAPI/错误码一致性

### 4.7 Router Plan

- 位置：`vibe/orchestrator.py:5566`
- 触发：计划阶段
- 输出：`Plan`
- 用途：
  - 把需求和架构翻成 plan tasks
  - 控制任务数与顺序

---

## 5. 实现前控制层 Prompt

### 5.1 Implementation Lead（初始蓝图）

- 位置：`vibe/orchestrator.py:5771`
- 触发：`gate:blueprint`
- 输出：`ImplementationBlueprint`
- 用途：
  - 把架构/计划翻译成文件级实现蓝图
  - 给每个 plan task 指定 scope / invariants / verification
  - 推荐 fix agent

### 5.2 Implementation Lead（fix-loop 蓝图刷新）

- 位置：`vibe/orchestrator.py:5857`
- 触发：`gate:blueprint_fix`
- 输出：`ImplementationBlueprint`
- 用途：
  - 当修复不收敛时，重算 fix scope / consult_agents / escalation_reason

### 5.3 Lead Consult

- 位置：`vibe/orchestrator.py:5958`
- 触发：`gate:lead_consult`
- 输出：`ChatReply`
- 用途：
  - implementation_lead 拉其他 agent 做会诊
  - 不是主产物，而是 advisory input

---

## 6. Replan 阶段 Prompt

### 6.1 Architect Replan

- 位置：`vibe/orchestrator.py:6118`
- 触发：`gate:replan`
- 输出：`DecisionPack`
- 用途：
  - 当前修复路径被证据证明不收敛时，刷新架构决策

### 6.2 API Confirm Replan

- 位置：`vibe/orchestrator.py:6180`
- 触发：`gate:replan_contract`
- 输出：`ContractPack`
- 用途：
  - blocker 指向契约失效时，刷新 contract

### 6.3 Router Replan

- 位置：`vibe/orchestrator.py:6234`
- 触发：replan 后重新规划
- 输出：`Plan`
- 用途：
  - 重新生成新计划，替换原先不收敛的任务路线

---

## 7. 实现阶段 Prompt

### 7.1 主实现 Prompt（plan task coder）

- 位置：`vibe/orchestrator.py:6326`
- 触发：`plan_task:*`
- 输出：`CodeChange`
- 用途：
  - 真正写代码
  - 受 `ImplementationBlueprint` + `workflow_hint` + task scope 约束

### 7.2 bootstrap coder

- 位置：`vibe/orchestrator.py:6701`
- 触发：`gate:bootstrap`
- 输出：`CodeChange`
- 用途：
  - 项目骨架 / 配置 / README / 初始可运行结构

---

## 8. 环境 / QA / 风险阶段 Prompt

### 8.1 Env Engineer

- 位置：`vibe/orchestrator.py:6598`
- 触发：`gate:envspec`
- 输出：`EnvSpec`
- 用途：
  - 提供 build/test/run 命令集
  - 当前还会在 fix-loop 中通过命令执行参与环境修复

### 8.2 Code Reviewer

- 位置：`vibe/orchestrator.py:6949`
- 触发：`gate:review`
- 输出：`ReviewReport`
- 用途：
  - 严格识别 blocker
  - correctness / breaking changes / security / data loss

### 8.3 Security

- 位置：`vibe/orchestrator.py:7006`
- 触发：`gate:security`
- 输出：`RiskRegister`

### 8.4 Compliance

- 位置：`vibe/orchestrator.py:7095`
- 触发：`gate:compliance`
- 输出：`ComplianceReport`

### 8.5 Performance

- 位置：`vibe/orchestrator.py:7135`
- 触发：`gate:performance`
- 输出：`PerfReport`

### 8.6 Ops Engineer / Triage

- 位置：`vibe/orchestrator.py:7582`
- 触发：`gate:triage`
- 输出：`FixPlanPack`
- 用途：
  - reproduce
  - root cause summary
  - proposed fixes
  - files_to_check

### 8.7 On-demand Specialist

- 位置：`vibe/orchestrator.py:7827`
- 触发：`gate:specialist`
- 输出：`ChatReply`
- 用途：
  - 当系统能力池缺口明显时，给可执行的补充建议

---

## 9. fix-loop Prompt 索引

### 9.1 Fixer 主 Prompt

- 位置：`vibe/orchestrator.py:7901`
- 触发：`gate:fix_loop`
- 输出：`CodeChange`

关键规则已经写进 prompt：

- 必须基于 `ErrorObject`
- 一次只修一个主根因
- 不允许批量猜多个假设
- 不允许架构大改
- Python 缺三方包时优先改 manifests 而不是改业务代码

### 9.2 CodeChange materialize repair prompt

- 位置：`vibe/orchestrator.py:4432`
- 触发：模型给的 `CodeChange` 不能落地时
- 输出：修正后的 `CodeChange`
- 用途：
  - 修路径
  - 修缺文件
  - 修依赖声明
  - 修写入内部目录等问题

这不是业务修复 prompt，而是**落地修复 prompt**。

---

## 10. 交付与收尾阶段 Prompt

### 10.1 Doc Writer

- 位置：`vibe/orchestrator.py:8443`
- 触发：`gate:docs`
- 输出：`DocPack`

### 10.2 Release Manager

- 位置：`vibe/orchestrator.py:8478`
- 触发：`gate:release_notes`
- 输出：`ReleasePack`

### 10.3 DevOps

- 位置：`vibe/orchestrator.py:8518`
- 触发：`gate:ci`
- 输出：`CIPack`

### 10.4 Support Engineer

- 位置：`vibe/orchestrator.py:8552`
- 触发：`gate:runbook`
- 输出：`RunbookPack`

### 10.5 Data Engineer

- 位置：`vibe/orchestrator.py:8585`
- 触发：`gate:migration`
- 输出：`MigrationPlan`

---

## 11. CLI / Chat Prompt 索引

### 11.1 Chat Digest / 压缩器

- 位置：`vibe/cli.py:766`
- 输出：`ChatDigest`
- 用途：
  - 长对话压缩
  - 归档旧消息
  - 保留 pinned facts

### 11.2 普通 chat 角色 Prompt

- 位置：`vibe/cli.py:894`
- 输出：`ChatReply`
- 用途：
  - UI 里和 PM / 架构 / 安全 / 工程等角色对话
  - 是“只聊天”的主要 prompt

### 11.3 Vision / OCR Prompt

- 位置：`vibe/cli.py:1097`
- 输出：`VisionReport`
- 用途：
  - 粘贴图片后做 OCR / 视觉分析

---

## 12. Provider 层 Prompt

### 12.1 JSON Repair

- 位置：`vibe/providers/base.py:559`
- 用途：
  - 当模型输出不符合 schema 时，让模型重吐 JSON
  - 这是结构层纠偏，不是业务逻辑层 prompt

### 12.2 DeepSeek 消息格式修正

- 位置：`vibe/providers/base.py:598`
- 用途：
  - 兼容 `deepseek-reasoner` 首条非 system 必须是 user 的格式要求

---

## 13. Style Prompt

### 13.1 风格输入

- 位置：`vibe/style.py:53`
- `style_prompt(style)`
- `vibe/style.py:75`
- `style_workflow_hint(style)`

用途：

- `free`
- `balanced`
- `detailed`

这些不是独立 agent prompt，而是**拼接到多个 orchestrator / cli prompt 里的附加提示**。

---

## 14. Prompt Review 时怎么查

### 14.1 想查某个角色到底怎么被要求输出

先看：

1. 它的 schema：`vibe/schemas/packs.py`
2. 它的 system prompt：本文件对应位置
3. 它被拼进去的数据：`orchestrator.py` 同段的 `user=` 文本
4. 它的风格附加：`vibe/style.py:75`

### 14.2 想查“为什么它乱说”

优先检查：

- 它是 chat prompt 还是 workflow prompt
- 它是否拿到真实 artifact / repo excerpt
- 它是不是只看了 README / manifest
- 它是不是没有拿到 `ErrorObject`

### 14.3 想查“为什么它已经说要做，但没做”

优先检查：

- 在 `cli.py` 的 chat 分支里，它到底走的是 `chat` 还是 `task add + run`
- 在 `orchestrator.py` 里，它是只生成了 Pack，还是已经 materialize 了 `CodeChange`

---

## 15. 当前 Prompt 设计上的已知问题

这部分是给维护时用的，不粉饰。

### 15.1 prompt 过于分散

问题：

- 主要 prompt 不在 config，而在 orchestrator/cli
- review 时不容易一眼全看见

### 15.2 orchestrator prompt 和系统状态强耦合

问题：

- prompt 不是静态文本，而是拼接很多上下文
- review 某个 system prompt 时，必须一起看它拼了什么 user/context

### 15.3 fix-loop prompt 仍然太重

问题：

- 尽管已经加了 `ErrorObject`
- 但 playbook/template 还没全部静态化

---

## 16. 建议的后续治理方向

### 16.1 抽离 prompt 注册表

理想形态：

- 把 orchestrator/cli 里的 system prompt 抽到 `vibe/prompts/*.py`
- 每个角色/阶段一个函数
- orchestrator 只负责拼接上下文，不负责藏 prompt 文案

### 16.2 给 prompt 加测试

至少测试：

- 关键 prompt 是否包含 schema 约束
- fix-loop 是否总带 `ErrorObject`
- env blocker 是否能触发 env engineer 路由

### 16.3 prompt 与 schema 一起维护

建议规则：

- 改 schema 时，同时 review prompt
- 改 prompt 输出字段时，同时补测试

