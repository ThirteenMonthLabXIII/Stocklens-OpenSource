"""Optional oracle crosscheck for stationary bootstrap CI using arch.

This script is not a project dependency. If ``arch`` is unavailable it writes a
SKIPPED artifact instead of pretending the oracle ran.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research.metrics_robustness import stationary_bootstrap_ci
from research.panel_db import OUT_DIR


def main() -> int:
    out = OUT_DIR / "arch_bootstrap_crosscheck.json"
    try:
        from arch.bootstrap import StationaryBootstrap
    except Exception as exc:  # noqa: BLE001
        payload = _payload("SKIPPED", {"reason": f"arch unavailable: {type(exc).__name__}: {exc}"})
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"status": payload["status"], "reason": payload["details"]["reason"]}, ensure_ascii=False))
        return 0

    returns = pd.Series([0.02, -0.01, 0.01, 0.00, 0.03, -0.02] * 8, dtype="float64")
    native = stationary_bootstrap_ci(returns, lambda s: float(s.mean()), n_boot=1000, expected_block=4, seed=17)
    bs = StationaryBootstrap(4, returns.to_numpy(), seed=17)
    arch_ci = bs.conf_int(lambda x: x.mean(), reps=1000, method="percentile", size=0.95)
    lower = float(arch_ci[0][0])
    upper = float(arch_ci[1][0])
    max_diff = max(abs(native["lower"] - lower), abs(native["upper"] - upper))
    tolerance = 0.05
    payload = _payload(
        "PASS" if max_diff <= tolerance else "FAIL",
        {
            "native": native,
            "arch": {"lower": lower, "upper": upper},
            "max_abs_diff": max_diff,
            "tolerance": tolerance,
        },
    )
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"status": payload["status"], "max_abs_diff": max_diff}, ensure_ascii=False))
    return 0 if payload["status"] == "PASS" else 1


def _payload(status: str, details: dict) -> dict:
    return {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "status": status,
        "confirmatory": False,
        "details": details,
    }


if __name__ == "__main__":
    raise SystemExit(main())
