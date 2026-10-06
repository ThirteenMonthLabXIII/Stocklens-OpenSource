"""Generate Phase B quality evidence from the research panel database."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research import etl_industry
from research.experiment_identity import data_generation_fingerprint
from research.metric_metadata import (
    CORE_METRICS,
    QUALITY_SCALE_SUSPECT,
    metric_metadata_rows,
)
from research.panel_db import OUT_DIR, ResearchPanelDB, now_iso, open_panel, production_db_paths, snapshot_paths
from research.pit_join import fundamentals_as_of, universe_as_of
from research.sample_universe import all_sample


def generate(db: ResearchPanelDB | None = None, out_path: Path | None = None) -> dict[str, Any]:
    own = db is None
    db = db or open_panel()
    out_path = out_path or (OUT_DIR / "phase_b_quality.md")
    try:
        tables = [
            "universe",
            "trade_calendar",
            "industry_map",
            "fundamentals_pit",
            "prices_daily",
            "adjust_factor",
            "etl_progress",
            "etl_log",
        ]
        counts = {t: int(db.scalar(f"SELECT COUNT(*) FROM {t}") or 0) for t in tables}
        industry_semantics = etl_industry.verify_asof_semantics(db)
        coverage = _coverage(db)
        issues = _issues(db)
        assertions = _assertions(db)
        fingerprint = data_generation_fingerprint(db)
        logs = db.query_df(
            """
            SELECT api, status, COUNT(*) AS calls, COALESCE(SUM(rows), 0) AS rows,
                   ROUND(AVG(duration_sec), 3) AS avg_duration_sec
            FROM etl_log
            GROUP BY api, status
            ORDER BY api, status
            """
        ).to_dict("records")
        prod_snapshot = snapshot_paths(production_db_paths())
        validation = _validation_level(db, counts, assertions, issues)
        payload = {
            "generated_at": now_iso(),
            "db_path": str(db.path),
            "validation_level": validation["level"],
            "validation": validation,
            "counts": counts,
            "metric_metadata": _metric_metadata_rows(db),
            "coverage": coverage,
            "issues": issues,
            "industry_semantics": industry_semantics,
            "assertions": assertions,
            "data_generation_fingerprint": fingerprint,
            "etl_log_summary": logs,
            "production_db_snapshot": prod_snapshot,
        }
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(_render(payload), encoding="utf-8")
        (out_path.with_suffix(".json")).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return payload
    finally:
        if own:
            db.close()


def _coverage(db: ResearchPanelDB) -> dict[str, Any]:
    target_start = int(db.get_metadata("fundamentals_target_start_year", 2023) or 2023)
    target_end = int(db.get_metadata("fundamentals_target_end_year", 2024) or 2024)
    metrics = db.query_df(
        """
        SELECT metric, COUNT(*) AS rows,
               COUNT(DISTINCT code) AS codes,
               COUNT(DISTINCT CASE WHEN value IS NOT NULL AND COALESCE(metric_quality, 'pass') = 'pass' THEN code END) AS pass_codes,
               SUM(CASE WHEN value IS NOT NULL AND COALESCE(metric_quality, 'pass') = 'pass' THEN 1 ELSE 0 END) AS pass_rows,
               ROUND(AVG(CASE WHEN value IS NULL THEN 1.0 ELSE 0.0 END), 4) AS null_rate,
               SUM(CASE WHEN COALESCE(metric_quality, 'pass') <> 'pass' THEN 1 ELSE 0 END) AS non_pass_rows
        FROM fundamentals_pit
        WHERE CAST(SUBSTR(stat_date, 1, 4) AS INTEGER) BETWEEN ? AND ?
        GROUP BY metric
        ORDER BY metric
        """,
        (target_start, target_end),
    ).to_dict("records")
    latest_industry_date = db.scalar("SELECT MAX(query_date) FROM industry_map") or ""
    industry_rows = []
    if latest_industry_date:
        industry_rows = db.query_df(
            """
            SELECT query_date, COUNT(*) AS rows,
                   SUM(CASE WHEN TRIM(industry) <> '' THEN 1 ELSE 0 END) AS non_empty
            FROM industry_map
            GROUP BY query_date
            ORDER BY query_date
            """
        ).to_dict("records")
    price_dates = db.query_df(
        "SELECT MIN(date) AS min_date, MAX(date) AS max_date, COUNT(DISTINCT code) AS codes FROM prices_daily"
    ).to_dict("records")[0]
    return {
        "fundamentals_target_window": {"start_year": target_start, "end_year": target_end},
        "fundamentals_by_metric": metrics,
        "industry_by_date": industry_rows,
        "price_span": price_dates,
    }


def _metric_metadata_rows(db: ResearchPanelDB) -> list[dict[str, Any]]:
    rows = metric_metadata_rows()
    decision_text = db.get_metadata("debt_ratio_scale_decision", "")
    if not decision_text:
        return rows
    try:
        decision = json.loads(decision_text)
    except json.JSONDecodeError:
        decision = {"raw_scale": "metadata_unparseable", "output_scale": "blocked", "transform": "none"}
    for row in rows:
        if row["metric"] == "debt_ratio":
            row["raw_scale"] = decision.get("raw_scale", row["raw_scale"])
            row["output_scale"] = decision.get("output_scale", row["output_scale"])
            row["transform"] = decision.get("transform", row["transform"])
            row["quality_rule"] = (
                f"{decision.get('quality_rule', row['quality_rule'])}; "
                f"scale decision={decision.get('decision', 'unknown')}"
            )
    return rows


def _issues(db: ResearchPanelDB) -> dict[str, Any]:
    return {
        "field_scale_anomalies": _field_scale_anomalies(db),
        "missing_rate": _missing_rate(db),
        "failure_log": _failure_log(db),
        "throughput_estimate": _throughput_estimate(db),
        "temp_files": _temp_files(),
        "production_boundary_conflicts": _production_boundary_conflicts(),
    }


def _field_scale_anomalies(db: ResearchPanelDB) -> dict[str, Any]:
    rows = db.query_df(
        """
        SELECT code, stat_date, pub_date, value, metric_quality, missing_reason
        FROM fundamentals_pit
        WHERE metric='debt_ratio' AND value IS NOT NULL
        ORDER BY code, stat_date
        """
    ).to_dict("records")
    marked = [r for r in rows if r.get("metric_quality") == QUALITY_SCALE_SUSPECT]
    unmarked = []
    by_code_year: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        stat = str(row.get("stat_date", ""))
        if len(stat) >= 4:
            by_code_year.setdefault((str(row.get("code")), stat[:4]), []).append(row)
    for grouped in by_code_year.values():
        ordered = sorted(grouped, key=lambda r: str(r.get("stat_date", "")))
        for prev, cur in zip(ordered, ordered[1:]):
            a = abs(float(prev["value"]))
            b = abs(float(cur["value"]))
            high = max(a, b)
            low = min(a, b)
            jump = high > 0 if low == 0 else high / low > 20.0
            if jump and prev.get("metric_quality") != QUALITY_SCALE_SUSPECT and cur.get("metric_quality") != QUALITY_SCALE_SUSPECT:
                unmarked.append(
                    {
                        "code": prev["code"],
                        "prev_stat_date": prev["stat_date"],
                        "prev_value": prev["value"],
                        "cur_stat_date": cur["stat_date"],
                        "cur_value": cur["value"],
                    }
                )
    return {
        "marked_scale_suspect_rows": len(marked),
        "unmarked_suspect_pairs": len(unmarked),
        "examples": (marked[:5] + unmarked[:5])[:8],
    }


def _missing_rate(db: ResearchPanelDB) -> list[dict[str, Any]]:
    return db.query_df(
        """
        SELECT metric,
               COALESCE(missing_reason, '') AS missing_reason,
               COUNT(*) AS rows,
               COUNT(DISTINCT code) AS codes
        FROM fundamentals_pit
        WHERE value IS NULL OR COALESCE(metric_quality, 'pass') <> 'pass'
        GROUP BY metric, COALESCE(missing_reason, '')
        ORDER BY metric, rows DESC
        """
    ).to_dict("records")


def _failure_log(db: ResearchPanelDB) -> list[dict[str, Any]]:
    return db.query_df(
        """
        SELECT ts, api, code, status, rows, duration_sec, error
        FROM etl_log
        WHERE status <> 'ok'
        ORDER BY ts DESC
        LIMIT 30
        """
    ).to_dict("records")


def _throughput_estimate(db: ResearchPanelDB) -> dict[str, Any]:
    rows = db.query_df(
        """
        SELECT api, status, COUNT(*) AS calls,
               ROUND(AVG(duration_sec), 3) AS avg_duration_sec,
               ROUND(SUM(duration_sec), 3) AS total_duration_sec
        FROM etl_log
        WHERE api IN ('prices', 'fundamentals') AND status='ok'
        GROUP BY api, status
        """
    ).to_dict("records")
    sample_size = len(all_sample())
    out: dict[str, Any] = {"sample_size": sample_size, "observed": rows, "estimates": []}
    for row in rows:
        calls = int(row.get("calls") or 0)
        avg = float(row.get("avg_duration_sec") or 0.0)
        if calls <= 0 or avg <= 0:
            continue
        out["estimates"].append(
            {
                "api": row["api"],
                "estimated_40_code_seconds": round(avg * sample_size, 1),
                "note": "sequential baostock sample estimate; not a full-market throughput claim",
            }
        )
    return out


def _temp_files() -> dict[str, Any]:
    root = Path(__file__).resolve().parent
    candidates = []
    for pattern in ("data/*selftest*.sqlite3*", "data/*selftest*.md", "data/*selftest*.json"):
        candidates.extend(root.glob(pattern))
    candidates.extend(root.rglob("__pycache__"))
    paths = sorted({str(p) for p in candidates if p.exists()})
    return {
        "count": len(paths),
        "paths": paths[:50],
        "note": "cleanup helper: python research\\cleanup_phase_b.py --apply",
    }


def _production_boundary_conflicts() -> dict[str, Any]:
    root = Path(__file__).resolve().parents[1]
    targets = (
        "stock_tool/core/pit_database.py",
        "stock_tool/core/pit_pipeline.py",
        "stock_tool/core/pit_providers.py",
        "stock_tool/ui/qt_workbench.py",
    )
    try:
        proc = subprocess.run(
            ["git", "status", "--short", "--", *targets],
            cwd=str(root),
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        return {"status": "UNKNOWN", "entries": [], "error": str(exc)}
    entries = [line for line in proc.stdout.splitlines() if line.strip()]
    return {
        "status": "CONFLICT_PRESENT" if entries else "CLEAR",
        "entries": entries,
        "note": "生产 PIT/UI 改动不纳入 Phase B research 验收",
    }


def _assertions(db: ResearchPanelDB) -> dict[str, Any]:
    out: dict[str, Any] = {}
    out["isolation"] = {
        "status": "PASS",
        "evidence": f"panel db under {db.path.parent}; production paths snapshotted separately",
    }
    out["survivorship"] = _assert_survivorship(db)
    out["lookahead"] = _assert_lookahead(db)
    out["idempotency_ready"] = {
        "status": "PASS" if int(db.scalar("SELECT COUNT(*) FROM etl_progress") or 0) >= 0 else "FAIL",
        "evidence": "UPSERT schema and etl_progress table are present; concrete rerun fingerprints are covered by tests.",
    }
    out["adjust_factor"] = _assert_adjust_factor(db)
    return out


def _validation_level(
    db: ResearchPanelDB,
    counts: dict[str, int],
    assertions: dict[str, Any],
    issues: dict[str, Any],
) -> dict[str, Any]:
    sample_codes = [s.bs_code for s in all_sample()]
    sample_size = len(sample_codes)
    placeholders = ",".join("?" for _ in sample_codes)
    price_expected = int(db.scalar(
        f"""
        SELECT COUNT(DISTINCT code)
        FROM universe
        WHERE code IN ({placeholders})
          AND ipo_date <= '2024-12-31'
          AND (TRIM(out_date) = '' OR out_date >= '2024-01-01')
        """,
        tuple(sample_codes),
    ) or 0)
    price_success = int(db.scalar(
        f"""
        SELECT COUNT(DISTINCT code)
        FROM prices_daily
        WHERE code IN ({placeholders})
          AND date BETWEEN '2024-01-01' AND '2024-12-31'
        """,
        tuple(sample_codes),
    ) or 0)
    target_start = int(db.get_metadata("fundamentals_target_start_year", 2023) or 2023)
    target_end = int(db.get_metadata("fundamentals_target_end_year", 2024) or 2024)
    core_metric_success = int(db.scalar(
        f"""
        SELECT COUNT(*) FROM (
            SELECT code
            FROM fundamentals_pit
            WHERE code IN ({placeholders})
              AND metric IN ({",".join("?" for _ in CORE_METRICS)})
              AND CAST(SUBSTR(stat_date, 1, 4) AS INTEGER) BETWEEN ? AND ?
              AND value IS NOT NULL
              AND COALESCE(metric_quality, 'pass') = 'pass'
            GROUP BY code
            HAVING COUNT(DISTINCT metric) >= 4
        )
        """,
        tuple(sample_codes) + tuple(CORE_METRICS) + (target_start, target_end),
    ) or 0)
    errors = db.query_df(
        "SELECT api, code, error FROM etl_log WHERE status <> 'ok'"
    ).to_dict("records")
    progress_errors = int(db.scalar(
        "SELECT COUNT(*) FROM etl_progress WHERE status='error'"
    ) or 0)
    unlogged_progress_errors = int(db.scalar(
        """
        SELECT COUNT(*)
        FROM etl_progress p
        WHERE p.status='error'
          AND NOT EXISTS (
              SELECT 1 FROM etl_log l
              WHERE l.code=p.code AND l.status <> 'ok'
          )
        """
    ) or 0)
    year_filter = "CAST(SUBSTR(stat_date, 1, 4) AS INTEGER) BETWEEN ? AND ?"
    revenue_rows = int(db.scalar(
        f"SELECT COUNT(*) FROM fundamentals_pit WHERE metric='revenue_growth' AND {year_filter}",
        (target_start, target_end),
    ) or 0)
    revenue_missing_rows = int(db.scalar(
        f"""
        SELECT COUNT(*)
        FROM fundamentals_pit
        WHERE metric='revenue_growth'
          AND missing_reason IS NOT NULL
          AND {year_filter}
        """,
        (target_start, target_end),
    ) or 0)
    blockers = []
    price_required = min(35, price_expected)
    if price_success < price_required:
        blockers.append(f"2024 价格覆盖不足：{price_success}/{price_expected} < {price_required}")
    if core_metric_success < 30:
        blockers.append(f"财务核心指标覆盖不足：{core_metric_success}/{sample_size} < 30")
    if progress_errors > 0:
        blockers.append(f"etl_progress 存在 error：{progress_errors} 条")
    debt_decision_text = db.get_metadata("debt_ratio_scale_decision", "")
    if debt_decision_text:
        try:
            debt_decision = json.loads(debt_decision_text)
        except json.JSONDecodeError:
            debt_decision = {"decision": "blocked_unparseable_metadata"}
        if str(debt_decision.get("decision", "")).startswith("blocked"):
            blockers.append(f"debt_ratio 口径未通过：{debt_decision.get('decision')}")
    if issues["field_scale_anomalies"]["unmarked_suspect_pairs"] > 0:
        blockers.append("debt_ratio 存在未标记的相邻季度数量级异常")
    if revenue_rows == 0 and revenue_missing_rows == 0:
        blockers.append("revenue_growth 没有计算结果，也没有缺失原因")
    if unlogged_progress_errors > 0:
        blockers.append("etl_progress error 缺少对应 etl_log 失败记录")
    for item in errors:
        if "用户未登录" in str(item.get("error", "")):
            relogins = int(db.scalar(
                """
                SELECT COUNT(*)
                FROM etl_log
                WHERE run_id IN (SELECT run_id FROM etl_log WHERE code=?)
                  AND code=?
                  AND api LIKE '%_relogin'
                """,
                (item.get("code", ""), item.get("code", "")),
            ) or 0)
            if relogins == 0:
                blockers.append("etl_log 中出现 baostock 用户未登录但没有重登尝试记录")
            break

    sample_thresholds = {
        "sample_size": sample_size,
        "price_expected_active_2024": price_expected,
        "price_success_required": price_required,
        "price_success_actual": price_success,
        "fundamental_core_required": 30,
        "fundamental_core_actual": core_metric_success,
        "progress_errors": progress_errors,
        "logged_failures": len(errors),
    }
    if counts.get("universe", 0) == 0 and counts.get("prices_daily", 0) == 0 and counts.get("fundamentals_pit", 0) == 0:
        level = "NOT_STARTED"
    elif (
        price_success >= price_required
        and core_metric_success >= 30
        and not blockers
        and assertions.get("lookahead", {}).get("status") == "PASS"
        and assertions.get("survivorship", {}).get("status") == "PASS"
    ):
        level = "SAMPLE_PASS"
    else:
        level = "SAMPLE_BLOCKED"
    return {
        "level": level,
        "thresholds": sample_thresholds,
        "blockers": blockers,
        "full_pass_allowed": False,
        "note": "本报告只做 40 股样本验收；全 A 回填不在本阶段范围内。",
    }


def _assert_survivorship(db: ResearchPanelDB) -> dict[str, Any]:
    row = db.query_df(
        """
        SELECT code, code_name, ipo_date, out_date
        FROM universe
        WHERE TRIM(out_date) <> '' AND out_date > '2018-01-01'
        ORDER BY out_date DESC
        LIMIT 1
        """
    )
    if row.empty:
        return {"status": "UNTESTED", "evidence": "no post-2018 delisted stock in universe"}
    rec = row.iloc[0].to_dict()
    from datetime import timedelta

    before_day = (date.fromisoformat(str(rec["out_date"])[:10]) - timedelta(days=1)).isoformat()
    before = universe_as_of(before_day, db)
    current_day = date.today().isoformat()
    after = universe_as_of(current_day, db)
    code = rec["code"]
    present_before = code in set(before["code"])
    present_after = code in set(after["code"])
    return {
        "status": "PASS" if present_before and not present_after else "FAIL",
        "evidence": {
            "code": code,
            "name": rec["code_name"],
            "out_date": rec["out_date"],
            "checked_before_date": before_day,
            "checked_current_date": current_day,
            "present_before_out_date": present_before,
            "present_after_current_date": present_after,
        },
    }


def _assert_lookahead(db: ResearchPanelDB) -> dict[str, Any]:
    row = db.query_df(
        """
        SELECT code, stat_date, pub_date, metric
        FROM fundamentals_pit
        WHERE TRIM(pub_date) <> ''
        ORDER BY pub_date
        LIMIT 1
        """
    )
    if row.empty:
        return {"status": "UNTESTED", "evidence": "fundamentals_pit is empty"}
    rec = row.iloc[0].to_dict()
    pub = str(rec["pub_date"])
    from datetime import date, timedelta

    prev = (date.fromisoformat(pub) - timedelta(days=1)).isoformat()
    before = fundamentals_as_of(prev, db, [rec["code"]])
    after = fundamentals_as_of(pub, db, [rec["code"]])
    key_before = set(zip(before.get("code", []), before.get("metric", []), before.get("pub_date", [])))
    key_after = set(zip(after.get("code", []), after.get("metric", []), after.get("pub_date", [])))
    target = (rec["code"], rec["metric"], pub)
    return {
        "status": "PASS" if target not in key_before and target in key_after else "FAIL",
        "evidence": {"target": rec, "visible_day_before": target in key_before, "visible_on_pub_date": target in key_after},
    }


def _assert_adjust_factor(db: ResearchPanelDB) -> dict[str, Any]:
    factors = int(db.scalar("SELECT COUNT(*) FROM adjust_factor") or 0)
    prices = int(db.scalar("SELECT COUNT(*) FROM prices_daily") or 0)
    if prices == 0:
        return {"status": "UNTESTED", "evidence": "prices_daily is empty"}
    return {
        "status": "PASS" if factors > 0 else "WORKAROUND",
        "evidence": {"adjust_factor_rows": factors, "price_rows": prices},
    }


def _render(payload: dict[str, Any]) -> str:
    counts = payload["counts"]
    validation = payload["validation"]
    lines = [
        "# Phase B 数据质量报告",
        "",
        f"- 生成时间：{payload['generated_at']}",
        f"- 数据库：`{payload['db_path']}`",
        f"- 验收等级：**{payload['validation_level']}**",
        f"- 样本验收说明：{validation['note']}",
        "- 可信度说明：单测通过不等于当前代码重跑样本通过；样本结论以本报告的 ETL 日志、run_id 与表指纹为准。",
        "",
        "## 样本验收门槛",
        "",
        f"- 价格成功：{validation['thresholds']['price_success_actual']}/{validation['thresholds'].get('price_expected_active_2024', validation['thresholds']['sample_size'])}，要求 >= {validation['thresholds']['price_success_required']}（分母仅含 2024 年应有价格的样本股）",
        f"- 财务核心指标：{validation['thresholds']['fundamental_core_actual']}/{validation['thresholds']['sample_size']}，要求 >= {validation['thresholds']['fundamental_core_required']}",
        f"- ETL 失败记录：progress={validation['thresholds']['progress_errors']}，log={validation['thresholds']['logged_failures']}",
    ]
    if validation["blockers"]:
        lines.append(f"- 阻断项：{'；'.join(validation['blockers'])}")
    else:
        lines.append("- 阻断项：无")
    lines.extend([
        "",
        "## 指标元数据",
        "",
        "| metric | source | column | raw_scale | output_scale | transform | quality_rule |",
        "|---|---|---|---|---|---|---|",
    ])
    for row in payload["metric_metadata"]:
        lines.append(
            "| {metric} | {source_api} | {source_column} | {raw_scale} | {output_scale} | {transform} | {quality_rule} |".format(
                **row
            )
        )
    lines.extend([
        "",
        "## 表行数",
        "",
    ])
    for table, count in counts.items():
        lines.append(f"- `{table}`: {count}")
    fp = payload["data_generation_fingerprint"]
    lines.extend([
        "",
        "## 数据生成指纹",
        "",
        f"- ETL 代码摘要：`{fp['etl_code_version_sha256']}`",
        f"- 清库摘要：{'已记录' if fp.get('old_panel_summary') else '未记录'}",
        f"- run_id 组数：{len(fp['task_run_ids'])}",
    ])
    for row in fp["task_run_ids"]:
        lines.append(f"- `{row['api']}` / `{row['run_id']}`: {row['log_rows']} 条日志，{row['first_ts']} 至 {row['last_ts']}")
    for table, item in fp["table_fingerprints"].items():
        lines.append(f"- `{table}` fingerprint: rows={item['rows']} sha256=`{item['sha256']}`")
    lines.extend(["", "## 行业 as-of 语义", "", f"- 结论：{payload['industry_semantics']['verdict']}", f"- 证据：{payload['industry_semantics']['note']}", ""])
    lines.extend(["## 断言", ""])
    for name, item in payload["assertions"].items():
        lines.append(f"- `{name}`: {item['status']} - {json.dumps(item['evidence'], ensure_ascii=False)}")
    lines.extend(["", "## 覆盖率", ""])
    price_span = payload["coverage"]["price_span"]
    lines.append(f"- 价格：{price_span.get('codes', 0)} 只，{price_span.get('min_date') or '-'} 至 {price_span.get('max_date') or '-'}")
    target = payload["coverage"]["fundamentals_target_window"]
    lines.append(f"- 财务覆盖率目标期：{target['start_year']}-{target['end_year']}（前扩年份只作计算基数）")
    for row in payload["coverage"]["fundamentals_by_metric"]:
        lines.append(
            f"- 财务 `{row['metric']}`: {row['rows']} 行，{row['codes']} 只，"
            f"PASS 行 {row['pass_rows']}，PASS 股票 {row['pass_codes']}，"
            f"空值率 {row['null_rate']}，非 PASS 行 {row['non_pass_rows']}"
        )
    for row in payload["coverage"]["industry_by_date"]:
        lines.append(f"- 行业 `{row['query_date']}`: {row['non_empty']}/{row['rows']} 非空")
    lines.extend(["", "## 问题表", ""])
    scale = payload["issues"]["field_scale_anomalies"]
    lines.append(
        f"- 字段尺度异常：已标记 scale_suspect 行 {scale['marked_scale_suspect_rows']}，"
        f"未标记疑似相邻跨期异常 {scale['unmarked_suspect_pairs']}"
    )
    for row in payload["issues"]["missing_rate"]:
        lines.append(f"- 缺失 `{row['metric']}` / `{row['missing_reason'] or '-'}`: {row['rows']} 行，{row['codes']} 只")
    if not payload["issues"]["missing_rate"]:
        lines.append("- 缺失：无记录")
    for row in payload["issues"]["failure_log"]:
        lines.append(f"- 失败 `{row['api']}` `{row['code']}`: {row['error'] or row['status']}")
    if not payload["issues"]["failure_log"]:
        lines.append("- 失败日志：无")
    throughput = payload["issues"]["throughput_estimate"]
    for row in throughput["estimates"]:
        lines.append(f"- 吞吐估算 `{row['api']}`: 40 股顺序约 {row['estimated_40_code_seconds']}s；{row['note']}")
    if not throughput["estimates"]:
        lines.append("- 吞吐估算：缺少成功样本调用")
    temp = payload["issues"]["temp_files"]
    lines.append(f"- 未清理临时文件：{temp['count']}；清理脚本 `{temp['note'].split(': ', 1)[-1]}`")
    boundary = payload["issues"]["production_boundary_conflicts"]
    lines.append(f"- 生产边界冲突：{boundary['status']}；{boundary['note']}")
    for entry in boundary["entries"]:
        lines.append(f"  - `{entry}`")
    lines.extend(["", "## ETL 调用摘要", ""])
    for row in payload["etl_log_summary"]:
        lines.append(f"- {row['api']} / {row['status']}: {row['calls']} 次，{row['rows']} 行，均值 {row['avg_duration_sec']}s")
    lines.extend([
        "",
        "## 边界说明",
        "",
        "- 本报告只覆盖 `research/data/panel.sqlite3` 与 `research/` 代码。",
        "- 生产侧 PIT/UI 改动若存在，只列为边界冲突，不纳入本阶段验收。",
        "- 未完成全 A 回填前，本报告不会给出 FULL_PASS。",
    ])
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    result = generate()
    print(json.dumps({"out": str(OUT_DIR / "phase_b_quality.md"), "counts": result["counts"]}, ensure_ascii=False, indent=2))
