from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from stock_tool.core.pit_database import PITDatabase, PITJobRecord, now_iso, today_iso
from stock_tool.core.pit_providers import PITProvider, ProviderUnavailable, TusharePITProvider, classify_provider_error, fetch_akshare_universe
from stock_tool.core.pit_protection import ProtectionEvent, SourceCircuitOpen


ProgressCallback = Callable[[int, int, str], None]


@dataclass(frozen=True)
class PITBuildResult:
    job_id: int
    status: str
    message: str
    report_path: str
    done: int
    failed: int


class PITDatasetBuilder:
    def __init__(
        self,
        database: PITDatabase,
        provider: PITProvider,
        batch_size: int = 50,
        *,
        tushare_token: str | None = None,
        fallback_providers: dict[str, PITProvider] | None = None,
    ) -> None:
        self.database = database
        self.provider = provider
        self.batch_size = max(1, int(batch_size or 1))
        self.tushare_token = tushare_token
        self.fallback_providers = fallback_providers or {}

    def run(
        self,
        *,
        mode: str = "full",
        start_date: str = "2019-01-01",
        end_date: str = "today",
        stock_limit: int | None = None,
        refresh_existing: bool = False,
        universe_source: str = "auto",
        progress: ProgressCallback | None = None,
    ) -> PITBuildResult:
        end_date = _resolve_end_date(end_date)
        start_date = _resolve_start_date(start_date, mode, end_date)
        if mode not in {"healthcheck", "preflight", "full", "incremental", "retry-failures"}:
            raise ValueError(f"unsupported PIT build mode: {mode}")
        if universe_source not in {"auto", "baostock", "tushare", "akshare", "cache"}:
            raise ValueError(f"unsupported universe source: {universe_source}")
        if mode == "preflight" and stock_limit is None:
            stock_limit = 50
        protection_profile = str(getattr(self.provider, "protection_profile_name", ""))
        job_id = self.database.create_job(
            mode,
            start_date,
            end_date,
            params={
                "provider": self.provider.name,
                "source_version": self.provider.source_version,
                "batch_size": self.batch_size,
                "stock_limit": stock_limit,
                "refresh_existing": bool(refresh_existing),
                "protection_profile": protection_profile,
                "universe_source": universe_source,
                "healthcheck_status": "",
                "latest_trade_day": "",
                "universe_method": "",
                "universe_rows": 0,
                "worker_restarts": 0,
            },
        )
        self._set_provider_event_sink(job_id)
        try:
            status, message = self.provider.available_status()
            if status != "GO":
                self.database.log_fetch(job_id=job_id, stage="provider", source=self.provider.name, status="failed", error_class=status, message=message)
                if mode == "healthcheck":
                    self.database.update_job_params(job_id, {"healthcheck_status": status})
                self.database.update_job(job_id, status=status, message=message, completed=True)
                report_path = self._write_quality_report(job_id, start_date, end_date)
                self.database.update_job(job_id, report_path=report_path)
                return self._result(job_id)
            if mode == "healthcheck":
                self._run_healthcheck(job_id, start_date, end_date, progress)
            elif mode == "retry-failures":
                self._retry_failures(job_id, start_date, end_date, progress)
            else:
                self._run_full_or_incremental(job_id, mode, start_date, end_date, stock_limit, refresh_existing, universe_source, progress)
            stats = self.database.stats()
            current_job = _find_job(self.database.list_recent_jobs(limit=50), job_id)
            counts = self.database.job_log_counts(job_id)
            failure_rate = _failure_rate_from_counts(counts)
            if mode == "healthcheck":
                final_status = "completed"
            elif mode == "preflight":
                passed = _preflight_passed(counts, failure_rate)
                final_status = "completed" if passed else "preflight_failed"
            else:
                final_status = "completed_with_errors" if current_job and current_job.failed else "completed"
            message = _completion_message(mode, stats.price_rows, stats.financial_rows, stats.failed_logs, counts, failure_rate)
            self.database.update_job(
                job_id,
                status=final_status,
                stage="quality_report",
                done=current_job.total if current_job else None,
                message=message,
                completed=True,
            )
            report_path = self._write_quality_report(job_id, start_date, end_date)
            self.database.update_job(job_id, report_path=report_path)
            return self._result(job_id)
        except SourceCircuitOpen as exc:
            error_class = classify_provider_error(exc)
            self.database.log_fetch(
                job_id=job_id,
                stage="pipeline",
                source=self.provider.name,
                status="failed",
                error_class=error_class,
                message=str(exc),
            )
            self.database.update_job(job_id, status=error_class, message=str(exc), completed=True)
            report_path = self._write_quality_report(job_id, start_date, end_date)
            self.database.update_job(job_id, report_path=report_path)
            return self._result(job_id)
        except Exception as exc:
            error_class = classify_provider_error(exc)
            self.database.log_fetch(
                job_id=job_id,
                stage="pipeline",
                source=self.provider.name,
                status="failed",
                error_class=error_class,
                message=str(exc),
            )
            self.database.update_job(job_id, status=error_class, message=str(exc), completed=True)
            report_path = self._write_quality_report(job_id, start_date, end_date)
            self.database.update_job(job_id, report_path=report_path)
            return self._result(job_id)
        finally:
            self._set_provider_event_sink(None)
            self._close_provider()

    def _run_full_or_incremental(
        self,
        job_id: int,
        mode: str,
        start_date: str,
        end_date: str,
        stock_limit: int | None,
        refresh_existing: bool,
        universe_source: str,
        progress: ProgressCallback | None,
    ) -> None:
        universe = self._run_stage(
            job_id,
            "universe",
            end_date,
            start_date,
            end_date,
            lambda: self._fetch_universe(job_id, mode, start_date, end_date, universe_source),
        )
        codes = sorted({str(row.get("code", "")).zfill(6) for row in universe if row.get("code")})
        codes = _deterministic_sample(codes, stock_limit)
        total = max(1, len(codes) * 2 + 2)
        self.database.update_job(job_id, total=total, done=1, stage="universe", message=f"universe {len(codes)} selected stocks")
        _progress(progress, 1, total, f"universe {len(codes)} selected stocks")

        done = 1
        for code in codes:
            if not refresh_existing and self.database.has_price_rows(code, start_date, end_date, self.provider.name):
                done += 1
                self._log_skip(job_id, "prices", code, start_date, end_date, "existing prices")
                self.database.update_job(job_id, done=done, stage="prices", message=f"skip existing prices {code}")
                _progress(progress, done, total, f"skip existing prices {code}")
                continue
            rows = self._safe_fetch(job_id, "prices", code, start_date, end_date, lambda code=code: self.provider.fetch_prices(code, start_date, end_date))
            if rows:
                self.database.upsert_prices(rows)
            done += 1
            self.database.update_job(job_id, done=done, stage="prices", message=f"prices {code}: {len(rows)} rows")
            _progress(progress, done, total, f"prices {code}: {len(rows)} rows")

        for code in codes:
            if not refresh_existing and self.database.has_successful_fetch_state("financials", code, self.provider.name, start_date, end_date):
                done += 1
                self._log_skip(job_id, "financials", code, start_date, end_date, "successful financial interval")
                self.database.update_job(job_id, done=done, stage="financials", message=f"skip existing financials {code}")
                _progress(progress, done, total, f"skip existing financials {code}")
                continue
            rows = self._safe_fetch(job_id, "financials", code, start_date, end_date, lambda code=code: self.provider.fetch_financials(code, start_date, end_date))
            if rows:
                self.database.upsert_financials(rows)
            done += 1
            self.database.update_job(job_id, done=done, stage="financials", message=f"financials {code}: {len(rows)} rows")
            _progress(progress, done, total, f"financials {code}: {len(rows)} rows")

        # industry 的 asof 必须复用 universe 已解析的交易日，否则两表 asof 错位（universe 走
        # 交易日回退、industry 用请求日），会让 _industry_coverage 同 asof 强匹配假阴性为 0。
        industry_asof = (universe[0].get("asof_date") if universe else "") or end_date
        industry = self._run_stage(job_id, "industry", industry_asof, start_date, industry_asof, lambda: self.provider.fetch_industry(industry_asof))
        self.database.update_job(job_id, done=total - 1, stage="industry", message=f"industry {len(industry)} rows")
        _progress(progress, total - 1, total, f"industry {len(industry)} rows")

    def _run_healthcheck(self, job_id: int, start_date: str, end_date: str, progress: ProgressCallback | None) -> None:
        self.database.update_job(job_id, total=1, done=0, stage="healthcheck", message="running healthcheck")
        _progress(progress, 0, 1, "running healthcheck")
        started = time.perf_counter()
        try:
            healthcheck = getattr(self.provider, "healthcheck", None)
            if healthcheck is None:
                status, message = self.provider.available_status()
                if status != "GO":
                    raise ProviderUnavailable(message, status)
                payload = {"healthcheck_status": "GO", "available_status": status, "available_message": message}
            else:
                payload = healthcheck(start_date, end_date)
            updates = {
                "healthcheck_status": payload.get("healthcheck_status", "GO"),
                "latest_trade_day": payload.get("latest_trade_day", ""),
                "universe_method": payload.get("universe_method", ""),
                "universe_rows": int(payload.get("universe_rows") or 0),
                "worker_restarts": int(payload.get("worker_restarts") or getattr(self.provider, "worker_restarts", 0) or 0),
            }
            self.database.update_job_params(job_id, updates)
            self.database.log_fetch(
                job_id=job_id,
                stage="healthcheck",
                target=self.provider.name,
                source=self.provider.name,
                status="healthcheck_success",
                message=json.dumps(payload, ensure_ascii=False)[:800],
                duration_ms=_elapsed_ms(started),
            )
            self.database.update_job(job_id, done=1, stage="healthcheck", message="healthcheck passed")
            _progress(progress, 1, 1, "healthcheck passed")
        except Exception as exc:
            error_class = classify_provider_error(exc)
            self.database.update_job_params(
                job_id,
                {
                    "healthcheck_status": error_class,
                    "worker_restarts": int(getattr(self.provider, "worker_restarts", 0) or 0),
                },
            )
            self.database.log_fetch(
                job_id=job_id,
                stage="healthcheck",
                target=self.provider.name,
                source=self.provider.name,
                status="healthcheck_failed",
                error_class=error_class,
                message=str(exc),
                duration_ms=_elapsed_ms(started),
            )
            self.database.update_job(job_id, failed_delta=1, stage="healthcheck", message=f"healthcheck failed: {exc}")
            raise

    def _fetch_universe(self, job_id: int, mode: str, start_date: str, end_date: str, universe_source: str) -> list[dict[str, Any]]:
        if mode == "preflight" and universe_source == "cache":
            raise ProviderUnavailable("preflight does not allow cache universe; run live universe check instead", "SOURCE_UNREACHABLE")

        if mode in {"full", "incremental"} and universe_source in {"auto", "cache"}:
            if self.database.has_recent_successful_preflight(self.provider.name, start_date, end_date, max_age_hours=24):
                cached = self.database.latest_universe_rows()
                if cached:
                    self.database.log_fetch(
                        job_id=job_id,
                        stage="universe",
                        target=end_date,
                        source="cache",
                        status="universe_fallback",
                        message=f"using cached universe rows after recent preflight: {len(cached)} rows",
                    )
                    self.database.update_job_params(
                        job_id,
                        {
                            "universe_source": "cache",
                            "universe_method": "cache.latest_universe_rows",
                            "latest_trade_day": cached[0].get("asof_date", ""),
                            "universe_rows": len(cached),
                            "worker_restarts": int(getattr(self.provider, "worker_restarts", 0) or 0),
                        },
                    )
                    return cached
            if universe_source == "cache":
                raise ProviderUnavailable("no recent successful preflight universe cache is available", "SOURCE_UNREACHABLE")

        if universe_source == "auto":
            sources = [self.provider.name]
            if self.provider.name == "baostock":
                sources.append("tushare")
                sources.append("akshare")
        else:
            sources = [universe_source]

        last_error: Exception | None = None
        for index, source in enumerate(sources):
            try:
                if index > 0:
                    self.database.log_fetch(
                        job_id=job_id,
                        stage="universe",
                        target=end_date,
                        source=source,
                        status="universe_fallback",
                        message=f"trying universe fallback after {last_error}",
                    )
                rows, provider = self._fetch_universe_from_source(source, end_date)
                self.database.update_job_params(
                    job_id,
                    {
                        "universe_source": source,
                        "universe_method": str(getattr(provider, "universe_method", "") or f"{source}.fetch_universe"),
                        "latest_trade_day": str(getattr(provider, "latest_trade_day_value", "") or ""),
                        "universe_rows": len(rows),
                        "worker_restarts": int(getattr(self.provider, "worker_restarts", 0) or 0),
                    },
                )
                return rows
            except SourceCircuitOpen:
                raise
            except Exception as exc:
                last_error = exc
                if universe_source != "auto":
                    raise
                continue
        raise last_error or ProviderUnavailable("universe fetch failed", "SOURCE_UNREACHABLE")

    def _fetch_universe_from_source(self, source: str, end_date: str) -> tuple[list[dict[str, Any]], Any]:
        if source == self.provider.name:
            rows = self.provider.fetch_universe(end_date)
            return rows, self.provider
        if source in self.fallback_providers:
            provider = self.fallback_providers[source]
            rows = provider.fetch_universe(end_date)
            return rows, provider
        if source == "tushare":
            provider = TusharePITProvider(token=self.tushare_token, protection_profile=str(getattr(self.provider, "protection_profile_name", "balanced")))
            status, message = provider.available_status()
            if status != "GO":
                raise ProviderUnavailable(message, status)
            rows = provider.fetch_universe(end_date)
            return rows, provider
        if source == "akshare":
            rows = fetch_akshare_universe(end_date)
            provider = type("AkShareUniverseMeta", (), {"universe_method": "akshare.stock_info_a_code_name", "latest_trade_day_value": end_date})()
            return rows, provider
        raise ProviderUnavailable(f"unsupported universe source: {source}", "ENV_UNAVAILABLE")

    def _run_stage(
        self,
        job_id: int,
        stage: str,
        target: str,
        start_date: str,
        end_date: str,
        fn: Callable[[], list[dict[str, Any]]],
    ) -> list[dict[str, Any]]:
        rows = self._safe_fetch(job_id, stage, target, start_date, end_date, fn)
        if not rows:
            return []
        if stage == "universe":
            self.database.upsert_universe(rows)
        elif stage == "industry":
            self.database.upsert_industries(rows)
        return rows

    def _safe_fetch(
        self,
        job_id: int,
        stage: str,
        target: str,
        start_date: str,
        end_date: str,
        fn: Callable[[], list[dict[str, Any]]],
    ) -> list[dict[str, Any]]:
        started = time.perf_counter()
        try:
            rows = fn()
            self.database.log_fetch(
                job_id=job_id,
                stage=stage,
                target=target,
                source=self.provider.name,
                status="success",
                message=f"{len(rows)} rows",
                duration_ms=_elapsed_ms(started),
            )
            self.database.record_fetch_state(
                job_id=job_id,
                stage=stage,
                target=target,
                source=self.provider.name,
                start_date=start_date,
                end_date=end_date,
                status="success",
                row_count=len(rows),
                source_version=self.provider.source_version,
            )
            return rows
        except SourceCircuitOpen as exc:
            error_class = classify_provider_error(exc)
            self.database.log_fetch(
                job_id=job_id,
                stage=stage,
                target=target,
                source=self.provider.name,
                status="failed",
                error_class=error_class,
                message=str(exc),
                duration_ms=_elapsed_ms(started),
            )
            self.database.record_fetch_state(
                job_id=job_id,
                stage=stage,
                target=target,
                source=self.provider.name,
                start_date=start_date,
                end_date=end_date,
                status="failed",
                row_count=0,
                source_version=self.provider.source_version,
                error_class=error_class,
                message=str(exc),
            )
            self.database.update_job(job_id, failed_delta=1, message=f"{stage} {target} stopped: {exc}")
            raise
        except Exception as exc:
            error_class = classify_provider_error(exc)
            self.database.log_fetch(
                job_id=job_id,
                stage=stage,
                target=target,
                source=self.provider.name,
                status="failed",
                error_class=error_class,
                message=str(exc),
                duration_ms=_elapsed_ms(started),
            )
            self.database.record_fetch_state(
                job_id=job_id,
                stage=stage,
                target=target,
                source=self.provider.name,
                start_date=start_date,
                end_date=end_date,
                status="failed",
                row_count=0,
                source_version=self.provider.source_version,
                error_class=error_class,
                message=str(exc),
            )
            self.database.update_job(job_id, failed_delta=1, message=f"{stage} {target} failed: {exc}")
            return []

    def _retry_failures(self, job_id: int, start_date: str, end_date: str, progress: ProgressCallback | None) -> None:
        failures = self.database.list_failed_fetch_states(self.provider.name, start_date, end_date, limit=5000)
        total = max(1, len(failures))
        self.database.update_job(job_id, total=total, stage="retry", message=f"retry {len(failures)} failures")
        for index, failure in enumerate(failures, start=1):
            stage = failure.stage
            target = failure.target
            if stage == "prices" and target:
                rows = self._safe_fetch(job_id, "prices", target, start_date, end_date, lambda target=target: self.provider.fetch_prices(target, start_date, end_date))
                if rows:
                    self.database.upsert_prices(rows)
            elif stage == "financials" and target:
                rows = self._safe_fetch(job_id, "financials", target, start_date, end_date, lambda target=target: self.provider.fetch_financials(target, start_date, end_date))
                if rows:
                    self.database.upsert_financials(rows)
            elif stage == "industry":
                rows = self._safe_fetch(job_id, "industry", target or end_date, start_date, end_date, lambda target=target: self.provider.fetch_industry(target or end_date))
                if rows:
                    self.database.upsert_industries(rows)
            elif stage == "universe":
                rows = self._safe_fetch(job_id, "universe", target or end_date, start_date, end_date, lambda target=target: self.provider.fetch_universe(target or end_date))
                if rows:
                    self.database.upsert_universe(rows)
            self.database.update_job(job_id, done=index, stage="retry", message=f"retry {index}/{total}: {stage} {target}")
            _progress(progress, index, total, f"retry {index}/{total}: {stage} {target}")

    def _log_skip(self, job_id: int, stage: str, target: str, start_date: str, end_date: str, reason: str) -> None:
        self.database.log_fetch(
            job_id=job_id,
            stage=stage,
            target=target,
            source=self.provider.name,
            status="skipped",
            message=f"{reason}; interval {start_date}..{end_date}",
        )

    def _set_provider_event_sink(self, job_id: int | None) -> None:
        setter = getattr(self.provider, "set_protection_event_sink", None)
        if setter is None:
            return
        if job_id is None:
            setter(None)
            return
        setter(lambda event: self._log_protection_event(job_id, event))

    def _close_provider(self) -> None:
        closer = getattr(self.provider, "close", None)
        if closer is not None:
            closer()

    def _log_protection_event(self, job_id: int, event: ProtectionEvent) -> None:
        message = event.message
        if event.delay_sec:
            suffix = f"delay={event.delay_sec:.2f}s"
            message = f"{message}; {suffix}" if message else suffix
        if event.attempt:
            suffix = f"attempt={event.attempt}"
            message = f"{message}; {suffix}" if message else suffix
        self.database.log_fetch(
            job_id=job_id,
            stage="protection",
            target=event.target,
            source=event.source,
            status=event.kind,
            error_class=event.error_class,
            message=message,
            duration_ms=event.duration_ms,
        )

    def _write_quality_report(self, job_id: int, start_date: str, end_date: str) -> str:
        report_dir = self.database.db_path.parent / "pit_reports"
        report_dir.mkdir(parents=True, exist_ok=True)
        json_path = report_dir / f"pit_quality_job_{job_id}.json"
        md_path = report_dir / f"pit_quality_job_{job_id}.md"
        self.database.update_job(job_id, report_path=str(md_path))
        stats = self.database.stats()
        jobs = self.database.list_recent_jobs(limit=5)
        current_job = _find_job(jobs, job_id)
        failures = self.database.list_failed_logs_for_job(job_id, limit=20)
        counts = self.database.job_log_counts(job_id)
        job_params = self.database.get_job_params(job_id)
        payload = {
            "generated_at": now_iso(),
            "job_id": job_id,
            "provider": self.provider.name,
            "source_version": self.provider.source_version,
            "start_date": start_date,
            "end_date": end_date,
            "stats": stats.__dict__,
            "job": current_job.__dict__ if current_job else {},
            "job_params": job_params,
            "counts": counts,
            "failure_rate": _failure_rate_from_counts(counts),
            "elapsed_sec": _elapsed_wall_seconds(current_job) if current_job else None,
            "recent_jobs": [job.__dict__ for job in jobs],
            "failures": [failure.__dict__ for failure in failures],
        }
        json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        md_path.write_text(_render_quality_report(payload), encoding="utf-8")
        return str(md_path)

    def _result(self, job_id: int) -> PITBuildResult:
        job = _find_job(self.database.list_recent_jobs(limit=50), job_id)
        if job is None:
            return PITBuildResult(job_id, "unknown", "", "", 0, 0)
        return PITBuildResult(job.id, job.status, job.message, job.report_path, job.done, job.failed)


def _find_job(jobs: list[PITJobRecord], job_id: int) -> PITJobRecord | None:
    for job in jobs:
        if job.id == job_id:
            return job
    return None


def _render_quality_report(payload: dict[str, Any]) -> str:
    stats = payload["stats"]
    counts = payload.get("counts") or {}
    params = payload.get("job_params") or {}
    coverage = stats.get("industry_coverage")
    coverage_text = "-" if coverage is None else f"{coverage * 100:.1f}%"
    failure_rate = payload.get("failure_rate")
    failure_rate_text = "-" if failure_rate is None else f"{failure_rate * 100:.1f}%"
    elapsed = payload.get("elapsed_sec")
    elapsed_text = "-" if elapsed is None else f"{elapsed:.1f}s"
    circuit_reason = _latest_circuit_reason(payload.get("failures") or [])
    lines = [
        "# PIT 数据质量报告",
        "",
        f"- 生成时间：{payload['generated_at']}",
        f"- Job：{payload['job_id']}",
        f"- 数据源：{payload['provider']} {payload['source_version']}",
        f"- 保护档位：{stats.get('latest_protection_profile') or '-'}",
        f"- 区间：{payload['start_date']} 至 {payload['end_date']}",
        f"- Healthcheck：{params.get('healthcheck_status') or '-'}",
        f"- 最近交易日：{params.get('latest_trade_day') or '-'}",
        f"- Universe 来源：{params.get('universe_source') or '-'}",
        f"- Universe 方法：{params.get('universe_method') or '-'}",
        f"- Universe 本任务行数：{params.get('universe_rows', 0)}",
        f"- Worker 重启数：{counts.get('worker_restart', 0) or params.get('worker_restarts', 0)}",
        f"- 耗时：{elapsed_text}",
        f"- 请求数：{counts.get('request', 0)}",
        f"- 请求成功数：{counts.get('request_success', 0)}",
        f"- 请求失败数：{counts.get('request_failed', 0)}",
        f"- 阶段成功数：{counts.get('success', 0)}",
        f"- 阶段失败数：{counts.get('failed', 0)}",
        f"- 跳过数：{counts.get('skipped', 0)}",
        f"- 重试数：{counts.get('retry', 0)}",
        f"- 冷却数：{counts.get('cooldown', 0)}",
        f"- 熔断次数：{counts.get('circuit_open', 0)}",
        f"- 失败率：{failure_rate_text}",
        f"- 熔断原因：{circuit_reason or '-'}",
        f"- Universe 行数：{stats['universe_rows']}",
        f"- 价格行数：{stats['price_rows']}，最新交易日：{stats['latest_price_date'] or '-'}",
        f"- 财务指标行数：{stats['financial_rows']}（按 pub_date 控制可见性）",
        f"- 行业行数：{stats['industry_rows']}，最新覆盖率：{coverage_text}",
        f"- 全库失败日志：{stats['failed_logs']}",
        "",
        "## 最近失败",
        "",
    ]
    failures = payload.get("failures") or []
    if not failures:
        lines.append("- 无")
    else:
        for item in failures[:20]:
            lines.append(f"- {item['stage']} {item['target']} [{item['error_class']}]: {item['message'][:180]}")
    lines.extend(
        [
            "",
            "## 点时约束",
            "",
            "- `pit_financials` 仅通过 `pub_date <= asof_date` 的查询接口对外可见，避免公告日前使用财务数据。",
            "- 请求保护层用于降低限流风险；不保证第三方数据源不会限流、断连或封禁。",
            "- 当前智能筛选仍使用 V1 因子库；PIT 库作为回测与数据健康基础，不直接改变筛选结果。",
            "",
        ]
    )
    return "\n".join(lines)


def _resolve_end_date(value: str) -> str:
    return today_iso() if not value or value == "today" else value


def _resolve_start_date(value: str, mode: str, end_date: str) -> str:
    if mode == "incremental":
        try:
            end = date.fromisoformat(end_date)
            return (end - timedelta(days=7)).isoformat()
        except ValueError:
            return end_date
    return value or "2019-01-01"


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def _progress(progress: ProgressCallback | None, done: int, total: int, message: str) -> None:
    if progress is not None:
        progress(done, total, message)


def _deterministic_sample(values: list[str], limit: int | None) -> list[str]:
    if limit is None or limit <= 0 or limit >= len(values):
        return values
    if limit == 1:
        return [values[0]]
    last = len(values) - 1
    selected: list[str] = []
    seen: set[int] = set()
    for index in range(limit):
        pos = round(index * last / (limit - 1))
        if pos not in seen:
            seen.add(pos)
            selected.append(values[pos])
    cursor = 0
    while len(selected) < limit and cursor < len(values):
        if cursor not in seen:
            selected.append(values[cursor])
            seen.add(cursor)
        cursor += 1
    return selected


def _failure_rate_from_counts(counts: dict[str, int]) -> float | None:
    failed = int(counts.get("failed", 0))
    success = int(counts.get("success", 0))
    total = failed + success
    if total <= 0:
        return None
    return round(failed / total, 4)


def _preflight_passed(counts: dict[str, int], failure_rate: float | None) -> bool:
    if int(counts.get("circuit_open", 0)) > 0:
        return False
    if int(counts.get("cooldown", 0)) > 1:
        return False
    return (failure_rate or 0.0) <= 0.05


def _completion_message(
    mode: str,
    price_rows: int,
    financial_rows: int,
    failed_logs: int,
    counts: dict[str, int],
    failure_rate: float | None,
) -> str:
    failure_rate_text = "-" if failure_rate is None else f"{failure_rate * 100:.1f}%"
    if mode == "healthcheck":
        return (
            f"PIT healthcheck 完成；请求 {counts.get('request', 0)}，请求失败 {counts.get('request_failed', 0)}，"
            f"重试 {counts.get('retry', 0)}，冷却 {counts.get('cooldown', 0)}，熔断 {counts.get('circuit_open', 0)}，"
            f"worker 重启 {counts.get('worker_restart', 0)}。"
        )
    prefix = "PIT preflight 完成" if mode == "preflight" else f"PIT {mode} 完成"
    return (
        f"{prefix}；价格 {price_rows} 行，财务 {financial_rows} 行，全库失败日志 {failed_logs} 条；"
        f"请求 {counts.get('request', 0)}，成功 {counts.get('success', 0)}，失败 {counts.get('failed', 0)}，"
        f"跳过 {counts.get('skipped', 0)}，重试 {counts.get('retry', 0)}，冷却 {counts.get('cooldown', 0)}，"
        f"熔断 {counts.get('circuit_open', 0)}，失败率 {failure_rate_text}。"
    )


def _elapsed_wall_seconds(job: PITJobRecord) -> float | None:
    end_text = job.completed_at or job.updated_at
    if not job.created_at or not end_text:
        return None
    try:
        started = _parse_iso(job.created_at)
        ended = _parse_iso(end_text)
    except ValueError:
        return None
    return max(0.0, (ended - started).total_seconds())


def _parse_iso(value: str):
    return date.fromisoformat(value[:10]) if "T" not in value else datetime.fromisoformat(value)


def _latest_circuit_reason(failures: list[dict[str, Any]]) -> str:
    for item in failures:
        if item.get("error_class") == "SOURCE_COOLDOWN":
            return str(item.get("message") or "")[:180]
    return ""
