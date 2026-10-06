from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from stock_tool.core.models import FinancialMetrics, StockQuote
from stock_tool.core.notify import (
    LocalFileChannel,
    Notifier,
    NotifyMessage,
    NotifyResult,
    WindowsToastChannel,
    _to_xueqiu_code,
    build_candidate_message,
)
from stock_tool.core.pit_database import PITDatabase
from stock_tool.core.screener_engine import ScreenCandidate, ScreenReport
from stock_tool.core.technical_profile import TechnicalProfile
import tools.run_screening_push as run_screening_push


def _candidate(code: str, name: str, score: float, reasons: list[str], missing_fields=None) -> ScreenCandidate:
    breakdown = {"missing_fields": missing_fields} if missing_fields is not None else {}
    return ScreenCandidate(
        quote=StockQuote(code=code, name=name, price=12.3, market_cap=100_000_000_000, pe=18, pb=2.1),
        metrics=FinancialMetrics(roe=18, net_margin=11, debt_ratio=45),
        technical=TechnicalProfile(True, opportunity_label="A 回踩候选", summary="趋势稳定"),
        local_score=score,
        reasons=reasons,
        score_breakdown=breakdown,
    )


def _sample_report() -> ScreenReport:
    return ScreenReport(
        results=[
            _candidate("600519", "贵州茅台", 91.2, ["ROE较好", "PE较温和"], ["market_cap_yi"]),
            _candidate("000001", "平安银行", 82.5, ["负债率较稳", "A 回踩候选"]),
        ],
        scanned=300,
        skipped=280,
        failed=1,
        errors=["000002 样例地产: 财务字段缺失"],
    )


def test_build_message() -> None:
    msg = build_candidate_message(
        "低估高ROE",
        "找ROE高于15，PE低于30的股票",
        "ROE >= 15；PE <= 30",
        _sample_report(),
        "2026-06-24",
        "pit",
        limit=20,
    )
    assert "低估高ROE" in msg.title
    assert "2026-06-24" in msg.title
    assert "贵州茅台" in msg.markdown
    assert "600519" in msg.markdown
    assert "https://xueqiu.com/S/SH600519" in msg.markdown
    assert "ROE较好" in msg.markdown
    assert "扫描 300 / 跳过 280 / 失败 1" in msg.markdown
    assert "market_cap_yi" in msg.markdown
    assert "不构成投资建议" in msg.markdown
    assert "买入" not in msg.markdown
    assert "收益承诺" not in msg.markdown

    neutral_msg = build_candidate_message(
        "原文带交易动作",
        "建议买入收益翻倍",
        "",
        _sample_report(),
        "2026-06-24",
        "pit",
    )
    assert "建议买入收益翻倍" not in neutral_msg.markdown
    assert "买入" not in neutral_msg.markdown
    print("[OK] 通知播报格式化")


def test_xueqiu_code() -> None:
    assert _to_xueqiu_code("600519") == "SH600519"
    assert _to_xueqiu_code("000001") == "SZ000001"
    assert _to_xueqiu_code("830001") == "BJ830001"
    assert _to_xueqiu_code("430001") == "BJ430001"
    print("[OK] 雪球代码映射")


def test_local_file_channel() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        msg = build_candidate_message("低估高ROE", "策略", "摘要", _sample_report(), "2026-06-24", "pit")
        result = LocalFileChannel(Path(tmp)).send(msg)
        assert result.ok is True
        path = Path(result.detail)
        assert path.exists()
        text = path.read_text(encoding="utf-8")
        assert "贵州茅台" in text
        assert "不构成投资建议" in text
    print("[OK] 本地 Markdown 通知文件")


def test_toast_channel_graceful() -> None:
    msg = NotifyMessage("标题", "正文", "标题\n正文", 1)
    false_result = WindowsToastChannel(sender=lambda title, text: False).send(msg)
    assert false_result.ok is False

    def boom(title: str, text: str) -> bool:
        raise RuntimeError("toast failed")

    error_result = WindowsToastChannel(sender=boom).send(msg)
    assert error_result.ok is False
    assert "RuntimeError" in error_result.detail
    print("[OK] Windows Toast 降级")


class BrokenChannel:
    name = "broken"

    def send(self, msg: NotifyMessage) -> NotifyResult:
        raise RuntimeError("channel failed")


def test_notifier_never_raises() -> None:
    msg = NotifyMessage("标题", "正文", "标题\n正文", 1)
    results = Notifier([BrokenChannel()]).send(msg)
    assert len(results) == 1
    assert results[0].channel == "broken"
    assert results[0].ok is False
    assert "RuntimeError" in results[0].detail
    print("[OK] Notifier 渠道异常隔离")


def test_pit_latest_asof() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db = PITDatabase(Path(tmp) / "pit.sqlite3")
        db.upsert_universe(
            [
                {
                    "code": "000001",
                    "name": "平安银行",
                    "exchange": "SZ",
                    "asof_date": "2026-06-20",
                    "source": "source_a",
                    "fetched_at": "2026-06-20T15:30:00",
                },
                {
                    "code": "600519",
                    "name": "贵州茅台",
                    "exchange": "SH",
                    "asof_date": "2026-06-24",
                    "source": "source_b",
                    "fetched_at": "2026-06-24T15:30:00",
                },
            ]
        )
        assert db.latest_asof() == "2026-06-24"
        assert db.latest_asof("source_a") == "2026-06-20"
        assert db.latest_asof("missing") is None
    print("[OK] PIT 最新日期查询")


def test_cli_data_dir_fallback() -> None:
    old_root = run_screening_push.ROOT
    old_read_data_dir = run_screening_push.read_data_dir
    try:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            data_dir = base / "data"
            data_dir.mkdir()
            (base / "data_location.txt").write_text(str(data_dir), encoding="utf-8")
            run_screening_push.ROOT = base
            run_screening_push.read_data_dir = lambda: None
            assert run_screening_push._read_cli_data_dir() == data_dir.resolve()
            assert run_screening_push._source_arg("auto") is None
            assert run_screening_push._source_arg("baostock") == "baostock"
    finally:
        run_screening_push.ROOT = old_root
        run_screening_push.read_data_dir = old_read_data_dir
    print("[OK] CLI 数据目录兜底")


def run_tests() -> None:
    test_build_message()
    test_xueqiu_code()
    test_local_file_channel()
    test_toast_channel_graceful()
    test_notifier_never_raises()
    test_pit_latest_asof()
    test_cli_data_dir_fallback()


if __name__ == "__main__":
    run_tests()
