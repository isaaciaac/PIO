# Vibe 维护手册（当前状态与排错准则）

> 这份文档回答两个问题：
>
> 1. 当前系统现在到底稳定到什么程度  
> 2. 出错时应该优先看哪里，按什么顺序判断

## 1. 当前情况总结

当前仓库已经不是“单个 CLI + 几个 prompt”。

它已经有这些稳定能力：

- `vibe init / task / run / checkpoint / branch`
- VS Code 侧边栏聊天与工作流触发
- 多路线 `L0-L4`
- `implementation_lead` 蓝图与修复工单
- `env_engineer / ops_engineer / qa / architect` 分层参与
- `ledger + artifacts + checkpoints + views + knowledge`
- 基于真实命令输出的 QA / fix-loop
- **PlanTask 分段验证**（每个任务落盘后先 smoke/unit，降低“最后一次性爆炸”概率）
- **自动 replan 续跑**（出现 `replan_required` 时自动创建 checkpoint 并继续，不要求用户手动 rerun）

但它的主要问题已经变成**系统性维护问题**，不是单点功能缺失：

- `orchestrator.py` 过大，职责过多
- 诊断和修复虽然成型，但还不够“像工程师”
- 对 contract/接口/数据形状类错误，仍然会有误判到 `env_engineer` 的情况
- `scope mismatch` 已引入 repair arena，并新增“合成工单”避免无效回退；但仍需要继续观察是否还有边界条件导致空转
  - 可通过 `.vibe/vibe.yaml -> behavior.plan_task_verify_profile` 调整分段验证力度
  - 可通过 `.vibe/vibe.yaml -> behavior.auto_replan_continue / max_replans_per_run` 调整 replan 续跑策略

## 2. 当前最重要的经验结论

### 2.1 不要先相信 PM 总结

先看：

- artifact 里的 stdout/stderr
- repo 实际文件
- ledger 事件链

最后才看 PM 的自然语言。

### 2.2 现在最容易误判的不是环境，而是契约

典型误判模式：

- 实际是导入路径错误 / 包骨架错误 / 异常体系分叉 / 数据结构不一致
- 系统却进入 `env remediation`
- 然后反复执行 `pip install` / `npm install`

如果出现“安装成功但失败不变”，优先按**契约错误**处理。

### 2.3 真正有效的修复单位不是“单个报错”，而是“主根因”

例如一轮测试可能报 8 个错，但通常能压成：

- 一个数据结构契约问题
- 一个异常体系问题
- 一个引擎接口问题
- 一个导入/导出问题

不做归并，fix-loop 就会看起来像在乱改。

## 3. 当前推荐的排错顺序

### 第一步：先判断失败属于哪一层

- **环境层**
  - 缺依赖
  - 缺 CLI
  - PATH / lockfile / site-packages / node_modules 问题
- **骨架层**
  - 入口文件不存在
  - 包路径与模块名冲突
  - `__init__.py` 导出错误
- **契约层**
  - 测试期望和实现接口不一致
  - 异常类型分叉
  - 数据结构不一致
  - 调用签名不一致
- **业务逻辑层**
  - 判定逻辑错
  - 断言内容错

### 第二步：看 ledger 事件链

优先看这些事件：

- `TEST_RUN`
- `TEST_FAILED`
- `INCIDENT_CREATED`
- `LEAD_BLUEPRINT_BUILT`
- `STATE_TRANSITION`
- `ARCH_UPDATED`

关键判断：

- 如果 `TEST_FAILED` 后立刻 `env remediation`，要确认是不是**误判**
- 如果 `scope mismatch` 反复出现，说明问题不是 coder 能单靠当前范围解决
- 如果 architect 被频繁叫起，多半说明升级太晚或分类太粗

### 第三步：最小复现

不要直接看全量日志。先跑最小失败集：

- 单个失败测试
- `pytest --collect-only`
- `python -m compileall .`
- 必要时只导入某个模块

### 第四步：做契约核对

优先核对这五类：

- 测试期望的 symbol / return shape
- 实现里是否真的存在这些 symbol
- JSON / YAML / 样本数据的实际结构
- 异常类是否统一来源
- 调用签名和函数定义是否一致

## 4. `D:\R\testproject` 这类失败说明了什么

最近真实项目 `D:\R\testproject` 暴露出的不是“某个包没装”，而是：

- `PolicyRule.conditions` 的数据形状与测试数据不一致
- `query_parser.ValidationError` 与 `error_handler.ValidationError` 分叉
- `FileNotFoundError` 被别名覆盖，异常捕获失效
- `rule_engine` 和 `models` 的接口不一致

这类失败说明：

- 当前系统已经能抓到不少线索
- 但还需要更强的 **contract audit / consistency audit**
- 也说明后续架构优化应该优先服务“诊断能力”，而不是继续堆角色

## 5. 当前模块职责边界

### `router`

- 负责总调度、gate、checkpoint、resume/replan
- 不应亲自诊断细节根因

### `implementation_lead`

- 负责蓝图、工单、scope、owner 分派
- 应该越来越像“战术指挥”，而不是纯顾问

### `env_engineer`

- 负责依赖、命令、运行环境、构建环境
- 不应吞掉契约类错误

### `ops_engineer`

- 负责复现、抓证据、压缩 incident
- 不应代替 coder 修业务代码

### `architect`

- 只处理设计级 blocker
- 不应处理“范围太小”或“依赖没装”

### `coder_* / integration_engineer`

- 负责落地代码
- 不应在没有 `ErrorObject` / 没有明确 scope 时硬修

## 6. 当前重构方向

### 已开始

- `vibe/orchestration/shared.py`
- `vibe/orchestration/contracts.py`

### 接下来最值钱的方向

- 把诊断相关逻辑继续从 `orchestrator.py` 拆到：
  - `diagnostics.py`
  - `fixloop.py`
  - `planning.py`
- 把“看报错打补丁”升级成：
  - `Observer`
  - `Diagnoser`
  - `Fixer`
  - `Verifier`
 这一条稳定闭环

## 7. 当前维护建议

如果你要继续改系统，而不是改单个生成项目，建议顺序是：

1. 先改诊断
2. 再改路由
3. 再改 scope 策略
4. 最后才改具体 prompt

不要反过来。

因为现在大多数“不聪明”的表现，根因不在 prompt，而在：

- 没看清结构
- 没归并根因
- 路由错层
- scope 放错

## 8. 后续维护目标

理想状态不是“知识库里什么坑都写满”，而是系统具备这三个通用能力：

- **看清结构**
  - 包、模块、导出、签名、数据结构、测试期望
- **归并根因**
  - 多个错误压成少数主根因
- **按层修复**
  - 环境问题给 env
  - 契约问题给 coder/integration
  - 设计问题才给 architect

这才是后续演进方向。
