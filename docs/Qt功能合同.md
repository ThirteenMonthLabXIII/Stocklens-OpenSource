# Qt 功能合同

此文档是 Qt-only 工作台的当前对齐口径。机器可读清单位于 `stock_tool/config/frontend_parity_contract.json`，严格合同命令为：

```powershell
python tools\test_frontend_default_gate.py
```

该命令会验证完整能力集合、合同字段、状态、测试登记，并在状态全部“已对齐”后执行每个必需能力的离线测试。任何 `SKIP`、测试失败或未对齐状态都会返回非零；它是合同闸门，不替代日常开发测试。

| 能力 ID | 来源 | 当前状态 | Qt 入口 | 核心结果 |
|---|---|---|---|---|
| `home.dashboard` | Qt 工作台与核心数据服务 | 部分对齐 | 今日概览 | 真实摘要和 KPI 路由待完成验收 |
| `research.stock_analysis` | Qt 工作台与核心数据服务 | 部分对齐 | 个股研究 | 搜索、详情和自选路径待完整验收 |
| `screening.parse_and_execute` | Qt 工作台与核心筛选服务 | 部分对齐 | 条件选股 | 已能解析和执行；完整点击闭环待完成 |
| `screening.closed_loop` | Qt 工作台与核心筛选服务 | 部分对齐 | 条件选股 | 已保存运行、上下文与观察样本；发布级异常覆盖待完成 |
| `screening.strategy_management` / `screening.history_compare` | Qt 工作台与策略服务 | 部分对齐 | 条件选股 | 保存、导出、历史和比较已接入 |
| `watchlist.management` | Qt 工作台与核心数据服务 | 部分对齐 | 自选股 | 基础操作已接入，完整回归待完成 |
| `alerts.price_monitoring` | Qt 工作台与预警服务 | 部分对齐 | 价格预警 | CRUD、检查、触发记录和 CSV 已接入 |
| `tracking.task_review` | Qt 工作台与复盘服务 | 部分对齐 | 观察与复盘 | 可创建观察/持仓任务并完成客观复核 |
| `performance.review` | Qt 工作台与复盘服务 | 部分对齐 | 观察与复盘 | 结算、模式隔离和候选状态复核仍待完整点击验收；专业公式治理不属于新 Qt 产品入口 |
| `data.*` / `settings.*` / `export.center` | Qt 工作台与数据服务 | 部分对齐 | 数据与更新、设置 | P4 已提供目录迁移、日志、PIT 和统一导出 |
| `acceptance.center` | Qt 工作台与验收服务 | 部分对齐 | 设置 | 可查看最近验收并打开 TXT、JSON 和报告目录 |
| `startup.default_ui` / `packaging.qt` | Qt-only 启动与构建 | 部分对齐 | 启动与构建 | 默认启动 Qt，未来构建不得包含已下线前端 |
| `legacy.ai_ranking` | 应下线能力 | 应下线/隐藏 | 无 | 不迁入正式 Qt |

口径：

- “已对齐”必须具有真实业务结果、重启后持久化、明确异常提示、非阻塞 UI 和自动回归覆盖。
- “观察任务”与“观察样本”不同：前者是单股任务，后者是筛选后的 3/5/10/20 日表现样本。
- “上涨比例”替代“胜率”；汇总使用“平均最大回撤”；观察字段使用“观察触发价、失效价、目标观察价”。
- Qt 是当前唯一前端；历史前端调用会明确拒绝，Qt 故障不提供界面回退。
- 不得将未完成能力的 `required_for_default` 改为 `false`。所有正式能力的必需集合由 `default_required_feature_ids` 固定，开发测试会检查它与每项标记完全一致。

## P4 验收证据

- 设置页以掩码保存 Tushare Token，并明确东财资金流失败后的可选 Tushare 兜底、权限与积分风险。
- 目录迁移先预检源/目标，再经暂存目录复制；目标必须为空，后台任务未结束时拒绝迁移。成功后只更新下次启动目录，要求重启，原目录保留。
- 数据页可查看/清空抓取日志、导出 PIT 失败清单，并集中导出筛选、历史比较、预警、观察任务和表现明细；新候选 CSV 不含公式分数或 AI 排名，CSV 使用 UTF-8 BOM 与同目录原子替换。
- 设置页提供验收中心，读取最近报告并提供 TXT、JSON、目录入口；缺失或损坏报告有明确提示。
- 自动验收：`python tools\test_frontend_p4_gate.py`；P4 仍只标为“部分对齐”，不改变 P5 的默认启动闸门。

## Qt-only 启动验收证据

- 源码入口固定选择 Qt；历史前端参数和环境变量明确拒绝并返回 2。
- Qt 导入或窗口初始化失败时输出脱敏错误编号和异常类型，显示失败提示并以非零状态退出。
- 启动探针只记录 `frontend=qt` 的选择、导入、窗口、事件循环和失败阶段。
- 自动验收：`python tools\test_frontend_p5_gate.py` 与 `python tools\test_qt_default_surface.py`。
- 该阶段仅验收源码启动闭环。`startup.default_ui` 和 `packaging.qt` 在 P7 完成干净 Windows 候选包验收前仍保持“部分对齐”。

## P6 实施证据

- 正式 `build.spec` 已包含 PySide6、`QtWebEngineProcess.exe`、Qt 资源、Web/K 线资源、图标和配置；保护构建暂存目录也会复制 `assets/`，避免带 `STOCK_TOOL_PYI_SOURCE_ROOT` 的正式构建丢失 exe 图标；`build_qt.spec` 仅作兼容保留。
- 免费 PyArmor 保护清单不再以过时的 32KB 本地阈值拒绝构建；所有声明目标都必须存在，实际混淆结果由 PyArmor 命令返回码和后续候选包 smoke 共同验证。
- 商业包 smoke 不再只检查进程存活：它通过只读启动探针验证候选包默认选择并完成 Qt 导入。
- 自动静态闸门：`python tools\test_frontend_p6_gate.py`。实际候选包仍必须设置 `STOCK_TOOL_PACKAGED_DIR` 并通过 `python tools\test_qt_packaged_smoke.py`。

## Qt 导航与查询口径

P0 固定以下用户语言，机器合同中的 `qt_navigation_groups` 与 Qt 侧栏使用同一套验收映射：

| 分组 | 页面 | 用户任务 |
|---|---|---|
| 工作台 | 今日概览 | 查询股票或条件，查看待处理事项和数据状态 |
| 研究 | 个股研究、条件选股、自选股 | 找股票、形成候选、整理关注清单 |
| 跟踪 | 价格预警、观察与复盘 | 管理提醒、观察任务和客观复核 |
| 管理 | 数据与更新、设置 | 更新数据、查看降级原因和管理本地配置 |

首页统一输入框负责识别“股票代码/名称”和“选股条件”，再跳转到个股研究或条件选股。页面名称不得使用“AI复盘”代替完整复盘能力；AI 只能作为局部增强，并且失败不能阻断本地功能。
