"""Optional oracle crosscheck for CPCV purge semantics using skfolio.

This script is not a project dependency. If ``skfolio`` is unavailable it writes
a SKIPPED artifact instead of pretending the oracle ran.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import importlib.metadata

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research.metrics_robustness import cscv_train_test_indices
from research.panel_db import OUT_DIR


def main() -> int:
    out = OUT_DIR / "skfolio_cpcv_crosscheck.json"
    try:
        from skfolio.model_selection import CombinatorialPurgedCV
    except Exception as exc:  # noqa: BLE001
        payload = _payload("SKIPPED", {"reason": f"skfolio unavailable: {type(exc).__name__}: {exc}"})
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"status": payload["status"], "reason": payload["details"]["reason"]}, ensure_ascii=False))
        return 0

    X = pd.DataFrame({"x": range(12)})
    cv = CombinatorialPurgedCV(n_folds=4, n_test_folds=2, purged_size=1, embargo_size=0)
    skfolio_splits = [_normalize_split(train_idx, test_idx) for train_idx, test_idx in cv.split(X)]
    native_splits = [
        (tuple(train_idx), tuple(test_idx))
        for train_idx, test_idx in cscv_train_test_indices(n_periods=len(X), n_splits=4, embargo=1)
    ]
    skfolio_sorted = sorted(skfolio_splits)
    native_sorted = sorted(native_splits)
    status = "PASS" if skfolio_sorted == native_sorted else "FAIL"
    payload = _payload(
        status,
        {
            "skfolio_version": _package_version("skfolio"),
            "oracle": "CombinatorialPurgedCV(n_folds=4, n_test_folds=2, purged_size=1, embargo_size=0).split(X)",
            "native": {
                "helper": "cscv_train_test_indices(n_periods=12, n_splits=4, embargo=1)",
                "splits": _jsonable(native_sorted),
            },
            "skfolio": {"splits": _jsonable(skfolio_sorted)},
            "difference_summary": _difference_summary(native_sorted, skfolio_sorted),
        },
    )
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"status": payload["status"], "splits": len(native_sorted)}, ensure_ascii=False))
    return 0 if status == "PASS" else 1


def _normalize_split(train_idx: object, test_idx: object) -> tuple[tuple[int, ...], tuple[int, ...]]:
    train = tuple(sorted(int(i) for i in train_idx))
    if isinstance(test_idx, (list, tuple)):
        flat_test = [int(i) for part in test_idx for i in part]
    else:
        flat_test = [int(i) for i in test_idx]
    return train, tuple(sorted(flat_test))


def _difference_summary(
    native: list[tuple[tuple[int, ...], tuple[int, ...]]],
    skfolio: list[tuple[tuple[int, ...], tuple[int, ...]]],
) -> dict:
    native_set = set(native)
    skfolio_set = set(skfolio)
    return {
        "native_count": len(native),
        "skfolio_count": len(skfolio),
        "missing_from_skfolio": _jsonable(sorted(native_set - skfolio_set)[:3]),
        "unexpected_from_skfolio": _jsonable(sorted(skfolio_set - native_set)[:3]),
    }


def _jsonable(splits: list[tuple[tuple[int, ...], tuple[int, ...]]]) -> list[dict]:
    return [{"train_idx": list(train), "test_idx": list(test)} for train, test in splits]


def _package_version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def _payload(status: str, details: dict) -> dict:
    return {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "status": status,
        "confirmatory": False,
        "details": details,
    }


if __name__ == "__main__":
    raise SystemExit(main())
