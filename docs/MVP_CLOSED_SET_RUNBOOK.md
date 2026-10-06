# MVP 闭集运行手册（legacy 历史兼容）

> 本手册只用于读取或复现旧 MVP 公式运行记录，不是当前生产运行手册。
> 当前生产路径固定为“不可变快照 → 数据质量闸门 → CandidateLens v1 → 稳定排序 → 五 Agent 证据整理 → F/G 层 → 人工观察”。
> 新流程不得读取、创建、启用、shadow、回滚或以专业公式作为失败回退；本文件中的公式字段、`score_mode` 和影子命令均不得用于生成新正式候选。

## 当前发布状态

- `stock_tool/config/mvp_runtime.json` 和下列公式状态只代表历史兼容运行配置。
- 历史 `shadow`/`production` 记录可读取、审计和复盘，但不代表当前 CandidateLens 生产口径。
- `legacy_score`：只读历史参考分，用于解释和异常对照，不作为 `score_mode`，也不改变排序。

## 首次构建与每日增量

在项目根目录依次运行：

```powershell
python tools/build_pit_dataset.py --mode incremental --source baostock
python tools/build_mvp_factor_store.py --universe all_a
python tools/build_mvp_factor_store.py --universe hs300
python tools/build_mvp_factor_store.py --universe chinext
python tools/run_mvp_shadow_matrix.py
```

首次 PIT 数据库为空时，第一条命令会从默认起点构建；后续按数据库进度增量。全池因子任务最多使用两个 worker，失败或过期股票会在下一次继续。任何股票池、覆盖率、字段新鲜度或批处理失败率门控不通过时，对应组合不会输出残缺榜单。

## 历史公式切换（不适用于当前生产）

旧版本在满足历史门控后可能把 `mvp_runtime.json` 的 `score_mode` 改为 `production`。当前版本不执行这条生产切换；新运行统一使用 CandidateLens 元数据和确定性排序。

出现异常时，当前流程只记录 Agent/F 层失败并保留确定性候选；不得回退到公式、旧 `_score()` 或 AI 排名。历史公式只保留为 `legacy_score` 参考数据。

## 影子权重

影子权重只能读取 2019–2024 训练证据，命令不会修改生产模板：

```powershell
python tools/derive_mvp_shadow_weights.py quality evidence.json --output research/out/quality_shadow_weights.json
```

无稳定正证据时自动退回生产先验；有证据时执行 `75% 先验 + 25% 证据`，并投影到每项相对先验 `±25%` 且总和为 1 的约束集合。2025-01-01 至 2026-06-16 只允许诊断。

## 离线验收

```powershell
python tools/test_mvp_closed_set.py
python tools/test_performance_loop.py
python tools/test_qt_workbench_smoke.py
python -m compileall stock_tool tools
python tools/test_all_blocks.py --quick
```
