# 阶段 3：基本面 Agent

阶段 3 只接入一个正式 Agent，用于验证“不可变证据包 → AgentResult → 可回放审计”的完整链路。第一 Agent 固定为 `fundamental`，不负责新增、删除、排序、评分或升级候选。

## 实现边界

实现位置：`stock_tool/core/fundamental_agent.py`。

- 输入必须是阶段 2 的正式证据包，并绑定 `snapshot_id`、`availability_cutoff`、候选集合和证据包哈希。
- Agent 只可看到基本面字段白名单：`report_date`、`roe`、`gross_margin`、`net_margin`、`debt_ratio`、`revenue_growth`、`profit_growth`、`operating_cashflow_per_share` 及明确列出的估值上下文字段。
- `report_date` 是财务报告期，不能用行情日期替代；距 `asof_at` 超过 365 天或晚于快照时间的报告期由本地闸门标记 `stale`。
- 缺失字段、过期报告期、非 verified 事实不得被 Agent 包装成当前支持/反向结论；模型必须用精确缺口标记回应。
- 外部公告、新闻标题和网页文本仅以 `<external_fact>...</external_fact>` 资料区块进入提示词，不是指令。
- 模型输出只包含证据、反向事实、数据缺口、风险标记、人工核查问题和降级说明；本地代码绑定 `agent_id`、版本、运行 ID、快照 ID 和模型身份，生成 `AgentResult v1`。

## 失败和旁路

- 正常 JSON 生成 `ok` 或 `degraded` 的正式 `AgentResult`。
- 没有任何允许的财务事实时，不发送请求，生成 `skipped` 并列明缺口。
- JSON 截断、未知字段、虚构 `fact_id`、stale 事实被当成当前结论、超时、API 错误和交易字段都会生成 `failed` 结果，并保留原始响应（若服务商已返回）。
- Agent 失败不改变证据包内的 `candidate_records`，确定性候选结果由本地层继续保留。
- 请求器通过依赖注入提供，离线测试不访问真实服务商；生产默认使用已有 `request_ai_json_with_raw()`。

## 阶段验收

```powershell
python tools\test_phase3_fundamental_agent.py
python -m compileall stock_tool tools
python tools\test_all_blocks.py --quick
python tools\release_acceptance.py
```

覆盖项：正常 JSON、JSON 截断、未知字段、虚构事实引用、缺少财务字段、过期报告期、超时、API 错误、交易字段污染，以及 Agent 失败时确定性候选保持不变。

本阶段不把五 Agent 编排、F 层汇总或 Qt 正式页面改造提前并入；这些属于后续阶段。当前 UI 的通用研究讨论兼容路径继续保留，不能把它误称为基本面 Agent 已接入五 Agent 正式流程。
