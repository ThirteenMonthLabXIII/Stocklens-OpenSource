from __future__ import annotations

import json
import shutil
import sys
import tempfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from stock_tool.core.factor_store import FactorSnapshot
from stock_tool.core.mvp_engine import screen_resolved_intent
from stock_tool.core.mvp_registry import CONFIG_DIR, RegistryValidationError, load_mvp_runtime_policy, load_product_registry
from stock_tool.core.mvp_resolver import code_in_universe, controlled_vocab_guardrail_issues, merge_ai_query_spec, resolve_mvp_intent, restore_mvp_intent
from stock_tool.core.mvp_scoring import (
    MISSING_DISCOUNT,
    _interval_scores,
    normalized_field_scores,
    score_snapshot,
    technical_profile_score,
)
from stock_tool.core.professional_formula import PROFESSIONAL_DIMENSIONS
from stock_tool.core.mvp_universe import (
    UniverseSnapshotStore,
    activation_status,
    production_rollout_status,
    record_shadow_run,
)
from stock_tool.core.shadow_weights import derive_shadow_weights
from stock_tool.core.strategy_store import StrategyStore
from stock_tool.core.v1_database import V1Database


TODAY = date(2026, 6, 22)


def test_registry() -> None:
    registry = load_product_registry()
    assert set(registry.dimensions) == {"quality", "growth", "value", "safety", "trend"}
    assert set(registry.templates) == {"quality", "growth", "value", "quality_value", "trend"}
    assert all(abs(sum(item.field_weights.values()) - 1) < 1e-8 for item in registry.templates.values())
    assert load_mvp_runtime_policy().score_mode == "shadow"
    with tempfile.TemporaryDirectory() as tmp:
        copied = Path(tmp) / "config"
        shutil.copytree(CONFIG_DIR, copied)
        runtime_path = copied / "mvp_runtime.json"
        runtime_payload = json.loads(runtime_path.read_text(encoding="utf-8"))
        runtime_payload["score_mode"] = "legacy"
        runtime_path.write_text(json.dumps(runtime_payload, ensure_ascii=False), encoding="utf-8")
        try:
            load_mvp_runtime_policy(copied)
        except RegistryValidationError as exc:
            assert "shadow/production" in str(exc)
        else:
            raise AssertionError("legacy 不得作为正式评分发布模式")
        runtime_payload["score_mode"] = "shadow"
        runtime_path.write_text(json.dumps(runtime_payload, ensure_ascii=False), encoding="utf-8")
        path = copied / "controlled_vocab.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["entries"][1]["aliases"].append("价值股")
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        try:
            load_product_registry(copied)
        except RegistryValidationError as exc:
            assert "别名冲突" in str(exc)
        else:
            raise AssertionError("别名冲突必须 fail closed")


def test_resolver() -> None:
    registry = load_product_registry()
    intent = resolve_mvp_intent("沪深300价值股，PE-TTM低于20，排名前12只", registry)
    assert intent and intent.executable
    assert intent.template_id == "value" and intent.universe_id == "hs300"
    assert intent.query_spec.result_limit == 12
    assert intent.query_spec.conditions[0].field == "pe_ttm"
    assert not intent.confirmations

    hs300_fuzzy = resolve_mvp_intent("沪深300 低估放量", registry)
    assert hs300_fuzzy and hs300_fuzzy.universe_id == "hs300"
    assert hs300_fuzzy.ai_remainder

    bare = resolve_mvp_intent("价值股，PE低于20", registry)
    assert bare and bare.confirmations and not bare.executable
    fuzzy = resolve_mvp_intent("优质股，ROE较高", registry)
    assert fuzzy and fuzzy.confirmations
    refused = resolve_mvp_intent("趋势股，要求MACD金叉和近三季度", registry)
    assert refused and len(refused.unsatisfiable) >= 2 and not refused.executable
    fastest = resolve_mvp_intent("创业板增长最快前8只", registry)
    assert fastest and fastest.template_id == "growth" and fastest.query_spec.result_limit == 8
    unknown = resolve_mvp_intent("价值股并且上市超过5年", registry)
    assert unknown and unknown.ai_remainder and not unknown.executable
    ai_spec = type(intent.query_spec)(raw_text=unknown.ai_remainder)
    merged = merge_ai_query_spec(unknown, ai_spec)
    assert merged.executable and not merged.ai_remainder
    turnover_confirmations, turnover_unsupported = controlled_vocab_guardrail_issues("成长股 换手率>15%", registry)
    assert not turnover_unsupported
    assert not turnover_confirmations
    turnover_intent = resolve_mvp_intent("成长股 换手率>15%", registry)
    assert turnover_intent and not turnover_intent.unsatisfiable
    assert any(condition.field == "turnover" and condition.op == ">" and condition.value == 15 for condition in turnover_intent.query_spec.conditions)
    confirmations, unsupported = controlled_vocab_guardrail_issues("找PE低于20且行业属于银行", registry)
    assert confirmations and unsupported

    restored = restore_mvp_intent(
        intent.template_id,
        intent.scoring_config.template_version,
        intent.universe_id,
        intent.query_spec,
        intent.config_hash,
        registry,
    )
    assert restored.executable
    try:
        restore_mvp_intent("value", "0.9.0", "hs300", intent.query_spec, intent.config_hash, registry)
    except RegistryValidationError:
        pass
    else:
        raise AssertionError("版本漂移必须 fail closed")


def test_scoring() -> None:
    registry = load_product_registry()
    rows = [
        FactorSnapshot("600001", "甲", roe=10, net_margin=5, gross_margin=20, pe_ttm=5, pb=1),
        FactorSnapshot("600002", "乙", roe=20, net_margin=5, gross_margin=30, pe_ttm=10, pb=2),
        FactorSnapshot("600003", "丙", roe=20, net_margin=15, gross_margin=40, pe_ttm=-1, pb=0),
        FactorSnapshot("600004", "ST丁", roe=999, net_margin=999, gross_margin=999, pe_ttm=1, pb=1),
    ]
    scores = normalized_field_scores(rows, registry)
    assert scores["roe"]["600002"] == scores["roe"]["600003"] == 75
    assert "600004" not in scores["roe"], "ST股票不得污染归一化参考集"
    assert "600003" not in scores["pe_ttm"] and "600003" not in scores["pb"]
    interval = _interval_scores({str(index): float(index + 1) for index in range(100)})
    assert interval["0"] == 0 and interval["99"] == 0
    assert interval["10"] == 100 and interval["40"] < 100

    template = registry.template("quality")
    missing = FactorSnapshot("600010", "缺一项", roe=20, net_margin=10)
    reference = [*rows, missing]
    normalized = normalized_field_scores(reference, registry)
    breakdown = score_snapshot(missing, template, normalized, universe_name="全A")
    assert breakdown and breakdown.field_scores["gross_margin"] == 50
    assert abs(breakdown.completeness_discount - MISSING_DISCOUNT) < 1e-10
    assert abs(sum(breakdown.weighted_contributions.values()) - breakdown.base_score) < 1e-6
    assert score_snapshot(FactorSnapshot("600011", "全缺"), template, normalized, universe_name="全A") is None
    assert technical_profile_score("S") == 100
    assert technical_profile_score("S（高风险）") == 65
    assert [technical_profile_score(label) for label in "ABCD"] == [80, 60, 40, 0]
    assert technical_profile_score("") is None


def test_universe_engine_and_gate() -> None:
    assert code_in_universe("300001", "chinext") and code_in_universe("301999", "chinext")
    assert not code_in_universe("000001", "chinext")
    assert code_in_universe("830001", "all_a"), "全A应包含北交所普通A股"
    with tempfile.TemporaryDirectory() as tmp:
        db = V1Database(Path(tmp) / "mvp.sqlite3")
        store = UniverseSnapshotStore(db)
        members = [(f"600{index:03d}", f"样本{index}") for index in range(600)]
        universe = store.save("all_a", members, asof_date=TODAY.isoformat(), source="test")
        assert universe.membership_hash == store.latest("all_a").membership_hash
        rows = [
            FactorSnapshot(
                code=code,
                name=name,
                roe=float(index),
                net_margin=float(index),
                gross_margin=float(index),
                industry_strength=float(index) / 6.0,
                updated_at="2026-06-22T09:00:00",
                financial_updated_at="2026-06-22T09:00:00",
            )
            for index, (code, name) in enumerate(members)
        ]
        registry = load_product_registry()
        intent = resolve_mvp_intent("全A优质股排名前50只", registry)
        assert intent
        report = screen_resolved_intent(rows, intent, registry=registry, score_mode="production", legacy=True)
        assert report.scanned == 600 and len(report.results) == 50
        assert any(item.quote.code == "600599" for item in report.results), "模板路径不得受300只市值扫描上限影响"
        assert all(0 <= item.local_score <= 100 for item in report.results)
        shadow = screen_resolved_intent(rows, intent, registry=registry, score_mode="shadow", legacy=True)
        assert all(
            item.local_score == item.score_breakdown["professional_formula"]["score"]
            and item.legacy_score is not None
            and item.template_score is not None
            for item in shadow.results
        )

        no_job = activation_status(universe, rows, "quality", registry=registry, today=TODAY, database=db)
        assert not no_job.active and any("任务记录" in reason for reason in no_job.reasons)
        with db.connect() as conn:
            conn.execute(
                "INSERT INTO factor_update_jobs(universe_id,status,total,success,failed,started_at,updated_at,completed_at,message) "
                "VALUES('all_a','completed',600,600,0,'2026-06-22','2026-06-22','2026-06-22','成功600，失败0，失败率0.00%')"
            )
        gate = activation_status(universe, rows, "quality", registry=registry, today=TODAY, database=db)
        assert gate.active, gate.reasons
        assert not production_rollout_status(db, registry.config_hash).ready
        for day in range(5):
            for universe_id in ("all_a", "hs300", "chinext"):
                for template_id, template in registry.templates.items():
                    record_shadow_run(
                        db,
                        run_date=f"2026-06-{15 + day:02d}",
                        template_id=template_id,
                        universe_id=universe_id,
                        template_version=template.version,
                        config_hash=registry.config_hash,
                        universe_hash="u",
                        result_hash="r",
                        scores=[50.0],
                        status="passed",
                    )
        assert production_rollout_status(db, registry.config_hash).ready
        stale = store.save("chinext", [("300001", "旧样本")], asof_date="2026-06-17", source="test")
        assert not activation_status(stale, rows, "quality", registry=registry, today=TODAY).active
        db.close()


def test_mvp_professional_formula_active_weights_define_primary_score() -> None:
    registry = load_product_registry()
    intent = resolve_mvp_intent("全A优质股排名前1只", registry)
    assert intent
    rows = [
        FactorSnapshot(
            "600001",
            "权重样例",
            price=10,
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

    quality_report = screen_resolved_intent(
        rows,
        intent,
        registry=registry,
        score_mode="shadow",
        legacy=True,
        professional_formula=quality_formula,
    )
    growth_report = screen_resolved_intent(
        rows,
        intent,
        registry=registry,
        score_mode="shadow",
        legacy=True,
        professional_formula=growth_formula,
    )

    quality_item = quality_report.results[0]
    growth_item = growth_report.results[0]
    quality_breakdown = quality_item.score_breakdown["professional_formula"]
    growth_breakdown = growth_item.score_breakdown["professional_formula"]
    assert quality_item.local_score == quality_breakdown["score"]
    assert growth_item.local_score == growth_breakdown["score"]
    assert quality_item.legacy_score == growth_item.legacy_score
    assert quality_item.local_score > growth_item.local_score
    assert quality_item.template_score == growth_item.template_score
    assert quality_breakdown["score"] > growth_breakdown["score"]
    assert quality_breakdown["dimension_weights"]["quality"] == 1.0
    assert growth_breakdown["dimension_weights"]["growth"] == 1.0


def test_mvp_formula_entry_requires_explicit_legacy_flag() -> None:
    registry = load_product_registry()
    intent = resolve_mvp_intent("全A优质股排名前1只", registry)
    assert intent
    try:
        screen_resolved_intent([], intent, registry=registry, score_mode="production")
    except ValueError as exc:
        assert "legacy" in str(exc)
    else:
        raise AssertionError("旧 MVP 公式入口必须显式声明 legacy=True")


def test_strategy_and_shadow_weight_governance() -> None:
    registry = load_product_registry()
    intent = resolve_mvp_intent("沪深300价值股", registry)
    assert intent
    with tempfile.TemporaryDirectory() as tmp:
        database = V1Database(Path(tmp) / "strategy.sqlite3")
        strategy_store = StrategyStore(database)
        saved = strategy_store.save_preset(
            "闭集策略",
            "沪深300价值股",
            intent.query_spec,
            template_id=intent.template_id,
            template_version=intent.scoring_config.template_version,
            universe_id=intent.universe_id,
            config_hash=intent.config_hash,
        )
        assert (saved.template_id, saved.universe_id, saved.config_hash) == ("value", "hs300", registry.config_hash)
        version = strategy_store.list_preset_versions(saved.id)[0]
        assert version.template_version == "1.0.0" and version.config_hash == registry.config_hash
        database.close()

    result = derive_shadow_weights({"a": 0.8, "b": 0.2}, {"a": 0.0, "b": 1.0})
    assert abs(sum(result.shadow_weights.values()) - 1) < 1e-10
    assert 0.6 <= result.shadow_weights["a"] <= 1.0
    assert 0.15 <= result.shadow_weights["b"] <= 0.25
    fallback = derive_shadow_weights({"a": 0.5, "b": 0.5}, {})
    assert not fallback.used_evidence and fallback.shadow_weights == {"a": 0.5, "b": 0.5}


def run_tests() -> None:
    test_registry()
    test_resolver()
    test_scoring()
    test_universe_engine_and_gate()
    test_mvp_professional_formula_active_weights_define_primary_score()
    test_mvp_formula_entry_requires_explicit_legacy_flag()
    test_strategy_and_shadow_weight_governance()
    print("[OK] MVP闭集、解析、评分、股票池、门控、留档与权重治理测试通过")


if __name__ == "__main__":
    run_tests()
