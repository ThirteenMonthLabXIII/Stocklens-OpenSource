from __future__ import annotations

import sys
import tempfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from stock_tool.core.alert_service import AlertDraft, AlertService
from stock_tool.core.cache import CacheStore
from stock_tool.core.models import StockQuote


def test_crud_audit_policy_and_check_contract() -> None:
    with tempfile.TemporaryDirectory() as raw:
        service = AlertService(CacheStore(Path(raw) / "cache.sqlite3"))
        invalid = service.create_alert(AlertDraft("1", "", "sell", 10), "tester")
        assert not invalid.ok and invalid.code == "validation_error"
        created = service.create_alert(AlertDraft("000001", "样例", "sell", 10, repeat=False), "tester")
        assert created.ok and created.alert is not None
        alert_id = created.alert.id
        changed = service.update_alert(alert_id, AlertDraft("000001", "样例二", "change_up", 2.5, repeat=True), "tester")
        assert changed.ok and changed.alert is not None and changed.alert.id == alert_id
        assert service.set_enabled(alert_id, False, "tester").code == "disabled"
        assert service.set_enabled(alert_id, True, "tester").code == "enabled"
        result = service.check_alerts({"000001": StockQuote("000001", "样例", price=11, change_pct=3)}, datetime(2026, 7, 10, 10))
        assert result.triggered_count == 1
        assert service.list_history()[0].created_at == datetime(2026, 7, 10, 10)
        assert service.check_alerts({"000001": None}).failed_codes == ("000001",)
        assert service.update_monitor_policy(True, 10) == {"enabled": True, "interval_minutes": 10}
        try:
            service.update_monitor_policy(True, 2)
            raise AssertionError("非法监控间隔必须拒绝")
        except ValueError:
            pass
        assert service.clear_history("tester", "test").ok
        assert service.delete_alert(alert_id, "tester", "test").ok
        actions = [row.action for row in service.cache.list_audit("alert", 20)]
        assert {"create", "update", "enable", "disable", "delete"}.issubset(actions)


def main() -> int:
    test_crud_audit_policy_and_check_contract()
    print("[OK] AlertService 合同测试通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
