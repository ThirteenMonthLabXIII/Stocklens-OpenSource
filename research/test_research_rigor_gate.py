"""Acceptance for the independent research-rigor gate."""

from __future__ import annotations

from pathlib import Path

from research.rigor_gate import evaluate_static_contract


def test_static_rigor_contract() -> None:
    report = evaluate_static_contract(Path(__file__).resolve().parents[1])
    assert report.passed, report.as_dict()


if __name__ == "__main__":
    test_static_rigor_contract()
    print("[OK] research rigor gate tests passed")
