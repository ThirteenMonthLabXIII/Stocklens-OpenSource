from __future__ import annotations

"""P4 settings, data, export and acceptance-center Qt interaction checks."""

import json
import os
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QTWEBENGINE_DISABLE_SANDBOX", "1")

from stock_tool.core.data_location import migrate_data_dir, preflight_data_migration
from stock_tool.core.data_fetcher import TUSHARE_MONEY_FLOW_FALLBACK_SETTING
from stock_tool.core.alert_monitor import TradingCalendar
from stock_tool.core.secrets import TUSHARE_TOKEN_NAME
from stock_tool.core.notify import MemoryNotificationChannel, Notifier
from stock_tool.ui import qt_workbench
from tools.test_qt_workbench_smoke import FakeWebEngineView, _weekday_calendar, wait_for


class MemorySecretStore:
    """Avoid touching the user's Windows Credential Manager in a UI test."""

    _values: dict[str, str] = {}
    backend_message = "P4 测试内存存储"

    def __init__(self, _cache, *, use_keyring: bool = True) -> None:
        pass

    def migrate_legacy_secret(self, _name: str) -> None:
        return None

    def get_secret(self, name: str) -> str:
        return self._values.get(name, "")

    def set_secret(self, name: str, value: str) -> None:
        self._values[name] = value


def _button(window, action_id: str):
    return next(item for item in window.findChildren(qt_workbench.QPushButton) if item.property("action_id") == action_id)


def test_data_migration_preflight_and_recovery() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        source = root / "source"; source.mkdir()
        (source / "stock_tool.sqlite3").write_bytes(b"sqlite-content")
        nested = source / "reports"; nested.mkdir(); (nested / "result.txt").write_text("ok", encoding="utf-8")
        target = root / "target"; target.mkdir()
        plan = preflight_data_migration(source, target, require_empty=True)
        assert plan.file_count == 2 and plan.total_bytes > 0
        migrate_data_dir(source, target, require_empty=True)
        assert (target / "stock_tool.sqlite3").read_bytes() == b"sqlite-content"
        assert (target / "reports" / "result.txt").read_text(encoding="utf-8") == "ok"
        occupied = root / "occupied"; occupied.mkdir(); (occupied / "keep.txt").write_text("keep", encoding="utf-8")
        try:
            preflight_data_migration(source, occupied, require_empty=True)
        except ValueError as exc:
            assert "必须为空" in str(exc)
        else:
            raise AssertionError("non-empty migration target must be rejected")


def test_qt_p4_controls_by_real_click() -> None:
    from PySide6.QtTest import QTest

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw:
        data_dir = Path(raw) / "data"; data_dir.mkdir()
        reports_dir = Path(raw) / "reports"; reports_dir.mkdir()
        txt_path = reports_dir / "quick.txt"; txt_path.write_text("结论：可发版", encoding="utf-8-sig")
        (reports_dir / "quick.json").write_text(
            json.dumps({"finished_at": "2026-07-12T10:00:00", "conclusion": "可发版", "live_enabled": False, "txt_report": str(txt_path), "blocks": []}),
            encoding="utf-8-sig",
        )
        qt_workbench.read_data_dir = lambda: data_dir
        qt_workbench.read_configured_data_dir = lambda: None
        qt_workbench.write_data_dir = lambda _p: None
        qt_workbench.SecretStore = MemorySecretStore
        qt_workbench.QWebEngineView = FakeWebEngineView
        qt_workbench.QMessageBox.question = staticmethod(lambda *_args, **_kwargs: qt_workbench.QMessageBox.Yes)
        qt_workbench.QMessageBox.information = staticmethod(lambda *_args, **_kwargs: None)
        qt_workbench.QMessageBox.warning = staticmethod(lambda *_args, **_kwargs: None)
        app = qt_workbench.QApplication.instance() or qt_workbench.QApplication([])
        window = qt_workbench.StockWorkbenchWindow(notifier=Notifier([MemoryNotificationChannel()]))
        window.review_automation.trading_clock = qt_workbench.TradingClock(TradingCalendar(_weekday_calendar))
        window.show()
        wait_for(window, "data_refresh")
        wait_for(window, "review_automation")
        window._acceptance_report_dir = lambda: reports_dir
        window._navigate("settings"); window.refresh_acceptance_center()
        assert window.acceptance_table.rowCount() == 1
        assert window.acceptance_table.item(0, 2).text() == "快速验收"
        assert window.tushare_token_input.echoMode() == qt_workbench.QLineEdit.Password
        window.tushare_token_input.setText("ts-token-for-test")
        window.tushare_fallback_check.setChecked(True)
        QTest.mouseClick(_button(window, "settings.save_validate"), qt_workbench.Qt.LeftButton)
        assert window.secret_store.get_secret(TUSHARE_TOKEN_NAME) == "ts-token-for-test"
        assert window.cache.get_setting(TUSHARE_MONEY_FLOW_FALLBACK_SETTING) == "1"

        window.cache.set_json("quote:000001", {"price": 10})
        window.cache.add_watch("000001", "样例")
        window.cache.add_fetch_log("P4", "000001", "test", "failed", "network")
        window._navigate("data")
        QTest.mouseClick(_button(window, "data.clear_fetch_logs"), qt_workbench.Qt.LeftButton)
        assert not window.cache.list_fetch_logs(limit=10)
        assert window.cache.get_json("quote:000001") is not None
        assert window.cache.list_watch()[0]["code"] == "000001"
        pit_export_path = data_dir / "pit_failures.csv"
        window._choose_csv_path = lambda *_args: str(pit_export_path)
        QTest.mouseClick(_button(window, "data.pit_failures.export"), qt_workbench.Qt.LeftButton)
        assert pit_export_path.read_bytes().startswith(b"\xef\xbb\xbf")

        migration_target = Path(raw) / "migrated"; migration_target.mkdir()
        qt_workbench.QFileDialog.getExistingDirectory = staticmethod(lambda *_args, **_kwargs: str(migration_target))
        window._navigate("settings")
        QTest.mouseClick(_button(window, "settings.change_data_dir"), qt_workbench.Qt.LeftButton)
        assert (migration_target / "stock_tool.sqlite3").exists()
        assert "迁移完成" in window.data_migration_status_label.text()

        required_actions = {
            "export.center.screener", "export.center.history_compare", "export.center.alerts",
            "export.center.tracking", "export.center.performance",
            "data.pit_failures.export",
            "acceptance.open_txt", "acceptance.open_json", "acceptance.open_directory", "acceptance.refresh",
        }
        registered = {item.property("action_id") for item in window.findChildren(qt_workbench.QPushButton)}
        assert required_actions <= registered
        window.close()
        if window.v1_database is not None:
            window.v1_database.close()
        app.processEvents()


if __name__ == "__main__":
    test_data_migration_preflight_and_recovery()
    test_qt_p4_controls_by_real_click()
    print("[OK] P4 Qt 设置、迁移、数据、导出和验收中心测试通过")
