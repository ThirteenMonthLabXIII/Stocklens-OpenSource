"""Offline regression tests for feature data readiness."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from threading import Event, Lock
import time
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from stock_tool.core.data_readiness import (
    DataFreshnessPolicy,
    DataReadinessCoordinator,
    DataRequirement,
    TradingDayRule,
)
from stock_tool.core.data_refresh import RefreshStepResult


NOW = datetime(2026, 7, 27, 10, 0, tzinfo=timezone.utc)


def _policy(settings=None) -> DataFreshnessPolicy:
    return DataFreshnessPolicy(settings or {"feature_auto_refresh_enabled": "1"}, now_fn=lambda: NOW)


def test_default_ttls_and_invalid_settings_fall_back() -> None:
    policy = _policy(
        {
            "feature_refresh_quote_ttl_seconds": "not-a-number",
            "feature_refresh_factor_snapshot_trading_days": "0",
        }
    )
    assert policy.ttl_for("quotes") == timedelta(seconds=30)
    assert policy.ttl_for("market_context") == timedelta(seconds=300)
    assert policy.ttl_for_detail("financial") == timedelta(days=1)
    assert policy.ttl_for("factor_snapshot") == TradingDayRule(1)


def test_trading_day_rule_skips_weekend() -> None:
    def trading_day(value: date) -> tuple[bool, bool]:
        return value.weekday() < 5, True

    policy = DataFreshnessPolicy(
        {"feature_refresh_factor_snapshot_trading_days": "1"},
        now_fn=lambda: NOW,
        trading_day_fn=trading_day,
    )
    friday = datetime(2026, 7, 24, 15, 0, tzinfo=timezone.utc)
    monday = datetime(2026, 7, 27, 9, 0, tzinfo=timezone.utc)
    tuesday = datetime(2026, 7, 28, 9, 0, tzinfo=timezone.utc)
    assert policy.is_fresh("factor_snapshot", friday, now=monday)
    assert not policy.is_fresh("factor_snapshot", friday, now=tuesday)


def test_invalid_research_mode_is_rejected_before_any_refresh() -> None:
    coordinator = DataReadinessCoordinator(policy=_policy())
    try:
        try:
            coordinator.ensure(DataRequirement("智能筛选", ("factor_snapshot",), mode="unsafe"))
        except ValueError as exc:
            assert "研究模式" in str(exc)
        else:
            raise AssertionError("未知研究模式不能绕过 formal 质量闸门")
    finally:
        coordinator.close()


def test_missing_data_refreshes_once_then_is_ready() -> None:
    state = {"fresh": False}
    calls = {"refresh": 0}

    def inspect(_requirement):
        return {"fresh": state["fresh"], "updated_at": NOW if state["fresh"] else None}

    def refresh(_requirement, _progress):
        calls["refresh"] += 1
        state["fresh"] = True
        return RefreshStepResult(stage="quotes", status="success", fresh=1, total=1)

    coordinator = DataReadinessCoordinator(
        policy=_policy(),
        inspectors={"quotes": inspect},
        refreshers={"quotes": refresh},
    )
    try:
        first = coordinator.ensure(DataRequirement("自选列表", ("quotes",), codes=("1",)))
        second = coordinator.ensure(DataRequirement("自选列表", ("quotes",), codes=("000001",)))
        assert first.status == "refreshed"
        assert second.status == "ready"
        assert calls["refresh"] == 1
    finally:
        coordinator.close()


def test_disabled_auto_refresh_blocks_stale_data_without_network_call() -> None:
    calls = {"refresh": 0}

    def inspect(_requirement):
        return {"fresh": False, "updated_at": NOW - timedelta(minutes=10), "errors": ("行情已过期",)}

    def refresh(_requirement, _progress):
        calls["refresh"] += 1
        raise AssertionError("关闭自动刷新时不应发起网络请求")

    coordinator = DataReadinessCoordinator(
        policy=_policy({"feature_auto_refresh_enabled": "0"}),
        inspectors={"quotes": inspect},
        refreshers={"quotes": refresh},
    )
    try:
        result = coordinator.ensure(DataRequirement("自选列表", ("quotes",), codes=("000001",)))
        assert result.status == "blocked"
        assert "功能按需自动刷新已关闭" in result.errors
        assert calls["refresh"] == 0
    finally:
        coordinator.close()


def test_refresh_failure_blocks_without_using_old_data() -> None:
    calls = {"refresh": 0}

    def inspect(_requirement):
        return {"fresh": False, "updated_at": NOW - timedelta(hours=2), "errors": ("缓存已过期",)}

    def refresh(_requirement, _progress):
        calls["refresh"] += 1
        return RefreshStepResult(
            stage="factor_snapshot",
            status="failed",
            failed=1,
            errors=["公开数据源限流"],
        )

    coordinator = DataReadinessCoordinator(
        policy=_policy(),
        inspectors={"factor_snapshot": inspect},
        refreshers={"factor_snapshot": refresh},
    )
    try:
        result = coordinator.ensure(DataRequirement("智能筛选", ("factor_snapshot",)))
        assert result.status == "blocked"
        assert "公开数据源限流" in result.errors
        assert calls["refresh"] == 1
    finally:
        coordinator.close()


def test_same_kind_and_scope_share_one_inflight_refresh() -> None:
    state = {"fresh": False}
    calls = {"refresh": 0}
    call_lock = Lock()

    def inspect(_requirement):
        return {"fresh": state["fresh"], "updated_at": NOW if state["fresh"] else None}

    def refresh(_requirement, _progress):
        with call_lock:
            calls["refresh"] += 1
        time.sleep(0.08)
        state["fresh"] = True
        return RefreshStepResult(stage="research_detail", status="success", fresh=4, total=4)

    coordinator = DataReadinessCoordinator(
        policy=_policy(),
        inspectors={"research_detail": inspect},
        refreshers={"research_detail": refresh},
    )
    try:
        requirement = DataRequirement("个股研究", ("research_detail",), codes=("000001",))
        with ThreadPoolExecutor(max_workers=2) as callers:
            results = list(callers.map(coordinator.ensure, (requirement, requirement)))
        assert [item.status for item in results] == ["refreshed", "refreshed"]
        assert calls["refresh"] == 1
    finally:
        coordinator.close()


def test_different_code_scopes_refresh_independently() -> None:
    state = {"000001": False, "000002": False}
    calls: list[tuple[str, ...]] = []
    call_lock = Lock()

    def inspect(requirement):
        code = requirement.codes[0]
        return {"fresh": state[code], "updated_at": NOW if state[code] else None}

    def refresh(requirement, _progress):
        with call_lock:
            calls.append(requirement.codes)
        time.sleep(0.04)
        state[requirement.codes[0]] = True
        return RefreshStepResult(stage="research_detail", status="success", fresh=4, total=4)

    coordinator = DataReadinessCoordinator(
        policy=_policy(),
        inspectors={"research_detail": inspect},
        refreshers={"research_detail": refresh},
    )
    try:
        first = DataRequirement("个股研究", ("research_detail",), codes=("000001",))
        second = DataRequirement("个股研究", ("research_detail",), codes=("000002",))
        with ThreadPoolExecutor(max_workers=2) as callers:
            results = list(callers.map(coordinator.ensure, (first, second)))
        assert [item.status for item in results] == ["refreshed", "refreshed"]
        assert sorted(calls) == [("000001",), ("000002",)]
    finally:
        coordinator.close()


def test_force_refresh_is_not_swallowed_by_fresh_ttl() -> None:
    state = {"fresh": True}
    calls = {"refresh": 0}

    def inspect(_requirement):
        return {"fresh": state["fresh"], "updated_at": NOW}

    def refresh(_requirement, _progress):
        calls["refresh"] += 1
        return RefreshStepResult(stage="market_context", status="success", fresh=2, total=2)

    coordinator = DataReadinessCoordinator(
        policy=_policy(),
        inspectors={"market_context": inspect},
        refreshers={"market_context": refresh},
    )
    try:
        ready = coordinator.ensure(DataRequirement("研究页", ("market_context",)))
        forced = coordinator.ensure(DataRequirement("数据中心", ("market_context",), force=True))
        assert ready.status == "ready"
        assert forced.status == "refreshed"
        assert calls["refresh"] == 1
    finally:
        coordinator.close()


def test_pit_incremental_uses_successful_job_state_not_table_presence() -> None:
    state = {"completed": False}

    def inspect(_requirement):
        if not state["completed"]:
            return {"fresh": False, "errors": ("未找到成功完成的 PIT 日增量",)}
        return {
            "fresh": True,
            "updated_at": NOW,
            "snapshot_id": "pit:2026-07-27",
            "refresh_event_id": "pit-job:8",
        }

    def refresh(_requirement, _progress):
        state["completed"] = True
        return RefreshStepResult(stage="pit_incremental", status="success", fresh=1, total=1)

    coordinator = DataReadinessCoordinator(
        policy=_policy(),
        inspectors={"pit_incremental": inspect},
        refreshers={"pit_incremental": refresh},
    )
    try:
        result = coordinator.ensure(DataRequirement("PIT 日增量", ("pit_incremental",), force=True))
        assert result.status == "refreshed"
        assert result.snapshot_id == "pit:2026-07-27"
        assert result.refresh_event_id == "pit-job:8"
    finally:
        coordinator.close()


def test_force_request_waits_then_runs_its_own_pass_after_normal_refresh() -> None:
    state = {"fresh": False}
    calls: list[bool] = []
    normal_started = Event()
    release_normal = Event()

    def inspect(_requirement):
        return {"fresh": state["fresh"], "updated_at": NOW if state["fresh"] else None}

    def refresh(requirement, _progress):
        calls.append(requirement.force)
        if not requirement.force:
            normal_started.set()
            assert release_normal.wait(2), "普通刷新未在测试期间释放"
        state["fresh"] = True
        return RefreshStepResult(stage="market_context", status="success", fresh=2, total=2)

    coordinator = DataReadinessCoordinator(
        policy=_policy(),
        inspectors={"market_context": inspect},
        refreshers={"market_context": refresh},
    )
    try:
        requirement = DataRequirement("研究页", ("market_context",))
        forced = DataRequirement("数据中心", ("market_context",), force=True)
        with ThreadPoolExecutor(max_workers=2) as callers:
            normal_future = callers.submit(coordinator.ensure, requirement)
            assert normal_started.wait(2), "普通刷新没有启动"
            forced_future = callers.submit(coordinator.ensure, forced)
            release_normal.set()
            assert normal_future.result().status == "refreshed"
            assert forced_future.result().status == "refreshed"
        assert calls == [False, True]
    finally:
        coordinator.close()


def run() -> None:
    tests = [value for name, value in globals().items() if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
    print(f"data readiness tests passed: {len(tests)}")


if __name__ == "__main__":
    run()
