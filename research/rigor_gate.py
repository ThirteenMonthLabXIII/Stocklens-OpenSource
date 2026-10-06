"""Independent research-rigor gate used by offline acceptance.

The normal application test suite answers whether code paths run.  This gate
answers whether a result is allowed to be described as formal research.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class GateResult:
    name: str
    passed: bool
    message: str


@dataclass(frozen=True)
class ResearchRigorReport:
    gates: tuple[GateResult, ...]

    @property
    def passed(self) -> bool:
        return all(gate.passed for gate in self.gates)

    def as_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "gates": [
                {"name": gate.name, "passed": gate.passed, "message": gate.message}
                for gate in self.gates
            ],
        }


def evaluate_static_contract(root: Path) -> ResearchRigorReport:
    """Evaluate the independent research gates without network or live data.

    This deliberately exercises the small contract seams as well as checking
    source topology.  The ordinary quick suite can therefore stay focused on
    application regressions while this command answers whether formal-research
    claims are still defensible.
    """

    qt_path = root / "stock_tool" / "ui" / "qt_workbench.py"
    source = qt_path.read_text(encoding="utf-8")
    factor_source = (root / "stock_tool" / "core" / "factor_store.py").read_text(encoding="utf-8")
    certificate_source = (root / "stock_tool" / "core" / "formal_certificate.py").read_text(encoding="utf-8")
    event_source = (root / "research" / "event_backtest.py").read_text(encoding="utf-8")
    ledger_source = (root / "research" / "execution_ledger.py").read_text(encoding="utf-8")

    gates = [
        GateResult(
            "AI Research Boundary Gate",
            "from stock_tool.core.trade_ai import" not in source
            and "import stock_tool.core.trade_ai" not in source
            and "ResearchDiscussionService" in source,
            "Qt production source must depend on ResearchDiscussionService only.",
        ),
        GateResult(
            "Fact Quality Gate",
            all(token in factor_source for token in ("fact_total", "verified_fact_count", "partial_fact_count", "unverified_fact_count", "quality_domains")),
            "Fact-level quality counts must be aggregated before a manifest can be trusted.",
        ),
        GateResult(
            "Formal Certificate Gate",
            "formal_screening_certificates" in (root / "stock_tool" / "core" / "v1_database.py").read_text(encoding="utf-8")
            and "validate_formal_observation_sample_insert" in (root / "stock_tool" / "core" / "v1_database.py").read_text(encoding="utf-8")
            and "partial_fact_count" in certificate_source,
            "Formal samples require a persisted certificate and consistent fact quality.",
        ),
        GateResult(
            "PIT/Universe Gate",
            all(token in certificate_source for token in ("UniverseDefinition", "universe_version", "members_digest"))
            and "available_at" in factor_source
            and "formal_mode" in factor_source,
            "Formal identity must bind a versioned universe and real available-at evidence.",
        ),
        GateResult(
            "Execution Ledger Gate",
            all(token in ledger_source for token in ("OrderIntent", "OrderFill", "CashLedgerEntry", "DailyEquity", "t_plus_one"))
            and "trade_ledger_v1" in event_source,
            "Formal event results must be backed by trade-level ledger records.",
        ),
        GateResult(
            "Research Robustness Gate",
            all(
                (root / relative).exists()
                for relative in (
                    "research/experiment_identity.py",
                    "research/test_experiment_manifest.py",
                    "research/test_event_backtest.py",
                    "research/test_execution_ledger.py",
                )
            ),
            "Experiment identity, event semantics and execution-ledger fixtures must be present.",
        ),
    ]
    dynamic = _run_dynamic_checks()
    gates.extend(dynamic)
    return ResearchRigorReport(gates=tuple(gates))


def _run_dynamic_checks() -> tuple[GateResult, ...]:
    """Run deterministic contract probes used by the independent gate."""

    try:
        from stock_tool.core.factor_store import FactorSnapshot, build_factor_snapshot_manifest
        from stock_tool.core.formal_certificate import (
            FormalObservationBlocked,
            FormalScreeningCertificate,
            UniverseDefinition,
        )

        partial = FactorSnapshot(
            code="000001",
            name="gate",
            roe=None,
            data_date="2026-07-23",
            quote_updated_at="2026-07-24T09:00:00+00:00",
            updated_at="2026-07-24T09:00:00+00:00",
            missing_fields="roe",
        )
        manifest, _facts = build_factor_snapshot_manifest(
            [partial],
            quote_updated_at=None,
            total=1,
            failed=0,
            limit=1,
        )
        quality_ok = manifest.quality_status == "partial"
        try:
            build_factor_snapshot_manifest(
                [partial],
                quote_updated_at=None,
                total=1,
                failed=0,
                limit=1,
                formal_mode=True,
            )
        except ValueError:
            pit_fallback_blocked = True
        else:
            pit_fallback_blocked = False

        universe = UniverseDefinition(
            universe_definition_id="a_share_listed_pit",
            universe_version="gate-v1",
            members_digest="members-v1",
            selection_scope="gate",
            selection_policy_version="v1",
            selected_limit=1,
        )
        try:
            FormalScreeningCertificate.issue(
                run_id=1,
                data_snapshot_id="snapshot-1",
                refresh_event_id="refresh-1",
                universe=universe,
                selection_scope="gate",
                formula_version="formula-v1",
                execution_mode="formal",
                quality_status="verified",
                quality_summary={"fact_total": 2, "verified_fact_count": 1, "partial_fact_count": 1},
                condition_hash="condition-v1",
                result_digest="result-v1",
            )
        except FormalObservationBlocked:
            certificate_consistency_blocked = True
        else:
            certificate_consistency_blocked = False
        quality_gate = quality_ok and certificate_consistency_blocked
        pit_gate = pit_fallback_blocked
    except Exception as exc:
        quality_gate = False
        pit_gate = False
        quality_error = str(exc)
    else:
        quality_error = ""

    try:
        from research.event_backtest import run_ledger_backtest
        from research.execution_ledger import ExecutionPolicy, OrderIntent

        ledger = run_ledger_backtest(
            {
                "2026-07-24": [
                    OrderIntent("2026-07-23", "2026-07-23", "2026-07-24", "000001", "buy", 100),
                    OrderIntent("2026-07-24", "2026-07-24", "2026-07-24", "000001", "sell", 100),
                ],
                "2026-07-25": [
                    OrderIntent("2026-07-25", "2026-07-25", "2026-07-25", "000001", "sell", 100),
                ],
            },
            {
                "2026-07-24": {"000001": {"open": 10, "close": 10, "trade_status": "normal"}},
                "2026-07-25": {"000001": {"open": 11, "close": 11, "trade_status": "normal"}},
            },
            policy=ExecutionPolicy(initial_cash=100_000, max_position_weight=1.0),
        )
        ledger_gate = (
            ledger.fills[1].untradeable_reason == "t_plus_one"
            and all(abs(item.total_equity - item.cash - item.holdings_value) < 1e-6 for item in ledger.daily_equity)
        )
        ledger_error = ""
    except Exception as exc:
        ledger_gate = False
        ledger_error = str(exc)

    return (
        GateResult(
            "Fact/Credential Consistency Probe",
            quality_gate,
            "partial facts must not issue a verified certificate."
            + (f" ({quality_error})" if quality_error else ""),
        ),
        GateResult(
            "PIT Fallback Probe",
            pit_gate,
            "formal manifest must reject updated_at/data_date as available_at."
            + (f" ({quality_error})" if quality_error else ""),
        ),
        GateResult(
            "Ledger Invariant Probe",
            ledger_gate,
            "T+1 and daily cash-plus-holdings identity must hold."
            + (f" ({ledger_error})" if ledger_error else ""),
        ),
    )
