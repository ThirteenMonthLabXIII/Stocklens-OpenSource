# Stocklens / A 股研究与复盘桌面工具

基于 Python 与 PySide6 的本地研究工作台，以 MIT 许可证开源，供学习、工程参考和二次开发。

**当前状态：实验性项目。免费行情与财务接口存在限流、缺失和失效问题，部分在线功能无法稳定使用；项目没有托管数据服务，也不附带全市场数据库。**

本项目用于公开数据整理、候选股研究和复盘辅助，不构成投资建议，不承诺收益、实时行情或提醒触达。离线测试与模拟演示不代表在线数据可用，也不证明策略有效。

## 可以参考什么

| 模块 | 可学习的内容 | 使用边界 |
| --- | --- | --- |
| Qt 工作台 | 页面导航、后台任务、表格与研究交互 | 源码唯一启动 Qt 工作台；有 Python/PySide6 环境要求 |
| 确定性候选层 | Universe、Candidate Lens、缺失字段处理、稳定排序 | 可用合成输入离线学习；实际研究需要完整、可追溯的数据 |
| 快照与证据包 | 数据来源、可获得时间、引用校验与回放 | 不附带历史数据；正式运行会阻断缺失、过期或未验证输入 |
| 观察与复盘 | 运行记录、观察样本、3/5/10/20 日评价与排除原因 | 实际结算依赖行情和 PIT 数据 |
| AI 证据整理 | 五 Agent 编排、失败重试、输出约束 | 用户自行提供 Key 并承担费用；AI 不参与正式候选排序 |
| 数据源与缓存 | 接口适配、缓存年龄、并发去重、错误提示 | 免费源可能失效；多个库可能共用同一上游 |

阅读路径见 [开源价值与模块导览](docs/开源价值与模块导览.md)。

## 先跑一个离线演示

演示只调用生产选股政策模块，用内存中的合成输入展示完整输入、缺失 ROE、ST 排除和未来事件四种情况。它不联网、不读取数据库、不生成正式筛选凭证，不需要 API Key。演示所用的质量和来源标记均是模拟值。

推荐先用 Windows 与 Python 3.12。以下演示仅使用 Python 标准库与项目内模块，无需安装完整依赖：

```powershell
python -X utf8 tools/demo_offline.py
```

若要观察结构化输出：

```powershell
python -X utf8 tools/demo_offline.py --json
```

这是命令行政策演示，桌面界面的唯一入口仍是 Qt 工作台。

## 从源码启动桌面应用

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -X utf8 main.py
```

首次启动选择位于仓库外的新的本地数据目录，避免使用他人的数据库。API Key 优先保存在 Windows 凭据管理器，失败时使用本地数据目录数据库兜底。不要上传该目录。

无 Key 时可以学习本地逻辑；AI 请求需要用户自己的服务商权限。Tushare 增强兜底默认关闭，需要用户自己的 Token 和相应权限。开启这些服务可能产生费用。

没有新鲜数据时，正式筛选、研究或复盘动作可能被阻断，这是现有数据质量要求。换用 `adata`、`efinance` 等库不一定能解决东方财富 IP 级限流。不要把缺失字段改成零、强制标记 verified，或把模拟数据导入正式观察记录来绕过检查。

## 验证与已知限制

安装依赖后，可分别运行核心离线回归：

```powershell
python -X utf8 tools/test_phase0_5_selection_policy.py
python -X utf8 tools/test_phase1_candidate_layer.py
python -X utf8 tools/test_phase2_evidence_package.py
python -X utf8 tools/test_phase4_multi_agent.py
python -X utf8 tools/test_open_source_export.py
```

完整原有回归入口：

```powershell
python -X utf8 tools/test_all_blocks.py --quick
python -X utf8 tools/test_all_blocks.py --full
```

完整回归保留商业发布与历史合同测试；相关固定证据、签名配置和商业包未随公开候选提供，运行结果需逐项判断。公开候选的本次验证记录、失败项与未执行项见 [开源准备说明](docs/开源准备说明.md)。本地离线通过不代表 GitHub CI 已通过。

本仓库的 GitHub Actions 默认只运行源码检查、离线演示与核心回归；更广的原有回归需手动触发。CI 不自动访问行情源，不需要生产 Key。不要为复现问题在公开 Issue 中提交密钥、完整日志或个人数据库。

## 源码结构

```text
stock_tool/core/     选股政策、数据适配、缓存、快照、证据与复盘
stock_tool/ui/       PySide6 Qt 工作台和界面资源
stock_tool/config/   公开的规则、模板与运行配置
research/           PIT、回测、执行账本和离线研究代码
tools/              回归、演示、源码导出及历史商业发布工具
docs/               模块说明与历史设计文档
```

`docs/开发说明.md`、阶段文档和商业发布脚本记录既有设计，其中“已完成”“PASS”“可交付”属于历史阶段口径。它们不代表当前免费源稳定，也不代表已完成面向公众的安装包验收。当前准备分享的是源码。

## 数据、贡献与许可证

项目代码采用 [MIT 许可证](LICENSE)，版权署名为十三月。允许使用、修改、分发和商用，需保留版权与许可声明。附带字体和其他第三方依赖继续遵循各自许可证。

仓库不附带数据库、缓存、个人策略、研究结果报告、API Key、私钥或安装包。第三方行情数据的使用和再分发权限不随代码开放。

欢迎修复可复现的问题、补充数据适配和文档；优先改善数据依赖与运行体验。维护投入可能有限，不承诺问题响应时限。提交变更前请阅读 [贡献说明](CONTRIBUTING.md)、[安全说明](SECURITY.md) 和 [第三方许可说明](THIRD_PARTY_NOTICES.md)。
