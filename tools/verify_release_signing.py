from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.release_integrity import atomic_write_json
from tools.release_schema import require_schema, validate_schema
from tools.release_tooling import TOOLCACHE, executable, verify_cached_tools
from tools.windows_credential import read_secret, write_secret


PRIVATE_TARGET = "Stocklens/V3/CosignPrivateKey/v1"
PASSWORD_TARGET = "Stocklens/V3/CosignPassword/v1"
PUBLIC_DIR = ROOT / "stock_tool" / "config" / "release_signing"
PUBLIC_KEY = PUBLIC_DIR / "cosign-v1.pub"
KEY_REGISTRY = PUBLIC_DIR / "keys.json"
SIGSTORE_TRUSTED_ROOT = (
    Path.home()
    / ".sigstore"
    / "root"
    / "tuf-repo-cdn.sigstore.dev"
    / "targets"
    / "trusted_root.json"
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run(command: list[str], *, env: dict[str, str] | None = None) -> str:
    transient_markers = (
        "tls handshake timeout",
        "i/o timeout",
        "context deadline exceeded",
        "connection reset",
        "connection refused",
        "failed to fetch trusted_root.json",
    )
    for attempt in range(3):
        completed = subprocess.run(command, cwd=ROOT, env=env, text=True, capture_output=True, timeout=120)
        if completed.returncode == 0:
            return completed.stdout
        diagnostic = ((completed.stdout or "") + "\n" + (completed.stderr or "")).lower()
        if attempt < 2 and any(marker in diagnostic for marker in transient_markers):
            time.sleep(2 * (attempt + 1))
            continue
        raise RuntimeError("Cosign 签名操作失败；内部输出已抑制，未打印凭据。")
    raise RuntimeError("Cosign 签名操作失败；内部输出已抑制，未打印凭据。")


def ensure_key() -> str:
    if PUBLIC_KEY.is_file() and KEY_REGISTRY.is_file():
        read_secret(PRIVATE_TARGET)
        read_secret(PASSWORD_TARGET)
        registry = json.loads(KEY_REGISTRY.read_text(encoding="utf-8"))
        return str(registry["active_key_id"])
    errors = verify_cached_tools()
    if errors:
        raise RuntimeError("Cosign 尚未通过固定版本校验。")
    runtime_root = TOOLCACHE / "runtime"
    runtime_root.mkdir(parents=True, exist_ok=True)
    temporary_dir = Path(tempfile.mkdtemp(prefix="cosign-key-", dir=runtime_root))
    try:
        prefix = temporary_dir / "stocklens-release"
        password = secrets.token_urlsafe(36)
        env = os.environ.copy()
        env["COSIGN_PASSWORD"] = password
        _run([str(executable("cosign")), "generate-key-pair", "--output-key-prefix", str(prefix)], env=env)
        private_key = prefix.with_suffix(".key").read_text(encoding="utf-8")
        public_key = prefix.with_suffix(".pub").read_bytes()
        write_secret(PRIVATE_TARGET, private_key)
        write_secret(PASSWORD_TARGET, password)
        if read_secret(PRIVATE_TARGET) != private_key or read_secret(PASSWORD_TARGET) != password:
            raise RuntimeError("Windows 凭据管理器写入后回读不一致。")
        PUBLIC_DIR.mkdir(parents=True, exist_ok=True)
        PUBLIC_KEY.write_bytes(public_key)
        key_id = hashlib.sha256(public_key).hexdigest()
        atomic_write_json(
            KEY_REGISTRY,
            {
                "schema_version": 1,
                "active_key_id": key_id,
                "keys": [{"key_id": key_id, "public_key_path": "stock_tool/config/release_signing/cosign-v1.pub", "status": "active"}],
            },
        )
        return key_id
    finally:
        shutil.rmtree(temporary_dir, ignore_errors=True)
        if temporary_dir.exists():
            raise RuntimeError("Cosign 密钥临时目录清理失败。")


def _inside_release(path: Path, release_dir: Path) -> bool:
    try:
        path.resolve().relative_to(release_dir.resolve())
        return True
    except ValueError:
        return False


def _relative(path: Path, release_dir: Path) -> str:
    return path.resolve().relative_to(release_dir.resolve()).as_posix()


def verify_signature(subject: Path, signature: Path, bundle: Path, *, public_key: Path = PUBLIC_KEY) -> None:
    command = [str(executable("cosign")), "verify-blob", "--key", str(public_key)]
    if SIGSTORE_TRUSTED_ROOT.is_file():
        command.extend(["--trusted-root", str(SIGSTORE_TRUSTED_ROOT)])
    command.extend(["--bundle", str(bundle), str(subject)])
    _run(command)


def sign_and_verify(subject: Path, signature: Path, bundle: Path) -> None:
    ensure_key()
    runtime_root = TOOLCACHE / "runtime"
    runtime_root.mkdir(parents=True, exist_ok=True)
    temporary_dir = Path(tempfile.mkdtemp(prefix="cosign-sign-", dir=runtime_root))
    try:
        private_path = temporary_dir / "cosign.key"
        private_path.write_text(read_secret(PRIVATE_TARGET), encoding="utf-8")
        env = os.environ.copy()
        env["COSIGN_PASSWORD"] = read_secret(PASSWORD_TARGET)
        signature.parent.mkdir(parents=True, exist_ok=True)
        detached_signature = _run(
            [
                str(executable("cosign")),
                "sign-blob",
                "--yes",
                # This is a local key-based release signature.  Cosign v3's
                # TUF signing config additionally contacts the public TSA;
                # that optional timestamp endpoint made the offline release
                # block fail on transient TLS timeouts.  The new bundle still
                # carries the transparency-log entry and is read back with
                # the pinned public key below.
                "--use-signing-config=false",
                "--key",
                str(private_path),
                "--bundle",
                str(bundle),
                str(subject),
            ],
            env=env,
        )
        if not detached_signature.strip():
            try:
                detached_signature = str(json.loads(bundle.read_text(encoding="utf-8"))["messageSignature"]["signature"])
            except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
                raise RuntimeError("Cosign bundle 未包含 detached signature。") from exc
        signature.write_text(detached_signature.strip() + "\n", encoding="ascii")
        # Cosign v3's new bundle already embeds the signature. Passing both
        # --bundle and --signature makes verification reject the duplicate
        # signature source.
        verify_signature(subject, signature, bundle)
    finally:
        shutil.rmtree(temporary_dir, ignore_errors=True)
        if temporary_dir.exists():
            raise RuntimeError("Cosign 签名临时目录清理失败。")


def _candidate_subjects(release_dir: Path) -> list[tuple[str, Path]]:
    manifest_path = release_dir / "RELEASE_MANIFEST.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    require_schema("release", manifest)
    subjects: list[tuple[str, Path]] = [("release_manifest", manifest_path)]
    for role in ("installer", "commercial_zip", "source_zip"):
        record = manifest.get("artifacts", {}).get(role)
        if not isinstance(record, dict):
            raise RuntimeError(f"release manifest 缺少 {role} artifact。")
        subject = (release_dir / str(record.get("path") or "")).resolve()
        if not _inside_release(subject, release_dir) or not subject.is_file():
            raise RuntimeError(f"{role} subject 不在 release 目录内或不存在。")
        if sha256_file(subject) != record.get("sha256") or subject.stat().st_size != record.get("size_bytes"):
            raise RuntimeError(f"{role} subject 与 release manifest 不一致。")
        subjects.append((role, subject))
    return subjects


def _final_subject(release_dir: Path) -> tuple[str, Path]:
    decision_path = release_dir / "FINAL_RELEASE_DECISION.json"
    if not decision_path.is_file():
        raise RuntimeError("缺少 FINAL_RELEASE_DECISION.json，不能形成完整签名链。")
    decision = json.loads(decision_path.read_text(encoding="utf-8"))
    require_schema("final_decision", decision)
    return "final_decision", decision_path


def _sign_entry(role: str, subject: Path, release_dir: Path, signature_dir: Path) -> dict[str, object]:
    signature = signature_dir / f"{role}.sig"
    bundle = signature_dir / f"{role}.bundle.json"
    sign_and_verify(subject, signature, bundle)
    return {
        "role": role,
        "subject_path": _relative(subject, release_dir),
        "subject_sha256": sha256_file(subject),
        "subject_size_bytes": subject.stat().st_size,
        "signature_path": _relative(signature, release_dir),
        "signature_sha256": sha256_file(signature),
        "bundle_path": _relative(bundle, release_dir),
        "bundle_sha256": sha256_file(bundle),
    }


def sign_candidate(release_dir: Path) -> Path:
    release_dir = release_dir.resolve()
    signature_dir = release_dir / "release_local" / "signatures"
    signature_dir.mkdir(parents=True, exist_ok=True)
    index = {
        "schema_version": 1,
        "key_id": ensure_key(),
        "public_key_path": "stock_tool/config/release_signing/cosign-v1.pub",
        "subjects": [_sign_entry(role, subject, release_dir, signature_dir) for role, subject in _candidate_subjects(release_dir)],
    }
    require_schema("candidate_signature_index", index)
    path = signature_dir / "CANDIDATE_SIGNATURES.json"
    atomic_write_json(path, index)
    errors = verify_candidate_signatures(release_dir)
    if errors:
        raise RuntimeError("候选四类签名回读验证失败：\n" + "\n".join(errors))
    return path


def sign_release(release_dir: Path) -> Path:
    release_dir = release_dir.resolve()
    candidate_errors = verify_candidate_signatures(release_dir)
    if candidate_errors:
        raise RuntimeError("候选四类 subject 尚未形成可信签名：\n" + "\n".join(candidate_errors))
    signature_dir = release_dir / "release_local" / "signatures"
    candidate_index = json.loads((signature_dir / "CANDIDATE_SIGNATURES.json").read_text(encoding="utf-8"))
    role, subject = _final_subject(release_dir)
    entries = [*candidate_index["subjects"], _sign_entry(role, subject, release_dir, signature_dir)]
    index = {
        "schema_version": 1,
        "key_id": candidate_index["key_id"],
        "public_key_path": "stock_tool/config/release_signing/cosign-v1.pub",
        "subjects": entries,
    }
    require_schema("signature_index", index)
    index_path = signature_dir / "SIGNATURES.json"
    atomic_write_json(index_path, index)
    errors = verify_release_signatures(release_dir)
    if errors:
        raise RuntimeError("候选签名回读验证失败：\n" + "\n".join(errors))
    return index_path


def _verify_index(release_dir: Path, index_name: str, definition: str, expected_roles: set[str], *, public_key: Path) -> list[str]:
    release_dir = release_dir.resolve()
    index_path = release_dir / "release_local" / "signatures" / index_name
    if not index_path.is_file():
        return [f"缺少签名索引 {index_name}"]
    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"签名索引不可读：{exc}"]
    errors = validate_schema(definition, index)
    if errors:
        return [f"签名索引 schema：{item}" for item in errors]
    registry = json.loads(KEY_REGISTRY.read_text(encoding="utf-8"))
    if index["key_id"] != registry.get("active_key_id"):
        errors.append("签名索引 key_id 不是当前受信公钥")
    actual_roles = {entry["role"] for entry in index["subjects"]}
    if actual_roles != expected_roles or len(index["subjects"]) != len(expected_roles):
        errors.append("签名 subject 角色集合不完整或重复")
    for entry in index["subjects"]:
        paths = [release_dir / entry[name] for name in ("subject_path", "signature_path", "bundle_path")]
        if not all(_inside_release(path, release_dir) for path in paths):
            errors.append(f"{entry['role']} 签名路径越出 release 目录")
            continue
        subject, signature, bundle = paths
        if not all(path.is_file() for path in paths):
            missing = [str(path) for path in paths if not path.is_file()]
            errors.append(f"{entry['role']} subject/signature/bundle 缺失：{missing}")
            continue
        if sha256_file(subject) != entry["subject_sha256"] or subject.stat().st_size != entry["subject_size_bytes"]:
            errors.append(f"{entry['role']} subject 已改变")
            continue
        if sha256_file(signature) != entry["signature_sha256"]:
            errors.append(f"{entry['role']} detached signature 已改变")
        if sha256_file(bundle) != entry["bundle_sha256"]:
            errors.append(f"{entry['role']} bundle 已改变")
    if errors:
        return errors
    for entry in index["subjects"]:
        subject, signature, bundle = [release_dir / entry[name] for name in ("subject_path", "signature_path", "bundle_path")]
        try:
            verify_signature(subject, signature, bundle, public_key=public_key)
        except RuntimeError:
            errors.append(f"{entry['role']} Cosign 验签失败")
    return errors


def verify_candidate_signatures(release_dir: Path, *, public_key: Path = PUBLIC_KEY) -> list[str]:
    return _verify_index(
        release_dir,
        "CANDIDATE_SIGNATURES.json",
        "candidate_signature_index",
        {"release_manifest", "installer", "commercial_zip", "source_zip"},
        public_key=public_key,
    )


def verify_release_signatures(release_dir: Path, *, public_key: Path = PUBLIC_KEY) -> list[str]:
    return _verify_index(
        release_dir,
        "SIGNATURES.json",
        "signature_index",
        {"release_manifest", "installer", "commercial_zip", "source_zip", "final_decision"},
        public_key=public_key,
    )


def self_test() -> None:
    runtime_root = TOOLCACHE / "runtime"
    runtime_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=runtime_root) as temporary_dir:
        root = Path(temporary_dir)
        subject = root / "subject.txt"
        subject.write_text("Stocklens local signing self-test\n", encoding="utf-8")
        sign_and_verify(subject, root / "subject.sig", root / "subject.bundle.json")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--release-dir", default="")
    parser.add_argument("--verify-release", default="")
    args = parser.parse_args()
    selected = sum(bool(value) for value in (args.self_test, args.release_dir, args.verify_release))
    if selected != 1:
        parser.error("必须且只能指定 --self-test、--release-dir 或 --verify-release")
    if args.release_dir:
        index_path = sign_release(Path(args.release_dir))
        print(f"[OK] 候选五类 subject 已签名并回读验证：{index_path}")
        return 0
    if args.verify_release:
        errors = verify_release_signatures(Path(args.verify_release))
        if errors:
            print("[FAIL] " + "\n[FAIL] ".join(errors))
            return 1
        print("[OK] 候选五类 subject 签名验证通过")
        return 0
    key_id = ensure_key()
    self_test()
    print(f"[OK] Cosign 本地签名与验证通过；key_id={key_id}")
    print("[OK] 加密私钥与密码仅存 Windows 凭据管理器，runtime 临时文件已清理")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
