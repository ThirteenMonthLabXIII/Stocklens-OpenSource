from __future__ import annotations

"""P5 gate for the source-tree Qt-only entry and legacy invocation rejection."""

import importlib
import io
import os
import sys
import types
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

LEGACY_ENV = "STOCK_TOOL_" + "CLASSIC_UI"
LEGACY_ARGUMENT = "--" + "classic"
REMOVED_MESSAGE = "经典" + "前端" + "已移除"


@contextmanager
def _environment(**updates: str | None):
    old = {name: os.environ.get(name) for name in updates}
    try:
        for name, value in updates.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        yield
    finally:
        for name, value in old.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


@contextmanager
def _qt_module(qt_run):
    name = "stock_tool.ui.qt_workbench"
    old = sys.modules.get(name)
    module = types.ModuleType(name)
    module.run_app = qt_run
    sys.modules[name] = module
    try:
        yield
    finally:
        if old is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = old


def _entry_module():
    import stock_tool.ui.app_entry as app_entry

    return importlib.reload(app_entry)


def test_default_selection_is_qt_and_probe_never_reports_legacy_frontend() -> None:
    entry = _entry_module()
    previous_argv = sys.argv[:]
    try:
        sys.argv = ["main.py"]
        output = io.StringIO()
        with _environment(STOCK_TOOL_STARTUP_PROBE="1", **{LEGACY_ENV: None}), redirect_stdout(output):
            assert entry.run_app() == 0
        assert output.getvalue().strip() == "frontend=qt stage=selected error_id=-"
        assert "frontend=" + "classic" not in output.getvalue()
    finally:
        sys.argv = previous_argv


def test_legacy_argument_and_environment_are_rejected_before_qt_import() -> None:
    entry = _entry_module()
    previous_argv = sys.argv[:]
    try:
        for argv, environment in (
            (["main.py", LEGACY_ARGUMENT], {LEGACY_ENV: None}),
            (["main.py"], {LEGACY_ENV: "1"}),
        ):
            sys.argv = argv
            stderr = io.StringIO()
            with _environment(**environment), _qt_module(lambda **_kwargs: (_ for _ in ()).throw(AssertionError("Qt 不应导入"))), redirect_stderr(stderr):
                assert entry.run_app() == 2
            assert REMOVED_MESSAGE in stderr.getvalue()
    finally:
        sys.argv = previous_argv


def test_qt_initialization_failure_is_redacted_and_never_falls_back() -> None:
    entry = _entry_module()
    calls: list[str] = []

    def broken_qt(**_kwargs) -> None:
        raise RuntimeError(r"C:\\Users\\private-user\\api_key=never-show-this")

    entry._show_qt_failure = lambda error_id, exception_name: calls.append(f"{error_id}:{exception_name}")
    stderr = io.StringIO()
    with _environment(STOCK_TOOL_STARTUP_PROBE=None, **{LEGACY_ENV: None}), _qt_module(broken_qt), redirect_stderr(stderr):
        assert entry.run_app() == 1

    assert len(calls) == 1 and calls[0].endswith(":RuntimeError")
    rendered = stderr.getvalue()
    assert "Qt 启动失败：RuntimeError" in rendered
    assert "private-user" not in rendered and "never-show-this" not in rendered


def test_fault_injection_requires_test_mode_and_startup_probe() -> None:
    entry = _entry_module()
    with _environment(
        STOCK_TOOL_TEST_MODE="1",
        STOCK_TOOL_STARTUP_PROBE="1",
        STOCK_TOOL_STARTUP_PROBE_MODE="until-imported",
        STOCK_TOOL_QT_FAULT_INJECTION="import",
    ):
        assert entry._test_fault("import")
    with _environment(
        STOCK_TOOL_TEST_MODE=None,
        STOCK_TOOL_STARTUP_PROBE="1",
        STOCK_TOOL_QT_FAULT_INJECTION="import",
    ):
        assert not entry._test_fault("import")
    with _environment(
        STOCK_TOOL_TEST_MODE="1",
        STOCK_TOOL_STARTUP_PROBE=None,
        STOCK_TOOL_QT_FAULT_INJECTION="import",
    ):
        assert not entry._test_fault("import")


def test_startup_probe_supports_qt_window_and_event_loop_stages() -> None:
    app_entry = (ROOT / "stock_tool" / "ui" / "app_entry.py").read_text(encoding="utf-8")
    qt_workbench = (ROOT / "stock_tool" / "ui" / "qt_workbench.py").read_text(encoding="utf-8")
    assert 'startup_probe("window-created")' in qt_workbench
    assert 'startup_probe("event-loop-ready")' in qt_workbench
    assert "stock_tool.ui." + "main_window" not in app_entry
    assert "frontend=" + "classic" not in app_entry


def main() -> int:
    test_default_selection_is_qt_and_probe_never_reports_legacy_frontend()
    test_legacy_argument_and_environment_are_rejected_before_qt_import()
    test_qt_initialization_failure_is_redacted_and_never_falls_back()
    test_fault_injection_requires_test_mode_and_startup_probe()
    test_startup_probe_supports_qt_window_and_event_loop_stages()
    print("[OK] P5 Qt-only 启动、旧调用拒绝与脱敏失败闸门通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
