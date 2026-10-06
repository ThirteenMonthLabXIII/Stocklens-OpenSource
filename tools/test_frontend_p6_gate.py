from __future__ import annotations

"""Static P6 guard for the only formal V3 build route: Nuitka standalone."""

from pathlib import Path

from build_v3_nuitka import nuitka_command
from stock_tool.config.release_identity import APP_VERSION, INSTALLER_NAME, NUITKA_VERSION, WINDOWS_FILE_VERSION


ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    command = nuitka_command()
    required = {
        "--standalone",
        "--mingw64",
        "--assume-yes-for-downloads",
        "--experimental=force-dependencies-pefile",
        "--enable-plugin=pyside6",
        "--include-module=PySide6.QtWebEngineCore",
        "--include-module=PySide6.QtWebEngineWidgets",
        "--include-module=PySide6.QtWebChannel",
        "--include-data-dir=stock_tool/config=stock_tool/config",
        "--include-data-dir=stock_tool/ui/web=stock_tool/ui/web",
        "--include-data-dir=assets=assets",
        "--windows-console-mode=disable",
        f"--file-version={WINDOWS_FILE_VERSION}",
        f"--product-version={WINDOWS_FILE_VERSION}",
    }
    assert required.issubset(command), f"V3 Nuitka 命令缺少正式依赖：{sorted(required - set(command))}"
    assert "--onefile" not in command, "V3 正式构建不得使用 onefile"
    assert f"Nuitka=={NUITKA_VERSION}" in (ROOT / "requirements-build.txt").read_text(encoding="utf-8")
    assert (ROOT / "requirements-v3.lock").is_file(), "V3 构建必须有独立依赖锁"

    package_smoke = (ROOT / "tools" / "test_qt_packaged_smoke.py").read_text(encoding="utf-8")
    for marker in ("event-loop-ready", "QtWebEngineProcess.exe", "qwindows.dll", "data_location.txt", "TemporaryDirectory"):
        assert marker in package_smoke, f"V3 候选 smoke 缺少：{marker}"
    assert "tk*.dll" not in package_smoke
    assert "classic" in package_smoke

    for script in ("打包exe.bat", "生成安装包.bat", "生成V3商业交付包.bat"):
        text = (ROOT / script).read_text(encoding="utf-8")
        assert "build_v3_nuitka.py" in text, f"{script} 未使用正式 Nuitka 入口"
        assert "build_protected_installer.py" not in text and "PyInstaller" not in text, f"{script} 仍调用旧构建链路"

    installer = (ROOT / "installer.iss").read_text(encoding="utf-8")
    assert '#define MyAppVersion "3.0.0"' not in installer
    assert '#define MyDistDir "dist\\Stocklens_V3"' not in installer
    assert '#define InstallerBaseFilename "Stocklens_V3_Setup"' not in installer
    for marker in ("MyAppVersion from release_identity.py", "MyDistDir from release_identity.py", "InstallerBaseFilename from release_identity.py"):
        assert marker in installer, f"installer.iss 未强制使用 release_identity：{marker}"
    assert 'Source: "{#MyDistDir}\\*"' in installer
    assert 'Filename: "{app}\\{#MyAppExeName}"' in installer
    assert INSTALLER_NAME == "Stocklens_V3_Setup.exe"
    assert (ROOT / "build.spec").is_file() and (ROOT / "build_qt.spec").is_file(), "旧 spec 只可兼容保留，不能删除"
    print("[OK] P6 V3 Nuitka 正式构建静态闸门通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
