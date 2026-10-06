from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any


CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"
ALLOWED_TEMPLATE_STATUS = {"draft", "shadow", "active", "retired"}
ALLOWED_AVAILABILITY = {"buildable_now", "blocked", "experimental"}
ALLOWED_DIRECTIONS = {"higher_better", "lower_better", "interval_optimal"}
MISSING_POLICY = "neutral_50_keep_weights_discount_0.97"


class RegistryValidationError(ValueError):
    pass


@dataclass(frozen=True)
class DimensionDefinition:
    dimension_id: str
    display_name: str
    eligible_fields: tuple[str, ...]
    field_directions: dict[str, str]
    normalization: dict[str, Any]
    missing_value_policy: str
    default_weight: float
    data_availability: dict[str, str]
    pit_requirement: dict[str, str]


@dataclass(frozen=True)
class TemplateDefinition:
    template_id: str
    version: str
    status: str
    display_name: str
    supported_dimensions: tuple[str, ...]
    field_weights: dict[str, float]
    hard_filters: tuple[str, ...]
    default_top_n: int
    required_fields: tuple[str, ...]
    optional_fields: tuple[str, ...]
    missing_data_policy: str
    explanation_template: str


@dataclass(frozen=True)
class VocabEntry:
    canonical_term: str
    aliases: tuple[str, ...]
    target_type: str
    target_id: str
    operator_mapping: dict[str, Any] | None
    unit: str | None
    ambiguity: str
    requires_confirmation: bool
    unsupported_reason: str | None


@dataclass(frozen=True)
class ProductRegistry:
    dimensions: dict[str, DimensionDefinition]
    templates: dict[str, TemplateDefinition]
    vocabulary: tuple[VocabEntry, ...]
    config_hash: str
    registry_version: str
    template_set_version: str
    vocab_version: str

    def template(self, template_id: str, *, allow_draft: bool = False) -> TemplateDefinition:
        template = self.templates.get(template_id)
        if template is None:
            raise RegistryValidationError(f"未知模板：{template_id}")
        if template.status in {"draft", "retired"} and not allow_draft:
            raise RegistryValidationError(f"模板 {template_id} 当前状态为 {template.status}，不可执行")
        return template


@dataclass(frozen=True)
class MvpRuntimePolicy:
    score_mode: str
    production_mode_requires_shadow_trading_days: int
    production_mode_requires_all_combinations: int


def load_mvp_runtime_policy(config_dir: Path | None = None) -> MvpRuntimePolicy:
    payload = _load_json((config_dir or CONFIG_DIR) / "mvp_runtime.json")
    mode = str(payload.get("score_mode") or "").strip()
    if mode not in {"shadow", "production"}:
        raise RegistryValidationError("mvp_runtime.score_mode 必须为 shadow/production")
    days = int(payload.get("production_mode_requires_shadow_trading_days", 0))
    combinations = int(payload.get("production_mode_requires_all_combinations", 0))
    if days < 5 or combinations != 15:
        raise RegistryValidationError("生产切换门槛必须为至少5个交易日和15个模板×股票池组合")
    return MvpRuntimePolicy(mode, days, combinations)


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RegistryValidationError(f"配置读取失败：{path.name}: {exc}") from exc
    if not isinstance(payload, dict):
        raise RegistryValidationError(f"{path.name} 顶层必须是对象")
    if payload.get("schema_version") != 1:
        raise RegistryValidationError(f"{path.name} schema_version 必须为 1")
    return payload


def _required_text(row: dict[str, Any], key: str, context: str) -> str:
    value = str(row.get(key) or "").strip()
    if not value:
        raise RegistryValidationError(f"{context}.{key} 不能为空")
    return value


def _validate_dimensions(payload: dict[str, Any]) -> dict[str, DimensionDefinition]:
    rows = payload.get("dimensions")
    if not isinstance(rows, list) or not rows:
        raise RegistryValidationError("dimension_registry.dimensions 必须是非空列表")
    result: dict[str, DimensionDefinition] = {}
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise RegistryValidationError(f"dimensions[{index}] 必须是对象")
        context = f"dimensions[{index}]"
        dimension_id = _required_text(row, "dimension_id", context)
        if dimension_id in result:
            raise RegistryValidationError(f"维度重复：{dimension_id}")
        fields = tuple(str(item).strip() for item in row.get("eligible_fields", []) if str(item).strip())
        directions = row.get("field_directions")
        availability = row.get("data_availability")
        if not fields or not isinstance(directions, dict) or set(fields) != set(directions):
            raise RegistryValidationError(f"{dimension_id} 的 eligible_fields 与 field_directions 必须一一对应")
        if not isinstance(availability, dict) or set(fields) != set(availability):
            raise RegistryValidationError(f"{dimension_id} 的 data_availability 必须覆盖全部字段")
        if any(direction not in ALLOWED_DIRECTIONS for direction in directions.values()):
            raise RegistryValidationError(f"{dimension_id} 含非法 field direction")
        if any(status not in ALLOWED_AVAILABILITY for status in availability.values()):
            raise RegistryValidationError(f"{dimension_id} 含非法 data availability")
        weight = float(row.get("default_weight", -1))
        if not math.isfinite(weight) or weight < 0 or weight > 1:
            raise RegistryValidationError(f"{dimension_id}.default_weight 必须在 0-1")
        policy = _required_text(row, "missing_value_policy", context)
        if policy != MISSING_POLICY:
            raise RegistryValidationError(f"{dimension_id} 使用了未支持的缺失策略")
        normalization = row.get("normalization")
        pit = row.get("pit_requirement")
        if not isinstance(normalization, dict) or not isinstance(pit, dict) or not pit.get("live") or not pit.get("backtest"):
            raise RegistryValidationError(f"{dimension_id} 缺少 normalization 或 PIT 约束")
        result[dimension_id] = DimensionDefinition(
            dimension_id=dimension_id,
            display_name=_required_text(row, "display_name", context),
            eligible_fields=fields,
            field_directions={str(k): str(v) for k, v in directions.items()},
            normalization=normalization,
            missing_value_policy=policy,
            default_weight=weight,
            data_availability={str(k): str(v) for k, v in availability.items()},
            pit_requirement={str(k): str(v) for k, v in pit.items()},
        )
    if set(result) != {"quality", "growth", "value", "safety", "trend"}:
        raise RegistryValidationError("MVP 维度闭集必须恰为 quality/growth/value/safety/trend")
    if not math.isclose(sum(item.default_weight for item in result.values()), 1.0, abs_tol=1e-9):
        raise RegistryValidationError("维度 default_weight 之和必须为 1")
    return result


def _validate_templates(payload: dict[str, Any], dimensions: dict[str, DimensionDefinition]) -> dict[str, TemplateDefinition]:
    rows = payload.get("templates")
    if not isinstance(rows, list) or not rows:
        raise RegistryValidationError("template_set.templates 必须是非空列表")
    all_fields = {field for dimension in dimensions.values() for field in dimension.eligible_fields}
    field_availability = {field: status for dimension in dimensions.values() for field, status in dimension.data_availability.items()}
    result: dict[str, TemplateDefinition] = {}
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise RegistryValidationError(f"templates[{index}] 必须是对象")
        context = f"templates[{index}]"
        template_id = _required_text(row, "template_id", context)
        if template_id in result:
            raise RegistryValidationError(f"模板重复：{template_id}")
        status = _required_text(row, "status", context)
        if status not in ALLOWED_TEMPLATE_STATUS:
            raise RegistryValidationError(f"模板 {template_id} 状态非法")
        dims = tuple(str(item) for item in row.get("supported_dimensions", []))
        if not dims or any(item not in dimensions for item in dims):
            raise RegistryValidationError(f"模板 {template_id} 引用了未知维度")
        weights_payload = row.get("field_weights")
        if not isinstance(weights_payload, dict) or not weights_payload:
            raise RegistryValidationError(f"模板 {template_id} field_weights 不能为空")
        weights = {str(key): float(value) for key, value in weights_payload.items()}
        allowed = {field for dim in dims for field in dimensions[dim].eligible_fields}
        if not set(weights).issubset(allowed) or not set(weights).issubset(all_fields):
            raise RegistryValidationError(f"模板 {template_id} 引用了维度外字段")
        if any(not math.isfinite(value) or value <= 0 for value in weights.values()):
            raise RegistryValidationError(f"模板 {template_id} 权重必须为正有限数")
        if not math.isclose(sum(weights.values()), 1.0, abs_tol=1e-8):
            raise RegistryValidationError(f"模板 {template_id} 权重之和必须为 1")
        unavailable = [field for field in weights if field_availability.get(field) != "buildable_now"]
        if unavailable and status in {"shadow", "active"}:
            raise RegistryValidationError(f"模板 {template_id} 引用了不可构建字段：{','.join(unavailable)}")
        required = tuple(str(item) for item in row.get("required_fields", []))
        optional = tuple(str(item) for item in row.get("optional_fields", []))
        if set(required) & set(optional) or set(required) | set(optional) != set(weights):
            raise RegistryValidationError(f"模板 {template_id} required/optional 必须无重叠且覆盖全部权重字段")
        top_n = int(row.get("default_top_n", 0))
        if not 1 <= top_n <= 500:
            raise RegistryValidationError(f"模板 {template_id}.default_top_n 越界")
        policy = _required_text(row, "missing_data_policy", context)
        if policy != MISSING_POLICY:
            raise RegistryValidationError(f"模板 {template_id} 使用了未支持的缺失策略")
        result[template_id] = TemplateDefinition(
            template_id=template_id,
            version=_required_text(row, "version", context),
            status=status,
            display_name=_required_text(row, "display_name", context),
            supported_dimensions=dims,
            field_weights=weights,
            hard_filters=tuple(str(item) for item in row.get("hard_filters", [])),
            default_top_n=top_n,
            required_fields=required,
            optional_fields=optional,
            missing_data_policy=policy,
            explanation_template=_required_text(row, "explanation_template", context),
        )
    if set(result) != {"quality", "growth", "value", "quality_value", "trend"}:
        raise RegistryValidationError("MVP 模板闭集必须恰为 quality/growth/value/quality_value/trend")
    return result


def _validate_vocabulary(payload: dict[str, Any], templates: dict[str, TemplateDefinition]) -> tuple[VocabEntry, ...]:
    rows = payload.get("entries")
    if not isinstance(rows, list) or not rows:
        raise RegistryValidationError("controlled_vocab.entries 必须是非空列表")
    aliases: dict[str, tuple[str, str]] = {}
    result: list[VocabEntry] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise RegistryValidationError(f"entries[{index}] 必须是对象")
        context = f"entries[{index}]"
        entry_aliases = tuple(str(item).strip() for item in row.get("aliases", []) if str(item).strip())
        if not entry_aliases:
            raise RegistryValidationError(f"{context}.aliases 不能为空")
        target_type = _required_text(row, "target_type", context)
        target_id = _required_text(row, "target_id", context)
        if target_type == "template" and target_id not in templates:
            raise RegistryValidationError(f"词条引用未知模板：{target_id}")
        reason = row.get("unsupported_reason")
        if target_type == "unsupported" and not str(reason or "").strip():
            raise RegistryValidationError(f"不支持词条 {target_id} 必须提供原因")
        for alias in entry_aliases:
            key = alias.casefold()
            previous = aliases.get(key)
            current = (target_type, target_id)
            if previous is not None and previous != current:
                raise RegistryValidationError(f"词条别名冲突：{alias} -> {previous} / {current}")
            aliases[key] = current
        mapping = row.get("operator_mapping")
        if mapping is not None and not isinstance(mapping, dict):
            raise RegistryValidationError(f"{context}.operator_mapping 必须是对象或 null")
        result.append(VocabEntry(
            canonical_term=_required_text(row, "canonical_term", context),
            aliases=entry_aliases,
            target_type=target_type,
            target_id=target_id,
            operator_mapping=mapping,
            unit=None if row.get("unit") is None else str(row.get("unit")),
            ambiguity=_required_text(row, "ambiguity", context),
            requires_confirmation=bool(row.get("requires_confirmation", False)),
            unsupported_reason=None if reason is None else str(reason),
        ))
    return tuple(result)


def load_product_registry(config_dir: Path | None = None) -> ProductRegistry:
    root = config_dir or CONFIG_DIR
    dimension_path = root / "dimension_registry.json"
    template_path = root / "template_set.json"
    vocab_path = root / "controlled_vocab.json"
    dimensions_payload = _load_json(dimension_path)
    templates_payload = _load_json(template_path)
    vocab_payload = _load_json(vocab_path)
    dimensions = _validate_dimensions(dimensions_payload)
    templates = _validate_templates(templates_payload, dimensions)
    vocabulary = _validate_vocabulary(vocab_payload, templates)
    digest = hashlib.sha256()
    for path in (dimension_path, template_path, vocab_path):
        digest.update(path.read_bytes())
    return ProductRegistry(
        dimensions=dimensions,
        templates=templates,
        vocabulary=vocabulary,
        config_hash=digest.hexdigest(),
        registry_version=str(dimensions_payload.get("registry_version") or ""),
        template_set_version=str(templates_payload.get("set_version") or ""),
        vocab_version=str(vocab_payload.get("vocab_version") or ""),
    )
