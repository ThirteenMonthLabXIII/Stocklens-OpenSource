from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research._testkit import raises, run_tests
from research.experiment_identity import data_generation_fingerprint
from research.panel_db import OUT_DIR, ResearchPanelDB
from research.v2_experiment import (
    TRAIN_VALID_MAX_DATE,
    V2DiagnosticConfig,
    append_trial_log,
    assert_forward_confirmation_ready,
    assert_training_window,
    assert_within_training_boundary,
    load_v2_registry,
    make_v2_result_payload,
    score_v2_cross_section,
    unique_trial_count,
)

TEST_LOG = OUT_DIR / "v2_trial_log_selftest.jsonl"
TEST_DB = Path(__file__).resolve().parent / "data" / "v2_fingerprint_selftest.sqlite3"


def test_training_boundary_rejects_future_asof():
    assert_within_training_boundary(TRAIN_VALID_MAX_DATE)
    with raises(AssertionError):
        assert_within_training_boundary("2026-06-17")
    with raises(AssertionError):
        assert_training_window("2019-01-01", "2026-06-17")


def test_training_payload_is_non_confirmatory_and_has_no_adoption_keys():
    payload = make_v2_result_payload(
        V2DiagnosticConfig(mode="train", start_date="2019-01-01", end_date="2026-06-16"),
        metrics={"mean": 0.01},
        period_rows=[{"rebalance_date": "2019-01-02"}],
    )
    assert payload["confirmatory"] is False
    assert payload["status"] == "TRAINING_DIAGNOSTIC_NOT_CONFIRMATION"
    assert payload["claim_scope"] == "diagnostic_only_no_adoption_or_production_claims"
    forbidden = {"winner", "accepted", "adopted", "production_ready", "confirmatory_winner"}
    assert forbidden.isdisjoint(payload.keys())


def test_experiment_identity_binds_data_content_and_trial_log():
    for suffix in ("", "-wal", "-shm"):
        path = Path(f"{TEST_DB}{suffix}")
        if path.exists():
            path.unlink()
    db = ResearchPanelDB(TEST_DB)
    try:
        db.init_schema()
        db.upsert(
            "universe",
            [{"code": "000001", "code_name": "fixture", "ipo_date": "2010-01-01", "out_date": None,
              "type": "A", "status": "normal", "fetched_at": "2026-01-01T00:00:00+08:00"}],
            ["code"],
        )
        first = data_generation_fingerprint(db)
        db.upsert(
            "universe",
            [{"code": "000001", "code_name": "fixture_changed", "ipo_date": "2010-01-01", "out_date": None,
              "type": "A", "status": "normal", "fetched_at": "2026-01-01T00:00:00+08:00"}],
            ["code"],
        )
        second = data_generation_fingerprint(db)
        assert first["table_fingerprints"]["universe"] != second["table_fingerprints"]["universe"]

        payload = make_v2_result_payload(
            V2DiagnosticConfig(mode="train", start_date="2019-01-01", end_date="2026-06-16"),
            data_fingerprint=first,
        )
        assert payload["data_fingerprint"] == first
        assert payload["data_fingerprint_hash"]
        append_trial_log(payload, TEST_LOG)
        entry = json.loads(TEST_LOG.read_text(encoding="utf-8").splitlines()[-1])
        assert entry["data_fingerprint_hash"] == payload["data_fingerprint_hash"]
        assert entry["data_fingerprint"]["table_fingerprints"] == first["table_fingerprints"]
    finally:
        db.close()
        if TEST_LOG.exists():
            TEST_LOG.unlink()
        for suffix in ("", "-wal", "-shm"):
            path = Path(f"{TEST_DB}{suffix}")
            if path.exists():
                path.unlink()


def test_forward_confirmation_requires_post_boundary_and_ledger():
    with raises(AssertionError):
        assert_forward_confirmation_ready("2026-06-16", ledger_path=Path("missing_forward_ledger.json"))
    with raises(RuntimeError):
        assert_forward_confirmation_ready("2026-06-17", ledger_path=Path("missing_forward_ledger.json"))


def test_trial_log_appends_and_counts_unique_config_hashes():
    if TEST_LOG.exists():
        TEST_LOG.unlink()
    payload = make_v2_result_payload(
        V2DiagnosticConfig(mode="validate", start_date="2024-01-01", end_date="2024-12-31"),
        metrics={"mean": 0.01},
    )
    try:
        append_trial_log(payload, TEST_LOG)
        append_trial_log(payload, TEST_LOG)
        assert TEST_LOG.exists()
        assert len(TEST_LOG.read_text(encoding="utf-8").splitlines()) == 2
        assert unique_trial_count(TEST_LOG) == 1
    finally:
        if TEST_LOG.exists():
            TEST_LOG.unlink()


def test_v2_scoring_uses_preregistered_components():
    registry = load_v2_registry()
    panel = pd.DataFrame(
        [
            {
                "code": "A",
                "industry": "i",
                "pb_mrq": 1,
                "ps_ttm": 1,
                "volatility_20d": 0.01,
                "turnover_20d": 0.5,
                "momentum_60d": -0.2,
                "momentum_120d": -0.1,
                "liquidity_amount_20d": 10,
            },
            {
                "code": "B",
                "industry": "i",
                "pb_mrq": 5,
                "ps_ttm": 5,
                "volatility_20d": 0.10,
                "turnover_20d": 5.0,
                "momentum_60d": 0.2,
                "momentum_120d": 0.1,
                "liquidity_amount_20d": 1000,
            },
        ]
    )
    scored = score_v2_cross_section(panel, registry).set_index("code")
    assert scored.loc["A", "score"] > scored.loc["B", "score"]


if __name__ == "__main__":
    raise SystemExit(run_tests(globals()))
