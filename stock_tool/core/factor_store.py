from __future__ import annotations

import json
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from stock_tool.core.data_snapshot import (
    DataSnapshotFact,
    DataSnapshotManifest,
    DataSnapshotStore,
    content_hash,
    fact_set_digest,
)
from stock_tool.core.data_fetcher import DataFetcher
from stock_tool.core.models import FinancialMetrics, StockQuote
from stock_tool.core.selection_policy import SelectionPolicyInput
from stock_tool.core.technical_profile import TechnicalProfile, build_technical_profile
from stock_tool.core.v1_database import V1Database


ProgressCallback = Callable[[int, int, str], None]

FACTOR_FIELDS = [
    "price",
    "pe",
    "pe_ttm",
    "pb",
    "market_cap_yi",
    "roe",
    "gross_margin",
    "net_margin",
    "debt_ratio",
    "revenue_growth",
    "profit_growth",
    "trend_20d",
    "trend_60d",
    "volume_ratio",
    "turnover",
    "box_score",
    "box_range_pct",
    "industry_strength",
]
FACTOR_SNAPSHOT_MAX_AGE = timedelta(hours=72)
LIVE_FACTOR_SNAPSHOT_SOURCE = "v1_factor_snapshot_live"
TECHNICAL_AVAILABLE_STATUSES = {"triggered", "candidate", "avoid"}


@dataclass
class FactorSnapshot:
    code: str
    name: str
    price: float | None = None
    pe: float | None = None
    pe_ttm: float | None = None
    pb: float | None = None
    market_cap_yi: float | None = None
    roe: float | None = None
    gross_margin: float | None = None
    net_margin: float | None = None
    debt_ratio: float | None = None
    revenue_growth: float | None = None
    profit_growth: float | None = None
    trend_20d: float | None = None
    trend_60d: float | None = None
    volume_ratio: float | None = None
    turnover: float | None = None
    box_score: float | None = None
    box_upper: float | None = None
    box_lower: float | None = None
    box_range_pct: float | None = None
    box_position_pct: float | None = None
    box_position_label: str = ""
    box_days: int = 0
    box_ma20_slope_pct: float | None = None
    box_boundary_touch_count: int = 0
    box_is_sideways: bool = False
    box_summary: str = ""
    industry: str = ""
    industry_strength: float | None = None
    industry_rank: int | None = None
    industry_sample_count: int | None = None
    industry_updated_at: str = ""
    technical_label: str = ""
    technical_status: str = ""
    technical_summary: str = ""
    risk_flags_json: str = "[]"
    data_date: str = ""
    quote_updated_at: str = ""
    report_date: str = ""
    financial_updated_at: str = ""
    history_updated_at: str = ""
    pe_ttm_updated_at: str = ""
    pe_ttm_source: str = ""
    pb_updated_at: str = ""
    pb_source: str = ""
    updated_at: str = ""
    missing_fields: str = ""

    def to_quote(self) -> StockQuote:
        market_cap = self.market_cap_yi * 100000000 if self.market_cap_yi is not None else None
        return StockQuote(
            code=self.code,
            name=self.name,
            price=self.price,
            market_cap=market_cap,
            pe=self.pe,
            pb=self.pb,
            pe_ttm=self.pe_ttm,
            turnover=self.turnover,
            industry=self.industry,
            industry_strength=self.industry_strength,
            industry_rank=self.industry_rank,
            industry_sample_count=self.industry_sample_count,
            industry_updated_at=self.industry_updated_at,
        )

    def to_metrics(self) -> FinancialMetrics:
        return FinancialMetrics(
            roe=self.roe,
            gross_margin=self.gross_margin,
            net_margin=self.net_margin,
            debt_ratio=self.debt_ratio,
            revenue_growth=self.revenue_growth,
            profit_growth=self.profit_growth,
            report_date=self.report_date,
        )

    def to_technical(self) -> TechnicalProfile:
        try:
            risk_flags = json.loads(self.risk_flags_json or "[]")
        except json.JSONDecodeError:
            risk_flags = []
        return TechnicalProfile(
            available=self.technical_status in TECHNICAL_AVAILABLE_STATUSES,
            status=self.technical_status or "missing",
            close=self.price,
            volume_ratio=self.volume_ratio,
            box_score=self.box_score,
            box_upper=self.box_upper,
            box_lower=self.box_lower,
            box_range_pct=self.box_range_pct,
            box_position_pct=self.box_position_pct,
            box_position_label=self.box_position_label,
            box_days=int(self.box_days or 0),
            box_ma20_slope_pct=self.box_ma20_slope_pct,
            box_boundary_touch_count=int(self.box_boundary_touch_count or 0),
            box_is_sideways=bool(self.box_is_sideways),
            box_summary=self.box_summary,
            opportunity_label=self.technical_label or "技术画像缺失",
            risk_flags=risk_flags if isinstance(risk_flags, list) else [],
            summary=self.technical_summary,
        )


@dataclass(frozen=True)
class FactorStoreStats:
    snapshot_count: int
    latest_updated_at: str
    missing_field_count: int
    missing_snapshot_count: int


@dataclass(frozen=True)
class FactorUpdateResult:
    total: int
    success: int
    failed: int
    errors: list[str]
    latest_updated_at: str
    data_snapshot_id: str = ""
    refresh_event_id: str = ""


@dataclass(frozen=True)
class DataHealthStatus:
    level: str
    title: str
    message: str
    risk_hint: str
    snapshot_count: int
    latest_updated_at: str
    missing_snapshot_count: int
    missing_field_count: int
    missing_ratio: float
    data_source: str = "本地 V1 因子库"


class V1FactorStore:
    def __init__(self, database: V1Database) -> None:
        self.database = database

    def upsert_snapshot(self, snapshot: FactorSnapshot) -> None:
        self.upsert_snapshots([snapshot])

    def upsert_snapshots(self, snapshots: list[FactorSnapshot]) -> None:
        if not snapshots:
            return
        with self.database.connect() as conn:
            self._upsert_snapshots(conn, snapshots)

    def commit_snapshot(
        self,
        snapshots: list[FactorSnapshot],
        manifest: DataSnapshotManifest,
        facts: list[DataSnapshotFact],
    ) -> None:
        """Atomically persist immutable evidence and its current materialization."""

        if not snapshots:
            raise ValueError("不能提交空的因子快照批次")
        with self.database.connect() as conn:
            DataSnapshotStore(self.database).insert_snapshot(conn, manifest, facts)
            self._upsert_snapshots(conn, snapshots)

    def _upsert_snapshots(self, conn, snapshots: list[FactorSnapshot]) -> None:
        columns = list(asdict(snapshots[0]).keys())
        placeholders = ", ".join("?" for _ in columns)
        updates = ", ".join(f"{column} = excluded.{column}" for column in columns if column != "code")
        sql = f"""
            INSERT INTO factor_snapshots({", ".join(columns)})
            VALUES({placeholders})
            ON CONFLICT(code) DO UPDATE SET {updates}
        """
        rows = [tuple(asdict(snapshot)[column] for column in columns) for snapshot in snapshots]
        conn.executemany(sql, rows)

    def list_latest_snapshots(self, limit: int | None = None, max_age: timedelta | None = None) -> list[FactorSnapshot]:
        sql = "SELECT * FROM factor_snapshots ORDER BY market_cap_yi DESC, code ASC"
        params: list[object] = []
        if max_age is not None:
            cutoff = (datetime.now() - max_age).isoformat(timespec="seconds")
            sql = "SELECT * FROM factor_snapshots WHERE updated_at >= ? ORDER BY market_cap_yi DESC, code ASC"
            params.append(cutoff)
        if limit is not None:
            sql += " LIMIT ?"
            params.append(max(1, int(limit)))
        with self.database.connect() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [FactorSnapshot(**dict(row)) for row in rows]

    def list_fresh_snapshots(self, limit: int | None = None) -> list[FactorSnapshot]:
        return self.list_latest_snapshots(limit=limit, max_age=FACTOR_SNAPSHOT_MAX_AGE)

    def latest_data_manifest(self) -> DataSnapshotManifest | None:
        return DataSnapshotStore(self.database).latest_manifest()

    def list_snapshots_for_manifest(self, snapshot_id: str) -> list[FactorSnapshot]:
        values_by_code = DataSnapshotStore(self.database).fact_values_by_code(snapshot_id)
        snapshot_fields = {item.name for item in fields(FactorSnapshot)}
        snapshots: list[FactorSnapshot] = []
        for code, values in values_by_code.items():
            payload = {
                "code": code,
                **{field_name: value for field_name, value in values.items() if field_name in snapshot_fields},
            }
            try:
                snapshots.append(FactorSnapshot(**payload))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"不可从数据快照重建因子 {code}: {exc}") from exc
        snapshots.sort(
            key=lambda item: (
                item.market_cap_yi is None,
                -(item.market_cap_yi or 0),
                item.code,
            )
        )
        return snapshots[:]

    def list_selection_inputs_for_manifest(self, snapshot_id: str) -> dict[str, SelectionPolicyInput]:
        """Hydrate the complete typed policy input from one immutable manifest."""

        snapshot_store = DataSnapshotStore(self.database)
        manifest = snapshot_store.get_manifest(snapshot_id)
        if manifest is None:
            raise ValueError(f"找不到数据快照 manifest：{snapshot_id}")
        values_by_code = snapshot_store.fact_values_by_code(snapshot_id)
        lineage_by_code = snapshot_store.fact_lineage_by_code(snapshot_id)
        inputs: dict[str, SelectionPolicyInput] = {}
        for raw_code, values in values_by_code.items():
            code = str(raw_code).zfill(6)
            lineage = lineage_by_code.get(raw_code) or lineage_by_code.get(code) or {}
            inputs[code] = SelectionPolicyInput.from_fact_values(
                code=code,
                values=values,
                snapshot_id=manifest.snapshot_id,
                as_of=manifest.asof_at,
                snapshot_quality_status=manifest.quality_status,
                fact_lineage=lineage,
            )
        return inputs

    def latest_updated_at(self) -> str:
        with self.database.connect() as conn:
            row = conn.execute("SELECT MAX(updated_at) AS latest FROM factor_snapshots").fetchone()
        return (row["latest"] if row and row["latest"] else "")

    def stats(self) -> FactorStoreStats:
        missing_expr_parts = []
        for field in FACTOR_FIELDS:
            missing_expr_parts.append(f"CASE WHEN {field} IS NULL AND instr(',' || missing_fields || ',', ',{field},') = 0 THEN 1 ELSE 0 END")
        missing_expr_parts.extend(
            [
                "CASE WHEN (technical_label = '' OR technical_label = '技术画像缺失') AND instr(',' || missing_fields || ',', ',technical_label,') = 0 THEN 1 ELSE 0 END",
                "CASE WHEN (technical_label = '' OR technical_label = '技术画像缺失') AND instr(',' || missing_fields || ',', ',technical_status,') = 0 THEN 1 ELSE 0 END",
                "CASE WHEN missing_fields = '' THEN 0 ELSE length(missing_fields) - length(replace(missing_fields, ',', '')) + 1 END",
            ]
        )
        missing_total_expr = " + ".join(missing_expr_parts)
        sql = f"""
            SELECT
                COUNT(*) AS snapshot_count,
                COALESCE(MAX(updated_at), '') AS latest_updated_at,
                COALESCE(SUM({missing_total_expr}), 0) AS missing_field_count,
                COALESCE(SUM(CASE WHEN ({missing_total_expr}) > 0 THEN 1 ELSE 0 END), 0) AS missing_snapshot_count
            FROM factor_snapshots
        """
        with self.database.connect() as conn:
            row = conn.execute(sql).fetchone()
        return FactorStoreStats(
            snapshot_count=int(row["snapshot_count"] or 0),
            latest_updated_at=str(row["latest_updated_at"] or ""),
            missing_field_count=int(row["missing_field_count"] or 0),
            missing_snapshot_count=int(row["missing_snapshot_count"] or 0),
        )


def calculate_data_health_status(
    stats: FactorStoreStats | None,
    *,
    database_available: bool = True,
    error_message: str = "",
    update_errors: list[str] | None = None,
    now: datetime | None = None,
) -> DataHealthStatus:
    now = now or datetime.now()
    update_errors = update_errors or []
    if not database_available or stats is None:
        detail = f"：{error_message}" if error_message else ""
        return DataHealthStatus(
            level="unavailable",
            title="不可用",
            message=f"V1 数据库不可读{detail}；筛选仍可尝试实时回退。",
            risk_hint="本地因子不可用于确定性筛选，请先修复数据目录或更新筛选数据。",
            snapshot_count=0,
            latest_updated_at="",
            missing_snapshot_count=0,
            missing_field_count=0,
            missing_ratio=1.0,
        )
    if stats.snapshot_count <= 0:
        return DataHealthStatus(
            level="unavailable",
            title="不可用",
            message="本地 V1 因子库为空；筛选仍可尝试实时回退。",
            risk_hint="当前没有本地因子，建议先更新筛选数据。",
            snapshot_count=0,
            latest_updated_at=stats.latest_updated_at,
            missing_snapshot_count=stats.missing_snapshot_count,
            missing_field_count=stats.missing_field_count,
            missing_ratio=1.0,
        )

    missing_ratio = stats.missing_snapshot_count / stats.snapshot_count
    failure = "最近抓取无失败记录"
    if update_errors:
        failure = "最近抓取失败：" + "；".join(update_errors[:2])

    stale = True
    try:
        latest = datetime.fromisoformat(stats.latest_updated_at)
        stale = now - latest > timedelta(hours=72)
    except ValueError:
        stale = True

    base = (
        f"本地 V1 因子 {stats.snapshot_count} 条，"
        f"缺失字段股票 {stats.missing_snapshot_count} 只/{stats.missing_field_count} 项，{failure}。"
    )
    if missing_ratio > 0.30:
        return DataHealthStatus(
            level="missing_many",
            title="缺失较多",
            message=base,
            risk_hint="超过 30% 股票存在字段缺失，筛选结果可能漏选或排序偏弱。",
            snapshot_count=stats.snapshot_count,
            latest_updated_at=stats.latest_updated_at,
            missing_snapshot_count=stats.missing_snapshot_count,
            missing_field_count=stats.missing_field_count,
            missing_ratio=missing_ratio,
        )
    if stale:
        return DataHealthStatus(
            level="stale",
            title="可用但过旧",
            message=base,
            risk_hint="最近更新时间超过 72 小时，建议更新筛选数据后再复盘。",
            snapshot_count=stats.snapshot_count,
            latest_updated_at=stats.latest_updated_at,
            missing_snapshot_count=stats.missing_snapshot_count,
            missing_field_count=stats.missing_field_count,
            missing_ratio=missing_ratio,
        )
    return DataHealthStatus(
        level="good",
        title="良好",
        message=base,
        risk_hint="本地因子可用于当前筛选；仍需以公开披露和交易所数据为准。",
        snapshot_count=stats.snapshot_count,
        latest_updated_at=stats.latest_updated_at,
        missing_snapshot_count=stats.missing_snapshot_count,
        missing_field_count=stats.missing_field_count,
        missing_ratio=missing_ratio,
    )


def _iso(value: datetime | None) -> str:
    if value is None:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone(timedelta(hours=8)))
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def _pct_from_ma(close: float | None, ma: float | None) -> float | None:
    if close is None or ma in (None, 0):
        return None
    return round((close - ma) / ma * 100, 2)


def _candidate_pool(quotes: list[StockQuote], limit: int) -> list[StockQuote]:
    items = [
        quote for quote in quotes
        if quote.code
        and quote.name
        and not quote.name.upper().startswith(("退", "ST", "*ST"))
    ]
    items.sort(key=lambda item: item.market_cap or 0, reverse=True)
    return items[: max(1, limit)]


def _missing_fields(snapshot: FactorSnapshot) -> str:
    fields = [field for field in FACTOR_FIELDS if getattr(snapshot, field) is None]
    if not snapshot.technical_label or snapshot.technical_label == "技术画像缺失":
        for field in ["technical_label", "technical_status"]:
            if field not in fields:
                fields.append(field)
    return ",".join(fields)


_QUOTE_EVIDENCE_FIELDS = frozenset(
    {
        "price",
        "pe",
        "pe_ttm",
        "pb",
        "market_cap_yi",
        "data_date",
        "quote_updated_at",
        "pe_ttm_updated_at",
        "pe_ttm_source",
        "pb_updated_at",
        "pb_source",
    }
)
_FINANCIAL_EVIDENCE_FIELDS = frozenset(
    {
        "roe",
        "gross_margin",
        "net_margin",
        "debt_ratio",
        "revenue_growth",
        "profit_growth",
        "report_date",
        "financial_updated_at",
    }
)
_HISTORY_EVIDENCE_FIELDS = frozenset(
    {
        "trend_20d",
        "trend_60d",
        "volume_ratio",
        "turnover",
        "box_score",
        "box_upper",
        "box_lower",
        "box_range_pct",
        "box_position_pct",
        "box_position_label",
        "box_days",
        "box_ma20_slope_pct",
        "box_boundary_touch_count",
        "box_is_sideways",
        "box_summary",
        "technical_label",
        "technical_status",
        "technical_summary",
        "risk_flags_json",
        "history_updated_at",
    }
)
_INDUSTRY_EVIDENCE_FIELDS = frozenset(
    {
        "industry",
        "industry_strength",
        "industry_rank",
        "industry_sample_count",
        "industry_updated_at",
    }
)


def _evidence_domain(field_name: str) -> str:
    if field_name in _QUOTE_EVIDENCE_FIELDS:
        return "quote"
    if field_name in _FINANCIAL_EVIDENCE_FIELDS:
        return "financial"
    if field_name in _HISTORY_EVIDENCE_FIELDS:
        return "history"
    if field_name in _INDUSTRY_EVIDENCE_FIELDS:
        return "industry"
    return "identity"


def factor_snapshot_fact_payloads(
    snapshot: FactorSnapshot,
    *,
    source_versions: dict[str, str],
    quality_status: str = "verified",
    formal_mode: bool = False,
) -> list[dict[str, Any]]:
    """Convert one materialized factor row into explicit evidence facts."""

    missing = {field.strip() for field in (snapshot.missing_fields or "").split(",") if field.strip()}
    fallback_time = snapshot.updated_at or snapshot.data_date
    effective_at = snapshot.data_date or fallback_time
    if not effective_at or not fallback_time:
        raise ValueError(f"因子快照 {snapshot.code} 缺少可用于证据的时间")
    source_names = {
        "quote": "spot_quotes",
        "financial": "financial_metrics",
        "history": "historical_prices",
        "industry": "industry_context",
        "identity": "factor_snapshot_builder",
    }
    domain_times = {
        "quote": snapshot.quote_updated_at,
        "financial": snapshot.financial_updated_at,
        "history": snapshot.history_updated_at,
        "industry": snapshot.industry_updated_at,
    }
    if formal_mode:
        missing_domains = [name for name, value in domain_times.items() if not str(value or "").strip()]
        if missing_domains:
            raise ValueError(
                f"正式因子快照缺少真实 available_at：{', '.join(missing_domains)}"
            )
    available_times = {
        "quote": domain_times["quote"] or fallback_time,
        "financial": domain_times["financial"] or fallback_time,
        "history": domain_times["history"] or fallback_time,
        "industry": domain_times["industry"] or fallback_time,
        "identity": fallback_time,
    }
    payloads: list[dict[str, Any]] = []
    values = asdict(snapshot)
    values.pop("code", None)
    for field_name, value in values.items():
        domain = _evidence_domain(field_name)
        field_quality = "partial" if value is None or field_name in missing else quality_status
        payloads.append(
            {
                "code": snapshot.code,
                "field_name": field_name,
                "value": value,
                "effective_at": effective_at,
                "available_at": available_times[domain],
                "source": source_names[domain],
                "source_version": source_versions.get(domain, "v1"),
                "fetched_at": fallback_time,
                "quality_status": field_quality,
                "revision_id": "",
            }
        )
    return payloads


def build_factor_snapshot_manifest(
    snapshots: list[FactorSnapshot],
    *,
    quote_updated_at: datetime | None,
    total: int,
    failed: int,
    limit: int,
    availability_cutoff: str | None = None,
    universe_id: str = "a_share_listed_pit",
    universe_version: str = "",
    source_policy_version: str = "factor_snapshot_builder.v1",
    required_fields: tuple[str, ...] = (),
    formal_mode: bool = False,
) -> tuple[DataSnapshotManifest, list[DataSnapshotFact]]:
    """Build one auditable batch identity before writing current factor rows."""

    if not snapshots:
        raise ValueError("不能为空快照批次创建 manifest")
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    asof_at = _iso(quote_updated_at) or now
    cutoff = availability_cutoff or now
    source_versions = {
        "quote": "spot_quotes.v1",
        "financial": "financial_metrics.v1",
        "history": "historical_prices.v1",
        "industry": "industry_context.v1",
        "identity": "factor_snapshot_builder.v1",
    }
    payloads: list[dict[str, Any]] = []
    for snapshot in snapshots:
        payloads.extend(
            factor_snapshot_fact_payloads(
                snapshot,
                source_versions=source_versions,
                formal_mode=formal_mode,
            )
        )
    fact_total = len(payloads)
    verified_fact_count = sum(1 for item in payloads if item.get("quality_status") == "verified")
    partial_fact_count = sum(1 for item in payloads if item.get("quality_status") == "partial")
    unverified_fact_count = sum(1 for item in payloads if item.get("quality_status") == "unverified")
    invalid_fact_count = 0
    for item in payloads:
        for time_key in ("effective_at", "available_at", "fetched_at"):
            raw_time = str(item.get(time_key) or "").strip()
            try:
                datetime.fromisoformat(raw_time.replace("Z", "+00:00"))
            except (TypeError, ValueError):
                invalid_fact_count += 1
                break
    required_missing_fields: list[str] = []
    for snapshot in snapshots:
        missing = {
            field.strip()
            for field in (snapshot.missing_fields or "").split(",")
            if field.strip()
        }
        for field in required_fields:
            if field in missing or getattr(snapshot, field, None) is None:
                required_missing_fields.append(f"{snapshot.code}.{field}")
    failover_count = sum(1 for item in payloads if item.get("source_route") not in {None, "", "primary"})
    quality_domains: dict[str, dict[str, int]] = {}
    for item in payloads:
        domain = str(item.get("source") or "unknown")
        bucket = quality_domains.setdefault(domain, {"verified": 0, "partial": 0, "unverified": 0, "blocked": 0})
        status = str(item.get("quality_status") or "unverified")
        bucket[status] = bucket.get(status, 0) + 1
    if required_missing_fields or invalid_fact_count or unverified_fact_count or any(
        item.get("quality_status") == "blocked" for item in payloads
    ):
        quality_status = "blocked"
    elif failed or partial_fact_count or failover_count:
        quality_status = "partial"
    else:
        quality_status = "verified"
    quality_summary = {
        "total": total,
        "success": len(snapshots),
        "failed": failed,
        "coverage": round(len(snapshots) / total, 6) if total else 0.0,
        "scan_limit": limit,
        "field_lineage": "materialized_factor_snapshot.v1",
        "fact_total": fact_total,
        "verified_fact_count": verified_fact_count,
        "partial_fact_count": partial_fact_count,
        "unverified_fact_count": unverified_fact_count,
        "invalid_fact_count": invalid_fact_count,
        "required_missing_fields": sorted(set(required_missing_fields)),
        "quality_domains": quality_domains,
        "failover_count": failover_count,
    }
    manifest = DataSnapshotManifest.create(
        asof_at=asof_at,
        availability_cutoff=cutoff,
        data_source=LIVE_FACTOR_SNAPSHOT_SOURCE,
        universe_id=universe_id,
        universe_version=universe_version,
        dataset_version="factor_snapshot_builder.v1",
        source_policy_version=source_policy_version,
        facts_digest=fact_set_digest(payloads),
        source_versions=source_versions,
        quality_status=quality_status,
        quality_summary=quality_summary,
        created_at=now,
    )
    facts = [DataSnapshotFact(snapshot_id=manifest.snapshot_id, **payload) for payload in payloads]
    return manifest, facts


def build_snapshot_from_fetcher(
    fetcher: DataFetcher,
    quote: StockQuote,
    quote_updated_at: datetime | None = None,
    pit_price_factor: dict | None = None,
    metrics_override: FinancialMetrics | None = None,
    history_override: Any | None = None,
    history_updated_at_override: datetime | None = None,
    batch_mode: bool = False,
    history_missing_reason: str = "",
) -> FactorSnapshot:
    pit_data = pit_price_factor or {}
    metrics, financial_updated_at = (
        (metrics_override, datetime.now().astimezone())
        if metrics_override is not None
        else (
            (FinancialMetrics(), None)
            if batch_mode
            else fetcher.financial_metrics(quote.code)
        )
    )
    technical = TechnicalProfile(False, status="missing", opportunity_label="技术画像缺失", summary="技术画像缺失")
    history_updated_at: datetime | None = history_updated_at_override
    try:
        if batch_mode:
            if history_override is None:
                raise RuntimeError(history_missing_reason or f"TickFlow K线未返回（{quote.code}）")
            history = history_override
        elif history_override is None:
            history, history_updated_at = fetcher.history(quote.code, days=180)
        else:
            history = history_override
        technical = build_technical_profile(history)
    except Exception as exc:
        technical = TechnicalProfile(
            False,
            status="missing",
            opportunity_label="技术画像缺失",
            summary=f"技术画像失败：{history_missing_reason or exc}",
        )

    now = datetime.now().astimezone().isoformat(timespec="seconds")
    snapshot = FactorSnapshot(
        code=quote.code,
        name=quote.name,
        price=quote.price,
        pe=quote.pe,
        pe_ttm=pit_data.get("pe_ttm"),
        pb=pit_data.get("pb_mrq") if pit_data.get("pb_mrq") is not None else quote.pb,
        market_cap_yi=round(quote.market_cap / 100000000, 4) if quote.market_cap is not None else None,
        roe=metrics.roe,
        gross_margin=metrics.gross_margin,
        net_margin=metrics.net_margin,
        debt_ratio=metrics.debt_ratio,
        revenue_growth=metrics.revenue_growth,
        profit_growth=metrics.profit_growth,
        trend_20d=_pct_from_ma(technical.close, technical.ma20),
        trend_60d=_pct_from_ma(technical.close, technical.ma60),
        volume_ratio=technical.volume_ratio,
        turnover=quote.turnover,
        box_score=technical.box_score,
        box_upper=technical.box_upper,
        box_lower=technical.box_lower,
        box_range_pct=technical.box_range_pct,
        box_position_pct=technical.box_position_pct,
        box_position_label=technical.box_position_label,
        box_days=technical.box_days,
        box_ma20_slope_pct=technical.box_ma20_slope_pct,
        box_boundary_touch_count=technical.box_boundary_touch_count,
        box_is_sideways=technical.box_is_sideways,
        box_summary=technical.box_summary,
        industry=str(pit_data.get("industry") or getattr(quote, "industry", "") or ""),
        industry_strength=pit_data.get("industry_strength", getattr(quote, "industry_strength", None)),
        industry_rank=pit_data.get("industry_rank", getattr(quote, "industry_rank", None)),
        industry_sample_count=pit_data.get("industry_sample_count", getattr(quote, "industry_sample_count", None)),
        industry_updated_at=str(pit_data.get("industry_updated_at") or getattr(quote, "industry_updated_at", "") or ""),
        technical_label=technical.opportunity_label or "技术画像缺失",
        technical_status=technical.status or "missing",
        technical_summary=technical.summary,
        risk_flags_json=json.dumps(technical.risk_flags, ensure_ascii=False),
        data_date=(quote_updated_at or datetime.now()).date().isoformat(),
        quote_updated_at=_iso(quote_updated_at),
        report_date=str(metrics.report_date or ""),
        financial_updated_at=_iso(financial_updated_at),
        history_updated_at=_iso(history_updated_at),
        pe_ttm_updated_at=str(pit_data.get("trade_date") or ""),
        pe_ttm_source=str(pit_data.get("source") or ""),
        pb_updated_at=str(pit_data.get("trade_date") or quote_updated_at or ""),
        pb_source=str(pit_data.get("source") or "production_spot_quotes"),
        updated_at=now,
    )
    snapshot.missing_fields = _missing_fields(snapshot)
    return snapshot


def update_factor_snapshots(
    fetcher: DataFetcher,
    store: V1FactorStore,
    limit: int = 300,
    progress: ProgressCallback | None = None,
    formal_mode: bool = False,
) -> FactorUpdateResult:
    snapshot_store = DataSnapshotStore(store.database)
    source_policy_version = "factor_snapshot_builder.v1"
    source_versions = {
        "quote": "spot_quotes.v1",
        "financial": "financial_metrics.v1",
        "history": "historical_prices.v1",
        "industry": "industry_context.v1",
        "identity": "factor_snapshot_builder.v1",
    }
    started_at = datetime.now().astimezone().isoformat(timespec="seconds")
    refresh_event = snapshot_store.record_refresh_attempt(
        asof_at=started_at,
        availability_cutoff=started_at,
        data_source=LIVE_FACTOR_SNAPSHOT_SOURCE,
        universe_id="a_share_listed_pit",
        universe_version="pending",
        dataset_version="factor_snapshot_builder.v1",
        source_policy_version=source_policy_version,
        source_versions=source_versions,
        started_at=started_at,
    )
    try:
        # A newly requested immutable batch must first ask the public source
        # for a quote snapshot.  Reusing a warm cache here would give the new
        # manifest a new commit time while retaining stale market facts.
        try:
            quotes, quote_updated_at = fetcher.spot_quotes(force=True)
        except TypeError as exc:
            # Keep the public helper usable by pre-existing offline fixtures
            # that model the older no-argument protocol.  Production
            # DataFetcher always receives force=True above.
            if "force" not in str(exc):
                raise
            quotes, quote_updated_at = fetcher.spot_quotes()
    except Exception as exc:
        snapshot_store.finalize_refresh(
            refresh_event.refresh_event_id,
            status="failed",
            error_message=f"全市场行情获取失败：{exc}",
        )
        return FactorUpdateResult(
            total=0,
            success=0,
            failed=1,
            errors=[f"全市场行情获取失败：{exc}"],
            latest_updated_at=store.latest_updated_at(),
            refresh_event_id=refresh_event.refresh_event_id,
        )
    candidates = _candidate_pool(quotes, limit)
    universe_version = content_hash({"codes": sorted(quote.code for quote in candidates), "limit": int(limit)})
    cutoff_at = datetime.now().astimezone().isoformat(timespec="seconds")
    snapshot_store.update_refresh_attempt_context(
        refresh_event.refresh_event_id,
        asof_at=_iso(quote_updated_at) or cutoff_at,
        availability_cutoff=cutoff_at,
        universe_version=universe_version,
        source_versions=source_versions,
    )
    errors: list[str] = []
    success = 0
    failed = 0
    total = len(candidates)
    snapshots: list[FactorSnapshot] = []
    batch_metrics: dict[str, FinancialMetrics] = {}
    batch_history: dict[str, Any] = {}
    batch_started_at: datetime | None = None
    batch_mode = callable(getattr(fetcher, "has_tickflow", None)) and fetcher.has_tickflow()
    missing_financial_codes: list[str] = []
    missing_history_codes: list[str] = []
    if batch_mode:
        batch_started_at = datetime.now().astimezone()
        codes = [quote.code for quote in candidates]
        try:
            batch_metrics = fetcher.batch_financial_metrics(codes)
            missing_financial_codes = [code for code in codes if code not in batch_metrics]
            if missing_financial_codes:
                errors.append(f"TickFlow 财务无记录：{','.join(missing_financial_codes)}")
        except Exception as exc:
            errors.append(f"TickFlow 财务批量源失败，财务字段标记缺失：{exc}")
        try:
            batch_history = fetcher.batch_history(codes, days=180)
            missing_history_codes = [code for code in codes if code not in batch_history]
            if missing_history_codes:
                errors.append(f"TickFlow K线无记录：{','.join(missing_history_codes)}")
        except Exception as exc:
            errors.append(f"TickFlow K线批量源失败，技术字段标记缺失：{exc}")
    for idx, quote in enumerate(candidates, start=1):
        if progress:
            progress(idx, total, f"正在更新 {idx}/{total}：{quote.name} {quote.code}")
        try:
            snapshot = build_snapshot_from_fetcher(
                fetcher,
                quote,
                quote_updated_at=quote_updated_at,
                metrics_override=batch_metrics.get(quote.code) if batch_mode else None,
                history_override=batch_history.get(quote.code),
                history_updated_at_override=batch_started_at if quote.code in batch_history else None,
                batch_mode=batch_mode,
                history_missing_reason=(
                    f"TickFlow K线无记录（{quote.code}）"
                    if batch_mode and quote.code in missing_history_codes
                    else ""
                ),
            )
            snapshots.append(snapshot)
            success += 1
        except Exception as exc:
            failed += 1
            if len(errors) < 8:
                errors.append(f"{quote.code} {quote.name}: {exc}")

    data_snapshot_id = ""
    if snapshots:
        try:
            cutoff_at = datetime.now().astimezone().isoformat(timespec="seconds")
            snapshot_store.update_refresh_attempt_context(
                refresh_event.refresh_event_id,
                availability_cutoff=cutoff_at,
            )
            manifest, facts = build_factor_snapshot_manifest(
                snapshots,
                quote_updated_at=quote_updated_at,
                total=total,
                failed=failed,
                limit=limit,
                availability_cutoff=cutoff_at,
                universe_version=universe_version,
                source_policy_version=source_policy_version,
                formal_mode=formal_mode,
            )
            finalized = snapshot_store.finalize_refresh(
                refresh_event.refresh_event_id,
                facts,
                status="success" if manifest.quality_status == "verified" else "partial",
                quality_status=manifest.quality_status,
                quality_summary=json.loads(manifest.quality_summary_json),
                coverage=json.loads(manifest.quality_summary_json),
                source_versions=dict(manifest.source_versions),
                finished_at=datetime.now().astimezone().isoformat(timespec="seconds"),
                after_snapshot=lambda conn: store._upsert_snapshots(conn, snapshots),
            )
            data_snapshot_id = finalized.snapshot_id if finalized is not None else ""
        except Exception as exc:
            failed += success
            success = 0
            if len(errors) < 8:
                errors.append(f"快照批次提交失败：{exc}")
    else:
        snapshot_store.finalize_refresh(
            refresh_event.refresh_event_id,
            status="failed",
            error_message="没有成功生成任何因子快照",
        )

    return FactorUpdateResult(
        total=total,
        success=success,
        failed=failed,
        errors=errors,
        latest_updated_at=store.latest_updated_at(),
        data_snapshot_id=data_snapshot_id,
        refresh_event_id=refresh_event.refresh_event_id,
    )
