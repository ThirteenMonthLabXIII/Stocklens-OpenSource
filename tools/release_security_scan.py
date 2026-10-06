from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.release_integrity import atomic_write_json
from tools.release_tooling import TOOLCACHE, executable, verify_cached_tools


REPORT_DIR = ROOT / "docs" / "acceptance_reports"
RELEASE_PYTHON = TOOLCACHE / "release-venv" / "Scripts" / "python.exe"


def _run(command: list[str], *, timeout: int = 600) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=ROOT, text=True, capture_output=True, timeout=timeout)


def _sanitized_gitleaks(findings: list[dict]) -> list[dict[str, object]]:
    allowed = ("RuleID", "Description", "File", "StartLine", "EndLine", "Commit", "Fingerprint")
    return [{key: item.get(key) for key in allowed if key in item} for item in findings]


def copy_scannable_source(destination: Path) -> None:
    completed = _run(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"])
    if completed.returncode != 0:
        raise RuntimeError("无法枚举 Git 源码边界。")
    for relative_text in completed.stdout.split("\0"):
        if not relative_text:
            continue
        source = ROOT / relative_text
        if source.is_file():
            target = destination / relative_text
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)


def run_gitleaks(target: Path = ROOT) -> Path:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    report_path = REPORT_DIR / "v3_gitleaks.json"
    with tempfile.TemporaryDirectory(dir=TOOLCACHE) as temporary_dir:
        raw_path = Path(temporary_dir) / "gitleaks.json"
        command = [
            str(executable("gitleaks")), "dir", str(target), "--no-banner", "--redact=100",
            "--config", str(ROOT / ".gitleaks.toml"), "--max-archive-depth", "2",
            "--report-format", "json", "--report-path", str(raw_path),
        ]
        completed = _run(command)
        findings = json.loads(raw_path.read_text(encoding="utf-8")) if raw_path.is_file() and raw_path.stat().st_size else []
        sanitized = _sanitized_gitleaks(findings if isinstance(findings, list) else [])
        atomic_write_json(report_path, {"passed": completed.returncode == 0 and not sanitized, "findings": sanitized})
        if completed.returncode not in {0, 1}:
            raise RuntimeError("Gitleaks 执行失败；详细信息见脱敏内部报告。")
        if sanitized:
            first = sanitized[0]
            raise RuntimeError(f"Gitleaks 发现未批准项：{first.get('RuleID')} {first.get('File')}:{first.get('StartLine')}")
    return report_path


def generate_sbom(target: Path = ROOT) -> Path:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORT_DIR / "v3_sbom.cdx.json"
    completed = _run(
        [
            str(executable("syft")), f"dir:{target}",
            "--exclude", "./.toolcache/**", "--exclude", "./.git/**", "--exclude", "./build/**",
            "--exclude", "./dist/**", "--exclude", "./release/**", "-o", f"cyclonedx-json={path}",
        ],
        timeout=900,
    )
    if completed.returncode != 0 or not path.is_file():
        raise RuntimeError("Syft SBOM 生成失败。")
    return path


def run_pip_audit() -> Path:
    if not RELEASE_PYTHON.is_file():
        raise RuntimeError("缺少隔离 release venv，无法运行 pip-audit。")
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORT_DIR / "v3_pip_audit.json"
    completed = _run(
        [str(RELEASE_PYTHON), "-m", "pip_audit", "-r", "requirements-v3.lock", "-f", "json", "-o", str(path)],
        timeout=900,
    )
    if completed.returncode != 0:
        raise RuntimeError("pip-audit 无法查询或发现未处置漏洞；请查看 JSON 报告。")
    return path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-tree", action="store_true")
    args = parser.parse_args()
    errors = verify_cached_tools()
    if errors:
        print("[BLOCKED] 发布安全工具尚未通过固定版本与 SHA256 校验")
        print("影响：不能生成可信的 secret scan、SBOM 或最终签名证据。")
        print("下一步：运行 python tools/release_tooling.py，并解决：" + "；".join(errors))
        return 1
    if args.source_tree:
        with tempfile.TemporaryDirectory(dir=TOOLCACHE) as temporary_dir:
            scan_root = Path(temporary_dir) / "source"
            copy_scannable_source(scan_root)
            run_gitleaks(scan_root)
    generate_sbom(ROOT)
    run_pip_audit()
    print("[OK] Gitleaks、Syft 与 pip-audit 发布安全检查通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
