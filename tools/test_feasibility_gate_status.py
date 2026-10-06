from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research.sources.baostock_probe import _probe_g3_universe, _probe_g4_industry
from research.sources.common import classify_error, throughput_gate, unavailable_gates


class FakeResultSet:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows
        self.fields = list(rows[0].keys()) if rows else []
        self.error_code = "0"
        self.error_msg = ""
        self._index = -1

    def next(self) -> bool:
        self._index += 1
        return self._index < len(self.rows)

    def get_row_data(self) -> list[str]:
        row = self.rows[self._index]
        return [str(row.get(field, "")) for field in self.fields]


class FakeBaostockForUniverse:
    def query_stock_basic(self):
        return FakeResultSet(
            [
                {"code": "sh.000001", "code_name": "上证指数", "ipoDate": "1990-12-19", "outDate": "", "type": "2", "status": "1"},
                {"code": "sz.399001", "code_name": "深证成指", "ipoDate": "1991-04-03", "outDate": "", "type": "2", "status": "1"},
                {"code": "sh.600000", "code_name": "浦发银行", "ipoDate": "1999-11-10", "outDate": "", "type": "1", "status": "1"},
            ]
        )

    def query_all_stock(self, day: str):
        return FakeResultSet([{"code": "sh.600000", "code_name": "浦发银行"}])


class FakeBaostockForIndustry:
    def query_stock_industry(self, date: str):
        rows = []
        for index in range(90):
            rows.append({"code": f"sh.600{index:03d}", "code_name": f"样例{index}", "industry": "银行", "updateDate": date})
        return FakeResultSet(rows)

    def query_all_stock(self, day: str):
        return FakeResultSet([{"code": f"sh.600{index:03d}", "code_name": f"样例{index}"} for index in range(100)])


def run_tests() -> None:
    assert classify_error("Remote end closed connection without response")["status"] == "SOURCE_UNREACHABLE"
    gates = unavailable_gates("ENV_UNAVAILABLE", "baostock not installed")
    assert gates["G1"]["status"] == "ENV_UNAVAILABLE"
    assert gates["G5"]["status"] == "ENV_UNAVAILABLE"

    median_for_39h = 39 * 3600 / 5000
    g5 = throughput_gate([{"ok": True, "label": "call", "duration_sec": median_for_39h}], "测试接口")
    assert g5["status"] == "WORKAROUND", g5
    assert round(g5["evidence"]["serial_5000_stock_hours"]) == 39

    g3 = _probe_g3_universe(FakeBaostockForUniverse(), [], {}, 1)
    assert g3["status"] == "GO", g3
    assert g3["evidence"]["stock_basic"]["filtered_a_share_shape"][0] == 1
    assert g3["evidence"]["stock_basic"]["excluded_non_stock_or_index"] == 2

    g4 = _probe_g4_industry(FakeBaostockForIndustry(), [], {}, 1)
    assert g4["status"] == "WORKAROUND", g4
    assert min(item["coverage"] for item in g4["evidence"]["dates"]) == 0.9

    print("[OK] feasibility gate status tests passed")


if __name__ == "__main__":
    run_tests()
