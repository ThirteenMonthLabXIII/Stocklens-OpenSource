from __future__ import annotations

from dataclasses import asdict

from stock_tool.core.factor_store import FactorSnapshot
from stock_tool.core.mvp_registry import ProductRegistry, load_mvp_runtime_policy, load_product_registry
from stock_tool.core.mvp_resolver import ResolvedIntent, UNIVERSE_NAMES, filter_universe
from stock_tool.core.mvp_scoring import normalized_field_scores, score_snapshot
from stock_tool.core.query_schema import query_spec_to_screen_filter
from stock_tool.core.screener_engine import ScreenCandidate, ScreenReport, build_scored_candidate, passes_filter


def _passes_trend(snapshot: FactorSnapshot, threshold: float | None, strict: bool) -> bool:
    if threshold is None:
        return True
    if snapshot.trend_20d is None:
        return False
    return snapshot.trend_20d > threshold if strict else snapshot.trend_20d >= threshold


def screen_resolved_intent(
    snapshots: list[FactorSnapshot],
    intent: ResolvedIntent,
    *,
    hs300_codes: set[str] | None = None,
    registry: ProductRegistry | None = None,
    score_mode: str | None = None,
    professional_formula: dict[str, object] | None = None,
    legacy: bool = False,
) -> ScreenReport:
    """Execute the historical formula template path explicitly as legacy."""

    if not legacy:
        raise ValueError("MVP 公式筛选仅支持 legacy 兼容入口；新生产流程必须使用 CandidateLens")
    if not intent.executable:
        details = "；".join([*intent.confirmations, *intent.unsatisfiable])
        raise ValueError(f"该模板意图不可执行：{details}")
    registry = registry or load_product_registry()
    score_mode = score_mode or load_mvp_runtime_policy().score_mode
    if score_mode not in {"shadow", "production"}:
        raise ValueError(f"未知评分发布模式：{score_mode}")
    template = registry.template(intent.template_id)
    universe_items = filter_universe(snapshots, intent.universe_id, hs300_codes)
    normalized = normalized_field_scores(universe_items, registry)
    filters = query_spec_to_screen_filter(intent.query_spec)
    dimension_weights = professional_formula.get("dimension_weights") if isinstance(professional_formula, dict) else None
    field_weights = professional_formula.get("field_weights") if isinstance(professional_formula, dict) else None
    if not isinstance(dimension_weights, dict):
        dimension_weights = None
    if not isinstance(field_weights, dict):
        field_weights = None
    candidates: list[ScreenCandidate] = []
    skipped = 0
    for snapshot in universe_items:
        quote = snapshot.to_quote()
        metrics = snapshot.to_metrics()
        technical = snapshot.to_technical()
        if not passes_filter(quote, metrics, filters) or not _passes_trend(snapshot, filters.min_trend_20d, filters.strict_min_trend_20d):
            skipped += 1
            continue
        breakdown = score_snapshot(snapshot, template, normalized, universe_name=UNIVERSE_NAMES[intent.universe_id])
        if breakdown is None:
            skipped += 1
            continue
        score_breakdown = asdict(breakdown)
        reasons = [breakdown.explanation]
        candidates.append(
            build_scored_candidate(
                quote=quote,
                metrics=metrics,
                technical=technical,
                extra_fields={"trend_20d": snapshot.trend_20d, "main_5d_yi": None},
                reasons=reasons,
                sort_values={"trend_20d": snapshot.trend_20d, "template_score": breakdown.score},
                template_score=breakdown.score,
                experimental_score=breakdown.score,
                score_breakdown=score_breakdown,
                dimension_weights=dimension_weights,
                field_weights=field_weights,
            )
        )
    candidates.sort(key=lambda item: (-float(item.local_score or 0), item.quote.code))
    return ScreenReport(
        results=candidates[: intent.query_spec.result_limit],
        scanned=len(universe_items),
        skipped=skipped,
        failed=0,
        errors=[],
    )
