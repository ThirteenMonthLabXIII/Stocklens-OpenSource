from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from stock_tool.config.frontend_contract import FRONTEND_CONTRACT_SCHEMA_VERSION
from test_frontend_default_gate import validate_contract


def main() -> int:
    path = ROOT / "stock_tool" / "config" / "frontend_parity_contract.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload.get("schema_version") == FRONTEND_CONTRACT_SCHEMA_VERSION
    failures = validate_contract(payload, strict=False)
    assert not failures, "\n".join(failures)
    print("[OK] frontend parity contract 开发检查通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
