# 第三方许可与数据说明

项目代码采用 MIT 许可证。字体和第三方依赖继续适用各自许可证，项目 MIT 许可不替代它们的许可。

## 字体

`stock_tool/ui/web/fonts/JetBrainsMono-Regular.ttf` 与 `JetBrainsMono-Medium.ttf` 为 JetBrains Mono 字体，随附的 `OFL.txt` 标注 SIL Open Font License 1.1。公开候选保留字体和完整原许可；项目自身的许可证不能替代字体许可。

来源：[JetBrains Mono](https://github.com/JetBrains/JetBrainsMono)，许可：[OFL.txt](stock_tool/ui/web/fonts/OFL.txt)。

## Python 与 Qt 依赖

依赖由用户通过 pip 获取，候选源码不附带 wheel、虚拟环境或编译后的第三方库。第三方库遵循各自许可证，不因本项目选择 MIT 或其他许可而改变。

PySide6/Qt for Python 提供开源与商业许可选项；具体模块与二进制分发要求应按实际依赖检查。参见 [Qt for Python 许可说明](https://doc.qt.io/qtforpython-6/licenses.html)。打包安装器、Nuitka 和可选研究库也需单独核对。

## 图形资源

附带 Logo、图标和 SVG 已由作者确认可以随源码公开。若资源本身附带独立的第三方版权与许可声明，应继续保留。

## 行情与财务数据

akshare、baostock、Tushare、tickflow 等是代码依赖或接口入口，不代表本项目取得了底层行情的再分发权。东方财富、腾讯、新浪等接口可能调整、限流或不可用；多个库可能依赖同一个上游。

源码开放不等于数据开放。请遵守数据提供方的使用条款、权限、积分及费用要求。候选不附带本机历史数据、个人缓存和研究产物。`tools/demo_offline.py` 中所有数值与来源标记都是合成输入，与实际股票没有数据对应关系。
