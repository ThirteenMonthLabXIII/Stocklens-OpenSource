from __future__ import annotations


RETRY_WITH_TOKEN = "如已配置 Tushare Token，可使用显式增强源重试；本流程不会自动消耗增强源额度。"
CONFIGURE_TOKEN = "需要更稳的数据源时，可在设置页配置 Tushare Token 后使用显式增强源重试。"


def format_pit_status_for_user(mode: str, status: str, message: str, token_available: bool = False) -> str:
    """Translate PIT job/source states into buyer-readable Chinese while keeping raw detail."""
    mode_text = _mode_label(mode)
    status_text = str(status or "").strip()
    raw_message = str(message or "").strip()
    normalized_message = _translate_raw_message(raw_message)
    original = _original_suffix(raw_message, normalized_message)
    token_hint = RETRY_WITH_TOKEN if token_available else CONFIGURE_TOKEN

    if status_text == "completed":
        if mode == "healthcheck":
            return f"{mode_text}通过。{normalized_message or 'Baostock 可以访问。'}{original}"
        if mode == "preflight":
            return f"{mode_text}通过，可以继续全量建库。{normalized_message}{original}".strip()
        return f"{mode_text}完成。{normalized_message}{original}".strip()

    if status_text == "completed_with_errors":
        return (
            f"{mode_text}已完成，但有部分股票或字段失败。已有成功数据已保留；"
            f"可稍后运行重试失败项，或导出失败清单排查。{_join_detail(normalized_message, original)}"
        )

    if status_text == "SOURCE_COOLDOWN":
        return (
            f"免费源冷却，已停止{_mode_action(mode)}。建议等待 10-30 分钟后重试，"
            f"不要连续点击以免触发更长限流。{token_hint}{_join_detail(normalized_message, original)}"
        )

    if status_text == "SOURCE_UNREACHABLE":
        return (
            f"免费源暂时连不上，已停止{_mode_action(mode)}。请检查网络、代理和防火墙，"
            f"也可能是公开接口临时波动。{token_hint}{_join_detail(normalized_message, original)}"
        )

    if status_text == "ENV_UNAVAILABLE":
        return (
            f"当前环境还不能运行{mode_text}。请先安装缺失依赖，或确认可选 Token/配置已填写。"
            f"{_join_detail(normalized_message, original)}"
        )

    if status_text == "preflight_failed":
        return (
            "50 只预检未通过，已停止全量建库。请先打开报告或导出失败清单查看失败来源；"
            f"如果是免费源限流或网络波动，稍后重试。{token_hint}{_join_detail(normalized_message, original)}"
        )

    if status_text == "healthcheck_failed":
        return (
            "Baostock 连通性检查未通过，未进入预检和全量建库。请检查网络、代理、baostock 依赖，"
            f"或稍后重试。{_join_detail(normalized_message, original)}"
        )

    if status_text == "interrupted":
        return (
            f"{mode_text}上次长时间没有进度，已标记为中断。已有成功数据会保留；"
            f"再次启动会继续复用已完成部分。{_join_detail(normalized_message, original)}"
        )

    if status_text == "running":
        return f"{mode_text}正在运行。{normalized_message or '请保持软件打开，完成后会自动刷新。'}{original}"

    if status_text in {"", "GO"}:
        return normalized_message or "Baostock 可以访问。"

    return f"{mode_text}状态：{status_text}。{normalized_message}{original}".strip()


def _mode_label(mode: str) -> str:
    return {
        "provider": "数据源检查",
        "healthcheck": "Baostock 连通性检查",
        "preflight": "50 只预检",
        "full": "全量建库",
        "incremental": "日增量",
        "retry-failures": "失败重试",
    }.get(str(mode or "").strip(), str(mode or "任务").strip() or "任务")


def _mode_action(mode: str) -> str:
    label = _mode_label(mode)
    return "全量建库" if mode == "preflight" else label


def _translate_raw_message(message: str) -> str:
    text = message.strip()
    lower = text.lower()
    if not text:
        return ""
    if "baostock not installed" in lower or "no module named" in lower:
        return "缺少 baostock 依赖，请先安装依赖后重试。"
    if "token not configured" in lower or "token" in lower and "not configured" in lower:
        return "增强源 Token 未配置。"
    if "login failed" in lower or "not login" in lower or "not logged" in lower or "10001001" in lower:
        return "Baostock 登录失败，通常是公开源临时不可用或网络被拦截。"
    if "timeout" in lower or "timed out" in lower:
        return "请求超时，可能是网络较慢、代理不稳定或公开源响应过慢。"
    if "remote end closed" in lower or "connection" in lower or "socket" in lower or "dns" in lower or "proxy" in lower:
        return "连接被中断，可能是网络、代理、防火墙或公开源临时波动。"
    if "query_all_stock returned 0 rows" in lower or "returned no trading day" in lower:
        return "公开源返回空数据，可能是数据源当日尚未发布或临时异常。"
    if "source_cooldown" in lower or "cooled down" in lower:
        return "请求保护已进入冷却，系统已主动停止以降低继续限流风险。"
    if "preflight" in lower and "failed" in lower:
        return "预检失败，当前失败率或冷却次数不适合继续全量建库。"
    return text


def _original_suffix(raw_message: str, translated_message: str) -> str:
    if not raw_message or raw_message == translated_message:
        return ""
    return f" 原始信息：{raw_message}"


def _join_detail(message: str, original: str) -> str:
    detail = f"{message}{original}".strip()
    return f" {detail}" if detail else ""
