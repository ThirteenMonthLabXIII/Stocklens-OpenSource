"""观察任务、表现复盘和专业公式治理的共享工作流。

此模块刻意只编排公共业务合同，不依赖具体 UI 工具包。UI 传入行情/价格边界，
服务返回可以直接渲染或导出的普通记录，避免两个入口各自解释状态。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
import csv
import json
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from stock_tool.core.cache import CacheStore, TrackingTaskRecord
from stock_tool.core.models import StockQuote, fmt_number
from stock_tool.core.performance_loop import ObservationSettlementService, PerformanceStore
from stock_tool.core.professional_formula import propose_professional_shadow_candidate


class PriceSource(Protocol):
    def price_window(self, code: str, start_date: str, horizon_days: int): ...


@dataclass(frozen=True)
class WorkflowResult:
    ok: bool
    code: str
    message: str
    payload: dict[str, Any] | None = None


def _task_payload(task: TrackingTaskRecord | None) -> dict[str, Any]:
    if task is None:
        return {}
    payload = asdict(task)
    payload["created_at"] = task.created_at.isoformat(timespec="seconds")
    return payload


def _today() -> date:
    return datetime.now().date()


class ReviewWorkflowService:
    """复盘服务；审计分别写入缓存库和 V1 数据库。"""

    def __init__(self, cache: CacheStore, performance_store: PerformanceStore | None = None) -> None:
        self.cache = cache
        self.performance_store = performance_store

    # ---- tracking ----------------------------------------------------

    def create_tracking_task(
        self,
        *,
        code: str,
        name: str,
        task_type: str = "watch",
        entry_price: float | None = None,
        fail_price: float | None = None,
        target_price: float | None = None,
        horizon_days: int = 5,
        due_date: str = "",
        note: str = "",
        actor: str = "",
    ) -> WorkflowResult:
        clean_code = str(code or "").strip()
        if not (clean_code.isdigit() and len(clean_code) == 6):
            return WorkflowResult(False, "validation_error", "股票代码必须为 6 位数字。")
        if task_type not in {"watch", "holding"}:
            return WorkflowResult(False, "validation_error", "观察任务类型无效。")
        try:
            prices = {
                "entry_price": None if entry_price in (None, "") else float(entry_price),
                "fail_price": None if fail_price in (None, "") else float(fail_price),
                "target_price": None if target_price in (None, "") else float(target_price),
            }
        except (TypeError, ValueError):
            return WorkflowResult(False, "validation_error", "任务价格必须是大于 0 的数字。")
        if any(value is not None and value <= 0 for value in prices.values()):
            return WorkflowResult(False, "validation_error", "任务价格必须是大于 0 的数字。")
        if prices["fail_price"] is not None and prices["target_price"] is not None and prices["fail_price"] >= prices["target_price"]:
            return WorkflowResult(False, "validation_error", "失效价必须低于目标价。")
        if prices["entry_price"] is not None:
            if prices["fail_price"] is not None and prices["fail_price"] >= prices["entry_price"]:
                return WorkflowResult(False, "validation_error", "失效价必须低于参考价。")
            if prices["target_price"] is not None and prices["target_price"] <= prices["entry_price"]:
                return WorkflowResult(False, "validation_error", "目标价必须高于参考价。")
        try:
            days = max(1, int(horizon_days))
        except (TypeError, ValueError):
            return WorkflowResult(False, "validation_error", "观察周期必须是正整数。")
        due = due_date or (_today() + timedelta(days=days)).isoformat()
        try:
            datetime.strptime(due, "%Y-%m-%d")
        except ValueError:
            return WorkflowResult(False, "validation_error", "到期日格式必须为 YYYY-MM-DD。")
        task_id = self.cache.add_tracking_task(
            clean_code, str(name or clean_code).strip() or clean_code, task_type,
            prices["entry_price"], prices["fail_price"], prices["target_price"], days, due, note,
        )
        if task_id is None:
            return WorkflowResult(False, "duplicate_open_task", "该股票已有同类型进行中的观察任务。")
        task = next((item for item in self.cache.list_tracking_tasks(True) if item.id == task_id), None)
        self.cache.add_audit("tracking_task", task_id, "create", actor, after=_task_payload(task))
        return WorkflowResult(True, "created", "已创建观察任务。", _task_payload(task))

    def list_tracking_tasks(self, filters: Mapping[str, Any] | None = None) -> list[TrackingTaskRecord]:
        filters = filters or {}
        tasks = self.cache.list_tracking_tasks(include_closed=bool(filters.get("include_closed", True)))
        status = str(filters.get("status") or "")
        task_type = str(filters.get("task_type") or "")
        code = str(filters.get("code") or "")
        return [
            task for task in tasks
            if (not status or task.status == status)
            and (not task_type or task.task_type == task_type)
            and (not code or task.code == code)
        ]

    def review_tracking_task(self, task_id: int, quote: StockQuote | None, actor: str, *, now: date | None = None) -> WorkflowResult:
        task = next((item for item in self.cache.list_tracking_tasks(True) if item.id == int(task_id)), None)
        if task is None:
            return WorkflowResult(False, "not_found", "观察任务已不存在。")
        if task.status != "open":
            return WorkflowResult(True, "already_closed", "任务已结束，无需再次客观复核。", _task_payload(task))
        if quote is None or quote.price is None:
            return WorkflowResult(False, "missing_quote", "当前行情缺失，暂不能复核。", _task_payload(task))
        price = float(quote.price)
        review_date = now or _today()
        status, result, detail = "open", "观察中", f"当前价 {fmt_number(price, '元')}；继续观察。"
        # 固定优先级：失效 -> 目标 -> 到期。不可由 UI 擅自调整。
        if task.fail_price is not None and price <= task.fail_price:
            status, result, detail = "closed", "观察依据失效", f"当前价 {fmt_number(price, '元')} 已触发失效条件 {fmt_number(task.fail_price, '元')}。"
        elif task.target_price is not None and price >= task.target_price:
            status, result, detail = "closed", "达到目标观察条件", f"当前价 {fmt_number(price, '元')} 已达到目标观察价 {fmt_number(task.target_price, '元')}。"
        else:
            try:
                due = datetime.strptime(task.due_date, "%Y-%m-%d").date()
            except ValueError:
                return WorkflowResult(False, "invalid_due_date", "任务到期日格式无效，未改动原状态。", _task_payload(task))
            if review_date >= due:
                status, result, detail = "closed", "到期未触发", "观察周期已到期，未触发目标或失效条件。"
        if not self.cache.update_tracking_task_if_open(task.id, status, result, detail):
            updated = next((item for item in self.cache.list_tracking_tasks(True) if item.id == task.id), task)
            return WorkflowResult(True, "already_closed", "任务已由其他进程复核，无需重复改动。", _task_payload(updated))
        updated = next(item for item in self.cache.list_tracking_tasks(True) if item.id == task.id)
        self.cache.add_audit("tracking_task", task.id, "review", actor, before=_task_payload(task), after=_task_payload(updated), reason=detail)
        return WorkflowResult(True, "reviewed", detail, _task_payload(updated))

    def review_due_tasks(
        self,
        quote_provider: Callable[[str], StockQuote | None] | Mapping[str, StockQuote | None],
        actor: str,
        *,
        now: date | None = None,
    ) -> list[WorkflowResult]:
        results: list[WorkflowResult] = []
        for task in self.list_tracking_tasks({"status": "open", "include_closed": False}):
            try:
                quote = quote_provider.get(task.code) if isinstance(quote_provider, Mapping) else quote_provider(task.code)
            except Exception:
                quote = None
            results.append(self.review_tracking_task(task.id, quote, actor, now=now))
        return results

    def append_tracking_note(self, task_id: int, note: str, actor: str) -> WorkflowResult:
        task = next((item for item in self.cache.list_tracking_tasks(True) if item.id == int(task_id)), None)
        if task is None:
            return WorkflowResult(False, "not_found", "观察任务已不存在。")
        extra = str(note or "").strip()
        if not extra:
            return WorkflowResult(False, "validation_error", "人工备注不能为空。")
        appended = (task.last_review + "\n" if task.last_review else "") + f"人工备注：{extra}"
        self.cache.update_tracking_task(task.id, task.status, task.result, appended)
        updated = next(item for item in self.cache.list_tracking_tasks(True) if item.id == task.id)
        self.cache.add_audit("tracking_task", task.id, "append_note", actor, before=_task_payload(task), after=_task_payload(updated), reason=extra)
        return WorkflowResult(True, "noted", "已追加人工备注；客观结论未修改。", _task_payload(updated))

    def delete_tracking_task(self, task_id: int, actor: str, reason: str) -> WorkflowResult:
        task = next((item for item in self.cache.list_tracking_tasks(True) if item.id == int(task_id)), None)
        if task is None:
            return WorkflowResult(False, "not_found", "观察任务已不存在。")
        self.cache.add_audit("tracking_task", task.id, "delete", actor, before=_task_payload(task), reason=reason)
        self.cache.delete_tracking_task(task.id)
        return WorkflowResult(True, "deleted", "观察任务已删除。")

    def export_tracking_rows(self, filters: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
        return [
            {
                "ID": task.id, "代码": task.code, "名称": task.name, "类型": task.task_type,
                "状态": task.status, "结论": task.result, "到期日": task.due_date,
                "触发价": task.entry_price, "失效价": task.fail_price, "目标价": task.target_price,
                "说明": task.last_review, "备注": task.note,
            }
            for task in self.list_tracking_tasks(filters)
        ]

    # ---- performance -------------------------------------------------

    def _require_performance(self) -> PerformanceStore:
        if self.performance_store is None:
            raise RuntimeError("表现复盘数据库不可用")
        return self.performance_store

    def settle_due_samples(self, asof_date: str, price_source: PriceSource) -> int:
        return ObservationSettlementService(self._require_performance(), price_source).settle_due_samples(asof_date=asof_date)

    def continue_run_observation(
        self,
        run_id: int,
        *,
        top_n: int,
        horizons: tuple[int, ...],
        ai_rank_map: Mapping[str, Any] | None = None,
    ) -> int:
        """为既有筛选运行补样本；UI 不直接接触表现库写入口。"""
        return self._require_performance().create_observation_samples_for_run(
            int(run_id), top_n=top_n, horizons=horizons, ai_rank_map=ai_rank_map or {},
        )

    def dashboard_snapshot(self, filters: Mapping[str, Any] | None = None):
        filters = filters or {}
        return self._require_performance().dashboard_snapshot(asof_date=filters.get("asof_date"), recent_days=int(filters.get("recent_days") or 60))

    def performance_summary(self, filters: Mapping[str, Any] | None = None):
        return self._require_performance().summarize(**self._performance_filters(filters))

    def performance_groups(self, group_by: str, filters: Mapping[str, Any] | None = None):
        return self._require_performance().grouped_summary(group_by, **self._performance_filters(filters))

    def performance_details(self, filters: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
        safe = self._performance_filters(filters)
        return self._require_performance().list_recent_outcomes(limit=int((filters or {}).get("limit") or 500), **safe)

    @staticmethod
    def _performance_filters(filters: Mapping[str, Any] | None) -> dict[str, Any]:
        filters = filters or {}
        # PerformanceStore 支持的过滤字段；其余字段保留给 UI 二次筛选，避免伪造 SQL 条件。
        names = {
            "start_date", "end_date", "horizon_days", "regime", "weight_policy_version", "template_id",
            "code", "stock_name", "industry", "strategy_id", "strategy_name", "strategy_version",
            "data_quality", "candidate_type", "sample_class", "evaluation_mode",
        }
        return {name: value for name, value in filters.items() if name in names and value not in (None, "")}

    def export_performance_rows(self, filters: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
        return self.performance_details(filters)

    @staticmethod
    def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
        """UTF-8 BOM + 同目录替换，任何写入失败都不改变目标文件。"""
        path = Path(path)
        fields = list(rows[0]) if rows else []
        temporary = path.with_suffix(path.suffix + ".tmp")
        try:
            with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                if fields:
                    writer.writeheader()
                    writer.writerows(rows)
            temporary.replace(path)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise

    # ---- formula governance -----------------------------------------

    def list_formula_state(self) -> dict[str, Any]:
        store = self._require_performance()
        return {"active": store.get_active_professional_formula(), "candidates": store.list_professional_formula_candidates(limit=50), "audit": self.list_governance_audit()}

    def generate_shadow_candidate(self, payload: dict[str, Any] | None = None, **kwargs: Any) -> WorkflowResult:
        store = self._require_performance()
        try:
            evidence = store.build_professional_evidence_package(persist_ranges=True)
            candidate_payload = payload or propose_professional_shadow_candidate(evidence)
            candidate_id, validation = store.save_validated_professional_candidate(candidate_payload, evidence_package=evidence, provider=str(kwargs.get("provider") or "local"), model=str(kwargs.get("model") or ""))
        except Exception as exc:
            self._audit_formula("generate_rejected", str(kwargs.get("actor") or ""), reason=str(exc))
            return WorkflowResult(False, "generation_failed", f"候选生成或验证失败：{exc}")
        self._audit_formula("generate", str(kwargs.get("actor") or ""), candidate_id, after=validation)
        return WorkflowResult(True, "generated", "已生成专业公式观察候选。", {"candidate_id": candidate_id, "validation": validation})

    def save_shadow_candidate(
        self, payload: dict[str, Any], evidence: dict[str, Any], *, actor: str, provider: str, model: str, cost_note: str = "",
    ) -> WorkflowResult:
        """保存已由 UI 边界取得的 AI/本地候选，治理写入仍只在此服务。"""
        try:
            candidate_id, validation = self._require_performance().save_validated_professional_candidate(
                payload, evidence_package=evidence, provider=provider, model=model, cost_note=cost_note,
            )
        except Exception as exc:
            self._audit_formula("generate_rejected", actor, reason=str(exc))
            return WorkflowResult(False, "generation_failed", f"候选验证失败：{exc}")
        self._audit_formula("generate", actor, candidate_id, after=validation)
        return WorkflowResult(True, "generated", "已生成专业公式观察候选。", {"candidate_id": candidate_id, "validation": validation})

    def next_suggested_candidate(self) -> dict[str, Any] | None:
        return next(iter(self._require_performance().list_professional_formula_candidates(status="suggest_enable", limit=1)), None)

    def continue_observation(self, candidate_id: int, actor: str, reason: str) -> WorkflowResult:
        if not str(reason or "").strip():
            return WorkflowResult(False, "reason_required", "继续观察需要填写理由。")
        store = self._require_performance()
        candidate = next((item for item in store.list_professional_formula_candidates(limit=200) if item["id"] == int(candidate_id)), None)
        if candidate is None:
            return WorkflowResult(False, "not_found", "公式候选已不存在。")
        with store.database.connect() as conn:
            conn.execute("UPDATE professional_formula_candidates SET status = 'shadow', updated_at = ? WHERE id = ?", (datetime.now().isoformat(timespec="seconds"), int(candidate_id)))
        self._audit_formula("continue_observation", actor, candidate_id, before=candidate, reason=reason)
        return WorkflowResult(True, "shadow", "候选将继续观察，不会覆盖 active。")

    def activate_candidate(self, candidate_id: int, actor: str, reason: str, *, confirmed: bool = False) -> WorkflowResult:
        if not confirmed:
            return WorkflowResult(False, "confirmation_required", "启用需要二次确认。")
        if not str(reason or "").strip():
            return WorkflowResult(False, "reason_required", "启用需要填写理由。")
        store = self._require_performance()
        candidate = next((item for item in store.list_professional_formula_candidates(limit=200) if item["id"] == int(candidate_id)), None)
        if candidate is None:
            return WorkflowResult(False, "not_found", "公式候选已不存在。")
        if candidate.get("status") != "suggest_enable":
            return WorkflowResult(False, "activation_rejected", "候选未满足样本量、质量或多周期启用条件。")
        before = store.get_active_professional_formula()
        active = store.activate_professional_formula_candidate(int(candidate_id))
        self._audit_formula("activate", actor, candidate_id, before=before, after=active, reason=reason)
        return WorkflowResult(True, "activated", "已启用经人工确认的公式版本。", active)

    def rollback_previous_active(self, actor: str, reason: str, *, confirmed: bool = False) -> WorkflowResult:
        if not confirmed:
            return WorkflowResult(False, "confirmation_required", "回滚需要二次确认。")
        if not str(reason or "").strip():
            return WorkflowResult(False, "reason_required", "回滚需要填写理由。")
        store = self._require_performance()
        current = store.get_active_professional_formula()
        with store.database.connect() as conn:
            row = conn.execute(
                "SELECT * FROM professional_formula_candidates WHERE status = 'archived' AND id != ? ORDER BY activated_at DESC, id DESC LIMIT 1",
                (int(current["id"]),),
            ).fetchone()
            if row is None:
                return WorkflowResult(False, "no_previous_active", "没有可回滚的已归档 active 版本。")
            previous_id = int(row["id"])
            now = datetime.now().isoformat(timespec="seconds")
            conn.execute("UPDATE professional_formula_candidates SET status = 'archived', updated_at = ? WHERE id = ?", (now, int(current["id"])))
            conn.execute("UPDATE professional_formula_candidates SET status = 'active', activated_at = ?, updated_at = ? WHERE id = ?", (now, now, previous_id))
        restored = store.get_active_professional_formula()
        self._audit_formula("rollback", actor, previous_id, before=current, after=restored, reason=reason)
        return WorkflowResult(True, "rolled_back", "已回滚到最近一版已归档 active。", restored)

    def _audit_formula(self, action: str, actor: str, candidate_id: int | None = None, *, before: Any = None, after: Any = None, reason: str = "") -> None:
        store = self._require_performance()
        with store.database.connect() as conn:
            conn.execute(
                """INSERT INTO formula_governance_audit(candidate_id, action, actor, before_json, after_json, reason, created_at)
                   VALUES(?, ?, ?, ?, ?, ?, ?)""",
                (candidate_id, action, actor or "unknown", json.dumps(before or {}, ensure_ascii=False, default=str), json.dumps(after or {}, ensure_ascii=False, default=str), reason, datetime.now().isoformat(timespec="seconds")),
            )

    def list_governance_audit(self) -> list[dict[str, Any]]:
        store = self._require_performance()
        with store.database.connect() as conn:
            rows = conn.execute("SELECT * FROM formula_governance_audit ORDER BY id DESC LIMIT 500").fetchall()
        return [dict(row) for row in rows]
