from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Protocol

from stock_tool.core.professional_formula import (
    PROFESSIONAL_DEFAULT_DIMENSION_WEIGHTS,
    PROFESSIONAL_DEFAULT_FIELD_WEIGHTS,
    PROFESSIONAL_DIMENSION_FIELDS,
    PROFESSIONAL_DIMENSION_LABELS,
    PROFESSIONAL_DIMENSIONS,
    PROFESSIONAL_FORMULA_VERSION,
    PROFESSIONAL_MIN_SETTLED_SAMPLES,
    PROFESSIONAL_OBSERVATION_PERIOD_WEIGHTS,
    PROMPT_PROFESSIONAL_WEIGHT_CANDIDATE_V1,
    build_default_professional_formula,
    build_professional_memory_entries,
    build_professional_score_breakdown,
    build_professional_weight_candidate_prompt,
    derive_professional_weight_ranges,
    derive_recent_precise_weights,
    extract_professional_breakdown,
    json_dumps as _professional_json_dumps,
    normalize_dimension_weights,
    normalize_field_weights,
    propose_professional_shadow_candidate,
    summarize_performance_rows,
    validate_professional_candidate_payload,
)
from stock_tool.core.v1_database import V1Database
from stock_tool.core.formal_certificate import (
    FormalObservationBlocked,
    FormalScreeningCertificateStore,
    SAMPLE_CLASSES,
)


DEFAULT_OBSERVATION_TOP_N = 20
DEFAULT_OBSERVATION_HORIZONS = (3, 5, 10, 20)
HS300_CODE = "000300"

REGIME_TREND_BULL = "trend_bull"
REGIME_STRUCTURAL_BULL = "structural_bull"
REGIME_RANGE_BOUND = "range_bound"
REGIME_RISK_OFF = "risk_off"
REGIME_REBOUND = "rebound"
REGIME_UNCERTAIN = "uncertain"
REGIME_VALUES = {
    REGIME_TREND_BULL,
    REGIME_STRUCTURAL_BULL,
    REGIME_RANGE_BOUND,
    REGIME_RISK_OFF,
    REGIME_REBOUND,
    REGIME_UNCERTAIN,
}

BASELINE_WEIGHTS = {
    "quality": 0.30,
    "growth": 0.20,
    "safety": 0.20,
    "valuation": 0.20,
    "trend": 0.10,
    "industry": 0.00,
}

REGIME_TARGET_WEIGHTS = {
    REGIME_TREND_BULL: {
        "quality": 0.25,
        "growth": 0.25,
        "safety": 0.15,
        "valuation": 0.15,
        "trend": 0.20,
        "industry": 0.00,
    },
    REGIME_STRUCTURAL_BULL: {
        "quality": 0.25,
        "growth": 0.20,
        "safety": 0.15,
        "valuation": 0.15,
        "trend": 0.15,
        "industry": 0.10,
    },
    REGIME_RANGE_BOUND: {
        "quality": 0.30,
        "growth": 0.15,
        "safety": 0.25,
        "valuation": 0.25,
        "trend": 0.05,
        "industry": 0.00,
    },
    REGIME_RISK_OFF: {
        "quality": 0.30,
        "growth": 0.10,
        "safety": 0.30,
        "valuation": 0.25,
        "trend": 0.05,
        "industry": 0.00,
    },
    REGIME_REBOUND: {
        "quality": 0.25,
        "growth": 0.20,
        "safety": 0.20,
        "valuation": 0.15,
        "trend": 0.15,
        "industry": 0.05,
    },
    REGIME_UNCERTAIN: dict(BASELINE_WEIGHTS),
}

DIMENSION_FIELDS = {
    "quality": ("roe", "gross_margin", "net_margin"),
    "growth": ("revenue_growth", "profit_growth"),
    "safety": ("debt_ratio",),
    "valuation": ("pe_ttm", "pb"),
    "trend": ("trend_20d", "trend_60d", "box_score"),
    "industry": ("industry_strength",),
}

PROMPT_REGIME_EXPLAIN_V1 = "regime_explain_v1"
PROMPT_RETROSPECTIVE_V1 = "retrospective_v1"
PROMPT_WEIGHT_CANDIDATE_V1 = "weight_candidate_v1"


@dataclass(frozen=True)
class MarketRegimeSnapshot:
    asof_date: str
    regime: str
    confidence: float
    metrics: dict[str, Any] = field(default_factory=dict)
    source: str = "deterministic"
    created_at: str = ""


@dataclass(frozen=True)
class WeightPolicyVersion:
    id: int
    name: str
    version: str
    status: str
    baseline_weights: dict[str, float]
    regime_targets: dict[str, dict[str, float]]
    max_daily_delta: float
    min_confirm_days: int
    data_quality_note: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class StrategyRunContext:
    run_id: int
    asof_date: str
    regime: str
    regime_confidence: float
    weight_policy_id: int | None
    weight_policy_name: str
    weight_policy_version: str
    effective_weights: dict[str, float]
    shadow: bool
    ai_log_id: int | None
    data_quality_note: str
    created_at: str


@dataclass(frozen=True)
class ObservationSample:
    id: int
    result_id: int
    run_id: int
    horizon_days: int
    sample_date: str
    due_date: str
    code: str
    name: str
    entry_price: float | None
    rank_order: int
    local_score: float | None
    industry: str
    metrics: dict[str, Any]
    score_breakdown: dict[str, Any]
    ai_score: float | None
    ai_level: str
    created_at: str
    sample_class: str = "legacy"
    certificate_id: str | None = None


@dataclass(frozen=True)
class ObservationOutcome:
    sample_id: int
    settled_at: str
    exit_date: str = ""
    exit_price: float | None = None
    return_pct: float | None = None
    max_favorable_pct: float | None = None
    max_drawdown_pct: float | None = None
    hs300_return_pct: float | None = None
    excess_vs_hs300_pct: float | None = None
    industry_return_pct: float | None = None
    excess_vs_industry_pct: float | None = None
    direction_hit: bool | None = None
    benchmark_hit: bool | None = None
    rating: str = ""
    data_quality: str = "ok"
    data_quality_note: str = ""
    metrics: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PerformanceSummary:
    sample_count: int
    settled_count: int
    direction_hit_rate: float | None
    benchmark_hit_rate: float | None
    avg_return_pct: float | None
    avg_excess_vs_hs300_pct: float | None
    avg_max_drawdown_pct: float | None
    rating_counts: dict[str, int]
    data_quality_counts: dict[str, int]
    valid_settled_count: int = 0
    invalid_settled_count: int = 0


@dataclass(frozen=True)
class ClosedLoopDashboardSnapshot:
    asof_date: str
    recent_days: int
    due_count: int
    due_by_horizon: dict[int, int]
    sample_count: int
    settled_count: int
    direction_hit_rate: float | None
    benchmark_hit_rate: float | None
    avg_excess_vs_hs300_pct: float | None
    period_summaries: dict[int, PerformanceSummary]
    strongest_strategy: str
    strongest_excess_pct: float | None
    weakest_strategy: str
    weakest_excess_pct: float | None
    data_gap_count: int
    data_quality_counts: dict[str, int]
    shadow_policy_count: int
    latest_shadow_policy: str
    next_action: str
    valid_settled_count: int = 0
    invalid_settled_count: int = 0


@dataclass(frozen=True)
class PriceBar:
    trade_date: str
    close: float
    high: float | None = None
    low: float | None = None


@dataclass(frozen=True)
class PriceWindow:
    code: str
    source: str
    bars: list[PriceBar]


class PriceSource(Protocol):
    def price_window(self, code: str, start_date: str, horizon_days: int) -> PriceWindow:
        ...

    def benchmark_window(self, start_date: str, horizon_days: int) -> PriceWindow:
        ...

    def industry_return(self, industry: str, start_date: str, horizon_days: int) -> float | None:
        ...


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _json_dumps(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _json_loads(value: str | None, default: Any) -> Any:
    try:
        payload = json.loads(value or "")
    except (TypeError, json.JSONDecodeError):
        return default
    return payload if payload is not None else default


def _to_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number


def _date_part(value: str | date | datetime | None) -> str:
    if value is None:
        return date.today().isoformat()
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value).strip()
    if not text:
        return date.today().isoformat()
    return text[:10]


def _parse_date(value: str) -> date:
    return datetime.fromisoformat(_date_part(value)).date()


def add_trading_days(start: str, trading_days: int) -> str:
    current = _parse_date(start)
    remaining = max(0, int(trading_days))
    while remaining > 0:
        current += timedelta(days=1)
        if current.weekday() < 5:
            remaining -= 1
    return current.isoformat()


def normalize_weights(weights: dict[str, float], dimensions: dict[str, float] | None = None) -> dict[str, float]:
    base = dimensions or BASELINE_WEIGHTS
    cleaned = {key: max(0.0, float(weights.get(key, 0.0) or 0.0)) for key in base}
    total = sum(cleaned.values())
    if total <= 0:
        return dict(base)
    return {key: round(value / total, 6) for key, value in cleaned.items()}


def target_weights_for_regime(regime: str) -> dict[str, float]:
    return dict(REGIME_TARGET_WEIGHTS.get(regime, BASELINE_WEIGHTS))


def redistribute_missing_dimensions(
    weights: dict[str, float],
    available_fields: set[str],
) -> tuple[dict[str, float], str]:
    missing_dimensions = [
        dimension
        for dimension, fields in DIMENSION_FIELDS.items()
        if not any(field in available_fields for field in fields)
    ]
    if not missing_dimensions:
        return normalize_weights(weights), ""
    adjusted = dict(weights)
    missing_weight = sum(float(adjusted.get(dimension, 0.0) or 0.0) for dimension in missing_dimensions)
    for dimension in missing_dimensions:
        adjusted[dimension] = 0.0
    receivers = [dimension for dimension in DIMENSION_FIELDS if dimension not in missing_dimensions]
    receiver_total = sum(float(adjusted.get(dimension, 0.0) or 0.0) for dimension in receivers)
    if receivers and missing_weight > 0:
        if receiver_total <= 0:
            share = missing_weight / len(receivers)
            for dimension in receivers:
                adjusted[dimension] = float(adjusted.get(dimension, 0.0) or 0.0) + share
        else:
            for dimension in receivers:
                current = float(adjusted.get(dimension, 0.0) or 0.0)
                adjusted[dimension] = current + missing_weight * current / receiver_total
    note = "缺少维度字段，已重分配：" + "、".join(missing_dimensions)
    return normalize_weights(adjusted), note


def smooth_weight_transition(
    current_weights: dict[str, float],
    target_weights: dict[str, float],
    *,
    baseline_weights: dict[str, float] | None = None,
    confidence: float,
    confirmed_days: int,
    max_daily_delta: float = 0.03,
    min_confirm_days: int = 3,
    max_baseline_deviation: float = 0.12,
) -> dict[str, float]:
    baseline = normalize_weights(baseline_weights or BASELINE_WEIGHTS)
    current = normalize_weights(current_weights or baseline)
    if confidence < 0.65 or confirmed_days < min_confirm_days:
        return current
    target = normalize_weights(target_weights or baseline)
    next_weights: dict[str, float] = {}
    for dimension, base_value in baseline.items():
        raw_target = target.get(dimension, base_value)
        bounded_target = max(base_value - max_baseline_deviation, min(base_value + max_baseline_deviation, raw_target))
        current_value = current.get(dimension, base_value)
        delta = max(-max_daily_delta, min(max_daily_delta, bounded_target - current_value))
        next_weights[dimension] = round(current_value + delta, 6)
    return normalize_weights(next_weights, baseline)


def classify_observation_rating(
    return_pct: float | None,
    max_favorable_pct: float | None,
    max_drawdown_pct: float | None,
    excess_vs_hs300_pct: float | None,
) -> str:
    if return_pct is None or max_drawdown_pct is None:
        return ""
    if return_pct < -8 or max_drawdown_pct < -15:
        return "F"
    beat_hs300 = excess_vs_hs300_pct is not None and excess_vs_hs300_pct > 0
    if return_pct > 0 and beat_hs300 and max_drawdown_pct > -8 and (max_favorable_pct or 0) >= 5:
        return "A"
    if return_pct > 0 and beat_hs300:
        return "B"
    if return_pct > 0 or (return_pct >= -2 and max_drawdown_pct > -8):
        return "C"
    return "D"


class PerformanceStore:
    def __init__(self, database: V1Database) -> None:
        self.database = database

    def ensure_default_weight_policy(self) -> WeightPolicyVersion:
        now = _now()
        with self.database.connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO weight_policy_versions(
                    name, version, status, baseline_weights_json, regime_targets_json,
                    max_daily_delta, min_confirm_days, data_quality_note, created_at, updated_at
                )
                VALUES(?, ?, 'active', ?, ?, 0.03, 3, '', ?, ?)
                """,
                (
                    "stocklens_default",
                    "v1",
                    _json_dumps(BASELINE_WEIGHTS),
                    _json_dumps(REGIME_TARGET_WEIGHTS),
                    now,
                    now,
                ),
            )
        return self.get_active_weight_policy()

    def get_active_weight_policy(self) -> WeightPolicyVersion:
        with self.database.connect() as conn:
            row = conn.execute(
                "SELECT * FROM weight_policy_versions WHERE status = 'active' ORDER BY id DESC LIMIT 1"
            ).fetchone()
        if row is None:
            return self.ensure_default_weight_policy()
        return self._weight_policy_from_row(row)

    def save_weight_policy(
        self,
        *,
        name: str,
        version: str,
        status: str,
        baseline_weights: dict[str, float],
        regime_targets: dict[str, dict[str, float]],
        max_daily_delta: float = 0.03,
        min_confirm_days: int = 3,
        data_quality_note: str = "",
    ) -> int:
        if status not in {"active", "shadow", "archived"}:
            raise ValueError("权重策略状态只能是 active/shadow/archived")
        now = _now()
        with self.database.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO weight_policy_versions(
                    name, version, status, baseline_weights_json, regime_targets_json,
                    max_daily_delta, min_confirm_days, data_quality_note, created_at, updated_at
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(name, version) DO UPDATE SET
                    status = excluded.status,
                    baseline_weights_json = excluded.baseline_weights_json,
                    regime_targets_json = excluded.regime_targets_json,
                    max_daily_delta = excluded.max_daily_delta,
                    min_confirm_days = excluded.min_confirm_days,
                    data_quality_note = excluded.data_quality_note,
                    updated_at = excluded.updated_at
                """,
                (
                    name,
                    version,
                    status,
                    _json_dumps(normalize_weights(baseline_weights)),
                    _json_dumps({key: normalize_weights(value) for key, value in regime_targets.items()}),
                    float(max_daily_delta),
                    int(min_confirm_days),
                    data_quality_note[:1000],
                    now,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT id FROM weight_policy_versions WHERE name = ? AND version = ?",
                (name, version),
            ).fetchone()
        return int(row["id"] if row else cursor.lastrowid)

    def ensure_default_professional_formula(self) -> dict[str, Any]:
        formula = build_default_professional_formula()
        now = _now()
        with self.database.connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO professional_formula_candidates(
                    name, version, status, dimension_weights_json, field_weights_json,
                    evidence_json, validation_json, created_at, updated_at, activated_at
                )
                VALUES(?, ?, 'active', ?, ?, '{}', ?, ?, ?, ?)
                """,
                (
                    "stocklens_professional",
                    "v1",
                    _json_dumps(formula["dimension_weights"]),
                    _json_dumps(formula["field_weights"]),
                    _json_dumps({"reason": "默认专业公式 active 版本"}),
                    now,
                    now,
                    now,
                ),
            )
        return self.get_active_professional_formula()

    def get_active_professional_formula(self) -> dict[str, Any]:
        with self.database.connect() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM professional_formula_candidates
                WHERE status = 'active'
                ORDER BY activated_at DESC, id DESC
                LIMIT 1
                """
            ).fetchone()
        if row is None:
            return self.ensure_default_professional_formula()
        return self._professional_formula_from_row(row)

    def list_professional_formula_candidates(self, status: str = "", limit: int = 20) -> list[dict[str, Any]]:
        params: list[Any] = []
        clause = ""
        if status:
            clause = "WHERE status = ?"
            params.append(status)
        with self.database.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT *
                FROM professional_formula_candidates
                {clause}
                ORDER BY updated_at DESC, id DESC
                LIMIT ?
                """,
                (*params, max(1, int(limit))),
            ).fetchall()
        return [self._professional_formula_from_row(row) for row in rows]

    def latest_professional_shadow_candidate(self) -> dict[str, Any] | None:
        with self.database.connect() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM professional_formula_candidates
                WHERE status IN ('suggest_enable', 'shadow', 'rejected')
                ORDER BY updated_at DESC, id DESC
                LIMIT 1
                """
            ).fetchone()
        return self._professional_formula_from_row(row) if row else None

    def activate_professional_formula_candidate(self, candidate_id: int) -> dict[str, Any]:
        now = _now()
        with self.database.connect() as conn:
            row = conn.execute(
                "SELECT * FROM professional_formula_candidates WHERE id = ?",
                (int(candidate_id),),
            ).fetchone()
            if row is None:
                raise ValueError("找不到专业公式候选")
            if row["status"] != "suggest_enable":
                raise ValueError("只有达到“建议启用”的候选才能确认启用")
            conn.execute(
                """
                UPDATE professional_formula_candidates
                SET status = 'archived', updated_at = ?
                WHERE status = 'active'
                """,
                (now,),
            )
            conn.execute(
                """
                UPDATE professional_formula_candidates
                SET status = 'active', updated_at = ?, activated_at = ?
                WHERE id = ?
                """,
                (now, now, int(candidate_id)),
            )
            row = conn.execute(
                "SELECT * FROM professional_formula_candidates WHERE id = ?",
                (int(candidate_id),),
            ).fetchone()
        return self._professional_formula_from_row(row)

    def save_professional_weight_ranges(self, ranges: dict[str, Any], data_quality_score: float = 0.0) -> int:
        created_at = _now()
        inserted = 0
        dimensions = ranges.get("dimensions") or {}
        fields = ranges.get("fields") or {}
        with self.database.connect() as conn:
            for dimension, payload in dimensions.items():
                conn.execute(
                    """
                    INSERT INTO professional_weight_ranges(
                        scope, dimension, field_name, min_weight, max_weight, center_weight,
                        sample_count, data_quality_score, evidence_json, created_at
                    )
                    VALUES('dimension', ?, '', ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        dimension,
                        float(payload.get("min") or 0.0),
                        float(payload.get("max") or 0.0),
                        float(payload.get("center") or 0.0),
                        int(payload.get("sample_count") or ranges.get("sample_count") or 0),
                        float(data_quality_score or 0.0),
                        _json_dumps(payload),
                        created_at,
                    ),
                )
                inserted += 1
            for dimension, field_payloads in fields.items():
                for field_name, payload in field_payloads.items():
                    conn.execute(
                        """
                        INSERT INTO professional_weight_ranges(
                            scope, dimension, field_name, min_weight, max_weight, center_weight,
                            sample_count, data_quality_score, evidence_json, created_at
                        )
                        VALUES('field', ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            dimension,
                            field_name,
                            float(payload.get("min") or 0.0),
                            float(payload.get("max") or 0.0),
                            float(payload.get("center") or 0.0),
                            int(payload.get("sample_count") or ranges.get("sample_count") or 0),
                            float(data_quality_score or 0.0),
                            _json_dumps(payload),
                            created_at,
                        ),
                    )
                    inserted += 1
        return inserted

    def list_professional_memory(self, limit: int = 12) -> list[dict[str, Any]]:
        with self.database.connect() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM professional_formula_memory
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (max(1, int(limit)),),
            ).fetchall()
        return [
            {
                "id": int(row["id"]),
                "memory_type": row["memory_type"],
                "title": row["title"],
                "payload": _json_loads(row["payload_json"], {}),
                "source_candidate_id": row["source_candidate_id"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def record_professional_memory(
        self,
        *,
        memory_type: str,
        title: str,
        payload: dict[str, Any],
        source_candidate_id: int | None = None,
    ) -> int:
        with self.database.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO professional_formula_memory(
                    memory_type, title, payload_json, source_candidate_id, created_at
                )
                VALUES(?, ?, ?, ?, ?)
                """,
                (
                    memory_type[:80],
                    title[:200],
                    _json_dumps(payload),
                    source_candidate_id,
                    _now(),
                ),
            )
        return int(cursor.lastrowid)

    def build_professional_evidence_package(self, *, persist_ranges: bool = False) -> dict[str, Any]:
        active = self.get_active_professional_formula()
        rows = self._professional_evidence_rows()
        recent_rows = self._recent_complete_round_rows(rows)
        ranges = derive_professional_weight_ranges(
            rows,
            active_dimension_weights=active["dimension_weights"],
            active_field_weights=active["field_weights"],
        )
        precise = derive_recent_precise_weights(
            recent_rows or rows,
            active_dimension_weights=active["dimension_weights"],
            active_field_weights=active["field_weights"],
        )
        long_term = self._professional_summary_payload(rows)
        recent_round = self._professional_summary_payload(recent_rows)
        data_gaps = self._professional_data_gaps(rows)
        evidence = {
            "version": PROFESSIONAL_FORMULA_VERSION,
            "prompt_version": PROMPT_PROFESSIONAL_WEIGHT_CANDIDATE_V1,
            "generated_at": _now(),
            "active": {
                "id": active["id"],
                "name": active["name"],
                "version": active["version"],
                "dimension_weights": active["dimension_weights"],
                "field_weights": active["field_weights"],
            },
            "sample_policy": {
                "min_settled_samples": PROFESSIONAL_MIN_SETTLED_SAMPLES,
                "period_weights": PROFESSIONAL_OBSERVATION_PERIOD_WEIGHTS,
            },
            "long_term": long_term,
            "recent_round": {
                **recent_round,
                "sample_date": recent_rows[0]["sample_date"] if recent_rows else "",
                "next_validation_conditions": [
                    "下一轮继续完成 3/5/10/20 日观察样本。",
                    "候选只在跑赢率、方向胜率、超额收益和回撤控制综合优于 active 时建议启用。",
                ],
            },
            "recent_precise_weights": precise,
            "historical_weight_ranges": ranges,
            "cases": self._professional_case_layers(rows),
            "data_gaps": data_gaps,
            "memory": self.list_professional_memory(limit=8),
        }
        if persist_ranges:
            self.save_professional_weight_ranges(ranges, float(long_term.get("data_quality_score") or 0.0))
        return evidence

    def save_validated_professional_candidate(
        self,
        payload: dict[str, Any],
        *,
        evidence_package: dict[str, Any] | None = None,
        provider: str = "",
        model: str = "",
        cost_note: str = "",
    ) -> tuple[int, dict[str, Any]]:
        evidence = evidence_package or self.build_professional_evidence_package(persist_ranges=True)
        validation = validate_professional_candidate_payload(payload, evidence)
        validation_payload = {
            "accepted": validation.accepted,
            "status": validation.status,
            "suggest_enable": validation.suggest_enable,
            "reason": validation.reason,
            "active_score": validation.active_score,
            "candidate_score": validation.candidate_score,
            "checks": validation.checks,
            "cost_note": cost_note,
        }
        ai_log_id = self.log_ai_decision(
            decision_type="professional_formula_candidate",
            prompt_version=PROMPT_PROFESSIONAL_WEIGHT_CANDIDATE_V1,
            input_summary=(
                f"settled={evidence.get('long_term', {}).get('settled_sample_count', 0)}; "
                f"quality={evidence.get('long_term', {}).get('data_quality_score', 0)}"
            ),
            output_json={"payload": payload, "validation": validation_payload},
            provider=provider,
            model=model,
            cost={"note": cost_note} if cost_note else {},
            adopted=False,
        )
        active = evidence.get("active") or {}
        dimensions = validation.dimension_weights or normalize_dimension_weights(active.get("dimension_weights"))
        fields = validation.field_weights or normalize_field_weights(active.get("field_weights"))
        base_name = str(payload.get("candidate_name") or "professional_shadow")
        version = datetime.now().strftime("v%Y%m%d%H%M%S")
        candidate_id = self._save_professional_formula_candidate(
            name=base_name,
            version=version,
            status=validation.status,
            dimension_weights=dimensions,
            field_weights=fields,
            evidence=evidence,
            validation=validation_payload,
            ai_log_id=ai_log_id,
        )
        for entry in build_professional_memory_entries(evidence):
            self.record_professional_memory(
                memory_type=entry["memory_type"],
                title=entry["title"],
                payload=entry["payload"],
                source_candidate_id=candidate_id,
            )
        return candidate_id, validation_payload

    def professional_loop_snapshot(self) -> dict[str, Any]:
        evidence = self.build_professional_evidence_package(persist_ranges=False)
        latest = self.latest_professional_shadow_candidate()
        return {
            "active": evidence["active"],
            "long_term": evidence["long_term"],
            "recent_round": evidence["recent_round"],
            "historical_weight_ranges": evidence["historical_weight_ranges"],
            "recent_precise_weights": evidence["recent_precise_weights"],
            "latest_candidate": latest,
            "memory": evidence["memory"],
            "data_gaps": evidence["data_gaps"],
        }

    def _save_professional_formula_candidate(
        self,
        *,
        name: str,
        version: str,
        status: str,
        dimension_weights: dict[str, Any],
        field_weights: dict[str, Any],
        evidence: dict[str, Any],
        validation: dict[str, Any],
        ai_log_id: int | None = None,
    ) -> int:
        if status not in {"active", "shadow", "suggest_enable", "rejected", "archived"}:
            raise ValueError("专业公式候选状态无效")
        now = _now()
        with self.database.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO professional_formula_candidates(
                    name, version, status, dimension_weights_json, field_weights_json,
                    evidence_json, validation_json, ai_log_id, created_at, updated_at, activated_at
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '')
                ON CONFLICT(name, version) DO UPDATE SET
                    status = excluded.status,
                    dimension_weights_json = excluded.dimension_weights_json,
                    field_weights_json = excluded.field_weights_json,
                    evidence_json = excluded.evidence_json,
                    validation_json = excluded.validation_json,
                    ai_log_id = excluded.ai_log_id,
                    updated_at = excluded.updated_at
                """,
                (
                    name[:120],
                    version[:40],
                    status,
                    _json_dumps(normalize_dimension_weights(dimension_weights)),
                    _json_dumps(normalize_field_weights(field_weights)),
                    _json_dumps(evidence),
                    _json_dumps(validation),
                    ai_log_id,
                    now,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT id FROM professional_formula_candidates WHERE name = ? AND version = ?",
                (name[:120], version[:40]),
            ).fetchone()
        return int(row["id"] if row else cursor.lastrowid)

    def save_market_regime(self, snapshot: MarketRegimeSnapshot) -> int:
        regime = snapshot.regime if snapshot.regime in REGIME_VALUES else REGIME_UNCERTAIN
        now = snapshot.created_at or _now()
        with self.database.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO market_regime_snapshots(asof_date, regime, confidence, metrics_json, source, created_at)
                VALUES(?, ?, ?, ?, ?, ?)
                ON CONFLICT(asof_date, source) DO UPDATE SET
                    regime = excluded.regime,
                    confidence = excluded.confidence,
                    metrics_json = excluded.metrics_json,
                    created_at = excluded.created_at
                """,
                (
                    _date_part(snapshot.asof_date),
                    regime,
                    float(snapshot.confidence),
                    _json_dumps(snapshot.metrics),
                    snapshot.source,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT id FROM market_regime_snapshots WHERE asof_date = ? AND source = ?",
                (_date_part(snapshot.asof_date), snapshot.source),
            ).fetchone()
        return int(row["id"] if row else cursor.lastrowid)

    def latest_market_regime(self, asof_date: str | None = None) -> MarketRegimeSnapshot | None:
        params: tuple[Any, ...]
        if asof_date:
            sql = "SELECT * FROM market_regime_snapshots WHERE asof_date <= ? ORDER BY asof_date DESC, id DESC LIMIT 1"
            params = (_date_part(asof_date),)
        else:
            sql = "SELECT * FROM market_regime_snapshots ORDER BY asof_date DESC, id DESC LIMIT 1"
            params = ()
        with self.database.connect() as conn:
            row = conn.execute(sql, params).fetchone()
        return self._regime_from_row(row) if row else None

    def save_run_context(
        self,
        run_id: int,
        *,
        asof_date: str | None = None,
        regime_snapshot: MarketRegimeSnapshot | None = None,
        effective_weights: dict[str, float] | None = None,
        shadow: bool = False,
        ai_log_id: int | None = None,
        data_quality_note: str = "",
    ) -> StrategyRunContext:
        policy = self.get_active_weight_policy()
        run_asof = _date_part(asof_date)
        snapshot = regime_snapshot or self.latest_market_regime(run_asof)
        if snapshot is None:
            snapshot = MarketRegimeSnapshot(run_asof, REGIME_UNCERTAIN, 0.0, {}, "fallback")
            self.save_market_regime(snapshot)
        weights = normalize_weights(effective_weights or policy.baseline_weights)
        now = _now()
        with self.database.connect() as conn:
            conn.execute(
                """
                INSERT INTO strategy_run_contexts(
                    run_id, asof_date, regime, regime_confidence, weight_policy_id,
                    weight_policy_name, weight_policy_version, effective_weights_json,
                    shadow, ai_log_id, data_quality_note, created_at
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    asof_date = excluded.asof_date,
                    regime = excluded.regime,
                    regime_confidence = excluded.regime_confidence,
                    weight_policy_id = excluded.weight_policy_id,
                    weight_policy_name = excluded.weight_policy_name,
                    weight_policy_version = excluded.weight_policy_version,
                    effective_weights_json = excluded.effective_weights_json,
                    shadow = excluded.shadow,
                    ai_log_id = excluded.ai_log_id,
                    data_quality_note = excluded.data_quality_note,
                    created_at = excluded.created_at
                """,
                (
                    int(run_id),
                    run_asof,
                    snapshot.regime,
                    float(snapshot.confidence),
                    policy.id,
                    policy.name,
                    policy.version,
                    _json_dumps(weights),
                    1 if shadow else 0,
                    ai_log_id,
                    data_quality_note[:1000],
                    now,
                ),
            )
            row = conn.execute("SELECT * FROM strategy_run_contexts WHERE run_id = ?", (int(run_id),)).fetchone()
        return self._run_context_from_row(row)

    def create_observation_samples_for_run(
        self,
        run_id: int,
        *,
        top_n: int = DEFAULT_OBSERVATION_TOP_N,
        horizons: tuple[int, ...] = DEFAULT_OBSERVATION_HORIZONS,
        ai_rank_map: dict[str, Any] | None = None,
    ) -> int:
        """Compatibility entry point for legacy sample creation.

        New formal callers must use ``create_formal_observation_samples``;
        silently creating a formal sample from this low-level method would
        recreate the exact bypass the research contract is meant to prevent.
        """
        return self._create_observation_samples_for_run(
            run_id,
            top_n=top_n,
            horizons=horizons,
            ai_rank_map=ai_rank_map,
            sample_class="legacy",
        )

    def create_formal_observation_samples(
        self,
        run_id: int,
        certificate_id: str,
        *,
        top_n: int = DEFAULT_OBSERVATION_TOP_N,
        horizons: tuple[int, ...] = DEFAULT_OBSERVATION_HORIZONS,
        ai_rank_map: dict[str, Any] | None = None,
    ) -> int:
        """Create only samples backed by a verified formal certificate."""
        FormalScreeningCertificateStore(self.database).require_for_sample(run_id, certificate_id)
        return self._create_observation_samples_for_run(
            run_id,
            top_n=top_n,
            horizons=horizons,
            ai_rank_map=ai_rank_map,
            sample_class="formal",
            certificate_id=certificate_id,
        )

    def create_review_observation_samples(
        self,
        run_id: int,
        *,
        top_n: int = DEFAULT_OBSERVATION_TOP_N,
        horizons: tuple[int, ...] = DEFAULT_OBSERVATION_HORIZONS,
        ai_rank_map: dict[str, Any] | None = None,
    ) -> int:
        """Create explicitly non-formal samples for review/exploration."""
        return self._create_observation_samples_for_run(
            run_id,
            top_n=top_n,
            horizons=horizons,
            ai_rank_map=ai_rank_map,
            sample_class="review",
        )

    def create_exploratory_observation_samples(
        self,
        run_id: int,
        *,
        top_n: int = DEFAULT_OBSERVATION_TOP_N,
        horizons: tuple[int, ...] = DEFAULT_OBSERVATION_HORIZONS,
        ai_rank_map: dict[str, Any] | None = None,
    ) -> int:
        """Create explicitly exploratory samples excluded from formal stats."""
        return self._create_observation_samples_for_run(
            run_id,
            top_n=top_n,
            horizons=horizons,
            ai_rank_map=ai_rank_map,
            sample_class="exploratory",
        )

    def create_legacy_observation_samples(
        self,
        run_id: int,
        *,
        top_n: int = DEFAULT_OBSERVATION_TOP_N,
        horizons: tuple[int, ...] = DEFAULT_OBSERVATION_HORIZONS,
        ai_rank_map: dict[str, Any] | None = None,
    ) -> int:
        """Preserve historical rows without upgrading them to formal."""
        return self._create_observation_samples_for_run(
            run_id,
            top_n=top_n,
            horizons=horizons,
            ai_rank_map=ai_rank_map,
            sample_class="legacy",
            allow_formal_context=True,
        )

    def _create_observation_samples_for_run(
        self,
        run_id: int,
        *,
        top_n: int,
        horizons: tuple[int, ...],
        ai_rank_map: dict[str, Any] | None,
        sample_class: str,
        certificate_id: str | None = None,
        allow_formal_context: bool = False,
    ) -> int:
        if sample_class not in SAMPLE_CLASSES:
            raise ValueError(f"不支持的观察样本类别：{sample_class}")
        with self.database.connect() as conn:
            run = conn.execute("SELECT * FROM query_runs WHERE id = ?", (int(run_id),)).fetchone()
            if run is None:
                raise ValueError("找不到策略运行记录")
            rows = conn.execute(
                """
                SELECT *
                FROM query_run_results
                WHERE run_id = ?
                ORDER BY rank_order ASC, id ASC
                LIMIT ?
                """,
                (int(run_id), max(1, int(top_n))),
            ).fetchall()
            run_scoring_context = _json_loads(run["scoring_context_json"], {})
            execution_mode = str(run_scoring_context.get("execution_mode") or "") if isinstance(run_scoring_context, dict) else ""
            screening_status = str(run_scoring_context.get("screening_status") or "") if isinstance(run_scoring_context, dict) else ""
            if sample_class == "formal":
                if execution_mode != "formal" or screening_status != "verified":
                    raise FormalObservationBlocked("运行记录不是 verified formal，禁止创建正式观察样本")
                if not certificate_id:
                    raise FormalObservationBlocked("正式观察样本必须绑定 certificate_id")
            elif execution_mode == "formal" and not allow_formal_context:
                raise FormalObservationBlocked(
                    "formal 运行不能通过兼容 API 创建样本，请使用 create_formal_observation_samples"
                )
            if isinstance(run_scoring_context, dict) and run_scoring_context.get("screening_status") in {
                "blocked", "partial", "unverified", "review_only",
            }:
                if execution_mode == "formal":
                    raise FormalObservationBlocked("正式运行的数据质量未达到样本创建要求")
                return 0
            sample_date = _date_part(run["created_at"])
            now = _now()
            inserted = 0
            for row in rows:
                metrics = _json_loads(row["metrics_json"], {})
                score_breakdown = _json_loads(row["score_breakdown_json"], {})
                if sample_class == "formal" and str(score_breakdown.get("eligibility_status") or "eligible") != "eligible":
                    continue
                professional_breakdown = extract_professional_breakdown(score_breakdown) or {}
                for field in (
                    "selection_profile",
                    "selection_grade",
                    "selection_state",
                    "veto_reasons",
                    "evidence_gaps",
                    "eligibility_status",
                    "required_fields",
                    "missing_required_fields",
                    "data_quality_status",
                ):
                    if field in score_breakdown:
                        metrics[field] = score_breakdown[field]
                metrics["primary_score"] = _to_float(row["local_score"])
                metrics["legacy_score"] = _to_float(metrics.get("legacy_score") or score_breakdown.get("legacy_score"))
                metrics["professional_data_quality_score"] = _to_float(professional_breakdown.get("data_quality_score"))
                if isinstance(run_scoring_context, dict) and run_scoring_context:
                    metrics["scoring_context"] = run_scoring_context
                    score_breakdown.setdefault("scoring_context", run_scoring_context)
                    active_formula = run_scoring_context.get("active_formula")
                    if isinstance(active_formula, dict):
                        metrics["active_formula_version"] = str(active_formula.get("version") or "")
                code = str(row["code"]).zfill(6)
                ai_rank = (ai_rank_map or {}).get(code)
                ai_score = _rank_value(ai_rank, "ai_score")
                ai_level = str(_rank_value(ai_rank, "level") or "")
                for horizon in horizons:
                    cursor = conn.execute(
                        """
                        INSERT OR IGNORE INTO observation_samples(
                            result_id, run_id, horizon_days, sample_date, due_date, code, name,
                            entry_price, rank_order, local_score, industry, metrics_json,
                            score_breakdown_json, ai_score, ai_level, sample_class,
                            certificate_id, created_at
                        )
                        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            int(row["id"]),
                            int(run_id),
                            int(horizon),
                            sample_date,
                            add_trading_days(sample_date, int(horizon)),
                            code,
                            row["name"],
                            _to_float(metrics.get("price")),
                            int(row["rank_order"]),
                            _to_float(row["local_score"]),
                            str(metrics.get("industry") or ""),
                            _json_dumps(metrics),
                            _json_dumps(score_breakdown),
                            ai_score,
                            ai_level,
                            sample_class,
                            certificate_id,
                            now,
                        ),
                    )
                    inserted += max(0, int(cursor.rowcount))
        return inserted

    def list_due_samples(self, asof_date: str | None = None, limit: int = 200) -> list[ObservationSample]:
        due = _date_part(asof_date)
        with self.database.connect() as conn:
            rows = conn.execute(
                """
                SELECT s.*
                FROM observation_samples s
                LEFT JOIN observation_outcomes o ON o.sample_id = s.id
                WHERE o.id IS NULL AND s.due_date <= ?
                ORDER BY s.due_date ASC, s.rank_order ASC, s.id ASC
                LIMIT ?
                """,
                (due, max(1, int(limit))),
            ).fetchall()
        return [self._sample_from_row(row) for row in rows]

    def update_sample_ai_scores(self, run_id: int, ai_rank_map: dict[str, Any]) -> int:
        updated = 0
        with self.database.connect() as conn:
            for raw_code, rank in ai_rank_map.items():
                code = str(raw_code).zfill(6)
                cursor = conn.execute(
                    """
                    UPDATE observation_samples
                    SET ai_score = ?, ai_level = ?
                    WHERE run_id = ? AND code = ?
                    """,
                    (_rank_value(rank, "ai_score"), str(_rank_value(rank, "level") or ""), int(run_id), code),
                )
                updated += max(0, int(cursor.rowcount))
        return updated

    def record_outcome(self, outcome: ObservationOutcome) -> None:
        with self.database.connect() as conn:
            conn.execute(
                """
                INSERT INTO observation_outcomes(
                    sample_id, settled_at, exit_date, exit_price, return_pct,
                    max_favorable_pct, max_drawdown_pct, hs300_return_pct,
                    excess_vs_hs300_pct, industry_return_pct, excess_vs_industry_pct,
                    direction_hit, benchmark_hit, rating, data_quality,
                    data_quality_note, metrics_json
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(sample_id) DO UPDATE SET
                    settled_at = excluded.settled_at,
                    exit_date = excluded.exit_date,
                    exit_price = excluded.exit_price,
                    return_pct = excluded.return_pct,
                    max_favorable_pct = excluded.max_favorable_pct,
                    max_drawdown_pct = excluded.max_drawdown_pct,
                    hs300_return_pct = excluded.hs300_return_pct,
                    excess_vs_hs300_pct = excluded.excess_vs_hs300_pct,
                    industry_return_pct = excluded.industry_return_pct,
                    excess_vs_industry_pct = excluded.excess_vs_industry_pct,
                    direction_hit = excluded.direction_hit,
                    benchmark_hit = excluded.benchmark_hit,
                    rating = excluded.rating,
                    data_quality = excluded.data_quality,
                    data_quality_note = excluded.data_quality_note,
                    metrics_json = excluded.metrics_json
                """,
                (
                    int(outcome.sample_id),
                    outcome.settled_at,
                    outcome.exit_date,
                    outcome.exit_price,
                    outcome.return_pct,
                    outcome.max_favorable_pct,
                    outcome.max_drawdown_pct,
                    outcome.hs300_return_pct,
                    outcome.excess_vs_hs300_pct,
                    outcome.industry_return_pct,
                    outcome.excess_vs_industry_pct,
                    _bool_to_db(outcome.direction_hit),
                    _bool_to_db(outcome.benchmark_hit),
                    outcome.rating,
                    outcome.data_quality,
                    outcome.data_quality_note[:1000],
                    _json_dumps(outcome.metrics),
                ),
            )

    def summarize(
        self,
        *,
        start_date: str = "",
        end_date: str = "",
        horizon_days: int | None = None,
        regime: str = "",
        weight_policy_version: str = "",
        template_id: str = "",
    ) -> PerformanceSummary:
        rows = self._summary_rows(
            start_date=start_date,
            end_date=end_date,
            horizon_days=horizon_days,
            regime=regime,
            weight_policy_version=weight_policy_version,
            template_id=template_id,
        )
        return _summary_from_rows(rows)

    def grouped_summary(self, group_by: str = "regime", **filters: Any) -> list[tuple[str, PerformanceSummary]]:
        rows = self._summary_rows(**filters)
        groups: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            if group_by == "horizon":
                keys = [str(row["horizon_days"])]
            elif group_by == "weight_policy":
                keys = [row.get("weight_policy_version") or "-"]
            elif group_by == "template":
                keys = [row.get("template_id") or "-"]
            elif group_by == "industry":
                keys = [row.get("industry") or "-"]
            elif group_by == "selection_profile":
                keys = [row.get("selection_profile") or "-"]
            elif group_by == "selection_grade":
                keys = [row.get("selection_grade") or "-"]
            elif group_by == "selection_state":
                keys = [row.get("selection_state") or "-"]
            elif group_by == "data_quality":
                quality = str(row.get("data_quality") or "pending")
                keys = [quality]
            elif group_by == "professional_data_quality":
                score = _to_float(row.get("professional_data_quality_score"))
                if score is None:
                    keys = ["quality_unknown"]
                elif score >= 80.0:
                    keys = ["quality_ok"]
                else:
                    keys = ["quality_gap"]
            elif group_by == "active_formula_version":
                keys = [row.get("active_formula_version") or "-"]
            elif group_by == "dimension_high":
                dimension_scores = row.get("professional_dimension_scores")
                keys = []
                if isinstance(dimension_scores, dict):
                    for dimension in PROFESSIONAL_DIMENSIONS:
                        score = _to_float(dimension_scores.get(dimension))
                        if score is not None and score >= 65.0:
                            keys.append(f"{dimension}:high")
                if not keys:
                    keys = ["no_dimension_high"]
            elif group_by == "strategy":
                keys = [row.get("preset_name") or row.get("template_id") or row.get("source_label") or "-"]
            else:
                keys = [row.get("regime") or REGIME_UNCERTAIN]
            for key in keys:
                groups.setdefault(key, []).append(row)
        return [(key, _summary_from_rows(group_rows)) for key, group_rows in sorted(groups.items())]

    def list_recent_outcomes(self, limit: int = 100, **filters: Any) -> list[dict[str, Any]]:
        clauses, params = self._filter_sql(filters)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self.database.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT s.id AS sample_id, s.code, s.name, s.horizon_days, s.sample_date, s.due_date, s.rank_order,
                       s.local_score, s.ai_score, s.industry, s.sample_class, s.certificate_id,
                       s.metrics_json,
                       r.id AS run_id, r.preset_id, r.preset_name, r.template_id, r.template_version,
                       c.regime, c.weight_policy_name, c.weight_policy_version,
                       o.exit_date, o.return_pct, o.max_favorable_pct,
                       o.excess_vs_hs300_pct, o.max_drawdown_pct,
                       o.rating, o.data_quality, o.data_quality_note
                FROM observation_samples s
                JOIN query_runs r ON r.id = s.run_id
                LEFT JOIN strategy_run_contexts c ON c.run_id = s.run_id
                LEFT JOIN observation_outcomes o ON o.sample_id = s.id
                {where}
                ORDER BY s.sample_date DESC, s.rank_order ASC, s.horizon_days ASC
                LIMIT ?
                """,
                (*params, max(1, int(limit))),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            metrics = _json_loads(str(item.pop("metrics_json", "") or "{}"), {})
            for field in (
                "selection_profile", "selection_grade", "selection_state", "veto_reasons", "evidence_gaps",
                "eligibility_status", "required_fields", "missing_required_fields", "data_quality_status",
            ):
                if field in metrics:
                    item[field] = metrics[field]
            item["professional_data_quality_score"] = metrics.get("professional_data_quality_score")
            result.append(item)
        return result

    def dashboard_snapshot(self, asof_date: str | None = None, recent_days: int = 60) -> ClosedLoopDashboardSnapshot:
        asof = _date_part(asof_date)
        safe_recent_days = max(1, int(recent_days))
        start_date = (_parse_date(asof) - timedelta(days=safe_recent_days)).isoformat()
        with self.database.connect() as conn:
            due_rows = conn.execute(
                """
                SELECT s.horizon_days, COUNT(*) AS count
                FROM observation_samples s
                LEFT JOIN observation_outcomes o ON o.sample_id = s.id
                WHERE o.id IS NULL AND s.due_date <= ?
                GROUP BY s.horizon_days
                ORDER BY s.horizon_days
                """,
                (asof,),
            ).fetchall()
        due_by_horizon = {int(row["horizon_days"]): int(row["count"]) for row in due_rows}
        due_count = sum(due_by_horizon.values())

        summary = self.summarize(start_date=start_date, end_date=asof)
        strategy_groups = self.grouped_summary("strategy", start_date=start_date, end_date=asof)
        horizon_groups = self.grouped_summary("horizon", start_date=start_date, end_date=asof)
        period_summaries = {horizon: _summary_from_rows([]) for horizon in DEFAULT_OBSERVATION_HORIZONS}
        for name, item in horizon_groups:
            try:
                horizon = int(name)
            except (TypeError, ValueError):
                continue
            if horizon in period_summaries:
                period_summaries[horizon] = item
        ranked_groups = [
            (name, item)
            for name, item in strategy_groups
            if item.settled_count > 0 and item.avg_excess_vs_hs300_pct is not None
        ]
        ranked_groups.sort(
            key=lambda pair: (
                pair[1].avg_excess_vs_hs300_pct if pair[1].avg_excess_vs_hs300_pct is not None else -999999.0,
                pair[1].benchmark_hit_rate if pair[1].benchmark_hit_rate is not None else -1.0,
                pair[1].direction_hit_rate if pair[1].direction_hit_rate is not None else -1.0,
            )
        )
        weakest_name, weakest_summary = ranked_groups[0] if ranked_groups else ("", None)
        strongest_name, strongest_summary = ranked_groups[-1] if ranked_groups else ("", None)
        data_gap_count = sum(count for key, count in summary.data_quality_counts.items() if key and key != "ok")

        with self.database.connect() as conn:
            shadow_rows = conn.execute(
                """
                SELECT name, version, status, updated_at
                FROM professional_formula_candidates
                WHERE status IN ('shadow', 'suggest_enable')
                ORDER BY updated_at DESC, id DESC
                """
            ).fetchall()
        latest_shadow = ""
        if shadow_rows:
            latest = shadow_rows[0]
            status_label = "建议启用" if latest["status"] == "suggest_enable" else "继续观察"
            latest_shadow = f"{latest['name']} {latest['version']}（{status_label}）"

        next_action = _dashboard_next_action(
            due_count=due_count,
            settled_count=summary.settled_count,
            data_gap_count=data_gap_count,
            shadow_policy_count=len(shadow_rows),
        )
        return ClosedLoopDashboardSnapshot(
            asof_date=asof,
            recent_days=safe_recent_days,
            due_count=due_count,
            due_by_horizon=due_by_horizon,
            sample_count=summary.sample_count,
            settled_count=summary.settled_count,
            direction_hit_rate=summary.direction_hit_rate,
            benchmark_hit_rate=summary.benchmark_hit_rate,
            avg_excess_vs_hs300_pct=summary.avg_excess_vs_hs300_pct,
            period_summaries=period_summaries,
            strongest_strategy=strongest_name,
            strongest_excess_pct=strongest_summary.avg_excess_vs_hs300_pct if strongest_summary else None,
            weakest_strategy=weakest_name,
            weakest_excess_pct=weakest_summary.avg_excess_vs_hs300_pct if weakest_summary else None,
            data_gap_count=data_gap_count,
            data_quality_counts=dict(summary.data_quality_counts),
            shadow_policy_count=len(shadow_rows),
            latest_shadow_policy=latest_shadow,
            next_action=next_action,
            valid_settled_count=summary.valid_settled_count,
            invalid_settled_count=summary.invalid_settled_count,
        )

    def log_ai_decision(
        self,
        *,
        decision_type: str,
        prompt_version: str,
        input_summary: str,
        output_json: dict[str, Any],
        provider: str = "",
        model: str = "",
        cost: dict[str, Any] | None = None,
        adopted: bool = False,
    ) -> int:
        with self.database.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO ai_decision_logs(
                    decision_type, prompt_version, input_summary, output_json,
                    provider, model, cost_json, adopted, created_at
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    decision_type,
                    prompt_version,
                    input_summary[:2000],
                    _json_dumps(output_json),
                    provider,
                    model,
                    _json_dumps(cost or {}),
                    1 if adopted else 0,
                    _now(),
                ),
            )
        return int(cursor.lastrowid)

    def save_research_discussion_audit(
        self,
        *,
        candidate_set_digest: str,
        data_snapshot_id: str,
        refresh_event_id: str,
        formula_version: str,
        evidence_hash: str,
        prompt_version: str,
        model_version: str,
        response_hash: str = "",
        status: str = "failed",
        error_message: str = "",
        cost_note: str = "",
        run_id: str = "",
        request_hash: str = "",
        evidence_package_hash: str = "",
        evidence_package: dict[str, Any] | None = None,
        request_text: str = "",
        raw_response: str = "",
        normalized_response: str = "",
    ) -> str:
        """Persist a research-only AI attempt as a side-channel audit row."""
        audit_id = f"discussion-{uuid.uuid4().hex}"
        resolved_evidence_hash = str(evidence_package_hash or evidence_hash or "")
        with self.database.connect() as conn:
            conn.execute(
                """
                INSERT INTO research_discussion_audits(
                    audit_id, run_id, candidate_set_digest, data_snapshot_id,
                    refresh_event_id, formula_version, evidence_hash,
                    evidence_package_hash, request_hash, prompt_version,
                    model_version, response_hash, status, error_message,
                    cost_note, evidence_package_json, request_text,
                    raw_response, normalized_response, created_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    audit_id,
                    str(run_id or ""),
                    candidate_set_digest,
                    data_snapshot_id,
                    refresh_event_id,
                    formula_version,
                    resolved_evidence_hash,
                    resolved_evidence_hash,
                    str(request_hash or ""),
                    prompt_version,
                    model_version,
                    response_hash,
                    status,
                    str(error_message or "")[:2000],
                    str(cost_note or "")[:2000],
                    _json_dumps(evidence_package or {}),
                    str(request_text or ""),
                    str(raw_response or ""),
                    str(normalized_response or "{}"),
                    _now(),
                ),
            )
        return audit_id

    def _summary_rows(self, **filters: Any) -> list[dict[str, Any]]:
        clauses, params = self._filter_sql(filters)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self.database.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT s.horizon_days, s.industry, r.preset_name, r.source_label, r.template_id,
                       s.metrics_json, s.score_breakdown_json,
                       c.regime, c.weight_policy_version,
                       o.return_pct, o.excess_vs_hs300_pct, o.max_drawdown_pct,
                       o.direction_hit, o.benchmark_hit, o.rating, o.data_quality
                FROM observation_samples s
                JOIN query_runs r ON r.id = s.run_id
                LEFT JOIN strategy_run_contexts c ON c.run_id = s.run_id
                LEFT JOIN observation_outcomes o ON o.sample_id = s.id
                WHERE s.sample_class = 'formal'
                  {(' AND ' + ' AND '.join(clauses)) if clauses else ''}
                """,
                tuple(params),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            metrics = _json_loads(str(item.pop("metrics_json", "") or "{}"), {})
            breakdown = _json_loads(str(item.pop("score_breakdown_json", "") or "{}"), {})
            professional = extract_professional_breakdown(breakdown) or {}
            for field in (
                "selection_profile", "selection_grade", "selection_state",
                "eligibility_status", "required_fields", "missing_required_fields", "data_quality_status",
            ):
                if field in metrics:
                    item[field] = metrics[field]
                elif field in breakdown:
                    item[field] = breakdown[field]
            scoring_context = metrics.get("scoring_context")
            if not isinstance(scoring_context, dict):
                scoring_context = {}
            active_formula = scoring_context.get("active_formula")
            if not isinstance(active_formula, dict):
                active_formula = {}
            item["active_formula_version"] = str(
                metrics.get("active_formula_version")
                or active_formula.get("version")
                or ""
            )
            item["professional_data_quality_score"] = _to_float(
                metrics.get("professional_data_quality_score") or professional.get("data_quality_score")
            )
            item["professional_dimension_scores"] = professional.get("dimension_scores") if isinstance(professional, dict) else {}
            result.append(item)
        return result

    def _filter_sql(self, filters: dict[str, Any]) -> tuple[list[str], list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        start_date = filters.get("start_date") or ""
        end_date = filters.get("end_date") or ""
        horizon_days = filters.get("horizon_days")
        regime = str(filters.get("regime") or "").strip()
        weight_policy_version = str(filters.get("weight_policy_version") or "").strip()
        template_id = str(filters.get("template_id") or "").strip()
        code = str(filters.get("code") or "").strip()
        stock_name = str(filters.get("stock_name") or "").strip()
        industry = str(filters.get("industry") or "").strip()
        strategy_id = filters.get("strategy_id")
        strategy_name = str(filters.get("strategy_name") or "").strip()
        strategy_version = str(filters.get("strategy_version") or "").strip()
        data_quality = str(filters.get("data_quality") or "").strip()
        candidate_type = str(filters.get("candidate_type") or "").strip()
        sample_class = str(filters.get("sample_class") or "").strip()
        evaluation_mode = str(filters.get("evaluation_mode") or "").strip()
        if evaluation_mode:
            sample_class = {
                "formal": "formal",
                "review_only": "review",
                "exploratory": "exploratory",
                "legacy": "legacy",
            }.get(evaluation_mode, evaluation_mode)
        if start_date:
            clauses.append("s.sample_date >= ?")
            params.append(_date_part(start_date))
        if end_date:
            clauses.append("s.sample_date <= ?")
            params.append(_date_part(end_date))
        if horizon_days:
            clauses.append("s.horizon_days = ?")
            params.append(int(horizon_days))
        if regime:
            clauses.append("c.regime = ?")
            params.append(regime)
        if weight_policy_version:
            clauses.append("c.weight_policy_version = ?")
            params.append(weight_policy_version)
        if template_id:
            clauses.append("r.template_id = ?")
            params.append(template_id)
        if code:
            clauses.append("s.code = ?")
            params.append(code.zfill(6) if code.isdigit() else code)
        if stock_name:
            clauses.append("s.name LIKE ?")
            params.append(f"%{stock_name}%")
        if industry:
            clauses.append("s.industry = ?")
            params.append(industry)
        if strategy_id not in (None, ""):
            clauses.append("r.preset_id = ?")
            params.append(int(strategy_id))
        if strategy_name:
            clauses.append("r.preset_name LIKE ?")
            params.append(f"%{strategy_name}%")
        if strategy_version:
            clauses.append("r.template_version = ?")
            params.append(strategy_version)
        if data_quality:
            clauses.append("COALESCE(o.data_quality, 'pending') = ?")
            params.append(data_quality)
        if candidate_type:
            clauses.append("COALESCE(json_extract(s.metrics_json, '$.selection_state'), '') = ?")
            params.append(candidate_type)
        if sample_class:
            clauses.append("s.sample_class = ?")
            params.append(sample_class)
        return clauses, params

    def _weight_policy_from_row(self, row: Any) -> WeightPolicyVersion:
        return WeightPolicyVersion(
            id=int(row["id"]),
            name=row["name"],
            version=row["version"],
            status=row["status"],
            baseline_weights=normalize_weights(_json_loads(row["baseline_weights_json"], {})),
            regime_targets={
                key: normalize_weights(value)
                for key, value in _json_loads(row["regime_targets_json"], {}).items()
                if isinstance(value, dict)
            },
            max_daily_delta=float(row["max_daily_delta"]),
            min_confirm_days=int(row["min_confirm_days"]),
            data_quality_note=row["data_quality_note"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _regime_from_row(self, row: Any) -> MarketRegimeSnapshot:
        return MarketRegimeSnapshot(
            asof_date=row["asof_date"],
            regime=row["regime"],
            confidence=float(row["confidence"]),
            metrics=_json_loads(row["metrics_json"], {}),
            source=row["source"],
            created_at=row["created_at"],
        )

    def _run_context_from_row(self, row: Any) -> StrategyRunContext:
        return StrategyRunContext(
            run_id=int(row["run_id"]),
            asof_date=row["asof_date"],
            regime=row["regime"],
            regime_confidence=float(row["regime_confidence"]),
            weight_policy_id=row["weight_policy_id"],
            weight_policy_name=row["weight_policy_name"],
            weight_policy_version=row["weight_policy_version"],
            effective_weights=normalize_weights(_json_loads(row["effective_weights_json"], {})),
            shadow=bool(row["shadow"]),
            ai_log_id=row["ai_log_id"],
            data_quality_note=row["data_quality_note"],
            created_at=row["created_at"],
        )

    def _sample_from_row(self, row: Any) -> ObservationSample:
        return ObservationSample(
            id=int(row["id"]),
            result_id=int(row["result_id"]),
            run_id=int(row["run_id"]),
            horizon_days=int(row["horizon_days"]),
            sample_date=row["sample_date"],
            due_date=row["due_date"],
            code=str(row["code"]).zfill(6),
            name=row["name"],
            entry_price=_to_float(row["entry_price"]),
            rank_order=int(row["rank_order"]),
            local_score=_to_float(row["local_score"]),
            industry=row["industry"],
            metrics=_json_loads(row["metrics_json"], {}),
            score_breakdown=_json_loads(row["score_breakdown_json"], {}),
            ai_score=_to_float(row["ai_score"]),
            ai_level=row["ai_level"],
            created_at=row["created_at"],
            sample_class=str(row["sample_class"] or "legacy"),
            certificate_id=row["certificate_id"],
        )

    def _professional_formula_from_row(self, row: Any) -> dict[str, Any]:
        return {
            "id": int(row["id"]),
            "name": row["name"],
            "version": row["version"],
            "status": row["status"],
            "dimension_weights": normalize_dimension_weights(_json_loads(row["dimension_weights_json"], {})),
            "field_weights": normalize_field_weights(_json_loads(row["field_weights_json"], {})),
            "evidence": _json_loads(row["evidence_json"], {}),
            "validation": _json_loads(row["validation_json"], {}),
            "ai_log_id": row["ai_log_id"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "activated_at": row["activated_at"],
        }

    def _professional_evidence_rows(self) -> list[dict[str, Any]]:
        with self.database.connect() as conn:
            rows = conn.execute(
                """
                SELECT s.id AS sample_id, s.code, s.name, s.horizon_days, s.sample_date,
                       s.due_date, s.rank_order, s.industry, s.metrics_json,
                       s.score_breakdown_json, r.preset_name, r.source_label,
                       r.template_id, c.regime, c.weight_policy_version,
                       o.exit_date, o.return_pct, o.max_favorable_pct,
                       o.max_drawdown_pct, o.hs300_return_pct, o.excess_vs_hs300_pct,
                       o.direction_hit, o.benchmark_hit, o.rating,
                       o.data_quality, o.data_quality_note
                FROM observation_samples s
                JOIN query_runs r ON r.id = s.run_id
                LEFT JOIN strategy_run_contexts c ON c.run_id = s.run_id
                LEFT JOIN observation_outcomes o ON o.sample_id = s.id
                WHERE s.sample_class = 'formal'
                ORDER BY s.sample_date ASC, s.horizon_days ASC, s.rank_order ASC
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def _recent_complete_round_rows(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        complete_dates = [
            str(row.get("sample_date") or "")
            for row in rows
            if int(row.get("horizon_days") or 0) == 20
            and (row.get("return_pct") is not None or row.get("data_quality"))
        ]
        if not complete_dates:
            return []
        latest_date = max(complete_dates)
        return [row for row in rows if str(row.get("sample_date") or "") == latest_date]

    def _professional_summary_payload(self, rows: list[dict[str, Any]]) -> dict[str, Any]:
        summary = summarize_performance_rows(rows)
        by_horizon: dict[str, dict[str, Any]] = {}
        weighted_score = 0.0
        used_weight = 0.0
        for horizon, period_weight in PROFESSIONAL_OBSERVATION_PERIOD_WEIGHTS.items():
            period_rows = [row for row in rows if int(row.get("horizon_days") or 0) == horizon]
            period_summary = summarize_performance_rows(period_rows)
            by_horizon[str(horizon)] = period_summary
            if period_summary.get("valid_settled_sample_count"):
                weighted_score += float(period_summary.get("robust_score") or 0.0) * period_weight
                used_weight += period_weight
        summary["by_horizon"] = by_horizon
        summary["multi_period_score"] = round(weighted_score / used_weight, 4) if used_weight > 0 else 0.0
        return summary

    def _professional_case_layers(self, rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
        settled = [row for row in rows if row.get("return_pct") is not None or row.get("data_quality")]

        def case(row: dict[str, Any]) -> dict[str, Any]:
            breakdown = extract_professional_breakdown(row.get("score_breakdown_json"))
            return {
                "code": row.get("code"),
                "name": row.get("name"),
                "horizon_days": row.get("horizon_days"),
                "sample_date": row.get("sample_date"),
                "rating": row.get("rating") or "",
                "return_pct": row.get("return_pct"),
                "excess_vs_hs300_pct": row.get("excess_vs_hs300_pct"),
                "max_drawdown_pct": row.get("max_drawdown_pct"),
                "dimension_scores": (breakdown or {}).get("dimension_scores", {}),
                "data_quality": row.get("data_quality") or "pending",
            }

        strong = [case(row) for row in settled if str(row.get("rating") or "") == "A"]
        qualified = [case(row) for row in settled if str(row.get("rating") or "") == "B"]
        failures = [case(row) for row in settled if str(row.get("rating") or "") in {"D", "F"}]
        neutral = [case(row) for row in settled if str(row.get("rating") or "") == "C"]
        strong.sort(key=lambda item: float(item.get("excess_vs_hs300_pct") or -999999), reverse=True)
        qualified.sort(key=lambda item: float(item.get("excess_vs_hs300_pct") or -999999), reverse=True)
        failures.sort(key=lambda item: float(item.get("max_drawdown_pct") or 0.0))
        return {
            "strong_success": strong[:8],
            "qualified_success": qualified[:8],
            "failures": failures[:8],
            "neutral": neutral[:8],
        }

    def _professional_data_gaps(self, rows: list[dict[str, Any]]) -> list[str]:
        counts: dict[str, int] = {}
        for row in rows:
            quality = str(row.get("data_quality") or "")
            if quality and quality != "ok":
                for part in quality.split(","):
                    key = part.strip()
                    if key:
                        counts[key] = counts.get(key, 0) + 1
            breakdown = extract_professional_breakdown(row.get("score_breakdown_json"))
            for field_name in (breakdown or {}).get("missing_fields", [])[:8]:
                counts[f"missing_{field_name}"] = counts.get(f"missing_{field_name}", 0) + 1
        return [f"{key}:{value}" for key, value in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:12]]


class StoreBackedPriceSource:
    def __init__(self, *, pit_database: Any | None = None, fetcher: Any | None = None) -> None:
        self.pit_database = pit_database
        self.fetcher = fetcher
        self._industry_return_cache: dict[tuple[str, str, int], float | None] = {}

    def price_window(self, code: str, start_date: str, horizon_days: int) -> PriceWindow:
        pit_window = self._pit_price_window(code, start_date, horizon_days)
        if _price_window_has_enough_bars(pit_window.bars, start_date, horizon_days):
            return pit_window
        history_window = self._history_price_window(code, start_date, horizon_days)
        if _price_window_has_enough_bars(history_window.bars, start_date, horizon_days):
            return history_window
        return pit_window

    def benchmark_window(self, start_date: str, horizon_days: int) -> PriceWindow:
        return self.price_window(HS300_CODE, start_date, horizon_days)

    def industry_return(self, industry: str, start_date: str, horizon_days: int) -> float | None:
        key = (str(industry or "").strip(), _date_part(start_date), int(horizon_days))
        if key in self._industry_return_cache:
            return self._industry_return_cache[key]
        if not key[0] or self.pit_database is None:
            self._industry_return_cache[key] = None
            return None
        try:
            asof = key[1].replace("-", "")
            with self.pit_database.connect() as conn:
                rows = conn.execute(
                    """
                    SELECT code, industry, asof_date, source
                    FROM pit_industry
                    WHERE TRIM(industry) = ? AND REPLACE(asof_date, '-', '') <= ?
                    ORDER BY code, REPLACE(asof_date, '-', '') DESC, source
                    """,
                    (key[0], asof),
                ).fetchall()
            members: list[str] = []
            seen: set[str] = set()
            for row in rows:
                code = str(row["code"] or "").split(".")[-1].zfill(6)
                if code and code not in seen:
                    members.append(code)
                    seen.add(code)
            returns: list[float] = []
            for code in members:
                window = self._pit_price_window(code, key[1], key[2])
                value = _window_return_pct(window.bars, key[2])
                if value is not None:
                    returns.append(value)
            result = round(sum(returns) / len(returns), 4) if len(returns) >= 3 else None
        except Exception:
            result = None
        self._industry_return_cache[key] = result
        return result

    def _pit_price_window(self, code: str, start_date: str, horizon_days: int) -> PriceWindow:
        if self.pit_database is None:
            return PriceWindow(code=code, source="pit", bars=[])
        start = _date_part(start_date).replace("-", "")
        normalized = str(code).split(".")[-1].zfill(6)
        with self.pit_database.connect() as conn:
            rows = conn.execute(
                """
                SELECT p.trade_date, p.close, p.close_raw, p.high, p.low
                FROM pit_prices p
                WHERE (p.code = ? OR p.code = ?)
                  AND p.adj = 'hfq'
                  AND REPLACE(p.trade_date, '-', '') >= ?
                  AND p.source = (
                      SELECT MIN(source)
                      FROM pit_prices
                      WHERE code = p.code AND trade_date = p.trade_date AND adj = p.adj
                  )
                ORDER BY REPLACE(p.trade_date, '-', '') ASC, p.trade_date ASC
                LIMIT ?
                """,
                (code, normalized, start, max(8, int(horizon_days) + 6)),
            ).fetchall()
        bars = [
            PriceBar(
                trade_date=_date_part(row["trade_date"]),
                close=float(row["close_raw"] if row["close_raw"] is not None else row["close"]),
                high=_to_float(row["high"]),
                low=_to_float(row["low"]),
            )
            for row in rows
            if _to_float(row["close_raw"] if row["close_raw"] is not None else row["close"]) is not None
        ]
        return PriceWindow(code=normalized, source="pit", bars=bars)

    def _history_price_window(self, code: str, start_date: str, horizon_days: int) -> PriceWindow:
        if self.fetcher is None:
            return PriceWindow(code=code, source="history", bars=[])
        try:
            frame, _updated_at = self.fetcher.history(code, days=max(80, int(horizon_days) * 4), force=False)
        except Exception:
            return PriceWindow(code=code, source="history", bars=[])
        bars = _bars_from_frame(frame)
        start = _date_part(start_date)
        return PriceWindow(code=str(code).zfill(6), source="history", bars=[bar for bar in bars if bar.trade_date >= start])


def _clip_price_window(window: PriceWindow, asof_date: str | None) -> PriceWindow:
    """把价格窗口裁剪到有效业务日期，避免结算读到未来数据。"""
    if not asof_date:
        return window
    cutoff = _date_part(asof_date)
    return PriceWindow(
        code=window.code,
        source=window.source,
        bars=[bar for bar in window.bars if _date_part(bar.trade_date) <= cutoff],
    )


class ObservationSettlementService:
    def __init__(self, store: PerformanceStore, price_source: PriceSource) -> None:
        self.store = store
        self.price_source = price_source

    def settle_due_samples(self, asof_date: str | None = None, limit: int = 200) -> int:
        settled = 0
        for sample in self.store.list_due_samples(asof_date=asof_date, limit=limit):
            self.settle_sample(sample)
            settled += 1
        return settled

    def settle_sample(
        self,
        sample: ObservationSample,
        *,
        record_missing_price: bool = True,
        asof_date: str | None = None,
    ) -> ObservationOutcome:
        window = _clip_price_window(
            self.price_source.price_window(sample.code, sample.sample_date, sample.horizon_days),
            asof_date,
        )
        price_metrics = _observation_price_metrics(sample, window.bars)
        if price_metrics is None:
            outcome = ObservationOutcome(
                sample_id=sample.id,
                settled_at=_now(),
                data_quality="missing_price",
                data_quality_note="缺少足够价格窗口，未编造观察收益。",
            )
            if record_missing_price:
                self.store.record_outcome(outcome)
            return outcome

        quality: list[str] = []
        benchmark_window = _clip_price_window(
            self.price_source.benchmark_window(sample.sample_date, sample.horizon_days),
            asof_date,
        )
        benchmark_return = _window_return_pct(benchmark_window.bars, sample.horizon_days)
        excess_vs_hs300 = None
        benchmark_hit = None
        if benchmark_return is None:
            quality.append("missing_benchmark")
        else:
            excess_vs_hs300 = round(price_metrics["return_pct"] - benchmark_return, 6)
            benchmark_hit = excess_vs_hs300 > 0

        industry_return = self.price_source.industry_return(sample.industry, sample.sample_date, sample.horizon_days)
        excess_vs_industry = None
        if sample.industry and industry_return is None:
            quality.append("missing_industry")
        elif industry_return is not None:
            excess_vs_industry = round(price_metrics["return_pct"] - industry_return, 6)

        data_quality = ",".join(quality) if quality else "ok"
        rating = classify_observation_rating(
            price_metrics["return_pct"],
            price_metrics["max_favorable_pct"],
            price_metrics["max_drawdown_pct"],
            excess_vs_hs300,
        )
        outcome = ObservationOutcome(
            sample_id=sample.id,
            settled_at=_now(),
            exit_date=price_metrics["exit_date"],
            exit_price=price_metrics["exit_price"],
            return_pct=price_metrics["return_pct"],
            max_favorable_pct=price_metrics["max_favorable_pct"],
            max_drawdown_pct=price_metrics["max_drawdown_pct"],
            hs300_return_pct=benchmark_return,
            excess_vs_hs300_pct=excess_vs_hs300,
            industry_return_pct=industry_return,
            excess_vs_industry_pct=excess_vs_industry,
            direction_hit=price_metrics["return_pct"] > 0,
            benchmark_hit=benchmark_hit,
            rating=rating,
            data_quality=data_quality,
            data_quality_note="；".join(quality),
            metrics={"price_source": window.source},
        )
        self.store.record_outcome(outcome)
        return outcome


class RegimeDetector:
    def detect(self, metrics: dict[str, Any], asof_date: str | None = None, source: str = "deterministic") -> MarketRegimeSnapshot:
        normalized = {key: _to_float(value) for key, value in metrics.items()}
        required = ["index_vs_ma20_pct", "index_vs_ma60_pct", "ma20_slope_pct", "drawdown_20d_pct", "volatility_20d_pct", "advancers_ratio"]
        missing = [key for key in required if normalized.get(key) is None]
        if len(missing) >= 3:
            return MarketRegimeSnapshot(_date_part(asof_date), REGIME_UNCERTAIN, 0.45, {"missing": missing, **metrics}, source)

        ma20 = normalized.get("index_vs_ma20_pct") or 0.0
        ma60 = normalized.get("index_vs_ma60_pct") or 0.0
        slope = normalized.get("ma20_slope_pct") or 0.0
        drawdown = normalized.get("drawdown_20d_pct") or 0.0
        volatility = normalized.get("volatility_20d_pct") or 0.0
        advancers = normalized.get("advancers_ratio") or 0.0
        amount = normalized.get("amount_vs_ma20") or 1.0
        concentration = normalized.get("industry_concentration") or 0.0
        dispersion = normalized.get("industry_dispersion") or 0.0

        regime = REGIME_RANGE_BOUND
        score = 0.68
        if ma20 > 0 and ma60 > 0 and slope > 0.15 and drawdown > -6 and advancers >= 0.55 and amount >= 1:
            regime, score = REGIME_TREND_BULL, 0.82
        elif drawdown <= -8 or (ma60 < -2 and advancers < 0.45) or (volatility >= 3.5 and advancers < 0.42):
            regime, score = REGIME_RISK_OFF, 0.80
        elif ma20 > 0 and slope > 0 and drawdown <= -5 and advancers >= 0.50:
            regime, score = REGIME_REBOUND, 0.74
        elif concentration >= 0.55 and dispersion >= 0.45 and advancers >= 0.42 and amount >= 0.9:
            regime, score = REGIME_STRUCTURAL_BULL, 0.76
        elif abs(ma20) <= 3 and abs(slope) <= 0.12 and drawdown > -8:
            regime, score = REGIME_RANGE_BOUND, 0.70
        else:
            regime, score = REGIME_UNCERTAIN, 0.58

        confidence = max(0.0, min(0.95, score - len(missing) * 0.06))
        if confidence < 0.65:
            regime = REGIME_UNCERTAIN
        return MarketRegimeSnapshot(_date_part(asof_date), regime, round(confidence, 4), metrics, source)


def build_regime_explain_prompt(snapshot: MarketRegimeSnapshot, effective_weights: dict[str, float]) -> str:
    return f"""
你是A股候选池复盘助手，只解释确定性市场状态和权重偏移，不给买入/卖出建议。

输出严格 JSON，字段：
{{"regime":"状态", "confidence":0.0, "weight_shift":"权重变化解释", "risks":["风险"], "data_gaps":["数据缺口"]}}

市场状态版本：{PROMPT_REGIME_EXPLAIN_V1}
状态：{snapshot.regime}
置信度：{snapshot.confidence}
指标：{_json_dumps(snapshot.metrics)}
生效权重：{_json_dumps(effective_weights)}
基准权重：{_json_dumps(BASELINE_WEIGHTS)}
""".strip()


def build_retrospective_prompt(summary: PerformanceSummary, grouped_rows: list[tuple[str, PerformanceSummary]]) -> str:
    payload = {
        "summary": summary.__dict__,
        "groups": [{"key": key, "summary": item.__dict__} for key, item in grouped_rows],
    }
    return f"""
你是A股候选池复盘助手，只基于已结算观察样本分析系统性偏差，不承诺收益、胜率或预测准确率。

输出严格 JSON，字段：
{{"findings":["偏差"], "repeated_errors":["重复错误"], "sample_gaps":["样本缺口"], "risk_notes":["风险"], "next_checks":["验证条件"]}}

复盘版本：{PROMPT_RETROSPECTIVE_V1}
数据：{_json_dumps(payload)}
""".strip()


def build_weight_candidate_prompt(summary_payload: dict[str, Any]) -> str:
    return f"""
你是A股候选池权重复盘助手，只能基于已结算样本提出候选权重，不能直接生效生产公式。

硬约束：
1. 样本数不足 30 的 regime+horizon 必须输出 should_adjust=false。
2. 单维度建议调整不得超过 0.05。
3. 最多输出 3 组候选权重。
4. 必须说明验证条件、风险、样本缺口。
5. 禁止承诺收益、胜率、预测准确率，禁止生成买入/卖出指令。

输出严格 JSON，字段：
{{"should_adjust":false, "reason":"", "candidates":[{{"name":"","weights":{{}}, "validation":"","risks":[""], "sample_gaps":[""]}}]}}

版本：{PROMPT_WEIGHT_CANDIDATE_V1}
基准权重：{_json_dumps(BASELINE_WEIGHTS)}
已结算样本摘要：{_json_dumps(summary_payload)}
""".strip()


def validate_weight_candidate_output(
    payload: dict[str, Any],
    *,
    settled_sample_count: int,
    reference_weights: dict[str, float] | None = None,
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("AI 权重候选必须是 JSON 对象")
    raw_text = _json_dumps(payload)
    forbidden = ("买入", "卖出", "必涨", "收益承诺", "准确率承诺", "稳赚")
    if any(word in raw_text for word in forbidden):
        raise ValueError("AI 权重候选包含禁止的投资建议或承诺表述")
    reference = normalize_weights(reference_weights or BASELINE_WEIGHTS)
    result = {
        "should_adjust": bool(payload.get("should_adjust")),
        "reason": str(payload.get("reason") or ""),
        "candidates": [],
        "validation_required": True,
    }
    if settled_sample_count < 30:
        result["should_adjust"] = False
        result["reason"] = "已结算样本不足 30，拒绝调权候选。"
        return result
    candidates = payload.get("candidates") or []
    if not isinstance(candidates, list):
        candidates = []
    for row in candidates[:3]:
        if not isinstance(row, dict):
            continue
        weights = row.get("weights") or {}
        if not isinstance(weights, dict):
            continue
        cleaned = normalize_weights(weights, reference)
        if any(abs(cleaned[dimension] - reference[dimension]) > 0.050001 for dimension in reference):
            continue
        result["candidates"].append(
            {
                "name": str(row.get("name") or "shadow_candidate"),
                "weights": cleaned,
                "validation": str(row.get("validation") or ""),
                "risks": _string_list(row.get("risks")),
                "sample_gaps": _string_list(row.get("sample_gaps")),
            }
        )
    if not result["candidates"]:
        result["should_adjust"] = False
    return result


def _rank_value(rank: Any, name: str) -> Any:
    if rank is None:
        return None
    if isinstance(rank, dict):
        return rank.get(name)
    return getattr(rank, name, None)


def _bool_to_db(value: bool | None) -> int | None:
    if value is None:
        return None
    return 1 if value else 0


def _summary_from_rows(rows: list[dict[str, Any]]) -> PerformanceSummary:
    sample_count = len(rows)
    settled_rows = [row for row in rows if row.get("return_pct") is not None or row.get("data_quality")]
    valid_rows = [row for row in settled_rows if str(row.get("data_quality") or "ok") == "ok"]
    invalid_rows = [row for row in settled_rows if str(row.get("data_quality") or "ok") != "ok"]
    direction_values = [row.get("direction_hit") for row in valid_rows if row.get("direction_hit") is not None]
    benchmark_values = [row.get("benchmark_hit") for row in valid_rows if row.get("benchmark_hit") is not None]
    rating_counts: dict[str, int] = {}
    quality_counts: dict[str, int] = {}
    for row in settled_rows:
        rating = str(row.get("rating") or "")
        if rating:
            rating_counts[rating] = rating_counts.get(rating, 0) + 1
        quality = str(row.get("data_quality") or "")
        if quality:
            for part in quality.split(","):
                key = part.strip()
                if key:
                    quality_counts[key] = quality_counts.get(key, 0) + 1
    return PerformanceSummary(
        sample_count=sample_count,
        settled_count=len(settled_rows),
        direction_hit_rate=_avg_bool(direction_values),
        benchmark_hit_rate=_avg_bool(benchmark_values),
        avg_return_pct=_avg(row.get("return_pct") for row in valid_rows),
        avg_excess_vs_hs300_pct=_avg(row.get("excess_vs_hs300_pct") for row in valid_rows),
        avg_max_drawdown_pct=_avg(row.get("max_drawdown_pct") for row in valid_rows),
        rating_counts=rating_counts,
        data_quality_counts=quality_counts,
        valid_settled_count=len(valid_rows),
        invalid_settled_count=len(invalid_rows),
    )


def _avg(values: Any) -> float | None:
    numbers = [_to_float(value) for value in values]
    clean = [value for value in numbers if value is not None]
    if not clean:
        return None
    return round(sum(clean) / len(clean), 6)


def _avg_bool(values: list[Any]) -> float | None:
    if not values:
        return None
    return round(sum(1 for value in values if bool(value)) / len(values), 6)


def _observation_price_metrics(sample: ObservationSample, bars: list[PriceBar]) -> dict[str, Any] | None:
    clean = [bar for bar in bars if _to_float(bar.close) is not None and float(bar.close) > 0]
    if not clean:
        return None
    entry_price = sample.entry_price if sample.entry_price and sample.entry_price > 0 else clean[0].close
    exit_index = _exit_index(clean, sample.sample_date, sample.horizon_days)
    if exit_index is None:
        return None
    observed = clean[: exit_index + 1]
    exit_bar = clean[exit_index]
    high_values = [bar.high if bar.high is not None and bar.high > 0 else bar.close for bar in observed]
    low_values = [bar.low if bar.low is not None and bar.low > 0 else bar.close for bar in observed]
    return {
        "exit_date": exit_bar.trade_date,
        "exit_price": round(exit_bar.close, 6),
        "return_pct": round((exit_bar.close / entry_price - 1) * 100, 6),
        "max_favorable_pct": round((max(high_values) / entry_price - 1) * 100, 6),
        "max_drawdown_pct": round((min(low_values) / entry_price - 1) * 100, 6),
    }


def _window_return_pct(bars: list[PriceBar], horizon_days: int) -> float | None:
    clean = [bar for bar in bars if _to_float(bar.close) is not None and float(bar.close) > 0]
    if not clean:
        return None
    exit_index = _exit_index(clean, clean[0].trade_date, horizon_days)
    if exit_index is None:
        return None
    return round((clean[exit_index].close / clean[0].close - 1) * 100, 6)


def _price_window_has_enough_bars(bars: list[PriceBar], start_date: str, horizon_days: int) -> bool:
    clean = [bar for bar in bars if _to_float(bar.close) is not None and float(bar.close) > 0]
    return _exit_index(clean, start_date, horizon_days) is not None


def _exit_index(bars: list[PriceBar], start_date: str, horizon_days: int) -> int | None:
    if not bars:
        return None
    first_is_start = bars[0].trade_date <= _date_part(start_date)
    index = int(horizon_days) if first_is_start else int(horizon_days) - 1
    if index < 0 or len(bars) <= index:
        return None
    return index


def _bars_from_frame(frame: Any) -> list[PriceBar]:
    if frame is None or getattr(frame, "empty", True):
        return []
    date_col = _first_col(frame, ["日期", "date", "day", "交易日期"])
    close_col = _first_col(frame, ["收盘", "close"])
    high_col = _first_col(frame, ["最高", "high"])
    low_col = _first_col(frame, ["最低", "low"])
    if not date_col or not close_col:
        return []
    bars: list[PriceBar] = []
    for _index, row in frame.iterrows():
        close = _to_float(row.get(close_col))
        if close is None or close <= 0:
            continue
        bars.append(
            PriceBar(
                trade_date=_date_part(str(row.get(date_col) or "")),
                close=close,
                high=_to_float(row.get(high_col)) if high_col else None,
                low=_to_float(row.get(low_col)) if low_col else None,
            )
        )
    bars.sort(key=lambda item: item.trade_date)
    return bars


def _first_col(frame: Any, names: list[str]) -> str | None:
    columns = list(getattr(frame, "columns", []))
    lower = {str(column).lower(): column for column in columns}
    for name in names:
        if name in columns:
            return name
        if name.lower() in lower:
            return lower[name.lower()]
    return None


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()]


def _dashboard_next_action(
    *,
    due_count: int,
    settled_count: int,
    data_gap_count: int,
    shadow_policy_count: int,
) -> str:
    if due_count > 0:
        return f"先结算 {due_count} 条到期观察样本，再看战绩归因。"
    if settled_count < 30:
        return (
            f"继续累积已结算样本（当前 {settled_count}/30）；"
            "自动复盘不会改变候选资格、排序或人工观察状态。"
        )
    if data_gap_count > 0:
        return "先修补价格、基准或行业数据缺口，避免把缺数当成策略表现。"
    return "样本量已达基础门槛，可以按市场状态和周期继续复盘；候选资格、排序和人工观察状态不由复盘改变。"
