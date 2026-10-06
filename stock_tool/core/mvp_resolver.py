from __future__ import annotations

import re
from dataclasses import dataclass, field

from stock_tool.core.local_query_parser import parse_local_query
from stock_tool.core.mvp_registry import ProductRegistry, RegistryValidationError, load_product_registry
from stock_tool.core.mvp_scoring import ScoringConfig, scoring_config_for
from stock_tool.core.query_schema import QuerySpec


UNIVERSE_NAMES = {"all_a": "全A", "hs300": "沪深300", "chinext": "创业板"}


def normalize_stock_code(code: object) -> str:
    text = str(code or "").strip()
    if re.fullmatch(r"\d+\.0", text):
        text = text[:-2]
    matches = re.findall(r"(?<!\d)(\d{6})(?!\d)", text)
    if matches:
        return matches[-1]
    tail = text.split(".")[-1]
    return tail.zfill(6) if tail.isdigit() else text


@dataclass(frozen=True)
class UniverseSpec:
    universe_id: str
    display_name: str
    membership_mode: str = "current_snapshot"
    historical_replay_supported: bool = False


@dataclass
class ResolvedIntent:
    template_id: str
    universe_id: str
    query_spec: QuerySpec
    scoring_config: ScoringConfig
    confirmations: list[str] = field(default_factory=list)
    unsatisfiable: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    confidence: float = 1.0
    config_hash: str = ""
    ai_remainder: str = ""

    @property
    def executable(self) -> bool:
        return not self.confirmations and not self.unsatisfiable


def universe_spec(universe_id: str) -> UniverseSpec:
    if universe_id not in UNIVERSE_NAMES:
        raise RegistryValidationError(f"未知股票池：{universe_id}")
    return UniverseSpec(universe_id=universe_id, display_name=UNIVERSE_NAMES[universe_id])


def restore_mvp_intent(
    template_id: str,
    template_version: str,
    universe_id: str,
    query_spec: QuerySpec,
    config_hash: str,
    registry: ProductRegistry | None = None,
) -> ResolvedIntent:
    """Rehydrate a saved typed intent without silently changing its contract."""
    registry = registry or load_product_registry()
    template = registry.templates.get(template_id)
    if template is None:
        raise RegistryValidationError(f"已保存策略引用未知模板：{template_id}")
    if template.version != template_version:
        raise RegistryValidationError(
            f"模板版本已变化（保存为 {template_version}，当前为 {template.version}），请重新解析并确认"
        )
    if config_hash and config_hash != registry.config_hash:
        raise RegistryValidationError("闭集配置已变化，请重新解析并确认后再执行")
    universe_spec(universe_id)
    return ResolvedIntent(
        template_id=template_id,
        universe_id=universe_id,
        query_spec=query_spec,
        scoring_config=scoring_config_for(template),
        config_hash=registry.config_hash,
    )


def code_in_universe(code: str, universe_id: str, hs300_codes: set[str] | None = None) -> bool:
    normalized = normalize_stock_code(code)
    if universe_id == "all_a":
        return normalized.startswith(("000", "001", "002", "003", "300", "301", "4", "8", "920", "600", "601", "603", "605", "688"))
    if universe_id == "chinext":
        return normalized.startswith(("300", "301"))
    if universe_id == "hs300":
        if hs300_codes is None:
            raise RegistryValidationError("沪深300股票池缺少已验证的成分快照")
        return normalized in {normalize_stock_code(item) for item in hs300_codes}
    raise RegistryValidationError(f"未知股票池：{universe_id}")


def filter_universe(items: list, universe_id: str, hs300_codes: set[str] | None = None) -> list:
    return [item for item in items if code_in_universe(getattr(item, "code", ""), universe_id, hs300_codes)]


def _matches(text: str, alias: str) -> bool:
    return alias.casefold() in text.casefold()


def _typed_vocab_matches(text: str, registry: ProductRegistry) -> list[tuple[str, str, str, object]]:
    matches: list[tuple[str, str, str, object]] = []
    for entry in registry.vocabulary:
        matching = [alias for alias in entry.aliases if _matches(text, alias)]
        if matching:
            alias = max(matching, key=len)
            if entry.target_id == "pe_ttm|pe" and re.search(r"PE\s*-?\s*TTM|PETTM|滚动市盈率|动态市盈率|动态PE", text, re.IGNORECASE):
                continue
            matches.append((entry.target_type, entry.target_id, alias, entry))
    matches.sort(key=lambda item: len(item[2]), reverse=True)
    return matches


def controlled_vocab_guardrail_issues(
    text: str, registry: ProductRegistry | None = None
) -> tuple[list[str], list[str]]:
    """Inspect original text independently of local/AI parsed output."""
    registry = registry or load_product_registry()
    matches = _typed_vocab_matches(str(text or ""), registry)
    confirmations: list[str] = []
    unsatisfiable: list[str] = []
    for target_type, target_id, alias, entry in matches:
        if target_type == "unsupported":
            unsatisfiable.append(f"“{alias}”：{entry.unsupported_reason}")
        if target_type == "field" and target_id == "pe_ttm|pe" and re.search(rf"{re.escape(alias)}[^，,。]{{0,12}}\d", text, re.IGNORECASE):
            confirmations.append("裸 PE/市盈率可能指动态 PE 或 PE-TTM，请明确口径")
    if any(item[0] == "field" for item in matches) and any(item[0] == "fuzzy" for item in matches):
        confirmations.append("模糊词正在修饰具体字段，请给出明确阈值")
    return list(dict.fromkeys(confirmations)), list(dict.fromkeys(unsatisfiable))


def _choose_template(matches: list[tuple[str, str, str, object]]) -> tuple[str, list[str]]:
    templates = [(target_id, alias) for target_type, target_id, alias, _entry in matches if target_type in {"template", "sort"}]
    if not templates:
        return "", []
    combined = [item for item in templates if item[0] == "quality_value"]
    if combined:
        return "quality_value", []
    ids = list(dict.fromkeys(item[0] for item in templates))
    if len(ids) > 1:
        return ids[0], [f"同时识别到多个模板意图（{', '.join(ids)}），请只保留一个或使用“质量价值”组合模板"]
    return ids[0], []


def _choose_universe(matches: list[tuple[str, str, str, object]]) -> tuple[str, list[str]]:
    ids = list(dict.fromkeys(target_id for target_type, target_id, _alias, _entry in matches if target_type == "universe"))
    if len(ids) > 1:
        return ids[0], [f"同时识别到多个股票池（{', '.join(ids)}），请确认一个股票池"]
    return (ids[0] if ids else "all_a"), []


def _limit_from_text(text: str, default: int) -> int:
    patterns = [r"排名前\s*(\d+)\s*(?:名|只)?", r"前\s*(\d+)\s*(?:名|只)"]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return max(1, min(500, int(match.group(1))))
    return default


def _unparsed_remainder(text: str, matches: list[tuple[str, str, str, object]]) -> str:
    remainder = text
    aliases = sorted({alias for _target_type, _target_id, alias, _entry in matches}, key=len, reverse=True)
    for alias in aliases:
        remainder = re.sub(re.escape(alias), " ", remainder, flags=re.IGNORECASE)
    remainder = re.sub(r"排名前\s*\d+\s*(?:名|只)?|前\s*\d+\s*(?:名|只)?", " ", remainder, flags=re.IGNORECASE)
    fillers = ("帮我", "给我", "我想要", "请", "筛选", "选择", "找", "股票", "公司", "要求", "并且", "同时", "且", "和", "中", "里", "按", "排序", "优先")
    for filler in fillers:
        remainder = remainder.replace(filler, " ")
    remainder = re.sub(r"[，,。；;：:、（）()\[\]{}]+", " ", remainder)
    remainder = re.sub(r"\s+", " ", remainder).strip()
    signature = re.sub(r"[-+]?\d+(?:\.\d+)?\s*(?:%|％|倍|亿|亿元|万|只|名|日|年|季度)?", "", remainder)
    signature = re.sub(r"\s+", "", signature)
    return remainder if signature else ""


def merge_ai_query_spec(intent: ResolvedIntent, ai_spec: QuerySpec) -> ResolvedIntent:
    """Merge only validated AI leftovers into an already typed template intent."""
    existing = {(item.field, item.op, item.value, item.value_to) for item in intent.query_spec.conditions}
    for condition in ai_spec.conditions:
        key = (condition.field, condition.op, condition.value, condition.value_to)
        if key not in existing:
            intent.query_spec.conditions.append(condition)
            existing.add(key)
    if ai_spec.sort:
        intent.query_spec.sort = ai_spec.sort
    confirmations = [item for item in intent.confirmations if not item.startswith("AI需解析剩余表达：")]
    confirmations.extend(ai_spec.clarifications)
    if ai_spec.needs_confirmation and not ai_spec.clarifications:
        confirmations.append("AI 对剩余表达仍不确定，请改写为明确字段和阈值")
    intent.confirmations = list(dict.fromkeys(confirmations))
    intent.ai_remainder = ""
    intent.confidence = min(intent.confidence, 0.85)
    return intent


def resolve_mvp_intent(text: str, registry: ProductRegistry | None = None) -> ResolvedIntent | None:
    registry = registry or load_product_registry()
    raw_text = str(text or "").strip()
    if not raw_text:
        return None
    matches = _typed_vocab_matches(raw_text, registry)
    template_id, confirmations = _choose_template(matches)
    if not template_id:
        return None
    universe_id, universe_confirmations = _choose_universe(matches)
    confirmations.extend(universe_confirmations)
    guard_confirmations, unsatisfiable = controlled_vocab_guardrail_issues(raw_text, registry)
    confirmations.extend(guard_confirmations)
    warnings: list[str] = []
    field_matches = [item for item in matches if item[0] == "field"]
    fuzzy_matches = [item for item in matches if item[0] == "fuzzy"]
    if field_matches and fuzzy_matches and "模糊词正在修饰具体字段，请给出明确阈值" in confirmations:
        confirmations[confirmations.index("模糊词正在修饰具体字段，请给出明确阈值")] = "模糊词正在修饰具体字段，请给出明确阈值或改为只选择模板风格"

    template = registry.template(template_id)
    parsed = parse_local_query(raw_text)
    if parsed is None:
        parsed = QuerySpec(raw_text=raw_text)
    else:
        # Template aliases intentionally replace the old behavior that demanded a numeric threshold.
        resolved_fragments = ("估值低", "低估", "便宜", "成长好", "高成长", "趋势好", "走势好", "稳健", "优质")
        remaining: list[str] = []
        for message in parsed.clarifications:
            if any(fragment in message for fragment in resolved_fragments):
                continue
            if "当前版本暂不支持" in message:
                if message not in unsatisfiable:
                    unsatisfiable.append(message)
            else:
                remaining.append(message)
        parsed.clarifications = []
        parsed.needs_confirmation = False
        confirmations.extend(remaining)
    parsed.result_limit = _limit_from_text(raw_text, template.default_top_n)
    parsed.raw_text = raw_text
    if template_id == "trend":
        parsed.with_technical = True
    ai_remainder = _unparsed_remainder(raw_text, matches)
    if ai_remainder:
        confirmations.append(f"AI需解析剩余表达：{ai_remainder}")
    return ResolvedIntent(
        template_id=template_id,
        universe_id=universe_id,
        query_spec=parsed,
        scoring_config=scoring_config_for(template),
        confirmations=list(dict.fromkeys(confirmations)),
        unsatisfiable=list(dict.fromkeys(unsatisfiable)),
        warnings=warnings,
        confidence=1.0 if not confirmations else 0.7,
        config_hash=registry.config_hash,
        ai_remainder=ai_remainder,
    )


def resolved_intent_summary(intent: ResolvedIntent, registry: ProductRegistry | None = None) -> str:
    registry = registry or load_product_registry()
    template = registry.template(intent.template_id)
    universe = universe_spec(intent.universe_id)
    parts = [f"模板：{template.display_name} v{template.version}", f"股票池：{universe.display_name}", f"结果上限：{intent.query_spec.result_limit}"]
    if intent.confirmations:
        parts.append("需确认：" + "；".join(intent.confirmations))
    if intent.unsatisfiable:
        parts.append("无法执行：" + "；".join(intent.unsatisfiable))
    return "；".join(parts)
