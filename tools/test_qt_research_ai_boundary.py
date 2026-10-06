"""Static boundary checks for the Qt research-only AI path."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def test_qt_does_not_import_legacy_trade_ai() -> None:
    source = (ROOT / "stock_tool" / "ui" / "qt_workbench.py").read_text(encoding="utf-8")
    assert "from stock_tool.core.trade_ai import" not in source
    assert "import stock_tool.core.trade_ai" not in source
    assert "ResearchDiscussionService" in source
    assert "FiveAgentOrchestrator" in source
    assert "format_agent_statuses" in source
    assert "candidate_set_preserved" in source
    assert "build_evidence_package" in source
    assert "snapshot_metadata=manifest" in source
    assert "facts=candidate_facts" in source
    assert "raw_response=" in source
    assert "normalized_response=" in source


if __name__ == "__main__":
    test_qt_does_not_import_legacy_trade_ai()
    print("[OK] Qt research AI boundary tests passed")
