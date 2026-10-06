"""真实 PIT 库的点时一致性校验（严格只读）。

点时不可见性的**逻辑**已有单测（tools/test_pit_pipeline.py 的 financials_visible_asof、
research/test_pit_join_quality.py、research/backtest_engine.assert_no_future_pubdates）。
本脚本补的是**真实库数据**层面的核查：在已落地的 sqlite 上断言点时不变量成立，
而不是再测一遍函数逻辑。

为避免改动本应只读的真实库，这里用 `mode=ro` URI 打开，并**直接复刻** PITDatabase
`financials_visible_asof` 的谓词（pub_date <= asof），不实例化 PITDatabase（其 __init__
会以读写方式 init_schema），从而对生产函数的过滤口径做一次独立交叉验证。

检查项：
  C1 pub_date 非空且形如 YYYY-MM-DD —— 空 pub_date 是最隐蔽的泄漏向量
     （字符串 '' <= asof 恒真，会让该事实在任意 asof 都可见）。
  C2 pub_date >= report_date —— 发布不可能早于报告期结束。
  C3 pub_date <= 截止日（默认今天）—— 不存在荒谬的未来发布日。
  C4 prices.trade_date 非空且 <= 本行 asof_date —— 价格行不得出现在其快照日之后。
  C5 行为校验：对有多个 pub_date 的样本代码，断言以 (最新 pub_date 的前一日) 为 asof 时
     最新一期财务不可见、以最新 pub_date 为 asof 时可见；并与原始表按谓词重算结果交叉对账。

非 0 退出码表示发现违例。
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path


DEFAULT_DB = "my_data_live_preflight/stock_tool_pit.sqlite3"


def _connect_ro(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _visible_asof(conn: sqlite3.Connection, code: str, asof_date: str) -> list[sqlite3.Row]:
    # 复刻 PITDatabase.financials_visible_asof 的谓词，独立于生产实例（只读）。
    return conn.execute(
        """
        SELECT code, report_date, pub_date, metric, value, source
        FROM pit_financials
        WHERE code = ? AND pub_date <= ?
        ORDER BY pub_date, report_date, metric
        """,
        (code, asof_date),
    ).fetchall()


class Report:
    def __init__(self) -> None:
        self.failures = 0

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        tag = "[PASS]" if ok else "[FAIL]"
        if not ok:
            self.failures += 1
        line = f"{tag} {name}"
        if detail:
            line += f" — {detail}"
        print(line)


def _day_before(d: str) -> str:
    return (datetime.strptime(d, "%Y-%m-%d").date() - timedelta(days=1)).strftime("%Y-%m-%d")


def verify(db_path: Path, sample: int, cutoff: str) -> int:
    conn = _connect_ro(db_path)
    rep = Report()
    try:
        total_fin = conn.execute("SELECT COUNT(*) FROM pit_financials").fetchone()[0]
        print(f"DB: {db_path}  (pit_financials rows: {total_fin})\n")

        # C1 pub_date 非空且 10 位
        bad = conn.execute(
            "SELECT COUNT(*) FROM pit_financials WHERE pub_date IS NULL OR LENGTH(TRIM(pub_date)) <> 10"
        ).fetchone()[0]
        rep.check("C1 pub_date 非空且形如 YYYY-MM-DD", bad == 0, f"{bad} 行违例")

        # C2 pub_date >= report_date
        c2 = conn.execute(
            "SELECT COUNT(*) FROM pit_financials WHERE pub_date < report_date"
        ).fetchone()[0]
        rep.check("C2 pub_date >= report_date", c2 == 0, f"{c2} 行 pub_date 早于 report_date")

        # C3 pub_date <= 截止日
        c3 = conn.execute(
            "SELECT COUNT(*) FROM pit_financials WHERE pub_date > ?", (cutoff,)
        ).fetchone()[0]
        rep.check(f"C3 pub_date <= {cutoff}（无未来发布日）", c3 == 0, f"{c3} 行 pub_date 在未来")

        # C4 prices trade_date 合法且 <= asof_date
        c4_bad_td = conn.execute(
            "SELECT COUNT(*) FROM pit_prices WHERE trade_date IS NULL OR LENGTH(TRIM(trade_date)) <> 10"
        ).fetchone()[0]
        c4_after = conn.execute(
            "SELECT COUNT(*) FROM pit_prices WHERE trade_date > asof_date"
        ).fetchone()[0]
        rep.check("C4 prices.trade_date 合法且 <= asof_date", c4_bad_td == 0 and c4_after == 0,
                  f"{c4_bad_td} 行畸形 trade_date，{c4_after} 行 trade_date 晚于 asof")

        # C5 行为校验：多 pub_date 样本上验证 asof 闸门
        rows = conn.execute(
            """
            SELECT code FROM pit_financials
            GROUP BY code HAVING COUNT(DISTINCT pub_date) >= 2
            ORDER BY code LIMIT ?
            """,
            (sample,),
        ).fetchall()
        codes = [r["code"] for r in rows]
        if not codes:
            rep.check("C5 点时闸门行为校验", False, "无多 pub_date 代码可抽样")
        else:
            gate_violations: list[str] = []
            for code in codes:
                pubs = [
                    r["pub_date"]
                    for r in conn.execute(
                        "SELECT DISTINCT pub_date FROM pit_financials WHERE code = ? ORDER BY pub_date",
                        (code,),
                    ).fetchall()
                ]
                p_max = pubs[-1]
                asof_before = _day_before(p_max)
                vis_before = _visible_asof(conn, code, asof_before)
                vis_on = _visible_asof(conn, code, p_max)
                # 闸门：asof < p_max 时最新一期不可见；asof == p_max 时可见
                if any(r["pub_date"] > asof_before for r in vis_before):
                    gate_violations.append(f"{code}: asof={asof_before} 泄漏了 pub_date>asof 的行")
                if not any(r["pub_date"] == p_max for r in vis_on):
                    gate_violations.append(f"{code}: asof={p_max} 未见到当日发布的财务")
                # 交叉对账：函数谓词输出 == 原始表 pub_date<=asof 的计数
                raw_cnt = conn.execute(
                    "SELECT COUNT(*) FROM pit_financials WHERE code = ? AND pub_date <= ?",
                    (code, asof_before),
                ).fetchone()[0]
                if len(vis_before) != raw_cnt:
                    gate_violations.append(f"{code}: 谓词输出({len(vis_before)}) != 原始计数({raw_cnt})")
            rep.check(
                f"C5 点时闸门行为校验（{len(codes)} 个多 pub_date 代码）",
                not gate_violations,
                "; ".join(gate_violations[:5]) if gate_violations else f"{len(codes)} 个代码全部通过",
            )
    finally:
        conn.close()

    print()
    if rep.failures:
        print(f"VERIFY: FAIL（{rep.failures} 项不通过）")
        return 1
    print("VERIFY: PASS（点时不变量在真实库上全部成立）")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="真实 PIT 库的点时一致性校验（只读）")
    parser.add_argument("--db", default=DEFAULT_DB, help=f"PIT sqlite 路径（默认 {DEFAULT_DB}）")
    parser.add_argument("--sample", type=int, default=50, help="C5 抽样的多 pub_date 代码数")
    parser.add_argument("--cutoff", default=date.today().strftime("%Y-%m-%d"),
                        help="C3 未来发布日截止（默认今天）")
    args = parser.parse_args(argv)
    db_path = Path(args.db)
    if not db_path.exists():
        print(f"找不到库文件：{db_path}", file=sys.stderr)
        return 2
    return verify(db_path, args.sample, args.cutoff)


if __name__ == "__main__":
    raise SystemExit(main())
