"""Clean Phase B self-test artifacts under research/ only."""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research.panel_db import now_iso

RESEARCH_DIR = Path(__file__).resolve().parent
OUT_DIR = RESEARCH_DIR / "out"
PANEL_DB = RESEARCH_DIR / "data" / "panel.sqlite3"


def collect_targets() -> list[Path]:
    targets: list[Path] = []
    targets.extend(RESEARCH_DIR.glob("data/panel.sqlite3*"))
    for pattern in ("data/*selftest*.sqlite3*", "data/*selftest*.md", "data/*selftest*.json"):
        targets.extend(RESEARCH_DIR.glob(pattern))
    targets.extend(RESEARCH_DIR.rglob("__pycache__"))
    return sorted({p for p in targets if p.exists()})


def old_panel_summary() -> dict[str, object]:
    summary: dict[str, object] = {
        "generated_at": now_iso(),
        "panel_path": str(PANEL_DB),
        "exists": PANEL_DB.exists(),
        "tables": {},
        "note": "旧库摘要用于说明清理前状态；Phase B 验收以清理后当前代码重跑结果为准。",
    }
    if not PANEL_DB.exists():
        return summary
    try:
        conn = sqlite3.connect(str(PANEL_DB))
        try:
            tables = [
                row[0]
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
            ]
            for table in tables:
                if table.startswith("sqlite_"):
                    continue
                count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                summary["tables"][table] = int(count)  # type: ignore[index]
        finally:
            conn.close()
    except sqlite3.Error as exc:
        summary["error"] = str(exc)
    return summary


def clean(apply: bool = False) -> dict[str, object]:
    summary = old_panel_summary()
    if apply:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        (OUT_DIR / "phase_b_old_panel_summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    removed: list[str] = []
    blocked: list[dict[str, str]] = []
    for path in collect_targets():
        try:
            if apply:
                if path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink()
            removed.append(str(path))
        except OSError as exc:
            blocked.append({"path": str(path), "error": str(exc)})
    return {
        "mode": "apply" if apply else "dry_run",
        "old_panel_summary": summary,
        "targets": removed,
        "blocked": blocked,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Clean Phase B research-only temporary artifacts.")
    parser.add_argument("--apply", action="store_true", help="delete targets; default is dry-run")
    args = parser.parse_args(argv)
    result = clean(apply=args.apply)
    print(f"mode={result['mode']}")
    for path in result["targets"]:
        print(path)
    for item in result["blocked"]:
        print(f"BLOCKED {item['path']}: {item['error']}")
    return 1 if result["blocked"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
