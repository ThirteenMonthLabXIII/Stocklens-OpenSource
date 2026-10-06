"""Shared, persistent screening workflow used by both desktop frontends.

The UI may decide how to obtain a data snapshot, but once a screen report
exists its persisted audit trail must be deterministic across repeated runs.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from typing import Any

from stock_tool.core.factor_store import FactorSnapshot, V1FactorStore
from stock_tool.core.data_snapshot import DataSnapshotManifest
from stock_tool.core.data_snapshot import DataSnapshotStore
from stock_tool.core.evidence_contracts import DEFAULT_CANDIDATE_LENSES
from stock_tool.core.formal_certificate import (
    FormalObservationBlocked,
    FormalScreeningCertificate,
    FormalScreeningCertificateStore,
    UniverseDefinition,
    screening_result_digest,
)
from stock_tool.core.performance_loop import (
    DEFAULT_OBSERVATION_HORIZONS,
    DEFAULT_OBSERVATION_TOP_N,
    PerformanceStore,
)
from stock_tool.core.query_schema import QuerySpec, query_spec_summary, query_spec_to_screen_filter
from stock_tool.core.screener_engine import ScreenReport, run_candidate_lens_screening
from stock_tool.core.selection_policy import DEFAULT_SELECTION_POLICY, SelectionPolicy
from stock_tool.core.screening_contract import (
    DataQualityAssessment,
    MISSING_DATA_POLICY_VERSION,
    assess_snapshot_quality,
    condition_hash,
    snapshot_asof,
    snapshot_id,
    stable_hash,
)
from stock_tool.core.strategy_store import StrategyStore


NEW_SCREENING_MODES = frozenset({"formal", "review_only", "exploratory"})
CANDIDATE_LENS_RANKING_ID = "candidate_lens.v1"
CANDIDATE_LENS_RANKING_VERSION = "candidate_lens.v1"
CANDIDATE_LENS_SELECTION_POLICY_VERSION = "v1"


@dataclass(frozen=True)
class ScreeningWorkflowResult:
    """The durable outputs of one screening run."""

    report: ScreenReport
    run_id: int
    observation_samples: int | None
    warnings: tuple[str, ...] = ()
    certificate_id: str | None = None


class ScreeningWorkflowService:
    """Persist a screening report and create its observation samples once."""

    def __init__(self, strategy_store: StrategyStore, performance_store: PerformanceStore | None) -> None:
        self.strategy_store = strategy_store
        self.performance_store = performance_store

    def run_snapshot_screening(
        self,
        snapshots: list[FactorSnapshot],
        spec: QuerySpec,
        *,
        source_label: str,
        user_text: str,
        data_source: str,
        professional_formula: dict[str, Any] | None = None,
        execution_mode: str | None = None,
        template_id: str = "",
        template_version: str = "",
        universe_id: str = "a_share_all",
        ranking_formula_id: str = "candidate_lens.v1",
        ranking_formula_version: str = "candidate_lens.v1",
        regime_id: str = "fixed_baseline",
        regime_detector_version: str = "v1",
        data_manifest: DataSnapshotManifest | None = None,
        require_data_manifest: bool = False,
        refresh_event_id: str = "",
        universe_version: str = "",
        members_digest: str = "",
        selection_scope: str = "",
        selection_policy_version: str = "v1",
        selected_limit: int | None = None,
        truncation_reason: str = "",
        member_status_policy: str = "listed_non_st",
        candidate_lens_mode: bool | None = None,
        lens_ids: tuple[str, ...] | None = None,
        selection_policy: SelectionPolicy | None = None,
        policy_mode: str | None = None,
        trading_calendar: list[str] | None = None,
        **record_options: Any,
    ) -> ScreeningWorkflowResult:
        """Screen one immutable snapshot and persist the resulting workflow."""
        if professional_formula is not None:
            raise ValueError("CandidateLens 新生产流程禁止传入 professional_formula")
        filters = query_spec_to_screen_filter(spec)
        actual_mode = execution_mode or spec.execution_mode
        quality = assess_snapshot_quality(snapshots, filters, execution_mode=actual_mode)
        data_asof = snapshot_asof(snapshots)
        data_id = snapshot_id(snapshots, data_source=data_source)
        strategy_database = getattr(self.strategy_store, "database", None)
        fact_lineage_by_code: dict[str, dict[str, dict[str, Any]]] = {}
        lens_values_by_code: dict[str, dict[str, dict[str, Any]]] = {}
        selection_inputs_by_code = {}
        if require_data_manifest and data_manifest is None and actual_mode not in {"review_only", "exploratory"}:
            quality = DataQualityAssessment(
                status="blocked",
                note="正式筛选要求不可变数据快照，但当前输入未绑定 data_manifest。",
            )
        if data_manifest is not None:
            if data_manifest.data_source != data_source:
                raise ValueError(
                    "data_manifest.data_source 必须与 screening data_source 一致"
                )
            data_asof = data_manifest.asof_at
            data_id = data_manifest.snapshot_id
            universe_version = universe_version or data_manifest.universe_version
            if strategy_database is not None:
                snapshot_store = DataSnapshotStore(strategy_database)
                refresh_event_id = refresh_event_id or snapshot_store.refresh_event_id_for_snapshot(
                    data_manifest.snapshot_id
                )
                fact_lineage_by_code = snapshot_store.fact_lineage_by_code(data_manifest.snapshot_id)
                selection_inputs_by_code = V1FactorStore(strategy_database).list_selection_inputs_for_manifest(
                    data_manifest.snapshot_id
                )
                for code, selection_input in selection_inputs_by_code.items():
                    if selection_input.events:
                        lens_values_by_code[code] = {
                            "event_verification": dict(selection_input.events[0]),
                        }
            provided_context = record_options.get("scoring_context")
            if not isinstance(provided_context, dict):
                provided_context = {}
            provided_context = {
                **provided_context,
                "availability_cutoff": data_manifest.availability_cutoff,
                "dataset_version": data_manifest.dataset_version,
                "snapshot_quality_status": data_manifest.quality_status,
                "source_versions": dict(data_manifest.source_versions),
                "selection_input_version": "selection_input.v1",
                "selection_input_codes": sorted(selection_inputs_by_code),
            }
            record_options["scoring_context"] = provided_context
            if data_manifest.quality_status == "blocked" and actual_mode not in {"review_only", "exploratory"}:
                quality = DataQualityAssessment(
                    status="blocked",
                    required_fields=quality.required_fields,
                    missing_fields=quality.missing_fields,
                    blocking_fields=quality.blocking_fields,
                    note="数据快照质量状态为 blocked，正式筛选被阻断。",
                )
            elif data_manifest.quality_status in {"partial", "unverified"}:
                if actual_mode == "review_only" and quality.status in {"verified", "review_only"}:
                    quality = DataQualityAssessment(
                        status="review_only",
                        required_fields=quality.required_fields,
                        missing_fields=quality.missing_fields,
                        blocking_fields=quality.blocking_fields,
                        note="数据快照存在未验证或部分字段，结果只能用于明确标记的研究复核。",
                    )
                elif actual_mode not in {"review_only", "exploratory"}:
                    if quality.status == "blocked" or quality.blocking_fields or not quality.required_fields:
                        quality = DataQualityAssessment(
                            status="blocked",
                            required_fields=quality.required_fields,
                            missing_fields=quality.missing_fields,
                            blocking_fields=quality.blocking_fields,
                            note=(
                                "当前模板的必需字段整体不可用，正式筛选被阻断；"
                                f"缺失：{', '.join(quality.missing_fields or quality.blocking_fields)}。"
                            ),
                        )
                    else:
                        quality = DataQualityAssessment(
                            status="partial",
                            required_fields=quality.required_fields,
                            missing_fields=quality.missing_fields,
                            blocking_fields=(),
                            note="当前模板必需字段大部分可用，少量样本字段缺失将被排除，结果标记 partial。",
                        )
            if strategy_database is not None and actual_mode not in {"review_only", "exploratory"}:
                persisted_quality = DataSnapshotStore(strategy_database).assess_snapshot_quality(
                    data_manifest.snapshot_id,
                    {
                        "required_fields": quality.required_fields,
                        "codes": [snapshot.code for snapshot in snapshots],
                        "mode": "formal",
                    },
                )
                if persisted_quality.status != "verified":
                    quality = DataQualityAssessment(
                        status="blocked",
                        required_fields=quality.required_fields,
                        missing_fields=tuple(
                            dict.fromkeys(
                                [*quality.missing_fields, *persisted_quality.missing_fields]
                            )
                        ),
                        blocking_fields=tuple(persisted_quality.missing_fields),
                        note=f"持久化事实质量闸门未通过：{persisted_quality.note}",
                    )
        provided_context = record_options.pop("scoring_context", None)
        if not isinstance(provided_context, dict):
            provided_context = {}
        if candidate_lens_mode is False:
            raise ValueError("formal、review_only、exploratory 均只能使用 CandidateLens")
        use_candidate_lens_mode = True
        effective_selection_policy = selection_policy or DEFAULT_SELECTION_POLICY
        if quality.status == "blocked":
            report = ScreenReport(
                results=[],
                scanned=0,
                skipped=0,
                failed=0,
                errors=[quality.note],
                notes=[],
                status="blocked",
                missing_fields=list(quality.missing_fields),
                data_source=data_source,
                snapshot_asof=data_asof,
                data_snapshot_id=data_id,
            )
        else:
            report = run_candidate_lens_screening(
                snapshots,
                filters,
                mode=actual_mode,
                lens_ids=lens_ids,
                data_snapshot_id=data_id,
                availability_cutoff=(data_manifest.availability_cutoff if data_manifest else ""),
                data_quality_status=quality.status,
                data_source=data_source,
                fact_lineage_by_code=fact_lineage_by_code,
                lens_values_by_code=lens_values_by_code,
                selection_inputs_by_code=selection_inputs_by_code,
                selection_policy=effective_selection_policy,
                trading_calendar=trading_calendar,
                member_status_policy=member_status_policy,
            )
            report.status = quality.status if quality.status != "verified" else report.status
            report.missing_fields = list(dict.fromkeys([*report.missing_fields, *quality.missing_fields]))
            report.data_source = data_source
            report.snapshot_asof = data_asof
            report.data_snapshot_id = data_id
            if quality.note:
                report.notes.append(quality.note)
        if use_candidate_lens_mode:
            provided_context = {
                **provided_context,
                "candidate_lens_mode": True,
                "candidate_lens_contract_version": "candidate_lens.v1",
                "lens_ids": list(lens_ids or tuple(item.lens_id for item in DEFAULT_CANDIDATE_LENSES)),
                "candidate_set_digest": report.candidate_set_digest,
                "selection_policy_version": "v1",
            }
            ranking_formula_id = "candidate_lens.v1"
            ranking_formula_version = "candidate_lens.v1"
            selection_policy_version = "v1"
            professional_formula = None
        return self.record_report(
            report,
            spec,
            source_label=source_label,
            user_text=user_text,
            data_source=data_source,
            professional_formula=professional_formula,
            execution_mode=actual_mode,
            template_id=template_id,
            template_version=template_version,
            universe_id=universe_id,
            ranking_formula_id=ranking_formula_id,
            ranking_formula_version=ranking_formula_version,
            regime_id=regime_id,
            regime_detector_version=regime_detector_version,
            condition_digest=condition_hash(spec),
            data_snapshot_id=data_id,
            snapshot_asof=data_asof,
            missing_policy_version=MISSING_DATA_POLICY_VERSION,
            scoring_context={**provided_context, "data_quality": quality.as_dict()},
            data_manifest_required=require_data_manifest,
            refresh_event_id=refresh_event_id,
            universe_version=universe_version,
            members_digest=members_digest,
            selection_scope=selection_scope,
            selection_policy_version=selection_policy_version,
            selected_limit=selected_limit,
            truncation_reason=truncation_reason,
            member_status_policy=member_status_policy,
            data_manifest=data_manifest,
            require_formal_certificate=actual_mode == "formal" and report.status == "verified",
            **record_options,
        )

    def record_report(
        self,
        report: ScreenReport,
        spec: QuerySpec,
        *,
        source_label: str,
        user_text: str,
        data_source: str,
        preset_id: int | None = None,
        preset_name: str = "",
        summary: str | None = None,
        note: str = "",
        template_id: str = "",
        template_version: str = "",
        universe_id: str = "",
        universe_hash: str = "",
        config_hash: str = "",
        professional_formula: dict[str, Any] | None = None,
        ai_rank_map: dict[str, Any] | None = None,
        asof_date: str | None = None,
        execution_mode: str = "formal",
        ranking_formula_id: str = "candidate_lens.v1",
        ranking_formula_version: str = "candidate_lens.v1",
        regime_id: str = "fixed_baseline",
        regime_detector_version: str = "v1",
        condition_digest: str = "",
        data_snapshot_id: str = "",
        snapshot_asof: str = "",
        missing_policy_version: str = MISSING_DATA_POLICY_VERSION,
        scoring_context: dict[str, Any] | None = None,
        data_manifest: DataSnapshotManifest | None = None,
        data_manifest_required: bool = False,
        refresh_event_id: str = "",
        universe_version: str = "",
        members_digest: str = "",
        selection_scope: str = "",
        selection_policy_version: str = "v1",
        selected_limit: int | None = None,
        truncation_reason: str = "",
        member_status_policy: str = "listed_non_st",
        require_formal_certificate: bool | None = None,
    ) -> ScreeningWorkflowResult:
        """Save one report, its scoring context and its 3/5/10/20-day samples."""
        if execution_mode in NEW_SCREENING_MODES:
            if professional_formula is not None:
                raise ValueError("CandidateLens 新生产流程禁止传入 professional_formula")
            if ranking_formula_id != CANDIDATE_LENS_RANKING_ID:
                raise ValueError("新生产流程的 ranking_formula_id 必须为 candidate_lens.v1")
            if ranking_formula_version != CANDIDATE_LENS_RANKING_VERSION:
                raise ValueError("新生产流程的 ranking_formula_version 必须为 candidate_lens.v1")
            if selection_policy_version != CANDIDATE_LENS_SELECTION_POLICY_VERSION:
                raise ValueError("新生产流程的 selection_policy_version 必须为 v1")
        elif professional_formula is not None and ranking_formula_id == CANDIDATE_LENS_RANKING_ID:
            raise ValueError("CandidateLens 新生产流程禁止传入 professional_formula")
        # ``formal`` is the safe default for this persistence boundary.  An
        # explicit False must never downgrade a formal run to legacy samples;
        # callers that need a non-formal record must say review_only or
        # exploratory in the QuerySpec/execution_mode.
        if execution_mode == "formal":
            require_formal_certificate = True
            data_manifest_required = True
        elif require_formal_certificate is None:
            require_formal_certificate = False
        condition_digest = condition_digest or condition_hash(spec)
        config_hash = config_hash or condition_digest
        run_context = dict(scoring_context or {})
        run_context.update(
            {
                "template_id": template_id,
                "template_version": template_version,
                "condition_hash": condition_digest,
                "data_snapshot_id": data_snapshot_id,
                "snapshot_asof": snapshot_asof,
                "universe_id": universe_id or "a_share_all",
                "data_source": data_source,
                "missing_policy_version": missing_policy_version,
                "execution_mode": execution_mode,
                "ranking_formula_id": ranking_formula_id,
                "ranking_formula_version": ranking_formula_version,
                "regime_id": regime_id,
                "regime_detector_version": regime_detector_version,
                "screening_status": report.status,
                "data_manifest_required": data_manifest_required,
                "refresh_event_id": refresh_event_id,
                "universe_version": universe_version,
                "members_digest": members_digest,
                "selection_scope": selection_scope,
                "selection_policy_version": selection_policy_version,
                "selected_limit": selected_limit,
                "truncation_reason": truncation_reason,
                "member_status_policy": member_status_policy,
            }
        )
        run_id = self.strategy_store.record_run(
            source_label=source_label,
            user_text=user_text,
            spec=spec,
            data_source=data_source,
            report=report,
            preset_id=preset_id,
            preset_name=preset_name,
            summary=summary or query_spec_summary(spec),
            note=note,
            template_id=template_id,
            template_version=template_version,
            universe_id=universe_id,
            universe_hash=universe_hash,
            config_hash=config_hash,
            professional_formula=professional_formula,
            scoring_context=run_context,
        )
        certificate_id: str | None = None
        if require_formal_certificate and report.status == "verified":
            try:
                if data_manifest is None:
                    raise FormalObservationBlocked("正式运行必须绑定不可变 data_manifest")
                if data_manifest.snapshot_id != data_snapshot_id:
                    raise FormalObservationBlocked("正式运行的 data_manifest 与 data_snapshot_id 不一致")
                candidate_codes = [candidate.quote.code for candidate in report.results]
                quality_summary = dict((scoring_context or {}).get("data_quality") or {})
                if data_manifest is not None:
                    quality_summary = {
                        **json.loads(data_manifest.quality_summary_json or "{}"),
                        **quality_summary,
                    }
                resolved_scope = selection_scope or template_id or universe_id
                resolved_universe_version = universe_version or data_snapshot_id
                resolved_members_digest = members_digest or stable_hash(sorted(candidate_codes))
                universe = UniverseDefinition(
                    universe_definition_id=universe_id,
                    universe_version=resolved_universe_version,
                    members_digest=resolved_members_digest,
                    selection_scope=resolved_scope,
                    selection_policy_version=selection_policy_version,
                    selected_limit=selected_limit or len(candidate_codes) or None,
                    truncation_reason=truncation_reason,
                    member_status_policy=member_status_policy,
                )
                certificate = FormalScreeningCertificate.issue(
                    run_id=run_id,
                    data_snapshot_id=data_snapshot_id,
                    refresh_event_id=refresh_event_id,
                    universe=universe,
                    selection_scope=resolved_scope,
                    formula_version=ranking_formula_version,
                    execution_mode=execution_mode,
                    quality_status=report.status,
                    quality_summary=quality_summary,
                    condition_hash=condition_digest,
                    result_digest=screening_result_digest(report.results),
                )
                FormalScreeningCertificateStore(self.strategy_store.database).save(certificate)
                certificate_id = certificate.certificate_id
            except (FormalObservationBlocked, ValueError, KeyError) as exc:
                report.status = "blocked"
                report.errors.append(f"正式筛选凭证未签发：{exc}")
                update_context = getattr(self.strategy_store, "update_run_scoring_context", None)
                if callable(update_context):
                    update_context(
                        run_id,
                        {"screening_status": "blocked", "formal_certificate_error": str(exc)},
                    )
        if self.performance_store is None:
            return ScreeningWorkflowResult(
                report=report,
                run_id=run_id,
                observation_samples=None,
                warnings=("筛选结果已保存，但本地战绩库不可用，未创建观察样本。",),
                certificate_id=certificate_id,
            )
        if report.status == "blocked":
            return ScreeningWorkflowResult(
                report=report,
                run_id=run_id,
                observation_samples=None,
                warnings=("结果已保存为审计记录，但未创建正式观察样本；请补齐数据或重新执行正式筛选。",),
                certificate_id=certificate_id,
            )
        if report.status in {"partial", "unverified", "review_only"}:
            return ScreeningWorkflowResult(
                report=report,
                run_id=run_id,
                observation_samples=None,
                warnings=("结果已保存为审计记录，但未创建正式观察样本；请补齐数据或重新执行正式筛选。",),
                certificate_id=certificate_id,
            )

        try:
            self.performance_store.save_run_context(run_id, asof_date=asof_date or date.today().isoformat())
            if require_formal_certificate:
                if not certificate_id:
                    raise FormalObservationBlocked("正式运行没有可用凭证")
                samples = self.performance_store.create_formal_observation_samples(
                    run_id,
                    certificate_id,
                    top_n=DEFAULT_OBSERVATION_TOP_N,
                    horizons=DEFAULT_OBSERVATION_HORIZONS,
                    ai_rank_map=ai_rank_map,
                )
            elif execution_mode == "review_only":
                samples = self.performance_store.create_review_observation_samples(
                    run_id,
                    top_n=DEFAULT_OBSERVATION_TOP_N,
                    horizons=DEFAULT_OBSERVATION_HORIZONS,
                    ai_rank_map=ai_rank_map,
                )
            elif execution_mode == "exploratory":
                samples = self.performance_store.create_exploratory_observation_samples(
                    run_id,
                    top_n=DEFAULT_OBSERVATION_TOP_N,
                    horizons=DEFAULT_OBSERVATION_HORIZONS,
                    ai_rank_map=ai_rank_map,
                )
            else:
                samples = self.performance_store.create_legacy_observation_samples(
                    run_id,
                    top_n=DEFAULT_OBSERVATION_TOP_N,
                    horizons=DEFAULT_OBSERVATION_HORIZONS,
                    ai_rank_map=ai_rank_map,
                )
        except Exception as exc:
            return ScreeningWorkflowResult(
                report=report,
                run_id=run_id,
                observation_samples=None,
                warnings=(f"执行记录已保存，但观察样本创建失败：{exc}",),
                certificate_id=certificate_id,
            )
        return ScreeningWorkflowResult(report=report, run_id=run_id, observation_samples=samples, certificate_id=certificate_id)
