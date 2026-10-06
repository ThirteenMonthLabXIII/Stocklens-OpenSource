"""Shared, hardened baostock session helpers for Phase B ETL.

Extracted verbatim (behavior-preserving) from the Phase A probe
``research/sources/baostock_probe.py`` (the ``_drain`` / ``_query_to_df``
helpers, lines 104-130). baostock keeps a single, **non-thread-safe** global
socket, so every query is driven sequentially in the current thread/process;
large whole-market responses intermittently raise a utf-8 decode / 接收数据异常
on the first attempt, so transient socket/decode failures are retried with a
fixed back-off. Parallelism across stocks is achieved with **separate
processes**, each owning its own ``bs.login`` (see ``etl_prices``), never
threads.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Any, Callable, Iterator

import pandas as pd

from research.panel_db import ResearchPanelDB

RETRIES = 3
RETRY_BACKOFF_SEC = 1.0
LOGIN_EXPIRED_CODE = "10001001"
LOGIN_EXPIRED_TEXT = "用户未登录"


def is_login_expired_result(rs: Any) -> bool:
    return (
        str(getattr(rs, "error_code", "")) == LOGIN_EXPIRED_CODE
        or LOGIN_EXPIRED_TEXT in str(getattr(rs, "error_msg", ""))
    )


@contextmanager
def bs_login() -> Iterator[Any]:
    """Log in to baostock for the lifetime of the block; always log out.

    One login per process. Raises if login fails so callers do not silently
    issue queries against an unauthenticated session.
    """
    import baostock as bs

    lg = bs.login()
    if getattr(lg, "error_code", "1") != "0":
        raise RuntimeError(f"baostock login failed: {getattr(lg, 'error_msg', '')}")
    try:
        yield bs
    finally:
        try:
            bs.logout()
        except Exception:  # noqa: BLE001 - logout best-effort; never mask the real error
            pass


class BaostockSession:
    """Sequential baostock session with one best-effort relogin per query.

    baostock exposes process-global socket state. This wrapper keeps the same
    single-threaded contract as ``bs_login`` while adding recovery for the known
    ``10001001 / 用户未登录`` session-expiry response.
    """

    def __init__(self, db: ResearchPanelDB | None = None, run_id: str = "", api: str = "baostock") -> None:
        self.db = db
        self.run_id = run_id
        self.api = api
        self._bs: Any | None = None
        self.relogin_attempts = 0

    def __enter__(self) -> "BaostockSession":
        import baostock as bs

        self._bs = bs
        self._login()
        return self

    def __exit__(self, *exc: object) -> None:
        self._logout()

    def __getattr__(self, name: str) -> Any:
        if self._bs is None:
            raise RuntimeError("baostock session is not open")
        attr = getattr(self._bs, name)
        if not callable(attr):
            return attr

        def call_with_relogin(*args: Any, **kwargs: Any) -> Any:
            rs = attr(*args, **kwargs)
            if not is_login_expired_result(rs):
                return rs
            code = _infer_code(args, kwargs)
            self._relogin(code, getattr(rs, "error_msg", ""))
            rs = attr(*args, **kwargs)
            if is_login_expired_result(rs):
                msg = f"baostock relogin did not recover: {getattr(rs, 'error_msg', '')}"
                self._log_relogin(code, "error", msg)
            return rs

        return call_with_relogin

    def _login(self) -> None:
        if self._bs is None:
            raise RuntimeError("baostock module is not loaded")
        lg = self._bs.login()
        if getattr(lg, "error_code", "1") != "0":
            raise RuntimeError(f"baostock login failed: {getattr(lg, 'error_msg', '')}")

    def _logout(self) -> None:
        if self._bs is None:
            return
        try:
            self._bs.logout()
        except Exception:  # noqa: BLE001 - logout best-effort; never mask the real error
            pass

    def _relogin(self, code: str, reason: str) -> None:
        self.relogin_attempts += 1
        self._logout()
        try:
            self._login()
        except Exception as exc:  # noqa: BLE001
            self._log_relogin(code, "error", f"{reason}; relogin failed: {exc}")
            raise
        self._log_relogin(code, "ok", reason)

    def _log_relogin(self, code: str, status: str, reason: str) -> None:
        if self.db is None:
            return
        self.db.log_call(
            self.run_id,
            f"{self.api}_relogin",
            code,
            status,
            self.relogin_attempts,
            0.0,
            reason[:300] if reason else None,
        )


def _infer_code(args: tuple[Any, ...], kwargs: dict[str, Any]) -> str:
    code = kwargs.get("code")
    if code is None and args:
        first = args[0]
        if isinstance(first, str) and ("." in first or first[:1].isdigit()):
            code = first
    return str(code or "")


def drain(rs) -> pd.DataFrame:
    """Drain a baostock result set into a DataFrame (verbatim from probe)."""
    rows = []
    while rs.error_code == "0" and rs.next():
        rows.append(rs.get_row_data())
    return pd.DataFrame(rows, columns=rs.fields)


def query_to_df(factory: Callable[[], Any], retries: int = RETRIES) -> pd.DataFrame:
    """Run a baostock query and drain it, retrying transient socket/decode errors.

    Behavior identical to ``baostock_probe._query_to_df``: on a non-"0"
    ``error_code`` or any exception, sleep ``RETRY_BACKOFF_SEC`` and retry up to
    ``retries`` times; re-raise the last error if all attempts fail.
    """
    last: Exception | None = None
    for _ in range(retries):
        try:
            rs = factory()
            if getattr(rs, "error_code", "1") != "0":
                last = RuntimeError(
                    f"baostock error {getattr(rs, 'error_code', '?')}: {getattr(rs, 'error_msg', '')}"
                )
                time.sleep(RETRY_BACKOFF_SEC)
                continue
            return drain(rs)
        except Exception as exc:  # noqa: BLE001 - transient baostock receive errors
            last = exc
            time.sleep(RETRY_BACKOFF_SEC)
    raise last if last is not None else RuntimeError("baostock query failed")


def timed_query(factory: Callable[[], Any], retries: int = RETRIES) -> tuple[pd.DataFrame, float]:
    """``query_to_df`` plus wall-clock seconds, for ETL provenance/throughput."""
    started = time.perf_counter()
    df = query_to_df(factory, retries=retries)
    return df, round(time.perf_counter() - started, 3)
