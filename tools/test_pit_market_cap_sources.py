from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.evaluate_pit_market_cap_sources import SourceProbe, _to_ts_code, build_conclusion


def test_to_ts_code() -> None:
    assert _to_ts_code("600519") == "600519.SH"
    assert _to_ts_code("000001") == "000001.SZ"
    assert _to_ts_code("sz.000001") == "000001.SZ"


def test_conclusion_keeps_market_cap_blocked_without_tushare_probe() -> None:
    probes = [
        SourceProbe("tushare.daily_basic", "unavailable", "missing token", {}),
        SourceProbe("akshare.stock_zh_a_spot_em", "probe_ok_current_only", "current only", {}),
    ]
    conclusion = build_conclusion(probes)
    assert "remains blocked" in conclusion
    assert "current-only" in conclusion


def test_conclusion_allows_next_step_only_after_tushare_probe() -> None:
    probes = [
        SourceProbe("tushare.daily_basic", "probe_ok_unit_inferred", "returned data", {}),
    ]
    conclusion = build_conclusion(probes)
    assert "only candidate" in conclusion
    assert "accepting the observed unit conversion" in conclusion


def run_tests() -> None:
    test_to_ts_code()
    test_conclusion_keeps_market_cap_blocked_without_tushare_probe()
    test_conclusion_allows_next_step_only_after_tushare_probe()
    print("[OK] PIT market_cap_yi source evaluation tests passed")


if __name__ == "__main__":
    run_tests()
