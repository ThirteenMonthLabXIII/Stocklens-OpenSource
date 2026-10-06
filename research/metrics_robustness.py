"""Small, auditable robustness metrics for Phase C."""

from __future__ import annotations

import itertools
import math
import random
from statistics import NormalDist

import pandas as pd

EULER_GAMMA = 0.5772156649015329


def return_summary(returns: pd.Series, *, periods_per_year: int = 12) -> dict:
    r = pd.to_numeric(returns, errors="coerce").dropna()
    if r.empty:
        return {
            "periods": 0,
            "mean": None,
            "period_sharpe": None,
            "sharpe": None,
            "max_drawdown": None,
            "win_rate": None,
        }
    mean = float(r.mean())
    sd = float(r.std(ddof=1)) if len(r) > 1 else 0.0
    period_sr = period_sharpe_ratio(r)
    sharpe = None if period_sr is None else period_sr * math.sqrt(periods_per_year)
    equity = (1.0 + r).cumprod()
    drawdown = equity / equity.cummax() - 1.0
    return {
        "periods": int(len(r)),
        "mean": mean,
        "period_sharpe": period_sr,
        "sharpe": sharpe,
        "max_drawdown": float(drawdown.min()),
        "win_rate": float((r > 0).mean()),
        "stdev": sd,
    }


def period_sharpe_ratio(returns: pd.Series) -> float | None:
    """Sharpe ratio on the native return period, with no annualization."""
    r = pd.to_numeric(returns, errors="coerce").dropna()
    if len(r) < 2:
        return None
    sd = float(r.std(ddof=1))
    if sd == 0:
        return None
    return float(r.mean()) / sd


def probabilistic_sharpe_ratio(
    returns: pd.Series,
    *,
    benchmark_sharpe: float = 0.0,
    periods_per_year: int = 12,
) -> float | None:
    r = pd.to_numeric(returns, errors="coerce").dropna()
    if len(r) < 3:
        return None
    sd = float(r.std(ddof=1))
    if sd == 0:
        mean = float(r.mean())
        if mean > benchmark_sharpe:
            return 1.0
        if mean < benchmark_sharpe:
            return 0.0
        return 0.5
    sr = float(r.mean()) / sd
    skew = float(r.skew())
    kurt = float(r.kurtosis() + 3.0)
    denom = math.sqrt(max(1e-12, 1.0 - skew * sr + (kurt - 1.0) / 4.0 * sr * sr))
    z = (sr - benchmark_sharpe) * math.sqrt(len(r) - 1.0) / denom
    return NormalDist().cdf(z)


def expected_max_sharpe(sharpe_estimates: pd.Series | list[float], *, trials: int | None = None) -> float:
    """Expected maximum Sharpe hurdle from a pre-registered candidate set.

    Uses the Bailey/Lopez de Prado approximation based on the observed
    dispersion of candidate Sharpe estimates. A single trial has no multiple
    testing hurdle, so the result is zero.
    """
    estimates = pd.to_numeric(pd.Series(sharpe_estimates), errors="coerce").dropna().astype(float)
    n_trials = int(trials or len(estimates) or 1)
    if n_trials <= 1:
        return 0.0
    mean = float(estimates.mean()) if not estimates.empty else 0.0
    if len(estimates) >= 2:
        sigma = float(estimates.std(ddof=1))
    else:
        sigma = 0.0
    if sigma == 0.0:
        return mean
    dist = NormalDist()
    q1 = dist.inv_cdf(1.0 - 1.0 / n_trials)
    q2 = dist.inv_cdf(1.0 - 1.0 / (n_trials * math.e))
    return mean + sigma * ((1.0 - EULER_GAMMA) * q1 + EULER_GAMMA * q2)


def deflated_sharpe_ratio(
    returns: pd.Series,
    *,
    sharpe_estimates: pd.Series | list[float] | None = None,
    trials: int | None = None,
    periods_per_year: int = 12,
) -> dict:
    """DSR: PSR against the expected maximum candidate Sharpe hurdle."""
    r = pd.to_numeric(returns, errors="coerce").dropna()
    if len(r) < 3:
        return {"expected_max_sharpe": None, "dsr": None}
    if sharpe_estimates is None:
        n_trials = max(1, int(trials or 1))
        if n_trials <= 1:
            hurdle = 0.0
        else:
            hurdle = math.sqrt(2.0 * math.log(n_trials) / max(1, len(r) - 1))
    else:
        hurdle = expected_max_sharpe(sharpe_estimates, trials=trials)
    psr = probabilistic_sharpe_ratio(r, benchmark_sharpe=hurdle, periods_per_year=periods_per_year)
    return {"expected_max_sharpe": hurdle, "dsr": psr}


def pbo_cscv(
    strategy_returns: pd.DataFrame,
    *,
    n_splits: int = 4,
    max_combinations: int = 2000,
    embargo: int = 0,
) -> dict:
    """Combinatorially symmetric cross-validation probability of overfit.

    Splits the train+validation return matrix into an even number of contiguous
    time blocks, selects the in-sample winner, then measures that winner's OOS
    relative rank/logit. Holdout returns must not be included in the input.

    ``embargo`` removes train rows within N periods of any test block boundary.
    This is a minimal purge for overlapping monthly labels such as a 20-trading-
    day holding period around month-start rebalances. The default keeps the
    legacy Phase C behavior.
    """
    data = strategy_returns.apply(pd.to_numeric, errors="coerce").dropna(how="all")
    data = data.dropna(axis=1, how="all")
    n_periods, n_strategies = data.shape
    n_splits = int(n_splits)
    embargo = int(embargo)
    if embargo < 0:
        raise ValueError("embargo must be >= 0")
    if n_splits < 2 or n_splits % 2 != 0:
        raise ValueError("n_splits must be an even integer >= 2")
    if n_periods < n_splits or n_strategies < 2:
        return {"pbo": None, "splits": 0, "logit_ranks": []}
    splits = cscv_train_test_indices(n_periods, n_splits=n_splits, embargo=embargo)
    if len(splits) > max_combinations:
        step = math.ceil(len(splits) / max_combinations)
        splits = splits[::step]
    logits: list[float] = []
    used = 0
    total_purged = 0
    for train_idx, test_idx in splits:
        total_purged += (n_periods - len(test_idx)) - len(train_idx)
        if not train_idx or not test_idx:
            continue
        train = data.iloc[sorted(train_idx)]
        test = data.iloc[sorted(test_idx)]
        train_scores = train.apply(period_sharpe_ratio)
        if train_scores.dropna().empty:
            continue
        winner = str(train_scores.astype(float).idxmax())
        test_scores = test.apply(period_sharpe_ratio)
        valid_scores = test_scores.dropna()
        if winner not in valid_scores.index or len(valid_scores) < 2:
            continue
        ranks = valid_scores.rank(method="average", ascending=True)
        omega = float(ranks[winner]) / (len(valid_scores) + 1.0)
        omega = min(max(omega, 1e-12), 1.0 - 1e-12)
        logits.append(math.log(omega / (1.0 - omega)))
        used += 1
    pbo = None if not logits else sum(1 for value in logits if value < 0.0) / len(logits)
    return {"pbo": pbo, "splits": used, "logit_ranks": logits, "embargo": embargo, "purged_train_rows": total_purged}


def cscv_train_test_indices(n_periods: int, n_splits: int, embargo: int = 0) -> list[tuple[list[int], list[int]]]:
    """Return native CSCV train/test index pairs with boundary purging.

    The folds are contiguous and the test set is every combination of half the
    folds. ``embargo`` removes train rows within N rows of each test-fold start
    and end boundary, matching the purge semantics checked against skfolio's
    ``CombinatorialPurgedCV(..., purged_size=N, embargo_size=0)`` for the
    parity fixture.
    """
    n_periods = int(n_periods)
    n_splits = int(n_splits)
    embargo = int(embargo)
    if embargo < 0:
        raise ValueError("embargo must be >= 0")
    if n_splits < 2 or n_splits % 2 != 0:
        raise ValueError("n_splits must be an even integer >= 2")
    if n_periods < n_splits:
        return []

    blocks = _cscv_blocks(n_periods, n_splits)
    rows: list[tuple[list[int], list[int]]] = []
    for test_blocks in itertools.combinations(range(n_splits), n_splits // 2):
        test_block_set = set(test_blocks)
        test_idx = [idx for block_id in test_blocks for idx in blocks[block_id]]
        train_idx = [idx for block_id, block in enumerate(blocks) if block_id not in test_block_set for idx in block]
        if embargo:
            train_idx = _apply_test_boundary_embargo(train_idx, [blocks[i] for i in test_blocks], embargo)
        rows.append((sorted(train_idx), sorted(test_idx)))
    return rows


def stationary_bootstrap_ci(
    returns: pd.Series,
    stat_fn,
    *,
    n_boot: int = 2000,
    expected_block: float = 3.0,
    alpha: float = 0.05,
    seed: int = 20260621,
    prob_threshold: float = 0.0,
) -> dict:
    """Stationary bootstrap confidence interval for a scalar statistic.

    Uses Politis-Romano geometric block resampling with cyclic continuation.
    The statistic function receives a pandas Series and must return a scalar.
    """
    r = pd.to_numeric(returns, errors="coerce").dropna().reset_index(drop=True)
    n = len(r)
    if n == 0:
        return {
            "stat": None,
            "lower": None,
            "upper": None,
            "alpha": alpha,
            "n_boot": 0,
            "expected_block": expected_block,
            "n_obs": 0,
            "prob_gt_threshold": None,
            "prob_threshold": _as_float_or_none(prob_threshold),
            "prob_n": 0,
        }
    if n_boot <= 0:
        raise ValueError("n_boot must be > 0")
    if expected_block <= 0:
        raise ValueError("expected_block must be > 0")
    if not (0.0 < alpha < 1.0):
        raise ValueError("alpha must be between 0 and 1")
    threshold = _as_float_or_none(prob_threshold)
    if threshold is None:
        raise ValueError("prob_threshold must be finite")

    observed = _as_float_or_none(stat_fn(r))
    rng = random.Random(seed)
    restart_prob = min(1.0, max(1e-12, 1.0 / float(expected_block)))
    stats: list[float] = []
    values = r.tolist()
    for _ in range(int(n_boot)):
        idx = rng.randrange(n)
        sample: list[float] = []
        for pos in range(n):
            if pos == 0 or rng.random() < restart_prob:
                idx = rng.randrange(n)
            else:
                idx = (idx + 1) % n
            sample.append(values[idx])
        value = _as_float_or_none(stat_fn(pd.Series(sample)))
        if value is not None:
            stats.append(value)
    if not stats:
        return {
            "stat": observed,
            "lower": None,
            "upper": None,
            "alpha": alpha,
            "n_boot": 0,
            "expected_block": expected_block,
            "n_obs": n,
            "prob_gt_threshold": None,
            "prob_threshold": threshold,
            "prob_n": 0,
        }
    prob_n = len(stats)
    prob_gt_threshold = sum(1 for value in stats if value > threshold) / prob_n
    stats = sorted(stats)
    lower = _quantile(stats, alpha / 2.0)
    upper = _quantile(stats, 1.0 - alpha / 2.0)
    return {
        "stat": observed,
        "lower": lower,
        "upper": upper,
        "alpha": alpha,
        "n_boot": len(stats),
        "expected_block": float(expected_block),
        "n_obs": n,
        "seed": int(seed),
        "prob_gt_threshold": float(prob_gt_threshold),
        "prob_threshold": threshold,
        "prob_n": prob_n,
    }


def _apply_test_boundary_embargo(train_idx: list[int], test_blocks: list[list[int]], embargo: int) -> list[int]:
    blocked: set[int] = set()
    for block in test_blocks:
        if not block:
            continue
        start = min(block)
        end = max(block)
        for boundary in (start, end):
            blocked.update(range(boundary - embargo, boundary + embargo + 1))
    return [idx for idx in train_idx if idx not in blocked]


def _cscv_blocks(n_periods: int, n_splits: int) -> list[list[int]]:
    return [
        list(range(int(i * n_periods / n_splits), int((i + 1) * n_periods / n_splits)))
        for i in range(n_splits)
    ]


def _as_float_or_none(value: object) -> float | None:
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(out) or math.isinf(out):
        return None
    return out


def _quantile(values: list[float], q: float) -> float:
    if not values:
        raise ValueError("values must not be empty")
    if len(values) == 1:
        return float(values[0])
    pos = min(max(q, 0.0), 1.0) * (len(values) - 1)
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return float(values[lo])
    weight = pos - lo
    return float(values[lo] * (1.0 - weight) + values[hi] * weight)
