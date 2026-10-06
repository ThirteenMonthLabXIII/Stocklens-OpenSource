"""Zero-dependency test helpers so research tests run via plain ``python`` too.

``raises`` is a drop-in for ``pytest.raises`` (works under pytest as well).
``run_tests`` discovers ``test_*`` callables in a module's globals and runs them,
returning a process exit code. This keeps the Phase B assertions runnable on the
project interpreter without requiring pytest to be installed.
"""

from __future__ import annotations

import contextlib
import traceback
from typing import Any, Callable


@contextlib.contextmanager
def raises(exc: type[BaseException]):
    try:
        yield
    except exc:
        return
    except BaseException as got:  # noqa: BLE001
        raise AssertionError(f"expected {exc.__name__}, got {type(got).__name__}: {got}")
    raise AssertionError(f"expected {exc.__name__}, nothing raised")


def run_tests(module_globals: dict[str, Any]) -> int:
    tests: list[tuple[str, Callable[[], None]]] = sorted(
        (name, fn)
        for name, fn in module_globals.items()
        if name.startswith("test_") and callable(fn)
    )
    failures: list[str] = []
    for name, fn in tests:
        try:
            fn()
            print(f"PASS {name}")
        except Exception:  # noqa: BLE001
            print(f"FAIL {name}")
            traceback.print_exc()
            failures.append(name)
    print(f"\n{len(tests) - len(failures)}/{len(tests)} passed")
    return 1 if failures else 0
