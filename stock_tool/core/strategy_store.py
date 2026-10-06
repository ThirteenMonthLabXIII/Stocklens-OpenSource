from __future__ import annotations

import json
import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from stock_tool.core.query_schema import (
    FIELD_LABELS,
    FIELD_UNITS,
    QuerySpec,
    QueryValidationError,
    STOCK_LIST_KEYS,
    query_spec_from_dict,
    query_spec_summary,
    query_spec_to_dict,
    query_spec_to_screen_filter,
)
from stock_tool.core.screener_engine import ScreenReport
from stock_tool.core.v1_database import V1Database


class StrategyStoreError(ValueError):
    pass


@dataclass(frozen=True)
class StrategyPreset:
    id: int
    name: str
    user_text: str
    query_spec_json: str
    summary: str
    parse_source: str
    ai_repair_retry: bool
    template_id: str
    template_version: str
    universe_id: str
    config_hash: str
    created_at: str
    updated_at: str
    last_run_at: str
    run_count: int


@dataclass(frozen=True)
class StrategyPresetVersion:
    id: int
    preset_id: int
    preset_name: str
    version_no: int
    user_text: str
    query_spec_json: str
    summary: str
    parse_source: str
    ai_repair_retry: bool
    template_id: str
    template_version: str
    universe_id: str
    config_hash: str
    change_note: str
    created_at: str


@dataclass(frozen=True)
class QueryRunRecord:
    id: int
    preset_id: int | None
    preset_name: str
    source_label: str
    user_text: str
    summary: str
    data_source: str
    scanned: int
    matched: int
    skipped: int
    failed: int
    note: str
    template_id: str
    template_version: str
    universe_id: str
    universe_hash: str
    config_hash: str
    scoring_context_json: str
    created_at: str

    @property
    def scoring_context(self) -> dict:
        try:
            payload = json.loads(self.scoring_context_json or "{}")
        except json.JSONDecodeError:
            return {}
        return payload if isinstance(payload, dict) else {}


@dataclass(frozen=True)
class QueryRunResult:
    id: int
    run_id: int
    rank_order: int
    code: str
    name: str
    local_score: float | None
    reason: str
    risk_flags_json: str
    metrics_json: str
    score_breakdown_json: str

    @property
    def risk_flags(self) -> list[str]:
        try:
            payload = json.loads(self.risk_flags_json or "[]")
        except json.JSONDecodeError:
            return []
        return [str(item) for item in payload] if isinstance(payload, list) else []

    @property
    def metrics(self) -> dict:
        try:
            payload = json.loads(self.metrics_json or "{}")
        except json.JSONDecodeError:
            return {}
        return payload if isinstance(payload, dict) else {}

    @property
    def score_breakdown(self) -> dict:
        try:
            payload = json.loads(self.score_breakdown_json or "{}")
        except json.JSONDecodeError:
            return {}
        return payload if isinstance(payload, dict) else {}


@dataclass(frozen=True)
class QueryRunDetail:
    run: QueryRunRecord
    results: list[QueryRunResult]


@dataclass(frozen=True)
class RunComparisonItem:
    status: str
    code: str
    name: str
    current_rank: int | None
    previous_rank: int | None
    rank_delta: int | None
    current_score: float | None
    previous_score: float | None
    score_delta: float | None
    metric_changes: dict[str, dict[str, object]]


@dataclass(frozen=True)
class QuerySpecDiffItem:
    category: str
    field_label: str
    old_value: str
    new_value: str
    message: str


@dataclass(frozen=True)
class RunComparisonSummary:
    added_count: int
    removed_count: int
    rank_changed_count: int
    metric_changed_count: int


@dataclass(frozen=True)
class RunComparison:
    current_run: QueryRunRecord
    previous_run: QueryRunRecord
    added: list[RunComparisonItem]
    removed: list[RunComparisonItem]
    kept: list[RunComparisonItem]

    @property
    def items(self) -> list[RunComparisonItem]:
        return [*self.added, *self.removed, *self.kept]


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _standard_spec_json(spec: QuerySpec) -> str:
    return json.dumps(query_spec_to_dict(spec), ensure_ascii=False, sort_keys=True)


def _stable_json_hash(payload: Any) -> str:
    raw = json.dumps(payload or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _professional_breakdown_from_report(report: ScreenReport) -> dict[str, Any]:
    for candidate in report.results:
        breakdown = candidate.score_breakdown or {}
        professional = breakdown.get("professional_formula") if isinstance(breakdown, dict) else None
        if isinstance(professional, dict):
            return professional
    return {}


def _selection_contract_from_breakdown(score_breakdown: Any) -> dict[str, Any]:
    breakdown = score_breakdown if isinstance(score_breakdown, dict) else {}
    fields = (
        "selection_profile",
        "selection_grade",
        "selection_state",
        "veto_reasons",
        "evidence_gaps",
        "eligibility_status",
        "required_fields",
        "missing_required_fields",
        "data_quality_status",
    )
    return {field: breakdown[field] for field in fields if field in breakdown}


def _build_scoring_context(
    *,
    report: ScreenReport,
    data_source: str,
    professional_formula: dict[str, Any] | None = None,
    scoring_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    base = dict(scoring_context or {})
    if base.get("candidate_lens_mode") or base.get("ranking_formula_id") == "candidate_lens.v1":
        if professional_formula is not None:
            raise ValueError("CandidateLens 新生产流程禁止传入 professional_formula")
        # New production runs carry a CandidateLens identity only.  Keep the
        # old formula columns readable for historical rows, but do not create
        # active/shadow formula state or a score field for new candidates.
        base.update(
            {
                "candidate_lens_mode": True,
                "ranking_formula_id": "candidate_lens.v1",
                "ranking_formula_version": "candidate_lens.v1",
                "selection_policy_version": "v1",
                "scoring_mode": "candidate_lens",
                "score_field": None,
                "legacy_score_field": None,
            }
        )
        base.pop("baseline_formula", None)
        base.pop("active_formula", None)
        base.pop("shadow_formula", None)
        base.pop("formula_hash", None)
        base.pop("shadow_formula_hash", None)
        return base
    breakdown = _professional_breakdown_from_report(report)
    formula = professional_formula if isinstance(professional_formula, dict) else {}
    dimensions = formula.get("dimension_weights") if isinstance(formula.get("dimension_weights"), dict) else breakdown.get("dimension_weights")
    fields = formula.get("field_weights") if isinstance(formula.get("field_weights"), dict) else breakdown.get("field_weights")
    active_formula = {
        "id": formula.get("id"),
        "name": formula.get("name", ""),
        "version": formula.get("version", ""),
        "status": formula.get("status", ""),
        "engine_version": breakdown.get("version", ""),
        "dimension_weights": dimensions if isinstance(dimensions, dict) else {},
        "field_weights": fields if isinstance(fields, dict) else {},
    }
    base.setdefault("scoring_mode", "fixed_baseline_formula_with_shadow")
    base.setdefault("data_source", data_source)
    base.setdefault("generated_at", _now())
    base.setdefault("score_field", "local_score")
    base.setdefault("legacy_score_field", "legacy_score")
    base.setdefault(
        "baseline_formula",
        {"id": "professional_formula.baseline", "version": "v1", "status": "active"},
    )
    base["active_formula"] = {**active_formula, **dict(base.get("active_formula") or {})}
    base["shadow_formula"] = dict(base["active_formula"])
    base.setdefault("shadow_formula_hash", _stable_json_hash(base["shadow_formula"]))
    base.setdefault("formula_hash", _stable_json_hash(base["active_formula"]))
    return base


def _preset_from_row(row) -> StrategyPreset:
    return StrategyPreset(
        id=int(row["id"]),
        name=row["name"],
        user_text=row["user_text"],
        query_spec_json=row["query_spec_json"],
        summary=row["summary"],
        parse_source=row["parse_source"],
        ai_repair_retry=bool(row["ai_repair_retry"]),
        template_id=row["template_id"],
        template_version=row["template_version"],
        universe_id=row["universe_id"],
        config_hash=row["config_hash"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        last_run_at=row["last_run_at"],
        run_count=int(row["run_count"]),
    )


def _preset_version_from_row(row) -> StrategyPresetVersion:
    return StrategyPresetVersion(
        id=int(row["id"]),
        preset_id=int(row["preset_id"]),
        preset_name=row["preset_name"],
        version_no=int(row["version_no"]),
        user_text=row["user_text"],
        query_spec_json=row["query_spec_json"],
        summary=row["summary"],
        parse_source=row["parse_source"],
        ai_repair_retry=bool(row["ai_repair_retry"]),
        template_id=row["template_id"],
        template_version=row["template_version"],
        universe_id=row["universe_id"],
        config_hash=row["config_hash"],
        change_note=row["change_note"],
        created_at=row["created_at"],
    )


def _run_from_row(row) -> QueryRunRecord:
    return QueryRunRecord(
        id=int(row["id"]),
        preset_id=row["preset_id"],
        preset_name=row["preset_name"],
        source_label=row["source_label"],
        user_text=row["user_text"],
        summary=row["summary"],
        data_source=row["data_source"],
        scanned=int(row["scanned"]),
        matched=int(row["matched"]),
        skipped=int(row["skipped"]),
        failed=int(row["failed"]),
        note=row["note"],
        template_id=row["template_id"],
        template_version=row["template_version"],
        universe_id=row["universe_id"],
        universe_hash=row["universe_hash"],
        config_hash=row["config_hash"],
        scoring_context_json=row["scoring_context_json"],
        created_at=row["created_at"],
    )


def _run_result_from_row(row) -> QueryRunResult:
    score = row["local_score"]
    return QueryRunResult(
        id=int(row["id"]),
        run_id=int(row["run_id"]),
        rank_order=int(row["rank_order"]),
        code=row["code"],
        name=row["name"],
        local_score=float(score) if score is not None else None,
        reason=row["reason"],
        risk_flags_json=row["risk_flags_json"],
        metrics_json=row["metrics_json"],
        score_breakdown_json=row["score_breakdown_json"],
    )


def _condition_key(condition: dict) -> tuple[str, str]:
    return str(condition.get("field") or ""), str(condition.get("op") or "")


def _format_scalar_value(value: object, unit: str = "") -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, (int, float)):
        return f"{value:g}{unit}"
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return "-"
        try:
            return f"{float(text):g}{unit}"
        except ValueError:
            return text
    return str(value)


def _format_condition_value(condition: dict) -> str:
    field_name = str(condition.get("field") or "")
    unit = FIELD_UNITS.get(field_name, "")
    if condition.get("op") == "between":
        left = _format_scalar_value(condition.get("value"), unit)
        right = _format_scalar_value(condition.get("value_to"), unit)
        return f"{left} 到 {right}"
    return _format_scalar_value(condition.get("value"), unit)


def _format_sort_rules(sort_rules: object) -> str:
    if not isinstance(sort_rules, list) or not sort_rules:
        return "无"
    parts: list[str] = []
    for item in sort_rules:
        if not isinstance(item, dict):
            continue
        field_name = str(item.get("field") or "")
        direction = str(item.get("direction") or "desc").lower()
        label = FIELD_LABELS.get(field_name, "本地分" if field_name == "local_score" else field_name)
        direction_label = "升序" if direction == "asc" else "降序"
        parts.append(f"{label}{direction_label}")
    return "、".join(parts) if parts else "无"


def _format_list_value(value: object) -> str:
    if not isinstance(value, list) or not value:
        return "无"
    return "、".join(str(item) for item in value)


def _format_bool_value(value: object) -> str:
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"true", "1", "yes", "y", "是"}:
            return "是"
        if text in {"false", "0", "no", "n", "否"}:
            return "否"
    return "是" if bool(value) else "否"


def query_spec_diff_items(old_json: str, new_json: str) -> list[QuerySpecDiffItem]:
    try:
        old_payload = json.loads(old_json or "{}")
        new_payload = json.loads(new_json or "{}")
    except json.JSONDecodeError:
        return [
            QuerySpecDiffItem(
                category="JSON",
                field_label="策略 JSON",
                old_value="",
                new_value="",
                message="策略 JSON 结构异常，暂不能生成中文差异",
            )
        ]
    if not isinstance(old_payload, dict) or not isinstance(new_payload, dict):
        return [
            QuerySpecDiffItem(
                category="JSON",
                field_label="策略 JSON",
                old_value="",
                new_value="",
                message="策略 JSON 结构异常，暂不能生成中文差异",
            )
        ]
    old_conditions = {_condition_key(item): item for item in old_payload.get("conditions", []) if isinstance(item, dict)}
    new_conditions = {_condition_key(item): item for item in new_payload.get("conditions", []) if isinstance(item, dict)}
    changes: list[QuerySpecDiffItem] = []
    for key in sorted(new_conditions):
        field_name, op = key
        label = FIELD_LABELS.get(field_name, field_name)
        new_value = _format_condition_value(new_conditions[key])
        if key not in old_conditions:
            message = f"新增条件：{label} {op} {new_value}"
            changes.append(QuerySpecDiffItem("条件变化", f"{label} {op}", "", new_value, message))
            continue
        old_value = _format_condition_value(old_conditions[key])
        if old_value != new_value:
            message = f"{label} {op} 从 {old_value} 改为 {new_value}"
            changes.append(QuerySpecDiffItem("条件变化", f"{label} {op}", old_value, new_value, message))
    for key in sorted(old_conditions):
        if key not in new_conditions:
            field_name, op = key
            label = FIELD_LABELS.get(field_name, field_name)
            old_value = _format_condition_value(old_conditions[key])
            message = f"删除条件：{label} {op} {old_value}"
            changes.append(QuerySpecDiffItem("条件变化", f"{label} {op}", old_value, "", message))
    for key, label, category in [
        ("scan_limit", "扫描上限", "扫描参数变化"),
        ("result_limit", "结果上限", "扫描参数变化"),
        ("allow_missing_pe", "允许 PE 缺失", "PE缺失设置变化"),
        ("with_technical", "计算技术画像", "技术画像设置变化"),
    ]:
        old_value = _format_bool_value(old_payload.get(key)) if key in {"allow_missing_pe", "with_technical"} else _format_scalar_value(old_payload.get(key))
        new_value = _format_bool_value(new_payload.get(key)) if key in {"allow_missing_pe", "with_technical"} else _format_scalar_value(new_payload.get(key))
        if old_value != new_value:
            changes.append(QuerySpecDiffItem(category, label, old_value, new_value, f"{label} 从 {old_value} 改为 {new_value}"))
    old_sort = old_payload.get("sort") or []
    new_sort = new_payload.get("sort") or []
    if old_sort != new_sort:
        old_value = _format_sort_rules(old_sort)
        new_value = _format_sort_rules(new_sort)
        changes.append(QuerySpecDiffItem("排序变化", "排序规则", old_value, new_value, f"排序规则从 {old_value} 改为 {new_value}"))
    old_exclude = old_payload.get("exclude") or []
    new_exclude = new_payload.get("exclude") or []
    if old_exclude != new_exclude:
        old_value = _format_list_value(old_exclude)
        new_value = _format_list_value(new_exclude)
        changes.append(QuerySpecDiffItem("排除规则变化", "排除规则", old_value, new_value, f"排除规则从 {old_value} 改为 {new_value}"))
    return changes


def query_spec_change_summary(old_json: str, new_json: str) -> str:
    changes = query_spec_diff_items(old_json, new_json)
    if len(changes) == 1 and changes[0].category == "JSON":
        return "策略 JSON 结构变化"
    return "；".join(item.message for item in changes) if changes else "策略条件未变化，仅更新原文、来源或说明"


def rank_direction_label(item: RunComparisonItem) -> str:
    if item.current_rank is None or item.previous_rank is None:
        return ""
    if item.current_rank < item.previous_rank:
        return "上升"
    if item.current_rank > item.previous_rank:
        return "下降"
    return "不变"


def comparison_change_type_label(item: RunComparisonItem) -> str:
    if item.status == "added":
        return "新增"
    if item.status == "removed":
        return "消失"
    changed: list[str] = []
    if item.rank_delta not in (None, 0):
        changed.append("排名变化")
    if item.metric_changes:
        changed.append("指标变化")
    return "、".join(changed) if changed else "保留"


def run_comparison_summary(comparison: RunComparison) -> RunComparisonSummary:
    return RunComparisonSummary(
        added_count=len(comparison.added),
        removed_count=len(comparison.removed),
        rank_changed_count=sum(1 for item in comparison.kept if item.rank_delta not in (None, 0)),
        metric_changed_count=sum(1 for item in comparison.kept if item.metric_changes),
    )


def validate_executable_query_spec(spec: QuerySpec) -> None:
    query_spec_to_screen_filter(spec)


def strategy_export_payload(
    name: str,
    user_text: str,
    spec: QuerySpec,
    summary: str | None = None,
    parse_source: str = "",
) -> dict:
    validate_executable_query_spec(spec)
    return {
        "format": "stock_tool_strategy",
        "version": 1,
        "strategy_name": name.strip() or "未命名策略",
        "user_text": user_text.strip() or spec.raw_text,
        "summary": summary or query_spec_summary(spec),
        "query_spec": query_spec_to_dict(spec),
        "parse_source": parse_source.strip(),
        "exported_at": _now(),
        "disclaimer": "本策略仅为筛选条件，不构成投资建议；导入后仍需本地校验和用户手动保存。",
    }


def query_spec_from_export_payload(payload: dict) -> tuple[str, str, QuerySpec, str, str]:
    if not isinstance(payload, dict):
        raise QueryValidationError("导入文件必须是JSON对象")
    if any(key in payload and payload.get(key) is not None for key in STOCK_LIST_KEYS):
        raise QueryValidationError("导入文件包含股票列表，当前链路只接受筛选条件，拒绝执行")
    spec_payload = payload.get("query_spec", payload)
    if not isinstance(spec_payload, dict):
        raise QueryValidationError("导入文件缺少 query_spec 对象")
    spec = query_spec_from_dict(spec_payload)
    validate_executable_query_spec(spec)
    name = str(payload.get("strategy_name") or payload.get("name") or "导入策略").strip()[:80] or "导入策略"
    user_text = str(payload.get("user_text") or spec.raw_text or "").strip()
    summary = str(payload.get("summary") or query_spec_summary(spec)).strip()
    parse_source = str(payload.get("parse_source") or "JSON导入").strip()
    return name, user_text, spec, summary, parse_source


CSV_RESULT_COLUMNS = [
    "排名",
    "代码",
    "名称",
    "本地分",
    "观察等级",
    "候选类型",
    "观察状态",
    "数据质量",
    "压档原因",
    "证据缺口",
    "理由",
    "风险标签",
    "行业",
    "箱体评分",
    "箱体区间",
    "箱体位置",
    "行业强度",
    "价格",
    "PE",
    "PB",
    "市值",
    "ROE",
    "净利率",
    "负债率",
    "营收增长",
    "利润增长",
    "技术状态",
]

RUN_COMPARISON_CSV_COLUMNS = [
    "状态",
    "变化类型",
    "代码",
    "名称",
    "当前排名",
    "上次排名",
    "排名变化",
    "排名方向",
    "当前本地分",
    "上次本地分",
    "本地分变化",
    "指标变化",
]


METRIC_CHANGE_LABELS = {
    **FIELD_LABELS,
    "price": "价格",
    "industry": "行业",
    "industry_rank": "行业排名",
    "industry_sample_count": "行业样本数",
    "industry_updated_at": "行业更新时间",
    "box_upper": "箱体上沿",
    "box_lower": "箱体下沿",
    "box_position_label": "箱体位置",
    "box_summary": "箱体摘要",
    "technical_status": "技术状态",
    "technical_label": "技术标签",
    "selection_profile": "候选类型",
    "selection_grade": "观察等级",
    "selection_state": "观察状态",
    "veto_reasons": "压档原因",
    "evidence_gaps": "证据缺口",
}


def run_results_csv_rows(results: list[QueryRunResult]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for result in results:
        metrics = result.metrics
        breakdown = result.score_breakdown if isinstance(result.score_breakdown, dict) else {}
        professional = breakdown.get("professional_formula") if isinstance(breakdown.get("professional_formula"), dict) else {}
        veto_reasons = metrics.get("veto_reasons", breakdown.get("veto_reasons"))
        evidence_gaps = metrics.get("evidence_gaps", breakdown.get("evidence_gaps"))
        veto_text = "、".join(str(item) for item in veto_reasons[:3]) if isinstance(veto_reasons, list) else ""
        gap_labels = []
        if isinstance(evidence_gaps, list):
            for item in evidence_gaps:
                if isinstance(item, dict):
                    text = str(item.get("label") or item.get("field") or "").strip()
                else:
                    text = str(item).strip()
                if text and text not in gap_labels:
                    gap_labels.append(text)
        technical = metrics.get("technical_label") or metrics.get("technical_status") or ""
        box_lower = metrics.get("box_lower")
        box_upper = metrics.get("box_upper")
        box_range = metrics.get("box_range_pct")
        box_range_text = "" if box_range in (None, "") else f"；振幅{box_range}%"
        box_range_text = "" if box_lower in (None, "") or box_upper in (None, "") else f"{box_lower}-{box_upper}{box_range_text}"
        row = {
                "排名": result.rank_order,
                "代码": result.code,
                "名称": result.name,
                "本地分": "" if result.local_score is None else result.local_score,
                "观察等级": metrics.get("selection_grade", breakdown.get("selection_grade", "")),
                "候选类型": metrics.get("selection_profile", breakdown.get("selection_profile", "")),
                "观察状态": metrics.get("selection_state", breakdown.get("selection_state", "")),
                "数据质量": metrics.get("professional_data_quality_score", professional.get("data_quality_score", "")),
                "压档原因": veto_text,
                "证据缺口": "、".join(gap_labels[:5]),
                "理由": result.reason,
                "风险标签": "、".join(result.risk_flags),
                "行业": metrics.get("industry", ""),
                "箱体评分": metrics.get("box_score", ""),
                "箱体区间": box_range_text,
                "箱体位置": metrics.get("box_position_label", ""),
                "行业强度": metrics.get("industry_strength", ""),
                "价格": metrics.get("price", ""),
                "PE": metrics.get("pe", ""),
                "PB": metrics.get("pb", ""),
                "市值": metrics.get("market_cap_yi", ""),
                "ROE": metrics.get("roe", ""),
                "净利率": metrics.get("net_margin", ""),
                "负债率": metrics.get("debt_ratio", ""),
                "营收增长": metrics.get("revenue_growth", ""),
                "利润增长": metrics.get("profit_growth", ""),
                "技术状态": technical,
            }
        if breakdown.get("candidate_lens_mode"):
            row.pop("本地分", None)
            row.pop("数据质量", None)
        rows.append(row)
    return rows


def format_metric_changes(metric_changes: dict[str, dict[str, object]]) -> str:
    parts: list[str] = []
    for field_name, change in metric_changes.items():
        label = METRIC_CHANGE_LABELS.get(field_name, field_name)
        previous = change.get("previous")
        current = change.get("current")
        parts.append(f"{label}: {previous} -> {current}")
    return "；".join(parts)


def run_comparison_csv_rows(comparison: RunComparison) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    status_label = {"added": "新增", "removed": "消失", "kept": "保留"}
    for item in comparison.items:
        row = {
                "状态": status_label.get(item.status, item.status),
                "变化类型": comparison_change_type_label(item),
                "代码": item.code,
                "名称": item.name,
                "当前排名": "" if item.current_rank is None else item.current_rank,
                "上次排名": "" if item.previous_rank is None else item.previous_rank,
                "排名变化": "" if item.rank_delta is None else item.rank_delta,
                "排名方向": rank_direction_label(item),
                "当前本地分": "" if item.current_score is None else item.current_score,
                "上次本地分": "" if item.previous_score is None else item.previous_score,
                "本地分变化": "" if item.score_delta is None else item.score_delta,
                "指标变化": format_metric_changes(item.metric_changes),
            }
        if item.current_score is None and item.previous_score is None:
            for key in ("当前本地分", "上次本地分", "本地分变化"):
                row.pop(key, None)
        rows.append(row)
    return rows


def _round_delta(current: float | None, previous: float | None) -> float | None:
    if current is None or previous is None:
        return None
    return round(current - previous, 4)


def _metric_changes(current: QueryRunResult, previous: QueryRunResult) -> dict[str, dict[str, object]]:
    fields = [
        "price",
        "pe",
        "pb",
        "market_cap_yi",
        "roe",
        "net_margin",
        "debt_ratio",
        "revenue_growth",
        "profit_growth",
        "technical_status",
        "technical_label",
    ]
    current_metrics = current.metrics
    previous_metrics = previous.metrics
    changes: dict[str, dict[str, object]] = {}
    for field_name in fields:
        current_value = current_metrics.get(field_name)
        previous_value = previous_metrics.get(field_name)
        if current_value != previous_value:
            changes[field_name] = {"current": current_value, "previous": previous_value}
    return changes


def _normalize_start_time(value: str) -> str:
    text = value.strip()
    if len(text) == 10 and text[4] == "-" and text[7] == "-":
        return text + "T00:00:00"
    return text


def _normalize_end_time(value: str) -> str:
    text = value.strip()
    if len(text) == 10 and text[4] == "-" and text[7] == "-":
        return text + "T23:59:59"
    return text


class StrategyStore:
    def __init__(self, database: V1Database) -> None:
        self.database = database

    def save_preset(
        self,
        name: str,
        user_text: str,
        spec: QuerySpec,
        summary: str | None = None,
        parse_source: str = "",
        ai_repair_retry: bool = False,
        change_note: str = "",
        template_id: str = "",
        template_version: str = "",
        universe_id: str = "",
        config_hash: str = "",
    ) -> StrategyPreset:
        name = name.strip()
        if not name:
            raise StrategyStoreError("策略名不能为空")
        if len(name) > 80:
            raise StrategyStoreError("策略名不能超过80个字符")
        validate_executable_query_spec(spec)
        spec_json = _standard_spec_json(spec)
        summary_text = summary or query_spec_summary(spec)
        now = _now()
        with self.database.connect() as conn:
            existing = conn.execute("SELECT * FROM strategy_presets WHERE name = ?", (name,)).fetchone()
            if existing is None:
                cursor = conn.execute(
                    """
                    INSERT INTO strategy_presets(
                        name, user_text, query_spec_json, summary, parse_source,
                        ai_repair_retry, template_id, template_version, universe_id,
                        config_hash, created_at, updated_at, last_run_at, run_count
                    )
                    VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', 0)
                    """,
                    (
                        name,
                        user_text.strip(),
                        spec_json,
                        summary_text,
                        parse_source.strip(),
                        1 if ai_repair_retry else 0,
                        template_id.strip(),
                        template_version.strip(),
                        universe_id.strip(),
                        config_hash.strip(),
                        now,
                        now,
                    ),
                )
                preset_id = int(cursor.lastrowid)
                version_no = 1
                note = change_note.strip() or "创建策略"
            else:
                preset_id = int(existing["id"])
                conn.execute(
                    """
                    UPDATE strategy_presets
                    SET user_text = ?,
                        query_spec_json = ?,
                        summary = ?,
                        parse_source = ?,
                        ai_repair_retry = ?,
                        template_id = ?,
                        template_version = ?,
                        universe_id = ?,
                        config_hash = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        user_text.strip(),
                        spec_json,
                        summary_text,
                        parse_source.strip(),
                        1 if ai_repair_retry else 0,
                        template_id.strip(),
                        template_version.strip(),
                        universe_id.strip(),
                        config_hash.strip(),
                        now,
                        preset_id,
                    ),
                )
                row = conn.execute(
                    "SELECT COALESCE(MAX(version_no), 0) AS version_no FROM strategy_preset_versions WHERE preset_id = ?",
                    (preset_id,),
                ).fetchone()
                version_no = int(row["version_no"] or 0) + 1
                note = change_note.strip() or query_spec_change_summary(existing["query_spec_json"], spec_json)
            conn.execute(
                """
                INSERT INTO strategy_preset_versions(
                    preset_id, preset_name, version_no, user_text, query_spec_json,
                    summary, parse_source, ai_repair_retry, template_id,
                    template_version, universe_id, config_hash, change_note, created_at
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    preset_id,
                    name,
                    version_no,
                    user_text.strip(),
                    spec_json,
                    summary_text,
                    parse_source.strip(),
                    1 if ai_repair_retry else 0,
                    template_id.strip(),
                    template_version.strip(),
                    universe_id.strip(),
                    config_hash.strip(),
                    note[:1000],
                    now,
                ),
            )
            row = conn.execute("SELECT * FROM strategy_presets WHERE id = ?", (preset_id,)).fetchone()
        return _preset_from_row(row)

    def list_presets(self) -> list[StrategyPreset]:
        with self.database.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM strategy_presets
                ORDER BY updated_at DESC, id DESC
                """
            ).fetchall()
        return [_preset_from_row(row) for row in rows]

    def get_preset(self, preset_id: int) -> StrategyPreset | None:
        with self.database.connect() as conn:
            row = conn.execute("SELECT * FROM strategy_presets WHERE id = ?", (int(preset_id),)).fetchone()
        return _preset_from_row(row) if row else None

    def load_query_spec(self, preset_id: int) -> tuple[StrategyPreset, QuerySpec]:
        preset = self.get_preset(preset_id)
        if preset is None:
            raise StrategyStoreError("策略不存在或已被删除")
        try:
            payload = json.loads(preset.query_spec_json)
            spec = query_spec_from_dict(payload)
            validate_executable_query_spec(spec)
        except Exception as exc:
            raise QueryValidationError(f"策略已过期或当前版本不支持：{exc}") from exc
        return preset, spec

    def list_preset_versions(self, preset_id: int) -> list[StrategyPresetVersion]:
        with self.database.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM strategy_preset_versions
                WHERE preset_id = ?
                ORDER BY version_no DESC, id DESC
                """,
                (int(preset_id),),
            ).fetchall()
        return [_preset_version_from_row(row) for row in rows]

    def list_archived_preset_versions(self, limit: int = 100) -> list[StrategyPresetVersion]:
        with self.database.connect() as conn:
            rows = conn.execute(
                """
                SELECT strategy_preset_versions.*
                FROM strategy_preset_versions
                LEFT JOIN strategy_presets
                  ON strategy_presets.id = strategy_preset_versions.preset_id
                WHERE strategy_presets.id IS NULL
                ORDER BY strategy_preset_versions.created_at DESC, strategy_preset_versions.id DESC
                LIMIT ?
                """,
                (max(1, int(limit)),),
            ).fetchall()
        return [_preset_version_from_row(row) for row in rows]

    def get_preset_version(self, version_id: int) -> StrategyPresetVersion | None:
        with self.database.connect() as conn:
            row = conn.execute("SELECT * FROM strategy_preset_versions WHERE id = ?", (int(version_id),)).fetchone()
        return _preset_version_from_row(row) if row else None

    def compare_preset_versions(self, version_id: int, target: str = "previous") -> list[QuerySpecDiffItem]:
        version = self.get_preset_version(version_id)
        if version is None:
            raise StrategyStoreError("策略版本不存在")
        if target == "previous":
            with self.database.connect() as conn:
                row = conn.execute(
                    """
                    SELECT * FROM strategy_preset_versions
                    WHERE preset_id = ?
                      AND version_no < ?
                    ORDER BY version_no DESC, id DESC
                    LIMIT 1
                    """,
                    (version.preset_id, version.version_no),
                ).fetchone()
            if row is None:
                return [
                    QuerySpecDiffItem(
                        category="提示",
                        field_label="上一版本",
                        old_value="",
                        new_value="",
                        message="无上一版本可对比",
                    )
                ]
            return query_spec_diff_items(_preset_version_from_row(row).query_spec_json, version.query_spec_json)
        if target == "current":
            preset = self.get_preset(version.preset_id)
            if preset is None:
                return [
                    QuerySpecDiffItem(
                        category="提示",
                        field_label="当前策略",
                        old_value="",
                        new_value="",
                        message="当前策略已删除，无法和当前策略对比",
                    )
                ]
            return query_spec_diff_items(version.query_spec_json, preset.query_spec_json)
        raise StrategyStoreError("不支持的版本差异目标")

    def load_preset_version_spec(self, version_id: int) -> tuple[StrategyPresetVersion, QuerySpec]:
        version = self.get_preset_version(version_id)
        if version is None:
            raise StrategyStoreError("策略版本不存在")
        try:
            spec = query_spec_from_dict(json.loads(version.query_spec_json))
            validate_executable_query_spec(spec)
        except Exception as exc:
            raise QueryValidationError(f"策略版本已过期或当前版本不支持：{exc}") from exc
        return version, spec

    def restore_preset_version(self, version_id: int, change_note: str) -> StrategyPreset:
        version, _spec = self.load_preset_version_spec(version_id)
        note = change_note.strip() or f"恢复 version {version.version_no}"
        with self.database.connect() as conn:
            existing = conn.execute("SELECT * FROM strategy_presets WHERE id = ?", (version.preset_id,)).fetchone()
            if existing is None:
                existing = conn.execute("SELECT * FROM strategy_presets WHERE name = ?", (version.preset_name,)).fetchone()
            now = _now()
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO strategy_presets(
                        id, name, user_text, query_spec_json, summary, parse_source,
                        ai_repair_retry, template_id, template_version, universe_id,
                        config_hash, created_at, updated_at, last_run_at, run_count
                    )
                    VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', 0)
                    ON CONFLICT(name) DO UPDATE SET
                        user_text = excluded.user_text,
                        query_spec_json = excluded.query_spec_json,
                        summary = excluded.summary,
                        parse_source = excluded.parse_source,
                        ai_repair_retry = excluded.ai_repair_retry,
                        template_id = excluded.template_id,
                        template_version = excluded.template_version,
                        universe_id = excluded.universe_id,
                        config_hash = excluded.config_hash,
                        updated_at = excluded.updated_at
                    """,
                    (
                        version.preset_id,
                        version.preset_name,
                        version.user_text,
                        version.query_spec_json,
                        version.summary,
                        version.parse_source,
                        1 if version.ai_repair_retry else 0,
                        version.template_id,
                        version.template_version,
                        version.universe_id,
                        version.config_hash,
                        now,
                        now,
                    ),
                )
                target_preset_id = version.preset_id
                target_preset_name = version.preset_name
            else:
                target_preset_id = int(existing["id"])
                target_preset_name = existing["name"]
                conn.execute(
                    """
                    UPDATE strategy_presets
                    SET user_text = ?,
                        query_spec_json = ?,
                        summary = ?,
                        parse_source = ?,
                        ai_repair_retry = ?,
                        template_id = ?,
                        template_version = ?,
                        universe_id = ?,
                        config_hash = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        version.user_text,
                        version.query_spec_json,
                        version.summary,
                        version.parse_source,
                        1 if version.ai_repair_retry else 0,
                        version.template_id,
                        version.template_version,
                        version.universe_id,
                        version.config_hash,
                        now,
                        target_preset_id,
                    ),
                )
            if existing is None:
                target_preset_id = version.preset_id
                target_preset_name = version.preset_name
            row = conn.execute(
                "SELECT COALESCE(MAX(version_no), 0) AS version_no FROM strategy_preset_versions WHERE preset_id = ?",
                (target_preset_id,),
            ).fetchone()
            version_no = int(row["version_no"] or 0) + 1
            conn.execute(
                """
                INSERT INTO strategy_preset_versions(
                    preset_id, preset_name, version_no, user_text, query_spec_json,
                    summary, parse_source, ai_repair_retry, template_id,
                    template_version, universe_id, config_hash, change_note, created_at
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    target_preset_id,
                    target_preset_name,
                    version_no,
                    version.user_text,
                    version.query_spec_json,
                    version.summary,
                    version.parse_source,
                    1 if version.ai_repair_retry else 0,
                    version.template_id,
                    version.template_version,
                    version.universe_id,
                    version.config_hash,
                    note[:1000],
                    now,
                ),
            )
            preset_row = conn.execute("SELECT * FROM strategy_presets WHERE id = ?", (target_preset_id,)).fetchone()
        return _preset_from_row(preset_row)

    def delete_preset(self, preset_id: int) -> None:
        with self.database.connect() as conn:
            conn.execute("DELETE FROM strategy_presets WHERE id = ?", (int(preset_id),))

    def record_run(
        self,
        source_label: str,
        user_text: str,
        spec: QuerySpec,
        data_source: str,
        report: ScreenReport,
        preset_id: int | None = None,
        preset_name: str = "",
        summary: str | None = None,
        note: str = "",
        template_id: str = "",
        template_version: str = "",
        universe_id: str = "",
        universe_hash: str = "",
        config_hash: str = "",
        professional_formula: dict[str, Any] | None = None,
        scoring_context: dict[str, Any] | None = None,
    ) -> int:
        validate_executable_query_spec(spec)
        spec_json = _standard_spec_json(spec)
        summary_text = summary or query_spec_summary(spec)
        created_at = _now()
        run_scoring_context = _build_scoring_context(
            report=report,
            data_source=data_source,
            professional_formula=professional_formula,
            scoring_context=scoring_context,
        )
        scoring_context_json = json.dumps(run_scoring_context, ensure_ascii=False, sort_keys=True)
        rows = []
        for index, candidate in enumerate(report.results, start=1):
            quote = candidate.quote
            metrics = candidate.metrics
            technical = candidate.technical
            risk_flags = getattr(technical, "risk_flags", []) or []
            selection_contract = _selection_contract_from_breakdown(candidate.score_breakdown)
            active_formula = run_scoring_context.get("active_formula") if isinstance(run_scoring_context, dict) else {}
            metrics_payload = {
                "price": quote.price,
                "pe": quote.pe,
                "pe_ttm": getattr(quote, "pe_ttm", None),
                "pb": quote.pb,
                "market_cap_yi": round(quote.market_cap / 100000000, 4) if quote.market_cap else None,
                "roe": metrics.roe,
                "gross_margin": metrics.gross_margin,
                "net_margin": metrics.net_margin,
                "debt_ratio": metrics.debt_ratio,
                "revenue_growth": metrics.revenue_growth,
                "profit_growth": metrics.profit_growth,
                "industry": getattr(quote, "industry", ""),
                "industry_strength": getattr(quote, "industry_strength", None),
                "industry_rank": getattr(quote, "industry_rank", None),
                "industry_sample_count": getattr(quote, "industry_sample_count", None),
                "industry_updated_at": getattr(quote, "industry_updated_at", ""),
                "box_score": getattr(technical, "box_score", None),
                "box_upper": getattr(technical, "box_upper", None),
                "box_lower": getattr(technical, "box_lower", None),
                "box_range_pct": getattr(technical, "box_range_pct", None),
                "box_position_label": getattr(technical, "box_position_label", ""),
                "box_summary": getattr(technical, "box_summary", ""),
                "technical_label": getattr(technical, "opportunity_label", ""),
                "technical_status": getattr(technical, "status", ""),
                "legacy_score": candidate.legacy_score,
                "template_score": candidate.template_score,
                "experimental_score": candidate.experimental_score,
                "baseline_formula_id": str((run_scoring_context.get("baseline_formula") or {}).get("id", "")),
                "baseline_formula_version": str((run_scoring_context.get("baseline_formula") or {}).get("version", "")),
                "active_formula_version": active_formula.get("version", "") if isinstance(active_formula, dict) else "",
                "shadow_formula_version": active_formula.get("version", "") if isinstance(active_formula, dict) else "",
                **selection_contract,
            }
            rows.append(
                (
                    index,
                    quote.code,
                    quote.name,
                    candidate.local_score,
                    "、".join(candidate.reasons)[:1000],
                    json.dumps(risk_flags, ensure_ascii=False),
                    json.dumps(metrics_payload, ensure_ascii=False, sort_keys=True),
                    json.dumps(candidate.score_breakdown or {}, ensure_ascii=False, sort_keys=True),
                )
            )

        with self.database.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO query_runs(
                    preset_id, preset_name, source_label, user_text, query_spec_json,
                    summary, data_source, scanned, matched, skipped, failed, note,
                    template_id, template_version, universe_id, universe_hash, config_hash,
                    scoring_context_json, created_at
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    preset_id,
                    preset_name,
                    source_label,
                    user_text,
                    spec_json,
                    summary_text,
                    data_source,
                    report.scanned,
                    len(report.results),
                    report.skipped,
                    report.failed,
                    note[:1000],
                    template_id,
                    template_version,
                    universe_id,
                    universe_hash,
                    config_hash,
                    scoring_context_json,
                    created_at,
                ),
            )
            run_id = int(cursor.lastrowid)
            conn.executemany(
                """
                INSERT INTO query_run_results(
                    run_id, rank_order, code, name, local_score, reason, risk_flags_json, metrics_json
                    , score_breakdown_json
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [(run_id, *row) for row in rows],
            )
            if preset_id is not None:
                conn.execute(
                    """
                    UPDATE strategy_presets
                    SET last_run_at = ?, run_count = run_count + 1
                    WHERE id = ?
                    """,
                    (created_at, int(preset_id)),
                )
        return run_id

    def update_run_scoring_context(self, run_id: int, updates: dict[str, Any]) -> None:
        """Atomically append a small gate result to a persisted run identity."""
        with self.database.connect() as conn:
            row = conn.execute(
                "SELECT scoring_context_json FROM query_runs WHERE id = ?",
                (int(run_id),),
            ).fetchone()
            if row is None:
                raise ValueError("找不到策略运行记录")
            current = json.loads(row["scoring_context_json"] or "{}")
            if not isinstance(current, dict):
                current = {}
            current.update(dict(updates or {}))
            conn.execute(
                "UPDATE query_runs SET scoring_context_json = ? WHERE id = ?",
                (json.dumps(current, ensure_ascii=False, sort_keys=True), int(run_id)),
            )

    def list_recent_runs(self, preset_id: int | None = None, limit: int = 10) -> list[QueryRunRecord]:
        params: tuple[int, ...]
        if preset_id is None:
            sql = "SELECT * FROM query_runs ORDER BY id DESC LIMIT ?"
            params = (max(1, int(limit)),)
        else:
            sql = "SELECT * FROM query_runs WHERE preset_id = ? ORDER BY id DESC LIMIT ?"
            params = (int(preset_id), max(1, int(limit)))
        with self.database.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_run_from_row(row) for row in rows]

    def search_runs(
        self,
        *,
        preset_id: int | None = None,
        keyword: str = "",
        data_source: str = "",
        start_time: str = "",
        end_time: str = "",
        min_matched: int | None = None,
        limit: int = 100,
    ) -> list[QueryRunRecord]:
        clauses: list[str] = []
        params: list[object] = []
        if preset_id is not None:
            clauses.append("preset_id = ?")
            params.append(int(preset_id))
        if data_source:
            clauses.append("data_source = ?")
            params.append(data_source)
        if start_time:
            clauses.append("created_at >= ?")
            params.append(_normalize_start_time(start_time))
        if end_time:
            clauses.append("created_at <= ?")
            params.append(_normalize_end_time(end_time))
        if min_matched is not None:
            clauses.append("matched >= ?")
            params.append(max(0, int(min_matched)))
        keyword = keyword.strip()
        if keyword:
            like = f"%{keyword}%"
            clauses.append(
                """
                (
                    preset_name LIKE ?
                    OR user_text LIKE ?
                    OR summary LIKE ?
                    OR EXISTS (
                        SELECT 1
                        FROM query_run_results
                        WHERE query_run_results.run_id = query_runs.id
                          AND (code LIKE ? OR name LIKE ?)
                    )
                )
                """
            )
            params.extend([like, like, like, like, like])
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        sql = f"SELECT * FROM query_runs{where} ORDER BY id DESC LIMIT ?"
        params.append(max(1, int(limit)))
        with self.database.connect() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [_run_from_row(row) for row in rows]

    def get_run(self, run_id: int) -> QueryRunRecord | None:
        with self.database.connect() as conn:
            row = conn.execute("SELECT * FROM query_runs WHERE id = ?", (int(run_id),)).fetchone()
        return _run_from_row(row) if row else None

    def list_run_results(self, run_id: int) -> list[QueryRunResult]:
        with self.database.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM query_run_results
                WHERE run_id = ?
                ORDER BY rank_order ASC, id ASC
                """,
                (int(run_id),),
            ).fetchall()
        return [_run_result_from_row(row) for row in rows]

    def get_run_detail(self, run_id: int) -> QueryRunDetail:
        run = self.get_run(run_id)
        if run is None:
            raise StrategyStoreError("执行记录不存在")
        return QueryRunDetail(run=run, results=self.list_run_results(run_id))

    def _find_previous_run(self, current: QueryRunRecord) -> QueryRunRecord | None:
        clauses = ["id < ?"]
        params: list[object] = [current.id]
        if current.preset_id is not None:
            clauses.append("preset_id = ?")
            params.append(current.preset_id)
        elif current.preset_name:
            clauses.append("preset_name = ?")
            params.append(current.preset_name)
        else:
            return None
        sql = "SELECT * FROM query_runs WHERE " + " AND ".join(clauses) + " ORDER BY id DESC LIMIT 1"
        with self.database.connect() as conn:
            row = conn.execute(sql, tuple(params)).fetchone()
        return _run_from_row(row) if row else None

    def compare_runs(self, current_run_id: int, previous_run_id: int | None = None) -> RunComparison:
        current = self.get_run(int(current_run_id))
        if current is None:
            raise StrategyStoreError("当前执行记录不存在")
        previous = self.get_run(int(previous_run_id)) if previous_run_id is not None else self._find_previous_run(current)
        if previous is None:
            raise StrategyStoreError("当前执行记录没有同策略上一条记录可对比")

        current_results = {item.code: item for item in self.list_run_results(current.id)}
        previous_results = {item.code: item for item in self.list_run_results(previous.id)}
        added: list[RunComparisonItem] = []
        removed: list[RunComparisonItem] = []
        kept: list[RunComparisonItem] = []

        for code in sorted(current_results.keys() - previous_results.keys()):
            item = current_results[code]
            added.append(
                RunComparisonItem(
                    status="added",
                    code=code,
                    name=item.name,
                    current_rank=item.rank_order,
                    previous_rank=None,
                    rank_delta=None,
                    current_score=item.local_score,
                    previous_score=None,
                    score_delta=None,
                    metric_changes={},
                )
            )
        for code in sorted(previous_results.keys() - current_results.keys()):
            item = previous_results[code]
            removed.append(
                RunComparisonItem(
                    status="removed",
                    code=code,
                    name=item.name,
                    current_rank=None,
                    previous_rank=item.rank_order,
                    rank_delta=None,
                    current_score=None,
                    previous_score=item.local_score,
                    score_delta=None,
                    metric_changes={},
                )
            )
        for code in sorted(current_results.keys() & previous_results.keys(), key=lambda value: current_results[value].rank_order):
            current_item = current_results[code]
            previous_item = previous_results[code]
            kept.append(
                RunComparisonItem(
                    status="kept",
                    code=code,
                    name=current_item.name,
                    current_rank=current_item.rank_order,
                    previous_rank=previous_item.rank_order,
                    rank_delta=previous_item.rank_order - current_item.rank_order,
                    current_score=current_item.local_score,
                    previous_score=previous_item.local_score,
                    score_delta=_round_delta(current_item.local_score, previous_item.local_score),
                    metric_changes=_metric_changes(current_item, previous_item),
                )
            )
        return RunComparison(current_run=current, previous_run=previous, added=added, removed=removed, kept=kept)
