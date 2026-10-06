from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from stock_tool.core.ai_analyzer import AIConfig
from stock_tool.core.research_discussion import (
    DiscussionValidationError,
    build_evidence_package,
    generate_research_discussion,
    validate_discussion_payload,
)


def run_tests() -> None:
    package = build_evidence_package(
        data_snapshot_id="snapshot-1",
        refresh_event_id="refresh-1",
        formula_version="baseline-v1",
        candidates=[{"code": "000001", "rank": 1, "score": 0.8}],
    )
    payload = {
        "supports": ["质量因子在行业内靠前"],
        "opposes": ["估值数据覆盖不足"],
        "factor_conflicts": [],
        "data_concerns": ["缺少最近一期公告时间"],
        "event_risks": ["等待公告核验"],
        "watch_conditions": ["补齐可验证数据后继续复核"],
        "data_completeness": {"coverage": 0.8},
    }
    report = generate_research_discussion(
        AIConfig(provider="fixture", api_key="fixture", model="fixture-model"),
        package,
        requester=lambda _config, _prompt: (payload, "fixture"),
    )
    assert report.status == "ok"
    assert report.data_snapshot_id == "snapshot-1"
    assert report.prompt_version == "research_discussion_v1"
    assert report.as_dict()["supports"] == ["质量因子在行业内靠前"]

    try:
        validate_discussion_payload({**payload, "action": "watch"})
    except DiscussionValidationError as exc:
        assert "未知字段" in str(exc)
    else:
        raise AssertionError("未知 AI 输出字段必须被拒绝")

    try:
        validate_discussion_payload({**payload, "supports": ["建议买入"]})
    except DiscussionValidationError:
        pass
    else:
        raise AssertionError("交易指令内容必须被拒绝")

    oversized = build_evidence_package(
        data_snapshot_id="snapshot-2",
        refresh_event_id="refresh-2",
        formula_version="baseline-v1",
        candidates=[{"code": str(index)} for index in range(40)],
    )
    assert len(oversized["candidates"]) == 30
    print("[OK] research discussion tests passed")


if __name__ == "__main__":
    run_tests()
