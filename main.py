from __future__ import annotations

import inspect
import os
import sys


def _harden_inspect_for_frozen() -> None:
    """Stop PySide6's __feature__ scanner from crashing the frozen app.

    PySide6's shibokensupport hooks every import and calls inspect.getsource()
    on the module to look for ``from __feature__ import``. Under PyInstaller it
    eventually inspects six's virtual ``six.moves`` module, whose loader
    (``_SixMetaPathImporter``) has no ``_path`` attribute, so building inspect's
    error/repr raises AttributeError and tears the whole process down before the
    window ever appears. Make getsource/getsourcefile return a benign result on
    any failure so the scanner simply treats the module as feature-free.
    """
    _orig_getsourcefile = inspect.getsourcefile
    _orig_getsource = inspect.getsource

    def _safe_getsourcefile(obj):
        try:
            return _orig_getsourcefile(obj)
        except Exception:
            return None

    def _safe_getsource(obj):
        try:
            return _orig_getsource(obj)
        except Exception:
            return ""

    inspect.getsourcefile = _safe_getsourcefile
    inspect.getsource = _safe_getsource


_harden_inspect_for_frozen()


def _ensure_standard_streams() -> None:
    """Windowed exe builds may set stdout/stderr to None.

    Some data libraries still write progress/debug text. Redirect those writes
    to the null device so GUI users do not see a crash.
    """
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")
    if getattr(sys, "__stdout__", None) is None:
        sys.__stdout__ = sys.stdout
    if getattr(sys, "__stderr__", None) is None:
        sys.__stderr__ = sys.stderr


_ensure_standard_streams()

from stock_tool.ui.app_entry import run_app


if __name__ == "__main__":
    raise SystemExit(run_app())
