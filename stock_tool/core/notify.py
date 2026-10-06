from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

from stock_tool.core.price_alert import send_windows_toast
from stock_tool.core.screener_engine import ScreenCandidate, ScreenReport


@dataclass(frozen=True)
class NotifyMessage:
    title: str
    markdown: str
    plaintext: str
    candidate_count: int


@dataclass(frozen=True)
class NotifyResult:
    channel: str
    ok: bool
    detail: str


class NotifyChannel(Protocol):
    name: str

    def send(self, msg: NotifyMessage) -> NotifyResult:
        ...


def build_candidate_message(
    preset_name: str,
    user_text: str,
    summary: str,
    report: ScreenReport,
    asof: str,
    engine: str,
    limit: int = 20,
) -> NotifyMessage:
    title = f"📈 选股播报 | {preset_name} | {asof}"
    candidates = report.results[: max(1, int(limit or 1))]
    summary_text = summary.strip() or "策略条件见执行记录"
    health = _health_line(report)
    missing = _missing_fields_line(candidates)
    source_line = f"- 引擎：{_engine_label(engine)}"
    summary_line = f"- 策略摘要：{summary_text}"
    safety_line = "- 定位：以下内容仅用于公开数据整理、候选股研究和复盘辅助，不构成投资建议。"

    markdown_lines = [
        f"# {title}",
        "",
        safety_line,
        source_line,
        summary_line,
        f"- 数据健康：{health}",
    ]
    if missing:
        markdown_lines.append(f"- 字段提示：{missing}")
    if report.errors:
        markdown_lines.append(f"- 异常样例：{'; '.join(report.errors[:3])}")
    markdown_lines.extend(["", "## Top 候选股"])

    if candidates:
        for rank, candidate in enumerate(candidates, start=1):
            markdown_lines.append(_candidate_markdown_line(rank, candidate))
    else:
        markdown_lines.append("本次未筛出候选股。")

    plaintext_lines = [
        title,
        "仅用于公开数据整理、候选股研究和复盘辅助，不构成投资建议。",
        f"引擎：{_engine_label(engine)}",
        f"策略摘要：{summary_text}",
        f"数据健康：{health}",
    ]
    if missing:
        plaintext_lines.append(f"字段提示：{missing}")
    for rank, candidate in enumerate(candidates, start=1):
        plaintext_lines.append(_candidate_plaintext_line(rank, candidate))

    return NotifyMessage(
        title=title,
        markdown="\n".join(markdown_lines).strip() + "\n",
        plaintext="\n".join(plaintext_lines).strip(),
        candidate_count=len(report.results),
    )


def _to_xueqiu_code(code: str) -> str:
    raw = "".join(ch for ch in str(code or "") if ch.isdigit())
    if not raw:
        return str(code or "")
    raw = raw[-6:].zfill(6)
    if raw.startswith("6"):
        return f"SH{raw}"
    if raw.startswith(("4", "8")):
        return f"BJ{raw}"
    return f"SZ{raw}"


def _candidate_markdown_line(rank: int, candidate: ScreenCandidate) -> str:
    quote = candidate.quote
    code = str(quote.code or "")
    name = str(quote.name or code or "-")
    xueqiu_code = _to_xueqiu_code(code)
    score = _format_score(candidate.local_score)
    reasons = _format_reasons(candidate.reasons)
    return f"{rank}. [{name}](https://xueqiu.com/S/{xueqiu_code}) {code} · 评分 {score} · {reasons}"


def _candidate_plaintext_line(rank: int, candidate: ScreenCandidate) -> str:
    quote = candidate.quote
    code = str(quote.code or "")
    name = str(quote.name or code or "-")
    score = _format_score(candidate.local_score)
    reasons = _format_reasons(candidate.reasons)
    return f"{rank}. {name} {code} · 评分 {score} · {reasons}"


def _format_score(value: float | None) -> str:
    try:
        return f"{float(value):.1f}"
    except (TypeError, ValueError):
        return "-"


def _format_reasons(reasons: list[str]) -> str:
    picked = [str(item).strip() for item in reasons[:3] if str(item).strip()]
    return "；".join(picked) if picked else "命中策略条件"


def _health_line(report: ScreenReport) -> str:
    return f"扫描 {report.scanned} / 跳过 {report.skipped} / 失败 {report.failed}"


def _missing_fields_line(candidates: list[ScreenCandidate]) -> str:
    fields: list[str] = []
    for candidate in candidates:
        value = (candidate.score_breakdown or {}).get("missing_fields")
        if isinstance(value, str):
            fields.extend(item.strip() for item in value.split(",") if item.strip())
        elif isinstance(value, (list, tuple, set)):
            fields.extend(str(item).strip() for item in value if str(item).strip())
    unique = list(dict.fromkeys(fields))
    if not unique:
        return ""
    sample = "、".join(unique[:5])
    return f"部分字段缺失（如 {sample}），评分和排序需结合数据完整度复核"


def _engine_label(engine: str) -> str:
    if engine == "pit":
        return "PIT 点时"
    if engine == "live":
        return "实时"
    return engine or "-"


class LocalFileChannel:
    name = "local_file"

    def __init__(self, out_dir: Path) -> None:
        self.out_dir = out_dir

    def send(self, msg: NotifyMessage) -> NotifyResult:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%H%M%S")
        filename = f"选股播报_{_safe_filename(msg.title)}_{timestamp}.md"
        path = self.out_dir / filename
        path.write_text(msg.markdown, encoding="utf-8")
        return NotifyResult(self.name, True, str(path))


class WindowsToastChannel:
    name = "windows_toast"

    def __init__(self, sender=send_windows_toast) -> None:
        self._sender = sender

    def send(self, msg: NotifyMessage) -> NotifyResult:
        text = _toast_text(msg)
        try:
            ok = bool(self._sender(msg.title, text))
        except Exception as exc:
            return NotifyResult(self.name, False, f"{exc.__class__.__name__}: {exc}")
        detail = "toast sent" if ok else "winotify 不可用或通知发送失败，已降级"
        return NotifyResult(self.name, ok, detail)


class MemoryNotificationChannel:
    """测试和 UI smoke 使用的内存通知通道，绝不触发系统 Toast。"""

    name = "memory"

    def __init__(self) -> None:
        self.messages: list[NotifyMessage] = []

    def send(self, msg: NotifyMessage) -> NotifyResult:
        self.messages.append(msg)
        return NotifyResult(self.name, True, "recorded in memory")


class Notifier:
    def __init__(self, channels: list[NotifyChannel]) -> None:
        self.channels = list(channels)

    def send(self, msg: NotifyMessage) -> list[NotifyResult]:
        results: list[NotifyResult] = []
        for channel in self.channels:
            name = getattr(channel, "name", channel.__class__.__name__)
            try:
                results.append(channel.send(msg))
            except Exception as exc:
                results.append(NotifyResult(str(name), False, f"{exc.__class__.__name__}: {exc}"))
        return results


def _toast_text(msg: NotifyMessage) -> str:
    lines = [line for line in msg.plaintext.splitlines() if line.strip()]
    body = "\n".join(lines[1:4])
    if msg.candidate_count:
        body = f"候选 {msg.candidate_count} 只\n{body}"
    return body[:240]


def _safe_filename(value: str) -> str:
    text = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in value.strip())
    text = "_".join(part for part in text.split("_") if part)
    return text[:80] or "result"


# Phase 2 预留：Webhook 渠道应实现 NotifyChannel，token 走 SecretStore，
# 开关和路由走 CacheStore 设置，避免在仓库或明文配置文件中保存密钥。
