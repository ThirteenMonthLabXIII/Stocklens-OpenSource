from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "stock_tool" / "config" / "release_evidence_chain.schema.json"
RELEASE_PYTHON = ROOT / ".toolcache" / "release-venv" / "Scripts" / "python.exe"


def _validate_with_release_python(definition: str, payload: Any) -> list[str]:
    if not RELEASE_PYTHON.is_file() or Path(sys.executable).resolve() == RELEASE_PYTHON.resolve():
        return ["<root>: 缺少 jsonschema 4.26.0 严格验证环境"]
    request = json.dumps({"definition": definition, "payload": payload}, ensure_ascii=False)
    script = (
        "import json,sys; "
        "from tools.release_schema import validate_schema; "
        "request=json.load(sys.stdin); "
        "print(json.dumps(validate_schema(request['definition'], request['payload']), ensure_ascii=False))"
    )
    completed = subprocess.run(
        [str(RELEASE_PYTHON), "-c", script],
        cwd=ROOT,
        input=request,
        text=True,
        capture_output=True,
        timeout=60,
    )
    if completed.returncode != 0:
        detail = ((completed.stderr or "") + "\n" + (completed.stdout or "")).strip()[-1000:]
        return [f"<root>: 严格 schema 验证器执行失败：{detail or 'unknown error'}"]
    try:
        errors = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return ["<root>: 严格 schema 验证器返回了无效结果"]
    if not isinstance(errors, list) or any(not isinstance(item, str) for item in errors):
        return ["<root>: 严格 schema 验证器返回了非法结构"]
    return errors


def validate_schema(definition: str, payload: Any) -> list[str]:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    try:
        from jsonschema import Draft202012Validator
    except ModuleNotFoundError:
        return _validate_with_release_python(definition, payload)
    validator = Draft202012Validator(
        {
            "$schema": schema["$schema"],
            "$defs": schema["$defs"],
            "$ref": f"#/$defs/{definition}",
        }
    )
    return [f"{'/'.join(map(str, error.absolute_path)) or '<root>'}: {error.message}" for error in sorted(validator.iter_errors(payload), key=lambda item: list(item.absolute_path))]


def require_schema(definition: str, payload: Any) -> None:
    errors = validate_schema(definition, payload)
    if errors:
        raise RuntimeError(f"{definition} JSON Schema 校验失败：\n" + "\n".join(errors))
