from __future__ import annotations

import hashlib
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Callable

from stock_tool.core.factor_store import (
    FACTOR_SNAPSHOT_MAX_AGE,
    FactorSnapshot,
    FactorUpdateResult,
    V1FactorStore,
    build_snapshot_from_fetcher,
)
from stock_tool.core.industry_strength import build_latest_industry_strength_report
from stock_tool.core.mvp_registry import ProductRegistry, load_product_registry
from stock_tool.core.mvp_resolver import UNIVERSE_NAMES, code_in_universe, normalize_stock_code
from stock_tool.core.pit_database import PITDatabase
from stock_tool.core.v1_database import V1Database


ProgressCallback = Callable[[int, int, str], None]


@dataclass(frozen=True)
class UniverseSnapshot:
    universe_id: str
    asof_date: str
    source: str
    source_version: str
    fetched_at: str
    membership_hash: str
    member_count: int
    status: str
    message: str
    members: tuple[str, ...]


@dataclass(frozen=True)
class ActivationStatus:
    active: bool
    universe_id: str
    template_id: str
    snapshot_coverage: float
    required_field_coverage: dict[str, float]
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class ShadowRolloutStatus:
    ready: bool
    complete_trading_days: int
    required_trading_days: int
    combinations_per_day: int
    reasons: tuple[str, ...]


class UniverseSnapshotStore:
    def __init__(self, database: V1Database) -> None:
        self.database = database

    def save(
        self,
        universe_id: str,
        members: list[tuple[str, str]],
        *,
        asof_date: str,
        source: str,
        source_version: str = "",
        fetched_at: str | None = None,
    ) -> UniverseSnapshot:
        if universe_id not in UNIVERSE_NAMES:
            raise ValueError(f"未知股票池：{universe_id}")
        normalized = sorted({(normalize_stock_code(code), str(name or "")) for code, name in members if normalize_stock_code(code)})
        if not normalized:
            raise ValueError(f"{UNIVERSE_NAMES[universe_id]}成分快照为空，拒绝覆盖旧快照")
        if universe_id == "hs300" and len(normalized) != 300:
            raise ValueError(f"沪深300成分应为300只，实际{len(normalized)}只，拒绝保存")
        fetched_at = fetched_at or datetime.now().astimezone().isoformat(timespec="seconds")
        digest = hashlib.sha256("\n".join(code for code, _name in normalized).encode("utf-8")).hexdigest()
        with self.database.connect() as conn:
            conn.execute(
                "DELETE FROM universe_memberships WHERE universe_id = ? AND asof_date = ? AND source = ?",
                (universe_id, asof_date, source),
            )
            conn.executemany(
                "INSERT INTO universe_memberships(universe_id, asof_date, source, code, name) VALUES(?, ?, ?, ?, ?)",
                [(universe_id, asof_date, source, code, name) for code, name in normalized],
            )
            conn.execute(
                """
                INSERT INTO universe_snapshots(
                    universe_id, asof_date, source, source_version, fetched_at,
                    membership_hash, member_count, status, message
                ) VALUES(?, ?, ?, ?, ?, ?, ?, 'complete', '')
                ON CONFLICT(universe_id, asof_date, source) DO UPDATE SET
                    source_version=excluded.source_version,
                    fetched_at=excluded.fetched_at,
                    membership_hash=excluded.membership_hash,
                    member_count=excluded.member_count,
                    status='complete', message=''
                """,
                (universe_id, asof_date, source, source_version, fetched_at, digest, len(normalized)),
            )
        return UniverseSnapshot(
            universe_id=universe_id,
            asof_date=asof_date,
            source=source,
            source_version=source_version,
            fetched_at=fetched_at,
            membership_hash=digest,
            member_count=len(normalized),
            status="complete",
            message="",
            members=tuple(code for code, _name in normalized),
        )

    def latest(self, universe_id: str) -> UniverseSnapshot | None:
        with self.database.connect() as conn:
            row = conn.execute(
                "SELECT * FROM universe_snapshots WHERE universe_id = ? AND status = 'complete' ORDER BY asof_date DESC, fetched_at DESC LIMIT 1",
                (universe_id,),
            ).fetchone()
            if not row:
                return None
            members = conn.execute(
                "SELECT code FROM universe_memberships WHERE universe_id = ? AND asof_date = ? AND source = ? ORDER BY code",
                (universe_id, row["asof_date"], row["source"]),
            ).fetchall()
        return UniverseSnapshot(
            universe_id=row["universe_id"],
            asof_date=row["asof_date"],
            source=row["source"],
            source_version=row["source_version"],
            fetched_at=row["fetched_at"],
            membership_hash=row["membership_hash"],
            member_count=int(row["member_count"]),
            status=row["status"],
            message=row["message"],
            members=tuple(item["code"] for item in members),
        )


def save_quote_universes(quotes: list, store: UniverseSnapshotStore, *, asof_date: str, source: str) -> dict[str, UniverseSnapshot]:
    all_a = [(item.code, item.name) for item in quotes if code_in_universe(item.code, "all_a")]
    chinext = [(item.code, item.name) for item in quotes if code_in_universe(item.code, "chinext")]
    return {
        "all_a": store.save("all_a", all_a, asof_date=asof_date, source=source),
        "chinext": store.save("chinext", chinext, asof_date=asof_date, source=source),
    }


def fetch_and_save_hs300(store: UniverseSnapshotStore) -> UniverseSnapshot:
    try:
        import akshare as ak
    except ImportError as exc:
        raise RuntimeError("缺少 akshare，无法更新沪深300成分快照") from exc
    frame = ak.index_stock_cons_csindex(symbol="000300")
    required = {"成分券代码", "成分券名称"}
    if frame is None or not required.issubset(set(frame.columns)):
        raise RuntimeError("沪深300接口缺少预期列，拒绝使用不完整结果")
    members = [(normalize_stock_code(row["成分券代码"]), str(row["成分券名称"])) for _idx, row in frame.iterrows()]
    raw_asof = str(frame.iloc[0].get("日期") or "").strip()
    asof_date = date.today().isoformat() if raw_asof.lower() in {"", "nan", "nat", "none"} else raw_asof[:10]
    return store.save(
        "hs300",
        members,
        asof_date=asof_date,
        source="akshare.index_stock_cons_csindex",
        source_version=str(getattr(ak, "__version__", "")),
    )


def _business_days_old(asof_date: str, today: date | None = None) -> int:
    try:
        current = datetime.fromisoformat(asof_date[:10]).date()
    except (ValueError, TypeError):
        return 9999
    today = today or date.today()
    if current >= today:
        return 0
    count = 0
    day = current
    while day < today:
        day += timedelta(days=1)
        if day.weekday() < 5:
            count += 1
    return count


def _required_field_is_fresh(snapshot: FactorSnapshot, field_name: str, today: date | None) -> bool:
    value = getattr(snapshot, field_name, None)
    if value is None:
        return False
    try:
        if field_name in {"pe_ttm", "pb"} and float(value) <= 0:
            return False
    except (TypeError, ValueError):
        return False
    if field_name == "pe_ttm":
        stamp = snapshot.pe_ttm_updated_at
    elif field_name == "pb":
        stamp = snapshot.pb_updated_at or snapshot.quote_updated_at
    elif field_name in {"trend_20d", "technical_profile_score"}:
        stamp = snapshot.history_updated_at
    else:
        stamp = snapshot.financial_updated_at
    return _business_days_old(str(stamp or "")[:10], today) <= 1


def _status_eligible(snapshot: FactorSnapshot) -> bool:
    name = str(snapshot.name or "").strip().upper()
    return bool(name) and not name.startswith(("退", "ST", "*ST"))


def activation_status(
    universe: UniverseSnapshot | None,
    snapshots: list[FactorSnapshot],
    template_id: str,
    *,
    registry: ProductRegistry | None = None,
    today: date | None = None,
    database: V1Database | None = None,
) -> ActivationStatus:
    registry = registry or load_product_registry()
    template = registry.template(template_id)
    reasons: list[str] = []
    if universe is None:
        return ActivationStatus(False, "", template_id, 0.0, {}, ("缺少股票池快照",))
    if _business_days_old(universe.asof_date, today) > 1:
        reasons.append(f"股票池快照已过期：{universe.asof_date}")
    member_set = set(universe.members)
    covered = [
        item for item in snapshots
        if normalize_stock_code(item.code) in member_set
        and _business_days_old(str(getattr(item, "updated_at", ""))[:10], today) <= 1
    ]
    snapshot_coverage = len({normalize_stock_code(item.code) for item in covered}) / max(1, universe.member_count)
    if snapshot_coverage < 0.95:
        reasons.append(f"因子快照覆盖率 {snapshot_coverage:.1%} 低于 95%")
    eligible_covered = [item for item in covered if _status_eligible(item)]
    field_coverage = {
        field_name: sum(_required_field_is_fresh(item, field_name, today) for item in eligible_covered) / max(1, len(eligible_covered))
        for field_name in template.required_fields
    }
    for field_name, coverage in field_coverage.items():
        if coverage < 0.90:
            reasons.append(f"required 字段 {field_name} 覆盖率 {coverage:.1%} 低于 90%")
    if database is not None:
        with database.connect() as conn:
            job = conn.execute(
                "SELECT status,success,failed,message FROM factor_update_jobs WHERE universe_id=? ORDER BY id DESC LIMIT 1",
                (universe.universe_id,),
            ).fetchone()
        if job is None:
            reasons.append("缺少全池因子更新任务记录")
        else:
            attempted = int(job["success"] or 0) + int(job["failed"] or 0)
            failure_rate = int(job["failed"] or 0) / max(1, attempted)
            if job["status"] != "completed" or failure_rate >= 0.05:
                reasons.append(f"最近批处理未通过失败率门控：{job['message'] or job['status']}")
    return ActivationStatus(
        active=not reasons,
        universe_id=universe.universe_id,
        template_id=template_id,
        snapshot_coverage=round(snapshot_coverage, 6),
        required_field_coverage={field: round(value, 6) for field, value in field_coverage.items()},
        reasons=tuple(reasons),
    )


def record_shadow_run(
    database: V1Database,
    *,
    run_date: str,
    template_id: str,
    universe_id: str,
    template_version: str,
    config_hash: str,
    universe_hash: str,
    result_hash: str,
    scores: list[float],
    status: str,
    message: str = "",
) -> None:
    if status not in {"passed", "failed"}:
        raise ValueError("shadow run status 必须为 passed/failed")
    if template_id not in {"quality", "growth", "value", "quality_value", "trend"} or universe_id not in UNIVERSE_NAMES:
        raise ValueError("shadow run 必须属于MVP的15个闭集组合")
    if status == "passed" and (not result_hash or not scores or any(score < 0 or score > 100 for score in scores)):
        raise ValueError("通过的shadow run必须有可复现结果哈希和0–100分数")
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    with database.connect() as conn:
        conn.execute(
            """
            INSERT INTO mvp_shadow_runs(
                run_date,template_id,universe_id,template_version,config_hash,
                universe_hash,result_hash,result_count,score_min,score_max,status,message,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(run_date,template_id,universe_id,config_hash) DO UPDATE SET
                template_version=excluded.template_version,
                universe_hash=excluded.universe_hash,
                result_hash=excluded.result_hash,
                result_count=excluded.result_count,
                score_min=excluded.score_min,
                score_max=excluded.score_max,
                status=excluded.status,
                message=excluded.message,
                created_at=excluded.created_at
            """,
            (
                run_date, template_id, universe_id, template_version, config_hash,
                universe_hash, result_hash, len(scores), min(scores) if scores else None,
                max(scores) if scores else None, status, message[:2000], now,
            ),
        )


def production_rollout_status(
    database: V1Database,
    config_hash: str,
    *,
    required_days: int = 5,
    combinations_per_day: int = 15,
) -> ShadowRolloutStatus:
    with database.connect() as conn:
        rows = conn.execute(
            """
            SELECT run_date,
                   COUNT(*) AS total,
                   SUM(CASE WHEN status='passed' THEN 1 ELSE 0 END) AS passed
            FROM mvp_shadow_runs
            WHERE config_hash=?
            GROUP BY run_date
            ORDER BY run_date DESC
            """,
            (config_hash,),
        ).fetchall()
    complete = [row["run_date"] for row in rows if int(row["total"]) == combinations_per_day and int(row["passed"]) == combinations_per_day]
    reasons: list[str] = []
    if len(complete) < required_days:
        reasons.append(f"完整影子交易日仅 {len(complete)}/{required_days}，每日必须通过 {combinations_per_day} 个组合")
    return ShadowRolloutStatus(
        ready=len(complete) >= required_days,
        complete_trading_days=len(complete),
        required_trading_days=required_days,
        combinations_per_day=combinations_per_day,
        reasons=tuple(reasons),
    )


class _StartRateLimiter:
    def __init__(self, min_interval: float = 0.8) -> None:
        self.min_interval = max(0.0, float(min_interval))
        self._lock = threading.Lock()
        self._last = 0.0

    def wait(self) -> None:
        with self._lock:
            delay = self.min_interval - (time.monotonic() - self._last)
            if delay > 0:
                time.sleep(delay)
            self._last = time.monotonic()


class _CircuitBreaker:
    def __init__(self, failure_threshold: int = 10, cooldown_seconds: float = 30.0) -> None:
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds
        self._lock = threading.Lock()
        self._consecutive_failures = 0
        self._open_until = 0.0

    def before_call(self) -> None:
        with self._lock:
            remaining = self._open_until - time.monotonic()
        if remaining > 0:
            time.sleep(remaining)

    def success(self) -> None:
        with self._lock:
            self._consecutive_failures = 0
            self._open_until = 0.0

    def failure(self) -> None:
        with self._lock:
            self._consecutive_failures += 1
            if self._consecutive_failures >= self.failure_threshold:
                self._open_until = max(self._open_until, time.monotonic() + self.cooldown_seconds)
                self._consecutive_failures = 0


def _start_job(database: V1Database, universe_id: str, codes: list[str]) -> int:
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    with database.connect() as conn:
        cursor = conn.execute(
            "INSERT INTO factor_update_jobs(universe_id,status,total,started_at,updated_at) VALUES(?, 'running', ?, ?, ?)",
            (universe_id, len(codes), now, now),
        )
        job_id = int(cursor.lastrowid)
        conn.executemany(
            "INSERT INTO factor_update_items(job_id,code,status,updated_at) VALUES(?, ?, 'pending', ?)",
            [(job_id, code, now) for code in codes],
        )
    return job_id


def _mark_job_item(database: V1Database, job_id: int, code: str, status: str, error: str = "") -> None:
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    with database.connect() as conn:
        conn.execute(
            "UPDATE factor_update_items SET status=?, error=?, updated_at=? WHERE job_id=? AND code=?",
            (status, error[:1000], now, job_id, code),
        )
        counts = {row["status"]: int(row["count"]) for row in conn.execute(
            "SELECT status,COUNT(*) AS count FROM factor_update_items WHERE job_id=? GROUP BY status", (job_id,)
        ).fetchall()}
        conn.execute(
            "UPDATE factor_update_jobs SET success=?, failed=?, updated_at=? WHERE id=?",
            (counts.get("success", 0), counts.get("failed", 0), now, job_id),
        )


def _finish_job(database: V1Database, job_id: int, success: int, failed: int) -> None:
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    rate = failed / max(1, success + failed)
    status = "completed" if rate < 0.05 else "failed_gate"
    message = f"成功{success}，失败{failed}，失败率{rate:.2%}"
    with database.connect() as conn:
        conn.execute(
            "UPDATE factor_update_jobs SET status=?,success=?,failed=?,updated_at=?,completed_at=?,message=? WHERE id=?",
            (status, success, failed, now, now, message, job_id),
        )


def update_universe_factor_snapshots(
    fetcher,
    store: V1FactorStore,
    database: V1Database,
    universe: UniverseSnapshot,
    *,
    pit_database: PITDatabase | None = None,
    max_workers: int = 2,
    refresh_existing: bool = False,
    progress: ProgressCallback | None = None,
) -> FactorUpdateResult:
    """Build a complete, resumable local universe snapshot without market-cap truncation."""
    quotes, quote_updated_at = fetcher.spot_quotes()
    member_set = set(universe.members)
    candidates = [item for item in quotes if normalize_stock_code(item.code) in member_set]
    candidates.sort(key=lambda item: str(item.code))
    existing = {item.code: item for item in store.list_latest_snapshots()}
    cutoff = datetime.now() - FACTOR_SNAPSHOT_MAX_AGE
    to_update = []
    for quote in candidates:
        current = existing.get(quote.code)
        fresh = False
        if current and current.updated_at:
            try:
                fresh = datetime.fromisoformat(current.updated_at) >= cutoff
            except ValueError:
                fresh = False
        if refresh_existing or not fresh:
            to_update.append(quote)
    pit_factors = pit_database.latest_price_factors()[1] if pit_database is not None else {}
    if pit_database is not None:
        industry_report = build_latest_industry_strength_report(pit_database)
        for code, info in industry_report.by_code.items():
            target = pit_factors.setdefault(code, {})
            target.update(
                {
                    "industry": info.industry,
                    "industry_strength": info.strength,
                    "industry_rank": info.rank,
                    "industry_sample_count": info.sample_count,
                    "industry_updated_at": info.updated_at,
                }
            )
    job_id = _start_job(database, universe.universe_id, [item.code for item in to_update])
    limiter = _StartRateLimiter()
    circuit = _CircuitBreaker()
    errors: list[str] = []
    success = 0
    failed = 0

    def build(quote):
        last_error: Exception | None = None
        for attempt in range(3):
            circuit.before_call()
            limiter.wait()
            try:
                snapshot = build_snapshot_from_fetcher(
                    fetcher,
                    quote,
                    quote_updated_at=quote_updated_at,
                    pit_price_factor=pit_factors.get(normalize_stock_code(quote.code)),
                )
                circuit.success()
                return snapshot
            except Exception as exc:
                last_error = exc
                circuit.failure()
                if attempt < 2:
                    time.sleep(2**attempt)
        assert last_error is not None
        raise last_error

    with ThreadPoolExecutor(max_workers=max(1, min(2, int(max_workers)))) as executor:
        futures = {executor.submit(build, quote): quote for quote in to_update}
        for index, future in enumerate(as_completed(futures), start=1):
            quote = futures[future]
            try:
                snapshot = future.result()
                store.upsert_snapshot(snapshot)
                success += 1
                _mark_job_item(database, job_id, quote.code, "success")
            except Exception as exc:
                failed += 1
                _mark_job_item(database, job_id, quote.code, "failed", str(exc))
                if len(errors) < 8:
                    errors.append(f"{quote.code} {quote.name}: {exc}")
            if progress:
                progress(index, len(to_update), f"正在更新{UNIVERSE_NAMES[universe.universe_id]} {quote.name} {quote.code}")
    _finish_job(database, job_id, success, failed)
    return FactorUpdateResult(
        total=len(to_update),
        success=success,
        failed=failed,
        errors=errors,
        latest_updated_at=store.latest_updated_at(),
    )
