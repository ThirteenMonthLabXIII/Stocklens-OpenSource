from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from stock_tool.core.mvp_registry import load_product_registry
from stock_tool.core.shadow_weights import derive_shadow_weights


def main() -> int:
    parser = argparse.ArgumentParser(description="按预注册约束生成研究态影子权重；不会修改生产模板")
    parser.add_argument("template_id")
    parser.add_argument("evidence_json", type=Path, help="仅含2019–2024稳定正证据的字段->数值JSON")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    registry = load_product_registry()
    template = registry.template(args.template_id)
    evidence = json.loads(args.evidence_json.read_text(encoding="utf-8"))
    result = derive_shadow_weights(template.field_weights, evidence)
    payload = {
        "status": "research_only",
        "template_id": template.template_id,
        "base_template_version": template.version,
        "training_window": ["2019-01-01", "2024-12-31"],
        "holdout_used_for_selection": False,
        "production_prior": result.production_prior,
        "evidence_weights": result.evidence_weights,
        "shadow_weights": result.shadow_weights,
        "reason": result.reason,
    }
    output = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output + "\n", encoding="utf-8")
    else:
        print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
