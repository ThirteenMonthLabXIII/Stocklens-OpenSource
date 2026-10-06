from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.release_integrity import atomic_write_json
from tools.release_tooling import executable
from tools.test_v3_delivery import _manifest
from tools.verify_release_signing import sign_candidate, sign_release, verify_candidate_signatures, verify_release_signatures


def _release(root: Path) -> Path:
    release_dir = root / "release"
    release_dir.mkdir()
    manifest = _manifest(release_dir)
    manifest.update(
        {
            "commercial_dir": "Stocklens_V3_商业交付包",
            "source_dir": "Stocklens_V3_源码交付包",
            "created_at": "2026-07-17T00:00:00",
            "inventories": {},
            "manual_checklist": {"json": "manual.json", "txt": "manual.txt"},
        }
    )
    atomic_write_json(release_dir / "RELEASE_MANIFEST.json", manifest)
    atomic_write_json(
        release_dir / "FINAL_RELEASE_DECISION.json",
        {
            "schema_version": 1,
            "created_at": "2026-07-17T00:00:00",
            "allowed_for_external_release": True,
            "rc1_manifest_sha256": "1" * 64,
            "rc1_p7_sha256": "2" * 64,
            "promotion_sha256": "3" * 64,
            "sections": {
                "automated": "passed",
                "manual_acceptance": "passed",
                "supply_chain_integrity": "passed",
                "known_risks": "unknown publisher",
                "external_release": "allowed",
            },
        },
    )
    return release_dir


def _wrong_public_key(root: Path) -> Path:
    prefix = root / "wrong"
    env = os.environ.copy()
    env["COSIGN_PASSWORD"] = "wrong-key-test-password"
    completed = subprocess.run(
        [str(executable("cosign")), "generate-key-pair", "--output-key-prefix", str(prefix)],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=120,
    )
    if completed.returncode != 0:
        raise AssertionError("无法生成负测公钥")
    return prefix.with_suffix(".pub")


def test_release_signing_and_negative_paths() -> None:
    with tempfile.TemporaryDirectory() as temporary_dir:
        root = Path(temporary_dir)
        release_dir = _release(root)
        candidate_index_path = sign_candidate(release_dir)
        assert candidate_index_path.is_file()
        assert not verify_candidate_signatures(release_dir)

        candidate_index = json.loads(candidate_index_path.read_text(encoding="utf-8"))
        manifest_entry = next(entry for entry in candidate_index["subjects"] if entry["role"] == "release_manifest")
        signature_path = release_dir / manifest_entry["signature_path"]
        signature_bytes = signature_path.read_bytes()
        signature_path.unlink()
        assert any("release_manifest subject/signature/bundle 缺失" in item for item in verify_candidate_signatures(release_dir))
        signature_path.write_bytes(signature_bytes)

        bundle_path = release_dir / manifest_entry["bundle_path"]
        bundle_bytes = bundle_path.read_bytes()
        bundle_path.write_bytes(bundle_bytes + b" ")
        assert any("release_manifest bundle 已改变" in item for item in verify_candidate_signatures(release_dir))
        bundle_path.write_bytes(bundle_bytes)

        index_path = sign_release(release_dir)
        assert index_path.is_file()
        assert not verify_release_signatures(release_dir)

        manifest = json.loads((release_dir / "RELEASE_MANIFEST.json").read_text(encoding="utf-8"))
        source_zip = release_dir / manifest["artifacts"]["source_zip"]["path"]
        original = source_zip.read_bytes()
        source_zip.write_bytes(original + b"tampered")
        assert any("source_zip subject 已改变" in item for item in verify_release_signatures(release_dir))
        source_zip.write_bytes(original)

        wrong_public_key = _wrong_public_key(root)
        wrong_key_errors = verify_release_signatures(release_dir, public_key=wrong_public_key)
        assert any("Cosign 验签失败" in item for item in wrong_key_errors)


def main() -> int:
    test_release_signing_and_negative_paths()
    print("[PASS] release signing positive/tamper/wrong-key tests")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
