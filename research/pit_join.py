"""Point-in-time as-of assembly for the research panel database."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Sequence

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research.panel_db import ResearchPanelDB, open_panel


def universe_as_of(asof_date: str, db: ResearchPanelDB | None = None) -> pd.DataFrame:
    own = db is None
    db = db or open_panel()
    try:
        return db.query_df(
            """
            SELECT code, code_name, ipo_date, out_date, type, status
            FROM universe
            WHERE ipo_date <= ?
              AND (TRIM(out_date) = '' OR out_date > ?)
            ORDER BY code
            """,
            (asof_date, asof_date),
        )
    finally:
        if own:
            db.close()


def fundamentals_as_of(
    asof_date: str,
    db: ResearchPanelDB | None = None,
    codes: Sequence[str] | None = None,
) -> pd.DataFrame:
    own = db is None
    db = db or open_panel()
    try:
        code_filter, params = _code_filter("code", codes)
        sql = f"""
        WITH ranked AS (
            SELECT code, metric, value, stat_date, pub_date, metric_quality, missing_reason,
                   ROW_NUMBER() OVER (
                       PARTITION BY code, metric
                       ORDER BY pub_date DESC, stat_date DESC
                   ) AS rn
            FROM fundamentals_pit
            WHERE pub_date <= ?
              AND stat_date <= ?
              {code_filter}
        )
        SELECT code, metric, value, stat_date, pub_date, metric_quality, missing_reason
        FROM ranked
        WHERE rn = 1
        ORDER BY code, metric
        """
        return db.query_df(sql, (asof_date, asof_date, *params))
    finally:
        if own:
            db.close()


def industry_as_of(
    asof_date: str,
    db: ResearchPanelDB | None = None,
    codes: Sequence[str] | None = None,
) -> pd.DataFrame:
    own = db is None
    db = db or open_panel()
    try:
        code_filter, params = _code_filter("code", codes)
        sql = f"""
        WITH ranked AS (
            SELECT code, industry, industry_classification, update_date, query_date,
                   ROW_NUMBER() OVER (
                       PARTITION BY code
                       ORDER BY query_date DESC, update_date DESC
                   ) AS rn
            FROM industry_map
            WHERE query_date <= ?
              {code_filter}
        )
        SELECT code, industry, industry_classification, update_date, query_date
        FROM ranked
        WHERE rn = 1
        ORDER BY code
        """
        return db.query_df(sql, (asof_date, *params))
    finally:
        if own:
            db.close()


def prices_on(
    asof_date: str,
    db: ResearchPanelDB | None = None,
    codes: Sequence[str] | None = None,
) -> pd.DataFrame:
    own = db is None
    db = db or open_panel()
    try:
        code_filter, params = _code_filter("code", codes)
        return db.query_df(
            f"""
            SELECT *
            FROM prices_daily
            WHERE date = ?
              {code_filter}
            ORDER BY code
            """,
            (asof_date, *params),
        )
    finally:
        if own:
            db.close()


def as_of(asof_date: str, db: ResearchPanelDB | None = None) -> pd.DataFrame:
    """Return one row per active code with latest visible fundamentals/industry."""
    own = db is None
    db = db or open_panel()
    try:
        universe = universe_as_of(asof_date, db)
        if universe.empty:
            return universe
        codes = universe["code"].tolist()
        out = universe.copy()
        industry = industry_as_of(asof_date, db, codes)
        if not industry.empty:
            out = out.merge(industry, on="code", how="left")
        prices = prices_on(asof_date, db, codes)
        if not prices.empty:
            out = out.merge(prices.drop(columns=["fetched_at"], errors="ignore"), on="code", how="left")
        fundamentals = fundamentals_as_of(asof_date, db, codes)
        if not fundamentals.empty:
            wide = fundamentals.pivot(index="code", columns="metric", values="value").reset_index()
            wide.columns = [str(c) for c in wide.columns]
            out = out.merge(wide, on="code", how="left")
            quality = fundamentals.pivot(index="code", columns="metric", values="metric_quality").reset_index()
            quality.columns = [
                "code" if c == "code" else f"{c}_quality"
                for c in [str(col) for col in quality.columns]
            ]
            out = out.merge(quality, on="code", how="left")
            missing = fundamentals.pivot(index="code", columns="metric", values="missing_reason").reset_index()
            missing.columns = [
                "code" if c == "code" else f"{c}_missing_reason"
                for c in [str(col) for col in missing.columns]
            ]
            out = out.merge(missing, on="code", how="left")
        return out
    finally:
        if own:
            db.close()


def _code_filter(column: str, codes: Sequence[str] | None) -> tuple[str, tuple[str, ...]]:
    if not codes:
        return "", ()
    clean = tuple(str(c) for c in codes)
    placeholders = ",".join("?" for _ in clean)
    return f"AND {column} IN ({placeholders})", clean
