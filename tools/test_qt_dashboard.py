from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from stock_tool.ui.qt_dashboard_data import (
    build_watch_rows,
    classify_search_intent,
    data_health_rows,
    merge_quote_cache_payloads,
    summarize_alerts,
    summarize_tracking,
)


@dataclass(frozen=True)
class Task:
    code: str
    name: str
    task_type: str
    status: str
    due_date: str


@dataclass(frozen=True)
class Alert:
    code: str
    enabled: bool = True


@dataclass(frozen=True)
class Quote:
    code: str
    name: str
    price: float | None
    change_pct: float | None


@dataclass(frozen=True)
class CacheItem:
    key: str
    updated_at: datetime


def test_classify_search_intent() -> None:
    assert classify_search_intent("000725") == "stock"
    assert classify_search_intent("sh600000") == "stock"
    assert classify_search_intent("贵州茅台") == "stock"
    assert classify_search_intent("ROE大于8 PE小于80") == "filter"
    assert classify_search_intent("主力进场 近5日") == "filter"
    assert classify_search_intent("资产负债率 <= 60") == "filter"


def test_tracking_and_alert_summary() -> None:
    tasks = [
        Task("000001", "平安银行", "watch", "open", "2026-06-26"),
        Task("000002", "万科A", "holding", "open", "2026-07-01"),
        Task("000003", "测试", "watch", "closed", "2026-06-20"),
    ]
    summary = summarize_tracking(tasks, today=date(2026, 6, 27))
    assert summary == {
        "pool": 2,
        "watching": 1,
        "holding": 1,
        "review": 1,
        "in_progress": 2,
        "done": 1,
    }
    assert summarize_alerts([Alert("000001"), Alert("000002", enabled=False)], [object(), object()]) == {
        "monitoring": 1,
        "triggered_today": 2,
    }


def test_watch_rows_reuse_status_tags() -> None:
    tasks = [
        Task("000001", "平安银行", "watch", "open", "2026-06-26"),
        Task("000002", "万科A", "holding", "open", "2026-07-01"),
    ]
    alerts = [Alert("000001")]
    rows = build_watch_rows(
        [{"code": "000001", "name": "平安银行"}, {"code": "000002", "name": "万科A"}],
        {
            "000001": Quote("000001", "平安银行", 10.5, 1.2),
            "000002": Quote("000002", "万科A", None, None),
        },
        tasks,
        alerts,
        updated_at=datetime(2026, 6, 27, 10, 0),
        today=date(2026, 6, 27),
    )
    assert rows[0]["tags"] == ["观察中", "待复盘", "价格预警"]
    assert rows[1]["tags"] == ["持仓观察", "数据缺失"]


def test_watch_quotes_override_stale_market_quotes() -> None:
    quotes_by_code = merge_quote_cache_payloads(
        [
            [{"code": "000001", "name": "旧行情", "price": 9.8, "change_pct": -0.4}],
            [{"code": "000001", "name": "新行情", "price": 10.6, "change_pct": 1.1}],
        ],
        Quote,
    )
    rows = build_watch_rows(
        [{"code": "000001", "name": "平安银行"}],
        quotes_by_code,
        [],
        [],
        updated_at=datetime(2026, 6, 27, 12, 0),
        today=date(2026, 6, 27),
    )
    assert rows[0]["name"] == "新行情"
    assert rows[0]["price"] == 10.6


def test_data_health_rows() -> None:
    now = datetime(2026, 6, 27, 12, 0)
    rows = data_health_rows(
        [
            CacheItem("watch_quotes", now - timedelta(minutes=2)),
            CacheItem("spot_quotes", now - timedelta(minutes=5)),
            CacheItem("history:000001", now - timedelta(days=3)),
            CacheItem("financial:000001", now - timedelta(days=20)),
        ],
        now=now,
    )
    assert rows[0] == ("自选行情", "正常", "2分钟前")
    assert rows[1] == ("全市场行情", "正常", "5分钟前")
    assert rows[2][1] == "正常"
    assert rows[3][1] == "过旧"
    assert rows[4] == ("事件", "暂不可用", "无可用缓存")


def run() -> None:
    test_classify_search_intent()
    test_tracking_and_alert_summary()
    test_watch_rows_reuse_status_tags()
    test_watch_quotes_override_stale_market_quotes()
    test_data_health_rows()
    print("[OK] qt_dashboard 纯函数回归通过")


if __name__ == "__main__":
    run()
