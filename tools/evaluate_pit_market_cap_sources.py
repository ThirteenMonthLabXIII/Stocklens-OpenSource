from __future__ import annotations

import argparse
import importlib
import importlib.metadata
import json
import os
import sys
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from stock_tool.core.cache import CacheStore
from stock_tool.core.data_location import read_data_dir
from stock_tool.core.pit_database import pit_db_path_for
from stock_tool.core.secrets import SecretStore, TUSHARE_TOKEN_NAME


DEFAULT_SAMPLE_CODE = "600519"
DEFAULT_SAMPLE_DATE = "2024-12-31"


@dataclass(frozen=True)
class SourceProbe:
    source: str
    status: str
    summary: str
    evidence: dict[str, Any]


def _package_status(name: str) -> dict[str, Any]:
    if importlib.util.find_spec(name) is None:
        return {"installed": False, "version": ""}
    try:
        version = importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        version = "unknown"
    return {"installed": True, "version": version}


def _to_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        text = str(value).replace(",", "").replace("%", "").strip()
        if text in {"", "-", "--", "nan", "None"}:
            return None
        return float(text)
    except (TypeError, ValueError):
        return None


def _plain_code(code: str) -> str:
    return "".join(ch for ch in str(code) if ch.isdigit())[-6:].zfill(6)


def _to_ts_code(code: str) -> str:
    plain = _plain_code(code)
    suffix = "SH" if plain.startswith(("6", "9", "5")) else "SZ"
    return f"{plain}.{suffix}"


def _date8(date_text: str) -> str:
    text = str(date_text or "").strip()
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:8]}"
    return text


def _safe_sample(records: list[dict[str, Any]], limit: int = 3) -> list[dict[str, Any]]:
    return [dict(row) for row in records[: max(0, limit)]]


def _find_col(columns: list[str], names: tuple[str, ...]) -> str:
    for name in names:
        if name in columns:
            return name
    return ""


def _resolve_tushare_token() -> tuple[bool, str]:
    if os.getenv("TUSHARE_TOKEN"):
        return True, "env:TUSHARE_TOKEN"
    if os.getenv("TS_TOKEN"):
        return True, "env:TS_TOKEN"
    try:
        token = SecretStore(CacheStore()).get_secret(TUSHARE_TOKEN_NAME)
    except Exception as exc:  # noqa: BLE001 - read-only diagnostics should report, not crash.
        return False, f"SecretStore unavailable: {exc.__class__.__name__}"
    if token:
        return True, "SecretStore:tushare_token"
    return False, "missing"


def evaluate_environment() -> dict[str, Any]:
    has_token, token_source = _resolve_tushare_token()
    return {
        "packages": {
            "tushare": _package_status("tushare"),
            "akshare": _package_status("akshare"),
            "baostock": _package_status("baostock"),
            "pandas": _package_status("pandas"),
        },
        "tushare_token_present": has_token,
        "tushare_token_source": token_source,
    }


def probe_tushare_daily_basic(sample_code: str, sample_date: str) -> SourceProbe:
    env = evaluate_environment()
    package = env["packages"]["tushare"]
    if not package["installed"]:
        return SourceProbe(
            "tushare.daily_basic",
            "unavailable",
            "tushare package is not installed; historical market_cap_yi cannot be verified or backfilled.",
            {"package": package, "token_present": env["tushare_token_present"]},
        )
    if not env["tushare_token_present"]:
        return SourceProbe(
            "tushare.daily_basic",
            "unavailable",
            "Tushare token is not configured; historical daily_basic(total_mv/circ_mv) probe was not run.",
            {"package": package, "token_source": env["tushare_token_source"]},
        )

    import tushare as ts

    token = os.getenv("TUSHARE_TOKEN") or os.getenv("TS_TOKEN") or SecretStore(CacheStore()).get_secret(TUSHARE_TOKEN_NAME)
    ts.set_token(token)
    pro = ts.pro_api(token)
    ts_code = _to_ts_code(sample_code)
    date_text = sample_date.replace("-", "")
    try:
        df = pro.daily_basic(
            ts_code=ts_code,
            start_date=date_text,
            end_date=date_text,
            fields="ts_code,trade_date,total_mv,circ_mv,close",
        )
    except Exception as exc:  # noqa: BLE001 - live probe evidence
        return SourceProbe(
            "tushare.daily_basic",
            "failed",
            "Tushare daily_basic live probe failed; no schema change should be made from this run.",
            {"error_class": exc.__class__.__name__, "message": str(exc), "ts_code": ts_code, "trade_date": sample_date},
        )
    records = df.to_dict(orient="records") if df is not None else []
    if not records:
        return SourceProbe(
            "tushare.daily_basic",
            "no_data",
            "Tushare daily_basic returned no rows for the sample; source remains unproven.",
            {"ts_code": ts_code, "trade_date": sample_date},
        )

    row = records[0]
    total_mv = _to_float(row.get("total_mv"))
    circ_mv = _to_float(row.get("circ_mv"))
    evidence = {
        "ts_code": ts_code,
        "trade_date": _date8(row.get("trade_date", sample_date)),
        "raw_total_mv": total_mv,
        "raw_circ_mv": circ_mv,
        "candidate_market_cap_yi_if_total_mv_is_wanyuan": round(total_mv / 10000, 4) if total_mv is not None else None,
        "candidate_float_market_cap_yi_if_circ_mv_is_wanyuan": round(circ_mv / 10000, 4) if circ_mv is not None else None,
        "raw_sample": row,
    }
    return SourceProbe(
        "tushare.daily_basic",
        "probe_ok_unit_inferred",
        "daily_basic returned total_mv/circ_mv. Unit conversion still requires explicit acceptance before production backfill.",
        evidence,
    )


def probe_akshare_spot(sample_codes: list[str]) -> SourceProbe:
    package = _package_status("akshare")
    if not package["installed"]:
        return SourceProbe(
            "akshare.stock_zh_a_spot_em",
            "unavailable",
            "akshare package is not installed; current spot market cap smoke was not run.",
            {"package": package},
        )
    try:
        import akshare as ak

        df = ak.stock_zh_a_spot_em()
    except Exception as exc:  # noqa: BLE001 - live probe evidence
        return SourceProbe(
            "akshare.stock_zh_a_spot_em",
            "failed",
            "akshare current spot smoke failed; this does not affect offline PIT data.",
            {"error_class": exc.__class__.__name__, "message": str(exc)},
        )
    records = df.to_dict(orient="records") if df is not None else []
    columns = list(df.columns) if df is not None else []
    code_col = _find_col(columns, ("代码", "code", "symbol"))
    name_col = _find_col(columns, ("名称", "name"))
    price_col = _find_col(columns, ("最新价", "price", "close"))
    total_col = _find_col(columns, ("总市值", "market_cap", "总市值-元"))
    float_col = _find_col(columns, ("流通市值", "float_market_cap", "流通市值-元"))
    sample_set = {_plain_code(code) for code in sample_codes}
    matched = []
    for row in records:
        code = _plain_code(row.get(code_col, "")) if code_col else ""
        if code in sample_set:
            total = _to_float(row.get(total_col)) if total_col else None
            floating = _to_float(row.get(float_col)) if float_col else None
            matched.append(
                {
                    "code": code,
                    "name": row.get(name_col, "") if name_col else "",
                    "price": _to_float(row.get(price_col)) if price_col else None,
                    "raw_total_market_cap": total,
                    "market_cap_yi_if_raw_is_yuan": round(total / 100000000, 4) if total is not None else None,
                    "float_market_cap_yi_if_raw_is_yuan": round(floating / 100000000, 4) if floating is not None else None,
                }
            )
    positive_total = 0
    for row in records:
        value = _to_float(row.get(total_col)) if total_col else None
        if value is not None and value > 0:
            positive_total += 1
    status = "probe_ok_current_only" if total_col and records else "missing_columns"
    summary = (
        "akshare spot returned current total market cap values; usable only as current smoke/fallback, not historical PIT backfill."
        if status == "probe_ok_current_only"
        else "akshare spot did not expose the expected market cap columns."
    )
    return SourceProbe(
        "akshare.stock_zh_a_spot_em",
        status,
        summary,
        {
            "package": package,
            "row_count": len(records),
            "columns": columns,
            "detected_columns": {
                "code": code_col,
                "name": name_col,
                "price": price_col,
                "total_market_cap": total_col,
                "float_market_cap": float_col,
            },
            "positive_total_market_cap_rows": positive_total,
            "sample_matches": matched,
            "sample_rows": _safe_sample(records, 2),
        },
    )


def probe_baostock_basic(sample_code: str) -> SourceProbe:
    package = _package_status("baostock")
    if not package["installed"]:
        return SourceProbe(
            "baostock.query_stock_basic",
            "unavailable",
            "baostock package is not installed; stock_basic fallback was not probed.",
            {"package": package},
        )
    try:
        import baostock as bs

        login = bs.login()
        if getattr(login, "error_code", "1") != "0":
            return SourceProbe(
                "baostock.query_stock_basic",
                "failed",
                "baostock login failed; stock_basic fallback remains unproven.",
                {"error_code": getattr(login, "error_code", ""), "error_msg": getattr(login, "error_msg", "")},
            )
        code = _to_ts_code(sample_code).replace(".SH", ".sh").replace(".SZ", ".sz")
        left, right = code.split(".")
        bs_code = f"{right}.{left}" if left.isdigit() else code
        if "." in bs_code and bs_code.split(".")[0].isdigit():
            digits, market = bs_code.split(".")
            bs_code = f"{market}.{digits}"
        rs = bs.query_stock_basic(code=bs_code)
        rows = []
        fields = [str(field) for field in getattr(rs, "fields", [])]
        while getattr(rs, "error_code", "0") == "0" and rs.next():
            rows.append(dict(zip(fields, rs.get_row_data())))
    except Exception as exc:  # noqa: BLE001 - live probe evidence
        return SourceProbe(
            "baostock.query_stock_basic",
            "failed",
            "baostock stock_basic probe failed; fallback remains unproven.",
            {"error_class": exc.__class__.__name__, "message": str(exc), "sample_code": sample_code},
        )
    finally:
        try:
            bs.logout()  # type: ignore[name-defined]
        except Exception:
            pass

    share_like_fields = [field for field in fields if any(key in field.lower() for key in ("share", "capital", "float", "total"))]
    status = "no_share_capital_fields" if not share_like_fields else "probe_ok_needs_unit_check"
    summary = (
        "query_stock_basic returned no obvious share-capital fields, so baostock stock_basic is not enough for market_cap_yi fallback."
        if status == "no_share_capital_fields"
        else "query_stock_basic exposes share-like fields; units and effective-date semantics still need a separate probe."
    )
    return SourceProbe(
        "baostock.query_stock_basic",
        status,
        summary,
        {"package": package, "fields": fields, "share_like_fields": share_like_fields, "rows": rows[:3]},
    )


def _default_output_dir() -> Path:
    data_dir = read_data_dir() or (ROOT / "my_data")
    return data_dir / "pit_reports"


def write_reports(payload: dict[str, Any], output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = output_dir / f"pit_market_cap_source_eval_{stamp}.json"
    md_path = output_dir / f"pit_market_cap_source_eval_{stamp}.md"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        "# PIT market_cap_yi Source Evaluation",
        "",
        f"- generated_at: {payload['generated_at']}",
        f"- pit_db: {payload['pit_db']}",
        f"- sample_code: {payload['sample_code']}",
        f"- sample_date: {payload['sample_date']}",
        "",
        "## Conclusion",
        "",
        payload["conclusion"],
        "",
        "## Probes",
        "",
    ]
    for probe in payload["probes"]:
        lines.extend(
            [
                f"### {probe['source']}",
                "",
                f"- status: `{probe['status']}`",
                f"- summary: {probe['summary']}",
                "",
            ]
        )
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, md_path


def build_conclusion(probes: list[SourceProbe]) -> str:
    by_source = {probe.source: probe for probe in probes}
    tushare = by_source.get("tushare.daily_basic")
    if tushare and tushare.status.startswith("probe_ok"):
        return (
            "Tushare daily_basic is the only candidate in this run with historical market value evidence. "
            "A production table may be implemented only after accepting the observed unit conversion and adding tests."
        )
    return (
        "market_cap_yi remains blocked for PIT historical use in this run: Tushare historical daily_basic was not proven. "
        "akshare spot, if available, is current-only evidence and must not be used for historical PIT backfill."
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only PIT market_cap_yi source evaluation.")
    parser.add_argument("--sample-code", default=DEFAULT_SAMPLE_CODE)
    parser.add_argument("--sample-date", default=DEFAULT_SAMPLE_DATE)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--skip-akshare", action="store_true", help="Skip current akshare spot smoke.")
    parser.add_argument("--skip-baostock", action="store_true", help="Skip baostock stock_basic smoke.")
    args = parser.parse_args(argv)

    probes = [probe_tushare_daily_basic(args.sample_code, args.sample_date)]
    if not args.skip_akshare:
        probes.append(probe_akshare_spot([args.sample_code, "000001", "600000"]))
    if not args.skip_baostock:
        probes.append(probe_baostock_basic(args.sample_code))

    pit_db = pit_db_path_for(read_data_dir() or (ROOT / "my_data"))
    payload = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "pit_db": str(pit_db),
        "sample_code": _plain_code(args.sample_code),
        "sample_date": args.sample_date,
        "environment": evaluate_environment(),
        "probes": [asdict(probe) for probe in probes],
        "conclusion": build_conclusion(probes),
    }
    json_path, md_path = write_reports(payload, args.output_dir or _default_output_dir())
    print(f"[OK] wrote {json_path}")
    print(f"[OK] wrote {md_path}")
    print(payload["conclusion"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
