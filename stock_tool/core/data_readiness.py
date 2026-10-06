"""Feature-level data readiness and refresh coordination.

The Qt workbench has several user actions which depend on different slices of
public data.  This module keeps the dependency contract, freshness policy and
in-flight refresh de-duplication in the core layer so UI actions do not need
to know how a cache or immutable snapshot is stored.
"""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from threading import RLock
from typing import Any, Callable, Literal, Mapping

from stock_tool.core.cache import CacheStore
from stock_tool.core.data_refresh import RefreshStepResult
from stock_tool.core.data_snapshot import DataSnapshotStore, is_manifest_fresh
from stock_tool.core.factor_store import FACTOR_SNAPSHOT_MAX_AGE, V1FactorStore, update_factor_snapshots


DataKind = Literal[
    "quotes",
    "research_detail",
    "market_context",
    "factor_snapshot",
    "pit_incremental",
]
DATA_KINDS = frozenset(
    {"quotes", "research_detail", "market_context", "factor_snapshot", "pit_incremental"}
)
RESEARCH_MODES = frozenset({"formal", "review_only", "exploratory"})

ProgressCallback = Callable[[RefreshStepResult], None]
RefreshCallable = Callable[["DataRequirement", ProgressCallback | None], Any]
InspectCallable = Callable[["DataRequirement"], Any]


@dataclass(frozen=True)
class TradingDayRule:
    """A freshness window expressed as completed trading days."""

    days: int = 1


@dataclass(frozen=True)
class DataRequirement:
    feature: str
    kinds: tuple[DataKind, ...]
    codes: tuple[str, ...] = ()
    mode: str = "formal"
    force: bool = False
    reason: str = ""

    def normalized(self) -> "DataRequirement":
        feature = " ".join(str(self.feature or "").split()) or "当前功能"
        kinds: list[DataKind] = []
        for kind in self.kinds:
            normalized = str(kind or "").strip().lower()
            if normalized not in DATA_KINDS:
                raise ValueError(f"不支持的数据类型：{kind}")
            if normalized not in kinds:
                kinds.append(normalized)  # type: ignore[arg-type]
        if not kinds:
            raise ValueError("功能数据依赖不能为空")
        codes: list[str] = []
        for code in self.codes:
            text = str(code or "").strip()
            if not text:
                continue
            if not text.isdigit() or len(text) > 6:
                raise ValueError(f"股票代码格式无效：{code}")
            normalized_code = text.zfill(6)
            if normalized_code not in codes:
                codes.append(normalized_code)
        mode = str(self.mode or "formal").strip().lower() or "formal"
        if mode not in RESEARCH_MODES:
            raise ValueError(f"不支持的研究模式：{self.mode}")
        return DataRequirement(
            feature=feature,
            kinds=tuple(kinds),
            codes=tuple(sorted(codes)),
            mode=mode,
            force=bool(self.force),
            reason=" ".join(str(self.reason or "").split()),
        )


@dataclass(frozen=True)
class DataReadinessResult:
    status: Literal["ready", "refreshed", "blocked"]
    feature: str
    kinds: tuple[DataKind, ...]
    snapshot_id: str = ""
    refresh_event_id: str = ""
    asof_by_kind: dict[str, str] = field(default_factory=dict)
    stages: tuple[RefreshStepResult, ...] = ()
    errors: tuple[str, ...] = ()
    message: str = ""


@dataclass(frozen=True)
class _FreshnessObservation:
    fresh: bool
    updated_at: datetime | str | None = None
    snapshot_id: str = ""
    refresh_event_id: str = ""
    quality_status: str = "verified"
    errors: tuple[str, ...] = ()


DEFAULT_REFRESH_SETTINGS: dict[str, str] = {
    "feature_auto_refresh_enabled": "1",
    "feature_refresh_quote_ttl_seconds": "30",
    "feature_refresh_market_context_ttl_seconds": "300",
    "feature_refresh_history_ttl_seconds": "900",
    "feature_refresh_money_flow_ttl_seconds": "300",
    "feature_refresh_event_ttl_seconds": "3600",
    "feature_refresh_financial_ttl_seconds": "86400",
    "feature_refresh_factor_snapshot_trading_days": "1",
    "feature_refresh_pit_ttl_seconds": "86400",
}

_KIND_SETTING_KEYS = {
    "quotes": "feature_refresh_quote_ttl_seconds",
    "research_detail": "feature_refresh_history_ttl_seconds",
    "market_context": "feature_refresh_market_context_ttl_seconds",
    "pit_incremental": "feature_refresh_pit_ttl_seconds",
}
_DEFAULT_TTLS = {
    "quotes": timedelta(seconds=30),
    "research_detail": timedelta(seconds=900),
    "market_context": timedelta(seconds=300),
    "pit_incremental": timedelta(seconds=86400),
}
_DETAIL_TTL_SETTINGS = {
    "history": "feature_refresh_history_ttl_seconds",
    "money_flow": "feature_refresh_money_flow_ttl_seconds",
    "event": "feature_refresh_event_ttl_seconds",
    "financial": "feature_refresh_financial_ttl_seconds",
}


class DataFreshnessPolicy:
    """Centralized TTL and trading-day freshness decisions.

    ``settings`` may be a ``CacheStore``, a mapping, or any object exposing
    ``get_setting``.  Invalid values deliberately fall back to the fixed
    defaults instead of making a feature silently appear fresh.
    """

    def __init__(
        self,
        settings: Any | None = None,
        *,
        now_fn: Callable[[], datetime] | None = None,
        trading_day_fn: Callable[[date], tuple[bool, bool]] | None = None,
    ) -> None:
        self.settings = settings
        self._now = now_fn or (lambda: datetime.now().astimezone())
        self._trading_day_fn = trading_day_fn

    def _setting(self, key: str, default: str) -> str:
        try:
            if isinstance(self.settings, Mapping):
                return str(self.settings.get(key, default))
            getter = getattr(self.settings, "get_setting", None)
            if callable(getter):
                return str(getter(key, default))
        except Exception:
            return default
        return default

    def seconds_for(self, setting_key: str) -> int:
        default = DEFAULT_REFRESH_SETTINGS.get(setting_key, "0")
        try:
            value = int(self._setting(setting_key, default))
        except (TypeError, ValueError):
            value = int(default)
        if value <= 0:
            try:
                value = int(default)
            except (TypeError, ValueError):
                value = 1
        return value

    def ttl_for_detail(self, detail_kind: str) -> timedelta:
        key = _DETAIL_TTL_SETTINGS.get(str(detail_kind).strip().lower())
        if key is None:
            raise ValueError(f"不支持的详情数据类型：{detail_kind}")
        return timedelta(seconds=self.seconds_for(key))

    def ttl_for(self, kind: DataKind) -> timedelta | TradingDayRule:
        normalized = str(kind).strip().lower()
        if normalized == "factor_snapshot":
            return TradingDayRule(
                max(1, self._int_setting("feature_refresh_factor_snapshot_trading_days", 1))
            )
        if normalized not in _KIND_SETTING_KEYS:
            raise ValueError(f"不支持的数据类型：{kind}")
        return timedelta(seconds=self.seconds_for(_KIND_SETTING_KEYS[normalized]))

    def _int_setting(self, key: str, default: int) -> int:
        try:
            return int(self._setting(key, str(default)))
        except (TypeError, ValueError):
            return default

    def auto_refresh_enabled(self) -> bool:
        return self._setting("feature_auto_refresh_enabled", "1").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }

    def is_fresh(
        self,
        kind: DataKind,
        updated_at: datetime | str | None,
        *,
        now: datetime | None = None,
    ) -> bool:
        parsed = _parse_datetime(updated_at)
        if parsed is None:
            return False
        current = _as_naive_utc(now or self._now())
        stamp = _as_naive_utc(parsed)
        if stamp > current + timedelta(seconds=1):
            return False
        ttl = self.ttl_for(kind)
        if isinstance(ttl, timedelta):
            return current - stamp <= ttl
        return self._within_trading_days(stamp.date(), current.date(), ttl.days)

    def _within_trading_days(self, updated_date: date, current_date: date, days: int) -> bool:
        if updated_date > current_date:
            return False
        if updated_date == current_date:
            return True
        if self._trading_day_fn is None:
            # A trading-day rule cannot be safely evaluated from weekends
            # alone; fail closed and let the caller refresh or block.
            return False
        completed = 0
        cursor = updated_date + timedelta(days=1)
        while cursor <= current_date:
            is_day, known = self._trading_day_fn(cursor)
            if not known:
                return False
            if is_day:
                completed += 1
            if completed > max(0, int(days)):
                return False
            cursor += timedelta(days=1)
        return True


class DataReadinessCoordinator:
    """Check, refresh and atomically report data readiness for one feature."""

    def __init__(
        self,
        *,
        cache: CacheStore | None = None,
        fetcher: Any | None = None,
        factor_store: V1FactorStore | None = None,
        policy: DataFreshnessPolicy | None = None,
        refreshers: Mapping[DataKind, RefreshCallable] | None = None,
        inspectors: Mapping[DataKind, InspectCallable] | None = None,
        pit_refresher: RefreshCallable | None = None,
        executor: ThreadPoolExecutor | None = None,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self.cache = cache
        self.fetcher = fetcher
        self.factor_store = factor_store
        self.policy = policy or DataFreshnessPolicy(cache, now_fn=now_fn)
        self.refreshers = dict(refreshers or {})
        if pit_refresher is not None:
            self.refreshers["pit_incremental"] = pit_refresher
        self.inspectors = dict(inspectors or {})
        self._executor = executor or ThreadPoolExecutor(max_workers=4, thread_name_prefix="feature-refresh")
        self._owns_executor = executor is None
        self._lock = RLock()
        self._inflight: dict[tuple[DataKind, tuple[str, ...]], Future[DataReadinessResult]] = {}
        self._inflight_force: dict[tuple[DataKind, tuple[str, ...]], bool] = {}

    @property
    def inflight(self) -> dict[tuple[DataKind, tuple[str, ...]], Future[DataReadinessResult]]:
        """Read-only-by-convention view used by diagnostics and tests."""

        with self._lock:
            return dict(self._inflight)

    def close(self) -> None:
        if self._owns_executor:
            self._executor.shutdown(wait=False, cancel_futures=False)

    def ensure(
        self,
        requirement: DataRequirement,
        *,
        progress: ProgressCallback | None = None,
    ) -> DataReadinessResult:
        normalized = requirement.normalized()
        results = [
            self._ensure_kind(
                DataRequirement(
                    feature=normalized.feature,
                    kinds=(kind,),
                    codes=normalized.codes,
                    mode=normalized.mode,
                    force=normalized.force,
                    reason=normalized.reason,
                ),
                progress=progress,
            )
            for kind in normalized.kinds
        ]
        return self._combine_results(normalized, results)

    def inspect(self, requirement: DataRequirement) -> DataReadinessResult:
        """Read readiness without triggering network refreshes.

        Data center health panels use this method; feature actions should use
        :meth:`ensure` so a stale result cannot accidentally continue.
        """

        normalized = requirement.normalized()
        results: list[DataReadinessResult] = []
        for kind in normalized.kinds:
            single = DataRequirement(
                feature=normalized.feature,
                kinds=(kind,),
                codes=normalized.codes,
                mode=normalized.mode,
                reason=normalized.reason,
            )
            observation = self._inspect(single)
            results.append(
                self._result_from_observation(
                    single,
                    observation,
                    status="ready" if observation.fresh else "blocked",
                )
            )
        return self._combine_results(normalized, results)

    def _ensure_kind(
        self,
        requirement: DataRequirement,
        *,
        progress: ProgressCallback | None,
    ) -> DataReadinessResult:
        kind = requirement.kinds[0]
        key = (kind, () if kind in {"factor_snapshot", "market_context", "pit_incremental"} else requirement.codes)
        with self._lock:
            existing = self._inflight.get(key)
            existing_force = self._inflight_force.get(key, False)
        if existing is not None:
            result = existing.result()
            # A forced request may join an ordinary request, then perform the
            # required second pass after it completes.
            if requirement.force and not existing_force:
                return self._ensure_kind(requirement, progress=progress)
            return result

        before = self._inspect(requirement)
        if before.fresh and not requirement.force:
            return self._result_from_observation(requirement, before, status="ready")
        if not self.policy.auto_refresh_enabled() and not requirement.force:
            return self._blocked_without_refresh(requirement, before)

        with self._lock:
            existing = self._inflight.get(key)
            existing_force = self._inflight_force.get(key, False)
            if existing is not None:
                result = existing.result()
                if requirement.force and not existing_force:
                    return self._ensure_kind(requirement, progress=progress)
                return result
            future = self._executor.submit(self._run_refresh, requirement, progress)
            self._inflight[key] = future
            self._inflight_force[key] = requirement.force

            def cleanup(done: Future[DataReadinessResult]) -> None:
                with self._lock:
                    if self._inflight.get(key) is done:
                        self._inflight.pop(key, None)
                        self._inflight_force.pop(key, None)

            future.add_done_callback(cleanup)
        return future.result()

    def _run_refresh(
        self,
        requirement: DataRequirement,
        progress: ProgressCallback | None,
    ) -> DataReadinessResult:
        before = self._inspect(requirement)
        try:
            raw = self._refresh(requirement, progress)
            stages, refresh_errors, refresh_failed = self._normalize_refresh_result(requirement.kinds[0], raw)
        except Exception as exc:
            stages = (
                RefreshStepResult(
                    stage=requirement.kinds[0],  # type: ignore[arg-type]
                    status="failed",
                    failed=1,
                    total=max(1, len(requirement.codes)),
                    message=f"{requirement.feature}刷新异常",
                    errors=[str(exc)],
                ),
            )
            refresh_errors = (str(exc),)
            refresh_failed = True
        after = self._inspect(requirement)
        errors = list(refresh_errors)
        if refresh_failed:
            errors.extend(after.errors)
        elif not after.fresh:
            errors.extend(after.errors or before.errors or ("刷新完成后数据仍未达到新鲜度要求",))
        errors = tuple(dict.fromkeys(str(item) for item in errors if str(item).strip()))
        if refresh_failed or not after.fresh:
            return DataReadinessResult(
                status="blocked",
                feature=requirement.feature,
                kinds=requirement.kinds,
                snapshot_id=after.snapshot_id,
                refresh_event_id=after.refresh_event_id,
                asof_by_kind=_asof_map(requirement.kinds[0], after.updated_at),
                stages=stages,
                errors=errors,
                message=self._blocked_message(requirement, after, errors),
            )
        return DataReadinessResult(
            status="refreshed",
            feature=requirement.feature,
            kinds=requirement.kinds,
            snapshot_id=after.snapshot_id,
            refresh_event_id=after.refresh_event_id,
            asof_by_kind=_asof_map(requirement.kinds[0], after.updated_at),
            stages=stages,
            errors=errors,
            message=(
                f"{requirement.feature}已刷新并就绪，但存在数据缺口：{errors[0]}"
                if errors
                else f"{requirement.feature}所需数据已就绪。"
            ),
        )

    def _inspect(self, requirement: DataRequirement) -> _FreshnessObservation:
        kind = requirement.kinds[0]
        inspector = self.inspectors.get(kind)
        if inspector is not None:
            return _coerce_observation(inspector(requirement))
        if kind == "factor_snapshot":
            return self._inspect_factor_snapshot(requirement)
        if kind == "pit_incremental":
            return _FreshnessObservation(False, errors=("PIT 日增量没有可用状态适配器",))
        if self.cache is None:
            return _FreshnessObservation(False, errors=(f"{kind}没有可用缓存",))
        return self._inspect_cache_kind(requirement)

    def _inspect_factor_snapshot(self, requirement: DataRequirement) -> _FreshnessObservation:
        if self.factor_store is None:
            return _FreshnessObservation(False, errors=("V1 因子库不可用",))
        try:
            manifest = self.factor_store.latest_data_manifest()
        except Exception as exc:
            return _FreshnessObservation(False, errors=(f"读取因子快照失败：{exc}",))
        if manifest is None:
            return _FreshnessObservation(False, errors=("未找到不可变因子快照",))
        snapshot_store = DataSnapshotStore(self.factor_store.database)
        refresh_event_id = snapshot_store.refresh_event_id_for_snapshot(manifest.snapshot_id)
        errors: list[str] = []
        asof_at = getattr(manifest, "asof_at", None) or manifest.created_at
        if requirement.mode == "review_only":
            fresh = is_manifest_fresh(manifest, FACTOR_SNAPSHOT_MAX_AGE)
        else:
            # The batch creation time is merely a local commit timestamp.  A
            # cache fallback must not become a fresh formal snapshot simply
            # because it was committed again, so judge against source as-of.
            fresh = self.policy.is_fresh("factor_snapshot", asof_at)
        if not fresh:
            errors.append("因子快照不存在或已超过自动刷新有效期")
        if requirement.mode == "formal" and not refresh_event_id:
            errors.append("因子快照缺少刷新事件 ID，正式功能不能追溯本次数据准备")
            fresh = False
        try:
            snapshots = self.factor_store.list_snapshots_for_manifest(manifest.snapshot_id)
            available_codes = {str(item.code).zfill(6) for item in snapshots}
        except Exception as exc:
            return _FreshnessObservation(
                False,
                updated_at=asof_at,
                snapshot_id=manifest.snapshot_id,
                refresh_event_id=refresh_event_id,
                quality_status=manifest.quality_status,
                errors=(f"重建因子快照失败：{exc}",),
            )
        missing = [code for code in requirement.codes if code not in available_codes]
        if missing:
            errors.append(f"当前股票不在正式筛选快照范围：{', '.join(missing)}")
            if requirement.mode == "formal":
                fresh = False
        return _FreshnessObservation(
            fresh=fresh,
            updated_at=asof_at,
            snapshot_id=manifest.snapshot_id,
            refresh_event_id=refresh_event_id,
            quality_status=manifest.quality_status,
            errors=tuple(errors),
        )

    def _inspect_cache_kind(self, requirement: DataRequirement) -> _FreshnessObservation:
        kind = requirement.kinds[0]
        keys: list[tuple[str, timedelta, str]] = []
        if kind == "quotes":
            if requirement.codes:
                keys = [(f"feature_quote:{code}", self._ttl(kind), code) for code in requirement.codes]
            else:
                keys = [("feature_quote:market", self._ttl(kind), "market")]
        elif kind == "research_detail":
            for code in requirement.codes:
                marker = self.cache.get_json(f"feature_research_detail:{code}")
                marker_times = getattr(marker, "payload", {}) if marker is not None else {}
                if isinstance(marker_times, dict) and marker_times:
                    keys.extend(
                        [
                            (f"feature_research_detail:{code}:history", self.policy.ttl_for_detail("history"), f"{code}.K线"),
                            (f"feature_research_detail:{code}:financial", self.policy.ttl_for_detail("financial"), f"{code}.财务"),
                            (f"feature_research_detail:{code}:money_flow", self.policy.ttl_for_detail("money_flow"), f"{code}.资金流"),
                            (f"feature_research_detail:{code}:event", self.policy.ttl_for_detail("event"), f"{code}.事件"),
                        ]
                    )
                else:
                    keys.extend(
                        [
                            (f"history:{code}", self.policy.ttl_for_detail("history"), f"{code}.K线"),
                            (f"financial:{code}", self.policy.ttl_for_detail("financial"), f"{code}.财务"),
                            (f"money_flow:{code}:20", self.policy.ttl_for_detail("money_flow"), f"{code}.资金流"),
                            (f"events:{code}", self.policy.ttl_for_detail("event"), f"{code}.事件"),
                        ]
                    )
        elif kind == "market_context":
            marker = self.cache.get_json("feature_market_context")
            marker_times = getattr(marker, "payload", {}) if marker is not None else {}
            if isinstance(marker_times, dict) and marker_times:
                keys = [
                    ("feature_market_context:sentiment", self._ttl(kind), "市场情绪"),
                    ("feature_market_context:money_flow", self._ttl(kind), "市场资金流"),
                ]
            else:
                keys = [
                    ("market_sentiment", self._ttl(kind), "市场情绪"),
                    ("market_money_flow", self._ttl(kind), "市场资金流"),
                ]
        else:
            return _FreshnessObservation(False, errors=(f"{kind}没有内置缓存检查",))
        if not keys:
            return _FreshnessObservation(False, errors=(f"{kind}缺少目标股票",))
        timestamps: list[datetime | str] = []
        errors: list[str] = []
        for key, ttl, label in keys:
            if key.startswith("feature_research_detail:"):
                code, detail = key.rsplit(":", 2)[1:]
                marker = self.cache.get_json(f"feature_research_detail:{code}")
                marker_payload = getattr(marker, "payload", {}) if marker is not None else {}
                stamp = marker_payload.get(detail) if isinstance(marker_payload, dict) else None
                record = marker
            elif key.startswith("feature_market_context:"):
                detail = key.rsplit(":", 1)[-1]
                marker = self.cache.get_json("feature_market_context")
                marker_payload = getattr(marker, "payload", {}) if marker is not None else {}
                stamp = marker_payload.get(detail) if isinstance(marker_payload, dict) else None
                record = marker
            else:
                try:
                    record = self.cache.get_json(key)
                except Exception as exc:
                    record = None
                    errors.append(f"读取{label}缓存失败：{exc}")
                stamp = _payload_source_time(record, require_explicit_source_time=kind == "quotes")
            if record is None or stamp is None or not self._is_ttl_fresh(stamp, ttl):
                errors.append(f"{label}缺失或已过期")
            else:
                timestamps.append(stamp)
        return _FreshnessObservation(
            fresh=not errors,
            updated_at=max(timestamps, key=lambda item: _as_naive_utc(_parse_datetime(item) or datetime.min)) if timestamps else None,
            errors=tuple(errors),
        )

    def _ttl(self, kind: DataKind) -> timedelta:
        value = self.policy.ttl_for(kind)
        if not isinstance(value, timedelta):
            raise ValueError(f"{kind}不是时间 TTL")
        return value

    def _is_ttl_fresh(self, updated_at: datetime | str | None, ttl: timedelta) -> bool:
        parsed = _parse_datetime(updated_at)
        if parsed is None:
            return False
        now = _as_naive_utc(self.policy._now())
        stamp = _as_naive_utc(parsed)
        return stamp <= now + timedelta(seconds=1) and now - stamp <= ttl

    def _refresh(self, requirement: DataRequirement, progress: ProgressCallback | None) -> Any:
        kind = requirement.kinds[0]
        refresher = self.refreshers.get(kind)
        if refresher is not None:
            return refresher(requirement, progress)
        if kind == "factor_snapshot" and self.fetcher is not None and self.factor_store is not None:
            def factor_progress(done: int, total: int, message: str) -> None:
                if progress is not None:
                    progress(
                        RefreshStepResult(
                            stage="factor_snapshot",  # type: ignore[arg-type]
                            status="running",
                            done=done,
                            total=total,
                            message=message,
                        )
                    )

            return update_factor_snapshots(
                self.fetcher,
                self.factor_store,
                limit=300,
                progress=factor_progress,
                formal_mode=False,
            )
        if kind == "quotes":
            return self._refresh_quotes(requirement, progress)
        if kind == "research_detail":
            return self._refresh_research_detail(requirement, progress)
        if kind == "market_context":
            return self._refresh_market_context(requirement, progress)
        raise RuntimeError(f"{requirement.feature}缺少{kind}刷新适配器")

    def _refresh_quotes(self, requirement: DataRequirement, progress: ProgressCallback | None) -> RefreshStepResult:
        if self.fetcher is None or self.cache is None:
            raise RuntimeError("行情刷新适配器不可用")
        codes = list(requirement.codes)
        if codes and callable(getattr(self.fetcher, "quotes_by_codes", None)):
            snapshots = self.fetcher.quotes_by_codes(codes)
            missing: list[str] = []
            for code, snapshot in snapshots.items():
                quote = getattr(snapshot, "quote", snapshot)
                quote_at = getattr(snapshot, "quote_at", None)
                if quote_at is None:
                    missing.append(str(code).zfill(6))
                    continue
                self.cache.set_json(
                    f"feature_quote:{str(code).zfill(6)}",
                    {
                        "quote": getattr(quote, "__dict__", dict(quote) if isinstance(quote, dict) else {}),
                        "quote_at": _format_datetime(quote_at),
                        "source": str(getattr(snapshot, "source", "") or ""),
                        "fetched_at": _format_datetime(getattr(snapshot, "fetched_at", None)),
                    },
                )
            missing.extend(
                code
                for code in codes
                if code not in snapshots and code not in missing
            )
            missing = list(dict.fromkeys(missing))
            fresh = len(codes) - len(missing)
            status = "success" if fresh == len(codes) else ("partial" if fresh else "failed")
            step = RefreshStepResult(
                stage="quotes",  # type: ignore[arg-type]
                status=status,
                done=len(codes),
                total=len(codes),
                fresh=fresh,
                failed=len(missing),
                message=f"行情已准备 {fresh}/{len(codes)} 只。",
                errors=[f"{code}没有真实源时间或未返回行情" for code in missing],
            )
        else:
            quotes, updated_at = self.fetcher.spot_quotes(force=True)
            source_time = _format_datetime(updated_at)
            self.cache.set_json("feature_quote:market", {"count": len(quotes), "source_time": source_time})
            step = RefreshStepResult(
                stage="quotes",  # type: ignore[arg-type]
                status="success" if quotes and updated_at else "failed",
                done=len(quotes),
                total=len(quotes),
                fresh=1 if quotes and updated_at else 0,
                failed=0 if quotes and updated_at else 1,
                message=f"全市场行情已准备 {len(quotes)} 条。",
                errors=[] if quotes and updated_at else ["行情源没有返回有效更新时间"],
            )
        if progress is not None:
            progress(step)
        return step

    def _refresh_research_detail(self, requirement: DataRequirement, progress: ProgressCallback | None) -> RefreshStepResult:
        if self.fetcher is None:
            raise RuntimeError("个股研究数据刷新适配器不可用")
        total = len(requirement.codes) * 4
        done = fresh = failed = 0
        errors: list[str] = []
        for code in requirement.codes:
            source_times: dict[str, str] = {}
            calls = (
                ("history", "K线", lambda: self.fetcher.history(code, days=365, force=True)),
                ("financial", "财务", lambda: self.fetcher.financial_metrics(code, force=True)),
                ("money_flow", "资金流", lambda: self.fetcher.money_flow(code, days=20, force=True)),
                ("event", "事件", lambda: self.fetcher.event_catalysts(code, limit=6, force=True)),
            )
            for detail_kind, label, call in calls:
                try:
                    _value, updated_at = call()
                    if updated_at is None:
                        raise RuntimeError("缺少真实更新时间")
                    fresh += 1
                    source_times[detail_kind] = _format_datetime(updated_at)
                except Exception as exc:
                    failed += 1
                    errors.append(f"{code}{label}：{exc}")
                done += 1
                if progress is not None:
                    progress(
                        RefreshStepResult(
                            stage="research_detail",  # type: ignore[arg-type]
                            status="running",
                            done=done,
                            total=total,
                            fresh=fresh,
                            failed=failed,
                            message=f"正在准备个股研究数据 {done}/{total}",
                            errors=errors[-3:],
                        )
                    )
            if self.cache is not None and len(source_times) == len(calls):
                self.cache.set_json(f"feature_research_detail:{code}", source_times)
        status = "success" if failed == 0 and total else ("partial" if fresh else "failed")
        return RefreshStepResult(
            stage="research_detail",  # type: ignore[arg-type]
            status=status,
            done=done,
            total=total,
            fresh=fresh,
            failed=failed,
            message=f"个股研究数据已准备 {fresh}/{total} 项。",
            errors=errors[:10],
        )

    def _refresh_market_context(self, requirement: DataRequirement, progress: ProgressCallback | None) -> RefreshStepResult:
        if self.fetcher is None:
            raise RuntimeError("市场辅助数据刷新适配器不可用")
        done = fresh = failed = 0
        errors: list[str] = []
        source_times: dict[str, str] = {}
        try:
            _sentiment, updated_at = self.fetcher.market_sentiment(force=True)
            if updated_at is None:
                raise RuntimeError("市场情绪缺少更新时间")
            fresh += 1
            source_times["sentiment"] = _format_datetime(updated_at)
        except Exception as exc:
            failed += 1
            errors.append(f"市场情绪：{exc}")
        done += 1
        try:
            self.fetcher.market_money_flow(force=True)
            source_name = str(getattr(self.fetcher, "last_market_money_flow_source", "") or "").lower()
            if source_name in {"cache", "cache_stale"}:
                raise RuntimeError("市场资金流本次未获得新源数据")
            record = self.cache.get_json("market_money_flow") if self.cache is not None else None
            # The market-flow adapter has no upstream timestamp.  Its explicit
            # successful fetch time is usable only after rejecting cache
            # fallbacks above; a missing cache record is allowed for injected
            # test/adaptor implementations that return data directly.
            fetched_at = getattr(record, "updated_at", None) or self.policy._now()
            fresh += 1
            source_times["money_flow"] = _format_datetime(fetched_at)
        except Exception as exc:
            failed += 1
            errors.append(f"市场资金流：{exc}")
        done += 1
        if self.cache is not None and len(source_times) == 2:
            self.cache.set_json("feature_market_context", source_times)
        status = "success" if failed == 0 else ("partial" if fresh else "failed")
        step = RefreshStepResult(
            stage="market_context",  # type: ignore[arg-type]
            status=status,
            done=done,
            total=2,
            fresh=fresh,
            failed=failed,
            message="市场辅助数据准备完成。",
            errors=errors,
        )
        if progress is not None:
            progress(step)
        return step

    def _normalize_refresh_result(
        self,
        kind: DataKind,
        raw: Any,
    ) -> tuple[tuple[RefreshStepResult, ...], tuple[str, ...], bool]:
        if isinstance(raw, DataReadinessResult):
            return raw.stages, raw.errors, raw.status == "blocked"
        if isinstance(raw, RefreshStepResult):
            stages = (raw,)
        elif hasattr(raw, "stages"):
            stages = tuple(getattr(raw, "stages") or ())
        else:
            failed = int(getattr(raw, "failed", 0) or 0)
            success = int(getattr(raw, "success", 0) or 0)
            total = int(getattr(raw, "total", success + failed) or 0)
            errors = tuple(str(item) for item in (getattr(raw, "errors", ()) or ()) if str(item).strip())
            stages = (
                RefreshStepResult(
                    stage=kind,  # type: ignore[arg-type]
                    status="success" if failed == 0 else ("partial" if success else "failed"),
                    done=success + failed,
                    total=total,
                    fresh=success,
                    failed=failed,
                    message=str(getattr(raw, "message", "") or f"{kind}刷新完成"),
                    errors=list(errors),
                ),
            )
        normalized_stages: list[RefreshStepResult] = []
        errors: list[str] = []
        failed = False
        for stage in stages:
            if not isinstance(stage, RefreshStepResult):
                continue
            normalized_stages.append(stage)
            errors.extend(stage.errors)
            failed = failed or stage.status == "failed"
        return tuple(normalized_stages), tuple(dict.fromkeys(errors)), failed

    def _result_from_observation(
        self,
        requirement: DataRequirement,
        observation: _FreshnessObservation,
        *,
        status: Literal["ready", "refreshed", "blocked"],
    ) -> DataReadinessResult:
        return DataReadinessResult(
            status=status,
            feature=requirement.feature,
            kinds=requirement.kinds,
            snapshot_id=observation.snapshot_id,
            refresh_event_id=observation.refresh_event_id,
            asof_by_kind=_asof_map(requirement.kinds[0], observation.updated_at),
            errors=observation.errors if status == "blocked" else (),
            message=(
                f"{requirement.feature}数据已就绪。"
                if status != "blocked"
                else self._blocked_message(requirement, observation, observation.errors)
            ),
        )

    def _blocked_without_refresh(
        self,
        requirement: DataRequirement,
        observation: _FreshnessObservation,
    ) -> DataReadinessResult:
        errors = tuple(dict.fromkeys((*observation.errors, "功能按需自动刷新已关闭")))
        return DataReadinessResult(
            status="blocked",
            feature=requirement.feature,
            kinds=requirement.kinds,
            snapshot_id=observation.snapshot_id,
            refresh_event_id=observation.refresh_event_id,
            asof_by_kind=_asof_map(requirement.kinds[0], observation.updated_at),
            errors=errors,
            message=self._blocked_message(requirement, observation, errors),
        )

    def _blocked_message(
        self,
        requirement: DataRequirement,
        observation: _FreshnessObservation,
        errors: tuple[str, ...],
    ) -> str:
        reason = errors[0] if errors else "数据源刷新失败"
        latest = _format_datetime(observation.updated_at) or "无"
        suffix = "；数据未就绪，未发起 AI 请求" if "AI" in requirement.feature else ""
        return (
            f"{requirement.feature}已阻断：{reason}。最近成功时间：{latest}。"
            f"请在数据中心执行对应手动刷新或检查公开数据源。{suffix}"
        )

    def _combine_results(
        self,
        requirement: DataRequirement,
        results: list[DataReadinessResult],
    ) -> DataReadinessResult:
        blocked = [item for item in results if item.status == "blocked"]
        status: Literal["ready", "refreshed", "blocked"] = "blocked" if blocked else (
            "refreshed" if any(item.status == "refreshed" for item in results) else "ready"
        )
        snapshot_id = next((item.snapshot_id for item in results if item.snapshot_id), "")
        refresh_event_id = next((item.refresh_event_id for item in results if item.refresh_event_id), "")
        asof: dict[str, str] = {}
        stages: list[RefreshStepResult] = []
        errors: list[str] = []
        for item in results:
            asof.update(item.asof_by_kind)
            stages.extend(item.stages)
            errors.extend(item.errors)
        errors_tuple = tuple(dict.fromkeys(errors))
        if status == "blocked":
            message = next((item.message for item in blocked if item.message), f"{requirement.feature}已阻断")
        elif status == "refreshed":
            message = (
                f"{requirement.feature}数据已刷新并就绪，但存在数据缺口：{errors[0]}"
                if errors
                else f"{requirement.feature}数据已刷新并就绪。"
            )
        else:
            message = f"{requirement.feature}数据已就绪。"
        return DataReadinessResult(
            status=status,
            feature=requirement.feature,
            kinds=requirement.kinds,
            snapshot_id=snapshot_id,
            refresh_event_id=refresh_event_id,
            asof_by_kind=asof,
            stages=tuple(stages),
            errors=errors_tuple,
            message=message,
        )


def _coerce_observation(value: Any) -> _FreshnessObservation:
    if isinstance(value, _FreshnessObservation):
        return value
    if isinstance(value, Mapping):
        return _FreshnessObservation(
            fresh=bool(value.get("fresh", False)),
            updated_at=value.get("updated_at"),
            snapshot_id=str(value.get("snapshot_id", "") or ""),
            refresh_event_id=str(value.get("refresh_event_id", "") or ""),
            quality_status=str(value.get("quality_status", "verified") or "verified"),
            errors=tuple(str(item) for item in (value.get("errors") or ()) if str(item).strip()),
        )
    raise TypeError("数据新鲜度检查器必须返回 FreshnessObservation 或字典")


def _payload_source_time(
    record: Any,
    *,
    require_explicit_source_time: bool = False,
) -> datetime | str | None:
    if record is None:
        return None
    payload = getattr(record, "payload", None)
    if isinstance(payload, dict):
        keys = ("quote_at", "source_time") if require_explicit_source_time else (
            "quote_at",
            "source_time",
            "updated_at",
            "fetched_at",
        )
        for key in keys:
            if payload.get(key):
                return payload[key]
    if require_explicit_source_time:
        return None
    return getattr(record, "updated_at", None)


def _parse_datetime(value: datetime | str | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _as_naive_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _format_datetime(value: datetime | str | None) -> str:
    parsed = _parse_datetime(value)
    if parsed is None:
        return str(value or "")
    return parsed.isoformat(timespec="seconds")


def _asof_map(kind: DataKind, value: datetime | str | None) -> dict[str, str]:
    formatted = _format_datetime(value)
    return {kind: formatted} if formatted else {}
