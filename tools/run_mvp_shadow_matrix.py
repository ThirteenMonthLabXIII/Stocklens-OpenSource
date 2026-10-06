from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from stock_tool.core.data_location import read_data_dir
from stock_tool.core.factor_store import FACTOR_SNAPSHOT_MAX_AGE, V1FactorStore
from stock_tool.core.mvp_engine import screen_resolved_intent
from stock_tool.core.mvp_registry import load_product_registry
from stock_tool.core.mvp_resolver import ResolvedIntent
from stock_tool.core.mvp_scoring import scoring_config_for
from stock_tool.core.mvp_universe import (
    UniverseSnapshotStore,
    activation_status,
    production_rollout_status,
    record_shadow_run,
)
from stock_tool.core.query_schema import QuerySpec
from stock_tool.core.v1_database import V1Database, v1_db_path_for


def _result_hash(report) -> str:
    payload = [(item.quote.code, item.template_score, item.score_breakdown.get("missing_fields", [])) for item in report.results]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description="执行并留档15个MVP影子组合，不覆盖local_score")
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--run-date", default=date.today().isoformat())
    args = parser.parse_args()
    data_dir = args.data_dir or read_data_dir()
    if data_dir is None:
        parser.error("未配置数据目录，请提供 --data-dir")
    database = V1Database(v1_db_path_for(data_dir.resolve()))
    factors = V1FactorStore(database).list_latest_snapshots(max_age=FACTOR_SNAPSHOT_MAX_AGE)
    universes = UniverseSnapshotStore(database)
    registry = load_product_registry()
    failures: list[str] = []
    for universe_id in ("all_a", "hs300", "chinext"):
        universe = universes.latest(universe_id)
        for template_id, template in registry.templates.items():
            status = "failed"
            message = ""
            result_hash = ""
            scores: list[float] = []
            universe_hash = universe.membership_hash if universe else ""
            try:
                gate = activation_status(universe, factors, template_id, registry=registry, database=database)
                if not gate.active:
                    raise RuntimeError("；".join(gate.reasons))
                spec = QuerySpec(scan_limit=300, result_limit=template.default_top_n, raw_text=f"shadow:{template_id}:{universe_id}")
                intent = ResolvedIntent(template_id, universe_id, spec, scoring_config_for(template), config_hash=registry.config_hash)
                kwargs = {"hs300_codes": set(universe.members)} if universe_id == "hs300" else {}
                first = screen_resolved_intent(factors, intent, registry=registry, score_mode="production", legacy=True, **kwargs)
                second = screen_resolved_intent(factors, intent, registry=registry, score_mode="production", legacy=True, **kwargs)
                result_hash = _result_hash(first)
                if result_hash != _result_hash(second):
                    raise RuntimeError("同输入重复运行结果不一致")
                scores = [float(item.template_score) for item in first.results]
                if not scores or any(score < 0 or score > 100 for score in scores):
                    raise RuntimeError("结果为空或分数越界")
                member_set = set(universe.members)
                if any(item.quote.code not in member_set for item in first.results):
                    raise RuntimeError("检测到股票池泄漏")
                status = "passed"
                message = f"通过；结果{len(scores)}只"
            except Exception as exc:
                message = str(exc)
                failures.append(f"{template_id}×{universe_id}: {message}")
            record_shadow_run(
                database,
                run_date=args.run_date,
                template_id=template_id,
                universe_id=universe_id,
                template_version=template.version,
                config_hash=registry.config_hash,
                universe_hash=universe_hash,
                result_hash=result_hash,
                scores=scores,
                status=status,
                message=message,
            )
            print(f"[{status}] {template_id} × {universe_id}: {message}")
    rollout = production_rollout_status(database, registry.config_hash)
    print(f"完整影子交易日：{rollout.complete_trading_days}/{rollout.required_trading_days}")
    return 0 if not failures else 3


if __name__ == "__main__":
    raise SystemExit(main())
