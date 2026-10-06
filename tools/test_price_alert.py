from __future__ import annotations

import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from stock_tool.core.alert_service import apply_alert_quotes
from stock_tool.core.cache import CacheStore
from stock_tool.core.models import StockQuote


def test_edit_preserves_alert_identity_and_history() -> None:
    with tempfile.TemporaryDirectory() as raw:
        cache = CacheStore(Path(raw) / "cache.db")
        alert_id = cache.add_price_alert("000001", "样例", "sell", 10.0, "旧备注")
        cache.add_alert_history(alert_id, "000001", "样例", "sell", 10.0, 10.0, 0.0, "旧记录")
        assert cache.update_price_alert(
            alert_id,
            code="000001",
            name="样例银行",
            kind="change_up",
            threshold=3.0,
            note="新备注",
            repeat=True,
        )
        alert = cache.list_price_alerts()[0]
        assert alert.id == alert_id
        assert (alert.name, alert.kind, alert.threshold, alert.note, alert.repeat) == ("样例银行", "change_up", 3.0, "新备注", True)
        assert alert.last_state == 0 and alert.last_triggered_at == "" and alert.trigger_count == 0
        assert cache.list_alert_history()[0].alert_id == alert_id


def test_batch_check_handles_edge_trigger_cooldown_and_single_disable() -> None:
    with tempfile.TemporaryDirectory() as raw:
        cache = CacheStore(Path(raw) / "cache.db")
        alert_id = cache.add_price_alert("000001", "样例", "sell", 10.0, repeat=False)
        alert = cache.list_price_alerts(enabled_only=True)[0]
        now = datetime(2026, 7, 10, 10, 0, 0)
        result = apply_alert_quotes(cache, [alert], {"000001": StockQuote("000001", "样例", price=10.5)}, now=now)
        assert result.checked_count == 1 and result.triggered_count == 1
        assert not cache.list_price_alerts()[0].enabled
        assert len(cache.list_alert_history()) == 1

        repeat_id = cache.add_price_alert("000002", "持续", "sell", 10.0, repeat=True)
        repeat = next(item for item in cache.list_price_alerts() if item.id == repeat_id)
        first = apply_alert_quotes(cache, [repeat], {"000002": StockQuote("000002", "持续", price=10.5)}, now=now)
        assert first.triggered_count == 1
        repeat = next(item for item in cache.list_price_alerts() if item.id == repeat_id)
        held = apply_alert_quotes(cache, [repeat], {"000002": StockQuote("000002", "持续", price=10.6)}, now=now + timedelta(minutes=20))
        assert held.triggered_count == 0
        repeat = next(item for item in cache.list_price_alerts() if item.id == repeat_id)
        apply_alert_quotes(cache, [repeat], {"000002": StockQuote("000002", "持续", price=9.9)}, now=now + timedelta(minutes=21))
        repeat = next(item for item in cache.list_price_alerts() if item.id == repeat_id)
        again = apply_alert_quotes(cache, [repeat], {"000002": StockQuote("000002", "持续", price=10.5)}, now=now + timedelta(minutes=22))
        assert again.triggered_count == 1


def main() -> int:
    test_edit_preserves_alert_identity_and_history()
    test_batch_check_handles_edge_trigger_cooldown_and_single_disable()
    print("[OK] price_alert 回归通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
