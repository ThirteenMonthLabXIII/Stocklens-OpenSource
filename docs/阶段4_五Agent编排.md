# 阶段 4：五 Agent 编排

阶段 4 在确定性候选层和证据包之上接入五个独立的正式 Agent：

```text
market_industry
fundamental
technical_state
fundflow_participation
event_risk
```

实现入口为 `stock_tool/core/multi_agent_orchestrator.py` 的
`FiveAgentOrchestrator`。它只接收已通过阶段 2 校验的不可变证据包；候选集合由
调用方提供的确定性结果冻结，Agent 只能整理证据、记录缺口和发现引用问题。

## 执行合同

- 五个 Agent 使用独立线程并行运行；`market_industry` 的定义标记为
  `market_snapshot`，其余 Agent 标记为 `candidate_set`。
- 正式输入候选最多 30 名。每个 Agent 都有独立的 `agent_version`、
  `prompt_version`、超时和重试边界。
- 每个结果保存完整的 `request_hash`、`response_hash`、原始响应、规范化响应、
  证据包身份和费用说明。可用 `SqliteAgentExecutionStore` 持久化，也可用内存实现
  做离线测试。
- 缓存键包含：

  ```text
  snapshot_id
  candidate_set_digest
  lens_version
  agent_version
  prompt_version
  model_version
  ```

- 线程收到的是证据包深拷贝，返回值只携带 AgentResult，不携带候选修改。编排结果
  会回显运行前的 `candidate_records` 和 `candidate_set_preserved`，用于阻止 AI
  影响候选集合。

## 状态与降级

`AgentResult v1` 的 `status` 仍严格限定为 `ok`、`degraded`、`failed`、`skipped`。
为了不破坏阶段 0 已冻结的契约，阶段 4 更细的运行原因记录在执行记录的
`outcome` 和 `AgentResult.diagnostics.outcome` 中：

| outcome | 含义 |
| --- | --- |
| `ok` | 当前 Agent 完成且引用有效 |
| `insufficient` | 关键事实缺失；status 为 `degraded` |
| `evidence_invalid` | 无效 claim 被逐条丢弃；status 为 `degraded` |
| `failover` | 使用备用源；不自动视为 verified；status 为 `degraded` |
| `failed` | 超时、解析错误、API 错误或其他运行错误 |
| `skipped` | 没有该 Agent 允许读取的事实，未发送请求 |

一个 Agent 失败不会取消其他 Agent。全部 Agent 失败或跳过时，规则候选仍保留，
编排结果为 `unavailable`，由 UI 显示 AI 区域不可用。编排结果为 `partial` 时，
各 Agent 的状态和 outcome 仍逐项保留，不能平均成单一分数或排名。

## 验收

```powershell
python tools\test_phase4_multi_agent.py
python -m compileall stock_tool tools
python tools\test_all_blocks.py --quick
```

离线验收覆盖：五 Agent 版本和缓存键、并行独立失败、超时/重试边界、缺失事实、
无效 fact 引用、备用源 failover、原始响应保存、缓存回放、候选集合不变性，以及
UI 状态文本对 `status` 和 `outcome` 的区分。真实服务商调用仍属于显式 live 风险检查，
不混入离线结论。

阶段 4 不实现 F 层冲突汇总、候选状态转换或正式 Qt 页面重构；F/G 层已在阶段 5
单独落地，正式 Qt 页面重构仍属于阶段 6。正式 Agent 编排已经具备可被后续页面调用
的状态与审计边界。
