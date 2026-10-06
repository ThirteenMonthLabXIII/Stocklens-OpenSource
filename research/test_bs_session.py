from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import research.bs_session as bs_session
from research._testkit import run_tests


class FakeResult:
    def __init__(self, error_code: str = "0", error_msg: str = "", rows: list[list[str]] | None = None) -> None:
        self.error_code = error_code
        self.error_msg = error_msg
        self.fields = ["value"]
        self._rows = rows or []
        self._idx = 0

    def next(self) -> bool:
        return self._idx < len(self._rows)

    def get_row_data(self) -> list[str]:
        row = self._rows[self._idx]
        self._idx += 1
        return row


class FakeBaostockModule:
    def __init__(self) -> None:
        self.login_count = 0
        self.logout_count = 0
        self.query_count = 0

    def login(self) -> FakeResult:
        self.login_count += 1
        return FakeResult()

    def logout(self) -> None:
        self.logout_count += 1

    def query_profit_data(self, code: str, year: int, quarter: int) -> FakeResult:
        self.query_count += 1
        if self.query_count == 1:
            return FakeResult("10001001", "用户未登录")
        return FakeResult(rows=[["ok"]])


class FakeDB:
    def __init__(self) -> None:
        self.logs = []

    def log_call(self, run_id, api, code, status, rows, duration_sec, error=None):
        self.logs.append({
            "run_id": run_id,
            "api": api,
            "code": code,
            "status": status,
            "rows": rows,
            "duration_sec": duration_sec,
            "error": error,
        })


def test_baostock_session_relogin_once_then_query_succeeds():
    fake_bs = FakeBaostockModule()
    fake_db = FakeDB()
    original_module = sys.modules.get("baostock")
    sys.modules["baostock"] = fake_bs
    try:
        with bs_session.BaostockSession(fake_db, "run123", "fundamentals") as bs:
            df = bs_session.query_to_df(lambda: bs.query_profit_data(code="sh.600001", year=2024, quarter=1), retries=1)
    finally:
        if original_module is None:
            sys.modules.pop("baostock", None)
        else:
            sys.modules["baostock"] = original_module

    assert fake_bs.login_count == 2
    assert fake_bs.logout_count >= 1
    assert fake_bs.query_count == 2
    assert df.to_dict("records") == [{"value": "ok"}]
    assert fake_db.logs == [
        {
            "run_id": "run123",
            "api": "fundamentals_relogin",
            "code": "sh.600001",
            "status": "ok",
            "rows": 1,
            "duration_sec": 0.0,
            "error": "用户未登录",
        }
    ]


if __name__ == "__main__":
    raise SystemExit(run_tests(globals()))
