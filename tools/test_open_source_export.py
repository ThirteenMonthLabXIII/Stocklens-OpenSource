"""Exercise source-export privacy boundaries through its command-line interface."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def run_tests() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        base = Path(temporary)
        source = base / "source"
        source.mkdir()
        shutil.copytree(ROOT / "docs" / "open_source", source / "docs" / "open_source")
        examples = {
            "main.py": "print('fixture')\n",
            "stock_tool/core/sample.py": "VALUE = 42\n",
            "stock_tool/config/sample.json": '{"public": true}\n',
            "research/sample.py": "# research source\n",
            "stock_tool/core/sample.sqlite3-wal": "PRIVATE",
            "stock_tool/core/.env.production": "PRIVATE",
            "stock_tool/core/secret.private.json": "PRIVATE",
            "stock_tool/core/cache/secret.py": "PRIVATE",
            "research/data/private.py": "PRIVATE",
            "research/out/results.json": "PRIVATE",
            "research/holdout_ledger.json": "PRIVATE",
            "my_data_live_preflight/private.py": "PRIVATE",
            "docs/acceptance_reports/secret.md": "PRIVATE",
            "docs/对话交接_private.md": "PRIVATE",
            "private_keys/private.key": "PRIVATE",
            "data_location.txt": "PRIVATE",
            "build/output.exe": "PRIVATE",
            ".git/config": "PRIVATE",
            ".claude/settings.local.json": "PRIVATE",
        }
        for name, content in examples.items():
            path = source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        output = base / "candidate"
        command = [sys.executable, str(ROOT / "tools" / "prepare_open_source.py"),
                   "--source", str(source), "--output", str(output)]
        environment = {**os.environ, "PYTHONUTF8": "1"}
        completed = subprocess.run(command, env=environment, capture_output=True, text=True)
        assert completed.returncode == 0, completed.stderr
        assert (output / "stock_tool/core/sample.py").read_bytes() == (source / "stock_tool/core/sample.py").read_bytes()
        assert (output / "README.md").read_bytes() == (source / "docs/open_source/README.md").read_bytes()
        assert (output / ".github/workflows/ci.yml").is_file()
        assert all(b"PRIVATE" not in path.read_bytes() for path in output.rglob("*") if path.is_file())
        again = subprocess.run(command, env=environment, capture_output=True, text=True)
        assert again.returncode != 0
        assert (source / "data_location.txt").read_text(encoding="utf-8") == "PRIVATE"
        nested = subprocess.run(command[:-1] + [str(source / "stock_tool" / "export")],
                                env=environment, capture_output=True, text=True)
        assert nested.returncode != 0
        assert not (source / "stock_tool" / "export").exists()
        external = base / "external.py"
        external.write_text("PRIVATE", encoding="utf-8")
        link = source / "stock_tool" / "external.py"
        try:
            link.symlink_to(external)
        except OSError:
            print("[SKIP] Linked-file case: OS did not allow creating a symlink")
        else:
            linked_output = base / "linked-candidate"
            linked = subprocess.run(command[:-1] + [str(linked_output)],
                                    env=environment, capture_output=True, text=True)
            assert linked.returncode != 0
            assert not linked_output.exists()
        if os.name == "nt":
            external_dir = base / "external-directory"
            external_dir.mkdir()
            (external_dir / "secret.py").write_text("PRIVATE", encoding="utf-8")
            junction = source / "stock_tool" / "external-directory"
            junction_env = {
                **environment, "STOCKLENS_LINK_FIXTURE": str(junction),
                "STOCKLENS_EXTERNAL_FIXTURE": str(external_dir),
            }
            created = subprocess.run([
                "powershell", "-NoProfile", "-NonInteractive", "-Command",
                "$ErrorActionPreference='Stop'; New-Item -ItemType Junction "
                "-Path $env:STOCKLENS_LINK_FIXTURE -Target $env:STOCKLENS_EXTERNAL_FIXTURE | Out-Null",
            ], env=junction_env, capture_output=True, text=True)
            assert created.returncode == 0, created.stderr
            junction_output = base / "junction-candidate"
            linked = subprocess.run(command[:-1] + [str(junction_output)],
                                    env=environment, capture_output=True, text=True)
            assert linked.returncode != 0
            assert not junction_output.exists()
            assert (external_dir / "secret.py").read_text(encoding="utf-8") == "PRIVATE"
    print("[OK] Source export excludes runtime data, credentials, reports and Git metadata; refuses overwrites and linked sources")


if __name__ == "__main__":
    run_tests()
