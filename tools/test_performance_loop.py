from __future__ import annotations

import sqlite3
import sys
import tempfile
import json
from contextlib import closing
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from stock_tool.core.performance_loop import (
    BASELINE_WEIGHTS,
    PROFESSIONAL_DEFAULT_DIMENSION_WEIGHTS,
    PROFESSIONAL_DEFAULT_FIELD_WEIGHTS,
    REGIME_RISK_OFF,
    REGIME_TREND_BULL,
    REGIME_UNCERTAIN,
    ObservationSettlementService,
    ObservationOutcome,
    PerformanceStore,
    PriceBar,
    PriceWindow,
    RegimeDetector,
    StoreBackedPriceSource,
    add_trading_days,
    build_professional_score_breakdown,
    build_weight_candidate_prompt,
    classify_observation_rating,
    redistribute_missing_dimensions,
    smooth_weight_transition,
    validate_weight_candidate_output,
)
from stock_tool.core.factor_store import FactorSnapshot
from stock_tool.core.data_snapshot import DataSnapshotFact, DataSnapshotStore
from stock_tool.core.formal_certificate import (
    FormalScreeningCertificate,
    FormalScreeningCertificateStore,
    UniverseDefinition,
)
from stock_tool.core.professional_formula import (
    PROFESSIONAL_DIMENSIONS,
    build_professional_weight_candidate_prompt,
    propose_professional_shadow_candidate,
    summarize_performance_rows,
    validate_professional_candidate_payload,
)
from stock_tool.core.query_schema import QuerySpec
from stock_tool.core.screener_engine import ScreenFilter, screen_factor_snapshots
from stock_tool.core.strategy_store import StrategyStore
from stock_tool.core.models import FinancialMetrics, StockQuote
from stock_tool.core.technical_profile import TechnicalProfile
from stock_tool.core.v1_database import V1Database, V1_SCHEMA_VERSION, v1_db_path_for


def _insert_run(
    db: V1Database,
    *,
    code: str = "000001",
    name: str = "样例银行",
    price: float = 10.0,
    industry: str = "银行",
    created_at: str = "2026-01-02T15:00:00",
) -> tuple[int, int]:
    with db.connect() as conn:
        cursor = conn.execute(
            """
            INSERT INTO query_runs(
                preset_id, preset_name, source_label, user_text, query_spec_json,
                summary, data_source, scanned, matched, skipped, failed, note,
                template_id, template_version, universe_id, universe_hash, config_hash, created_at
            )
            VALUES(NULL, '闭环测试', '自然语言策略', '测试策略', '{}',
                   '测试策略摘要', 'v1', 1, 1, 0, 0, '',
                   'template_a', 'v1', 'all_a', 'universe_hash', 'config_hash', ?)
            """,
            (created_at,),
        )
        run_id = int(cursor.lastrowid)
        result_cursor = conn.execute(
            """
            INSERT INTO query_run_results(
                run_id, rank_order, code, name, local_score, reason,
                risk_flags_json, metrics_json, score_breakdown_json
            )
            VALUES(?, 1, ?, ?, 88.0, '测试理由', '[]', ?, ?)
            """,
            (
                run_id,
                code,
                name,
                (
                    '{"price": %.2f, "industry": "%s", "roe": 18, "gross_margin": 40, '
                    '"net_margin": 15, "debt_ratio": 45, "revenue_growth": 20, '
                    '"profit_growth": 18, "pe_ttm": 18, "pb": 2.1, "trend_20d": 6, '
                    '"trend_60d": 12, "box_score": 80, "industry_strength": 75}'
                )
                % (price, industry),
                (
                    '{"professional_formula":{"version":"professional_formula_v1",'
                    '"dimension_scores":{"quality":82,"growth":78,"valuation":72,'
                    '"safety":76,"trend":74,"industry":75,"money_flow":45},'
                    '"missing_fields":["main_5d_yi"]}}'
                ),
            ),
        )
    return run_id, int(result_cursor.lastrowid)


def _bars(start: str, closes: list[float]) -> list[PriceBar]:
    rows = []
    for offset, close in enumerate(closes):
        trade_date = start if offset == 0 else add_trading_days(start, offset)
        rows.append(PriceBar(trade_date=trade_date, close=close, high=close + 0.4, low=close - 0.5))
    return rows


def _prepare_formal_certificate(db: V1Database, run_id: int, *, code: str = "000001") -> str:
    """Give summary-focused fixtures the same provenance as production formal runs."""
    snapshots = DataSnapshotStore(db)
    event = snapshots.record_refresh_attempt(
        asof_at="2026-01-02T09:00:00+00:00",
        availability_cutoff="2026-01-02T09:00:00+00:00",
        data_source="fixture",
        universe_id="a_share_listed_pit",
        universe_version="fixture-u-v1",
        dataset_version="fixture-d-v1",
        source_policy_version="fixture-policy-v1",
        source_versions={"fixture": "v1"},
        started_at="2026-01-02T09:00:00+00:00",
    )
    manifest = snapshots.finalize_refresh(
        event.refresh_event_id,
        [
            DataSnapshotFact(
                snapshot_id="pending",
                code=code,
                field_name="roe",
                value=20,
                effective_at="2026-01-01T00:00:00+00:00",
                available_at="2026-01-02T08:00:00+00:00",
                source="fixture",
                source_version="v1",
                fetched_at="2026-01-02T09:00:00+00:00",
            )
        ],
        status="success",
        quality_status="verified",
        quality_summary={"fact_total": 1, "verified_fact_count": 1},
        finished_at="2026-01-02T09:01:00+00:00",
    )
    assert manifest is not None
    with db.connect() as conn:
        existing = conn.execute(
            "SELECT scoring_context_json FROM query_runs WHERE id = ?",
            (int(run_id),),
        ).fetchone()
        context = json.loads(existing["scoring_context_json"] or "{}") if existing else {}
        context.update({"execution_mode": "formal", "screening_status": "verified"})
        conn.execute(
            "UPDATE query_runs SET scoring_context_json = ? WHERE id = ?",
            (json.dumps(context), int(run_id)),
        )
    certificate = FormalScreeningCertificate.issue(
        run_id=run_id,
        data_snapshot_id=manifest.snapshot_id,
        refresh_event_id=event.refresh_event_id,
        universe=UniverseDefinition(
            universe_definition_id="a_share_listed_pit",
            universe_version="fixture-u-v1",
            members_digest="fixture-members-v1",
            selection_scope="fixture",
            selection_policy_version="v1",
            selected_limit=1,
            member_status_policy="listed_non_st",
        ),
        selection_scope="fixture",
        formula_version="fixture-formula-v1",
        execution_mode="formal",
        quality_status="verified",
        quality_summary={"fact_total": 1, "verified_fact_count": 1},
        condition_hash="fixture-condition-v1",
        result_digest="fixture-result-v1",
    )
    FormalScreeningCertificateStore(db).save(certificate)
    return certificate.certificate_id


class FakePriceSource:
    def __init__(
        self,
        prices: dict[str, list[PriceBar]],
        benchmark: list[PriceBar] | None = None,
        industry_returns: dict[str, float] | None = None,
    ) -> None:
        self.prices = prices
        self.benchmark = benchmark or []
        self.industry_returns = industry_returns or {}

    def price_window(self, code: str, start_date: str, horizon_days: int) -> PriceWindow:
        return PriceWindow(code=code, source="fake", bars=self.prices.get(code, []))

    def benchmark_window(self, start_date: str, horizon_days: int) -> PriceWindow:
        return PriceWindow(code="000300", source="fake_benchmark", bars=self.benchmark)

    def industry_return(self, industry: str, start_date: str, horizon_days: int) -> float | None:
        return self.industry_returns.get(industry)


def test_schema_and_sample_creation() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        db_path = v1_db_path_for(Path(tmp))
        db = V1Database(db_path)
        db.init_schema()
        store = PerformanceStore(db)
        store.ensure_default_weight_policy()
        run_id, result_id = _insert_run(db)
        store.save_run_context(run_id, asof_date="2026-01-02")
        certificate_id = _prepare_formal_certificate(db, run_id)
        inserted = store.create_formal_observation_samples(run_id, certificate_id)
        inserted_again = store.create_formal_observation_samples(run_id, certificate_id)
        ai_updated = store.update_sample_ai_scores(run_id, {"000001": {"ai_score": 91, "level": "A"}})

        with closing(sqlite3.connect(db_path)) as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            sample_count = conn.execute("SELECT COUNT(*) FROM observation_samples WHERE result_id = ?", (result_id,)).fetchone()[0]
            ai_count = conn.execute(
                "SELECT COUNT(*) FROM observation_samples WHERE run_id = ? AND ai_score = 91 AND ai_level = 'A'",
                (run_id,),
            ).fetchone()[0]
        assert version == V1_SCHEMA_VERSION == 16
        assert {
            "market_regime_snapshots",
            "weight_policy_versions",
            "strategy_run_contexts",
            "observation_samples",
            "observation_outcomes",
            "ai_decision_logs",
            "professional_formula_candidates",
            "professional_weight_ranges",
            "professional_formula_memory",
            "formula_governance_audit",
        }.issubset(tables)
        assert inserted == 4
        assert inserted_again == 0
        assert sample_count == 4
        assert ai_updated == 4
        assert ai_count == 4
        dashboard = store.dashboard_snapshot(asof_date="2026-03-01")
        assert dashboard.due_count == 4
        assert dashboard.due_by_horizon == {3: 1, 5: 1, 10: 1, 20: 1}
        assert set(dashboard.period_summaries) == {3, 5, 10, 20}
        assert dashboard.period_summaries[3].sample_count == 1
        assert dashboard.period_summaries[3].settled_count == 0
        assert "结算 4 条" in dashboard.next_action
        db.close()


def test_settlement_metrics_quality_and_summary() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        db = V1Database(v1_db_path_for(Path(tmp)))
        store = PerformanceStore(db)
        run_id, _result_id = _insert_run(db)
        store.save_run_context(run_id, asof_date="2026-01-02")
        certificate_id = _prepare_formal_certificate(db, run_id)
        store.create_formal_observation_samples(run_id, certificate_id)
        stock_bars = _bars(
            "2026-01-02",
            [10.0, 10.8, 11.4, 10.9, 11.6, 12.0, 12.4, 12.8, 12.6, 13.0, 13.5, 13.8, 14.0, 14.2, 14.5, 14.8, 15.0, 15.3, 15.6, 16.0, 16.3, 16.6],
        )
        benchmark_bars = _bars("2026-01-02", [10.0 + index * 0.05 for index in range(22)])
        service = ObservationSettlementService(
            store,
            FakePriceSource({"000001": stock_bars}, benchmark=benchmark_bars, industry_returns={"银行": 1.2}),
        )
        settled = service.settle_due_samples(asof_date="2026-03-01")
        assert settled == 4

        rows = store.list_recent_outcomes(limit=10)
        by_horizon = {int(row["horizon_days"]): row for row in rows}
        assert set(by_horizon) == {3, 5, 10, 20}
        for horizon in (3, 5, 10, 20):
            assert by_horizon[horizon]["return_pct"] is not None
            assert by_horizon[horizon]["max_favorable_pct"] is not None
            assert by_horizon[horizon]["max_drawdown_pct"] is not None
            assert by_horizon[horizon]["rating"] in {"A", "B", "C", "D", "F"}
        assert round(float(by_horizon[3]["return_pct"]), 2) == 9.0
        assert float(by_horizon[3]["max_favorable_pct"]) > 10
        assert float(by_horizon[3]["max_drawdown_pct"]) < 0
        assert by_horizon[3]["rating"] in {"A", "B"}
        assert by_horizon[3]["data_quality"] == "ok"

        summary = store.summarize()
        assert summary.sample_count == 4
        assert summary.settled_count == 4
        assert summary.direction_hit_rate == 1.0
        assert summary.benchmark_hit_rate == 1.0
        dashboard = store.dashboard_snapshot(asof_date="2026-03-01")
        assert dashboard.due_count == 0
        assert dashboard.settled_count == 4
        for horizon in (3, 5, 10, 20):
            assert dashboard.period_summaries[horizon].sample_count == 1
            assert dashboard.period_summaries[horizon].settled_count == 1
            assert dashboard.period_summaries[horizon].direction_hit_rate == 1.0
        assert dashboard.strongest_strategy == "闭环测试"
        assert dashboard.weakest_strategy == "闭环测试"
        assert "继续累积" in dashboard.next_action
        db.close()


def test_missing_price_benchmark_and_industry_quality() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        db = V1Database(v1_db_path_for(Path(tmp)))
        store = PerformanceStore(db)
        run_id, _result_id = _insert_run(db, code="000002", name="缺口样例", industry="医药")
        store.save_run_context(run_id, asof_date="2026-01-02")
        store.create_observation_samples_for_run(run_id, horizons=(3,))
        service = ObservationSettlementService(store, FakePriceSource({}))
        service.settle_due_samples(asof_date="2026-03-01")
        missing_price = store.list_recent_outcomes(limit=1)[0]
        assert missing_price["data_quality"] == "missing_price"

        run_id, _result_id = _insert_run(db, code="000003", name="缺基准样例", industry="银行")
        store.save_run_context(run_id, asof_date="2026-01-02")
        store.create_observation_samples_for_run(run_id, horizons=(3,))
        service = ObservationSettlementService(
            store,
            FakePriceSource({"000003": _bars("2026-01-02", [10, 10.5, 10.8, 10.7])}),
        )
        service.settle_due_samples(asof_date="2026-03-01")
        outcome_rows = store.list_recent_outcomes(limit=10)
        quality = next(row["data_quality"] for row in outcome_rows if row["code"] == "000003")
        assert "missing_benchmark" in quality
        assert "missing_industry" in quality
        db.close()


def test_rating_boundaries() -> None:
    assert classify_observation_rating(6, 7, -4, 1) == "A"
    assert classify_observation_rating(3, 3, -9, 1) == "B"
    assert classify_observation_rating(2, 2, -4, -1) == "C"
    assert classify_observation_rating(-1.5, 1, -6, -2) == "C"
    assert classify_observation_rating(-5, 1, -10, -3) == "D"
    assert classify_observation_rating(-8.1, 1, -10, -3) == "F"
    assert classify_observation_rating(-1, 1, -15.1, 0) == "F"


def test_regime_and_weight_rules() -> None:
    detector = RegimeDetector()
    bull = detector.detect(
        {
            "index_vs_ma20_pct": 3,
            "index_vs_ma60_pct": 6,
            "ma20_slope_pct": 0.3,
            "drawdown_20d_pct": -3,
            "volatility_20d_pct": 1.4,
            "advancers_ratio": 0.62,
            "amount_vs_ma20": 1.1,
        },
        asof_date="2026-01-02",
    )
    assert bull.regime == REGIME_TREND_BULL
    uncertain = detector.detect({"index_vs_ma20_pct": None}, asof_date="2026-01-02")
    assert uncertain.regime == REGIME_UNCERTAIN

    current = dict(BASELINE_WEIGHTS)
    risk_target = {"quality": 0.30, "growth": 0.10, "safety": 0.30, "valuation": 0.25, "trend": 0.05, "industry": 0.0}
    assert smooth_weight_transition(current, risk_target, confidence=0.6, confirmed_days=5) == current
    assert smooth_weight_transition(current, risk_target, confidence=0.8, confirmed_days=2) == current
    next_weights = smooth_weight_transition(current, risk_target, confidence=0.8, confirmed_days=3)
    assert round(next_weights["growth"], 2) == 0.17
    assert round(next_weights["safety"], 2) == 0.23

    redistributed, note = redistribute_missing_dimensions(dict(BASELINE_WEIGHTS), {"roe", "debt_ratio", "pe_ttm"})
    assert redistributed["industry"] == 0
    assert "industry" in note


def test_ai_prompt_constraints_and_shadow_policy() -> None:
    prompt = build_weight_candidate_prompt({"regime": REGIME_RISK_OFF, "settled_sample_count": 12})
    assert "weight_candidate_v1" in prompt
    blocked = validate_weight_candidate_output(
        {"should_adjust": True, "candidates": [{"weights": dict(BASELINE_WEIGHTS)}]},
        settled_sample_count=12,
    )
    assert blocked["should_adjust"] is False
    assert not blocked["candidates"]

    invalid = dict(BASELINE_WEIGHTS)
    invalid["growth"] = 0.35
    invalid["quality"] = 0.15
    rejected = validate_weight_candidate_output(
        {"should_adjust": True, "candidates": [{"weights": invalid}]},
        settled_sample_count=40,
    )
    assert rejected["should_adjust"] is False

    valid = dict(BASELINE_WEIGHTS)
    valid["quality"] = 0.31
    valid["growth"] = 0.19
    accepted = validate_weight_candidate_output(
        {"should_adjust": True, "candidates": [{"name": "small_shift", "weights": valid, "validation": "shadow 20日"}]},
        settled_sample_count=40,
    )
    assert accepted["should_adjust"] is True
    assert accepted["candidates"][0]["name"] == "small_shift"

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        db = V1Database(v1_db_path_for(Path(tmp)))
        store = PerformanceStore(db)
        active = store.ensure_default_weight_policy()
        store.save_weight_policy(
            name="ai_candidate",
            version="v1",
            status="shadow",
            baseline_weights=valid,
            regime_targets={REGIME_RISK_OFF: valid},
        )
        assert store.get_active_weight_policy().id == active.id
        formula = store.ensure_default_professional_formula()
        with db.connect() as conn:
            conn.execute(
                """
                INSERT INTO professional_formula_candidates(
                    name, version, status, dimension_weights_json, field_weights_json,
                    evidence_json, validation_json, created_at, updated_at, activated_at
                )
                VALUES('professional_shadow_test', 'v1', 'shadow', ?, ?, '{}', ?, '2026-01-02', '2026-01-02', '')
                """,
                (
                    json.dumps(formula["dimension_weights"], ensure_ascii=False, sort_keys=True),
                    json.dumps(formula["field_weights"], ensure_ascii=False, sort_keys=True),
                    json.dumps({"status": "shadow", "reason": "继续观察"}, ensure_ascii=False, sort_keys=True),
                ),
            )
        dashboard = store.dashboard_snapshot(asof_date="2026-03-01")
        assert dashboard.shadow_policy_count == 1
        assert dashboard.latest_shadow_policy == "professional_shadow_test v1（继续观察）"
        db.close()


def test_invalid_outcomes_do_not_count_for_professional_formula() -> None:
    summary = summarize_performance_rows(
        [
            {
                "return_pct": 8.0,
                "direction_hit": True,
                "benchmark_hit": True,
                "excess_vs_hs300_pct": 4.0,
                "max_drawdown_pct": -2.0,
                "rating": "A",
                "data_quality": "ok",
            },
            {
                "return_pct": -99.0,
                "direction_hit": False,
                "benchmark_hit": False,
                "excess_vs_hs300_pct": -99.0,
                "max_drawdown_pct": -99.0,
                "rating": "F",
                "data_quality": "missing_benchmark",
            },
        ]
    )
    assert summary["settled_sample_count"] == 2
    assert summary["valid_settled_sample_count"] == 1
    assert summary["invalid_settled_sample_count"] == 1
    assert summary["direction_hit_rate"] == 1.0
    assert summary["avg_excess_vs_hs300_pct"] == 4.0

    evidence = _professional_evidence()
    evidence["long_term"]["valid_settled_sample_count"] = 20
    rejected = validate_professional_candidate_payload({"should_adjust": False}, evidence)
    assert rejected.accepted is False
    assert rejected.checks["settled_sample_count"] == 40
    assert rejected.checks["valid_settled_sample_count"] == 20


def _professional_evidence(
    *,
    settled: int = 40,
    quality: float = 92.0,
    active_shift: bool = True,
) -> dict:
    active_dimensions = dict(PROFESSIONAL_DEFAULT_DIMENSION_WEIGHTS)
    active_fields = {dimension: dict(fields) for dimension, fields in PROFESSIONAL_DEFAULT_FIELD_WEIGHTS.items()}
    precise_dimensions = dict(active_dimensions)
    if active_shift:
        precise_dimensions["quality"] += 0.03
        precise_dimensions["growth"] -= 0.03
    ranges = {
        "dimensions": {
            dimension: {
                "min": max(0.01, weight - 0.08),
                "max": min(0.45, weight + 0.08),
                "center": weight,
                "sample_count": settled,
            }
            for dimension, weight in active_dimensions.items()
        },
        "fields": {
            dimension: {
                field: {
                    "min": max(0.01, weight - 0.12),
                    "max": min(1.0, weight + 0.12),
                    "center": weight,
                    "sample_count": settled,
                }
                for field, weight in fields.items()
            }
            for dimension, fields in active_fields.items()
        },
    }
    return {
        "active": {
            "id": 1,
            "name": "stocklens_professional",
            "version": "v1",
            "dimension_weights": active_dimensions,
            "field_weights": active_fields,
        },
        "long_term": {
            "sample_count": settled,
            "settled_sample_count": settled,
            "direction_hit_rate": 0.62,
            "benchmark_hit_rate": 0.58,
            "avg_excess_vs_hs300_pct": 1.8,
            "avg_max_drawdown_pct": -7.5,
            "data_quality_score": quality,
            "multi_period_score": 76.0,
            "by_horizon": {
                str(horizon): {
                    "sample_count": max(1, settled // 4),
                    "settled_sample_count": max(1, settled // 4),
                    "direction_hit_rate": 0.60,
                    "benchmark_hit_rate": 0.56,
                    "avg_excess_vs_hs300_pct": 1.2,
                    "avg_max_drawdown_pct": -8.0,
                    "data_quality_score": quality,
                }
                for horizon in (3, 5, 10, 20)
            },
        },
        "recent_round": {
            "sample_count": 8,
            "settled_sample_count": 8,
            "data_quality_score": quality,
            "multi_period_score": 78.0,
        },
        "recent_precise_weights": {
            "dimension_weights": precise_dimensions,
            "field_weights": active_fields,
        },
        "historical_weight_ranges": ranges,
        "cases": {
            "strong_success": [{"code": "000001", "rating": "A", "excess_vs_hs300_pct": 3.2}],
            "failures": [{"code": "000002", "rating": "F", "max_drawdown_pct": -16.0}],
        },
        "data_gaps": ["missing_industry:2"],
    }


def test_professional_formula_scoring_missing_and_prompt() -> None:
    quote = StockQuote(
        code="000001",
        name="专业样例",
        price=10,
        pe=18,
        pe_ttm=16,
        pb=2.0,
        industry="银行",
        industry_strength=76,
    )
    metrics = FinancialMetrics(
        roe=18,
        gross_margin=42,
        net_margin=16,
        debt_ratio=45,
        revenue_growth=18,
        profit_growth=22,
    )
    technical = TechnicalProfile(True, close=10.5, ma20=10.0, rsi14=56, box_score=82, max_drawdown_60d=-7)
    full = build_professional_score_breakdown(quote, metrics, technical, extra_fields={"main_5d_yi": 0.8})
    missing = build_professional_score_breakdown(StockQuote(code="000002", name="缺失样例"), FinancialMetrics(), TechnicalProfile(False))
    assert full["score"] > missing["score"]
    assert full["field_scores"]["quality"]["roe"]["score"] is not None
    assert "main_5d_yi" in missing["missing_fields"]
    prompt = build_professional_weight_candidate_prompt(_professional_evidence())
    assert "professional_weight_candidate_v1" in prompt
    assert "买入" in prompt and "禁止" in prompt


def test_professional_formula_active_weights_enter_snapshot_screening() -> None:
    snapshots = [
        FactorSnapshot(
            code="600001",
            name="权重样例",
            price=10,
            pe=18,
            pe_ttm=18,
            pb=2.0,
            roe=25,
            gross_margin=60,
            net_margin=25,
            debt_ratio=45,
            revenue_growth=-15,
            profit_growth=-20,
            trend_20d=5,
            box_score=70,
            technical_status="candidate",
        )
    ]
    quality_formula = {
        "dimension_weights": {dimension: (1.0 if dimension == "quality" else 0.0) for dimension in PROFESSIONAL_DIMENSIONS},
        "field_weights": {"quality": {"roe": 1.0, "gross_margin": 0.0, "net_margin": 0.0}},
    }
    growth_formula = {
        "dimension_weights": {dimension: (1.0 if dimension == "growth" else 0.0) for dimension in PROFESSIONAL_DIMENSIONS},
        "field_weights": {"growth": {"revenue_growth": 1.0, "profit_growth": 0.0}},
    }
    filters = ScreenFilter(min_roe=None, max_debt_ratio=None, max_pe=None, result_limit=1)

    quality_report = screen_factor_snapshots(snapshots, filters, professional_formula=quality_formula)
    growth_report = screen_factor_snapshots(snapshots, filters, professional_formula=growth_formula)

    quality_breakdown = quality_report.results[0].score_breakdown["shadow_formula"]
    growth_breakdown = growth_report.results[0].score_breakdown["shadow_formula"]
    assert quality_breakdown["dimension_weights"]["quality"] == 1.0
    assert growth_breakdown["dimension_weights"]["growth"] == 1.0
    assert quality_breakdown["score"] > growth_breakdown["score"]


def test_professional_formula_score_is_primary_sort_key_and_traceable() -> None:
    snapshots = [
        FactorSnapshot(
            code="600010",
            name="旧分更高",
            price=10,
            pe=20,
            pe_ttm=20,
            pb=2.0,
            roe=5,
            gross_margin=10,
            net_margin=2,
            debt_ratio=40,
            revenue_growth=5,
            profit_growth=5,
            trend_20d=12,
            box_score=100,
            industry_strength=100,
            technical_status="triggered",
            technical_label="S级机会",
        ),
        FactorSnapshot(
            code="600011",
            name="专业分更高",
            price=10,
            pe=30,
            pe_ttm=30,
            pb=2.5,
            roe=25,
            gross_margin=60,
            net_margin=25,
            debt_ratio=90,
            revenue_growth=-10,
            profit_growth=-10,
            trend_20d=-2,
            box_score=0,
            industry_strength=10,
            technical_status="candidate",
        ),
    ]
    formula = {
        "id": 7,
        "name": "quality_only",
        "version": "v-test",
        "status": "active",
        "dimension_weights": {dimension: (1.0 if dimension == "quality" else 0.0) for dimension in PROFESSIONAL_DIMENSIONS},
        "field_weights": {"quality": {"roe": 1.0, "gross_margin": 0.0, "net_margin": 0.0}},
    }
    filters = ScreenFilter(min_roe=None, max_debt_ratio=None, max_pe=None, result_limit=2)
    report = screen_factor_snapshots(snapshots, filters, professional_formula=formula)

    assert [item.quote.code for item in report.results] == ["600010", "600011"]
    assert report.results[0].local_score == report.results[0].score_breakdown["professional_formula"]["score"]
    assert report.results[0].score_breakdown["shadow_formula"]["score"] < report.results[1].score_breakdown["shadow_formula"]["score"]
    assert report.results[0].legacy_score is not None
    assert report.results[0].legacy_score > report.results[1].legacy_score

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        db = V1Database(v1_db_path_for(Path(tmp)))
        db.init_schema()
        strategy_store = StrategyStore(db)
        run_id = strategy_store.record_run(
            source_label="测试",
            user_text="专业分排序测试",
            spec=QuerySpec(raw_text="专业分排序测试", scan_limit=50, result_limit=2),
            data_source="v1",
            report=report,
            professional_formula=formula,
        )
        detail = strategy_store.get_run(run_id)
        context = detail.scoring_context
        assert context["scoring_mode"] == "fixed_baseline_formula_with_shadow"
        assert context["baseline_formula"]["id"] == "professional_formula.baseline"
        assert context["shadow_formula"]["id"] == 7
        assert context["active_formula"]["id"] == 7
        assert context["active_formula"]["version"] == "v-test"
        assert context["formula_hash"]

        performance_store = PerformanceStore(db)
        certificate_id = _prepare_formal_certificate(db, run_id, code="600010")
        inserted = performance_store.create_formal_observation_samples(run_id, certificate_id, horizons=(3,))
        assert inserted == 2
        with db.connect() as conn:
            sample = conn.execute(
                "SELECT metrics_json, score_breakdown_json FROM observation_samples WHERE code = '600011'"
            ).fetchone()
        metrics = json.loads(sample["metrics_json"])
        breakdown = json.loads(sample["score_breakdown_json"])
        sample_candidate = next(item for item in report.results if item.quote.code == "600011")
        grouped_by_profile = performance_store.grouped_summary("selection_profile")
        grouped_by_dimension = dict(performance_store.grouped_summary("dimension_high"))
        grouped_by_professional_quality = dict(performance_store.grouped_summary("professional_data_quality"))
        grouped_by_formula = dict(performance_store.grouped_summary("active_formula_version"))
        recent_outcome_rows = performance_store.list_recent_outcomes(limit=5)
        recent_sample = next(item for item in recent_outcome_rows if item["code"] == "600011")
        assert any(key == sample_candidate.score_breakdown["selection_profile"] for key, _summary in grouped_by_profile)
        assert "quality:high" in grouped_by_dimension
        assert set(grouped_by_professional_quality).issubset({"quality_ok", "quality_gap", "quality_unknown"})
        assert grouped_by_professional_quality
        assert "v-test" in grouped_by_formula
        assert recent_sample["selection_profile"] == sample_candidate.score_breakdown["selection_profile"]
        assert recent_sample["selection_grade"] == sample_candidate.score_breakdown["selection_grade"]
        assert recent_sample["selection_state"] == sample_candidate.score_breakdown["selection_state"]
        assert metrics["primary_score"] == sample_candidate.local_score
        assert metrics["legacy_score"] == sample_candidate.legacy_score
        assert metrics["professional_data_quality_score"] == sample_candidate.score_breakdown["professional_formula"]["data_quality_score"]
        assert metrics["scoring_context"]["active_formula"]["id"] == 7
        assert metrics["active_formula_version"] == "v-test"
        assert metrics["selection_profile"] == sample_candidate.score_breakdown["selection_profile"]
        assert metrics["selection_grade"] == sample_candidate.score_breakdown["selection_grade"]
        assert metrics["selection_state"] == sample_candidate.score_breakdown["selection_state"]
        assert metrics["veto_reasons"] == sample_candidate.score_breakdown["veto_reasons"]
        assert metrics["evidence_gaps"] == sample_candidate.score_breakdown["evidence_gaps"]
        assert breakdown["professional_formula"]["score"] == sample_candidate.local_score
        assert breakdown["shadow_formula"]["score"] == sample_candidate.score_breakdown["shadow_formula"]["score"]
        sample_id = sample["id"] if "id" in sample.keys() else None
        if sample_id is None:
            with db.connect() as conn:
                sample_id = conn.execute("SELECT id FROM observation_samples WHERE code = '600011'").fetchone()["id"]
        performance_store.record_outcome(
            ObservationOutcome(
                sample_id=int(sample_id),
                settled_at="2026-01-08T15:00:00",
                exit_date="2026-01-08",
                exit_price=11.2,
                return_pct=12.0,
                max_favorable_pct=13.0,
                max_drawdown_pct=-3.0,
                hs300_return_pct=2.0,
                excess_vs_hs300_pct=10.0,
                direction_hit=True,
                benchmark_hit=True,
                rating="A",
                data_quality="ok",
            )
        )
        grouped_by_quality = dict(performance_store.grouped_summary("data_quality"))
        assert grouped_by_quality["ok"].settled_count == 1
        db.close()


class _SimpleFrame:
    columns = ["date", "close", "high", "low"]
    empty = False

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def iterrows(self):
        return iter(enumerate(self._rows))


class _HistoryFetcher:
    def history(self, code: str, days: int = 80, force: bool = False):
        rows = [
            {"date": add_trading_days("2026-01-02", index), "close": 10 + index, "high": 10.5 + index, "low": 9.5 + index}
            for index in range(4)
        ]
        return _SimpleFrame(rows), "2026-01-02"


class _PitDb:
    def __init__(self, path: Path) -> None:
        self.path = path

    def connect(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn


def test_price_window_requires_horizon_plus_one_when_start_bar_present() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        path = Path(tmp) / "pit.sqlite3"
        with closing(sqlite3.connect(path)) as conn:
            conn.execute(
                """
                CREATE TABLE pit_prices(
                    code TEXT, trade_date TEXT, close REAL, close_raw REAL,
                    high REAL, low REAL, adj TEXT, source TEXT
                )
                """
            )
            for index, close in enumerate([10, 11, 12]):
                trade_date = add_trading_days("2026-01-02", index)
                conn.execute(
                    "INSERT INTO pit_prices VALUES(?, ?, ?, ?, ?, ?, 'hfq', 'pit')",
                    ("000001", trade_date, close, close, close + 0.5, close - 0.5),
                )
            conn.commit()

        source = StoreBackedPriceSource(pit_database=_PitDb(path), fetcher=_HistoryFetcher())
        window = source.price_window("000001", "2026-01-02", 3)
        assert window.source == "history"
        assert len(window.bars) == 4


def test_professional_candidate_gates_and_activation() -> None:
    evidence = _professional_evidence()
    payload = propose_professional_shadow_candidate(evidence)
    validation = validate_professional_candidate_payload(payload, evidence)
    assert validation.accepted is True
    assert validation.status == "shadow"
    assert validation.suggest_enable is False
    assert "shadow" in validation.reason
    assert validation.candidate_score is not None and validation.active_score is not None
    assert validation.candidate_score > validation.active_score
    activation_payload = dict(payload)
    activation_payload["shadow_validation"] = dict(evidence["long_term"])
    activation_validation = validate_professional_candidate_payload(activation_payload, evidence)
    assert activation_validation.accepted is True
    assert activation_validation.status == "suggest_enable"
    assert activation_validation.candidate_score is not None and activation_validation.active_score is not None
    assert activation_validation.candidate_score > activation_validation.active_score

    low_sample = validate_professional_candidate_payload(payload, _professional_evidence(settled=12))
    assert low_sample.accepted is False
    assert "样本不足" in low_sample.reason
    low_quality = validate_professional_candidate_payload(payload, _professional_evidence(quality=72))
    assert low_quality.accepted is False
    assert "数据质量" in low_quality.reason
    weak_record = _professional_evidence()
    weak_record["long_term"]["benchmark_hit_rate"] = 0.40
    weak_record["long_term"]["avg_excess_vs_hs300_pct"] = -0.5
    gated = validate_professional_candidate_payload(payload, weak_record)
    assert gated.accepted is False
    assert "跑赢沪深300" in gated.reason or "平均超额" in gated.reason

    forbidden = dict(payload)
    forbidden["adjustment_reasons"] = ["建议买入并保证收益"]
    try:
        validate_professional_candidate_payload(forbidden, evidence)
        raise AssertionError("禁用投资承诺词应被拒绝")
    except ValueError as exc:
        assert "禁止" in str(exc)

    invalid = dict(payload)
    invalid["dimension_weights"] = dict(payload["dimension_weights"])
    invalid["dimension_weights"]["quality"] += 0.07
    invalid["dimension_weights"]["growth"] -= 0.07
    rejected = validate_professional_candidate_payload(invalid, evidence)
    assert rejected.accepted is False
    assert "超过" in rejected.reason

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        db = V1Database(v1_db_path_for(Path(tmp)))
        db.init_schema()
        store = PerformanceStore(db)
        active = store.ensure_default_professional_formula()
        evidence["active"]["id"] = active["id"]
        candidate_id, saved_validation = store.save_validated_professional_candidate(
            activation_payload,
            evidence_package=evidence,
            provider="local",
            model="deterministic",
            cost_note="test",
        )
        assert saved_validation["status"] == "suggest_enable"
        candidate = store.list_professional_formula_candidates(status="suggest_enable", limit=1)[0]
        assert candidate["id"] == candidate_id
        new_active = store.activate_professional_formula_candidate(candidate_id)
        assert new_active["id"] == candidate_id
        assert new_active["status"] == "active"
        assert store.list_professional_memory(limit=10)
        db.close()


def run_tests() -> None:
    test_schema_and_sample_creation()
    test_settlement_metrics_quality_and_summary()
    test_missing_price_benchmark_and_industry_quality()
    test_rating_boundaries()
    test_regime_and_weight_rules()
    test_ai_prompt_constraints_and_shadow_policy()
    test_invalid_outcomes_do_not_count_for_professional_formula()
    test_professional_formula_scoring_missing_and_prompt()
    test_professional_formula_active_weights_enter_snapshot_screening()
    test_professional_formula_score_is_primary_sort_key_and_traceable()
    test_price_window_requires_horizon_plus_one_when_start_bar_present()
    test_professional_candidate_gates_and_activation()


if __name__ == "__main__":
    run_tests()
    print("test_performance_loop passed")
