"""价格预警的判定逻辑与提醒发送。

参照腾讯自选股的「到价提醒」：用户给某只股票设定一个价格或涨跌幅阈值，
软件在交易时段定时拉取行情，价格满足条件时主动提醒。

支持的预警类型（kind）：
- ``buy``        当前价 <= 阈值（跌到心理买入价，提示可考虑买入）
- ``sell``       当前价 >= 阈值（涨到目标卖出价，提示可考虑卖出/止盈）
- ``change_up``  当日涨幅 >= 阈值%（异动拉升提醒）
- ``change_down`` 当日跌幅 >= 阈值%（异动跳水/止损提醒）

数据来源是 5 分钟缓存的快照行情，并非实时逐笔，因此预警是「分钟级、软件开启时」
的提醒，不等同于券商的实时推送。
"""

from __future__ import annotations

from datetime import datetime, time as dtime, timedelta

from stock_tool.core.models import StockQuote, fmt_number


# kind -> (中文名, 需要的单位)
ALERT_KINDS: dict[str, str] = {
    "buy": "跌到买入价",
    "sell": "涨到卖出价",
    "change_up": "涨幅达到",
    "change_down": "跌幅达到",
}

# 阈值是百分比的类型
_PERCENT_KINDS = {"change_up", "change_down"}


def is_percent_kind(kind: str) -> bool:
    return kind in _PERCENT_KINDS


def describe_alert(kind: str, threshold: float) -> str:
    """生成人类可读的条件描述，例如 “跌到买入价 ≤ 12.50 元”。"""
    label = ALERT_KINDS.get(kind, kind)
    if kind == "buy":
        return f"{label} ≤ {fmt_number(threshold, '元')}"
    if kind == "sell":
        return f"{label} ≥ {fmt_number(threshold, '元')}"
    if kind == "change_up":
        return f"{label} +{threshold:.2f}%"
    if kind == "change_down":
        return f"{label} -{threshold:.2f}%"
    return f"{label} {threshold}"


def evaluate_alert(kind: str, threshold: float, quote: StockQuote) -> tuple[bool, str | None]:
    """判断给定行情是否触发预警。

    返回 (是否触发, 提醒文案)。行情缺失关键字段时返回 (False, None)。
    """
    if kind in {"buy", "sell"}:
        price = quote.price
        if price is None:
            return False, None
        if kind == "buy" and price <= threshold:
            return True, f"{quote.name} 现价 {fmt_number(price, '元')}，已跌到买入价 {fmt_number(threshold, '元')}"
        if kind == "sell" and price >= threshold:
            return True, f"{quote.name} 现价 {fmt_number(price, '元')}，已涨到卖出价 {fmt_number(threshold, '元')}"
        return False, None

    if kind in {"change_up", "change_down"}:
        change = quote.change_pct
        if change is None:
            return False, None
        if kind == "change_up" and change >= threshold:
            return True, f"{quote.name} 今日涨幅 {change:+.2f}%，已达到 +{threshold:.2f}% 预警线（现价 {fmt_number(quote.price, '元')}）"
        if kind == "change_down" and change <= -threshold:
            return True, f"{quote.name} 今日跌幅 {change:.2f}%，已达到 -{threshold:.2f}% 预警线（现价 {fmt_number(quote.price, '元')}）"
        return False, None

    return False, None


# A股交易时段：上午 9:30-11:30，下午 13:00-15:00，周一至周五。
_MORNING = (dtime(9, 30), dtime(11, 30))
_AFTERNOON = (dtime(13, 0), dtime(15, 0))


def in_trading_session(now: datetime | None = None) -> bool:
    now = now or datetime.now()
    if now.weekday() >= 5:  # 周六/周日
        return False
    current = now.time()
    return (_MORNING[0] <= current <= _MORNING[1]) or (_AFTERNOON[0] <= current <= _AFTERNOON[1])


# ---- 去重:边沿触发 + 冷却 ------------------------------------------------
#
# 电平触发(只看"此刻是否满足")会在价格趴在阈值线下时每个间隔重复提醒。
# 这里改成:只在条件「从不满足跨到满足」的那一刻提醒(边沿),并对两次提醒
# 之间加最小间隔(冷却),抑制价格在阈值附近来回抖动导致的连环提醒。

ALERT_COOLDOWN_MIN = 10  # 同一条预警两次提醒的最小间隔(分钟)


def _cooldown_elapsed(last_triggered_at: str, cooldown_min: int, now: datetime | None = None) -> bool:
    """距上次提醒是否已超过冷却时间。从未提醒或时间无法解析时放行(不漏报)。"""
    if not last_triggered_at:
        return True
    now = now or datetime.now()
    try:
        prev = datetime.strptime(last_triggered_at, "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return True
    return (now - prev) >= timedelta(minutes=cooldown_min)


def decide_alert_action(
    hit: bool,
    last_state: int,
    last_triggered_at: str,
    cooldown_min: int = ALERT_COOLDOWN_MIN,
    now: datetime | None = None,
) -> tuple[bool, int]:
    """根据本次命中与上次状态,决定是否提醒以及新的 last_state。

    返回 (是否提醒, 新 last_state)。last_state: 0=上次不满足, 1=上次已满足。

    - 未命中               → (False, 0)  重新武装,下次跨入才算新事件
    - 命中且上次已满足      → (False, 1)  趴在线下不重复(边沿)
    - 命中、新穿越、过冷却   → (True, 1)   提醒
    - 命中、新穿越、未过冷却 → (False, 1)  抖动抑制
    """
    if not hit:
        return False, 0
    if last_state == 1:
        return False, 1
    if _cooldown_elapsed(last_triggered_at, cooldown_min, now):
        return True, 1
    return False, 1


def send_windows_toast(title: str, message: str, app_id: str = "A股选股仪") -> bool:
    """用 winotify 发送 Windows 右下角通知。

    成功返回 True；winotify 未安装或发送失败返回 False，由调用方决定回退方式。
    """
    try:
        from winotify import Notification  # type: ignore
    except Exception:
        return False
    try:
        toast = Notification(app_id=app_id, title=title, msg=message)
        toast.show()
        return True
    except Exception:
        return False
