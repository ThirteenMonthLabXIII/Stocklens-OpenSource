from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.release_integrity import atomic_write_json
from tools.release_security_scan import _sanitized_gitleaks
from tools.release_tooling import sha256_file, tool_lock


def main() -> int:
    lock = tool_lock()
    assert lock["tools"]["cosign"]["version"] == "3.0.6"
    assert lock["tools"]["gitleaks"]["version"] == "8.28.0"
    assert lock["tools"]["syft"]["version"] == "1.44.0"
    for name in ("cosign", "gitleaks", "syft"):
        assert len(lock["tools"][name]["sha256"]) == 64
        assert "latest" not in lock["tools"][name]["asset_url"]
    finding = {"RuleID": "generic-api-key", "File": "fixture.txt", "StartLine": 2, "Secret": "must-not-leak", "Match": "token=must-not-leak"}
    sanitized = _sanitized_gitleaks([finding])
    assert sanitized == [{"RuleID": "generic-api-key", "File": "fixture.txt", "StartLine": 2}]
    with tempfile.TemporaryDirectory() as temporary_dir:
        path = Path(temporary_dir) / "atomic.json"
        atomic_write_json(path, {"ok": True})
        assert json.loads(path.read_text(encoding="utf-8")) == {"ok": True}
        assert not list(path.parent.glob("*.tmp"))
        assert sha256_file(path) != "0" * 64
    print("[OK] 发布安全工具锁、脱敏报告与原子写入测试通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
