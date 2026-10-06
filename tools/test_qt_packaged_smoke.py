from __future__ import annotations

"""Cold-start smoke for the formal Nuitka V3 Qt-only package."""

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.release_integrity import ArtifactInventory, Issue, compare_inventories, directory_inventory

EXPECTED_EXE = "Stocklens.exe"


def assert_candidate_unchanged_after_quiet_window(
    candidate_dir: Path,
    before: ArtifactInventory,
    *,
    quiet_seconds: float = 5.0,
) -> list[Issue]:
    time.sleep(quiet_seconds)
    return compare_inventories(before, directory_inventory(candidate_dir))


def candidate_processes(candidate_dir: Path) -> list[str]:
    env = os.environ.copy()
    env["STOCKLENS_CANDIDATE_ROOT"] = str(candidate_dir.resolve())
    script = (
        "$root=$env:STOCKLENS_CANDIDATE_ROOT; "
        "Get-CimInstance Win32_Process | Where-Object { "
        "($_.ExecutablePath -and $_.ExecutablePath.StartsWith($root,[System.StringComparison]::OrdinalIgnoreCase)) "
        "} | ForEach-Object { \"$($_.ProcessId)|$($_.Name)|$($_.ExecutablePath)\" }"
    )
    completed = subprocess.run(
        ["powershell", "-NoProfile", "-Command", script],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    if completed.returncode != 0:
        raise AssertionError("无法使用 Windows 进程信息确认候选进程已退出")
    return [line.strip() for line in completed.stdout.splitlines() if line.strip()]


def _atomic_write_text(path: Path, text: str) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def main() -> int:
    dist_dir_raw = os.environ.get("STOCK_TOOL_PACKAGED_DIR", "").strip()
    assert dist_dir_raw, "必须设置 STOCK_TOOL_PACKAGED_DIR 为候选 Qt 安装目录；不允许跳过打包 smoke"
    dist_dir = Path(dist_dir_raw).resolve()
    assert dist_dir.is_dir(), f"候选 Qt 安装目录不存在：{dist_dir}"
    processes = list(dist_dir.rglob("QtWebEngineProcess.exe"))
    assert processes, "候选安装包缺少 QtWebEngineProcess.exe"
    assert list(dist_dir.rglob("platforms/qwindows.dll")), "候选安装包缺少 Qt 平台插件 qwindows.dll"
    assert (dist_dir / "stock_tool" / "ui" / "web" / "stock_workbench.html").is_file(), "候选安装包缺少 Web 资源"
    assert (dist_dir / "stock_tool" / "ui" / "web" / "stocklens_icon.png").is_file(), "候选安装包缺少正式图标资源"
    executables = [path for path in dist_dir.glob(EXPECTED_EXE)]
    assert executables, f"候选安装包缺少主程序 {EXPECTED_EXE}"
    candidate_data_location = dist_dir / "data_location.txt"
    assert not candidate_data_location.exists(), "候选包不得携带开发者 data_location.txt"
    before_inventory = directory_inventory(dist_dir)

    with tempfile.TemporaryDirectory() as temporary_dir:
        isolated_data_dir = Path(temporary_dir) / "isolated_candidate_data"
        isolated_data_dir.mkdir()
        _atomic_write_text(candidate_data_location, str(isolated_data_dir))
        probe_file = Path(temporary_dir) / "startup_probe.txt"
        env = os.environ.copy()
        env["QT_QPA_PLATFORM"] = "offscreen"
        env["QTWEBENGINE_DISABLE_SANDBOX"] = "1"
        env["STOCK_TOOL_STARTUP_PROBE"] = "1"
        env["STOCK_TOOL_STARTUP_PROBE_MODE"] = "until-event-loop-ready"
        env["STOCK_TOOL_STARTUP_PROBE_FILE"] = str(probe_file)
        try:
            completed = subprocess.run([str(executables[0])], cwd=dist_dir, env=env, capture_output=True, text=True, timeout=30)
            assert completed.returncode == 0, f"候选 Qt 包启动探针异常退出：{completed.stdout}\n{completed.stderr}"
            assert probe_file.exists(), "候选 Qt 包没有写出启动探针"
            probe = probe_file.read_text(encoding="utf-8").strip()
            assert probe == "frontend=qt stage=event-loop-ready error_id=-", f"候选包未完成 Qt 事件循环就绪：{probe}"

            env["STOCK_TOOL_STARTUP_PROBE_MODE"] = "until-imported"
            if probe_file.exists():
                probe_file.unlink()
            legacy_argument = "--" + "classic"
            completed = subprocess.run([str(executables[0]), legacy_argument], cwd=dist_dir, env=env, capture_output=True, text=True, timeout=30)
            assert completed.returncode == 2, f"旧调用未按 Qt-only 合同拒绝：{completed.stdout}\n{completed.stderr}"
            if probe_file.exists():
                assert "frontend=" + "classic" not in probe_file.read_text(encoding="utf-8")

            env["STOCK_TOOL_TEST_MODE"] = "1"
            env["STOCK_TOOL_QT_FAULT_INJECTION"] = "import"
            if probe_file.exists():
                probe_file.unlink()
            completed = subprocess.run([str(executables[0])], cwd=dist_dir, env=env, capture_output=True, text=True, timeout=30)
            assert completed.returncode != 0, "Qt 故障必须以非零状态退出"
            assert probe_file.read_text(encoding="utf-8").strip().startswith("frontend=qt stage=failed error_id="), "Qt 故障必须记录失败阶段"
        finally:
            if candidate_data_location.exists():
                candidate_data_location.unlink()
            residual = candidate_processes(dist_dir)
            assert not residual, "包级 smoke 退出后仍有候选进程残留：" + "；".join(residual)
            inventory_issues = assert_candidate_unchanged_after_quiet_window(dist_dir, before_inventory)
            assert not inventory_issues, "包级 smoke 后候选目录发生新增、修改或迟到写入：" + "；".join(map(str, inventory_issues))
        assert not candidate_data_location.exists(), "包级 smoke 必须在 finally 中清理临时 data_location.txt"
    print("[OK] Qt 候选安装包冷启动 smoke 通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
