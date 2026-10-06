from __future__ import annotations

from datetime import datetime, timedelta
import math

from stock_tool.core.models import EventCatalyst, MarketSentiment, MoneyFlowSnapshot, StockQuote, TradeReference, fmt_number
from stock_tool.core.technical_profile import TechnicalProfile


def _risk_reward(entry: float | None, fail: float | None, target: float | None) -> float | None:
    if entry is None or fail is None or target is None:
        return None
    try:
        entry_value = float(entry)
        fail_value = float(fail)
        target_value = float(target)
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(value) for value in [entry_value, fail_value, target_value]):
        return None
    risk = entry_value - fail_value
    reward = target_value - entry_value
    if risk <= 0 or reward <= 0:
        return None
    return round(reward / risk, 2)


def _trend_view(technical: TechnicalProfile) -> str:
    if not technical.available:
        return "技术数据不足，暂不预测趋势"
    trend_map = {
        "uptrend": "上升趋势，短线优先观察趋势延续",
        "recovery": "修复趋势，需放量确认能否继续上攻",
        "sideways": "震荡整理，等待区间突破或回踩确认",
        "downtrend": "下降趋势，反弹先按弱反弹处理",
    }
    status_map = {
        "triggered": "形态已触发",
        "candidate": "形态仍是候选",
        "avoid": "结构偏弱",
        "failed": "形态失效",
        "low_data": "数据不足",
    }
    trend = trend_map.get(technical.trend_stage, "趋势不明")
    status = status_map.get(technical.status, technical.status or "状态不明")
    return f"{trend}；{status}；{technical.opportunity_label}"


def infer_main_force_behavior(money_flow: MoneyFlowSnapshot | None) -> str:
    if not money_flow or not money_flow.rows:
        return "资金流数据缺失，主力行为暂不可判定"
    sample_days = len(money_flow.rows)
    if sample_days < 3:
        return f"资金样本仅{sample_days}日，不能判断主力连续行为"

    main_3d = money_flow.main_3d or 0.0
    main_5d = money_flow.main_5d or 0.0
    main_10d = money_flow.main_10d or 0.0
    xlarge_5d = money_flow.xlarge_5d or 0.0
    small_5d = money_flow.small_5d or 0.0
    streak = money_flow.streak or 0

    if main_5d > 0 and xlarge_5d >= 0 and streak >= 3:
        return "公开资金流显示主力/超大单连续流入，偏吸筹或增仓"
    if main_5d > 0 and small_5d <= 0:
        return "近5日主力流入且散户未明显接盘，偏主动收集，但连续性需确认"
    if main_3d > 0 and main_10d < 0:
        return "短线有资金回补，但10日仍偏流出，像修复或试探，不宜直接定性吸筹"
    if main_3d < 0 and main_10d > 0:
        return "短线主力流出但10日仍有底仓，可能是洗盘或阶段分歧，需看失效价"
    if main_5d < 0 and small_5d > 0:
        return "近5日主力流出且散户接盘，偏派发风险"
    if main_5d < 0 and xlarge_5d < 0 and streak <= -3:
        return "主力/超大单连续流出，偏减仓或撤退"
    if abs(streak) < 3:
        return "资金连续性不足，主力行为仍是中性观察"
    if main_5d > 0:
        return "主力资金阶段流入，但缺少更强共振证据"
    if main_5d < 0:
        return "主力资金阶段流出，短线承压"
    return "资金流接近平衡，主力行为不明确"


def _buy_decision(action: str, technical: TechnicalProfile) -> str:
    if not technical.available:
        return "不能买：K线/技术画像不足，无法给出可靠买点"
    if technical.status == "avoid":
        return "不能买：结构偏弱，等待重新修复"
    if technical.status == "candidate":
        return "暂不买：候选形态不等于买点，需放量站上触发价"
    if action == "观察，不追":
        return "暂不追：已触发但风险收益或资金确认不足"
    if technical.status == "triggered":
        return "可小仓试错：仅限触发后轻仓验证，跌破失效价退出"
    return "观察：未形成足够清晰的新增买入条件"


def build_trade_reference(
    quote: StockQuote,
    technical: TechnicalProfile,
    money_flow: MoneyFlowSnapshot | None,
    sentiment: MarketSentiment | None,
    events: list[EventCatalyst] | None,
) -> TradeReference:
    risks: list[str] = []
    positives: list[str] = []

    level = "C"
    action = "观察"
    horizon_days = 5
    entry = technical.entry_trigger if technical.available else None
    fail = technical.fail_level if technical.available else None
    target = technical.target_level if technical.available else None

    if not technical.available:
        level, action = "D", "回避"
        risks.append(technical.summary or "技术画像缺失，不生成买卖价位")
    elif technical.opportunity_label.startswith("S"):
        level, action = "S", "已触发，轻仓试错"
    elif technical.opportunity_label.startswith("A"):
        level, action = "A", "等待触发"
    elif technical.opportunity_label.startswith("B"):
        level, action = "B", "持仓观察"
    elif technical.opportunity_label.startswith("D"):
        level, action = "D", "回避"

    if technical.status == "candidate":
        action = "等待触发"
        risks.append("技术形态仍是候选，未放量站上触发价前不视为买点")
    elif technical.status == "avoid":
        action = "回避"
        risks.append("技术结构偏弱或回避，不建议新增买入")
    if technical.risk_flags:
        risks.extend(f"技术风险：{flag}" for flag in technical.risk_flags[:3])

    main_force = infer_main_force_behavior(money_flow)
    if money_flow:
        if (money_flow.main_5d or 0) > 0 and (money_flow.small_5d or 0) <= 0:
            positives.append("近5日主力流入且散户未明显接盘")
        if (money_flow.main_5d or 0) < 0 and (money_flow.small_5d or 0) > 0:
            risks.append("近5日主力流出且散户接盘")
            if level in {"S", "A"}:
                action = "观察，不追"
        if abs(money_flow.streak) < 3:
            risks.append("资金连续性不足，主力行为仍需观察")

    if sentiment:
        if sentiment.down > sentiment.up * 1.5:
            risks.append("市场情绪偏弱，降低买入积极性")
        elif sentiment.up > sentiment.down * 1.5:
            positives.append("市场情绪偏暖，有利于短线试错")

    if events:
        positives.append(f"近事件/新闻 {len(events)} 条，可作为催化，但需量价和资金验证")
    else:
        risks.append("暂无明确事件催化")

    rr = _risk_reward(entry, fail, target)
    if rr is not None:
        if rr < 2.0:
            risks.append(f"风险收益比约 {rr:.2f}，低于2.0，不作为主动买点")
            if action not in {"回避", "持仓观察"}:
                action = "观察，不追"

    if action in {"已触发，轻仓试错"}:
        position_hint = "单次试错不超过1-2成，跌破失效价退出"
        horizon_days = 3
    elif action == "观察，不追":
        position_hint = "不追高，等待回踩不破失效价或资金重新共振"
        horizon_days = 3
    elif action == "等待触发":
        position_hint = "未触发前不买，触发后再小仓位验证"
        horizon_days = 5
    elif action == "持仓观察":
        position_hint = "已有仓位可观察，未持仓不追"
        horizon_days = 7
    else:
        position_hint = "不新增买入，等待结构修复"
        horizon_days = 5

    buy_decision = _buy_decision(action, technical)
    trend_view = _trend_view(technical)
    entry_text = fmt_number(entry, "元")
    target_text = fmt_number(target, "元")
    fail_text = fmt_number(fail, "元")
    rr_text = f"{rr:.2f}" if rr is not None else "-"
    invalidation = f"跌破失效价 {fail_text}" if fail is not None else "技术/资金数据不足或主力资金继续流出"

    thesis_parts = [
        f"能否买入：{buy_decision}",
        f"参考买入：{entry_text}；参考卖出/目标：{target_text}；风控止损：{fail_text}；风险收益比：{rr_text}",
        f"趋势判断：{trend_view}",
        f"主力行为：{main_force}",
        f"技术面：{technical.summary or technical.opportunity_label}",
    ]
    if money_flow:
        thesis_parts.append(f"资金面：{money_flow.summary}")
    if sentiment:
        thesis_parts.append(f"市场：{sentiment.summary}")
    if positives:
        thesis_parts.append("正面因素：" + "；".join(positives[:3]))
    if risks:
        thesis_parts.append("主要风险：" + "；".join(risks[:4]))
    thesis_parts.append(f"反证条件：{invalidation}")

    return TradeReference(
        action=action,
        level=level,
        entry_price=entry,
        fail_price=fail,
        target_price=target,
        horizon_days=horizon_days,
        position_hint=position_hint,
        thesis="\n".join(thesis_parts),
        risks=risks[:6],
    )


def due_date_from_now(horizon_days: int) -> str:
    return (datetime.now() + timedelta(days=max(1, int(horizon_days or 5)))).strftime("%Y-%m-%d")


def review_tracking_task(task, quote: StockQuote) -> tuple[str, str, str]:
    price = quote.price
    if price is None:
        return "open", "行情缺失", "当前价缺失，暂不能复盘。"
    status = "open"
    result = "观察中"
    notes = [f"当前价 {fmt_number(price, '元')}。"]
    if task.target_price and price >= task.target_price:
        status = "closed"
        result = "达到目标"
        notes.append(f"已达到目标价 {fmt_number(task.target_price, '元')}。")
    elif task.fail_price and price <= task.fail_price:
        status = "closed"
        result = "触发失效"
        notes.append(f"已跌破失效价 {fmt_number(task.fail_price, '元')}。")
    else:
        try:
            due = datetime.strptime(task.due_date, "%Y-%m-%d").date()
            if datetime.now().date() >= due:
                status = "closed"
                result = "到期未触发"
                notes.append("观察周期到期，未达到目标价或失效价。")
        except ValueError:
            result = "数据异常"
            notes.append(f"到期日格式无效：{task.due_date or '-'}。")
    return status, result, " ".join(notes)
