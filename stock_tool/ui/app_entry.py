from __future__ import annotations

import os
import json
import sys
import uuid
from datetime import datetime
from pathlib import Path


def _legacy_frontend_requested() -> bool:
    legacy_environment = "STOCK_TOOL_" + "CLASSIC_UI"
    legacy_argument = "--" + "classic"
    return os.environ.get(legacy_environment, "").strip() == "1" or legacy_argument in sys.argv[1:]


def _p7_session_argument() -> Path | None:
    arguments = sys.argv[1:]
    if "--p7-session" not in arguments:
        return None
    index = arguments.index("--p7-session")
    if index + 1 >= len(arguments) or not arguments[index + 1].strip():
        raise RuntimeError("P7 session 参数缺少文件路径。")
    return Path(arguments[index + 1]).expanduser().resolve()


def _build_identity_path() -> Path:
    override = os.environ.get("STOCK_TOOL_BUILD_IDENTITY", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / "stock_tool" / "config" / "v3_build_identity.json"
    return Path(__file__).resolve().parents[1] / "config" / "v3_build_identity.json"


def _activate_p7_session(session_path: Path | None) -> None:
    if session_path is None:
        return
    identity_path = _build_identity_path()
    if not session_path.is_file() or not identity_path.is_file():
        raise RuntimeError("P7 session 或只读 build identity 不存在。")
    session = json.loads(session_path.read_text(encoding="utf-8"))
    identity = json.loads(identity_path.read_text(encoding="utf-8"))
    for key in ("candidate_id", "candidate_stage", "git_commit"):
        if not session.get(key) or session.get(key) != identity.get(key):
            raise RuntimeError(f"P7 session 与 build identity 不一致：{key}")
    challenge = str(session.get("challenge_id") or "")
    if not challenge:
        raise RuntimeError("P7 session 缺少 challenge。")
    os.environ["STOCK_TOOL_P7_BANNER"] = (
        f"P7 验收模式｜候选 {str(session['candidate_id'])[-12:]}｜"
        f"阶段 {str(session['candidate_stage']).upper()}｜挑战码 {challenge}"
    )
    pointer = Path(sys.executable).resolve().parent / "data_location.txt"
    if pointer.is_file():
        data_dir = Path(pointer.read_text(encoding="utf-8").strip()).expanduser().resolve()
        data_dir.mkdir(parents=True, exist_ok=True)
        runtime_record = {
            "schema_version": 1,
            "candidate_id": session["candidate_id"],
            "candidate_stage": session["candidate_stage"],
            "challenge_id": challenge,
            "session_sha256": __import__("hashlib").sha256(session_path.read_bytes()).hexdigest(),
            "started_at": datetime.now().isoformat(timespec="seconds"),
        }
        target = data_dir / "p7_runtime_session.json"
        temporary = target.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(runtime_record, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temporary, target)


def _emit_startup_probe(stage: str, error_id: str = "-") -> bool:
    if os.environ.get("STOCK_TOOL_STARTUP_PROBE", "").strip() != "1":
        return False
    record = f"frontend=qt stage={stage} error_id={error_id}"
    output_path = os.environ.get("STOCK_TOOL_STARTUP_PROBE_FILE", "").strip()
    if output_path:
        Path(output_path).write_text(record + "\n", encoding="utf-8")
    else:
        print(record)
    stop_stage = os.environ.get("STOCK_TOOL_STARTUP_PROBE_MODE", "").strip()
    if not stop_stage:
        # Historical probe callers expect the initial selection record to stop
        # the process before any graphical object is created.
        return True
    if stage == "selected":
        return stop_stage not in {"until-imported", "until-window-created", "until-event-loop-ready"}
    return stop_stage == f"until-{stage}"


def _test_probe_enabled() -> bool:
    return (
        os.environ.get("STOCK_TOOL_TEST_MODE", "").strip() == "1"
        and os.environ.get("STOCK_TOOL_STARTUP_PROBE", "").strip() == "1"
    )


def _test_fault(stage: str) -> bool:
    """Allow fault injection only in an explicit probe-driven test process."""
    return _test_probe_enabled() and os.environ.get("STOCK_TOOL_QT_FAULT_INJECTION", "").strip() == stage


def _show_qt_failure(error_id: str, exception_name: str) -> None:
    """Show only sanitized Qt failure details; never offer another frontend."""
    message = (
        "Qt 工作台启动失败。\n\n"
        f"错误编号：{error_id}\n"
        f"异常类型：{exception_name}\n\n"
        "请检查 Qt 依赖和运行环境后重试。诊断信息不包含密钥或个人数据。"
    )
    if _test_probe_enabled():
        return
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, message, "Stocklens Qt 工作台启动失败", 0x0000 | 0x0010)
    except Exception:
        return None


def run_app() -> int:
    if _legacy_frontend_requested():
        print("经典" + "前端已移除，请使用 Qt 工作台。", file=sys.stderr)
        return 2

    _activate_p7_session(_p7_session_argument())
    if _emit_startup_probe("selected"):
        return 0

    try:
        if _test_fault("import"):
            raise ImportError("test-injected Qt import fault")
        from stock_tool.ui.qt_workbench import run_app as run_qt_app
        if _emit_startup_probe("imported"):
            return 0
        run_qt_app(startup_probe=lambda stage: _emit_startup_probe(stage))
        return 0
    except Exception as exc:
        error_id = uuid.uuid4().hex[:10]
        print(f"[startup:{error_id}] Qt 启动失败：{type(exc).__name__}", file=sys.stderr)
        _emit_startup_probe("failed", error_id)
        _show_qt_failure(error_id, type(exc).__name__)
        return 1
