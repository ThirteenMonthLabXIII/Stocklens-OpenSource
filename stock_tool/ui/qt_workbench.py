from __future__ import annotations

import csv
import getpass
import json
import os
import sys
import threading
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from stock_tool import __version__
from stock_tool.config.release_identity import DISPLAY_VERSION, PRODUCT_SHORT_NAME, QT_WINDOW_TITLE
from stock_tool.core.ai_analyzer import AIConfig
from stock_tool.core.ai_client import AIValidationResult, validate_ai_key
from stock_tool.core.ai_cost import pricing_summary
from stock_tool.core.alert_monitor import (
    AlertMonitorCoordinator,
    FREE_SOURCE_NOTICE,
    TradingCalendar,
    TradingClock,
    as_shanghai,
)
from stock_tool.core.alert_service import AlertDraft, AlertService
from stock_tool.core.cache import CacheStore
from stock_tool.core.data_snapshot import DataSnapshotStore, is_manifest_fresh
from stock_tool.core.data_readiness import (
    DataFreshnessPolicy,
    DataReadinessCoordinator,
    DataRequirement,
)
from stock_tool.core.data_refresh import (
    DataRefreshCoordinator,
    RefreshRunSummary,
    RefreshStage,
    RefreshStepResult,
    RefreshTrigger,
)
from stock_tool.core.charting import ChartMode, ChartPeriod, ChartRange, build_chart_payload
from stock_tool.core.data_fetcher import DataFetcher, QuoteSnapshot, TUSHARE_MONEY_FLOW_FALLBACK_SETTING
from stock_tool.core.candidate_state import (
    CandidateStateError,
    CandidateStateService,
    SqliteCandidateStateStore,
)
from stock_tool.core.data_location import (
    db_path_for,
    migrate_data_dir,
    preflight_data_migration,
    read_configured_data_dir,
    read_data_dir,
    write_data_dir,
)
from stock_tool.core.factor_store import FACTOR_SNAPSHOT_MAX_AGE, V1FactorStore
from stock_tool.core.local_query_parser import parse_local_query
from stock_tool.core.models import FinancialMetrics, StockQuote, TradeReference, fmt_number
from stock_tool.core.multi_agent_orchestrator import (
    AgentOrchestrationResult,
    MemoryAgentExecutionStore,
    SqliteAgentExecutionStore,
    FiveAgentOrchestrator,
    format_agent_statuses,
)
from stock_tool.core.evidence_summary import aggregate_agent_evidence
from stock_tool.core.evidence_run_store import EvidenceReplayError, SqliteEvidenceRunStore
from stock_tool.core.exploratory_proposal_store import (
    SqliteExploratoryProposalStore,
    proposals_from_agent_result,
)
from stock_tool.core.evaluation_stores import (
    AgentEvaluationStore,
    DataQualityEvaluationStore,
    LensEvaluationStore,
    ObservationOutcomeStore,
    SAMPLE_CLASS_TO_MODE,
)
from stock_tool.core.notify import Notifier, WindowsToastChannel
from stock_tool.core.performance_loop import DEFAULT_OBSERVATION_HORIZONS, DEFAULT_OBSERVATION_TOP_N, PerformanceStore, StoreBackedPriceSource
from stock_tool.core.price_alert import ALERT_KINDS, describe_alert, is_percent_kind
from stock_tool.core.pit_database import PITDatabase, pit_db_path_for
from stock_tool.core.pit_pipeline import PITBuildResult, PITDatasetBuilder
from stock_tool.core.pit_providers import BaostockPITProvider
from stock_tool.core.query_schema import (
    QuerySpec,
    apply_local_query_guardrails,
    query_spec_summary,
    query_spec_to_dict,
    query_spec_to_screen_filter,
)
from stock_tool.core.screener_engine import ScreenReport
from stock_tool.core.screening_contract import condition_hash
from stock_tool.core.screening_contract import stable_hash
from stock_tool.core.research_discussion import (
    DiscussionAttempt,
    ResearchDiscussionService,
    build_evidence_package,
)
from stock_tool.core.screening_evidence import build_screening_evidence_package
from stock_tool.core.screening_workflow import ScreeningWorkflowService
from stock_tool.core.secrets import AI_API_KEY_NAME, TICKFLOW_API_KEY_NAME, TUSHARE_TOKEN_NAME, SecretStore
from stock_tool.core.strategy_store import (
    StrategyStore,
    query_spec_from_export_payload,
    run_comparison_csv_rows,
    run_comparison_summary,
    strategy_export_payload,
)
from stock_tool.core.screener_presets import ScreenerPreset, builtin_screener_presets
from stock_tool.core.technical_profile import build_technical_profile
from stock_tool.core.trade_observer import build_trade_reference, due_date_from_now
from stock_tool.core.review_workflow import ReviewWorkflowService
from stock_tool.core.review_automation import AutomationRunReport, ReviewAutomationCoordinator
from stock_tool.core.v1_database import V1Database, v1_db_path_for
from stock_tool.ui.qt_actions import ActionDescriptor, WorkflowResult
from stock_tool.ui.qt_dashboard_data import (
    build_watch_rows,
    classify_search_intent,
    data_health_rows,
    merge_quote_cache_payloads,
    summarize_alerts,
    summarize_tracking,
)
from stock_tool.ui.qt_flow_layout import FlowLayout
from stock_tool.ui.qt_home import HomeDashboard
from stock_tool.ui.polish_helpers import load_acceptance_report_summaries
from stock_tool.ui.qt_sidebar import NAVIGATION_LABELS, Sidebar
from stock_tool.ui.qt_theme import PALETTE, build_workbench_qss
from stock_tool.ui.qt_watchlist import WatchlistPage
from stock_tool.ui.qt_widgets import Card, build_table


DISCLAIMER = "本软件仅供数据观察参考，不构成投资建议；请以交易所公告和上市公司披露为准。"

SCREENING_MODE_OPTIONS = (
    ("正式 formal", "formal"),
    ("研究复核 review_only", "review_only"),
    ("探索 exploratory", "exploratory"),
)
SCREENING_MODE_LABELS = {value: label for label, value in SCREENING_MODE_OPTIONS}
EVALUATION_MODE_OPTIONS = (
    ("正式 formal", "formal"),
    ("研究复核 review_only", "review_only"),
    ("探索 exploratory", "exploratory"),
    ("兼容 legacy", "legacy"),
)
EVALUATION_MODE_LABELS = {value: label for label, value in EVALUATION_MODE_OPTIONS}

# Values are intentionally bounded in the UI.  They remain cache-backed so
# the core freshness policy observes a change immediately, without reopening
# the workbench or creating a second source of truth for TTLs.
FEATURE_TTL_CONTROL_SPECS = (
    ("feature_refresh_quote_ttl_seconds", "行情", 5, 600, "秒"),
    ("feature_refresh_market_context_ttl_seconds", "市场", 60, 3600, "秒"),
    ("feature_refresh_history_ttl_seconds", "K线", 300, 86400, "秒"),
    ("feature_refresh_money_flow_ttl_seconds", "资金流", 60, 3600, "秒"),
    ("feature_refresh_event_ttl_seconds", "事件", 300, 86400, "秒"),
    ("feature_refresh_financial_ttl_seconds", "财务", 3600, 604800, "秒"),
    ("feature_refresh_factor_snapshot_trading_days", "筛选快照", 1, 5, "交易日"),
    ("feature_refresh_pit_ttl_seconds", "PIT", 3600, 604800, "秒"),
)


class _PreparedQuoteProvider:
    """Small adapter that hands review automation the already-prepared quotes."""

    def __init__(self, snapshots: dict[str, QuoteSnapshot]) -> None:
        self._snapshots = dict(snapshots)

    def quotes_by_codes(self, codes: list[str]) -> dict[str, QuoteSnapshot]:
        return {code: self._snapshots[code] for code in codes if code in self._snapshots}


def analyze_trade_observation_with_ai(
    config: AIConfig,
    quote: StockQuote,
    technical: Any,
    money_flow: Any,
    sentiment: Any,
    events: list[Any],
    reference: Any,
    *,
    evidence_package: dict[str, Any] | None = None,
    return_attempt: bool = False,
) -> str | DiscussionAttempt:
    """Compatibility name for the UI action, backed by research discussion only.

    The legacy function name is retained so existing UI smoke hooks remain
    stable.  Its production implementation accepts only the immutable
    evidence package and serializes the research-only report; it cannot
    produce a trade decision payload.
    """

    if not isinstance(evidence_package, dict):
        raise ValueError("AI研究讨论缺少不可变证据包，请先更新筛选快照")
    attempt = ResearchDiscussionService(config).discuss(evidence_package)
    if return_attempt:
        return attempt
    if attempt.status != "ok" or attempt.report is None:
        raise ValueError(attempt.error or "AI研究讨论失败")
    return json.dumps(attempt.report.as_dict(), ensure_ascii=False, sort_keys=True)


def run_formal_five_agent_analysis(
    config: AIConfig,
    evidence_package: dict[str, Any],
    *,
    result_store: Any | None = None,
    timeout_seconds: int = 60,
    retries: int = 1,
) -> AgentOrchestrationResult:
    """Run the formal five-Agent path used by the Qt research action.

    The legacy helper above remains an injectable compatibility seam for old
    smoke tests and callers.  The default Qt action uses this formal path and
    persists each Agent independently when a performance database is present.
    """

    store = result_store or MemoryAgentExecutionStore()
    return FiveAgentOrchestrator(
        config,
        timeout_seconds=timeout_seconds,
        retries=retries,
        result_store=store,
        cache=store if hasattr(store, "get") else None,
    ).run(evidence_package)


def _candidate_code(value: Any) -> str:
    return str(value or "").strip().zfill(6)


def _agent_evidence_views(
    formal_result: AgentOrchestrationResult | list[AgentOrchestrationResult] | None,
    evidence_aggregation: Any | list[Any] | None,
    *,
    base_data_gaps_by_code: dict[str, list[str]] | None = None,
    research_mode: str = "formal",
    evidence_package_hash: str = "",
    evidence_package_hashes: list[str] | None = None,
) -> dict[str, dict[str, Any]]:
    """Build candidate-scoped UI views from one or more five-Agent batches."""

    formal_results = (
        [formal_result]
        if isinstance(formal_result, AgentOrchestrationResult)
        else [item for item in (formal_result or ()) if isinstance(item, AgentOrchestrationResult)]
    )
    if not formal_results:
        return {}
    if isinstance(evidence_aggregation, (list, tuple)):
        aggregations = list(evidence_aggregation)
    elif evidence_aggregation is None:
        aggregations = []
    else:
        aggregations = [evidence_aggregation]
    aggregation_by_run = {
        str(getattr(item, "run_id", "")): item
        for item in aggregations
        if getattr(item, "run_id", "")
    }
    views: dict[str, dict[str, Any]] = {}
    for index, batch_result in enumerate(formal_results):
        batch_aggregation = aggregation_by_run.get(batch_result.run_id)
        if batch_aggregation is None and index < len(aggregations):
            batch_aggregation = aggregations[index]
        batch_codes = {
            _candidate_code(item.get("code"))
            for item in batch_result.candidate_records
            if isinstance(item, dict) and _candidate_code(item.get("code")) != "000000"
        }
        batch_gaps = {
            code: list(values)
            for code, values in (base_data_gaps_by_code or {}).items()
            if _candidate_code(code) in batch_codes
        }
        batch_hash = ""
        if evidence_package_hashes is not None and index < len(evidence_package_hashes):
            batch_hash = str(evidence_package_hashes[index] or "")
        elif len(formal_results) == 1:
            batch_hash = evidence_package_hash
        views.update(
            _agent_evidence_views_single(
                batch_result,
                batch_aggregation,
                base_data_gaps_by_code=batch_gaps,
                research_mode=research_mode,
                evidence_package_hash=batch_hash,
            )
        )
    return views


def _agent_evidence_views_single(
    formal_result: AgentOrchestrationResult | None,
    evidence_aggregation: Any | None,
    *,
    base_data_gaps_by_code: dict[str, list[str]] | None = None,
    research_mode: str = "formal",
    evidence_package_hash: str = "",
) -> dict[str, dict[str, Any]]:
    """Build candidate-scoped UI views from one complete five-Agent batch."""

    if formal_result is None:
        return {}
    summary_by_code = {
        _candidate_code(getattr(item, "code", "")): item
        for item in (getattr(evidence_aggregation, "summaries", ()) or ())
        if _candidate_code(getattr(item, "code", "")) != "000000"
    }
    claims_by_id: dict[str, dict[str, Any]] = {}
    coverage_by_agent: dict[str, str] = {}
    missing_by_code: dict[str, list[str]] = {}
    questions_by_code: dict[str, list[str]] = {}
    risk_flags: list[str] = []
    for execution in formal_result.executions:
        result = execution.result
        if result is None:
            coverage_by_agent[execution.agent_id] = f"{execution.agent_id}: 无结果"
            continue
        coverage = result.coverage if isinstance(result.coverage, dict) else {}
        coverage_by_code = coverage.get("by_code") if isinstance(coverage.get("by_code"), dict) else {}
        coverage_by_agent[execution.agent_id] = json.dumps(
            coverage_by_code or coverage,
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        risk_flags.extend(result.risk_flags)
        for value in (*result.missing_evidence, *result.unresolved_questions):
            text = str(value or "").strip()
            if not text:
                continue
            code_prefix = text.split(".", 1)[0] if "." in text else ""
            if code_prefix and _candidate_code(code_prefix) != "000000":
                missing_by_code.setdefault(_candidate_code(code_prefix), []).append(text)
            else:
                questions_by_code.setdefault("*", []).append(text)
        for claim in (*result.claims, *result.counter_claims):
            claim_id = str(claim.get("claim_id") or "").strip()
            if claim_id:
                claims_by_id[claim_id] = dict(claim)

    codes = set(summary_by_code) | set(base_data_gaps_by_code or {}) | set(missing_by_code)
    statuses_text = format_agent_statuses(formal_result).replace("\n", "；") or "未运行"
    all_risks = list(dict.fromkeys(str(item) for item in risk_flags if str(item).strip()))
    views: dict[str, dict[str, Any]] = {}
    for code in codes:
        summary = summary_by_code.get(code)
        summary_payload = summary.as_dict() if summary is not None else {}
        supporting_ids = set(summary_payload.get("supporting_claim_ids") or ())
        opposing_ids = set(summary_payload.get("opposing_claim_ids") or ())
        supporting = [claims_by_id[item] for item in sorted(supporting_ids) if item in claims_by_id]
        opposing = [claims_by_id[item] for item in sorted(opposing_ids) if item in claims_by_id]
        summary_questions = list(summary_payload.get("unresolved_questions") or ())
        base_gaps = list((base_data_gaps_by_code or {}).get(code) or ())
        data_gaps = list(
            dict.fromkeys(
                [
                    *base_gaps,
                    *missing_by_code.get(code, []),
                    *summary_questions,
                ]
            )
        )
        next_checks = list(dict.fromkeys([*summary_questions, *data_gaps, *questions_by_code.get("*", [])]))
        views[code] = {
            "research_mode": research_mode,
            "research_mode_label": SCREENING_MODE_LABELS.get(research_mode, research_mode),
            "agent_status": statuses_text,
            "coverage": "；".join(
                f"{agent_id}: {coverage}" for agent_id, coverage in coverage_by_agent.items()
            ) or "未运行",
            "supporting_evidence": supporting,
            "opposing_evidence": opposing,
            "conflicts": [dict(item) for item in (summary_payload.get("conflicts") or ())],
            "risk_flags": all_risks,
            "data_gaps": data_gaps,
            "next_checks": next_checks,
            "f_layer_status": str(summary_payload.get("status") or "failed"),
            "agent_run_id": formal_result.run_id,
            "evidence_package_hash": evidence_package_hash,
            "evidence_summary_hash": getattr(evidence_aggregation, "summary_hash", "") if evidence_aggregation else "",
        }
    return views


_DEFAULT_RESEARCH_AI_HELPER = analyze_trade_observation_with_ai

REFRESH_STAGE_ORDER: tuple[RefreshStage, ...] = (
    "cache_snapshot",
    "watch_quotes",
    "current_detail",
    "watch_details",
    "market_context",
    "v1_watch_snapshots",
    "pit_status",
)
REFRESH_STAGE_LABELS: dict[RefreshStage, str] = {
    "cache_snapshot": "缓存秒显",
    "watch_quotes": "自选实时行情",
    "current_detail": "当前个股深度",
    "watch_details": "自选详情补齐",
    "market_context": "市场辅助数据",
    "v1_watch_snapshots": "筛选快照入口",
    "pit_status": "PIT入口",
}
REFRESH_STATUS_LABELS = {
    "running": "运行中",
    "success": "成功",
    "partial": "部分源降级",
    "failed": "失败",
    "skipped": "跳过",
}

# Navigation may request only a cheap quote refresh.  Expensive dependencies
# (individual research detail, factor snapshots and PIT) are prepared by the
# action that actually needs their result through DataReadinessCoordinator.
AUTO_REFRESH_PAGES = frozenset({"home", "watchlist", "alerts"})
AUTO_DATA_REFRESH_INTERVALS = (15, 30, 60, 300)

RESEARCH_SPLITTER_INITIAL_SIZES = (220, 820, 360)
RESEARCH_RIGHT_PANEL_MIN_WIDTH = 360
RESEARCH_EDGE_REVEAL_DISTANCE = 24
RESEARCH_EDGE_BUTTON_WIDTH = 32
RESEARCH_EDGE_BUTTON_HEIGHT = 48


def _load_qt():
    from PySide6.QtCore import QEvent, QObject, Qt, QTimer, QUrl, Signal, Slot
    from PySide6.QtGui import QColor, QCursor, QDesktopServices, QFontDatabase, QIcon, QPixmap
    from PySide6.QtWebChannel import QWebChannel
    from PySide6.QtWebEngineWidgets import QWebEngineView
    from PySide6.QtWidgets import (
        QApplication,
        QCheckBox,
        QComboBox,
        QFileDialog,
        QFrame,
        QGridLayout,
        QHBoxLayout,
        QInputDialog,
        QLabel,
        QLayout,
        QLineEdit,
        QListWidget,
        QListWidgetItem,
        QMainWindow,
        QMessageBox,
        QPushButton,
        QScrollArea,
        QSizePolicy,
        QSplitter,
        QStackedWidget,
        QTabWidget,
        QTableWidgetItem,
        QTextBrowser,
        QToolButton,
        QVBoxLayout,
        QWidget,
    )

    return {
        "QEvent": QEvent,
        "QObject": QObject,
        "Qt": Qt,
        "QTimer": QTimer,
        "QUrl": QUrl,
        "Signal": Signal,
        "Slot": Slot,
        "QWebChannel": QWebChannel,
        "QWebEngineView": QWebEngineView,
        "QApplication": QApplication,
        "QCheckBox": QCheckBox,
        "QComboBox": QComboBox,
        "QColor": QColor,
        "QCursor": QCursor,
        "QDesktopServices": QDesktopServices,
        "QFontDatabase": QFontDatabase,
        "QIcon": QIcon,
        "QPixmap": QPixmap,
        "QFileDialog": QFileDialog,
        "QFrame": QFrame,
        "QGridLayout": QGridLayout,
        "QHBoxLayout": QHBoxLayout,
        "QInputDialog": QInputDialog,
        "QLabel": QLabel,
        "QLayout": QLayout,
        "QLineEdit": QLineEdit,
        "QListWidget": QListWidget,
        "QListWidgetItem": QListWidgetItem,
        "QMainWindow": QMainWindow,
        "QMessageBox": QMessageBox,
        "QPushButton": QPushButton,
        "QScrollArea": QScrollArea,
        "QSizePolicy": QSizePolicy,
        "QSplitter": QSplitter,
        "QStackedWidget": QStackedWidget,
        "QTabWidget": QTabWidget,
        "QTableWidgetItem": QTableWidgetItem,
        "QTextBrowser": QTextBrowser,
        "QToolButton": QToolButton,
        "QVBoxLayout": QVBoxLayout,
        "QWidget": QWidget,
    }


QT = _load_qt()
QEvent = QT["QEvent"]
QObject = QT["QObject"]
Qt = QT["Qt"]
QTimer = QT["QTimer"]
QUrl = QT["QUrl"]
Signal = QT["Signal"]
Slot = QT["Slot"]
QWebChannel = QT["QWebChannel"]
QWebEngineView = QT["QWebEngineView"]
QApplication = QT["QApplication"]
QCheckBox = QT["QCheckBox"]
QComboBox = QT["QComboBox"]
QColor = QT["QColor"]
QCursor = QT["QCursor"]
QDesktopServices = QT["QDesktopServices"]
QFontDatabase = QT["QFontDatabase"]
QIcon = QT["QIcon"]
QPixmap = QT["QPixmap"]
QFileDialog = QT["QFileDialog"]
QFrame = QT["QFrame"]
QGridLayout = QT["QGridLayout"]
QHBoxLayout = QT["QHBoxLayout"]
QInputDialog = QT["QInputDialog"]
QLabel = QT["QLabel"]
QLayout = QT["QLayout"]
QLineEdit = QT["QLineEdit"]
QListWidget = QT["QListWidget"]
QListWidgetItem = QT["QListWidgetItem"]
QMainWindow = QT["QMainWindow"]
QMessageBox = QT["QMessageBox"]
QPushButton = QT["QPushButton"]
QScrollArea = QT["QScrollArea"]
QSizePolicy = QT["QSizePolicy"]
QSplitter = QT["QSplitter"]
QStackedWidget = QT["QStackedWidget"]
QTabWidget = QT["QTabWidget"]
QTableWidgetItem = QT["QTableWidgetItem"]
QTextBrowser = QT["QTextBrowser"]
QToolButton = QT["QToolButton"]
QVBoxLayout = QT["QVBoxLayout"]
QWidget = QT["QWidget"]


def _health_status_item(status: str) -> QTableWidgetItem:
    item = QTableWidgetItem(f"● {status}")
    if status in {"正常", "成功"}:
        color = PALETTE["ok"]
    elif status in {"偏旧", "延迟", "部分使用缓存", "部分源降级", "运行中", "跳过"}:
        color = PALETTE["warn"]
    elif status in {"失败", "过旧", "暂不可用"}:
        color = PALETTE["triggered"]
    else:
        color = PALETTE["muted"]
    item.setForeground(QColor(color))
    return item


def _ui_resource_path(*parts: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    candidates = [
        base / "stock_tool" / "ui",
        base,
        Path(__file__).resolve().parent,
    ]
    for candidate in candidates:
        path = candidate.joinpath(*parts)
        if path.exists():
            return path
    return Path(__file__).resolve().parent.joinpath(*parts)


class IntelligencePanel(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("intelligencePanel")
        self.setMinimumHeight(220)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(0)
        self.scroll_area = QScrollArea()
        self.scroll_area.setObjectName("intelScroll")
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.content_widget = QWidget()
        self.content_widget.setObjectName("intelContent")
        self.content_widget.setMinimumWidth(0)
        self.content_widget.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self._content_layout = QVBoxLayout(self.content_widget)
        self._content_layout.setContentsMargins(12, 12, 12, 12)
        self._content_layout.setSpacing(10)
        self._content_layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        self.scroll_area.setWidget(self.content_widget)
        self._layout.addWidget(self.scroll_area)
        self.set_empty()

    def set_empty(self) -> None:
        self.set_sections(
            quote_metrics=[],
            trend_summary="加载个股后显示结构化情报。",
            ma_metrics=[],
            momentum_metrics=[],
            opportunities=[],
            risks=[],
            gaps=[],
        )

    def set_sections(
        self,
        *,
        quote_metrics: list[tuple[str, str]],
        trend_summary: str,
        ma_metrics: list[tuple[str, str]],
        momentum_metrics: list[tuple[str, str]],
        opportunities: list[str],
        risks: list[str],
        gaps: list[str],
    ) -> None:
        self._clear()
        self._add_metric_section("基础行情", quote_metrics)
        self._add_summary_section("趋势", trend_summary or "-")
        self._add_metric_section("均线", ma_metrics)
        self._add_metric_section("动能", momentum_metrics)
        self._add_pill_section("机会", opportunities, "opportunity")
        self._add_pill_section("风险", risks, "risk")
        self._add_pill_section("数据缺口", gaps, "gap")
        self._refresh_content_geometry()

    def _clear(self) -> None:
        while self._content_layout.count():
            item = self._content_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()

    def _section_frame(self, title: str) -> tuple[QFrame, QVBoxLayout]:
        frame = QFrame()
        frame.setObjectName("intelSection")
        frame.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(0, 0, 0, 8)
        layout.setSpacing(6)
        label = QLabel(title)
        label.setObjectName("intelSectionTitle")
        label.setMinimumWidth(0)
        layout.addWidget(label)
        return frame, layout

    def _add_summary_section(self, title: str, text: str) -> None:
        frame, layout = self._section_frame(title)
        summary = QLabel(text)
        summary.setObjectName("intelSummary")
        summary.setWordWrap(True)
        summary.setMinimumWidth(0)
        summary.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        layout.addWidget(summary)
        self._content_layout.addWidget(frame)

    def _add_metric_section(self, title: str, metrics: list[tuple[str, str]]) -> None:
        frame, layout = self._section_frame(title)
        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(5)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 1)
        for column in range(4):
            grid.setColumnMinimumWidth(column, 0)
        if not metrics:
            metrics = [("状态", "-")]
        for index, (name, value) in enumerate(metrics):
            row = index // 2
            col = (index % 2) * 2
            key = QLabel(name)
            key.setObjectName("intelMetricLabel")
            key.setMinimumWidth(0)
            key.setWordWrap(True)
            key.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Preferred)
            val = QLabel(value)
            val.setObjectName("intelMetricValue")
            val.setMinimumWidth(0)
            val.setWordWrap(True)
            val.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            grid.addWidget(key, row, col)
            grid.addWidget(val, row, col + 1)
        layout.addLayout(grid)
        self._content_layout.addWidget(frame)

    def _add_pill_section(self, title: str, items: list[str], tier: str) -> None:
        frame, layout = self._section_frame(title)
        flow = FlowLayout(spacing=6)
        deduped = [str(item).strip() for item in dict.fromkeys(items or []) if str(item).strip()]
        if not deduped:
            deduped = ["暂无"]
            tier = "info" if title != "数据缺口" else "gap"
        for item in deduped:
            pill = QLabel(item)
            pill.setProperty("tier", tier)
            pill.setWordWrap(True)
            pill.setMinimumWidth(0)
            pill.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            flow.addWidget(pill)
        layout.addLayout(flow)
        self._content_layout.addWidget(frame)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._refresh_content_geometry()

    def _refresh_content_geometry(self) -> None:
        viewport_width = self.scroll_area.viewport().width()
        if viewport_width <= 0:
            return
        max_pill_width = max(1, viewport_width - 24)
        for pill in self.content_widget.findChildren(QLabel):
            if pill.property("tier"):
                pill.setMaximumWidth(max_pill_width)
        self._content_layout.invalidate()
        self._content_layout.activate()
        content_height = max(
            self._content_layout.minimumSize().height(),
            self._content_layout.totalHeightForWidth(viewport_width),
        )
        self.content_widget.setMinimumHeight(content_height)
        self.content_widget.updateGeometry()
        self.scroll_area.updateGeometry()


def _configure_research_splitter(splitter: QSplitter, right_panel: QWidget) -> None:
    """Keep the research intelligence panel shrinkable, but never collapsible."""
    right_index = splitter.indexOf(right_panel)
    if right_index < 0:
        raise ValueError("research splitter right panel must be added before it is configured")
    right_panel.setMinimumWidth(RESEARCH_RIGHT_PANEL_MIN_WIDTH)
    splitter.setCollapsible(right_index, False)
    splitter.setSizes(list(RESEARCH_SPLITTER_INITIAL_SIZES))


class ResearchEdgeToggle(QObject):
    """Hide/show the research side panel without making it unrecoverable."""

    def __init__(self, page: QWidget, splitter: QSplitter, right_panel: QWidget) -> None:
        super().__init__(page)
        self.page = page
        self.splitter = splitter
        self.right_panel = right_panel
        self._right_index = splitter.indexOf(right_panel)
        if self._right_index < 0:
            raise ValueError("research splitter right panel must be added before the edge toggle")
        self._restore_sizes = list(RESEARCH_SPLITTER_INITIAL_SIZES)

        self.button = QToolButton(page)
        self.button.setObjectName("researchEdgeToggle")
        self.button.setFixedSize(RESEARCH_EDGE_BUTTON_WIDTH, RESEARCH_EDGE_BUTTON_HEIGHT)
        self.button.setFocusPolicy(Qt.StrongFocus)
        self.button.setAccessibleName("右侧情报栏切换")
        self.button.clicked.connect(self.toggle)
        self._set_expanded_state()
        self.button.hide()

        page.setMouseTracking(True)
        for child in page.findChildren(QWidget):
            child.setMouseTracking(True)
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)
        self._position_button()

    def eventFilter(self, watched, event) -> bool:
        if event.type() == QEvent.Resize and watched is self.page:
            self._position_button()
        elif event.type() in (QEvent.Enter, QEvent.MouseMove):
            self._update_edge_visibility()
        return super().eventFilter(watched, event)

    def _position_button(self) -> None:
        self.button.move(
            max(0, self.page.width() - self.button.width()),
            max(0, (self.page.height() - self.button.height()) // 2),
        )
        self.button.raise_()

    def _update_edge_visibility(self) -> None:
        if not self.page.isVisible() or self.page.width() <= 0:
            self.button.hide()
            return
        page_pos = self.page.mapFromGlobal(QCursor.pos())
        near_edge = (
            self.page.rect().contains(page_pos)
            and page_pos.x() >= self.page.width() - RESEARCH_EDGE_REVEAL_DISTANCE
        )
        if near_edge:
            self._position_button()
            self.button.show()
            self.button.raise_()
        elif not self.button.underMouse() and not self.button.hasFocus():
            self.button.hide()

    def _set_expanded_state(self) -> None:
        self.button.setText("›")
        self.button.setToolTip("隐藏右侧情报栏")
        self.button.setAccessibleDescription("收起右侧情报栏，让中间图表获得更多空间。")

    def _set_collapsed_state(self) -> None:
        self.button.setText("‹")
        self.button.setToolTip("显示右侧情报栏")
        self.button.setAccessibleDescription("恢复右侧情报栏，并保留上次使用的宽度。")

    def toggle(self) -> None:
        sizes = self.splitter.sizes()
        if self.right_panel.isVisible():
            if len(sizes) > self._right_index and sizes[self._right_index] > 0:
                self._restore_sizes = list(sizes)
            self.right_panel.hide()
            hidden_sizes = list(sizes)
            hidden_sizes[self._right_index] = 0
            hidden_sizes[1] += sizes[self._right_index]
            self.splitter.setSizes(hidden_sizes)
            self._set_collapsed_state()
            return

        self.right_panel.show()
        self.splitter.setSizes(list(self._restore_sizes))
        self._set_expanded_state()


class WorkSignals(QObject):
    done = Signal(object)
    failed = Signal(str)
    progress = Signal(object)


class ChartBridge(QObject):
    chartEvent = Signal(dict)

    @Slot(str)
    def emitEvent(self, payload: str) -> None:
        try:
            event = json.loads(payload)
        except json.JSONDecodeError:
            event = {"type": "invalid", "raw": payload}
        self.chartEvent.emit(event)


class StockWorkbenchWindow(QMainWindow):
    def __init__(self, *, notifier: Notifier | None = None) -> None:
        super().__init__()
        self.setWindowTitle(QT_WINDOW_TITLE)
        icon_path = _ui_resource_path("web", "stocklens_icon.png")
        if icon_path.exists():
            self.setWindowIcon(QIcon(str(icon_path)))
        self.resize(1440, 860)
        self.setMinimumSize(1180, 720)

        self.data_dir = self._ensure_data_dir()
        self.cache = CacheStore(db_path_for(self.data_dir))
        self.alert_service = AlertService(self.cache)
        self.secret_store = SecretStore(self.cache, use_keyring=True)
        self.secret_store.migrate_legacy_secret(AI_API_KEY_NAME)
        self.secret_store.migrate_legacy_secret(TUSHARE_TOKEN_NAME)
        self.secret_store.migrate_legacy_secret(TICKFLOW_API_KEY_NAME)
        self.fetcher = DataFetcher(
            self.cache,
            tushare_token_provider=self._pit_token_text,
            tickflow_api_key_provider=lambda: self.secret_store.get_secret(TICKFLOW_API_KEY_NAME),
        )
        self.alert_notifier = notifier or Notifier([WindowsToastChannel()])
        self.alert_monitor = AlertMonitorCoordinator(self.alert_service, self.fetcher, notifier=self.alert_notifier)
        self.refresh_coordinator = DataRefreshCoordinator(self.cache, self.fetcher)
        self.v1_database: V1Database | None = None
        self.factor_store: V1FactorStore | None = None
        self.strategy_store: StrategyStore | None = None
        self.performance_store: PerformanceStore | None = None
        self.observation_outcome_store: ObservationOutcomeStore | None = None
        self.lens_evaluation_store: LensEvaluationStore | None = None
        self.agent_evaluation_store: AgentEvaluationStore | None = None
        self.data_quality_evaluation_store: DataQualityEvaluationStore | None = None
        self.evaluation_error_message = ""
        self.v1_error_message = ""
        self.pit_database: PITDatabase | None = None
        self.pit_error_message = ""
        self._init_v1_factor_store()
        self._feature_trading_calendar = TradingCalendar()
        self.data_readiness = DataReadinessCoordinator(
            cache=self.cache,
            fetcher=self.fetcher,
            factor_store=self.factor_store,
            policy=DataFreshnessPolicy(
                self.cache,
                trading_day_fn=self._feature_trading_calendar.is_trading_day,
            ),
        )
        self._init_pit_database()
        self.data_readiness.inspectors["pit_incremental"] = self._inspect_pit_incremental_readiness
        self.data_readiness.refreshers["pit_incremental"] = self._refresh_pit_incremental_readiness
        self.candidate_state_store = None
        self.candidate_state_service = None
        self.candidate_state_error = ""
        self.formal_evidence_store = None
        self.formal_evidence_error = ""
        self.exploratory_proposal_store = None
        self.exploratory_proposal_error = ""
        if self.performance_store is not None:
            try:
                database_path = str(self.performance_store.database.db_path)
                self.observation_outcome_store = ObservationOutcomeStore(database_path)
                self.lens_evaluation_store = LensEvaluationStore(self.observation_outcome_store)
                self.agent_evaluation_store = AgentEvaluationStore(database_path)
                self.data_quality_evaluation_store = DataQualityEvaluationStore(database_path)
                self.evaluation_error_message = ""
            except Exception as exc:
                self.evaluation_error_message = str(exc)
            try:
                self.candidate_state_store = SqliteCandidateStateStore(
                    str(self.performance_store.database.db_path)
                )
                self.candidate_state_service = CandidateStateService(self.candidate_state_store)
            except Exception as exc:
                self.candidate_state_error = str(exc)
            try:
                self.formal_evidence_store = SqliteEvidenceRunStore(
                    str(self.performance_store.database.db_path)
                )
            except Exception as exc:
                self.formal_evidence_error = str(exc)
            try:
                self.exploratory_proposal_store = SqliteExploratoryProposalStore(
                    str(self.performance_store.database.db_path)
                )
            except Exception as exc:
                self.exploratory_proposal_error = str(exc)
        self.review_service = ReviewWorkflowService(self.cache, self.performance_store)
        self.review_automation = ReviewAutomationCoordinator(
            self.cache,
            self.review_service,
            self.performance_store,
            trading_clock=TradingClock(),
        )

        self.current_quote: StockQuote | None = None
        self.current_metrics: FinancialMetrics | None = None
        self.current_history = pd.DataFrame()
        self.current_payload: dict[str, Any] | None = None
        self.current_trade_reference: TradeReference | None = None
        self.current_observation_note: str = ""
        self._observation_save_block_message: str | None = None
        self._stock_load_version = 0
        self._observer_state_version = 0
        self._pending_generate_observation_code: str | None = None
        self._running_tasks: set[str] = set()
        self._active_background_signals: set[WorkSignals] = set()
        self._active_background_threads: set[threading.Thread] = set()
        self._refresh_stage_results: dict[RefreshStage, RefreshStepResult] = {}
        self._last_refresh_summary: RefreshRunSummary | None = None
        self._active_data_refresh_trigger: RefreshTrigger | None = None
        self.current_query_spec: QuerySpec | None = None
        self.current_screen_report: ScreenReport | None = None
        self.current_screen_rows: list[dict[str, Any]] = []
        self.current_screener_run_id: int | None = None
        self.current_screening_mode = "review_only"
        self.current_agent_orchestration: AgentOrchestrationResult | None = None
        self.current_agent_orchestrations: list[AgentOrchestrationResult] = []
        self.current_evidence_aggregation: Any | None = None
        self.current_evidence_aggregations: list[Any] = []
        self.current_candidate_evidence: dict[str, dict[str, Any]] = {}
        self.current_candidate_states: dict[str, dict[str, Any]] = {}
        self._formal_candidate_research_context = False
        self.current_strategy_preset_id: int | None = None
        self.current_template_contract: ScreenerPreset | None = None
        self._setting_screener_input = False
        self._builtin_screener_cards: dict[str, Card] = {}
        self._builtin_screener_card_status: dict[str, QLabel] = {}
        self.action_registry: dict[str, ActionDescriptor] = {}
        self._alert_check_in_progress = False
        self._alert_monitor_timer = QTimer(self)
        self._alert_monitor_timer.setSingleShot(True)
        self._alert_monitor_timer.timeout.connect(self._alert_monitor_tick)
        self._review_automation_timer = QTimer(self)
        self._review_automation_timer.setSingleShot(True)
        self._review_automation_timer.timeout.connect(self._review_automation_tick)
        self._auto_data_refresh_timer = QTimer(self)
        self._auto_data_refresh_timer.setSingleShot(True)
        self._auto_data_refresh_timer.timeout.connect(self._auto_data_refresh_tick)
        self._review_automation_pending_after_refresh = False
        self._last_review_automation_report: AutomationRunReport | None = None
        self._ui_generation = 0
        self._window_closing = False
        self._chart_ready = False
        self._auto_refresh_ready = False

        self.bridge = ChartBridge()
        self.bridge.chartEvent.connect(self._on_chart_event)

        self._register_fonts()
        self._build_ui()
        self._load_chart_page()
        self._load_watchlist()
        self._refresh_dashboard_snapshot()
        self._fill_fetch_log_table()
        self._start_data_refresh("startup")
        self._auto_refresh_ready = True

    def _ensure_data_dir(self) -> Path:
        data_dir = read_data_dir()
        if data_dir is not None:
            return data_dir
        configured_data_dir = read_configured_data_dir()
        if configured_data_dir is not None:
            QMessageBox.warning(
                self,
                "原数据目录不可访问",
                "上次记录的数据存储文件夹当前不可访问：\n"
                f"{configured_data_dir}\n\n"
                "请先恢复原目录，或选择新的数据目录；原目录中的个人数据不会自动迁移。",
            )
        selected = QFileDialog.getExistingDirectory(
            self,
            "请选择 A股选股仪 数据存储文件夹",
            str(Path.home()),
        )
        if not selected:
            raise SystemExit("未选择数据文件夹")
        data_dir = Path(selected).resolve()
        write_data_dir(data_dir)
        return data_dir

    def _init_v1_factor_store(self) -> None:
        try:
            self.v1_database = V1Database(v1_db_path_for(self.data_dir))
            self.factor_store = V1FactorStore(self.v1_database)
            self.strategy_store = StrategyStore(self.v1_database)
            self.performance_store = PerformanceStore(self.v1_database)
            self.v1_error_message = ""
        except Exception as exc:
            self.v1_database = None
            self.factor_store = None
            self.strategy_store = None
            self.performance_store = None
            self.v1_error_message = str(exc)

    def _init_pit_database(self) -> None:
        try:
            self.pit_database = PITDatabase(pit_db_path_for(self.data_dir))
            self.pit_error_message = ""
        except Exception as exc:
            self.pit_database = None
            self.pit_error_message = str(exc)

    def _sync_data_readiness_dependencies(self) -> None:
        """Keep the core coordinator aligned with swappable UI seams.

        Qt smoke fixtures and recovery flows can replace a fetcher after the
        workbench starts.  Readiness must use that same instance rather than
        opening an unrelated live-data path.
        """

        self.data_readiness.fetcher = self.fetcher
        self.data_readiness.factor_store = self.factor_store

    def _inspect_pit_incremental_readiness(self, _requirement: DataRequirement) -> dict[str, Any]:
        """Expose PIT completion state through the same freshness protocol.

        A populated table alone is not sufficient: the last incremental job
        must have completed successfully and still be within the configured
        TTL.  This lets a future PIT-dependent feature fail closed instead of
        treating an old local database as current data.
        """

        if self.pit_database is None:
            return {"fresh": False, "errors": ("PIT 数据库不可用",)}
        try:
            job = next(
                (
                    item
                    for item in self.pit_database.list_recent_jobs(limit=50)
                    if item.mode == "incremental" and item.status == "completed"
                ),
                None,
            )
            latest_asof = self.pit_database.latest_asof()
        except Exception as exc:
            return {"fresh": False, "errors": (f"读取 PIT 日增量状态失败：{exc}",)}
        if job is None:
            return {"fresh": False, "errors": ("未找到成功完成的 PIT 日增量",)}
        completed_at = job.completed_at or job.updated_at
        errors: list[str] = []
        if not latest_asof:
            errors.append("PIT 日增量没有可用的 as-of 数据")
        if not self.data_readiness.policy.is_fresh("pit_incremental", completed_at):
            errors.append("PIT 日增量已超过有效期")
        return {
            "fresh": not errors,
            "updated_at": completed_at,
            "snapshot_id": f"pit:{latest_asof}" if latest_asof else "",
            "refresh_event_id": f"pit-job:{job.id}",
            "errors": tuple(errors),
        }

    def _refresh_pit_incremental_readiness(
        self,
        _requirement: DataRequirement,
        progress: Callable[[RefreshStepResult], None] | None = None,
    ) -> RefreshStepResult:
        """Run the existing incremental builder as a readiness refresh seam."""

        result = self._execute_pit_build(
            "incremental",
            start_date="2019-01-01",
            end_date=date.today().isoformat(),
            universe_source="baostock",
        )
        completed = str(getattr(result, "status", "")) == "completed"
        done = max(0, int(getattr(result, "done", 0) or 0))
        failed = max(0, int(getattr(result, "failed", 0) or 0))
        message = str(getattr(result, "message", "") or "PIT 日增量完成")
        step = RefreshStepResult(
            stage="pit_incremental",  # type: ignore[arg-type]
            status="success" if completed else "failed",
            done=done,
            total=max(1, done + failed),
            fresh=1 if completed else 0,
            failed=0 if completed else max(1, failed),
            message=message,
            errors=[] if completed else [message],
        )
        if progress is not None:
            progress(step)
        return step

    def _quote_from_feature_readiness_cache(self, fallback: StockQuote) -> StockQuote:
        """Use the exact quote prepared for a feature when it is available."""

        try:
            record = self.cache.get_json(f"feature_quote:{fallback.code}")
            payload = getattr(record, "payload", {}) if record is not None else {}
            quote_payload = payload.get("quote") if isinstance(payload, dict) else None
            if isinstance(quote_payload, dict):
                return StockQuote(**quote_payload)
        except (TypeError, ValueError):
            pass
        return fallback

    def _prepared_quote_provider(self, codes: tuple[str, ...]) -> _PreparedQuoteProvider:
        """Build a review-automation provider from the readiness quote cache.

        The automation service deliberately checks quote timestamps again. By
        passing these snapshots instead of the live fetcher, its check is
        performed against exactly the data that satisfied readiness and does
        not trigger a second same-code fetch in the background.
        """

        snapshots: dict[str, QuoteSnapshot] = {}
        for code in codes:
            record = self.cache.get_json(f"feature_quote:{code}")
            payload = getattr(record, "payload", {}) if record is not None else {}
            quote_payload = payload.get("quote") if isinstance(payload, dict) else None
            quote_at_text = payload.get("quote_at") if isinstance(payload, dict) else None
            if not isinstance(quote_payload, dict) or not quote_at_text:
                raise ValueError(f"{code}缺少已准备的真实行情时间")
            try:
                quote_at = datetime.fromisoformat(str(quote_at_text))
                fetched_at_text = payload.get("fetched_at") or quote_at_text
                fetched_at = datetime.fromisoformat(str(fetched_at_text))
                quote = StockQuote(**quote_payload)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{code}已准备行情无法解析：{exc}") from exc
            snapshots[code] = QuoteSnapshot(
                quote,
                str(payload.get("source") or "feature_readiness"),
                quote_at,
                fetched_at,
            )
        return _PreparedQuoteProvider(snapshots)

    def _pit_token_text(self) -> str:
        return (
            self.secret_store.get_secret(TUSHARE_TOKEN_NAME)
            or os.environ.get("TUSHARE_TOKEN")
            or os.environ.get("TS_TOKEN")
            or ""
        )

    def _pit_token_available(self) -> bool:
        return bool(self._pit_token_text().strip())

    def _register_action(
        self,
        action_id: str,
        label: str,
        surface: str,
        kind: str,
        run: Callable[[], Any] | None = None,
        *,
        risk_level: str = "low",
        preconditions: tuple[str, ...] = (),
        status_message: str = "",
    ) -> ActionDescriptor:
        descriptor = ActionDescriptor(
            id=action_id,
            label=label,
            surface=surface,
            kind=kind,  # type: ignore[arg-type]
            risk_level=risk_level,
            preconditions=preconditions,
            run=run,
            status_message=status_message,
        )
        self.action_registry[action_id] = descriptor
        return descriptor

    def _bind_action(
        self,
        widget,
        action_id: str,
        label: str,
        surface: str,
        kind: str,
        run: Callable[[], Any] | None = None,
        *,
        risk_level: str = "low",
        preconditions: tuple[str, ...] = (),
        status_message: str = "",
    ) -> None:
        descriptor = self._register_action(
            action_id,
            label,
            surface,
            kind,
            run,
            risk_level=risk_level,
            preconditions=preconditions,
            status_message=status_message,
        )
        widget.setProperty("action_id", descriptor.id)
        widget.setProperty("action_kind", descriptor.kind)
        if hasattr(widget, "setToolTip") and not widget.toolTip():
            widget.setToolTip(descriptor.status_message or descriptor.label)

    def _build_ui(self) -> None:
        root = QWidget()
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(14, 12, 14, 10)
        root_layout.setSpacing(10)
        self.setCentralWidget(root)

        header = QHBoxLayout()
        mark_path = _ui_resource_path("web", "stocklens_mark.png")
        if mark_path.exists():
            logo = QLabel()
            logo.setObjectName("brandMark")
            logo.setPixmap(
                QPixmap(str(mark_path)).scaled(34, 34, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            )
            logo.setFixedSize(34, 34)
            header.addWidget(logo, 0, Qt.AlignVCenter)
        title = QLabel(f"{PRODUCT_SHORT_NAME} {DISPLAY_VERSION}")
        title.setObjectName("title")
        subtitle = QLabel("个股研究、条件选股、自选股、预警与复盘")
        subtitle.setObjectName("subtitle")
        title_wrap = QVBoxLayout()
        title_wrap.addWidget(title)
        title_wrap.addWidget(subtitle)
        header.addLayout(title_wrap, 1)
        self.data_status_pill = QPushButton("数据状态 待刷新")
        self.data_status_pill.setObjectName("statusPill")
        self.data_status_pill.setToolTip(
            "进入数据功能会自动尝试更新；公开源不可用时保留缓存。点击查看刷新阶段和失败原因。"
        )
        self._bind_action(
            self.data_status_pill,
            "top.data_status",
            "数据状态",
            "topbar",
            "execute",
            self._on_data_status_clicked,
            preconditions=("存在数据目录",),
            status_message="运行自选优先刷新；失败后打开刷新中心。",
        )
        self.data_status_pill.clicked.connect(self._on_data_status_clicked)
        self.ai_status_pill = QLabel("AI Key 待检查")
        self.ai_status_pill.setObjectName("statusPill")
        self.status_label = QLabel("")
        self.status_label.setObjectName("statusPill")
        self.status_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        header.addStretch(1)
        header.addWidget(self.data_status_pill, 0, Qt.AlignRight | Qt.AlignVCenter)
        header.addWidget(self.ai_status_pill, 0, Qt.AlignRight | Qt.AlignVCenter)
        header.addWidget(self.status_label, 0, Qt.AlignRight | Qt.AlignVCenter)
        root_layout.addLayout(header)

        p7_banner_text = os.environ.get("STOCK_TOOL_P7_BANNER", "").strip()
        if p7_banner_text:
            self.p7_session_banner = QLabel(p7_banner_text)
            self.p7_session_banner.setObjectName("experimentBanner")
            self.p7_session_banner.setWordWrap(True)
            self.p7_session_banner.setTextInteractionFlags(Qt.TextSelectableByMouse)
            root_layout.addWidget(self.p7_session_banner)

        shell = QHBoxLayout()
        shell.setContentsMargins(0, 0, 0, 0)
        shell.setSpacing(10)
        self.sidebar = Sidebar()
        self.sidebar.navigated.connect(self._navigate)
        shell.addWidget(self.sidebar)

        self.stack = QStackedWidget()
        self.pages: dict[str, QWidget] = {}
        self._add_page("home", self._scroll_page(self._build_home_page()))
        self._add_page("research", self._build_research_page())
        self._add_page("screener", self._scroll_page(self._build_screener_placeholder_page()))
        self._add_page("watchlist", self._build_watchlist_page())
        self._add_page("alerts", self._scroll_page(self._build_alerts_page()))
        self._add_page("review", self._scroll_page(self._build_review_page()))
        self._add_page("data", self._scroll_page(self._build_data_status_page()))
        self._add_page("settings", self._scroll_page(self._build_settings_page()))
        shell.addWidget(self.stack, 1)
        root_layout.addLayout(shell, 1)

        footer = QHBoxLayout()
        disclaimer = QLabel(DISCLAIMER)
        disclaimer.setObjectName("footer")
        signature = QLabel("十三月年二 出品")
        signature.setObjectName("footer")
        signature.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        footer.addWidget(disclaimer, 1)
        footer.addWidget(signature, 0, Qt.AlignRight | Qt.AlignVCenter)
        root_layout.addLayout(footer)
        self._register_shell_actions()
        self._apply_style()
        self._navigate("home")

    def _register_shell_actions(self) -> None:
        for page_key, label in NAVIGATION_LABELS.items():
            self._register_action(
                f"nav.{page_key}",
                label,
                "sidebar",
                "navigate",
                lambda page_key=page_key: self._navigate(page_key),
                status_message=f"打开{label}页面。",
            )
        self._register_action(
            "nav.toggle",
            "折叠侧栏",
            "sidebar",
            "info",
            lambda: self.sidebar.set_collapsed(not self.sidebar._collapsed),
            status_message="切换侧栏宽度。",
        )
        for action_id, label, page_key in (
            ("home.quick.research", "搜索个股", "research"),
            ("home.quick.screener", "写筛选条件", "screener"),
            ("home.quick.watchlist", "看观察/复盘", "watchlist"),
        ):
            self._register_action(
                action_id,
                label,
                "home",
                "navigate",
                lambda page_key=page_key: self._navigate(page_key),
                status_message=f"跳转到{label}。",
            )
        for action_id, label, page_key in (
            ("home.kpi.watchlist", "打开自选股", "watchlist"),
            ("home.kpi.alerts", "打开价格预警", "alerts"),
            ("home.kpi.review", "打开观察与复盘", "review"),
            ("home.kpi.data", "打开数据与更新", "data"),
        ):
            self._register_action(
                action_id,
                label,
                "home",
                "navigate",
                lambda page_key=page_key: self._navigate(page_key),
                status_message=f"跳转到{label}。",
            )
        self._register_action(
            "home.search.submit",
            "首页搜索跳转",
            "home",
            "navigate",
            lambda: self._route_search(self.home_page.search_input.text()),
            preconditions=("输入股票代码/名称或筛选条件",),
            status_message="按输入内容跳转到个股研究或条件选股。",
        )
        self._register_action("home.mode.stock", "首页股票模式", "home", "info", lambda: self.home_page._force_intent("stock"))
        self._register_action("home.mode.filter", "首页筛选模式", "home", "info", lambda: self.home_page._force_intent("filter"))
        for action_id, label, kind, run in (
            ("watchlist.refresh", "刷新行情", "execute", lambda: self._start_data_refresh("manual")),
            ("watchlist.open", "查看自选股", "navigate", self.watchlist_page._open_selected),
            ("watchlist.observe", "生成观察记录", "execute", self.watchlist_page._generate_selected),
            ("watchlist.remove", "移出自选", "execute", self.watchlist_page._remove_selected),
        ):
            self._register_action(
                action_id,
                label,
                "watchlist",
                kind,
                run,
                preconditions=("选择自选股",) if action_id != "watchlist.refresh" else (),
            )

    def _add_page(self, key: str, page: QWidget) -> None:
        self.pages[key] = page
        self.stack.addWidget(page)

    def _scroll_page(self, page: QWidget) -> QScrollArea:
        scroll = QScrollArea()
        scroll.setObjectName("pageScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setWidget(page)
        return scroll

    def _build_home_page(self) -> HomeDashboard:
        self.home_page = HomeDashboard()
        self.home_page.searchSubmitted.connect(self._route_search)
        self.home_page.quickAction.connect(self._handle_quick_action)
        self.home_page.kpiAction.connect(self._navigate)
        self.home_page.openStock.connect(self._open_stock_from_row)
        return self.home_page

    def _handle_quick_action(self, key: str) -> None:
        if key == "refresh":
            self._start_data_refresh("manual")
            return
        self._navigate(key)

    def _build_research_page(self) -> QWidget:
        page = QWidget()
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(0, 0, 0, 0)
        page_layout.setSpacing(8)

        splitter = QSplitter(Qt.Horizontal)
        splitter.setObjectName("researchSplitter")
        page_layout.addWidget(splitter, 1)

        left = QWidget()
        left.setObjectName("panel")
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(12, 12, 12, 12)
        left_layout.setSpacing(8)
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("输入代码/名称")
        self.search_input.returnPressed.connect(self.search_stock)
        search_button = QPushButton("搜索")
        search_button.setObjectName("primaryBtn")
        self._bind_action(
            search_button,
            "research.search",
            "搜索股票",
            "research",
            "execute",
            self.search_stock,
            preconditions=("输入股票代码或名称",),
        )
        search_button.clicked.connect(lambda: self.search_stock())
        row = QHBoxLayout()
        row.addWidget(self.search_input, 1)
        row.addWidget(search_button)
        left_layout.addLayout(row)
        self.search_list = QListWidget()
        self.search_list.itemActivated.connect(self._load_selected_item)
        self.search_list.itemClicked.connect(self._load_selected_item)
        left_layout.addWidget(self.search_list, 2)
        watch_label = QLabel("自选股")
        watch_label.setObjectName("sectionLabel")
        left_layout.addWidget(watch_label)
        self.watch_list = QListWidget()
        self.watch_list.itemActivated.connect(self._load_watch_item)
        self.watch_list.itemClicked.connect(self._load_watch_item)
        left_layout.addWidget(self.watch_list, 1)
        add_watch = QPushButton("加入自选")
        self._bind_action(
            add_watch,
            "research.add_watch",
            "加入自选",
            "research",
            "execute",
            self.add_current_watch,
            preconditions=("已加载股票",),
        )
        add_watch.clicked.connect(self.add_current_watch)
        refresh = QPushButton("刷新当前")
        self._bind_action(
            refresh,
            "research.refresh_current",
            "刷新当前股票",
            "research",
            "execute",
            lambda: self.load_stock(force=True),
            preconditions=("已加载股票",),
        )
        refresh.clicked.connect(lambda: self.load_stock(force=True))
        left_layout.addWidget(add_watch)
        left_layout.addWidget(refresh)
        splitter.addWidget(left)

        center = QWidget()
        center_layout = QVBoxLayout(center)
        center_layout.setContentsMargins(0, 0, 0, 0)
        center_layout.setSpacing(8)
        toolbar = QHBoxLayout()
        self.range_combo = self._combo([("全部", "all"), ("240日", "240d"), ("120日", "120d"), ("60日", "60d")])
        self.period_combo = self._combo([("日线", "daily"), ("周线", "weekly"), ("月线", "monthly")])
        self.mode_combo = self._combo([("K线", "candlestick"), ("趋势线", "line")])
        self.subchart_combo = self._combo([("成交量", "volume"), ("MACD", "macd"), ("RSI", "rsi"), ("KDJ", "kdj")])
        for label, combo in [("范围", self.range_combo), ("周期", self.period_combo), ("模式", self.mode_combo), ("副图", self.subchart_combo)]:
            toolbar.addWidget(QLabel(label))
            toolbar.addWidget(combo)
        toolbar.addStretch(1)
        center_layout.addLayout(toolbar)
        for combo in [self.range_combo, self.period_combo, self.mode_combo, self.subchart_combo]:
            combo.currentIndexChanged.connect(self._rebuild_payload_from_controls)
        self.web_view = QWebEngineView()
        self.web_view.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        center_layout.addWidget(self.web_view, 1)
        splitter.addWidget(center)

        right = QWidget()
        right.setObjectName("panel")
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(12, 12, 12, 12)
        self.stock_title = QLabel("请选择股票")
        self.stock_title.setObjectName("stockTitle")
        right_layout.addWidget(self.stock_title)
        self.hover_box = QTextBrowser()
        self.hover_box.setMaximumHeight(130)
        self.hover_box.setHtml("十字光标数据会显示在这里。")
        right_layout.addWidget(self.hover_box)
        self.intelligence_panel = IntelligencePanel()
        right_layout.addWidget(self.intelligence_panel, 2)
        self.report_box = QTextBrowser()
        self.report_box.setObjectName("reportBox")
        self.report_box.setMinimumHeight(180)
        right_layout.addWidget(self.report_box, 1)
        observer_buttons = QHBoxLayout()
        self.build_observer_button = QPushButton("生成本地观察")
        self.build_observer_button.setObjectName("primaryBtn")
        self._bind_action(
            self.build_observer_button,
            "research.generate_observation",
            "生成本地观察",
            "research",
            "execute",
            self.generate_trade_observation,
            preconditions=("已加载股票",),
            status_message="生成技术、资金、市场和事件证据摘要，不消耗 AI token。",
        )
        self.build_observer_button.clicked.connect(self.generate_trade_observation)
        self.ai_enhance_observer_button = QPushButton("AI证据整理")
        self._bind_action(
            self.ai_enhance_observer_button,
            "research.ai_observation",
            "AI证据整理",
            "research",
            "execute",
            self.analyze_trade_observation_with_ai,
            risk_level="medium",
            preconditions=("已生成本地观察", "已配置 API Key"),
            status_message="研究旁路只整理支持/反向证据和数据缺口，不改变候选集合；费用以服务商账单为准。",
        )
        self.ai_enhance_observer_button.clicked.connect(self.analyze_trade_observation_with_ai)
        self.save_watch_button = QPushButton("保存观察")
        self._bind_action(
            self.save_watch_button,
            "research.save_watch",
            "保存观察",
            "research",
            "execute",
            lambda: self.save_tracking_task("watch"),
            preconditions=("已生成本地观察",),
        )
        self.save_watch_button.clicked.connect(lambda: self.save_tracking_task("watch"))
        self.save_holding_button = QPushButton("保存持仓")
        self._bind_action(
            self.save_holding_button,
            "research.save_holding",
            "保存持仓观察",
            "research",
            "execute",
            lambda: self.save_tracking_task("holding"),
            preconditions=("已生成本地观察",),
        )
        self.save_holding_button.clicked.connect(lambda: self.save_tracking_task("holding"))
        observer_buttons.addWidget(self.build_observer_button)
        observer_buttons.addWidget(self.ai_enhance_observer_button)
        observer_buttons.addWidget(self.save_watch_button)
        observer_buttons.addWidget(self.save_holding_button)
        right_layout.addLayout(observer_buttons)
        self._refresh_observer_save_buttons()
        splitter.addWidget(right)
        _configure_research_splitter(splitter, right)
        self.research_edge_toggle = ResearchEdgeToggle(page, splitter, right)
        self._bind_action(
            self.research_edge_toggle.button,
            "research.toggle_right_panel",
            "切换右侧情报栏",
            "research",
            "info",
            self.research_edge_toggle.toggle,
            status_message="隐藏或恢复右侧情报栏，保留上次使用的宽度。",
        )
        return page

    def _build_screener_placeholder_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        title = QLabel(NAVIGATION_LABELS["screener"])
        title.setObjectName("pageTitle")
        layout.addWidget(title)

        mode_card = Card(
            "研究模式",
            "正式模式默认开启；复核和探索结果会带明显标识，且不会进入正式观察绩效样本。",
        )
        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel("当前模式"))
        self.screening_mode_combo = self._combo(SCREENING_MODE_OPTIONS)
        self.screening_mode_combo.setObjectName("screeningModeCombo")
        review_only_index = self.screening_mode_combo.findData("review_only")
        if review_only_index >= 0:
            self.screening_mode_combo.setCurrentIndex(review_only_index)
        self.screening_mode_combo.setToolTip(
            "formal 只接受通过质量闸门的正式候选；review_only 仅作历史复核；exploratory 不进入正式绩效样本。"
        )
        self.screening_mode_combo.currentIndexChanged.connect(self._on_screening_mode_changed)
        mode_row.addWidget(self.screening_mode_combo)
        self.screening_mode_notice = QLabel()
        self.screening_mode_notice.setWordWrap(True)
        self.screening_mode_notice.setObjectName("screenerModeNotice")
        mode_row.addWidget(self.screening_mode_notice, 1)
        mode_card.layout.addLayout(mode_row)
        self.selection_policy_notice = QLabel(
            "冻结规则：a_share_all / UniverseProfile v1（主板、创业板、科创板、北交所独立规则） · "
            "quality_research / trend_observation / event_verification · PriorityPolicy v1（无跨 Lens 总分）"
        )
        self.selection_policy_notice.setWordWrap(True)
        self.selection_policy_notice.setObjectName("selectionPolicyNotice")
        mode_card.layout.addWidget(self.selection_policy_notice)
        layout.addWidget(mode_card)
        self._refresh_screening_mode_notice()

        card = Card("筛选条件", "优先使用本地 V1 因子快照执行；不会因为点击筛选而自动长跑全A live 抓取。")
        self.screener_query_input = QLineEdit()
        self.screener_query_input.setPlaceholderText("例如：箱体横盘、ROE大于15、PE小于40、行业强")
        self.screener_query_input.returnPressed.connect(self.parse_screener_query)
        self.screener_query_input.textChanged.connect(self._on_screener_query_text_changed)
        card.layout.addWidget(self.screener_query_input)
        self.screener_strategy_name_input = QLineEdit()
        self.screener_strategy_name_input.setPlaceholderText("策略名称（同名保存会创建新版本）")
        card.layout.addWidget(self.screener_strategy_name_input)

        builtin_card = Card("内置研究模板", "模板条件是结构化契约；自然语言只用于展示和搜索。点击卡片会载入 v1 条件并绑定运行归因。")
        builtin_grid = QGridLayout()
        builtin_action_ids = (
            "screener.builtin.main_inflow_dip",
            "screener.builtin.strong_pullback",
            "screener.builtin.undervalued_value",
            "screener.builtin.high_growth",
            "screener.builtin.bluechip_stable",
            "screener.builtin.volume_price_rise",
            "screener.builtin.ma_bullish_breakout",
            "screener.builtin.box_sideways_quality",
        )
        for index, preset in enumerate(builtin_screener_presets()):
            action_id = f"screener.builtin.{preset.key}"
            if action_id not in builtin_action_ids:
                raise ValueError(f"内置筛选模板缺少正式 action 合同：{action_id}")
            item_card = Card(f"{preset.emoji} {preset.name} · {preset.template_version}", preset.description)
            contract_label = QLabel(preset.contract_text())
            contract_label.setWordWrap(True)
            contract_label.setObjectName("screenerTemplateContract")
            item_card.layout.addWidget(contract_label)
            status_label = QLabel("当前状态：待运行")
            status_label.setWordWrap(True)
            item_card.layout.addWidget(status_label)
            use_button = QPushButton(f"使用 {preset.name}")
            use_button.setObjectName(f"builtinScreener_{preset.key}")
            self._bind_action(
                use_button,
                action_id,
                f"使用 {preset.name}",
                "screener",
                "execute",
                lambda key=preset.key: self.apply_builtin_screener_preset(key),
                preconditions=("载入结构化内置模板",),
            )
            use_button.clicked.connect(lambda _checked=False, key=preset.key: self.apply_builtin_screener_preset(key))
            item_card.layout.addWidget(use_button)
            self._builtin_screener_cards[preset.key] = item_card
            self._builtin_screener_card_status[preset.key] = status_label
            builtin_grid.addWidget(item_card, index // 2, index % 2)
        builtin_card.layout.addLayout(builtin_grid)
        layout.addWidget(builtin_card)

        action_row = QHBoxLayout()
        self.screener_parse_button = QPushButton("解析条件")
        self._bind_action(
            self.screener_parse_button,
            "screener.parse",
            "解析筛选条件",
            "screener",
            "execute",
            self.parse_screener_query,
            preconditions=("输入筛选条件",),
        )
        self.screener_parse_button.clicked.connect(self.parse_screener_query)
        self.screener_run_button = QPushButton("执行筛选")
        self.screener_run_button.setObjectName("primaryBtn")
        self._bind_action(
            self.screener_run_button,
            "screener.run",
            "执行筛选",
            "screener",
            "execute",
            self.run_screener_query,
            preconditions=("已解析条件", "存在本地 V1 因子快照"),
        )
        self.screener_run_button.clicked.connect(self.run_screener_query)
        self.screener_save_button = QPushButton("保存策略")
        self._bind_action(
            self.screener_save_button,
            "screener.save_strategy",
            "保存策略",
            "screener",
            "execute",
            self.save_screener_strategy,
            preconditions=("已解析条件",),
        )
        self.screener_save_button.clicked.connect(self.save_screener_strategy)
        self.screener_history_button = QPushButton("查看历史")
        self._bind_action(
            self.screener_history_button,
            "screener.history",
            "查看筛选历史",
            "screener",
            "execute",
            self.refresh_screener_history,
        )
        self.screener_history_button.clicked.connect(self.refresh_screener_history)
        self.screener_export_csv_button = QPushButton("导出CSV")
        self._bind_action(
            self.screener_export_csv_button,
            "screener.export_csv",
            "导出CSV",
            "screener",
            "execute",
            self.export_screener_csv,
            preconditions=("已有筛选结果",),
        )
        self.screener_export_csv_button.clicked.connect(self.export_screener_csv)
        self.screener_export_json_button = QPushButton("导出JSON")
        self._bind_action(
            self.screener_export_json_button,
            "screener.export_json",
            "导出JSON",
            "screener",
            "execute",
            self.export_screener_json,
            preconditions=("已有筛选结果",),
        )
        self.screener_export_json_button.clicked.connect(self.export_screener_json)
        for button in (
            self.screener_parse_button,
            self.screener_run_button,
            self.screener_save_button,
            self.screener_history_button,
            self.screener_export_csv_button,
            self.screener_export_json_button,
        ):
            action_row.addWidget(button)
        action_row.addStretch(1)
        card.layout.addLayout(action_row)

        self.screener_summary_box = QTextBrowser()
        self.screener_summary_box.setMaximumHeight(150)
        self.screener_summary_box.setPlainText("输入筛选条件后先解析确认，系统会回显“我理解为”和数据来源。")
        card.layout.addWidget(self.screener_summary_box)
        layout.addWidget(card)

        result_card = Card(
            "候选证据列表",
            "Agent 只整理证据；候选资格和当前状态由本地规则维护。未运行 Agent 时显示“未运行”，不把缺失当作支持。",
        )
        self.screener_result_table = build_table([
            "序号",
            "代码",
            "名称",
            "Lens",
            "确定性资格",
            "当前候选状态",
            "数据时间/来源",
            "Agent 状态",
            "覆盖度",
            "支持证据",
            "反向证据",
            "冲突",
            "数据缺口",
            "后续核查",
        ])
        self.screener_result_table.itemSelectionChanged.connect(self._show_selected_candidate_detail)
        result_card.layout.addWidget(self.screener_result_table)
        result_actions = QHBoxLayout()
        self.screener_add_watch_button = QPushButton("人工加入观察")
        self.screener_add_watch_button.setObjectName("primaryBtn")
        self._bind_action(
            self.screener_add_watch_button,
            "screener.add_watch_manual",
            "人工加入观察",
            "screener",
            "execute",
            self.add_selected_candidate_to_watch,
            risk_level="medium",
            preconditions=("选中 candidate 状态", "明确人工动作"),
            status_message="仅由人工确认把 candidate 转为 watch；Agent 和本地规则不能自动加入观察。",
        )
        self.screener_add_watch_button.clicked.connect(self.add_selected_candidate_to_watch)
        result_actions.addWidget(self.screener_add_watch_button)
        self.screener_research_button = QPushButton("打开研究页整理证据")
        self._bind_action(
            self.screener_research_button,
            "screener.open_research",
            "打开研究页整理证据",
            "screener",
            "navigate",
            self.open_selected_candidate_research,
            preconditions=("选中候选",),
            status_message="打开个股研究页；AI 只整理当前快照证据，不改变候选集合。",
        )
        self.screener_research_button.clicked.connect(self.open_selected_candidate_research)
        result_actions.addWidget(self.screener_research_button)
        self.screener_retry_agents_button = QPushButton("重试五 Agent")
        self._bind_action(
            self.screener_retry_agents_button,
            "screener.retry_agents",
            "重试五 Agent",
            "screener",
            "execute",
            self.retry_screening_agents,
            preconditions=("已有筛选结果", "使用同一不可变快照"),
            status_message="只重试失败、跳过或未完成批次，不重新筛选、不创建重复候选。",
        )
        self.screener_retry_agents_button.clicked.connect(self.retry_screening_agents)
        result_actions.addWidget(self.screener_retry_agents_button)
        result_actions.addStretch(1)
        result_card.layout.addLayout(result_actions)
        self.screener_retryable_status_label = QLabel()
        self.screener_retryable_status_label.setObjectName("screenerRetryableStatus")
        self.screener_retryable_status_label.setWordWrap(True)
        result_card.layout.addWidget(self.screener_retryable_status_label)
        self.screener_candidate_detail = QTextBrowser()
        self.screener_candidate_detail.setObjectName("candidateEvidenceDetail")
        self.screener_candidate_detail.setMinimumHeight(190)
        self.screener_candidate_detail.setPlainText("选中候选后显示完整证据、数据来源、冲突、缺口和后续核查条件。")
        result_card.layout.addWidget(self.screener_candidate_detail)
        layout.addWidget(result_card, 1)

        history_card = Card("最近执行", "记录策略版本、数据来源、匹配数量和运行时间，便于复核。")
        self.screener_history_table = build_table(["时间", "来源", "匹配", "扫描", "摘要"])
        history_card.layout.addWidget(self.screener_history_table)
        layout.addWidget(history_card)
        strategy_card = Card("策略管理", "同名保存会保留版本历史；导入仅预览和校验，确认后再保存。")
        strategy_controls = QHBoxLayout()
        self.screener_strategy_combo = QComboBox()
        self.screener_strategy_combo.setMinimumWidth(240)
        load_strategy = QPushButton("载入")
        self._bind_action(load_strategy, "screener.strategy.load", "载入策略", "screener", "execute", self.load_screener_strategy)
        load_strategy.clicked.connect(self.load_screener_strategy)
        delete_strategy = QPushButton("删除")
        self._bind_action(delete_strategy, "screener.strategy.delete", "删除策略", "screener", "execute", self.delete_screener_strategy, risk_level="medium")
        delete_strategy.clicked.connect(self.delete_screener_strategy)
        import_strategy = QPushButton("导入预览")
        self._bind_action(import_strategy, "screener.strategy.import_preview", "导入策略预览", "screener", "execute", self.import_screener_strategy_preview)
        import_strategy.clicked.connect(self.import_screener_strategy_preview)
        for widget in (self.screener_strategy_combo, load_strategy, delete_strategy, import_strategy):
            strategy_controls.addWidget(widget)
        strategy_controls.addStretch(1)
        strategy_card.layout.addLayout(strategy_controls)
        version_controls = QHBoxLayout()
        self.screener_strategy_version_combo = QComboBox()
        self.screener_strategy_version_combo.setMinimumWidth(240)
        restore_strategy = QPushButton("恢复所选版本")
        self._bind_action(restore_strategy, "screener.strategy.restore", "恢复策略版本", "screener", "execute", self.restore_screener_strategy_version, risk_level="medium")
        restore_strategy.clicked.connect(self.restore_screener_strategy_version)
        compare_history = QPushButton("对比所选执行")
        self._bind_action(compare_history, "screener.history.compare", "与同策略上次比较", "screener", "execute", self.compare_selected_screener_run)
        compare_history.clicked.connect(self.compare_selected_screener_run)
        export_compare = QPushButton("导出比较 CSV")
        self._bind_action(export_compare, "screener.history.export_compare_csv", "导出筛选比较 CSV", "screener", "execute", self.export_selected_screener_comparison_csv)
        export_compare.clicked.connect(self.export_selected_screener_comparison_csv)
        for widget in (self.screener_strategy_version_combo, restore_strategy, compare_history, export_compare):
            version_controls.addWidget(widget)
        version_controls.addStretch(1)
        strategy_card.layout.addLayout(version_controls)
        layout.addWidget(strategy_card)
        self.screener_strategy_combo.currentIndexChanged.connect(self._refresh_screener_strategy_versions)
        self._refresh_screener_controls()
        self.refresh_screener_strategies()
        self.refresh_screener_history()
        return page

    def _build_watchlist_page(self) -> WatchlistPage:
        self.watchlist_page = WatchlistPage()
        self.watchlist_page.refreshRequested.connect(lambda: self._start_data_refresh("manual"))
        self.watchlist_page.openStock.connect(self._open_stock_from_row)
        self.watchlist_page.generateObservation.connect(self._generate_observation_from_row)
        self.watchlist_page.removeRequested.connect(self._remove_watch_code)
        return self.watchlist_page

    def _build_alerts_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        title = QLabel("价格预警")
        title.setObjectName("pageTitle")
        layout.addWidget(title)
        notice = QLabel(FREE_SOURCE_NOTICE)
        notice.setObjectName("infoNotice")
        notice.setWordWrap(True)
        self.alert_source_notice = notice
        layout.addWidget(notice)

        form_card = Card("预警设置", "软件关闭后不提醒；自动检查仅在交易时段运行。编辑会保留历史记录，并重新计算触发状态。")
        form = QGridLayout()
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(8)
        self.alert_code_input = QLineEdit()
        self.alert_code_input.setPlaceholderText("股票代码，如 000001")
        self.alert_name_input = QLineEdit()
        self.alert_name_input.setPlaceholderText("名称（可留空）")
        self.alert_kind_combo = self._combo([(label, key) for key, label in ALERT_KINDS.items()])
        self.alert_threshold_input = QLineEdit()
        self.alert_threshold_input.setPlaceholderText("阈值")
        self.alert_note_input = QLineEdit()
        self.alert_note_input.setPlaceholderText("备注（可选）")
        self.alert_unit_label = QLabel("单位：元")
        self.alert_repeat_check = QCheckBox("持续提醒（不勾选则触发一次后自动停用）")
        self.alert_save_button = QPushButton("新建预警")
        self.alert_save_button.setObjectName("primaryBtn")
        self._bind_action(self.alert_save_button, "alerts.create", "新建预警", "alerts", "execute", self.save_alert)
        self._register_action("alerts.edit", "编辑预警", "alerts", "execute", self.save_alert)
        self.alert_save_button.clicked.connect(self.save_alert)
        self.alert_new_button = QPushButton("新建第一条预警")
        self._bind_action(self.alert_new_button, "alerts.new", "清空预警表单", "alerts", "info", self.clear_alert_form)
        self.alert_new_button.clicked.connect(self.clear_alert_form)
        form.addWidget(QLabel("股票代码"), 0, 0)
        form.addWidget(self.alert_code_input, 0, 1)
        form.addWidget(QLabel("名称"), 0, 2)
        form.addWidget(self.alert_name_input, 0, 3)
        form.addWidget(QLabel("预警类型"), 1, 0)
        form.addWidget(self.alert_kind_combo, 1, 1)
        form.addWidget(QLabel("阈值"), 1, 2)
        form.addWidget(self.alert_threshold_input, 1, 3)
        form.addWidget(self.alert_unit_label, 2, 1)
        form.addWidget(self.alert_repeat_check, 2, 2, 1, 2)
        form.addWidget(QLabel("备注"), 3, 0)
        form.addWidget(self.alert_note_input, 3, 1, 1, 3)
        form.addWidget(self.alert_save_button, 4, 2)
        form.addWidget(self.alert_new_button, 4, 3)
        form_card.layout.addLayout(form)
        layout.addWidget(form_card)
        self.alert_kind_combo.currentIndexChanged.connect(self._refresh_alert_unit_label)

        control_card = Card("检查与监控", "自动检查只在软件运行且有效交易时段内执行；立即检查在非交易时段仅做快照预览，不写历史、不改变状态、不发通知。")
        controls = QHBoxLayout()
        self.alert_check_button = QPushButton("立即检查")
        self._bind_action(self.alert_check_button, "alerts.check", "立即检查预警", "alerts", "execute", lambda: self.run_alert_check(manual=True))
        self.alert_check_button.clicked.connect(lambda: self.run_alert_check(manual=True))
        self.alert_monitor_check = QCheckBox("交易时段自动检查")
        alert_policy = self.alert_monitor.policy()
        self.alert_monitor_check.setChecked(alert_policy.enabled)
        self.alert_monitor_check.toggled.connect(self._set_alert_monitor_enabled)
        self._register_action("alerts.monitor.toggle", "切换预警监控", "alerts", "execute", lambda: self._set_alert_monitor_enabled(self.alert_monitor_check.isChecked()))
        self.alert_interval_combo = self._combo([
            ("每 15 秒", "15"), ("每 30 秒", "30"), ("每 1 分钟", "60"),
            ("每 5 分钟", "300"), ("每 10 分钟", "600"), ("每 30 分钟", "1800"),
        ])
        saved_interval = str(alert_policy.configured_interval_seconds)
        index = self.alert_interval_combo.findData(saved_interval)
        self.alert_interval_combo.setCurrentIndex(index if index >= 0 else 1)
        self.alert_interval_combo.currentIndexChanged.connect(self._set_alert_monitor_interval)
        self._register_action("alerts.monitor.interval", "设置监控间隔", "alerts", "execute", self._set_alert_monitor_interval)
        self.alert_status_label = QLabel("")
        self.alert_status_label.setWordWrap(True)
        controls.addWidget(self.alert_check_button)
        controls.addWidget(self.alert_monitor_check)
        controls.addWidget(self.alert_interval_combo)
        controls.addWidget(self.alert_status_label, 1)
        control_card.layout.addLayout(controls)
        layout.addWidget(control_card)

        alert_card = Card("启用中的预警", "选中一行后可编辑、启停或删除。")
        self.alert_table = build_table(["ID", "代码", "名称", "条件", "状态", "类型", "触发次数", "备注"])
        self.alert_table.itemSelectionChanged.connect(self._load_selected_alert_for_edit)
        alert_card.layout.addWidget(self.alert_table)
        alert_actions = QHBoxLayout()
        self.alert_toggle_button = QPushButton("启用/停用")
        self._bind_action(self.alert_toggle_button, "alerts.toggle", "启用或停用预警", "alerts", "execute", self.toggle_selected_alert)
        self.alert_toggle_button.clicked.connect(self.toggle_selected_alert)
        self.alert_delete_button = QPushButton("删除")
        self._bind_action(self.alert_delete_button, "alerts.delete", "删除预警", "alerts", "execute", self.delete_selected_alert, risk_level="medium")
        self.alert_delete_button.clicked.connect(self.delete_selected_alert)
        alert_actions.addWidget(self.alert_toggle_button)
        alert_actions.addWidget(self.alert_delete_button)
        alert_actions.addStretch(1)
        alert_card.layout.addLayout(alert_actions)
        layout.addWidget(alert_card)

        history_card = Card("触发记录", "导出当前筛选范围为 UTF-8 BOM CSV；清空前会要求确认。")
        history_controls = QHBoxLayout()
        self.alert_history_scope = self._combo([("全部记录", "all"), ("今天", "today")])
        self.alert_history_scope.currentIndexChanged.connect(self._refresh_alert_history)
        self._register_action("alerts.history.scope", "切换预警记录范围", "alerts", "info", self._refresh_alert_history)
        refresh = QPushButton("刷新记录")
        self._bind_action(refresh, "alerts.history.refresh", "刷新预警记录", "alerts", "execute", self._refresh_alert_history)
        refresh.clicked.connect(self._refresh_alert_history)
        export = QPushButton("导出CSV")
        self._bind_action(export, "alerts.history.export_csv", "导出预警记录CSV", "alerts", "execute", self.export_alert_history_csv)
        export.clicked.connect(self.export_alert_history_csv)
        clear = QPushButton("清空记录")
        self._bind_action(clear, "alerts.history.clear", "清空预警记录", "alerts", "execute", self.clear_alert_history, risk_level="medium")
        clear.clicked.connect(self.clear_alert_history)
        for widget in (self.alert_history_scope, refresh, export, clear):
            history_controls.addWidget(widget)
        history_controls.addStretch(1)
        history_card.layout.addLayout(history_controls)
        self.alert_history_table = build_table(["时间", "代码", "名称", "条件", "价格", "涨跌幅", "数据源", "行情时间", "说明"])
        history_card.layout.addWidget(self.alert_history_table)
        layout.addWidget(history_card)
        layout.addStretch(1)

        self._editing_alert_id: int | None = None
        self._refresh_alerts_page()
        if self.alert_monitor_check.isChecked():
            self._set_alert_monitor_enabled(True)
        return page

    def _refresh_alert_unit_label(self) -> None:
        kind = str(self.alert_kind_combo.currentData() or "")
        self.alert_unit_label.setText("单位：%（例如 5 表示 5%）" if is_percent_kind(kind) else "单位：元")

    def _selected_alert_id(self) -> int | None:
        row = self.alert_table.currentRow() if hasattr(self, "alert_table") else -1
        item = self.alert_table.item(row, 0) if row >= 0 else None
        try:
            return int(item.text()) if item is not None else None
        except ValueError:
            return None

    def clear_alert_form(self) -> None:
        self._editing_alert_id = None
        self.alert_code_input.clear()
        self.alert_name_input.clear()
        self.alert_kind_combo.setCurrentIndex(0)
        self.alert_threshold_input.clear()
        self.alert_note_input.clear()
        self.alert_repeat_check.setChecked(False)
        self.alert_save_button.setText("新建预警")
        self._refresh_alert_unit_label()
        self.alert_code_input.setFocus(Qt.OtherFocusReason)

    def _load_selected_alert_for_edit(self) -> None:
        alert_id = self._selected_alert_id()
        if alert_id is None:
            return
        alert = next((item for item in self.alert_service.list_alerts() if item.id == alert_id), None)
        if alert is None:
            return
        self._editing_alert_id = alert.id
        self.alert_code_input.setText(alert.code)
        self.alert_name_input.setText(alert.name)
        index = self.alert_kind_combo.findData(alert.kind)
        self.alert_kind_combo.setCurrentIndex(index if index >= 0 else 0)
        self.alert_threshold_input.setText(f"{alert.threshold:g}")
        self.alert_note_input.setText(alert.note)
        self.alert_repeat_check.setChecked(alert.repeat)
        self.alert_save_button.setText("保存编辑")
        self._register_action("alerts.edit", "编辑预警", "alerts", "execute", self.save_alert)

    def save_alert(self) -> None:
        code = self.alert_code_input.text().strip()
        name = self.alert_name_input.text().strip() or code
        kind = str(self.alert_kind_combo.currentData() or "")
        try:
            threshold = float(self.alert_threshold_input.text().strip())
        except ValueError:
            QMessageBox.warning(self, "预警设置", "阈值必须是大于 0 的数字。")
            return
        draft = AlertDraft(code, name, kind, threshold, self.alert_note_input.text().strip(), self.alert_repeat_check.isChecked())
        actor = getpass.getuser()
        if self._editing_alert_id is None:
            result = self.alert_service.create_alert(draft, actor)
        else:
            result = self.alert_service.update_alert(self._editing_alert_id, draft, actor)
        if not result.ok:
            QMessageBox.warning(self, "预警设置", result.message)
            return
        self.alert_monitor.enforce_policy()
        self.clear_alert_form()
        self._refresh_alerts_page()
        self._set_status(result.message)

    def toggle_selected_alert(self) -> None:
        alert_id = self._selected_alert_id()
        alert = next((item for item in self.alert_service.list_alerts() if item.id == alert_id), None)
        if alert is None:
            QMessageBox.information(self, "价格预警", "请先选择一条预警。")
            return
        result = self.alert_service.set_enabled(alert.id, not alert.enabled, getpass.getuser())
        self.alert_monitor.enforce_policy()
        self._refresh_alerts_page()
        self._set_status(result.message)

    def delete_selected_alert(self) -> None:
        alert_id = self._selected_alert_id()
        if alert_id is None:
            QMessageBox.information(self, "价格预警", "请先选择一条预警。")
            return
        if QMessageBox.question(self, "确认删除", "确定删除选中的预警？历史记录会保留。") != QMessageBox.Yes:
            return
        result = self.alert_service.delete_alert(alert_id, getpass.getuser(), "用户在 Qt 确认删除")
        if not result.ok:
            QMessageBox.warning(self, "价格预警", result.message)
            return
        self.clear_alert_form()
        self.alert_monitor.enforce_policy()
        self._refresh_alerts_page()
        self._set_status("预警已删除")

    def _set_alert_monitor_enabled(self, enabled: bool) -> None:
        if not hasattr(self, "alert_monitor_check"):
            return
        try:
            policy = self.alert_monitor.update_policy(bool(enabled), self._alert_interval_seconds())
        except ValueError as exc:
            self._set_status(str(exc))
            return
        self._sync_alert_interval_combo(policy.configured_interval_seconds)
        if enabled:
            self.run_alert_check(manual=False)
        else:
            self._alert_monitor_timer.stop()
        self._refresh_alert_status()

    def _set_alert_monitor_interval(self) -> None:
        if not hasattr(self, "alert_interval_combo"):
            return
        try:
            policy = self.alert_monitor.update_policy(self.alert_monitor_check.isChecked(), self._alert_interval_seconds())
        except ValueError as exc:
            self._set_status(str(exc))
            return
        self._sync_alert_interval_combo(policy.configured_interval_seconds)
        if self.alert_monitor_check.isChecked():
            self._schedule_alert_monitor(self.alert_monitor.trading_clock.next_check_at(datetime.now(), policy.effective_interval_seconds))
        self._refresh_alert_status(policy.adjustment_reason)

    def _alert_interval_seconds(self) -> int:
        try:
            value = int(self.alert_interval_combo.currentData() or 30)
            return value if value in {15, 30, 60, 300, 600, 1800} else 30
        except (TypeError, ValueError):
            return 30

    def _sync_alert_interval_combo(self, seconds: int) -> None:
        if not hasattr(self, "alert_interval_combo"):
            return
        index = self.alert_interval_combo.findData(str(int(seconds)))
        if index >= 0 and self.alert_interval_combo.currentIndex() != index:
            self.alert_interval_combo.blockSignals(True)
            self.alert_interval_combo.setCurrentIndex(index)
            self.alert_interval_combo.blockSignals(False)

    @staticmethod
    def _alert_interval_label(seconds: int) -> str:
        return f"{seconds} 秒" if seconds < 60 else f"{seconds // 60} 分钟"

    def _schedule_alert_monitor(self, next_check_at: datetime | None) -> None:
        self._alert_monitor_timer.stop()
        if not self.alert_monitor_check.isChecked() or next_check_at is None:
            return
        delay_ms = max(1, int((next_check_at - datetime.now(next_check_at.tzinfo)).total_seconds() * 1000))
        self._alert_monitor_timer.start(delay_ms)

    def _alert_monitor_tick(self) -> None:
        if self.alert_monitor_check.isChecked():
            self.run_alert_check(manual=False)

    def run_alert_check(self, *, manual: bool) -> bool:
        if self._alert_check_in_progress:
            if manual:
                self._set_status("预警检查仍在后台运行。")
            return False
        self._alert_check_in_progress = True
        self.alert_check_button.setEnabled(False)

        def task():
            alerts = self.alert_service.list_alerts(enabled_only=True)
            codes = tuple(dict.fromkeys(str(item.code).zfill(6) for item in alerts))
            if not codes:
                return self.alert_monitor.check(manual=manual)
            self._sync_data_readiness_dependencies()
            readiness = self.data_readiness.ensure(
                DataRequirement(
                    feature="手动预警检查" if manual else "自动预警检查",
                    kinds=("quotes",),
                    codes=codes,
                    mode="formal",
                    reason="预警状态更新和通知前必须使用真实源时间行情",
                )
            )
            if readiness.status == "blocked":
                raise ValueError(readiness.message)
            return self.alert_monitor.check(
                manual=manual,
                quote_provider=self._prepared_quote_provider(codes),
            )

        def done(result) -> None:
            self._alert_check_in_progress = False
            self.alert_check_button.setEnabled(True)
            self._refresh_alerts_page()
            if result.code == "calendar_unavailable":
                summary = result.message
                self._refresh_alert_status(summary)
                if self.alert_monitor_check.isChecked():
                    self._schedule_alert_monitor(result.next_check_at)
                if manual:
                    self._set_status(summary)
                return
            failed = f"，失败 {len(result.failed_codes)} 条" if result.failed_codes else ""
            stale = f"，旧行情 {len(result.stale_codes)} 条" if result.stale_codes else ""
            age = ""
            if result.quote_times:
                latest = max(result.quote_times.values())
                age = f"，最新行情 {latest.strftime('%H:%M:%S')}（{max(0, int((result.last_check_at - latest).total_seconds()))} 秒前）"
            summary = f"免费行情快照：已检查 {result.checked_count} 条，命中 {len(result.matches)} 条，触发 {len(result.triggers)} 条{failed}{stale}{age}"
            if result.stale_codes:
                summary += "；行情时间不是当前有效快照，本次未触发。"
            if result.preview:
                summary = result.message
            self._refresh_alert_status(summary)
            if self.alert_monitor_check.isChecked():
                self._schedule_alert_monitor(result.next_check_at)
            if manual:
                self._set_status(summary)

        def failed(message: str) -> None:
            self._alert_check_in_progress = False
            self.alert_check_button.setEnabled(True)
            self._refresh_alert_status("检查失败，请查看数据状态或稍后重试")
            if self.alert_monitor_check.isChecked():
                self._schedule_alert_monitor(self.alert_monitor.trading_clock.next_check_at(datetime.now(), self.alert_monitor.policy().effective_interval_seconds))
            if manual:
                self._set_status(f"预警检查失败：{message}")

        return self._run_background("正在检查价格预警...", task, done, task_key="alert_check", failed=failed)

    def _refresh_alert_status(self, suffix: str = "") -> None:
        if not hasattr(self, "alert_status_label"):
            return
        policy = self.alert_monitor.policy()
        if policy.enabled:
            text = (
                f"免费行情快照 · 启用 {len(self.alert_service.list_alerts(enabled_only=True))} 条 / 覆盖 {policy.covered_codes} 只；"
                f"配置 {self._alert_interval_label(policy.configured_interval_seconds)}，有效 {self._alert_interval_label(policy.effective_interval_seconds)}"
            )
        else:
            text = f"免费行情快照 · 监控关闭；启用 {len(self.alert_service.list_alerts(enabled_only=True))} 条 / 覆盖 {policy.covered_codes} 只"
        details = "；".join(item for item in (policy.adjustment_reason, suffix) if item)
        self.alert_status_label.setText(text + (f" · {details}" if details else ""))

    def _refresh_alerts_page(self) -> None:
        if not hasattr(self, "alert_table"):
            return
        alerts = self.alert_service.list_alerts()
        self.alert_table.setRowCount(len(alerts))
        for row, alert in enumerate(alerts):
            values = [
                alert.id,
                alert.code,
                alert.name,
                describe_alert(alert.kind, alert.threshold),
                "启用" if alert.enabled else "停用",
                "持续" if alert.repeat else "单次",
                alert.trigger_count,
                alert.note or "-",
            ]
            for column, value in enumerate(values):
                self.alert_table.setItem(row, column, QTableWidgetItem(str(value)))
        self._refresh_alert_history()
        self._refresh_alert_status()
        self._refresh_dashboard_snapshot()

    def _history_records_in_scope(self):
        return self.alert_service.list_history(str(self.alert_history_scope.currentData() or "all"))

    def _refresh_alert_history(self) -> None:
        if not hasattr(self, "alert_history_table"):
            return
        records = self._history_records_in_scope()
        self.alert_history_table.setRowCount(len(records))
        for row, item in enumerate(records):
            values = [
                item.created_at.strftime("%Y-%m-%d %H:%M:%S"), item.code, item.name,
                describe_alert(item.kind, item.threshold),
                "-" if item.price is None else f"{item.price:g}",
                "-" if item.change_pct is None else f"{item.change_pct:+.2f}%",
                item.quote_source or "-", item.quote_at or "-", item.message,
            ]
            for column, value in enumerate(values):
                self.alert_history_table.setItem(row, column, QTableWidgetItem(str(value)))

    def export_alert_history_csv(self) -> None:
        path = self._choose_csv_path("导出预警记录", "price_alert_history.csv")
        if not path:
            return
        rows = self.alert_service.export_history_rows(str(self.alert_history_scope.currentData() or "all"))
        temporary = Path(path).with_suffix(Path(path).suffix + ".tmp")
        try:
            with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=["时间", "代码", "名称", "条件", "价格", "涨跌幅", "数据源", "行情时间", "说明"])
                writer.writeheader()
                writer.writerows(rows)
            temporary.replace(path)
        except PermissionError:
            temporary.unlink(missing_ok=True)
            QMessageBox.warning(self, "导出失败", "目标文件没有写入权限；数据库未变化。请关闭占用文件或选择其他位置后重试。")
            return
        except OSError as exc:
            temporary.unlink(missing_ok=True)
            QMessageBox.warning(self, "导出失败", f"未写入文件，数据库未变化：{exc}。请检查路径后重试。")
            return
        self._set_status(f"预警记录已导出：{path}")

    def _choose_csv_path(self, title: str, default_name: str) -> str:
        """真实、非原生文件对话框：发布测试可用 QTest 稳定操作。"""
        dialog = QFileDialog(self, title)
        dialog.setAcceptMode(QFileDialog.AcceptSave)
        dialog.setNameFilter("CSV 文件 (*.csv)")
        dialog.setDefaultSuffix("csv")
        option = getattr(QFileDialog, "DontUseNativeDialog", QFileDialog.Option.DontUseNativeDialog)
        dialog.setOption(option, True)
        dialog.selectFile(default_name)
        return dialog.selectedFiles()[0] if dialog.exec() and dialog.selectedFiles() else ""

    def clear_alert_history(self) -> None:
        if QMessageBox.question(self, "确认清空", "确定清空当前所有预警记录？") != QMessageBox.Yes:
            return
        result = self.alert_service.clear_history(getpass.getuser(), "用户在 Qt 确认清空")
        self._refresh_alert_history()
        self._refresh_dashboard_snapshot()
        self._set_status(result.message)

    # ---- 观察与复盘：所有业务状态由 ReviewWorkflowService 决定 --------

    def _build_review_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        title = QLabel("观察与复盘")
        title.setObjectName("pageTitle")
        layout.addWidget(title)
        automation_card = Card(
            "本地自动复盘",
            "软件关闭期间不会运行；重启后会自动补做遗漏。数据缺失不会生成虚假收益，自动复盘不会改变候选资格、排序或人工观察状态。",
        )
        automation_controls = QHBoxLayout()
        self.review_automation_enabled_check = QCheckBox("启用自动复盘")
        self.review_automation_enabled_check.setChecked(self.review_automation.policy().enabled)
        self.review_automation_enabled_check.toggled.connect(self._set_review_automation_enabled)
        self.review_automation_run_button = QPushButton("立即检查")
        self.review_automation_run_button.setObjectName("primaryBtn")
        self.review_automation_run_button.setProperty("test_id", "reviewAutomationRunButton")
        self._bind_action(
            self.review_automation_run_button,
            "review.automation.run",
            "立即检查自动复盘",
            "review",
            "execute",
            lambda: self._start_review_automation("manual", force_tracking=True, force_settlement=True),
        )
        self.review_automation_run_button.clicked.connect(
            lambda: self._start_review_automation("manual", force_tracking=True, force_settlement=True)
        )
        self.review_automation_status_label = QLabel("")
        self.review_automation_status_label.setObjectName("experimentBanner")
        self.review_automation_status_label.setWordWrap(True)
        automation_controls.addWidget(self.review_automation_enabled_check)
        automation_controls.addWidget(self.review_automation_run_button)
        automation_controls.addStretch(1)
        automation_card.layout.addLayout(automation_controls)
        automation_card.layout.addWidget(self.review_automation_status_label)
        layout.addWidget(automation_card)
        tabs = QTabWidget()
        tabs.setObjectName("reviewTabs")
        tabs.addTab(self._build_tracking_review_tab(), "待复盘观察")
        tabs.addTab(self._build_performance_review_tab(), "战绩统计")
        tabs.addTab(self._build_evaluation_review_tab(), "系统评价")
        tabs.addTab(self._build_exploratory_review_tab(), "探索待审")
        tabs.tabBar().setUsesScrollButtons(False)
        for button in tabs.findChildren(QToolButton):
            self._bind_action(button, "review.tabs.scroll", "切换复盘页签", "review", "info", lambda: None)
        layout.addWidget(tabs)
        self._refresh_review_page()
        return page

    def _build_tracking_review_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        controls = QHBoxLayout()
        self.review_tracking_status = self._combo([("全部状态", ""), ("进行中", "open"), ("已结束", "closed")])
        self.review_tracking_status.currentIndexChanged.connect(self._refresh_tracking_review)
        review_button = QPushButton("客观复核")
        review_button.setObjectName("primaryBtn")
        review_button.setProperty("test_id", "reviewTrackingButton")
        self._bind_action(review_button, "review.tracking.review", "客观复核", "review", "execute", self.review_selected_tracking_task)
        review_button.clicked.connect(self.review_selected_tracking_task)
        delete_button = QPushButton("删除")
        delete_button.setObjectName("deleteTrackingButton")
        self._bind_action(delete_button, "review.tracking.delete", "删除观察任务", "review", "execute", self.delete_selected_tracking_task, risk_level="medium")
        delete_button.clicked.connect(self.delete_selected_tracking_task)
        export_button = QPushButton("导出CSV")
        export_button.setObjectName("exportTrackingButton")
        self._bind_action(export_button, "review.tracking.export", "导出观察任务", "review", "execute", self.export_tracking_review_csv)
        export_button.clicked.connect(self.export_tracking_review_csv)
        controls.addWidget(self.review_tracking_status)
        controls.addWidget(review_button)
        controls.addWidget(delete_button)
        controls.addWidget(export_button)
        controls.addStretch(1)
        layout.addLayout(controls)
        self.review_tracking_table = build_table(["ID", "代码", "名称", "类型", "状态", "结论", "到期日", "说明"])
        self.review_tracking_table.setWordWrap(True)
        self.review_tracking_table.setTextElideMode(Qt.ElideNone)
        layout.addWidget(self.review_tracking_table)
        self.review_tracking_hint = QLabel("选择任务后可用真实行情进行客观复核；行情缺失不会改动状态。")
        self.review_tracking_hint.setObjectName("pageSubtitle")
        self.review_tracking_hint.setWordWrap(True)
        layout.addWidget(self.review_tracking_hint)
        return page

    def _build_performance_review_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        controls = QHBoxLayout()
        self.performance_start_input = QLineEdit()
        self.performance_start_input.setPlaceholderText("起始日期 YYYY-MM-DD")
        self.performance_end_input = QLineEdit()
        self.performance_end_input.setPlaceholderText("结束日期 YYYY-MM-DD")
        self.performance_code_input = QLineEdit()
        self.performance_code_input.setPlaceholderText("股票代码/名称")
        self.performance_industry_input = QLineEdit()
        self.performance_industry_input.setPlaceholderText("行业")
        self.performance_strategy_id_input = QLineEdit()
        self.performance_strategy_id_input.setPlaceholderText("策略 ID")
        self.performance_strategy_name_input = QLineEdit()
        self.performance_strategy_name_input.setPlaceholderText("策略名称")
        self.performance_strategy_version_input = QLineEdit()
        self.performance_strategy_version_input.setPlaceholderText("策略版本")
        self.performance_horizon_combo = self._combo([("全部周期", ""), ("3日", "3"), ("5日", "5"), ("10日", "10"), ("20日", "20")])
        self.performance_regime_combo = self._combo([("全部市场状态", ""), ("上涨", "bull"), ("震荡", "range"), ("下跌", "bear"), ("不确定", "uncertain")])
        self.performance_template_input = QLineEdit()
        self.performance_template_input.setPlaceholderText("模板")
        self.performance_weight_input = QLineEdit()
        self.performance_weight_input.setPlaceholderText("权重版本")
        self.performance_quality_combo = self._combo([("全部数据质量", ""), ("完整", "complete"), ("缺价格", "missing_price"), ("缺基准", "missing_benchmark"), ("缺行业", "missing_industry")])
        self.performance_candidate_combo = self._combo([("全部候选类型", ""), ("候选", "candidate"), ("观察", "watch"), ("排除", "excluded")])
        self.performance_mode_combo = self._combo([("全部评价模式", ""), *EVALUATION_MODE_OPTIONS])
        for widget in (
            self.performance_start_input, self.performance_end_input, self.performance_code_input,
            self.performance_industry_input, self.performance_strategy_id_input, self.performance_strategy_name_input,
            self.performance_strategy_version_input, self.performance_horizon_combo, self.performance_regime_combo,
            self.performance_template_input, self.performance_weight_input, self.performance_quality_combo,
            self.performance_candidate_combo, self.performance_mode_combo,
        ):
            controls.addWidget(widget)
        settle = QPushButton("结算到期样本")
        settle.setObjectName("primaryBtn")
        settle.setProperty("test_id", "settleSamplesButton")
        self._bind_action(settle, "review.performance.settle", "结算到期样本", "review", "execute", self.settle_review_samples)
        settle.clicked.connect(self.settle_review_samples)
        refresh = QPushButton("刷新统计")
        self._bind_action(refresh, "review.performance.refresh", "刷新统计", "review", "execute", self._refresh_performance_review)
        refresh.clicked.connect(self._refresh_performance_review)
        export = QPushButton("导出CSV")
        export.setObjectName("exportPerformanceButton")
        self._bind_action(export, "review.performance.export", "导出表现复盘", "review", "execute", self.export_performance_review_csv)
        export.clicked.connect(self.export_performance_review_csv)
        controls.addWidget(settle)
        controls.addWidget(refresh)
        controls.addWidget(export)
        controls.addStretch(1)
        layout.addLayout(controls)
        self.performance_summary_label = QLabel("")
        self.performance_summary_label.setObjectName("experimentBanner")
        self.performance_summary_label.setWordWrap(True)
        layout.addWidget(self.performance_summary_label)
        self.performance_table = build_table(["模式", "代码", "名称", "周期", "样本日", "收益", "HS300超额", "评级", "质量"])
        layout.addWidget(self.performance_table)
        return page

    def _build_evaluation_review_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        controls = QHBoxLayout()
        self.evaluation_mode_combo = self._combo(list(EVALUATION_MODE_OPTIONS))
        self.evaluation_mode_combo.currentIndexChanged.connect(self._refresh_evaluation_review)
        refresh = QPushButton("刷新系统评价")
        refresh.setObjectName("primaryBtn")
        self._bind_action(refresh, "review.evaluation.refresh", "刷新系统评价", "review", "execute", self._refresh_evaluation_review)
        refresh.clicked.connect(self._refresh_evaluation_review)
        controls.addWidget(QLabel("评价模式"))
        controls.addWidget(self.evaluation_mode_combo)
        controls.addWidget(refresh)
        controls.addStretch(1)
        layout.addLayout(controls)
        self.evaluation_data_quality_label = QLabel("")
        self.evaluation_data_quality_label.setObjectName("experimentBanner")
        self.evaluation_data_quality_label.setWordWrap(True)
        layout.addWidget(self.evaluation_data_quality_label)
        self.evaluation_agent_label = QLabel("")
        self.evaluation_agent_label.setObjectName("experimentBanner")
        self.evaluation_agent_label.setWordWrap(True)
        layout.addWidget(self.evaluation_agent_label)
        self.evaluation_table = build_table(
            ["模式", "Lens", "版本", "周期", "样本", "有效", "覆盖", "平均收益", "HS300超额", "行业超额", "最差回撤", "样本外"]
        )
        layout.addWidget(self.evaluation_table)
        self.evaluation_hint = QLabel(
            "正式表现只统计 formal；review_only、exploratory、legacy 独立展示。缺数、过期、未结算和人工关闭样本保留但不进入有效表现；评价不读取 AI 置信度。"
        )
        self.evaluation_hint.setObjectName("pageSubtitle")
        self.evaluation_hint.setWordWrap(True)
        layout.addWidget(self.evaluation_hint)
        return page

    def _build_exploratory_review_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        hint = QLabel(
            "探索 Agent 只能提交建议；这里必须由 human:<id> / user:<id> 使用原始不可变快照重新通过 CandidateLens。通过后仍不会自动进入 watch。"
        )
        hint.setObjectName("pageSubtitle")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        controls = QHBoxLayout()
        refresh = QPushButton("刷新探索待审")
        self._bind_action(refresh, "review.exploratory.refresh", "刷新探索待审", "review", "execute", self.refresh_exploratory_proposals)
        refresh.clicked.connect(self.refresh_exploratory_proposals)
        formalize = QPushButton("正式复核")
        formalize.setObjectName("primaryBtn")
        self._bind_action(
            formalize,
            "review.exploratory.formalize",
            "正式复核探索建议",
            "review",
            "execute",
            self.formalize_selected_exploratory,
            preconditions=("选中 pending_human_review 建议", "原始不可变快照可用", "人工操作者"),
        )
        formalize.clicked.connect(self.formalize_selected_exploratory)
        reject = QPushButton("拒绝")
        self._bind_action(reject, "review.exploratory.reject", "拒绝探索建议", "review", "execute", self.reject_selected_exploratory)
        reject.clicked.connect(self.reject_selected_exploratory)
        controls.addWidget(refresh)
        controls.addWidget(formalize)
        controls.addWidget(reject)
        controls.addStretch(1)
        layout.addLayout(controls)
        self.exploratory_proposals_table = build_table(
            ["建议 ID", "代码", "提议 Lens", "来源运行", "快照", "事实引用", "理由", "缺失证据", "状态", "操作者", "审计时间"]
        )
        layout.addWidget(self.exploratory_proposals_table)
        self.exploratory_proposal_status_label = QLabel("")
        self.exploratory_proposal_status_label.setWordWrap(True)
        self.exploratory_proposal_status_label.setObjectName("experimentBanner")
        layout.addWidget(self.exploratory_proposal_status_label)
        self.refresh_exploratory_proposals()
        return page

    def refresh_exploratory_proposals(self) -> WorkflowResult:
        if not hasattr(self, "exploratory_proposals_table"):
            return WorkflowResult("skipped", "探索待审表尚未初始化。")
        if self.exploratory_proposal_store is None:
            message = self.exploratory_proposal_error or "探索建议存储不可用"
            self.exploratory_proposal_status_label.setText(message)
            return WorkflowResult("failed", message)
        records = self.exploratory_proposal_store.list(status="", limit=200)
        self._exploratory_proposal_records = records
        self.exploratory_proposals_table.setRowCount(len(records))
        for row_index, record in enumerate(records):
            values = (
                record.proposal_id,
                record.proposal.code,
                record.proposal.proposed_lens_id,
                record.proposal.source_run_id,
                record.snapshot_id,
                "、".join(record.proposal.evidence_refs),
                record.proposal.proposal_reason,
                "、".join(record.proposal.missing_evidence) or "-",
                record.status,
                record.operator or "-",
                record.audited_at or "-",
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                if column == 0:
                    item.setData(Qt.UserRole, record.proposal_id)
                self.exploratory_proposals_table.setItem(row_index, column, item)
        self.exploratory_proposal_status_label.setText(
            f"共 {len(records)} 条探索建议；只有 pending_human_review 可正式复核。"
        )
        return WorkflowResult("success", f"已加载 {len(records)} 条探索建议。")

    def _selected_exploratory_proposal_id(self) -> str | None:
        if not hasattr(self, "exploratory_proposals_table"):
            return None
        row = self.exploratory_proposals_table.currentRow()
        item = self.exploratory_proposals_table.item(row, 0) if row >= 0 else None
        return str(item.data(Qt.UserRole) or item.text()) if item is not None else None

    def reject_selected_exploratory(self) -> WorkflowResult:
        proposal_id = self._selected_exploratory_proposal_id()
        if not proposal_id or self.exploratory_proposal_store is None:
            result = WorkflowResult("skipped", "请先选择一条探索待审建议。")
            self._set_status(result.message)
            return result
        reason, ok = QInputDialog.getText(self, "拒绝探索建议", "请填写拒绝理由：")
        if not ok or not reason.strip():
            return WorkflowResult("skipped", "已取消拒绝；必须填写理由。")
        try:
            record = self.exploratory_proposal_store.reject(proposal_id, f"human:{getpass.getuser()}", reason.strip())
        except Exception as exc:
            result = WorkflowResult("failed", f"拒绝探索建议失败：{exc}")
            self._set_status(result.message)
            return result
        self.refresh_exploratory_proposals()
        result = WorkflowResult("success", f"探索建议 {record.proposal_id} 已拒绝。")
        self._set_status(result.message)
        return result

    def formalize_selected_exploratory(self) -> bool:
        proposal_id = self._selected_exploratory_proposal_id()
        if not proposal_id or self.exploratory_proposal_store is None or self.factor_store is None:
            self._set_status("请先选择探索建议，并确认 V1 因子库可用。")
            return False
        actor = f"human:{getpass.getuser()}"

        def task():
            record = self.exploratory_proposal_store.get(proposal_id)
            if record is None:
                raise ValueError("探索建议不存在")
            snapshot_store = DataSnapshotStore(self.factor_store.database)
            manifest = snapshot_store.get_manifest(record.snapshot_id)
            if manifest is None:
                raise ValueError("原始不可变快照不存在，不能正式复核")
            snapshots = [
                item for item in self.factor_store.list_snapshots_for_manifest(record.snapshot_id)
                if str(item.code).zfill(6) == record.proposal.code.zfill(6)
            ]
            if not snapshots:
                raise ValueError("原始不可变快照中没有该股票")
            inputs = V1FactorStore(self.factor_store.database).list_selection_inputs_for_manifest(record.snapshot_id)
            selection_input = inputs.get(record.proposal.code.zfill(6))
            fact_ids = [fact.fact_id for fact in snapshot_store.list_facts(record.snapshot_id)]
            audited, selection_result = self.exploratory_proposal_store.formalize(
                proposal_id,
                actor=actor,
                snapshot_id=record.snapshot_id,
                item=selection_input or snapshots[0],
                fact_ids=fact_ids,
                availability_cutoff=manifest.availability_cutoff,
            )
            if selection_result.status != "candidate":
                return {"record": audited, "selection": selection_result, "workflow": None}
            try:
                workflow = ScreeningWorkflowService(self.strategy_store, self.performance_store).run_snapshot_screening(
                    snapshots,
                    QuerySpec(execution_mode="formal"),
                    source_label="Qt探索建议正式复核",
                    user_text=audited.proposal.proposal_reason,
                    data_source=manifest.data_source,
                    execution_mode="formal",
                    data_manifest=manifest,
                    require_data_manifest=True,
                    candidate_lens_mode=True,
                    lens_ids=(record.proposal.proposed_lens_id,),
                    ranking_formula_id="candidate_lens.v1",
                    ranking_formula_version="candidate_lens.v1",
                    selection_policy_version="v1",
                    refresh_event_id=snapshot_store.refresh_event_id_for_snapshot(record.snapshot_id),
                    universe_version=manifest.universe_version,
                    members_digest=stable_hash([item.code for item in snapshots]),
                    selection_scope="exploratory_proposal_formalize",
                    selected_limit=1,
                )
            except Exception as exc:
                failed_record = self.exploratory_proposal_store.mark_formalization_failure(
                    proposal_id,
                    actor,
                    f"正式 CandidateLens 运行失败：{exc}",
                )
                return {"record": failed_record, "selection": selection_result, "workflow": None, "error": str(exc)}
            if workflow.report.status != "verified" or not workflow.report.results:
                failed_record = self.exploratory_proposal_store.mark_formalization_failure(
                    proposal_id,
                    actor,
                    "正式 CandidateLens 质量闸门未通过或未产生候选："
                    + ("；".join(workflow.report.errors[:3]) or "candidate_set 为空"),
                )
                return {"record": failed_record, "selection": selection_result, "workflow": None}
            if (
                workflow.report.data_snapshot_id != record.snapshot_id
                or not workflow.report.candidate_set_digest
                or any(
                    record.proposal.proposed_lens_id not in tuple(candidate.lens_ids or ())
                    for candidate in workflow.report.results
                )
            ):
                failed_record = self.exploratory_proposal_store.mark_formalization_failure(
                    proposal_id,
                    actor,
                    "正式复核结果未绑定原始快照、候选摘要或提议 Lens。",
                )
                return {"record": failed_record, "selection": selection_result, "workflow": None}
            formal_rows = [
                self._screen_candidate_row(index, candidate, report=workflow.report)
                for index, candidate in enumerate(workflow.report.results, start=1)
            ]
            for row in formal_rows:
                row["research_mode"] = "formal"
                row["research_mode_label"] = SCREENING_MODE_LABELS["formal"]
            agent_payload = self._run_screening_agents_for_rows(
                formal_rows,
                manifest,
                execution_mode="formal",
                ai_config=self._current_ai_config(),
            ) if formal_rows else ([], [], [], [])
            try:
                self.exploratory_proposal_store.attach_formal_run(proposal_id, str(workflow.run_id))
            except Exception as exc:
                failed_record = self.exploratory_proposal_store.mark_formalization_failure(
                    proposal_id,
                    actor,
                    f"正式运行已完成但 formal_run_id 持久化失败：{exc}",
                )
                return {"record": failed_record, "selection": selection_result, "workflow": None, "error": str(exc)}
            return {
                "record": self.exploratory_proposal_store.get(proposal_id),
                "selection": selection_result,
                "workflow": workflow,
                "rows": formal_rows,
                "agents": agent_payload,
            }

        def done(payload) -> None:
            workflow = payload.get("workflow")
            if workflow is None:
                self.refresh_exploratory_proposals()
                self._set_status("探索建议正式复核未通过，未进入正式候选。")
                return
            self.current_screen_report = workflow.report
            self.current_screener_run_id = workflow.run_id
            self.current_screen_rows = list(payload.get("rows") or ())
            results, aggregations, packages, warnings = payload.get("agents") or ([], [], [], [])
            self.current_agent_orchestrations = list(results)
            self.current_agent_orchestration = results[0] if results else None
            self.current_evidence_aggregations = list(aggregations)
            self.current_evidence_aggregation = aggregations[0] if aggregations else None
            views = _agent_evidence_views(results, aggregations, research_mode="formal")
            for row in self.current_screen_rows:
                row.update(views.get(_candidate_code(row.get("code")), {}))
            self._fill_screener_results(self.current_screen_rows)
            self._refresh_candidate_states(snapshot_id=workflow.report.data_snapshot_id)
            self.refresh_exploratory_proposals()
            self._set_status("探索建议已通过 CandidateLens 正式复核并生成正式候选；仍需人工单独加入观察。")

        return self._run_background(
            "正在使用原始不可变快照正式复核探索建议并整理五 Agent...",
            task,
            done,
            task_key="exploratory_formalize",
            duplicate_text="探索建议正式复核仍在后台运行。",
        )

    def _selected_table_id(self, table) -> int | None:
        item = table.item(table.currentRow(), 0) if table.currentRow() >= 0 else None
        try:
            return int(item.text()) if item is not None else None
        except ValueError:
            return None

    def _refresh_review_page(self) -> None:
        self._refresh_review_automation_status()
        if hasattr(self, "review_tracking_table"):
            self._refresh_tracking_review()
        if hasattr(self, "performance_table"):
            self._refresh_performance_review()
        if hasattr(self, "evaluation_table"):
            self._refresh_evaluation_review()

    def _refresh_review_automation_status(self, report: AutomationRunReport | None = None) -> None:
        if not hasattr(self, "review_automation_status_label"):
            return
        latest = self.cache.latest_review_automation_run()
        current_report = report or self._last_review_automation_report
        enabled = self.review_automation.policy().enabled
        state = "已启用" if enabled else "已暂停"
        last_text = "暂无运行记录"
        result_text = "-"
        effective_date = "-"
        counters = "待复核观察：-；待结算样本：-；有效结算：-；数据无效：-；可重试：-"
        if latest is not None:
            stamp = latest.finished_at or latest.started_at
            last_text = stamp.replace("T", " ")[:19]
            result_text = {"success": "成功", "partial": "部分成功", "failed": "失败", "skipped": "已暂停", "interrupted": "中断"}.get(latest.status, latest.status)
            effective_date = latest.business_date or "-"
            open_count = len(self.review_service.list_tracking_tasks({"status": "open", "include_closed": False}))
            due_count = "-"
            if self.performance_store is not None and effective_date != "-":
                try:
                    due_count = str(len(self.performance_store.list_due_samples(asof_date=effective_date, limit=100000)))
                except Exception:
                    due_count = "-"
            counters = (
                f"待复核观察：{open_count}；待结算样本：{due_count}；有效结算：{latest.valid_settled}；"
                f"数据无效：{latest.invalid_settled}；可重试：{latest.retryable_count}"
            )
        next_tracking = current_report.next_tracking_at if current_report else None
        next_settlement = current_report.next_settlement_at if current_report else None
        next_text = (
            f"下次观察检查：{next_tracking.strftime('%Y-%m-%d %H:%M') if next_tracking else '-'}；"
            f"下次样本结算：{next_settlement.strftime('%Y-%m-%d %H:%M') if next_settlement else '-'}"
        )
        self.review_automation_status_label.setText(
            f"自动复盘：{state}\n上次运行：{last_text}；上次结果：{result_text}；有效业务日期：{effective_date}\n"
            f"{counters}\n{next_text}\n"
            "仅用于研究和复盘辅助，不产生买入/卖出建议；自动复盘不会改变候选资格、排序或人工观察状态。"
        )

    def _set_review_automation_enabled(self, enabled: bool) -> None:
        self.cache.set_setting("review_automation_enabled", "1" if enabled else "0")
        if not enabled:
            self._review_automation_timer.stop()
            self._set_status("自动复盘已暂停；运行记录、观察任务和 outcome 均保留。")
        else:
            self._schedule_review_automation(datetime.now())
            self._set_status("自动复盘已启用；将在下一次调度或启动补偿时运行。")
        self._refresh_review_automation_status()

    def _schedule_review_automation(self, next_at: datetime | None = None) -> None:
        if not self.review_automation.policy().enabled or self._window_closing:
            self._review_automation_timer.stop()
            return
        current = as_shanghai(datetime.now())
        target = as_shanghai(next_at) if next_at else current + timedelta(minutes=self.review_automation.policy().tracking_interval_minutes)
        delay_ms = max(1000, int((target - current).total_seconds() * 1000))
        self._review_automation_timer.start(delay_ms)

    def _review_automation_tick(self) -> None:
        self._start_review_automation("timer")

    def _start_review_automation(
        self,
        trigger: str,
        *,
        force_tracking: bool = False,
        force_settlement: bool = False,
    ) -> bool:
        if trigger != "manual" and not self.review_automation.policy().enabled:
            return False
        if "data_refresh" in self._running_tasks:
            self._review_automation_pending_after_refresh = True
            self._set_status("数据刷新正在运行，完成后将自动执行复盘。")
            return False
        source = StoreBackedPriceSource(pit_database=self.pit_database, fetcher=self.fetcher)

        def task() -> AutomationRunReport:
            open_tasks = self.review_service.list_tracking_tasks({"status": "open", "include_closed": False})
            quote_provider: Any = self.fetcher
            if open_tasks:
                self._sync_data_readiness_dependencies()
                codes = tuple(dict.fromkeys(str(item.code).zfill(6) for item in open_tasks))
                readiness = self.data_readiness.ensure(
                    DataRequirement(
                        feature="自动复盘观察检查",
                        kinds=("quotes",),
                        codes=codes,
                        mode="formal",
                        reason="自动复盘改变观察任务状态前必须准备真实源时间行情",
                    )
                )
                if readiness.status == "blocked":
                    raise ValueError(readiness.message)
                quote_provider = self._prepared_quote_provider(codes)
            current = as_shanghai(datetime.now())
            try:
                has_due_samples = bool(
                    self.performance_store
                    and self.performance_store.list_due_samples(
                        asof_date=current.date().isoformat(), limit=1
                    )
                )
            except Exception:
                has_due_samples = False
            # PIT is intentionally a daily incremental dependency, not a
            # startup side effect.  It is prepared for an explicit settlement
            # or only after close when there is actually a sample to settle.
            should_prepare_pit = force_settlement or (
                has_due_samples and trigger in {"startup", "timer"} and current.time() >= time(15, 5)
            )
            if should_prepare_pit:
                pit_readiness = self.data_readiness.ensure(
                    DataRequirement(
                        feature="复盘结算 PIT 日增量",
                        kinds=("pit_incremental",),
                        mode="formal",
                        reason="复盘结算前必须确认 PIT 日增量已在有效期内",
                    )
                )
                if pit_readiness.status == "blocked":
                    raise ValueError(pit_readiness.message)
            return self.review_automation.run_once(
                trigger=trigger, quote_provider=quote_provider, price_source=source,
                actor=getpass.getuser(), force_tracking=force_tracking, force_settlement=force_settlement,
            )

        def done(report: AutomationRunReport) -> None:
            self._last_review_automation_report = report
            self._refresh_review_page()
            self._refresh_dashboard_snapshot()
            if report.status == "already_running":
                self._set_status("自动复盘正在运行，未创建第二个任务。")
            elif report.status == "failed":
                self._set_status(f"自动复盘失败：{report.error_summary or '请查看运行记录。'}")
            else:
                self._set_status(
                    f"自动复盘{report.status}：复核 {report.tracking_reviewed}/{report.tracking_scanned} 条观察，"
                    f"结算有效 {report.valid_settled} 条、数据无效 {report.invalid_settled} 条、可重试 {report.retryable_count} 条。"
                )
            self._schedule_review_automation(report.next_run_at)

        return self._run_background(
            "正在执行本地自动复盘...", task, done, task_key="review_automation",
            duplicate_text="自动复盘正在运行。", failed=lambda message: self._set_status(f"自动复盘失败：{message}"),
        )

    def _refresh_tracking_review(self) -> None:
        if not hasattr(self, "review_tracking_table"):
            return
        status = str(self.review_tracking_status.currentData() or "")
        tasks = self.review_service.list_tracking_tasks({"status": status, "include_closed": True})
        self.review_tracking_table.setRowCount(len(tasks))
        for row, task in enumerate(tasks):
            values = [task.id, task.code, task.name, "持仓" if task.task_type == "holding" else "观察", "进行中" if task.status == "open" else "已结束", task.result or "-", task.due_date, task.last_review or "-"]
            for col, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                if col == len(values) - 1 and value not in ("", "-"):
                    item.setToolTip(str(value))
                self.review_tracking_table.setItem(row, col, item)
        self.review_tracking_table.resizeRowsToContents()
        self.review_tracking_hint.setText(
            "暂无观察任务；可先在个股研究页生成观察并保存。"
            if not tasks
            else (
                f"当前 {len(tasks)} 条任务。复核规则：失效价是下限，当前价≤失效价→观察依据失效；"
                "当前价≥目标价→达到目标；都未触发且到期→到期未触发。"
                "复核还会检查真实行情时间和数据质量；数据缺失或快照过旧不会擅自改变状态。"
            )
        )

    def review_selected_tracking_task(self) -> None:
        task_id = self._selected_table_id(self.review_tracking_table)
        if task_id is None:
            QMessageBox.information(self, "待复盘观察", "请先选择一条观察任务。")
            return
        task = next((item for item in self.review_service.list_tracking_tasks({"include_closed": True}) if item.id == task_id), None)
        if task is None:
            self._refresh_tracking_review()
            return
        def task_fn():
            self._sync_data_readiness_dependencies()
            readiness = self.data_readiness.ensure(
                DataRequirement(
                    feature=f"{task.name} 观察复核",
                    kinds=("quotes",),
                    codes=(task.code,),
                    mode="formal",
                    reason="复核观察任务前必须使用真实源时间的最新行情",
                )
            )
            if readiness.status == "blocked":
                raise ValueError(readiness.message)
            quote = self._quote_from_feature_readiness_cache(
                StockQuote(code=task.code, name=task.name)
            )
            return self.review_service.review_tracking_task(task.id, quote, getpass.getuser())
        def done(result):
            self._refresh_tracking_review()
            self._set_status(result.message)
        self._run_background(
            "正在复核观察任务...",
            task_fn,
            done,
            task_key="tracking_review",
            duplicate_text="复核任务仍在运行。",
            failed=lambda message: self._set_status(f"观察任务复核已阻断：{message}"),
        )

    def delete_selected_tracking_task(self) -> None:
        task_id = self._selected_table_id(self.review_tracking_table)
        if task_id is None:
            QMessageBox.information(self, "待复盘观察", "请先选择一条观察任务。")
            return
        if QMessageBox.question(self, "确认删除", "确定删除选中的观察任务？") != QMessageBox.Yes:
            return
        result = self.review_service.delete_tracking_task(task_id, getpass.getuser(), "用户在 Qt 确认删除")
        self._refresh_tracking_review()
        self._set_status(result.message)

    def export_tracking_review_csv(self) -> None:
        path = self._choose_csv_path("导出观察任务", "tracking_tasks.csv")
        if not path:
            return
        try:
            self.review_service.write_csv(Path(path), self.review_service.export_tracking_rows({"status": str(self.review_tracking_status.currentData() or "")}))
        except PermissionError:
            QMessageBox.warning(self, "导出失败", "目标文件没有写入权限；数据未变化。请关闭占用文件后重试。")
            return
        except OSError as exc:
            QMessageBox.warning(self, "导出失败", f"未写入文件，数据未变化：{exc}")
            return
        self._set_status(f"观察任务已导出：{path}")

    def _refresh_performance_review(self) -> None:
        if not hasattr(self, "performance_table"):
            return
        try:
            snapshot = self.review_service.dashboard_snapshot()
            rows = self.review_service.performance_details(self._performance_filter_args(limit=100))
        except RuntimeError as exc:
            self.performance_summary_label.setText(str(exc))
            return
        self.performance_summary_label.setText(
            f"到期待结算 {snapshot.due_count} 条；样本 {snapshot.sample_count} 条，已结算 {snapshot.settled_count} 条；"
            f"有效 {snapshot.valid_settled_count} 条，数据无效 {snapshot.invalid_settled_count} 条；下一步：{snapshot.next_action}"
        )
        self.performance_table.setRowCount(len(rows))
        for row, item in enumerate(rows):
            sample_class = str(item.get("sample_class") or "legacy")
            mode = SAMPLE_CLASS_TO_MODE.get(sample_class, sample_class)
            values = [mode, item.get("code", ""), item.get("name", ""), item.get("horizon_days", ""), item.get("sample_date", ""), item.get("return_pct", "-"), item.get("excess_vs_hs300_pct", "-"), item.get("rating", "-"), item.get("data_quality", "pending")]
            for col, value in enumerate(values):
                self.performance_table.setItem(row, col, QTableWidgetItem(str(value)))

    def settle_review_samples(self) -> None:
        self._start_review_automation("manual", force_settlement=True)

    def export_performance_review_csv(self) -> None:
        path = self._choose_csv_path("导出表现复盘", "performance_review.csv")
        if not path:
            return
        try:
            self.review_service.write_csv(Path(path), self.review_service.export_performance_rows(self._performance_filter_args(limit=500)))
        except PermissionError:
            QMessageBox.warning(self, "导出失败", "目标文件没有写入权限；数据未变化。请关闭占用文件后重试。")
            return
        except OSError as exc:
            QMessageBox.warning(self, "导出失败", f"未写入文件，数据未变化：{exc}")
            return
        self._set_status(f"表现复盘已导出：{path}")

    def _performance_filter_args(self, *, limit: int) -> dict[str, Any]:
        code_or_name = self.performance_code_input.text().strip()
        filters: dict[str, Any] = {
            "start_date": self.performance_start_input.text().strip(),
            "end_date": self.performance_end_input.text().strip(),
            "industry": self.performance_industry_input.text().strip(),
            "strategy_name": self.performance_strategy_name_input.text().strip(),
            "strategy_version": self.performance_strategy_version_input.text().strip(),
            "horizon_days": self.performance_horizon_combo.currentData(),
            "regime": self.performance_regime_combo.currentData(),
            "template_id": self.performance_template_input.text().strip(),
            "weight_policy_version": self.performance_weight_input.text().strip(),
            "data_quality": self.performance_quality_combo.currentData(),
            "candidate_type": self.performance_candidate_combo.currentData(),
            "evaluation_mode": self.performance_mode_combo.currentData(),
            "limit": limit,
        }
        if code_or_name:
            filters["code" if code_or_name.isdigit() else "stock_name"] = code_or_name
        strategy_id = self.performance_strategy_id_input.text().strip()
        if strategy_id:
            if strategy_id.isdigit():
                filters["strategy_id"] = int(strategy_id)
            else:
                self._set_status("策略 ID 必须是数字；已忽略此过滤条件。")
        return {key: value for key, value in filters.items() if value not in (None, "")}

    @staticmethod
    def _evaluation_percent(value: Any) -> str:
        if value is None:
            return "-"
        try:
            return f"{float(value):.3f}%"
        except (TypeError, ValueError):
            return "-"

    @staticmethod
    def _evaluation_number(value: Any) -> str:
        if value is None:
            return "-"
        try:
            return f"{float(value):.2f}"
        except (TypeError, ValueError):
            return "-"

    @staticmethod
    def _evaluation_rate(value: Any) -> str:
        if value is None:
            return "-"
        try:
            return f"{float(value) * 100:.1f}%"
        except (TypeError, ValueError):
            return "-"

    def _refresh_evaluation_review(self) -> None:
        if not hasattr(self, "evaluation_table"):
            return
        mode = str(self.evaluation_mode_combo.currentData() or "formal")
        if self.evaluation_error_message or self.lens_evaluation_store is None:
            message = self.evaluation_error_message or "评价存储尚未可用。"
            self.evaluation_data_quality_label.setText(message)
            self.evaluation_agent_label.setText(message)
            self.evaluation_table.setRowCount(0)
            return
        try:
            data_quality = self.data_quality_evaluation_store.evaluate() if self.data_quality_evaluation_store else None
            agent = self.agent_evaluation_store.summarize() if self.agent_evaluation_store else {}
            rows = self.lens_evaluation_store.evaluate(mode=mode)
        except Exception as exc:
            self.evaluation_data_quality_label.setText(f"系统评价读取失败：{exc}")
            self.evaluation_agent_label.setText("未生成评价结果；原始观察和执行记录未修改。")
            self.evaluation_table.setRowCount(0)
            return
        if data_quality is None:
            self.evaluation_data_quality_label.setText("数据质量：暂无快照评价。")
        else:
            metrics = data_quality.metrics
            notes = "；".join(data_quality.notes) if data_quality.notes else "无"
            self.evaluation_data_quality_label.setText(
                "数据质量："
                f"快照 {data_quality.snapshot_count}，刷新事件 {data_quality.refresh_event_count}；"
                f"最新快照年龄 {self._evaluation_number(metrics.get('latest_snapshot_age_hours'))} 小时；"
                f"字段缺失率 {self._evaluation_rate(metrics.get('field_missing_rate'))}；"
                f"源稳定率 {self._evaluation_rate(metrics.get('source_stability_rate'))}；"
                f"快照可复现记录率 {self._evaluation_rate(metrics.get('snapshot_reproducibility_rate'))}；"
                f"缓存/备份使用率 {self._evaluation_rate(metrics.get('cache_usage_rate'))}；"
                f"failover 使用率 {self._evaluation_rate(metrics.get('failover_usage_rate'))}；"
                f"修订率 {self._evaluation_rate(metrics.get('revision_rate'))}。备注：{notes}"
            )
        self.evaluation_agent_label.setText(
            "Agent 过程（正式执行记录）："
            f"执行 {agent.get('execution_count', 0)}，成功率 {self._evaluation_rate(agent.get('success_rate'))}，"
            f"超时率 {self._evaluation_rate(agent.get('timeout_rate'))}，JSON 合规率 {self._evaluation_rate(agent.get('json_compliance_rate'))}，"
            f"证据引用有效率 {self._evaluation_rate(agent.get('evidence_reference_valid_rate'))}，"
            f"不支持声明率 {self._evaluation_rate(agent.get('unsupported_claim_rate'))}，"
            f"平均延迟 {agent.get('average_latency_ms') or '-'} ms，冲突 {agent.get('conflict_count', 0)}，"
            f"费用记录 {agent.get('total_cost_usd') if agent.get('total_cost_usd') is not None else '-'} USD。"
        )
        self.evaluation_table.setRowCount(len(rows))
        for row_index, item in enumerate(rows):
            oos = item.get("out_of_sample") or {}
            values = [
                EVALUATION_MODE_LABELS.get(mode, mode),
                item.get("lens_id", ""),
                ", ".join(item.get("lens_versions") or []) or "-",
                item.get("horizon_days", ""),
                item.get("sample_count", 0),
                item.get("valid_settled_count", 0),
                self._evaluation_rate(item.get("valid_sample_coverage_rate")),
                self._evaluation_percent(item.get("avg_return_pct")),
                self._evaluation_percent(item.get("avg_excess_vs_hs300_pct")),
                self._evaluation_percent(item.get("avg_excess_vs_industry_pct")),
                self._evaluation_percent(item.get("worst_max_drawdown_pct")),
                "可用" if oos.get("available") else "样本不足",
            ]
            for column, value in enumerate(values):
                self.evaluation_table.setItem(row_index, column, QTableWidgetItem(str(value)))
        if not rows:
            self.evaluation_hint.setText(
                f"{EVALUATION_MODE_LABELS.get(mode, mode)} 暂无带 Lens ID 的观察样本；不会用总分或 AI 置信度替代 Lens 评价。"
            )

    def _build_data_status_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        title = QLabel(NAVIGATION_LABELS["data"])
        title.setObjectName("pageTitle")
        layout.addWidget(title)

        action_card = Card(
            "刷新中心",
            "自动更新只轮询自选/当前个股行情；进入研究功能时再更新财务、K线、资金流和事件。"
            "全A快照与 PIT 不会在后台无提示长跑。",
        )
        action_row = QHBoxLayout()
        self.refresh_watch_data_button = QPushButton("刷新自选数据")
        self.refresh_watch_data_button.setObjectName("primaryBtn")
        self._bind_action(
            self.refresh_watch_data_button,
            "data.refresh_watch",
            "刷新自选数据",
            "data",
            "execute",
            lambda: self._start_data_refresh("data_page"),
            status_message="按自选优先顺序刷新行情、当前个股、市场辅助数据和缓存状态。",
        )
        self.refresh_watch_data_button.clicked.connect(lambda: self._start_data_refresh("data_page"))
        v1_button = QPushButton("更新筛选快照")
        self._bind_action(
            v1_button,
            "data.update_v1_snapshot",
            "更新筛选快照",
            "data",
            "execute",
            self._start_v1_snapshot_update,
            risk_level="medium",
            preconditions=("确认公开数据源风险",),
            status_message="确认后后台更新 V1 因子快照，用于智能筛选。",
        )
        v1_button.clicked.connect(self._start_v1_snapshot_update)
        pit_button = QPushButton("PIT日增量")
        self._bind_action(
            pit_button,
            "data.pit_incremental",
            "PIT日增量",
            "data",
            "execute",
            self._start_pit_incremental,
            risk_level="medium",
            preconditions=("确认 PIT 数据库可用", "确认公开数据源风险"),
            status_message="确认后运行 PIT 日增量，保留预检和失败提示。",
        )
        pit_button.clicked.connect(self._start_pit_incremental)
        logs_button = QPushButton("查看抓取日志")
        self._bind_action(
            logs_button,
            "data.fetch_logs",
            "查看抓取日志",
            "data",
            "execute",
            self._show_fetch_logs,
            status_message="刷新并查看最近公开数据源调用日志。",
        )
        logs_button.clicked.connect(self._show_fetch_logs)
        clear_logs_button = QPushButton("清空抓取日志")
        self._bind_action(
            clear_logs_button,
            "data.clear_fetch_logs",
            "清空抓取日志",
            "data",
            "execute",
            self._clear_fetch_logs,
            risk_level="medium",
            preconditions=("确认仅清空抓取日志",),
            status_message="清空抓取日志；不影响缓存、策略、预警、任务、Key 或审计。",
        )
        clear_logs_button.clicked.connect(self._clear_fetch_logs)
        pit_failures_button = QPushButton("导出 PIT 失败清单")
        self._bind_action(
            pit_failures_button,
            "data.pit_failures.export",
            "导出 PIT 失败清单",
            "data",
            "execute",
            self.export_pit_failures_csv,
            status_message="导出 PIT 失败清单；没有失败记录时仍生成含表头的 UTF-8 BOM CSV。",
        )
        pit_failures_button.clicked.connect(self.export_pit_failures_csv)
        for button in (self.refresh_watch_data_button, v1_button, pit_button, logs_button, clear_logs_button, pit_failures_button):
            action_row.addWidget(button)
        action_row.addStretch(1)
        action_card.layout.addLayout(action_row)
        auto_row = QHBoxLayout()
        self.auto_data_refresh_check = QCheckBox("自动刷新行情")
        self.auto_data_refresh_check.setChecked(
            self.cache.get_setting("auto_data_refresh_enabled", "1") == "1"
        )
        self._bind_action(
            self.auto_data_refresh_check,
            "data.auto_refresh.toggle",
            "自动刷新行情",
            "data",
            "execute",
            lambda: self._set_auto_data_refresh_enabled(self.auto_data_refresh_check.isChecked()),
            status_message="按设定间隔自动更新自选和当前个股行情；关闭后仍可手动刷新。",
        )
        self.auto_data_refresh_check.toggled.connect(self._set_auto_data_refresh_enabled)
        self.auto_data_refresh_interval_combo = self._combo(
            [(f"每 {seconds} 秒", str(seconds)) for seconds in AUTO_DATA_REFRESH_INTERVALS]
        )
        try:
            saved_interval = int(self.cache.get_setting("auto_data_refresh_interval_seconds", "30"))
        except (TypeError, ValueError):
            saved_interval = 30
        interval_index = self.auto_data_refresh_interval_combo.findData(str(saved_interval))
        self.auto_data_refresh_interval_combo.setCurrentIndex(interval_index if interval_index >= 0 else 1)
        self._bind_action(
            self.auto_data_refresh_interval_combo,
            "data.auto_refresh.interval",
            "自动刷新间隔",
            "data",
            "execute",
            self._set_auto_data_refresh_interval,
            status_message="设置自动行情轮询间隔；公开源失败时会自动保留上一份数据。",
        )
        self.auto_data_refresh_interval_combo.currentIndexChanged.connect(self._set_auto_data_refresh_interval)
        auto_hint = QLabel("仅在交易时段轮询；财务、K线等低频数据不会每 15 秒重复抓取。")
        auto_hint.setObjectName("emptyState")
        auto_hint.setWordWrap(True)
        auto_row.addWidget(self.auto_data_refresh_check)
        auto_row.addWidget(self.auto_data_refresh_interval_combo)
        auto_row.addWidget(auto_hint, 1)
        action_card.layout.addLayout(auto_row)
        feature_row = QHBoxLayout()
        self.feature_auto_refresh_check = QCheckBox("功能按需自动刷新")
        self.feature_auto_refresh_check.setChecked(
            self.cache.get_setting("feature_auto_refresh_enabled", "1") == "1"
        )
        self._bind_action(
            self.feature_auto_refresh_check,
            "data.feature_auto_refresh.toggle",
            "功能按需自动刷新",
            "data",
            "execute",
            lambda: self._set_feature_auto_refresh_enabled(self.feature_auto_refresh_check.isChecked()),
            status_message="控制筛选、AI研究、个股研究和复盘入口是否自动准备依赖数据；关闭后过期数据会严格阻断。",
        )
        self.feature_auto_refresh_check.toggled.connect(self._set_feature_auto_refresh_enabled)
        feature_hint = QLabel(
            "控制筛选、AI、研究和复盘的数据准备；关闭后不会偷偷使用过期数据。默认：行情30秒、市场5分钟、K线15分钟、资金流5分钟、事件1小时、财务1天、筛选快照1个交易日。"
        )
        feature_hint.setObjectName("emptyState")
        feature_hint.setWordWrap(True)
        feature_row.addWidget(self.feature_auto_refresh_check)
        feature_row.addWidget(feature_hint, 1)
        action_card.layout.addLayout(feature_row)
        ttl_grid = QGridLayout()
        ttl_grid.setHorizontalSpacing(8)
        ttl_grid.setVerticalSpacing(6)
        self.feature_ttl_inputs: dict[str, QLineEdit] = {}
        for index, (setting_key, label, minimum, maximum, unit) in enumerate(FEATURE_TTL_CONTROL_SPECS):
            default = DataFreshnessPolicy(self.cache).seconds_for(setting_key)
            try:
                saved = int(self.cache.get_setting(setting_key, str(default)))
            except (TypeError, ValueError):
                saved = default
            value = saved if minimum <= saved <= maximum else default
            input_box = QLineEdit(str(value))
            input_box.setMaximumWidth(72)
            input_box.setToolTip(f"范围 {minimum}–{maximum} {unit}；保存后立即用于下一次功能数据新鲜度检查。")
            input_box.editingFinished.connect(
                lambda setting_key=setting_key: self._save_feature_ttl(setting_key)
            )
            self.feature_ttl_inputs[setting_key] = input_box
            column = (index % 4) * 2
            row = index // 4
            ttl_grid.addWidget(QLabel(f"{label} TTL"), row, column)
            ttl_grid.addWidget(input_box, row, column + 1)
        ttl_hint = QLabel("可调整功能数据新鲜度阈值；输入无效或超出范围时不会写入，仍保留上一次有效设置。")
        ttl_hint.setObjectName("emptyState")
        ttl_hint.setWordWrap(True)
        action_card.layout.addLayout(ttl_grid)
        action_card.layout.addWidget(ttl_hint)
        self.refresh_stage_table = build_table(["阶段", "状态", "进度", "结果", "说明"])
        action_card.layout.addWidget(self.refresh_stage_table)
        layout.addWidget(action_card)

        card = Card("功能数据健康度", "展示功能依赖的新鲜度、不可变快照和刷新事件；旧数据不会静默进入正式筛选或 AI 研究。")
        self.data_health_detail_table = build_table(["数据类型", "状态", "更新时间", "快照/事件 ID", "下次检查"])
        card.layout.addWidget(self.data_health_detail_table)
        layout.addWidget(card)

        log_card = Card("最近抓取日志", "展示最近公开数据源调用结果，用于判断实时、缓存和失败原因。")
        self.fetch_log_table = build_table(["时间", "动作", "对象", "状态", "说明"])
        log_card.layout.addWidget(self.fetch_log_table)
        layout.addWidget(log_card)

        export_card = Card("统一导出", "所有 CSV 均使用 UTF-8 BOM 和同目录原子替换；文件写入失败时，原目标文件不会改变。")
        export_row = QHBoxLayout()
        export_actions = (
            ("筛选结果", "export.center.screener", self.export_screener_csv),
            ("历史比较", "export.center.history_compare", self.export_selected_screener_comparison_csv),
            ("预警记录", "export.center.alerts", self.export_alert_history_csv),
            ("观察任务", "export.center.tracking", self.export_tracking_review_csv),
            ("表现明细", "export.center.performance", self.export_performance_review_csv),
        )
        for label, action_id, callback in export_actions:
            button = QPushButton(label)
            self._bind_action(button, action_id, f"导出{label}", "data", "execute", callback)
            button.clicked.connect(callback)
            export_row.addWidget(button)
        export_row.addStretch(1)
        export_card.layout.addLayout(export_row)
        layout.addWidget(export_card)

        layout.addStretch(1)
        return page

    def _build_settings_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        title = QLabel("设置")
        title.setObjectName("pageTitle")
        layout.addWidget(title)

        ai_card = Card("AI 模型", "API Key 保存在 Windows 凭据管理器；失败时才使用本地数据库兜底。")
        settings_grid = QGridLayout()
        settings_grid.setHorizontalSpacing(10)
        settings_grid.setVerticalSpacing(8)
        self.provider_combo = self._combo([("DeepSeek", "deepseek"), ("OpenAI", "openai")])
        self.model_input = QLineEdit()
        self.api_key_input = QLineEdit()
        self.api_key_input.setEchoMode(QLineEdit.Password)
        self.api_key_status_label = QLabel("")
        self.ai_cost_label = QLabel("")
        self.ai_cost_label.setWordWrap(True)
        self.save_settings_button = QPushButton("保存并验证")
        self.save_settings_button.setObjectName("primaryBtn")
        self._bind_action(
            self.save_settings_button,
            "settings.save_validate",
            "保存并验证",
            "settings",
            "execute",
            self.save_settings,
            risk_level="medium",
            preconditions=("填写 API Key 或确认清空",),
            status_message="保存 AI 设置并后台验证连接。",
        )
        self.save_settings_button.clicked.connect(self.save_settings)
        validate_button = QPushButton("重新验证连接")
        self._bind_action(
            validate_button,
            "settings.validate_ai_key",
            "重新验证连接",
            "settings",
            "execute",
            self.validate_saved_api_key,
            preconditions=("已填写 API Key",),
        )
        validate_button.clicked.connect(self.validate_saved_api_key)
        self._load_ai_settings()
        self.provider_combo.currentIndexChanged.connect(self._refresh_ai_cost_label)
        self.model_input.textChanged.connect(self._refresh_ai_cost_label)
        settings_grid.addWidget(QLabel("供应商"), 0, 0)
        settings_grid.addWidget(self.provider_combo, 0, 1)
        settings_grid.addWidget(QLabel("模型"), 1, 0)
        settings_grid.addWidget(self.model_input, 1, 1)
        settings_grid.addWidget(QLabel("API Key"), 2, 0)
        settings_grid.addWidget(self.api_key_input, 2, 1)
        settings_grid.addWidget(self.save_settings_button, 3, 0)
        settings_grid.addWidget(validate_button, 3, 1)
        settings_grid.addWidget(QLabel("状态"), 4, 0)
        settings_grid.addWidget(self.api_key_status_label, 4, 1)
        settings_grid.addWidget(self.ai_cost_label, 5, 0, 1, 2)
        self._refresh_ai_cost_label()
        self._refresh_top_ai_pill()
        ai_card.layout.addLayout(settings_grid)
        layout.addWidget(ai_card)

        tushare_card = Card("Tushare 数据兜底", "Token 默认掩码保存。仅在东财资金流失败且没有新缓存时，才会按开关尝试 Tushare；可能受权限和积分限制。")
        tushare_grid = QGridLayout()
        self.tushare_token_input = QLineEdit()
        self.tushare_token_input.setEchoMode(QLineEdit.Password)
        self.tushare_fallback_check = QCheckBox("东财资金流失败时启用 Tushare 兜底")
        self.tushare_status_label = QLabel("")
        tushare_grid.addWidget(QLabel("Tushare Token"), 0, 0)
        tushare_grid.addWidget(self.tushare_token_input, 0, 1)
        tushare_grid.addWidget(self.tushare_fallback_check, 1, 0, 1, 2)
        tushare_grid.addWidget(self.tushare_status_label, 2, 0, 1, 2)
        tushare_card.layout.addLayout(tushare_grid)
        layout.addWidget(tushare_card)

        tickflow_card = Card("TickFlow 付费兜底", "API Key 保存在 Windows 凭据管理器；配置后优先使用批量行情、财务和 K 线，公开免费源仍保留为回退。")
        tickflow_grid = QGridLayout()
        self.tickflow_api_key_input = QLineEdit()
        self.tickflow_api_key_input.setEchoMode(QLineEdit.Password)
        self.tickflow_status_label = QLabel("")
        tickflow_grid.addWidget(QLabel("TickFlow API Key"), 0, 0)
        tickflow_grid.addWidget(self.tickflow_api_key_input, 0, 1)
        tickflow_grid.addWidget(self.tickflow_status_label, 1, 0, 1, 2)
        tickflow_card.layout.addLayout(tickflow_grid)
        layout.addWidget(tickflow_card)

        storage_card = Card("数据存储", "自选股、观察任务、缓存和 API Key 数据库兜底都在当前数据目录。")
        storage_grid = QGridLayout()
        storage_grid.setHorizontalSpacing(10)
        storage_grid.setVerticalSpacing(8)
        self.data_dir_label = QLabel(str(self.data_dir))
        self.data_dir_label.setWordWrap(True)
        self.data_migration_status_label = QLabel("迁移前会预检目标目录；运行中的后台任务会阻止迁移。")
        self.data_migration_status_label.setWordWrap(True)
        open_button = QPushButton("打开数据文件夹")
        self._bind_action(
            open_button,
            "settings.open_data_dir",
            "打开数据文件夹",
            "settings",
            "external",
            self._open_data_dir,
            status_message="在系统文件管理器中打开当前数据目录。",
        )
        open_button.clicked.connect(self._open_data_dir)
        change_button = QPushButton("更换目录")
        self._bind_action(
            change_button,
            "settings.change_data_dir",
            "更换数据目录",
            "settings",
            "execute",
            self._change_data_dir,
            risk_level="medium",
            status_message="预检并迁移到空目录；成功后要求重启，避免旧任务迟到写入。",
        )
        change_button.clicked.connect(self._change_data_dir)
        cleanup_button = QPushButton("清理缓存")
        self._bind_action(
            cleanup_button,
            "settings.clear_cache",
            "清理缓存",
            "settings",
            "execute",
            self._clear_cache_from_settings,
            risk_level="medium",
            preconditions=("确认清理缓存",),
            status_message="清理行情、财务和 K 线缓存；不删除自选、观察任务和 API Key。",
        )
        cleanup_button.clicked.connect(self._clear_cache_from_settings)
        storage_grid.addWidget(QLabel("当前目录"), 0, 0)
        storage_grid.addWidget(self.data_dir_label, 0, 1, 1, 3)
        storage_grid.addWidget(open_button, 1, 0)
        storage_grid.addWidget(change_button, 1, 1)
        storage_grid.addWidget(cleanup_button, 1, 2)
        storage_grid.addWidget(QLabel("迁移状态"), 2, 0)
        storage_grid.addWidget(self.data_migration_status_label, 2, 1, 1, 3)
        storage_card.layout.addLayout(storage_grid)
        layout.addWidget(storage_card)

        acceptance_card = Card("验收中心", "展示最近 quick、full 与发布验收记录。报告缺失或 JSON 损坏会明确提示，不会影响本地数据。")
        self.acceptance_status_label = QLabel("")
        self.acceptance_status_label.setWordWrap(True)
        self.acceptance_table = build_table(["时间", "结论", "模式", "失败区块"])
        acceptance_actions = QHBoxLayout()
        for label, action_id, callback in (
            ("打开 TXT", "acceptance.open_txt", self.open_acceptance_txt),
            ("打开 JSON", "acceptance.open_json", self.open_acceptance_json),
            ("打开报告目录", "acceptance.open_directory", self.open_acceptance_report_dir),
            ("刷新", "acceptance.refresh", self.refresh_acceptance_center),
        ):
            button = QPushButton(label)
            self._bind_action(button, action_id, label, "settings", "external" if "打开" in label else "execute", callback)
            button.clicked.connect(callback)
            acceptance_actions.addWidget(button)
        acceptance_actions.addStretch(1)
        acceptance_card.layout.addWidget(self.acceptance_status_label)
        acceptance_card.layout.addWidget(self.acceptance_table)
        acceptance_card.layout.addLayout(acceptance_actions)
        layout.addWidget(acceptance_card)
        self.refresh_acceptance_center()

        usage_card = Card("使用说明")
        usage = QLabel(DISCLAIMER + "\n\n公开免费数据源可能延迟、缺失或变化；AI 功能会调用用户自己的服务商额度，费用以服务商账单为准。")
        usage.setObjectName("emptyState")
        usage.setWordWrap(True)
        usage_card.layout.addWidget(usage)
        layout.addWidget(usage_card)
        layout.addStretch(1)
        return page

    def _navigate(self, page_key: str) -> None:
        page = self.pages.get(page_key)
        if page is None:
            self._set_status(f"页面暂不可用：{page_key}")
            return
        if self._auto_refresh_ready and page_key in AUTO_REFRESH_PAGES:
            self._start_data_refresh("auto")
        self.stack.setCurrentWidget(page)
        self.sidebar.set_current(page_key)
        if page_key in {"home", "watchlist", "alerts", "review", "data"}:
            self._refresh_dashboard_snapshot()
            if page_key == "alerts":
                self._refresh_alerts_page()
            if page_key == "review":
                self._refresh_review_page()
            if page_key == "data":
                self._fill_fetch_log_table()

    def _route_search(self, text: str) -> None:
        intent = self.home_page.current_search_intent() if hasattr(self, "home_page") else classify_search_intent(text)
        if intent == "filter":
            self._navigate("screener")
            self.screener_query_input.setText(text.strip())
            self.screener_query_input.setFocus(Qt.OtherFocusReason)
            self.parse_screener_query()
            self._set_status("已带入智能筛选条件；可确认后执行本地快照筛选。")
            return
        self._navigate("research")
        self.search_input.setText(text.strip())
        self.search_stock(auto_load_single=True)

    def _selected_screening_mode(self) -> str:
        if not hasattr(self, "screening_mode_combo"):
            return self.current_screening_mode
        value = str(self.screening_mode_combo.currentData() or "formal")
        return value if value in SCREENING_MODE_LABELS else "formal"

    def _refresh_screening_mode_notice(self) -> None:
        if not hasattr(self, "screening_mode_notice"):
            return
        mode = self._selected_screening_mode()
        self.current_screening_mode = mode
        notices = {
            "formal": "正式模式：只有通过确定性数据质量闸门的结果才可进入 formal 观察样本；筛选完成后后台自动整理五 Agent，Agent 不改变候选集合。未配置 API Key 时不会发起请求。",
            "review_only": "研究复核：允许查看偏旧/部分数据；筛选后仍可自动整理证据，但结果只用于复核，不能视为正式候选或进入正式绩效样本。",
            "exploratory": "探索模式：仅用于实验和材料整理；筛选后仍可自动整理证据，界面持续显示 exploratory，永不进入正式绩效样本。",
        }
        self.screening_mode_notice.setText(notices[mode])
        self.screening_mode_notice.setProperty("screeningMode", mode)
        self.screening_mode_notice.style().unpolish(self.screening_mode_notice)
        self.screening_mode_notice.style().polish(self.screening_mode_notice)

    def _on_screening_mode_changed(self, _index: int) -> None:
        previous = self.current_screening_mode
        self._refresh_screening_mode_notice()
        if previous == self.current_screening_mode:
            return
        self.current_agent_orchestration = None
        self.current_agent_orchestrations = []
        self.current_evidence_aggregation = None
        self.current_evidence_aggregations = []
        self.current_candidate_evidence = {}
        self.current_candidate_states = {}
        self.current_screen_rows = []
        self._fill_screener_results([])
        if hasattr(self, "screener_candidate_detail"):
            self.screener_candidate_detail.setPlainText(
                f"已切换为 {SCREENING_MODE_LABELS[self.current_screening_mode]}；请重新执行筛选，避免混用不同模式的结果。"
            )
        self._set_status(f"已切换研究模式：{SCREENING_MODE_LABELS[self.current_screening_mode]}")
        self._refresh_screener_controls()

    def _refresh_screener_controls(self) -> None:
        if not hasattr(self, "screener_run_button"):
            return
        self._refresh_retryable_agent_status()
        has_spec = self.current_query_spec is not None
        has_rows = bool(self.current_screen_rows)
        self.screener_run_button.setEnabled(
            bool(self.screener_query_input.text().strip())
            and "screener_run" not in self._running_tasks
        )
        self.screener_save_button.setEnabled(has_spec and self.strategy_store is not None)
        self.screener_export_csv_button.setEnabled(has_rows)
        self.screener_export_json_button.setEnabled(has_rows)
        selected_row = self.screener_result_table.currentRow() if hasattr(self, "screener_result_table") else -1
        selected = self.current_screen_rows[selected_row] if 0 <= selected_row < len(self.current_screen_rows) else None
        self.screener_add_watch_button.setEnabled(
            bool(
                selected
                and selected.get("research_mode", self._selected_screening_mode()) == "formal"
                and selected.get("candidate_status") == "candidate"
            )
        )
        self.screener_research_button.setEnabled(bool(selected))
        if hasattr(self, "screener_retry_agents_button"):
            self.screener_retry_agents_button.setEnabled(
                (has_rows or bool(getattr(self, "_retryable_agent_batches", ())))
                and "agent_retry" not in self._running_tasks
            )

    def _refresh_retryable_agent_status(self) -> None:
        self._retryable_agent_batches = []
        label = getattr(self, "screener_retryable_status_label", None)
        if self.formal_evidence_store is None:
            if label is not None:
                label.setText("待重试五 Agent：证据队列不可用。")
            return
        try:
            self._retryable_agent_batches = self.formal_evidence_store.list_retryable(limit=100)
        except EvidenceReplayError as exc:
            if label is not None:
                label.setText(f"待重试五 Agent：队列校验失败，请查看数据目录：{exc}")
            return
        if label is not None:
            if not self._retryable_agent_batches:
                label.setText("待重试五 Agent：0 批")
            else:
                summary = "、".join(
                    f"{record.status}/{record.candidate_count}只"
                    for record in self._retryable_agent_batches[:5]
                )
                label.setText(
                    f"待重试五 Agent：{len(self._retryable_agent_batches)} 批（{summary}）；"
                    "重试使用已持久化证据包，不重新筛选。"
                )

    def _builtin_screener_preset(self, key: str) -> ScreenerPreset | None:
        return next((item for item in builtin_screener_presets() if item.key == key), None)

    def _detach_screener_template(self, *, reason: str = "") -> None:
        if self.current_template_contract is None:
            return
        self.current_template_contract = None
        self.current_strategy_preset_id = None
        if reason and hasattr(self, "screener_summary_box"):
            self.screener_summary_box.setPlainText(
                f"已解除内置模板绑定：{reason}\n\n当前条件属于模板派生条件/自定义策略，请保存后再运行。"
            )

    def _on_screener_query_text_changed(self, _text: str) -> None:
        if self._setting_screener_input:
            return
        self.current_query_spec = None
        self._detach_screener_template(reason="你修改了实际条件")
        self._refresh_screener_controls()

    def _update_builtin_screener_status(
        self,
        *,
        data_source: str = "待运行",
        snapshot_asof: str = "待获取",
        status: str = "待运行",
    ) -> None:
        preset = self.current_template_contract
        if preset is None:
            return
        label = self._builtin_screener_card_status.get(preset.key)
        if label is not None:
            label.setText(f"当前状态：{status}；来源：{data_source}；快照：{snapshot_asof}")

    def apply_builtin_screener_preset(self, key: str) -> WorkflowResult:
        preset = self._builtin_screener_preset(key)
        if preset is None:
            result = WorkflowResult("failed", f"内置模板不存在：{key}")
            self._apply_screener_workflow_result(result)
            return result
        try:
            spec = preset.to_query_spec()
            query_spec_to_screen_filter(spec)
        except Exception as exc:
            result = WorkflowResult("failed", f"内置模板不可执行：{exc}")
            self._apply_screener_workflow_result(result)
            return result
        self.current_template_contract = preset
        self.current_strategy_preset_id = None
        self.current_query_spec = spec
        self.current_screen_report = None
        self.current_screen_rows = []
        self.current_screener_run_id = None
        self.current_agent_orchestration = None
        self.current_agent_orchestrations = []
        self.current_evidence_aggregation = None
        self.current_evidence_aggregations = []
        self.current_candidate_evidence = {}
        self.current_candidate_states = {}
        self._setting_screener_input = True
        try:
            self.screener_query_input.setText(preset.nl_text)
            self.screener_strategy_name_input.setText(f"{preset.name} {preset.template_version}")
        finally:
            self._setting_screener_input = False
        self.screener_summary_box.setPlainText(
            preset.contract_text()
            + "\n\n实际执行条件来自结构化模板，不以自然语言重新推断。\n"
            "输出是候选股研究列表，不构成买卖建议。"
        )
        self._update_builtin_screener_status()
        self._fill_screener_results([])
        self._set_status(f"已载入内置模板：{preset.name} {preset.template_version}")
        self._refresh_screener_controls()
        return WorkflowResult("success", f"已载入内置模板：{preset.name} {preset.template_version}")

    def _selected_screener_strategy_id(self) -> int | None:
        if not hasattr(self, "screener_strategy_combo"):
            return None
        value = self.screener_strategy_combo.currentData()
        return int(value) if value not in (None, "") else None

    def refresh_screener_strategies(self, selected_id: int | None = None) -> None:
        if not hasattr(self, "screener_strategy_combo"):
            return
        combo = self.screener_strategy_combo
        target_id = selected_id if selected_id is not None else self.current_strategy_preset_id
        combo.blockSignals(True)
        combo.clear()
        if self.strategy_store is None:
            combo.addItem("策略库不可用", None)
        else:
            presets = self.strategy_store.list_presets()
            if not presets:
                combo.addItem("暂无已保存策略", None)
            for preset in presets:
                combo.addItem(f"{preset.name} · v{preset.run_count or 1}", preset.id)
            index = combo.findData(target_id)
            if index >= 0:
                combo.setCurrentIndex(index)
        combo.blockSignals(False)
        self._refresh_screener_strategy_versions()

    def _refresh_screener_strategy_versions(self) -> None:
        if not hasattr(self, "screener_strategy_version_combo"):
            return
        combo = self.screener_strategy_version_combo
        combo.clear()
        preset_id = self._selected_screener_strategy_id()
        if self.strategy_store is None or preset_id is None:
            combo.addItem("先选择已保存策略", None)
            return
        versions = self.strategy_store.list_preset_versions(preset_id)
        if not versions:
            combo.addItem("暂无版本历史", None)
            return
        for version in versions:
            note = version.change_note or "无说明"
            combo.addItem(f"v{version.version_no} · {note[:28]}", version.id)

    def load_screener_strategy(self) -> WorkflowResult:
        preset_id = self._selected_screener_strategy_id()
        if self.strategy_store is None or preset_id is None:
            result = WorkflowResult("skipped", "请先选择一个已保存策略。")
            self._apply_screener_workflow_result(result)
            return result
        try:
            preset, spec = self.strategy_store.load_query_spec(preset_id)
        except Exception as exc:
            result = WorkflowResult("failed", f"载入策略失败：{exc}")
            self._apply_screener_workflow_result(result)
            return result
        self.current_strategy_preset_id = preset.id
        self.current_query_spec = spec
        self.current_screen_rows = []
        self.current_screen_report = None
        self.current_screener_run_id = None
        self.current_agent_orchestration = None
        self.current_agent_orchestrations = []
        self.current_evidence_aggregation = None
        self.current_evidence_aggregations = []
        self.current_candidate_evidence = {}
        self.current_candidate_states = {}
        template = next(
            (
                item
                for item in builtin_screener_presets()
                if item.template_id == preset.template_id
                and item.template_version == preset.template_version
                and condition_hash(item.to_query_spec()) == condition_hash(spec)
            ),
            None,
        )
        self.current_template_contract = template
        self._setting_screener_input = True
        try:
            self.screener_query_input.setText(preset.user_text)
            self.screener_strategy_name_input.setText(preset.name)
        finally:
            self._setting_screener_input = False
        self.screener_summary_box.setPlainText(f"已载入策略：{preset.name}\n\n我理解为：\n{preset.summary}")
        if template is not None:
            self.screener_summary_box.setPlainText(template.contract_text() + f"\n\n已载入保存策略：{preset.name}")
        result = WorkflowResult("success", f"已载入策略：{preset.name}")
        self._apply_screener_workflow_result(result)
        return result

    def delete_screener_strategy(self) -> WorkflowResult:
        preset_id = self._selected_screener_strategy_id()
        if self.strategy_store is None or preset_id is None:
            result = WorkflowResult("skipped", "请先选择一个已保存策略。")
            self._apply_screener_workflow_result(result)
            return result
        preset = self.strategy_store.get_preset(preset_id)
        if preset is None:
            self.refresh_screener_strategies()
            result = WorkflowResult("failed", "策略已不存在，请刷新列表。")
            self._apply_screener_workflow_result(result)
            return result
        confirmed = QMessageBox.question(
            self,
            "删除策略",
            f"删除“{preset.name}”？已保存的历史执行记录不会删除。",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if confirmed != QMessageBox.Yes:
            result = WorkflowResult("skipped", "已取消删除策略。")
            self._apply_screener_workflow_result(result)
            return result
        self.strategy_store.delete_preset(preset_id)
        if self.current_strategy_preset_id == preset_id:
            self.current_strategy_preset_id = None
        self.refresh_screener_strategies()
        result = WorkflowResult("success", f"已删除策略：{preset.name}")
        self._apply_screener_workflow_result(result)
        return result

    def import_screener_strategy_preview(self) -> WorkflowResult:
        path, _ = QFileDialog.getOpenFileName(self, "导入策略 JSON（仅预览）", str(self.data_dir), "JSON 文件 (*.json)")
        if not path:
            result = WorkflowResult("skipped", "已取消导入策略。")
            self._apply_screener_workflow_result(result)
            return result
        try:
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
            name, user_text, spec, summary, _parse_source = query_spec_from_export_payload(payload)
        except Exception as exc:
            result = WorkflowResult("failed", f"导入文件不可用，未写入策略库：{exc}")
            self._apply_screener_workflow_result(result)
            return result
        self.current_strategy_preset_id = None
        self.current_query_spec = spec
        self.current_screen_rows = []
        self.current_screen_report = None
        self.current_screener_run_id = None
        self.current_agent_orchestration = None
        self.current_agent_orchestrations = []
        self.current_evidence_aggregation = None
        self.current_evidence_aggregations = []
        self.current_candidate_evidence = {}
        self.current_candidate_states = {}
        self.screener_query_input.setText(user_text)
        self.screener_strategy_name_input.setText(name)
        self.screener_summary_box.setPlainText(f"导入预览（尚未保存）：{name}\n\n我理解为：\n{summary}\n\n请确认后点击“保存策略”。")
        result = WorkflowResult("success", "导入预览已通过本地校验；尚未写入策略库。")
        self._apply_screener_workflow_result(result)
        return result

    def restore_screener_strategy_version(self) -> WorkflowResult:
        if self.strategy_store is None:
            result = WorkflowResult("failed", "策略库不可用，请检查数据目录。")
            self._apply_screener_workflow_result(result)
            return result
        version_id = self.screener_strategy_version_combo.currentData() if hasattr(self, "screener_strategy_version_combo") else None
        if version_id in (None, ""):
            result = WorkflowResult("skipped", "请先选择一个策略版本。")
            self._apply_screener_workflow_result(result)
            return result
        confirmed = QMessageBox.question(
            self,
            "恢复策略版本",
            "恢复会将所选版本保存为当前策略的新版本，原版本仍会保留。是否继续？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if confirmed != QMessageBox.Yes:
            result = WorkflowResult("skipped", "已取消恢复策略版本。")
            self._apply_screener_workflow_result(result)
            return result
        try:
            preset = self.strategy_store.restore_preset_version(int(version_id), "Qt 工作台恢复历史版本")
        except Exception as exc:
            result = WorkflowResult("failed", f"恢复策略版本失败：{exc}")
            self._apply_screener_workflow_result(result)
            return result
        self.refresh_screener_strategies(preset.id)
        return self.load_screener_strategy()

    def parse_screener_query(self) -> WorkflowResult:
        text = self.screener_query_input.text().strip()
        self.current_query_spec = None
        self.current_screen_report = None
        self.current_screen_rows = []
        self.current_screener_run_id = None
        self.current_agent_orchestration = None
        self.current_agent_orchestrations = []
        self.current_evidence_aggregation = None
        self.current_evidence_aggregations = []
        self.current_candidate_evidence = {}
        self.current_candidate_states = {}
        self._fill_screener_results([])
        if not text:
            result = WorkflowResult("skipped", "请输入筛选条件，例如：箱体横盘、ROE大于15、PE小于40。")
            self._apply_screener_workflow_result(result)
            return result
        try:
            spec = parse_local_query(text)
            if spec is None:
                raise ValueError("本地规则暂未识别该表达；请改写为可执行条件，例如 ROE大于15、PE小于40、箱体横盘。")
            spec = apply_local_query_guardrails(spec)
            query_spec_to_screen_filter(spec)
        except Exception as exc:
            result = WorkflowResult(
                "failed",
                f"筛选条件暂不可执行：{exc}",
                warnings=["可先使用 ROE、PE、市值、换手率、RSI、箱体评分、行业强度等已支持字段。"],
            )
            self._apply_screener_workflow_result(result)
            return result

        if self.current_template_contract is not None:
            if condition_hash(spec) != condition_hash(self.current_template_contract.to_query_spec()):
                self._detach_screener_template(reason="解析后的结构化条件已不同于原模板")
        self.current_query_spec = spec
        summary = query_spec_summary(spec)
        message = "已解析条件。筛选使用本地 V1 因子快照；顶部行情已更新不代表筛选快照已就绪，请在数据状态页确认“因子筛选快照”。"
        result = WorkflowResult(
            "success",
            message,
            rows=[{"summary": summary}],
            warnings=list(spec.resolved_notes or []),
        )
        self.screener_summary_box.setPlainText(
            "我理解为：\n"
            f"{summary}\n\n"
            "数据来源：本地 V1 因子快照；顶部行情已更新不等于筛选快照已就绪。\n"
            "请在数据状态页确认“因子筛选快照”状态；收盘快照不完整时请切换为研究复核。\n"
            "边界：输出是候选股研究列表，不构成买卖建议。"
        )
        self._set_status(message)
        self._refresh_screener_controls()
        return result

    def run_screener_query(self) -> bool:
        if self.current_query_spec is None:
            result = self.parse_screener_query()
            if result.status != "success":
                return False
        assert self.current_query_spec is not None
        spec = self.current_query_spec
        user_text = self.screener_query_input.text().strip()
        requested_mode = self._selected_screening_mode()
        screening_ai_config = self._current_ai_config()
        self.current_screen_rows = []
        self.current_candidate_evidence = {}
        self.current_candidate_states = {}
        self.current_agent_orchestration = None
        self.current_agent_orchestrations = []
        self.current_evidence_aggregation = None
        self.current_evidence_aggregations = []
        self._fill_screener_results([])

        def task() -> WorkflowResult:
            return self._execute_screener_query(
                spec,
                user_text,
                requested_mode=requested_mode,
                ai_config=screening_ai_config,
            )

        def done(result: WorkflowResult) -> None:
            if requested_mode != self._selected_screening_mode():
                self._set_status("筛选结果所属研究模式已变化，已丢弃旧模式结果；请按当前模式重新执行。")
                return
            self.current_agent_orchestration = result.private_artifacts.get("agent_orchestration")
            self.current_agent_orchestrations = list(result.private_artifacts.get("agent_orchestrations") or ())
            if self.current_agent_orchestration is None and self.current_agent_orchestrations:
                self.current_agent_orchestration = self.current_agent_orchestrations[0]
            self.current_evidence_aggregation = result.private_artifacts.get("evidence_aggregation")
            self.current_evidence_aggregations = list(result.private_artifacts.get("evidence_aggregations") or ())
            if self.current_evidence_aggregation is None and self.current_evidence_aggregations:
                self.current_evidence_aggregation = self.current_evidence_aggregations[0]
            self._apply_screener_workflow_result(result)
            self._refresh_screener_controls()

        loading_text = (
            "正在准备智能筛选数据并整理五 Agent（优先本地回放；未命中时可能产生模型费用）..."
            if screening_ai_config.api_key.strip()
            else "正在准备智能筛选数据（未配置 API Key，不会发起五 Agent 请求）..."
        )
        started = self._run_background(
            loading_text,
            task,
            done,
            task_key="screener_run",
            duplicate_text="筛选任务仍在后台运行，完成后会自动更新结果。",
            failed=lambda message: self._apply_screener_workflow_result(WorkflowResult("failed", f"筛选失败：{message}")),
        )
        self._refresh_screener_controls()
        return started

    def _execute_screener_query(
        self,
        spec: QuerySpec,
        user_text: str,
        *,
        requested_mode: str | None = None,
        ai_config: AIConfig | None = None,
    ) -> WorkflowResult:
        if self.factor_store is None or self.strategy_store is None:
            detail = f"：{self.v1_error_message}" if self.v1_error_message else ""
            return WorkflowResult("failed", f"V1 因子库不可用{detail}。请检查数据目录或重新选择数据目录。")

        warnings: list[str] = []
        execution_mode = requested_mode or self._selected_screening_mode()
        if execution_mode not in SCREENING_MODE_LABELS:
            execution_mode = "formal"
        self._sync_data_readiness_dependencies()
        readiness = self.data_readiness.ensure(
            DataRequirement(
                feature="智能筛选",
                kinds=("factor_snapshot",),
                mode=execution_mode,
                reason="执行正式筛选前必须使用同一份已验证不可变快照",
            )
        )
        if readiness.status == "blocked":
            review_hint = (
                "当前没有可用于正式筛选的 verified 快照。若数据中心显示已更新，那通常只是自选/个股行情；"
                "请先确认“因子筛选快照”。收盘后如仅有真实但不完整的快照，可切换“研究复核 review_only”查看候选和缺口。"
            )
            return WorkflowResult(
                "failed",
                f"{readiness.message}\n{review_hint}",
                warnings=list(readiness.errors),
                private_artifacts={"data_readiness": readiness},
            )
        try:
            snapshot_store = DataSnapshotStore(self.factor_store.database)
            data_manifest = snapshot_store.get_manifest(readiness.snapshot_id)
            snapshots = self.factor_store.list_snapshots_for_manifest(readiness.snapshot_id)
        except Exception as exc:
            return WorkflowResult(
                "failed",
                f"本次数据快照不可重建，未执行筛选：{exc}",
                private_artifacts={"data_readiness": readiness},
            )
        if data_manifest is None or not snapshots:
            return WorkflowResult(
                "failed",
                "本次刷新没有提交可用的不可变因子快照，智能筛选已阻断。",
                warnings=["未回退到可变因子缓存，避免快照身份与数值错配。"],
                private_artifacts={"data_readiness": readiness},
            )
        data_source = data_manifest.data_source
        if execution_mode == "review_only" and not is_manifest_fresh(data_manifest, FACTOR_SNAPSHOT_MAX_AGE):
            return WorkflowResult(
                "failed",
                "历史复核快照已超过 72 小时最大有效边界，智能筛选已阻断。",
                private_artifacts={"data_readiness": readiness},
            )
        if data_manifest.quality_status in {"partial", "unverified"}:
            warnings.append(f"本次快照质量为 {data_manifest.quality_status}，结果仅保留研究复核语义。")

        template = self.current_template_contract
        workflow = ScreeningWorkflowService(self.strategy_store, self.performance_store).run_snapshot_screening(
            snapshots,
            spec,
            source_label="Qt智能筛选",
            user_text=user_text,
            data_source=data_source,
            preset_id=self.current_strategy_preset_id,
            summary=query_spec_summary(spec),
            note="；".join(warnings[:6]),
            execution_mode=execution_mode,
            template_id=template.template_id if template else "",
            template_version=template.template_version if template else "",
            universe_id=template.universe_id if template else "a_share_all",
            ranking_formula_id="candidate_lens.v1",
            ranking_formula_version="candidate_lens.v1",
            regime_id="fixed_baseline",
            regime_detector_version="v1",
            data_manifest=data_manifest,
            require_data_manifest=True,
            refresh_event_id=readiness.refresh_event_id,
            candidate_lens_mode=True,
            universe_version=data_manifest.universe_version if data_manifest is not None else "",
            members_digest=stable_hash(sorted(snapshot.code for snapshot in snapshots)),
            selection_scope=(
                f"top_{len(snapshots)}_by_market_cap"
                if data_manifest is not None
                else ""
            ),
            selection_policy_version="v1",
            selected_limit=(
                int(json.loads(data_manifest.quality_summary_json or "{}").get("scan_limit") or len(snapshots))
                if data_manifest is not None
                else None
            ),
            truncation_reason=(
                "公共行情候选按市值截断"
                if data_manifest is not None and len(snapshots) >= 300
                else ""
            ),
            member_status_policy="listed_non_st",
        )
        report = workflow.report
        if (
            data_manifest is not None
            and execution_mode == "review_only"
            and data_manifest.quality_status in {"partial", "unverified"}
            and report.status == "review_only"
        ):
            # Keep the UI's quality diagnosis visible while the persisted run
            # remains explicitly review_only in its execution context.
            report.status = data_manifest.quality_status
        self.current_screen_report = report
        rows = [
            self._screen_candidate_row(index, candidate, report=report)
            for index, candidate in enumerate(report.results, start=1)
        ]
        for row in rows:
            row["research_mode"] = execution_mode
            row["research_mode_label"] = SCREENING_MODE_LABELS.get(execution_mode, execution_mode)
        agent_warnings: list[str] = []
        formal_results: list[AgentOrchestrationResult] = []
        evidence_aggregations: list[Any] = []
        evidence_packages: list[dict[str, Any]] = []
        if rows and data_manifest is not None:
            try:
                formal_results, evidence_aggregations, evidence_packages, agent_warnings = (
                    self._run_screening_agents_for_rows(
                        rows,
                        data_manifest,
                        execution_mode=execution_mode,
                        ai_config=ai_config,
                    )
                )
            except Exception as exc:
                agent_warnings.append(f"五 Agent 自动整理失败，已保留确定性候选：{exc}")
        if formal_results:
            base_gaps_by_code = {
                _candidate_code(row.get("code")): list(row.get("data_gaps") or ())
                for row in rows
            }
            views = _agent_evidence_views(
                formal_results,
                evidence_aggregations,
                base_data_gaps_by_code=base_gaps_by_code,
                research_mode=execution_mode,
                evidence_package_hashes=[
                    str(package.get("evidence_package_hash") or "")
                    for package in evidence_packages
                ],
            )
            for row in rows:
                code = _candidate_code(row.get("code"))
                row.update(views.get(code, {}))
                if code not in views:
                    row.update(
                        {
                            "agent_status": "五 Agent 未返回该候选结果",
                            "coverage": "未运行",
                            "f_layer_status": "skipped",
                            "next_checks": list(
                                dict.fromkeys(
                                    [*(row.get("next_checks") or ()), "五 Agent 未返回该候选结果"]
                                )
                            ),
                        }
                    )
        else:
            reason = "未配置 API Key，未发起五 Agent 请求" if not (ai_config and ai_config.api_key.strip()) else "五 Agent 未返回可回放结果"
            for row in rows:
                row.update(
                    {
                        "agent_status": reason,
                        "coverage": "未运行",
                        "f_layer_status": "skipped",
                        "next_checks": list(dict.fromkeys([*(row.get("next_checks") or ()), reason])),
                    }
                )
        warnings.extend(agent_warnings)
        warnings.extend(report.notes)
        warnings.extend(report.errors)
        report_status = report.status or "verified"
        status = "success" if report_status == "verified" else "partial"
        if report_status == "blocked":
            status = "skipped"
        if report.errors and rows:
            status = "partial"
        elif report.errors and not rows:
            status = "skipped" if report_status == "blocked" else "failed"
        elif warnings:
            status = "partial"

        self.current_screener_run_id = workflow.run_id
        artifacts = {
            "run_id": str(workflow.run_id),
            "research_mode": execution_mode,
            "screening_status": report_status,
            "data_source": report.data_source or data_source,
            "snapshot_asof": report.snapshot_asof or "",
            "data_snapshot_id": report.data_snapshot_id or "",
            "refresh_event_id": readiness.refresh_event_id,
        }
        if workflow.certificate_id:
            artifacts["certificate_id"] = workflow.certificate_id
        if workflow.observation_samples is not None:
            artifacts["observation_samples"] = str(workflow.observation_samples)
        if workflow.warnings:
            warnings.extend(workflow.warnings)
            status = "partial" if status == "success" else status
        if report_status == "blocked":
            message = f"正式筛选已阻断：{report.errors[0] if report.errors else '关键数据不可验证'}"
        elif report_status == "review_only":
            message = f"历史复核完成：匹配 {len(rows)} 只，扫描 {report.scanned} 只；结果不可视为正式筛选。"
        else:
            message = f"筛选完成：匹配 {len(rows)} 只，扫描 {report.scanned} 只；数据来源 {data_source}。"
        self._update_builtin_screener_status(
            data_source=report.data_source or data_source,
            snapshot_asof=report.snapshot_asof or "",
            status=report_status,
        )
        if formal_results:
            if len(formal_results) == 1:
                artifacts["agent_run_id"] = formal_results[0].run_id
                artifacts["agent_status"] = formal_results[0].status
                if evidence_aggregations and evidence_aggregations[0] is not None:
                    artifacts["evidence_summary_hash"] = evidence_aggregations[0].summary_hash
                if evidence_packages:
                    artifacts["evidence_package_hash"] = str(evidence_packages[0]["evidence_package_hash"])
            else:
                artifacts["agent_batch_count"] = len(formal_results)
                artifacts["agent_run_ids"] = [item.run_id for item in formal_results]
                artifacts["agent_statuses"] = [item.status for item in formal_results]
                artifacts["evidence_summary_hashes"] = [
                    item.summary_hash for item in evidence_aggregations if item is not None
                ]
                artifacts["evidence_package_hashes"] = [
                    str(item["evidence_package_hash"]) for item in evidence_packages
                ]
        return WorkflowResult(
            status,
            message,
            rows=rows,
            artifacts=artifacts,
            warnings=warnings,
            private_artifacts={
                "data_readiness": readiness,
                "agent_orchestration": formal_results[0] if len(formal_results) == 1 else None,
                "agent_orchestrations": formal_results,
                "evidence_aggregation": evidence_aggregations[0] if len(evidence_aggregations) == 1 else None,
                "evidence_aggregations": evidence_aggregations,
                "evidence_package": evidence_packages[0] if len(evidence_packages) == 1 else None,
                "evidence_packages": evidence_packages,
            },
        )

    def _run_screening_agents_for_rows(
        self,
        rows: list[dict[str, Any]],
        manifest,
        *,
        execution_mode: str,
        ai_config: AIConfig | None,
    ) -> tuple[list[AgentOrchestrationResult], list[Any], list[dict[str, Any]], list[str]]:
        """Run or replay the complete five-Agent overlay for every screen row.

        The formal Agent contract caps one package at 30 candidates.  The Qt
        result list can be larger, so each batch is independently validated,
        orchestrated, aggregated and persisted; no row is silently dropped.
        """

        if self.factor_store is None:
            return [], [], [], ["五 Agent 自动整理跳过：V1 因子库不可用"]
        snapshot_store = DataSnapshotStore(self.factor_store.database)
        facts = snapshot_store.list_facts(manifest.snapshot_id)
        warnings: list[str] = []
        formal_results: list[AgentOrchestrationResult] = []
        evidence_aggregations: list[Any] = []
        evidence_packages: list[dict[str, Any]] = []
        refresh_event_id = snapshot_store.refresh_event_id_for_snapshot(manifest.snapshot_id)
        batch_size = 30
        for batch_start in range(0, len(rows), batch_size):
            batch_rows = rows[batch_start: batch_start + batch_size]
            try:
                package = build_screening_evidence_package(
                    manifest=manifest,
                    facts=facts,
                    rows=batch_rows,
                    refresh_event_id=refresh_event_id,
                    lens_version="candidate_lens.v1",
                    batch_range=(batch_start + 1, batch_start + len(batch_rows)),
                )
            except Exception as exc:
                warnings.append(
                    f"五 Agent 第 {batch_start // batch_size + 1} 批证据包构造失败，已保留该批确定性候选：{exc}"
                )
                continue
            if self.formal_evidence_store is not None:
                try:
                    self.formal_evidence_store.save_package(
                        package,
                        status="pending",
                    )
                except EvidenceReplayError as exc:
                    warnings.append(f"第 {batch_start // batch_size + 1} 批证据包持久化失败：{exc}")
            replay = None
            if self.formal_evidence_store is not None:
                try:
                    replay = self.formal_evidence_store.latest(
                        snapshot_id=manifest.snapshot_id,
                        candidate_set_digest=str(package["candidate_set_digest"]),
                    )
                    if replay is not None and replay.evidence_package_hash == str(package["evidence_package_hash"]):
                        retryable = replay.orchestration is None or any(
                            execution.status in {"failed", "skipped"} or execution.outcome != "ok"
                            for execution in replay.orchestration.executions
                        )
                        if replay.orchestration is not None and not retryable:
                            warnings.append(
                                f"第 {batch_start // batch_size + 1} 批已从本地持久化证据包回放，未重新调用模型"
                            )
                            formal_results.append(replay.orchestration)
                            evidence_aggregations.append(replay.aggregation)
                            evidence_packages.append(package)
                            self.formal_evidence_store.mark_package(
                                package,
                                status="completed",
                                run_id=replay.run_id,
                            )
                            continue
                        warnings.append(
                            f"第 {batch_start // batch_size + 1} 批存在失败/跳过 Agent，将仅重试该批未完成执行"
                        )
                except EvidenceReplayError as exc:
                    warnings.append(f"第 {batch_start // batch_size + 1} 批已有 F 层回放校验失败，未采用该记录：{exc}")
            if ai_config is None:
                ai_config = AIConfig(provider="deepseek", api_key="", model="")
            if not ai_config.api_key.strip():
                warnings.append("五 Agent 自动整理跳过：未配置 API Key，证据包已保存，待配置后可重试")
            if self.performance_store is not None:
                agent_store = SqliteAgentExecutionStore(str(self.performance_store.database.db_path))
            else:
                agent_store = MemoryAgentExecutionStore()
            try:
                formal_result = run_formal_five_agent_analysis(
                    ai_config,
                    package,
                    result_store=agent_store,
                )
                formal_results.append(formal_result)
                evidence_packages.append(package)
                if execution_mode == "exploratory" and self.exploratory_proposal_store is not None:
                    for execution in formal_result.executions:
                        for proposal in proposals_from_agent_result(
                            execution.result,
                            snapshot_id=manifest.snapshot_id,
                            source_run_id=formal_result.run_id,
                        ):
                            try:
                                self.exploratory_proposal_store.save(proposal, snapshot_id=manifest.snapshot_id)
                            except (TypeError, ValueError) as exc:
                                warnings.append(f"探索建议未入队：{exc}")
            except Exception as exc:
                warnings.append(
                    f"五 Agent 第 {batch_start // batch_size + 1} 批运行失败，已保留该批确定性候选：{exc}"
                )
                continue
            try:
                evidence_aggregation = aggregate_agent_evidence(
                    package,
                    formal_result,
                    run_id=formal_result.run_id,
                    created_at=datetime.now().isoformat(timespec="seconds"),
                )
                evidence_aggregations.append(evidence_aggregation)
                if self.formal_evidence_store is not None:
                    all_ok = all(
                        execution.status in {"ok", "degraded"} and execution.outcome == "ok"
                        for execution in formal_result.executions
                    )
                    queue_status = "completed" if all_ok else (
                        "skipped" if all(execution.status == "skipped" for execution in formal_result.executions)
                        else "partial"
                    )
                    self.formal_evidence_store.mark_package(
                        package,
                        status=queue_status,
                        run_id=formal_result.run_id,
                        error="；".join(execution.error for execution in formal_result.executions if execution.error),
                    )
            except Exception as exc:
                evidence_aggregations.append(None)
                warnings.append(f"第 {batch_start // batch_size + 1} 批 F 层证据校验失败：{exc}")
                continue
            if self.formal_evidence_store is not None:
                try:
                    self.formal_evidence_store.save(
                        package,
                        evidence_aggregation,
                        orchestration=formal_result,
                    )
                except Exception as exc:
                    warnings.append(f"第 {batch_start // batch_size + 1} 批 F 层回放保存失败，当前会话结果仍可用：{exc}")
        if len(rows) > batch_size:
            warnings.insert(0, f"候选共 {len(rows)} 只，已按五 Agent 单包上限分为 {(len(rows) + batch_size - 1) // batch_size} 批自动整理")
        return formal_results, evidence_aggregations, evidence_packages, warnings

    def _retry_persisted_agent_batches(
        self,
        *,
        ai_config: AIConfig | None,
        records: list[Any] | None = None,
    ) -> tuple[list[AgentOrchestrationResult], list[Any], list[dict[str, Any]], list[str]]:
        """Retry durable batches without rebuilding a screening result."""

        if self.formal_evidence_store is None:
            return [], [], [], ["五 Agent 持久化队列不可用，未执行重试"]
        if records is None:
            try:
                retryable = self.formal_evidence_store.list_retryable(limit=100)
            except EvidenceReplayError as exc:
                return [], [], [], [f"待重试五 Agent 队列校验失败：{exc}"]
        else:
            retryable = list(records)
        if not retryable:
            return [], [], [], ["当前没有待重试的五 Agent 批次"]
        config = ai_config or AIConfig(provider="deepseek", api_key="", model="")
        agent_store = (
            SqliteAgentExecutionStore(str(self.performance_store.database.db_path))
            if self.performance_store is not None
            else MemoryAgentExecutionStore()
        )
        formal_results: list[AgentOrchestrationResult] = []
        aggregations: list[Any] = []
        packages: list[dict[str, Any]] = []
        warnings: list[str] = []
        for record in retryable:
            package = dict(record.package)
            try:
                result = run_formal_five_agent_analysis(
                    config,
                    package,
                    result_store=agent_store,
                )
                aggregation = aggregate_agent_evidence(
                    package,
                    result,
                    run_id=result.run_id,
                    created_at=datetime.now().isoformat(timespec="seconds"),
                )
                all_ok = all(
                    execution.status in {"ok", "degraded"} and execution.outcome == "ok"
                    for execution in result.executions
                )
                queue_status = "completed" if all_ok else (
                    "skipped" if all(execution.status == "skipped" for execution in result.executions)
                    else "partial"
                )
                self.formal_evidence_store.mark_package(
                    package,
                    status=queue_status,
                    run_id=result.run_id,
                    error="；".join(execution.error for execution in result.executions if execution.error),
                )
                self.formal_evidence_store.save(package, aggregation, orchestration=result)
                formal_results.append(result)
                aggregations.append(aggregation)
                packages.append(package)
            except Exception as exc:
                warnings.append(f"批次 {record.package_id[:12]} 重试失败，仍可再次重试：{exc}")
        return formal_results, aggregations, packages, warnings

    def retry_screening_agents(self) -> bool:
        """Retry only incomplete Agent batches for the current immutable result."""

        if not self.current_screen_rows:
            self._refresh_retryable_agent_status()
            if not self._retryable_agent_batches:
                self._set_status("当前没有可重试的筛选候选或持久化 Agent 批次。")
                return False

            def retry_task():
                return self._retry_persisted_agent_batches(ai_config=self._current_ai_config())

            def retry_done(payload) -> None:
                _formal_results, _aggregations, _packages, warnings = payload
                self._refresh_retryable_agent_status()
                self._set_status(
                    "已从持久化证据包重试待完成五 Agent；没有重新执行筛选。"
                    + ("；" + "；".join(warnings[:3]) if warnings else "")
                )

            return self._run_background(
                "正在从持久化证据包重试五 Agent...",
                retry_task,
                retry_done,
                task_key="agent_retry",
                duplicate_text="五 Agent 重试仍在后台运行。",
            )
        rows = list(self.current_screen_rows)
        snapshot_id = str(rows[0].get("data_snapshot_id") or "")
        execution_mode = str(rows[0].get("research_mode") or self._selected_screening_mode())
        config = self._current_ai_config()

        persisted_records: list[Any] = []
        persisted_queue_warning = ""
        if self.formal_evidence_store is not None:
            current_codes = {
                _candidate_code(row.get("code"))
                for row in rows
                if _candidate_code(row.get("code"))
            }
            try:
                persisted_records = [
                    record
                    for record in self.formal_evidence_store.list_retryable(limit=100)
                    if record.snapshot_id == snapshot_id
                    and {
                        _candidate_code(item.get("code"))
                        for item in record.package.get("candidate_records", [])
                        if isinstance(item, dict) and _candidate_code(item.get("code"))
                    } <= current_codes
                ]
            except EvidenceReplayError as exc:
                persisted_queue_warning = f"当前快照持久化队列校验失败，将回退当前行重建：{exc}"

        def apply_agent_retry_payload(payload) -> tuple[list[str], list[Any]]:
            formal_results, aggregations, packages, warnings = payload
            self.current_agent_orchestrations = list(formal_results)
            self.current_agent_orchestration = formal_results[0] if formal_results else None
            self.current_evidence_aggregations = list(aggregations)
            self.current_evidence_aggregation = aggregations[0] if aggregations else None
            views = _agent_evidence_views(
                formal_results,
                aggregations,
                base_data_gaps_by_code={
                    _candidate_code(row.get("code")): list(row.get("data_gaps") or ())
                    for row in rows
                },
                research_mode=execution_mode,
                evidence_package_hashes=[str(item.get("evidence_package_hash") or "") for item in packages],
            )
            for row in self.current_screen_rows:
                row.update(views.get(_candidate_code(row.get("code")), {}))
            self._fill_screener_results(self.current_screen_rows)
            self._refresh_candidate_states(snapshot_id=snapshot_id)
            self._refresh_retryable_agent_status()
            return warnings, packages

        if persisted_records:
            def persisted_task():
                return self._retry_persisted_agent_batches(
                    ai_config=config,
                    records=persisted_records,
                )

            def persisted_done(payload) -> None:
                warnings, _packages = apply_agent_retry_payload(payload)
                self._set_status(
                    "已使用当前快照的持久化证据包重试五 Agent；候选资格、排序和人工观察状态未改变。"
                    + ("；" + ";".join(warnings[:3]) if warnings else "")
                )

            return self._run_background(
                "正在使用当前快照的持久化证据包重试五 Agent...",
                persisted_task,
                persisted_done,
                task_key="agent_retry",
                duplicate_text="五 Agent 重试仍在后台运行。",
            )

        if self.factor_store is None:
            self._set_status("当前候选缺少 V1 因子库，未执行重试。")
            return False
        manifest = DataSnapshotStore(self.factor_store.database).get_manifest(snapshot_id)
        if manifest is None:
            self._set_status("当前候选缺少原始不可变快照，未执行重试。")
            return False

        def task():
            return self._run_screening_agents_for_rows(
                rows,
                manifest,
                execution_mode=execution_mode,
                ai_config=config,
            )

        def done(payload) -> None:
            warnings, _packages = apply_agent_retry_payload(payload)
            self._set_status(
                "五 Agent 重试完成；候选资格、排序和人工观察状态未改变。"
                + ("；" + "；".join(([persisted_queue_warning] if persisted_queue_warning else []) + warnings[:3]) if (persisted_queue_warning or warnings) else "")
            )

        return self._run_background(
            "正在使用同一快照重试失败或未完成的五 Agent 批次...",
            task,
            done,
            task_key="agent_retry",
            duplicate_text="五 Agent 重试仍在后台运行。",
        )

    def _screen_candidate_row(self, order: int, candidate, *, report: ScreenReport | None = None) -> dict[str, Any]:
        quote = candidate.quote
        metrics = candidate.metrics
        technical = candidate.technical
        report = report or self.current_screen_report
        data_snapshot_id = str(getattr(candidate, "data_snapshot_id", "") or getattr(report, "data_snapshot_id", "") or "")
        data_asof = str(getattr(report, "snapshot_asof", "") or "")
        data_source = str(getattr(report, "data_source", "") or "")
        missing_fields = list(getattr(candidate, "missing_fields", []) or [])
        stale_fields = list(getattr(candidate, "stale_fields", []) or [])
        # CandidateState and EvidenceSummary both use the stable stock code as
        # the fallback identity; the immutable candidate_digest remains the
        # snapshot-specific identity shown and exported separately.
        candidate_id = str(quote.code or "").zfill(6)
        return {
            "order": order,
            "candidate_id": candidate_id,
            "code": quote.code,
            "name": quote.name,
            "lens_ids": list(getattr(candidate, "lens_ids", ()) or ()),
            "lens_status": "、".join(getattr(candidate, "lens_ids", ()) or ("-",)),
            "eligibility_status": getattr(candidate, "eligibility_status", ""),
            "candidate_status": "candidate" if getattr(candidate, "eligibility_status", "") == "eligible" else getattr(candidate, "eligibility_status", ""),
            "price": quote.price,
            "pe": quote.pe,
            "roe": metrics.roe,
            "box_score": getattr(technical, "box_score", None),
            "industry_strength": getattr(quote, "industry_strength", None),
            "reason": "、".join(candidate.reasons[:4]),
            "data_status": getattr(candidate, "data_status", "verified"),
            "data_asof": data_asof,
            "data_source": data_source,
            "data_time_source": f"{data_asof or '-'} / {data_source or '-'}",
            "mode": getattr(candidate, "mode", "formal") or "formal",
            "exploratory_only": bool(getattr(candidate, "exploratory_only", False)),
            "board_profile_id": getattr(candidate, "board_profile_id", "") or "",
            "universe_id": getattr(candidate, "universe_id", "") or "",
            "universe_version": getattr(candidate, "universe_version", "") or "",
            "priority_tier": getattr(candidate, "priority_tier", "") or "",
            "priority_explanation": list(getattr(candidate, "priority_explanation", []) or []),
            "selection_policy_version": getattr(candidate, "selection_policy_version", "") or "",
            "missing_fields": missing_fields,
            "reason_codes": list(getattr(candidate, "reason_codes", []) or []),
            "stale_fields": stale_fields,
            "data_gaps": list(dict.fromkeys([*missing_fields, *stale_fields])),
            "next_checks": list(dict.fromkeys([*missing_fields, *stale_fields])),
            "data_snapshot_id": data_snapshot_id,
            "candidate_digest": getattr(candidate, "candidate_digest", ""),
            "fact_sources": dict(getattr(candidate, "fact_sources", {}) or {}),
            "lens_evaluations": dict(getattr(candidate, "lens_evaluations", {}) or {}),
            "agent_status": "未运行",
            "coverage": "未运行",
            "supporting_evidence": [],
            "opposing_evidence": [],
            "conflicts": [],
        }

    def _apply_screener_workflow_result(self, result: WorkflowResult) -> None:
        if result.rows and "code" in result.rows[0]:
            self.current_screen_rows = list(result.rows)
            snapshot_id = str(result.artifacts.get("data_snapshot_id") or "")
            self._refresh_candidate_states(snapshot_id=snapshot_id)
            self._fill_screener_results(self.current_screen_rows)
        elif not result.rows and not self.current_screen_rows:
            self.current_candidate_states = {}
            if hasattr(self, "screener_candidate_detail"):
                self.screener_candidate_detail.setPlainText(
                    "当前没有候选结果。正式模式下请先通过数据质量闸门；复核/探索结果会明确标识研究边界。"
                )
        warning_text = "\n".join(f"- {item}" for item in result.warnings[:6])
        artifact_text = "\n".join(f"{key}: {value}" for key, value in result.artifacts.items())
        detail_parts = [result.message]
        if warning_text:
            detail_parts.append("\n提示：\n" + warning_text)
        if artifact_text:
            detail_parts.append("\n产物：\n" + artifact_text)
        if hasattr(self, "screener_summary_box"):
            existing = self.screener_summary_box.toPlainText()
            base = existing.split("\n\n执行结果：", 1)[0] if existing else ""
            self.screener_summary_box.setPlainText((base + "\n\n执行结果：\n" + "\n".join(detail_parts)).strip())
        self._update_builtin_screener_status(
            data_source=str(result.artifacts.get("data_source") or ("未就绪" if result.status in {"failed", "skipped"} else "待运行")),
            snapshot_asof=str(result.artifacts.get("snapshot_asof") or "未获取"),
            status=str(
                result.artifacts.get("screening_status")
                or ("blocked" if "阻断" in result.message else result.status)
            ),
        )
        self._set_status(result.message)
        self.refresh_screener_history()
        self._refresh_screener_controls()

    def _refresh_candidate_states(self, *, snapshot_id: str = "") -> None:
        if not self.current_screen_rows:
            self.current_candidate_states = {}
            return
        resolved_snapshot_id = snapshot_id or str(self.current_screen_rows[0].get("data_snapshot_id") or "")
        if not resolved_snapshot_id:
            resolved_snapshot_id = stable_hash(
                {"mode": self._selected_screening_mode(), "codes": [row.get("code") for row in self.current_screen_rows]}
            )
        records = [
            {
                "candidate_id": row.get("candidate_id") or row.get("code"),
                "code": row.get("code"),
                "eligibility_status": row.get("eligibility_status") or "eligible",
                "lens_ids": row.get("lens_ids") or (),
                "candidate_digest": row.get("candidate_digest") or "",
                "reason_codes": row.get("reason_codes") or (),
                "missing_fields": row.get("missing_fields") or (),
                "stale_fields": row.get("stale_fields") or (),
            }
            for row in self.current_screen_rows
        ]
        if self.candidate_state_service is None:
            self.current_candidate_states = {}
            for row in self.current_screen_rows:
                row["state_error"] = self.candidate_state_error or "候选状态存储不可用"
                row["candidate_status"] = (
                    row.get("eligibility_status")
                    if row.get("eligibility_status") not in {"eligible", ""}
                    else "candidate"
                )
            return
        try:
            evidence_summaries = tuple(
                summary
                for aggregation in self.current_evidence_aggregations
                for summary in (getattr(aggregation, "summaries", ()) or ())
            )
            if not evidence_summaries and self.current_evidence_aggregation is not None:
                evidence_summaries = tuple(
                    getattr(self.current_evidence_aggregation, "summaries", ()) or ()
                )
            batch = self.candidate_state_service.resolve_batch(
                records,
                evidence_summaries,
                snapshot_id=resolved_snapshot_id,
            )
        except (CandidateStateError, ValueError) as exc:
            self.current_candidate_states = {}
            for row in self.current_screen_rows:
                row["state_error"] = str(exc)
            return
        self.current_candidate_states = {state.candidate_id: state.as_dict() for state in batch.states}
        for row in self.current_screen_rows:
            state = self.current_candidate_states.get(str(row.get("candidate_id") or row.get("code")))
            if state is None:
                continue
            row["candidate_status"] = state["status"]
            row["state_reason_codes"] = list(state.get("reason_codes") or ())
            row["state_snapshot_id"] = state.get("snapshot_id", resolved_snapshot_id)
            row["state_updated_at"] = state.get("updated_at", "")

    @staticmethod
    def _evidence_claim_text(claim: dict[str, Any]) -> str:
        claim_id = str(claim.get("claim_id") or "-")
        text = str(claim.get("text") or claim.get("claim") or "-").strip()
        refs = ", ".join(str(item) for item in (claim.get("evidence_refs") or ()))
        return f"{claim_id}: {text}" + (f"（事实：{refs}）" if refs else "")

    def _apply_agent_evidence_to_candidate(
        self,
        code: str,
        formal_result: AgentOrchestrationResult | None,
        evidence_aggregation: Any | None,
        *,
        base_data_gaps: list[str] | tuple[str, ...] = (),
        research_mode: str = "formal",
    ) -> None:
        if formal_result is None:
            return
        normalized_code = str(code or "").zfill(6)
        summary = None
        for item in getattr(evidence_aggregation, "summaries", ()) or ():
            if str(getattr(item, "code", "")).zfill(6) == normalized_code:
                summary = item
                break
        summary_payload = summary.as_dict() if summary is not None else {}
        supporting_ids = set(summary_payload.get("supporting_claim_ids") or ())
        opposing_ids = set(summary_payload.get("opposing_claim_ids") or ())
        claims_by_id: dict[str, dict[str, Any]] = {}
        missing_evidence: list[str] = []
        unresolved_questions: list[str] = []
        risk_flags: list[str] = []
        coverage_items: list[str] = []
        for execution in formal_result.executions:
            result = execution.result
            if result is None:
                coverage_items.append(f"{execution.agent_id}: 无结果")
                continue
            coverage_items.append(
                f"{execution.agent_id}: {json.dumps(result.coverage, ensure_ascii=False, sort_keys=True, default=str)}"
            )
            missing_evidence.extend(result.missing_evidence)
            unresolved_questions.extend(result.unresolved_questions)
            risk_flags.extend(result.risk_flags)
            for claim in (*result.claims, *result.counter_claims):
                claim_id = str(claim.get("claim_id") or "").strip()
                if claim_id:
                    claims_by_id[claim_id] = dict(claim)
        supporting = [claims_by_id[item] for item in sorted(supporting_ids) if item in claims_by_id]
        opposing = [claims_by_id[item] for item in sorted(opposing_ids) if item in claims_by_id]
        conflicts = [dict(item) for item in (summary_payload.get("conflicts") or ())]
        data_gaps = list(dict.fromkeys([*base_data_gaps, *missing_evidence]))
        next_checks = list(dict.fromkeys([*unresolved_questions, *data_gaps]))
        view = {
            "research_mode": research_mode,
            "research_mode_label": SCREENING_MODE_LABELS.get(research_mode, research_mode),
            "agent_status": format_agent_statuses(formal_result).replace("\n", "；") or "未运行",
            "coverage": "；".join(coverage_items) or "未运行",
            "supporting_evidence": supporting,
            "opposing_evidence": opposing,
            "conflicts": conflicts,
            "risk_flags": list(dict.fromkeys(risk_flags)),
            "data_gaps": data_gaps,
            "next_checks": next_checks,
            "f_layer_status": str(summary_payload.get("status") or "failed"),
            "agent_run_id": formal_result.run_id,
            "evidence_summary_hash": getattr(evidence_aggregation, "summary_hash", "") if evidence_aggregation else "",
        }
        self.current_candidate_evidence[normalized_code] = view
        for row in self.current_screen_rows:
            if str(row.get("code") or "").zfill(6) == normalized_code:
                row.update(view)
        if self.current_screen_rows:
            self._refresh_candidate_states(snapshot_id=str(self.current_screen_rows[0].get("data_snapshot_id") or ""))
            self._fill_screener_results(self.current_screen_rows)

    def _show_selected_candidate_detail(self) -> None:
        if not hasattr(self, "screener_candidate_detail"):
            return
        row_index = self.screener_result_table.currentRow()
        if row_index < 0 or row_index >= len(self.current_screen_rows):
            self.screener_candidate_detail.setPlainText("选中候选后显示完整证据、数据来源、冲突、缺口和后续核查条件。")
            self._refresh_screener_controls()
            return
        row = self.current_screen_rows[row_index]
        mode = row.get("research_mode_label") or SCREENING_MODE_LABELS.get(
            self._selected_screening_mode(), self._selected_screening_mode()
        )
        fact_sources = row.get("fact_sources") or {}
        lines = [
            f"{row.get('name', '-')}（{row.get('code', '-')}）",
            f"研究模式：{mode}",
            f"板块规则：{row.get('board_profile_id') or '待绑定 BoardProfile（正式策略不隐式复用主板）'}",
            f"Candidate Lens：{'、'.join(row.get('lens_ids') or ['-'])}",
            f"数据质量：{row.get('data_status') or '-'}；优先级：{row.get('priority_tier') or '-'}；规则版本：{row.get('selection_policy_version') or 'candidate_lens.v1'}",
            f"优先级解释：{'；'.join(row.get('priority_explanation') or ['按固定词典序和代码稳定排序'])}",
            f"确定性资格：{row.get('eligibility_status', '-')}",
            f"当前候选状态：{row.get('candidate_status', '-')}",
            f"状态理由码：{'、'.join(row.get('state_reason_codes') or ['-'])}",
            f"进入候选理由：{row.get('reason') or '-'}",
            "确定性指标："
            f"PE={fmt_number(row.get('pe'))}；"
            f"ROE={fmt_number(row.get('roe'), '%')}；箱体={fmt_number(row.get('box_score'))}；"
            f"行业强度={fmt_number(row.get('industry_strength'))}",
            f"理由码：{'、'.join(row.get('reason_codes') or ['-'])}",
            f"数据时间：{row.get('data_asof') or '-'}；来源：{row.get('data_source') or '-'}；快照：{row.get('data_snapshot_id') or '-'}",
            f"事实来源：{json.dumps(fact_sources, ensure_ascii=False, sort_keys=True) if fact_sources else '-'}",
            f"Agent 状态：{row.get('agent_status') or '-'}",
            f"数据覆盖度：{row.get('coverage') or '-'}",
            "支持证据：",
        ]
        lines.extend(
            f"- {self._evidence_claim_text(item)}"
            for item in row.get("supporting_evidence") or ()
        )
        if not row.get("supporting_evidence"):
            lines.append("- 暂无已校验证据；Agent 未运行或没有通过本地引用校验的 claim。")
        lines.append("反向证据：")
        lines.extend(f"- {self._evidence_claim_text(item)}" for item in row.get("opposing_evidence") or ())
        if not row.get("opposing_evidence"):
            lines.append("- 未观察到已校验反向证据；不代表不存在反向事实。")
        lines.append("Agent 风险旗标：")
        lines.extend(f"- {item}" for item in row.get("risk_flags") or ())
        if not row.get("risk_flags"):
            lines.append("- 无")
        lines.append("冲突：")
        lines.extend(
            f"- {item.get('reason', item) if isinstance(item, dict) else item}"
            for item in row.get("conflicts") or ()
        )
        if not row.get("conflicts"):
            lines.append("- 无已识别冲突")
        lines.append(f"数据缺口：{'、'.join(row.get('data_gaps') or ['无'])}")
        lines.append(f"后续核查条件：{'；'.join(row.get('next_checks') or ['暂无'])}")
        if row.get("state_error"):
            lines.append(f"候选状态诊断：{row['state_error']}")
        self.screener_candidate_detail.setPlainText("\n".join(lines))
        self._refresh_screener_controls()

    def _fill_screener_results(self, rows: list[dict[str, Any]]) -> None:
        if not hasattr(self, "screener_result_table"):
            return
        if not rows:
            self.screener_result_table.setRowCount(1)
            values = ("-", "-", "暂无结果", "-", "-", "-", "-", "-", "-", "-", "-", "-", "-", "解析并执行后显示候选证据")
            for col, value in enumerate(values):
                self.screener_result_table.setItem(0, col, QTableWidgetItem(str(value)))
            if hasattr(self, "screener_add_watch_button"):
                self.screener_add_watch_button.setEnabled(False)
            if hasattr(self, "screener_research_button"):
                self.screener_research_button.setEnabled(False)
            if hasattr(self, "screener_candidate_detail"):
                self.screener_candidate_detail.setPlainText(
                    "选中候选后显示完整证据、数据来源、冲突、缺口和后续核查条件。"
                )
            return
        self.screener_result_table.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            values = (
                row.get("order", "-"),
                row.get("code", "-"),
                row.get("name", "-"),
                "、".join(row.get("lens_ids") or ["-"]),
                row.get("eligibility_status", "-"),
                row.get("candidate_status", "-"),
                row.get("data_time_source", "-"),
                row.get("agent_status", "-"),
                row.get("coverage", "-"),
                "；".join(self._evidence_claim_text(item) for item in (row.get("supporting_evidence") or ())) or "-",
                "；".join(self._evidence_claim_text(item) for item in (row.get("opposing_evidence") or ())) or "-",
                "；".join(str(item.get("reason", item)) if isinstance(item, dict) else str(item) for item in (row.get("conflicts") or ())) or "-",
                "、".join(row.get("data_gaps") or ["-"]),
                "；".join(row.get("next_checks") or ["-"]),
            )
            for col, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                if col == 1:
                    item.setData(Qt.UserRole, row)
                self.screener_result_table.setItem(row_index, col, item)
        self.screener_result_table.selectRow(0)
        self._show_selected_candidate_detail()

    def add_selected_candidate_to_watch(self) -> None:
        if self._selected_screening_mode() != "formal":
            self._set_status("只有 formal 模式的候选可以加入正式观察；review_only/exploratory 结果不会进入正式观察样本。")
            return
        row_index = self.screener_result_table.currentRow() if hasattr(self, "screener_result_table") else -1
        if row_index < 0 or row_index >= len(self.current_screen_rows):
            self._set_status("请先选中一个 candidate 候选。")
            return
        row = self.current_screen_rows[row_index]
        if row.get("research_mode", "formal") != "formal":
            self._set_status("当前结果实际属于 review_only/exploratory，不能加入正式观察。")
            return
        if row.get("candidate_status") != "candidate":
            self._set_status("只有当前状态为 candidate 的候选可以人工加入观察。")
            return
        if self.candidate_state_service is None:
            self._set_status(f"候选状态不可用：{self.candidate_state_error or '请检查数据目录'}")
            return
        try:
            state, _transition = self.candidate_state_service.add_to_watch(
                str(row.get("candidate_id") or row.get("code") or ""),
                actor=f"human:{getpass.getuser() or 'user'}",
                reason_codes=("MANUAL_ADD_WATCH",),
                snapshot_id=str(row.get("state_snapshot_id") or row.get("data_snapshot_id") or ""),
            )
        except (CandidateStateError, ValueError) as exc:
            self._set_status(f"人工加入观察失败：{exc}")
            return
        row["candidate_status"] = state.status
        row["state_reason_codes"] = list(state.reason_codes)
        row["state_updated_at"] = state.updated_at
        self._fill_screener_results(self.current_screen_rows)
        self._set_status(f"已由人工加入观察：{row.get('name', row.get('code', '-'))}；Agent 未获得状态变更权限。")

    def open_selected_candidate_research(self) -> None:
        row_index = self.screener_result_table.currentRow() if hasattr(self, "screener_result_table") else -1
        if row_index < 0 or row_index >= len(self.current_screen_rows):
            self._set_status("请先选中一个候选。")
            return
        self._formal_candidate_research_context = True
        self._refresh_observer_save_buttons()
        self._open_stock_from_row(self.current_screen_rows[row_index])
        self._set_status("已打开个股研究页；可在当前不可变快照上整理五 Agent 证据。")

    def save_screener_strategy(self) -> WorkflowResult:
        if self.current_query_spec is None:
            result = self.parse_screener_query()
            if result.status != "success":
                return result
        if self.strategy_store is None:
            result = WorkflowResult("failed", "策略库不可用，请检查数据目录。")
            self._apply_screener_workflow_result(result)
            return result
        assert self.current_query_spec is not None
        text = self.screener_query_input.text().strip()
        name_seed = "".join(ch for ch in text if ch.strip())[:24] or "智能筛选"
        supplied_name = self.screener_strategy_name_input.text().strip() if hasattr(self, "screener_strategy_name_input") else ""
        name = (supplied_name or f"Qt策略 {name_seed} {datetime.now().strftime('%m%d%H%M')}")[:80]
        template = self.current_template_contract
        preset = self.strategy_store.save_preset(
            name,
            text,
            self.current_query_spec,
            summary=query_spec_summary(self.current_query_spec),
            parse_source="Qt本地解析",
            change_note="Qt工作台保存",
            template_id=template.template_id if template else "",
            template_version=template.template_version if template else "",
            universe_id=template.universe_id if template else "a_share_all",
            config_hash=condition_hash(self.current_query_spec),
        )
        self.current_strategy_preset_id = preset.id
        if hasattr(self, "screener_strategy_name_input"):
            self.screener_strategy_name_input.setText(preset.name)
        self.refresh_screener_strategies(preset.id)
        result = WorkflowResult("success", f"已保存策略：{preset.name}", artifacts={"preset_id": str(preset.id)})
        self._apply_screener_workflow_result(result)
        return result

    def _selected_screener_run_id(self) -> int | None:
        if not hasattr(self, "screener_history_table"):
            return None
        row = self.screener_history_table.currentRow()
        item = self.screener_history_table.item(row, 0) if row >= 0 else None
        value = item.data(Qt.UserRole) if item is not None else None
        return int(value) if value not in (None, "") else None

    def _selected_screener_comparison(self):
        run_id = self._selected_screener_run_id()
        if self.strategy_store is None or run_id is None:
            raise ValueError("请先在最近执行中选择一条记录。")
        return self.strategy_store.compare_runs(run_id)

    def compare_selected_screener_run(self) -> WorkflowResult:
        try:
            comparison = self._selected_screener_comparison()
        except Exception as exc:
            result = WorkflowResult("skipped", f"无法比较：{exc}")
            self._apply_screener_workflow_result(result)
            return result
        self._current_screener_comparison = comparison
        summary = run_comparison_summary(comparison)
        result = WorkflowResult(
            "success",
            f"已对比执行 #{comparison.current_run.id} 与 #{comparison.previous_run.id}：新增 {summary.added_count}，消失 {summary.removed_count}，确定性顺序变化 {summary.rank_changed_count}，指标变化 {summary.metric_changed_count}。",
            artifacts={"current_run_id": str(comparison.current_run.id), "previous_run_id": str(comparison.previous_run.id)},
        )
        self._apply_screener_workflow_result(result)
        return result

    def export_selected_screener_comparison_csv(self) -> WorkflowResult:
        try:
            comparison = self._selected_screener_comparison()
            rows = run_comparison_csv_rows(comparison)
            path = self._screener_export_dir() / f"qt_screener_compare_{comparison.current_run.id}_vs_{comparison.previous_run.id}.csv"
            ReviewWorkflowService.write_csv(path, rows)
        except Exception as exc:
            result = WorkflowResult("failed", f"导出筛选比较 CSV 失败：{exc}")
            self._apply_screener_workflow_result(result)
            return result
        result = WorkflowResult("success", f"筛选比较 CSV 已导出：{path}", artifacts={"csv": str(path)})
        self._apply_screener_workflow_result(result)
        return result

    def refresh_screener_history(self) -> WorkflowResult:
        if not hasattr(self, "screener_history_table"):
            return WorkflowResult("skipped", "筛选历史表尚未初始化。")
        if self.strategy_store is None:
            self.screener_history_table.setRowCount(1)
            for col, value in enumerate(("-", "策略库不可用", "-", "-", self.v1_error_message or "请检查数据目录")):
                self.screener_history_table.setItem(0, col, QTableWidgetItem(str(value)))
            return WorkflowResult("failed", "策略库不可用。")
        runs = self.strategy_store.list_recent_runs(limit=8)
        if not runs:
            self.screener_history_table.setRowCount(1)
            for col, value in enumerate(("-", "暂无执行记录", "-", "-", "执行筛选后会自动记录。")):
                self.screener_history_table.setItem(0, col, QTableWidgetItem(str(value)))
            return WorkflowResult("skipped", "暂无筛选历史。")
        self.screener_history_table.setRowCount(len(runs))
        for row_index, run in enumerate(runs):
            values = (
                run.created_at[:16].replace("T", " "),
                run.data_source or run.source_label,
                run.matched,
                run.scanned,
                run.summary,
            )
            for col, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                if col == 0:
                    item.setData(Qt.UserRole, run.id)
                self.screener_history_table.setItem(row_index, col, item)
        self.screener_history_table.selectRow(0)
        return WorkflowResult("success", f"已加载 {len(runs)} 条筛选历史。")

    def _screener_export_dir(self) -> Path:
        path = self.data_dir / "exports"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def export_screener_csv(self) -> WorkflowResult:
        if not self.current_screen_rows:
            result = WorkflowResult("skipped", "还没有可导出的筛选结果。")
            self._apply_screener_workflow_result(result)
            return result
        path = self._screener_export_dir() / f"qt_screener_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        columns = [
            "order", "candidate_id", "code", "name", "lens_status", "lens_ids",
            "eligibility_status", "candidate_status", "research_mode", "research_mode_label",
            "pe", "roe", "box_score", "industry_strength", "reason",
            "data_status", "data_asof", "data_source", "data_time_source", "missing_fields",
            "stale_fields", "data_gaps", "reason_codes", "next_checks", "data_snapshot_id",
            "candidate_digest", "fact_sources", "agent_status", "coverage",
            "supporting_evidence", "opposing_evidence", "conflicts", "risk_flags", "f_layer_status",
            "agent_run_id", "evidence_summary_hash", "state_reason_codes", "state_snapshot_id",
            "state_updated_at",
        ]
        export_rows = [
            {
                key: value
                for key, value in row.items()
                if key not in {"price", "entry_price", "fail_price", "target_price", "stop_price", "position_hint"}
            }
            for row in self.current_screen_rows
        ]
        with path.open("w", encoding="utf-8-sig", newline="") as file:
            writer = csv.DictWriter(file, fieldnames=columns, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(export_rows)
        result = WorkflowResult("success", f"CSV 已导出：{path}", artifacts={"csv": str(path)})
        self._apply_screener_workflow_result(result)
        return result

    def export_screener_json(self) -> WorkflowResult:
        if not self.current_screen_rows or self.current_query_spec is None:
            result = WorkflowResult("skipped", "还没有可导出的筛选结果。")
            self._apply_screener_workflow_result(result)
            return result
        path = self._screener_export_dir() / f"qt_screener_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        payload = {
            "format": "stocklens_qt_screener_result",
            "version": 1,
            "strategy": strategy_export_payload(
                "Qt智能筛选",
                self.screener_query_input.text().strip(),
                self.current_query_spec,
                parse_source="Qt本地解析",
            ),
            "query_spec": query_spec_to_dict(self.current_query_spec),
            "run_id": self.current_screener_run_id,
            "rows": [
                {
                    key: value
                    for key, value in row.items()
                    if key not in {"price", "entry_price", "fail_price", "target_price", "stop_price", "position_hint"}
                }
                for row in self.current_screen_rows
            ],
            "disclaimer": DISCLAIMER,
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        result = WorkflowResult("success", f"JSON 已导出：{path}", artifacts={"json": str(path)})
        self._apply_screener_workflow_result(result)
        return result

    def _open_stock_from_row(self, row: dict[str, Any]) -> None:
        code = str(row.get("code") or "").strip()
        if not code:
            self._set_status("这行没有可打开的股票代码")
            return
        self._navigate("research")
        self.current_quote = StockQuote(
            code=code.zfill(6),
            name=str(row.get("name") or code),
            price=row.get("price"),
            change_pct=row.get("change_pct"),
        )
        self.load_stock()

    def _generate_observation_from_row(self, row: dict[str, Any]) -> None:
        raw_code = str(row.get("code") or "").strip()
        self._pending_generate_observation_code = raw_code.zfill(6) if raw_code else None
        self._open_stock_from_row(row)
        self._set_status("已打开个股研究页；行情加载完成后会自动生成观察记录。")

    def _remove_watch_code(self, code: str) -> None:
        raw_code = str(code or "").strip()
        if not raw_code:
            return
        normalized = raw_code.zfill(6)
        self.cache.remove_watch(normalized)
        self._load_watchlist()
        self._refresh_dashboard_snapshot()
        self._set_status(f"已移出自选：{normalized}")

    def _refresh_dashboard_data(self) -> None:
        self._start_data_refresh("manual")

    def _dashboard_stats_from_cache(self) -> dict[str, Any]:
        now = datetime.now()
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        cache_items = self.cache.list_cache_metadata(limit=1000)
        spot_record = self.cache.get_json("spot_quotes")
        spot_updated_at = spot_record.updated_at if spot_record is not None else None
        watch_record = self.cache.get_json("watch_quotes")
        watch_updated_at = watch_record.updated_at if watch_record is not None else None
        watches = self.cache.list_watch()
        tasks = self.review_service.list_tracking_tasks({"include_closed": True})
        alerts_all = self.cache.list_price_alerts(enabled_only=False)
        alerts_enabled = [alert for alert in alerts_all if alert.enabled]
        history_today = self.cache.list_alert_history(limit=100, since=today_start)
        recent_history = self.cache.list_alert_history(limit=8)
        quotes_by_code = merge_quote_cache_payloads(
            [
                spot_record.payload if spot_record is not None else None,
                watch_record.payload if watch_record is not None else None,
            ],
            StockQuote,
        )
        return {
            "tracking": summarize_tracking(tasks),
            "alerts": summarize_alerts(alerts_enabled, history_today),
            "health_rows": data_health_rows(cache_items, now=now),
            "watch_rows": build_watch_rows(
                watches,
                quotes_by_code,
                tasks,
                alerts_enabled,
                updated_at=watch_updated_at or spot_updated_at,
            ),
            "alert_rows": [
                {
                    "time": item.created_at.strftime("%m-%d %H:%M"),
                    "stock": f"{item.name} {item.code}",
                    "message": item.message,
                }
                for item in recent_history
            ],
        }

    def _refresh_dashboard_snapshot(self) -> None:
        if not hasattr(self, "home_page"):
            return
        stats = self._dashboard_stats_from_cache()
        self.home_page.apply_stats(stats)
        if hasattr(self, "watchlist_page"):
            self.watchlist_page.apply_rows(stats.get("watch_rows") or [])
        self._fill_data_health_detail(stats.get("health_rows") or [])
        if "data_refresh" not in self._running_tasks and self._last_refresh_summary is None:
            stale_count = sum(1 for _name, status, _age in stats.get("health_rows", []) if status != "正常")
            self.data_status_pill.setText("数据状态 正常" if stale_count == 0 else f"数据状态 待复核 {stale_count}项")
        self._refresh_top_ai_pill()

    def _fill_data_health_detail(self, rows: list[tuple[str, ...]]) -> None:
        if not hasattr(self, "data_health_detail_table"):
            return
        feature_rows: list[tuple[str, ...]] = []
        for kind, label in (
            ("quotes", "功能行情"),
            ("market_context", "市场上下文"),
            ("factor_snapshot", "因子筛选快照"),
            ("pit_incremental", "PIT 日增量"),
        ):
            try:
                readiness = self.data_readiness.inspect(
                    DataRequirement(feature="数据中心", kinds=(kind,))
                )
                status = "正常" if readiness.status == "ready" else "待刷新/阻断"
                asof = readiness.asof_by_kind.get(kind, "-") or "-"
                identity = "/".join(item for item in (readiness.snapshot_id, readiness.refresh_event_id) if item) or "-"
                ttl = self.data_readiness.policy.ttl_for(kind)
                next_check = f"{ttl.days}交易日" if hasattr(ttl, "days") and not isinstance(ttl, timedelta) else f"{int(ttl.total_seconds())}秒"
                if readiness.errors:
                    status = f"{status}：{readiness.errors[0]}"
                feature_rows.append((label, status, asof, identity, next_check))
            except Exception as exc:
                feature_rows.append((label, "暂不可用", "-", "-", str(exc)))
        rows = [*rows, *feature_rows]
        if not rows:
            rows = [("缓存", "暂不可用", "尚未产生缓存记录", "-", "-" )]
        self.data_health_detail_table.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            for col in range(self.data_health_detail_table.columnCount()):
                value = row[col] if col < len(row) else "-"
                item = _health_status_item(str(value)) if col == 1 else QTableWidgetItem(str(value))
                self.data_health_detail_table.setItem(row_index, col, item)

    def _fill_refresh_stage_table(self) -> None:
        if not hasattr(self, "refresh_stage_table"):
            return
        rows = [self._refresh_stage_results[stage] for stage in REFRESH_STAGE_ORDER if stage in self._refresh_stage_results]
        if not rows:
            self.refresh_stage_table.setRowCount(1)
            self.refresh_stage_table.setItem(0, 0, QTableWidgetItem("刷新阶段"))
            self.refresh_stage_table.setItem(0, 1, _health_status_item("跳过"))
            self.refresh_stage_table.setItem(0, 2, QTableWidgetItem("-"))
            self.refresh_stage_table.setItem(0, 3, QTableWidgetItem("尚未运行"))
            self.refresh_stage_table.setItem(0, 4, QTableWidgetItem("打开软件会自动刷新自选核心数据；也可手动刷新。"))
            return
        self.refresh_stage_table.setRowCount(len(rows))
        for row_index, step in enumerate(rows):
            status_text = REFRESH_STATUS_LABELS.get(step.status, step.status)
            progress_text = f"{step.done}/{step.total}" if step.total else "-"
            result_text = f"实时 {step.fresh} · 缓存 {step.cache} · 失败 {step.failed}"
            detail = step.message
            if step.errors:
                detail = f"{detail}；{step.errors[0]}"
            values = (
                REFRESH_STAGE_LABELS.get(step.stage, step.stage),
                status_text,
                progress_text,
                result_text,
                detail,
            )
            for col, value in enumerate(values):
                item = _health_status_item(str(value)) if col == 1 else QTableWidgetItem(str(value))
                self.refresh_stage_table.setItem(row_index, col, item)

    def _fill_fetch_log_table(self) -> None:
        if not hasattr(self, "fetch_log_table"):
            return
        logs = self.cache.list_fetch_logs(limit=12)
        if not logs:
            self.fetch_log_table.setRowCount(1)
            self.fetch_log_table.setItem(0, 0, QTableWidgetItem("-"))
            self.fetch_log_table.setItem(0, 1, QTableWidgetItem("暂无抓取日志"))
            self.fetch_log_table.setItem(0, 2, QTableWidgetItem("-"))
            self.fetch_log_table.setItem(0, 3, _health_status_item("跳过"))
            self.fetch_log_table.setItem(0, 4, QTableWidgetItem("运行刷新后会显示实时源、缓存命中和失败原因。"))
            return
        self.fetch_log_table.setRowCount(len(logs))
        for row_index, log in enumerate(logs):
            status_text = {
                "success": "成功",
                "cache": "缓存",
                "failed": "失败",
                "skipped": "跳过",
                "info": "提示",
            }.get(log.status, log.status)
            values = (
                log.created_at.strftime("%m-%d %H:%M"),
                log.action,
                log.target or "-",
                status_text,
                log.message,
            )
            for col, value in enumerate(values):
                item = _health_status_item(str(value)) if col == 3 else QTableWidgetItem(str(value))
                self.fetch_log_table.setItem(row_index, col, item)

    def _set_refresh_controls_enabled(self, enabled: bool) -> None:
        if hasattr(self, "data_status_pill"):
            self.data_status_pill.setEnabled(enabled)
        if hasattr(self, "refresh_watch_data_button"):
            self.refresh_watch_data_button.setEnabled(enabled)

    def _auto_data_refresh_interval_seconds(self) -> int:
        if not hasattr(self, "auto_data_refresh_interval_combo"):
            return 30
        try:
            value = int(self.auto_data_refresh_interval_combo.currentData() or 30)
        except (TypeError, ValueError):
            return 30
        return value if value in AUTO_DATA_REFRESH_INTERVALS else 30

    def _set_auto_data_refresh_enabled(self, enabled: bool) -> None:
        if not hasattr(self, "auto_data_refresh_check"):
            return
        self.cache.set_setting("auto_data_refresh_enabled", "1" if enabled else "0")
        if enabled:
            self._schedule_auto_data_refresh(delay_from_now=True)
            self._set_status(
                f"已开启自动行情刷新：每 {self._auto_data_refresh_interval_seconds()} 秒尝试更新。"
            )
        else:
            self._auto_data_refresh_timer.stop()
            self._set_status("已关闭自动行情刷新；仍可使用“刷新自选数据”手动更新。")

    def _set_feature_auto_refresh_enabled(self, enabled: bool) -> None:
        if not hasattr(self, "feature_auto_refresh_check"):
            return
        self.cache.set_setting("feature_auto_refresh_enabled", "1" if enabled else "0")
        if enabled:
            self._set_status("已开启功能按需自动刷新：筛选、AI、研究和复盘会先检查依赖数据。")
        else:
            self._set_status("已关闭功能按需自动刷新；过期数据不会静默使用，请在数据中心手动刷新。")

    def _save_feature_ttl(self, setting_key: str) -> None:
        """Validate a data-readiness TTL before persisting it to the cache."""

        input_box = getattr(self, "feature_ttl_inputs", {}).get(setting_key)
        spec = next((item for item in FEATURE_TTL_CONTROL_SPECS if item[0] == setting_key), None)
        if input_box is None or spec is None:
            return
        _key, label, minimum, maximum, unit = spec
        try:
            value = int(input_box.text().strip())
        except (TypeError, ValueError):
            value = -1
        if not minimum <= value <= maximum:
            default = DataFreshnessPolicy({}).seconds_for(setting_key)
            try:
                previous = int(self.cache.get_setting(setting_key, str(default)))
            except (TypeError, ValueError):
                previous = default
            input_box.setText(str(previous if minimum <= previous <= maximum else default))
            self._set_status(f"{label} TTL 未保存：请输入 {minimum}–{maximum} {unit}。")
            return
        self.cache.set_setting(setting_key, str(value))
        self._set_status(f"已保存{label} TTL：{value} {unit}；下一次功能数据检查立即生效。")
        self._refresh_dashboard_snapshot()

    def _set_auto_data_refresh_interval(self) -> None:
        if not hasattr(self, "auto_data_refresh_interval_combo"):
            return
        seconds = self._auto_data_refresh_interval_seconds()
        self.cache.set_setting("auto_data_refresh_interval_seconds", str(seconds))
        if self.auto_data_refresh_check.isChecked():
            self._schedule_auto_data_refresh(delay_from_now=True)
            self._set_status(f"自动行情刷新间隔已调整为 {seconds} 秒。")

    def _schedule_auto_data_refresh(self, *, delay_from_now: bool = False) -> None:
        if not hasattr(self, "_auto_data_refresh_timer"):
            return
        self._auto_data_refresh_timer.stop()
        if self._window_closing or not self.auto_data_refresh_check.isChecked():
            return
        seconds = self._auto_data_refresh_interval_seconds()
        try:
            if delay_from_now:
                next_check_at = as_shanghai(datetime.now()) + timedelta(seconds=seconds)
            else:
                next_check_at = self.alert_monitor.trading_clock.next_check_at(datetime.now(), seconds)
            delay_ms = max(
                1_000,
                int((next_check_at - datetime.now(next_check_at.tzinfo)).total_seconds() * 1000),
            )
        except Exception:
            delay_ms = seconds * 1000
        self._auto_data_refresh_timer.start(delay_ms)

    def _auto_data_refresh_tick(self) -> None:
        if not hasattr(self, "auto_data_refresh_check") or not self.auto_data_refresh_check.isChecked():
            return
        if "data_refresh" in self._running_tasks:
            self._schedule_auto_data_refresh(delay_from_now=True)
            return
        if not self._start_data_refresh("auto"):
            self._schedule_auto_data_refresh(delay_from_now=True)

    def _apply_auto_quote_snapshot(self) -> None:
        if not self.current_quote:
            return
        record = self.cache.get_json("watch_quotes")
        payload = record.payload if record is not None else None
        if not isinstance(payload, list):
            return
        for item in payload:
            if not isinstance(item, dict) or str(item.get("code") or "").zfill(6) != self.current_quote.code:
                continue
            try:
                self.current_quote = StockQuote(**item)
            except (TypeError, ValueError):
                return
            if self.current_payload is not None:
                self._rebuild_payload_from_controls()
            return

    def _on_data_status_clicked(self) -> None:
        if "data_refresh" in self._running_tasks:
            self._set_status("自选数据正在刷新，完成后会自动更新。")
            return
        if self._last_refresh_summary and self._last_refresh_summary.status in {"partial", "failed"}:
            self._navigate("data")
            self._set_status("已打开刷新中心，可查看失败原因或手动重跑。")
            return
        self._start_data_refresh("manual")

    def _start_data_refresh(self, trigger: RefreshTrigger) -> bool:
        task_key = "data_refresh"
        if task_key in self._running_tasks:
            self._set_status("自选数据正在刷新，完成后会自动更新。")
            return False
        self._running_tasks.add(task_key)
        self._active_data_refresh_trigger = trigger
        self._refresh_stage_results = {}
        self._fill_refresh_stage_table()
        self._set_refresh_controls_enabled(False)
        self.data_status_pill.setText("数据刷新中")
        self._set_status("正在按自选优先顺序刷新数据...")

        signals = WorkSignals()
        self._active_background_signals.add(signals)

        def release_background() -> None:
            self._active_background_signals.discard(signals)
            self._active_background_threads.discard(worker)

        def finish(summary: RefreshRunSummary) -> None:
            self._running_tasks.discard(task_key)
            try:
                self._finish_data_refresh(summary)
            finally:
                release_background()

        def fail(message: str) -> None:
            self._running_tasks.discard(task_key)
            try:
                self._fail_data_refresh(message)
            finally:
                release_background()

        signals.progress.connect(self._apply_refresh_step)
        signals.done.connect(finish)
        signals.failed.connect(fail)

        current_code = self.current_quote.code if self.current_quote else None

        def runner() -> None:
            try:
                summary = self.refresh_coordinator.run(
                    trigger,
                    current_code=current_code,
                    progress=signals.progress.emit,
                )
                signals.done.emit(summary)
            except Exception as exc:
                signals.failed.emit(str(exc))

        worker = threading.Thread(target=runner, daemon=True)
        self._active_background_threads.add(worker)
        worker.start()
        return True

    def _apply_refresh_step(self, step: RefreshStepResult) -> None:
        self._refresh_stage_results[step.stage] = step
        self._fill_refresh_stage_table()
        if step.stage in {"watch_quotes", "watch_details"} and step.total:
            self.data_status_pill.setText(step.message)
            self._set_status(step.message)

    def _finish_data_refresh(self, summary: RefreshRunSummary) -> None:
        self._active_data_refresh_trigger = None
        self._last_refresh_summary = summary
        for step in summary.stages:
            self._refresh_stage_results[step.stage] = step
        self._refresh_dashboard_snapshot()
        self._fill_refresh_stage_table()
        self._fill_fetch_log_table()
        self._set_refresh_controls_enabled(True)
        if summary.trigger == "auto":
            self._apply_auto_quote_snapshot()
            finished_at = summary.finished_at or datetime.now()
            if summary.status == "success":
                self.data_status_pill.setText(f"行情已自动更新 {finished_at.strftime('%H:%M:%S')}")
            elif summary.status == "partial":
                self.data_status_pill.setText("行情已更新 · 部分源降级 · 查看")
            else:
                self.data_status_pill.setText("行情自动更新失败 · 查看")
        elif summary.status == "success":
            self.data_status_pill.setText("数据状态 已刷新")
        elif summary.status == "partial":
            self.data_status_pill.setText("已刷新 · 部分源降级 · 查看")
        else:
            self.data_status_pill.setText("刷新失败 · 点击查看")
        self._set_status(summary.message)
        if summary.trigger != "auto" and (
            self._review_automation_pending_after_refresh
            or self.cache.get_setting("review_automation_enabled", "1") == "1"
        ):
            self._review_automation_pending_after_refresh = False
            self._start_review_automation("startup")
        self._schedule_auto_data_refresh()

    def _fail_data_refresh(self, message: str) -> None:
        trigger = self._active_data_refresh_trigger
        self._active_data_refresh_trigger = None
        self._set_refresh_controls_enabled(True)
        self.data_status_pill.setText("行情自动更新失败 · 查看" if trigger == "auto" else "刷新失败 · 点击查看")
        self._set_status(f"数据刷新失败：{message}")
        self._fill_fetch_log_table()
        if trigger != "auto" and (
            self._review_automation_pending_after_refresh
            or self.cache.get_setting("review_automation_enabled", "1") == "1"
        ):
            self._review_automation_pending_after_refresh = False
            self._start_review_automation("startup")
        self._schedule_auto_data_refresh()

    def _show_fetch_logs(self) -> None:
        self._fill_fetch_log_table()
        self._set_status("已刷新抓取日志列表。")

    def _clear_fetch_logs(self) -> None:
        yes_button = getattr(QMessageBox, "Yes", QMessageBox.StandardButton.Yes)
        no_button = getattr(QMessageBox, "No", QMessageBox.StandardButton.No)
        answer = QMessageBox.question(
            self,
            "清空抓取日志",
            "确定清空抓取日志？这不会清理缓存，也不会删除策略、预警、观察任务、Key 或审计。",
            yes_button | no_button,
            no_button,
        )
        if answer != yes_button:
            self._set_status("已取消清空抓取日志。")
            return
        self.cache.clear_fetch_logs()
        self._fill_fetch_log_table()
        self._set_status("抓取日志已清空；缓存和业务数据均未改变。")

    def export_pit_failures_csv(self) -> None:
        if self.pit_database is None:
            QMessageBox.warning(self, "导出 PIT 失败清单", "PIT 数据库不可用。请检查数据目录后重试；数据没有变化。")
            return
        path = self._choose_csv_path("导出 PIT 失败清单", "pit_failures.csv")
        if not path:
            return
        try:
            count = self.pit_database.export_failed_logs_csv(Path(path))
        except OSError as exc:
            QMessageBox.warning(self, "导出失败", f"未写入文件，数据未变化：{exc}")
            return
        self._set_status(f"PIT 失败清单已导出：{path}（{count} 条）。")

    def _show_v1_snapshot_entry(self) -> None:
        self._start_v1_snapshot_update()

    def _start_v1_snapshot_update(self) -> bool:
        if self.factor_store is None:
            self._set_status(f"V1 因子库不可用：{self.v1_error_message or '请检查数据目录'}")
            QMessageBox.warning(self, "更新筛选快照", "V1 因子库不可用，请先检查或更换数据目录。")
            return False
        task_key = "v1_snapshot_update"
        if task_key in self._running_tasks:
            self._set_status("筛选快照正在更新，完成后会自动刷新状态。")
            return False
        yes_button = getattr(QMessageBox, "Yes", QMessageBox.StandardButton.Yes)
        no_button = getattr(QMessageBox, "No", QMessageBox.StandardButton.No)
        answer = QMessageBox.question(
            self,
            "更新筛选快照",
            "将使用 TickFlow（如已配置）批量更新最多 300 只候选股的行情、财务和 K 线；未配置时继续使用公开源。\n\n"
            "本次刷新提交研究复核 review_only 快照，今晚可用于条件选股；只有字段完整且通过质量闸门的 verified 快照才可进入正式筛选。"
            "不会消耗 AI token；源失败时会保留已有缓存并显示失败原因。是否继续？",
            yes_button | no_button,
            no_button,
        )
        if answer != yes_button:
            self._set_status("已取消更新筛选快照。")
            return False

        def task():
            self._sync_data_readiness_dependencies()
            return self.data_readiness.ensure(
                DataRequirement(
                    feature="更新筛选快照",
                    kinds=("factor_snapshot",),
                    mode="review_only",
                    force=True,
                    reason="用户在数据中心明确要求重新生成研究复核筛选快照",
                )
            )

        def done(result) -> None:
            self._refresh_dashboard_snapshot()
            self._fill_fetch_log_table()
            if result.status == "blocked":
                message = f"筛选快照更新失败：{result.message}"
                self.data_status_pill.setText("筛选快照更新失败")
                self._set_status(message)
                return
            stage = next((item for item in result.stages if item.stage == "factor_snapshot"), None)
            progress = f"{stage.fresh}/{stage.total}" if stage and stage.total else "已完成"
            snapshot = result.snapshot_id or "已生成"
            mode_label = "研究复核可用" if self._selected_screening_mode() != "formal" else ("正式可用" if result.status in {"ready", "refreshed"} else "仅研究复核")
            gap = f"；缺口：{result.errors[0]}" if result.errors else ""
            message = f"筛选快照已更新：{progress}，快照 {snapshot}，{mode_label}{gap}。"
            self._set_status(message)
            self.data_status_pill.setText(f"筛选快照已更新 · {mode_label}")

        return self._run_background(
            "正在更新筛选快照...",
            task,
            done,
            task_key=task_key,
            duplicate_text="筛选快照正在更新，完成后会自动刷新状态。",
            failed=lambda message: self._set_status(f"筛选快照更新失败：{message}"),
        )

    def _show_pit_incremental_entry(self) -> None:
        self._start_pit_incremental()

    def _start_pit_incremental(self) -> bool:
        if self.pit_database is None:
            self._set_status(f"PIT 数据库不可用：{self.pit_error_message or '请检查数据目录'}")
            QMessageBox.warning(self, "PIT日增量", "PIT 数据库不可用，请先检查或更换数据目录。")
            return False
        task_key = "pit_incremental"
        if task_key in self._running_tasks:
            self._set_status("PIT 日增量正在后台运行，完成后会自动更新。")
            return False
        yes_button = getattr(QMessageBox, "Yes", QMessageBox.StandardButton.Yes)
        no_button = getattr(QMessageBox, "No", QMessageBox.StandardButton.No)
        answer = QMessageBox.question(
            self,
            "PIT日增量",
            "将运行 PIT 日增量并写入本地 PIT 数据库。公开免费源可能限流或临时不可用；已有成功数据会保留。\n\n"
            "如需全量建库，仍建议在默认主界面完成连通性检查和 50 只预检后再执行。是否继续日增量？",
            yes_button | no_button,
            no_button,
        )
        if answer != yes_button:
            self._set_status("已取消 PIT 日增量。")
            return False

        def task():
            return self.data_readiness.ensure(
                DataRequirement(
                    feature="PIT 日增量",
                    kinds=("pit_incremental",),
                    mode="formal",
                    force=True,
                    reason="用户在数据中心明确要求执行 PIT 日增量",
                )
            )

        def done(result) -> None:
            if result.status == "blocked":
                message = f"PIT 日增量失败：{result.message}"
                self.data_status_pill.setText("PIT 日增量需复核")
                self._set_status(message)
                self._refresh_dashboard_snapshot()
                self._fill_fetch_log_table()
                return
            stage = next((item for item in result.stages if item.stage == "pit_incremental"), None)
            detail = stage.message if stage is not None else result.message
            message = f"PIT 日增量完成：{detail}"
            self._set_status(message)
            self.data_status_pill.setText("PIT 日增量完成")
            self._refresh_dashboard_snapshot()
            self._fill_fetch_log_table()

        return self._run_background(
            "正在运行 PIT 日增量...",
            task,
            done,
            task_key=task_key,
            duplicate_text="PIT 日增量正在后台运行，完成后会自动更新。",
            failed=lambda message: self._set_status(f"PIT 日增量失败：{message}"),
        )

    def _execute_pit_build(
        self,
        mode: str,
        *,
        start_date: str,
        end_date: str,
        stock_limit: int | None = None,
        refresh_existing: bool = False,
        universe_source: str = "baostock",
    ) -> PITBuildResult:
        if self.pit_database is None:
            raise RuntimeError("PIT 数据库不可用")
        self.pit_database.mark_stale_running_jobs()
        provider = BaostockPITProvider(timeout_sec=60, protection_profile="balanced")
        builder = PITDatasetBuilder(
            self.pit_database,
            provider,
            batch_size=50,
            tushare_token=self._pit_token_text(),
        )
        return builder.run(
            mode=mode,
            start_date=start_date,
            end_date=end_date,
            stock_limit=stock_limit,
            refresh_existing=refresh_existing,
            universe_source=universe_source,
        )

    def _refresh_top_ai_pill(self) -> None:
        if not hasattr(self, "ai_status_pill"):
            return
        api_key = ""
        if hasattr(self, "api_key_input"):
            api_key = self.api_key_input.text().strip()
        else:
            api_key = self.secret_store.get_secret(AI_API_KEY_NAME).strip()
        self.ai_status_pill.setText("AI Key 已配置" if api_key else "AI Key 未填写")

    def _open_data_dir(self) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.data_dir)))

    def _acceptance_report_dir(self) -> Path:
        return Path(__file__).resolve().parents[2] / "docs" / "acceptance_reports"

    def refresh_acceptance_center(self) -> None:
        if not hasattr(self, "acceptance_table"):
            return
        self._acceptance_reports = load_acceptance_report_summaries(self._acceptance_report_dir(), limit=10)
        self.acceptance_table.setRowCount(len(self._acceptance_reports))
        for row_index, item in enumerate(self._acceptance_reports):
            stem = item.json_path.stem.lower()
            if "quick" in stem:
                mode = "快速验收"
            elif "full" in stem:
                mode = "完整验收"
            elif "release" in stem:
                mode = "发布验收"
            else:
                mode = "验收记录"
            if item.live_enabled:
                mode += " · live"
            failed = item.error or "、".join(item.failed_blocks) or "-"
            values = (item.time_text or "-", item.conclusion, mode, failed)
            for col, value in enumerate(values):
                self.acceptance_table.setItem(row_index, col, QTableWidgetItem(str(value)))
        if self._acceptance_reports:
            self.acceptance_table.selectRow(0)
            newest = self._acceptance_reports[0]
            self.acceptance_status_label.setText(
                f"最近一次：{newest.conclusion}；TXT：{newest.txt_path.name}；JSON：{newest.json_path.name}"
            )
        else:
            self.acceptance_status_label.setText("暂无验收记录。运行 quick、full 或 release_acceptance 后会在这里显示结果。")
        self._set_status("验收中心已刷新。")

    def _selected_acceptance_report(self):
        reports = getattr(self, "_acceptance_reports", [])
        row = self.acceptance_table.currentRow() if hasattr(self, "acceptance_table") else -1
        return reports[row] if 0 <= row < len(reports) else None

    def _open_acceptance_path(self, path: Path, label: str) -> None:
        if not path.exists():
            QMessageBox.information(self, "验收中心", f"{label}不存在：\n{path}")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def open_acceptance_txt(self) -> None:
        item = self._selected_acceptance_report()
        if item is None:
            QMessageBox.information(self, "验收中心", "请先选择一条验收记录。")
            return
        self._open_acceptance_path(item.txt_path, "TXT 报告")

    def open_acceptance_json(self) -> None:
        item = self._selected_acceptance_report()
        if item is None:
            QMessageBox.information(self, "验收中心", "请先选择一条验收记录。")
            return
        self._open_acceptance_path(item.json_path, "JSON 报告")

    def open_acceptance_report_dir(self) -> None:
        report_dir = self._acceptance_report_dir()
        if not report_dir.exists():
            QMessageBox.information(self, "验收中心", f"验收目录不存在：\n{report_dir}")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(report_dir)))

    def _change_data_dir(self) -> None:
        selected = QFileDialog.getExistingDirectory(self, "选择新的数据存储文件夹", str(self.data_dir))
        if not selected:
            return
        target = Path(selected).resolve()
        if self._running_tasks:
            if hasattr(self, "data_migration_status_label"):
                self.data_migration_status_label.setText("未开始：仍有后台任务使用旧目录。")
            QMessageBox.warning(
                self,
                "暂不能迁移数据目录",
                "仍有后台任务在使用旧目录。请等待任务完成后再迁移；这样可避免旧任务在迁移后迟到写入。",
            )
            return
        try:
            plan = preflight_data_migration(self.data_dir, target, require_empty=True)
        except (OSError, ValueError) as exc:
            if hasattr(self, "data_migration_status_label"):
                self.data_migration_status_label.setText(f"预检失败：{exc}")
            QMessageBox.warning(self, "数据目录预检失败", f"未开始迁移，当前目录保持不变：\n{exc}")
            return
        size_mb = plan.total_bytes / (1024 * 1024)
        yes_button = getattr(QMessageBox, "Yes", QMessageBox.StandardButton.Yes)
        no_button = getattr(QMessageBox, "No", QMessageBox.StandardButton.No)
        answer = QMessageBox.question(
            self,
            "确认迁移数据目录",
            f"将复制 {plan.file_count} 个文件（约 {size_mb:.1f} MB）到空目录：\n{target}\n\n"
            "迁移成功后会更新目录设置，但必须重启软件后才会使用新目录。原目录会保留，失败时不会切换目录。是否继续？",
            yes_button | no_button,
            no_button,
        )
        if answer != yes_button:
            if hasattr(self, "data_migration_status_label"):
                self.data_migration_status_label.setText("已取消：当前目录未变化。")
            self._set_status("已取消数据目录迁移。")
            return
        self._ui_generation += 1
        if hasattr(self, "data_migration_status_label"):
            self.data_migration_status_label.setText("迁移中：正在复制数据；请勿关闭软件或移动目录。")
            QApplication.processEvents()
        try:
            migrate_data_dir(self.data_dir, target, require_empty=True)
            write_data_dir(target)
        except OSError as exc:
            if hasattr(self, "data_migration_status_label"):
                self.data_migration_status_label.setText("迁移失败：当前目录未切换，可修复磁盘问题后重试。")
            QMessageBox.warning(self, "数据目录迁移失败", f"当前目录未切换，原数据仍保留：\n{exc}\n\n请检查磁盘空间和文件占用后重试。")
            return
        if hasattr(self, "data_dir_label"):
            self.data_dir_label.setText(f"{target}（已迁移，重启后生效）")
        if hasattr(self, "data_migration_status_label"):
            self.data_migration_status_label.setText("迁移完成：新目录已保存，重启后生效；原目录仍保留。")
        QMessageBox.information(self, "数据目录迁移完成", "数据已复制且新目录已保存。请重启软件后继续使用，避免旧进程向旧目录写入。")
        self._set_status("数据目录迁移完成；请重启软件后继续使用新目录。")

    def _clear_cache_from_settings(self) -> None:
        yes_button = getattr(QMessageBox, "Yes", QMessageBox.StandardButton.Yes)
        no_button = getattr(QMessageBox, "No", QMessageBox.StandardButton.No)
        answer = QMessageBox.question(
            self,
            "清理缓存",
            "确定清理行情、财务和 K 线缓存？自选股、观察任务和 API Key 不会删除。",
            yes_button | no_button,
            no_button,
        )
        if answer != yes_button:
            return
        self.cache.clear_cache()
        self._last_refresh_summary = None
        self._refresh_stage_results = {}
        self._refresh_dashboard_snapshot()
        self._fill_refresh_stage_table()
        self._fill_fetch_log_table()
        self._set_status("缓存已清理；下次查询会重新拉取公开数据。")

    def _combo(self, items: list[tuple[str, str]]) -> QComboBox:
        combo = QComboBox()
        for label, value in items:
            combo.addItem(label, value)
        return combo

    def _set_combo_value(self, combo: QComboBox, value: str) -> None:
        index = combo.findData(value)
        combo.setCurrentIndex(index if index >= 0 else 0)

    def _load_ai_settings(self) -> None:
        provider = self.cache.get_setting("ai_provider", "deepseek")
        model = self.cache.get_setting("ai_model", "deepseek-v4-flash")
        api_key = self.secret_store.get_secret(AI_API_KEY_NAME)
        self._set_combo_value(self.provider_combo, provider)
        self.model_input.setText(model)
        self.api_key_input.setText(api_key)
        self.api_key_status_label.setText("未填写" if not api_key.strip() else "待验证")
        if hasattr(self, "tushare_token_input"):
            token = self.secret_store.get_secret(TUSHARE_TOKEN_NAME)
            self.tushare_token_input.setText(token)
            enabled = self.cache.get_setting(TUSHARE_MONEY_FLOW_FALLBACK_SETTING, "0") == "1"
            self.tushare_fallback_check.setChecked(enabled)
            if not token.strip():
                self.tushare_status_label.setText("未填写 Token；即使打开兜底也不会调用 Tushare。")
            elif enabled:
                self.tushare_status_label.setText("兜底已启用；权限或积分不足时会显示公开数据源失败原因。")
            else:
                self.tushare_status_label.setText("Token 已保存；东财失败时暂不使用 Tushare 兜底。")
        if hasattr(self, "tickflow_api_key_input"):
            tickflow_key = self.secret_store.get_secret(TICKFLOW_API_KEY_NAME)
            self.tickflow_api_key_input.setText(tickflow_key)
            self.tickflow_status_label.setText("未填写；使用免费源" if not tickflow_key.strip() else "已配置；优先使用 TickFlow 批量源")
        self._refresh_top_ai_pill()

    def _refresh_ai_cost_label(self) -> None:
        if not hasattr(self, "ai_cost_label"):
            return
        self.ai_cost_label.setText(pricing_summary(str(self.provider_combo.currentData() or "deepseek"), self.model_input.text().strip()))

    def _apply_style(self) -> None:
        self.setStyleSheet(build_workbench_qss())

    def _register_fonts(self) -> None:
        for name in ("JetBrainsMono-Regular.ttf", "JetBrainsMono-Medium.ttf"):
            font_path = _ui_resource_path("web", "fonts", name)
            if font_path.exists():
                QFontDatabase.addApplicationFont(str(font_path))

    def _load_chart_page(self) -> None:
        channel = QWebChannel(self.web_view.page())
        channel.registerObject("qtBridge", self.bridge)
        self.web_view.page().setWebChannel(channel)
        self.web_view.loadFinished.connect(self._on_chart_loaded)
        html = _ui_resource_path("web", "stock_workbench.html")
        self.web_view.load(QUrl.fromLocalFile(str(html)))

    def _on_chart_loaded(self, ok: bool) -> None:
        self._chart_ready = ok
        if ok and self.current_payload:
            self._render_chart(self.current_payload)

    def _set_status(self, text: str) -> None:
        self.status_label.setText(text)

    def _set_observation_save_block(self, message: str | None) -> None:
        self._observation_save_block_message = message
        self._refresh_observer_save_buttons()

    def _refresh_observer_save_buttons(self) -> None:
        if not hasattr(self, "save_watch_button"):
            return
        formal_context = bool(self._formal_candidate_research_context)
        can_save = bool(self.current_quote and self.current_trade_reference and not self._observation_save_block_message)
        for button_name in (
            "build_observer_button",
            "ai_enhance_observer_button",
            "save_watch_button",
            "save_holding_button",
        ):
            button = getattr(self, button_name, None)
            if button is not None:
                button.setVisible(not formal_context)
                button.setEnabled(can_save and not formal_context if "save_" in button_name else not formal_context)

    def _show_observation_generating(self, quote: StockQuote, message: str) -> None:
        self.report_box.setPlainText(
            f"{quote.name}（{quote.code}）观察参考\n\n{message}\n\n完成前不能保存观察单或持仓单。"
        )

    def _run_background(
        self,
        loading: str,
        fn: Callable[[], Any],
        done: Callable[[Any], None],
        task_key: str | None = None,
        duplicate_text: str | None = None,
        failed: Callable[[str], None] | None = None,
    ) -> bool:
        if self._window_closing:
            return False
        if task_key and task_key in self._running_tasks:
            self._set_status(duplicate_text or "任务仍在后台运行，完成后会自动刷新。")
            return False
        if task_key:
            self._running_tasks.add(task_key)
        self._set_status(loading)
        generation = self._ui_generation
        signals = WorkSignals()
        self._active_background_signals.add(signals)

        def release_background() -> None:
            self._active_background_signals.discard(signals)
            self._active_background_threads.discard(worker)

        def finish(result: Any) -> None:
            if task_key:
                self._running_tasks.discard(task_key)
            try:
                if not self._window_closing and generation == self._ui_generation:
                    done(result)
            finally:
                release_background()

        def fail(message: str) -> None:
            if task_key:
                self._running_tasks.discard(task_key)
            try:
                if self._window_closing or generation != self._ui_generation:
                    return
                if failed is not None:
                    failed(message)
                else:
                    self._show_error(message)
            finally:
                release_background()

        signals.done.connect(finish)
        signals.failed.connect(fail)

        def runner() -> None:
            try:
                result = fn()
            except Exception as exc:
                if not self._window_closing:
                    try:
                        signals.failed.emit(str(exc))
                    except RuntimeError:
                        pass
            else:
                if not self._window_closing:
                    try:
                        signals.done.emit(result)
                    except RuntimeError:
                        pass
            finally:
                if self._window_closing:
                    release_background()

        worker = threading.Thread(target=runner, daemon=True)
        self._active_background_threads.add(worker)
        worker.start()
        return True

    def closeEvent(self, event) -> None:
        """停止 Qt 定时器并使后台线程的旧回调失效，防止关闭后迟到写入 UI。"""
        self._window_closing = True
        self._ui_generation += 1
        self._alert_monitor_timer.stop()
        self._review_automation_timer.stop()
        self._auto_data_refresh_timer.stop()
        if hasattr(self, "data_readiness"):
            self.data_readiness.close()
        self._running_tasks.clear()
        event.accept()

    def _show_error(self, message: str) -> None:
        self._set_status("操作失败")
        QMessageBox.warning(self, "提示", message)

    def _current_ai_config(self) -> AIConfig:
        return AIConfig(
            provider=str(self.provider_combo.currentData() or "deepseek"),
            model=self.model_input.text().strip(),
            api_key=self.api_key_input.text().strip(),
        )

    def _set_api_key_status(self, result: AIValidationResult) -> None:
        labels = {
            "ok": "验证通过",
            "empty": "未填写",
            "invalid_key": "Key 无效",
            "balance_insufficient": "余额不足",
            "rate_limited": "请求过快",
            "timeout": "网络异常",
            "network_error": "网络异常",
            "model_unavailable": "模型可能不可用",
            "unsupported_provider": "供应商不支持",
        }
        text = labels.get(result.status, result.message or "验证失败")
        if result.model:
            text = f"{text}（{result.provider}/{result.model}）"
        self.api_key_status_label.setText(text)
        self._refresh_top_ai_pill()
        self._set_status(f"API Key 状态：{text}")

    def save_settings(self) -> None:
        config = self._current_ai_config()
        self.cache.set_setting("ai_provider", config.provider)
        self.cache.set_setting("ai_model", config.model)
        self.secret_store.set_secret(AI_API_KEY_NAME, config.api_key)
        if hasattr(self, "tushare_token_input"):
            self.secret_store.set_secret(TUSHARE_TOKEN_NAME, self.tushare_token_input.text().strip())
            self.cache.set_setting(
                TUSHARE_MONEY_FLOW_FALLBACK_SETTING,
                "1" if self.tushare_fallback_check.isChecked() else "0",
            )
        if hasattr(self, "tickflow_api_key_input"):
            self.secret_store.set_secret(TICKFLOW_API_KEY_NAME, self.tickflow_api_key_input.text().strip())
        self._load_ai_settings()
        self._refresh_ai_cost_label()
        self._refresh_top_ai_pill()
        self._set_status(f"设置已保存，API Key 存储：{self.secret_store.backend_message}")
        self.validate_saved_api_key()

    def validate_saved_api_key(self) -> None:
        config = self._current_ai_config()
        if not config.api_key.strip():
            self._set_api_key_status(AIValidationResult(False, "empty", "未填写", config.provider, config.model))
            return
        self.api_key_status_label.setText("正在验证")
        self._run_background("正在验证 API Key...", lambda: validate_ai_key(config, timeout=20), self._set_api_key_status)

    def search_stock(self, auto_load_single: bool = False) -> None:
        self._formal_candidate_research_context = False
        self._refresh_observer_save_buttons()
        keyword = self.search_input.text().strip()
        if not keyword:
            QMessageBox.information(self, "提示", "请输入股票代码或名称")
            return

        def done(matches: list[StockQuote]) -> None:
            self.search_list.clear()
            for quote in matches:
                item = QListWidgetItem(f"{quote.name} {quote.code}")
                item.setData(Qt.UserRole, quote.__dict__)
                self.search_list.addItem(item)
            self._set_status(f"找到 {len(matches)} 条结果")
            if auto_load_single and len(matches) == 1:
                self.current_quote = matches[0]
                self.load_stock()

        self._run_background("正在搜索股票...", lambda: self.fetcher.search(keyword), done)

    def _quote_from_item(self, item: QListWidgetItem) -> StockQuote:
        data = item.data(Qt.UserRole) or {}
        return StockQuote(**data)

    def _load_selected_item(self, item: QListWidgetItem) -> None:
        self._formal_candidate_research_context = False
        self._refresh_observer_save_buttons()
        self.current_quote = self._quote_from_item(item)
        self.load_stock()

    def _load_watch_item(self, item: QListWidgetItem) -> None:
        self._formal_candidate_research_context = False
        self._refresh_observer_save_buttons()
        self.current_quote = self._quote_from_item(item)
        self.load_stock()

    def load_stock(self, force: bool = True) -> None:
        if not self.current_quote:
            QMessageBox.information(self, "提示", "请先选择股票")
            return
        code = self.current_quote.code
        self._stock_load_version += 1
        load_version = self._stock_load_version
        self._observer_state_version += 1
        self.current_trade_reference = None
        self.current_observation_note = ""
        self._set_observation_save_block(None)

        def task():
            errors: list[str] = []
            quote = self.fetcher.quote_by_code(code) or self.current_quote
            try:
                metrics, _ = self.fetcher.financial_metrics(code, force=force)
            except Exception as exc:
                metrics = FinancialMetrics()
                errors.append(f"财务暂不可用：{exc}")
            try:
                history, _ = self.fetcher.history(code, days=365, force=force)
            except Exception as exc:
                history = pd.DataFrame()
                errors.append(f"K线暂不可用：{exc}")
            return quote, metrics, history, errors

        def done(result) -> None:
            if load_version != self._stock_load_version:
                self._set_status(f"{code} 加载结果已过期，未覆盖当前股票")
                return
            quote, metrics, history, errors = result
            self.current_quote = quote
            self.current_metrics = metrics
            self.current_history = history
            self.current_trade_reference = None
            self.current_observation_note = ""
            self._set_observation_save_block(None)
            self.stock_title.setText(f"{quote.name}（{quote.code}）")
            self._rebuild_payload_from_controls()
            self._set_status(f"{quote.name} 已加载" + (f"；{'；'.join(errors[:2])}" if errors else ""))
            if self._pending_generate_observation_code == quote.code:
                self._pending_generate_observation_code = None
                self.generate_trade_observation()

        self._run_background(f"正在加载 {self.current_quote.name}...", task, done)

    def _rebuild_payload_from_controls(self) -> None:
        if not self.current_quote:
            return
        payload = build_chart_payload(
            self.current_quote,
            self.current_history,
            self.current_metrics,
            period=self.period_combo.currentData() or "daily",
            chart_range=self.range_combo.currentData() or "240d",
            mode=self.mode_combo.currentData() or "candlestick",
        ).to_dict()
        payload["subchart"] = self.subchart_combo.currentData() or "volume"
        self.current_payload = payload
        self._render_intelligence(payload)
        self._render_local_report(payload)
        self._render_chart(payload)

    def _render_chart(self, payload: dict[str, Any]) -> None:
        if not self._chart_ready:
            return
        js = "renderChart(" + json.dumps(payload, ensure_ascii=False) + ");"
        self.web_view.page().runJavaScript(js)

    def _render_intelligence(self, payload: dict[str, Any]) -> None:
        info = payload.get("intelligence", {})
        quote = info.get("quote", {})
        ma = info.get("moving_average", {})
        momentum = info.get("momentum", {})
        self.intelligence_panel.set_sections(
            quote_metrics=[
                ("价格", fmt_number(quote.get("price"), "元")),
                ("涨跌幅", fmt_number(quote.get("change_pct"), "%")),
                ("换手率", fmt_number(quote.get("turnover"), "%")),
                ("PE / PB", f"{fmt_number(quote.get('pe'))} / {fmt_number(quote.get('pb'))}"),
            ],
            trend_summary=info.get("trend", {}).get("summary") or "-",
            ma_metrics=[
                ("状态", str(ma.get("state") or "-")),
                ("MA5", fmt_number(ma.get("ma5"))),
                ("MA20", fmt_number(ma.get("ma20"))),
                ("MA60", fmt_number(ma.get("ma60"))),
            ],
            momentum_metrics=[
                ("RSI6", fmt_number(momentum.get("rsi6"))),
                ("MACD", fmt_number(momentum.get("macd"))),
                ("KDJ-J", fmt_number(momentum.get("kdj_j"))),
            ],
            opportunities=list(info.get("opportunity_tags") or []),
            risks=list(info.get("risk_tags") or []),
            gaps=list(info.get("data_gaps") or []),
        )

    def _render_local_report(self, payload: dict[str, Any]) -> None:
        info = payload.get("intelligence", {})
        report = {
            "核心结论": info.get("trend", {}).get("summary") or "等待数据加载",
            "技术面": info.get("moving_average", {}),
            "量价资金": "首版本地报告以量价指标为主，资金流在生成观察时补充。",
            "主要风险": info.get("risk_tags", []),
            "后续核查": "点击“生成本地观察”后补充技术、资金、市场和事件证据摘要，不消耗 AI token。",
            "合规说明": "仅作数据观察参考，不承诺收益，不构成保证性买卖建议。",
        }
        self.report_box.setPlainText(json.dumps(report, ensure_ascii=False, indent=2))

    def _build_trade_observation_context(
        self,
        base_quote: StockQuote,
        history=None,
        reference: TradeReference | None = None,
        *,
        refresh: bool = True,
    ):
        quote = base_quote
        code = quote.code
        gaps: list[str] = []
        if refresh:
            try:
                quote = self.fetcher.quote_by_code(code) or base_quote
            except Exception as exc:
                quote = base_quote
                gaps.append(f"实时行情刷新失败：{exc}")
        if history is None or history.empty:
            try:
                history, _ = self.fetcher.history(code, days=180, force=refresh)
            except Exception as exc:
                history = pd.DataFrame()
                gaps.append(f"K线数据获取失败：{exc}")
        technical = build_technical_profile(history)
        gaps.extend(getattr(technical, "data_gaps", []) or [])
        try:
            money_flow, _ = self.fetcher.money_flow(code, days=20, force=refresh)
        except Exception as exc:
            money_flow = None
            gaps.append(f"资金流数据获取失败：{exc}")
        try:
            sentiment, _ = self.fetcher.market_sentiment(force=refresh)
        except Exception as exc:
            sentiment = None
            gaps.append(f"市场情绪数据获取失败：{exc}")
        try:
            events, _ = self.fetcher.event_catalysts(code, force=refresh)
        except Exception as exc:
            events = []
            gaps.append(f"事件催化数据获取失败：{exc}")
        events = list(events or [])
        trade_reference = reference or build_trade_reference(quote, technical, money_flow, sentiment, events)
        return quote, trade_reference, technical, money_flow, sentiment, events, gaps

    def _compact_observation_note(
        self,
        quote,
        reference,
        technical,
        money_flow,
        sentiment,
        events,
        data_gaps: list[str],
        ai_report: str | None = None,
        ai_error: str | None = None,
    ) -> str:
        event_text = "；".join(f"{event.date or '-'} {event.title}" for event in events[:3]) if events else "无"
        risk_text = "；".join(reference.risks[:4]) if reference.risks else "无额外反证"
        gap_text = "；".join(dict.fromkeys(data_gaps)) if data_gaps else "无明显数据缺口"
        ai_text = "已追加" if ai_report else (f"失败：{ai_error}" if ai_error else "未触发")
        lines = [
            f"生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            f"股票：{quote.name}（{quote.code}）" if quote else "股票：-",
            "本地研究摘要：已生成，可结合数据时间、来源和缺口继续核查",
            f"技术：{technical.summary if technical else 'K线数据缺失'}",
            f"资金：{money_flow.summary if money_flow else '资金流数据缺失'}",
            f"市场：{sentiment.summary if sentiment else '市场情绪数据缺失'}",
            f"事件：{event_text}",
            f"反证：{risk_text}",
            f"数据缺口：{gap_text}",
            f"AI证据整理：{ai_text}",
        ]
        text = "\n".join(lines)
        return text if len(text) <= 980 else text[:977] + "..."

    def _render_trade_reference(
        self,
        quote,
        reference,
        technical,
        money_flow,
        sentiment,
        events,
        data_gaps: list[str],
        ai_report: str | None = None,
        ai_error: str | None = None,
    ) -> None:
        technical_text = technical.summary if technical and technical.available else (technical.summary if technical else "K线数据缺失")
        money_text = money_flow.summary if money_flow else "资金流数据缺失"
        sentiment_text = sentiment.summary if sentiment else "市场情绪数据缺失"
        event_lines = "\n".join(f"- {event.date or '-'} {event.title}" for event in events[:5]) if events else "- 暂无明确事件催化"
        risk_lines = "\n".join(f"- {risk}" for risk in reference.risks[:6]) if reference.risks else "- 暂无额外反证条件"
        gap_items = list(data_gaps)
        if not technical or not technical.available:
            gap_items.append("K线/技术画像不足")
        if not money_flow:
            gap_items.append("资金流数据缺失")
        if not sentiment:
            gap_items.append("市场情绪数据缺失")
        if not events:
            gap_items.append("事件催化数据缺失")
        if ai_error:
            gap_items.append(f"AI证据整理失败：{ai_error}")
        deduped_gaps = list(dict.fromkeys(gap_items))
        gap_lines = "\n".join(f"- {gap}" for gap in deduped_gaps) if deduped_gaps else "- 暂无明显数据缺口"
        ai_block = f"\n\nAI证据整理（研究旁路）：\n{ai_report.strip()}" if ai_report else ""
        text = f"""
 {quote.name}（{quote.code}）研究证据摘要
生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

研究边界：
本页只展示确定性事实、Agent 证据、反向证据、数据缺口和后续核查条件；不展示交易价格、仓位或收益判断。

技术面：
{technical_text}

资金面：
{money_text}

市场情绪：
{sentiment_text}

事件催化：
{event_lines}

反证条件：
{risk_lines}

数据缺口：
{gap_lines}

本地推理链：
{reference.thesis}
{ai_block}

说明：这是基于行情、K线、资金流、市场情绪和事件数据生成的观察参考，不构成投资建议。
""".strip()
        self.report_box.setPlainText(text)
        self.current_observation_note = self._compact_observation_note(
            quote,
            reference,
            technical,
            money_flow,
            sentiment,
            events,
            deduped_gaps,
            ai_report,
            ai_error,
        )
        self._refresh_observer_save_buttons()

    def _on_chart_event(self, event: dict[str, Any]) -> None:
        event_type = event.get("type")
        if event_type == "hover":
            row = event.get("row") or {}
            html = f"""
            <b>{row.get('date', '-')}</b><br>
            开 {fmt_number(row.get('open'))}　高 {fmt_number(row.get('high'))}　低 {fmt_number(row.get('low'))}　收 {fmt_number(row.get('close'))}<br>
            成交量 {fmt_number(row.get('volume'), digits=0)}<br>
            MA5 {fmt_number(row.get('ma5'))}　MA20 {fmt_number(row.get('ma20'))}　MACD {fmt_number(row.get('macd'))}
            """
            self.hover_box.setHtml(html)
        elif event_type == "signalClick":
            signal = event.get("signal") or {}
            self._set_status(f"图表信号：{signal.get('date', '-')} {signal.get('label', '-')}")

    def add_current_watch(self) -> None:
        if not self.current_quote:
            QMessageBox.information(self, "提示", "请先选择股票")
            return
        self.cache.add_watch(self.current_quote.code, self.current_quote.name)
        self._load_watchlist()
        self._refresh_dashboard_snapshot()
        self._set_status(f"已加入自选：{self.current_quote.name}")

    def _load_watchlist(self) -> None:
        if not hasattr(self, "watch_list"):
            return
        self.watch_list.clear()
        for row in self.cache.list_watch():
            quote = StockQuote(code=row["code"], name=row["name"])
            item = QListWidgetItem(f"{quote.name} {quote.code}")
            item.setData(Qt.UserRole, quote.__dict__)
            self.watch_list.addItem(item)

    def generate_trade_observation(self) -> None:
        if self._formal_candidate_research_context:
            self._set_status("正式候选研究页只允许查看证据与数据缺口，不生成旧交易观察单。")
            return
        if not self.current_quote:
            QMessageBox.information(self, "提示", "请先选择股票")
            return
        quote_snapshot = self.current_quote
        history_snapshot = self.current_history.copy(deep=True) if self.current_history is not None else pd.DataFrame()
        task_key = f"observer:{quote_snapshot.code}"
        duplicate_text = f"{quote_snapshot.name} 的本地观察任务仍在后台运行，完成后会自动刷新。"
        if task_key in self._running_tasks:
            self._set_status(duplicate_text)
            return
        self._observer_state_version += 1
        observer_version = self._observer_state_version
        self.current_quote = quote_snapshot
        self.current_trade_reference = None
        self.current_observation_note = ""
        self._set_observation_save_block("观察生成中，请等待完成后再保存")
        self._show_observation_generating(quote_snapshot, f"正在生成 {quote_snapshot.name} 本地观察参考...")

        def task():
            self._sync_data_readiness_dependencies()
            readiness = self.data_readiness.ensure(
                DataRequirement(
                    feature=f"{quote_snapshot.name} 生成观察记录",
                    kinds=("quotes", "research_detail", "market_context"),
                    codes=(quote_snapshot.code,),
                    mode="formal",
                    reason="生成观察记录前必须先准备完整个股研究数据",
                )
            )
            if readiness.status == "blocked":
                raise ValueError(readiness.message)
            prepared_quote = self._quote_from_feature_readiness_cache(quote_snapshot)
            return self._build_trade_observation_context(prepared_quote, None, refresh=False)

        def done(result) -> None:
            if observer_version != self._observer_state_version:
                self._set_status("本地观察结果已过期，未覆盖当前股票")
                return
            quote, reference, technical, money_flow, sentiment, events, data_gaps = result
            self.current_quote = quote
            self.current_trade_reference = reference
            self._set_observation_save_block(None)
            self._render_trade_reference(quote, reference, technical, money_flow, sentiment, events, data_gaps)
            self._set_status("本地观察参考已生成，不消耗 AI token")

        def failed(message: str) -> None:
            if observer_version != self._observer_state_version:
                return
            self.current_quote = quote_snapshot
            self.current_trade_reference = None
            self.current_observation_note = ""
            self._set_observation_save_block(None)
            self._show_observation_generating(quote_snapshot, f"本地观察生成失败：{message}")
            self._set_status(f"{quote_snapshot.name} 本地观察生成失败")
            QMessageBox.warning(self, "提示", message)

        self._run_background(
            "正在生成观察参考...",
            task,
            done,
            task_key=task_key,
            duplicate_text=duplicate_text,
            failed=failed,
        )

    def analyze_trade_observation_with_ai(self) -> None:
        if self._formal_candidate_research_context:
            self._set_status("正式候选研究页已由筛选任务统一整理五 Agent 证据，不进入旧交易 AI 入口。")
            return
        if not self.current_quote or not self.current_trade_reference:
            QMessageBox.information(self, "提示", "请先生成本地观察")
            return
        config = self._current_ai_config()
        if not config.api_key.strip():
            QMessageBox.information(self, "提示", "请先在设置页填写 API Key")
            return
        yes_button = getattr(QMessageBox, "Yes", QMessageBox.StandardButton.Yes)
        no_button = getattr(QMessageBox, "No", QMessageBox.StandardButton.No)
        answer = QMessageBox.question(
            self,
            "AI证据整理（研究旁路）",
            "只整理给定快照中的支持/反向证据、数据缺口和待核查问题，不新增、删除、排序或升级候选；调用模型可能产生费用，实际以服务商账单为准。是否继续？",
            yes_button | no_button,
            no_button,
        )
        if answer != yes_button:
            return

        base_quote = self.current_quote
        base_reference = self.current_trade_reference
        history_snapshot = self.current_history.copy(deep=True) if self.current_history is not None else pd.DataFrame()
        task_key = f"observer_ai:{base_quote.code}"
        duplicate_text = f"{base_quote.name} 的AI证据整理任务仍在后台运行，完成后会自动刷新。"
        if task_key in self._running_tasks:
            self._set_status(duplicate_text)
            return
        self._observer_state_version += 1
        observer_version = self._observer_state_version
        self._set_observation_save_block("观察生成中，请等待完成后再保存")

        def task():
            self._sync_data_readiness_dependencies()
            readiness = self.data_readiness.ensure(
                DataRequirement(
                    feature=f"{base_quote.name} AI研究",
                    kinds=("quotes", "research_detail", "market_context", "factor_snapshot"),
                    codes=(base_quote.code,),
                    mode="formal",
                    reason="AI请求前必须先准备个股研究数据和正式因子快照",
                )
            )
            if readiness.status == "blocked":
                raise ValueError(readiness.message)
            if self.factor_store is None or not readiness.snapshot_id:
                raise ValueError("AI研究缺少本次准备完成的不可变因子快照；数据未就绪，未发起 AI 请求")
            prepared_quote = self._quote_from_feature_readiness_cache(base_quote)
            quote, reference, technical, money_flow, sentiment, events, data_gaps = self._build_trade_observation_context(
                prepared_quote,
                None,
                base_reference,
                refresh=False,
            )
            snapshot_store = DataSnapshotStore(self.factor_store.database)
            manifest = snapshot_store.get_manifest(readiness.snapshot_id)
            if manifest is None:
                raise ValueError("AI研究缺少本次准备完成的不可变因子快照；数据未就绪，未发起 AI 请求")
            snapshot_facts = snapshot_store.list_facts(manifest.snapshot_id)
            candidate_facts = [fact for fact in snapshot_facts if str(fact.code).zfill(6) == str(quote.code).zfill(6)]
            if not candidate_facts:
                raise ValueError("当前股票不在正式筛选快照范围，已阻止 AI 请求；数据未就绪，未发起 AI 请求")
            refresh_event_id = readiness.refresh_event_id or snapshot_store.refresh_event_id_for_snapshot(manifest.snapshot_id)
            discussion_formula_version = "research_discussion_v1"
            research_mode = "formal"
            if not refresh_event_id:
                raise ValueError("AI研究缺少刷新事件 ID；数据未就绪，未发起 AI 请求")
            evidence_package = build_evidence_package(
                data_snapshot_id=manifest.snapshot_id,
                refresh_event_id=refresh_event_id,
                formula_version=discussion_formula_version,
                snapshot_metadata=manifest if candidate_facts else None,
                facts=candidate_facts,
                candidate_records=[
                    {
                        "code": quote.code,
                        "name": quote.name,
                        "price": quote.price,
                        "industry": getattr(quote, "industry", ""),
                        "industry_strength": getattr(quote, "industry_strength", None),
                        "technical_status": getattr(technical, "status", ""),
                        "technical_summary": getattr(technical, "summary", ""),
                    }
                ],
                candidate_set_digest=stable_hash([quote.code]),
                factor_breakdown={
                    "technical": getattr(technical, "summary", ""),
                    "money_flow": getattr(money_flow, "summary", "") if money_flow else "",
                    "market_sentiment": getattr(sentiment, "summary", "") if sentiment else "",
                },
                risk_status={"counter_evidence": list(getattr(reference, "risks", []) or [])},
                missing_fields={"data_gaps": list(data_gaps)},
                financial_changes={},
                historical_similar_samples=[],
                external_facts=[
                    {
                        "external_fact_id": f"event-{index + 1:03d}",
                        "code": quote.code,
                        "text": getattr(event, "title", ""),
                        "source": getattr(event, "source", ""),
                        "effective_at": getattr(event, "date", ""),
                    }
                    for index, event in enumerate(events or [])
                    if getattr(event, "title", "")
                ],
            )
            ai_error = None
            discussion_attempt = None
            formal_result = None
            evidence_aggregation = None
            try:
                if analyze_trade_observation_with_ai is _DEFAULT_RESEARCH_AI_HELPER:
                    if self.performance_store is not None:
                        formal_store = SqliteAgentExecutionStore(
                            str(self.performance_store.database.db_path)
                        )
                    else:
                        formal_store = MemoryAgentExecutionStore()
                    formal_result = run_formal_five_agent_analysis(
                        config,
                        evidence_package,
                        result_store=formal_store,
                    )
                    try:
                        evidence_aggregation = aggregate_agent_evidence(
                            evidence_package,
                            formal_result,
                            run_id=formal_result.run_id,
                            created_at=datetime.now().isoformat(timespec="seconds"),
                        )
                        if self.formal_evidence_store is not None:
                            try:
                                self.formal_evidence_store.save(
                                    evidence_package,
                                    evidence_aggregation,
                                    orchestration=formal_result,
                                )
                            except Exception as exc:
                                ai_error = f"F 层回放保存失败：{exc}"
                    except Exception as exc:
                        ai_error = f"F 层证据校验失败：{exc}"
                    verified_claim_ids = {
                        claim_id
                        for item in (getattr(evidence_aggregation, "summaries", ()) or ())
                        for claim_id in (
                            *getattr(item, "supporting_claim_ids", ()),
                            *getattr(item, "opposing_claim_ids", ()),
                        )
                    }
                    agent_summary = [
                        {
                            "agent_id": item.agent_id,
                            "status": item.status,
                            "outcome": item.outcome,
                            "claims": [
                                dict(claim)
                                for claim in (item.result.claims if item.result else ())
                                if str(claim.get("claim_id") or "") in verified_claim_ids
                            ],
                            "counter_claims": [
                                dict(claim)
                                for claim in (item.result.counter_claims if item.result else ())
                                if str(claim.get("claim_id") or "") in verified_claim_ids
                            ],
                            "missing_evidence": list(item.result.missing_evidence if item.result else ()),
                        }
                        for item in formal_result.executions
                    ]
                    ai_report = (
                        f"研究模式：{SCREENING_MODE_LABELS[research_mode]}\n"
                        "五 Agent 状态（研究旁路，不改变候选集合）：\n"
                        + format_agent_statuses(formal_result)
                        + "\n\n"
                        + json.dumps(
                            {
                                "orchestration_status": formal_result.status,
                                "ai_available": formal_result.ai_available,
                                "candidate_set_preserved": formal_result.candidate_set_preserved,
                                "evidence_summary_status": (
                                    evidence_aggregation.status if evidence_aggregation is not None else "failed"
                                ),
                                "agents": agent_summary,
                            },
                            ensure_ascii=False,
                            sort_keys=True,
                        )
                    )
                    if not formal_result.ai_available:
                        ai_error = "五 Agent AI 区域不可用，已保留确定性规则候选"
                else:
                    discussion_result = analyze_trade_observation_with_ai(
                        config,
                        quote,
                        technical,
                        money_flow,
                        sentiment,
                        events,
                        reference,
                        evidence_package=evidence_package,
                        return_attempt=True,
                    )
                    discussion_attempt = discussion_result if isinstance(discussion_result, DiscussionAttempt) else None
                    if discussion_attempt is None:
                        # Keep the existing UI/test seam: an injected compatibility
                        # helper may return the rendered report directly.
                        ai_report = str(discussion_result)
                    else:
                        if discussion_attempt.status != "ok" or discussion_attempt.report is None:
                            raise ValueError(discussion_attempt.error or "AI研究讨论失败")
                        ai_report = json.dumps(discussion_attempt.report.as_dict(), ensure_ascii=False, sort_keys=True)
                if self.performance_store is not None and discussion_attempt is not None:
                    self.performance_store.save_research_discussion_audit(
                        candidate_set_digest=stable_hash([quote.code]),
                        data_snapshot_id=manifest.snapshot_id,
                        refresh_event_id=refresh_event_id,
                        formula_version=discussion_formula_version,
                        evidence_hash=str(evidence_package["evidence_package_hash"]),
                        evidence_package_hash=str(evidence_package["evidence_package_hash"]),
                        run_id=str(getattr(discussion_attempt, "run_id", "")),
                        request_hash=str(getattr(discussion_attempt, "request_hash", "")),
                        evidence_package=evidence_package,
                        request_text=str(getattr(discussion_attempt, "request_text", "")),
                        raw_response=str(getattr(discussion_attempt, "raw_response", "")),
                        normalized_response=str(getattr(discussion_attempt, "normalized_response", "{}")),
                        prompt_version=str(getattr(discussion_attempt, "prompt_version", "research_discussion_v2")),
                        model_version=str(config.model or ""),
                        response_hash=str(getattr(discussion_attempt, "response_hash", "")) or stable_hash(ai_report),
                        status="ok",
                        cost_note=str(getattr(getattr(discussion_attempt, "report", None), "cost_note", "")),
                    )
            except Exception as exc:
                ai_report = None
                ai_error = str(exc)
                if self.performance_store is not None:
                    failed_attempt = locals().get("discussion_attempt")
                    self.performance_store.save_research_discussion_audit(
                        candidate_set_digest=stable_hash([quote.code]),
                        data_snapshot_id=manifest.snapshot_id,
                        refresh_event_id=refresh_event_id,
                        formula_version=discussion_formula_version,
                        evidence_hash=str(evidence_package["evidence_package_hash"]),
                        evidence_package_hash=str(evidence_package["evidence_package_hash"]),
                        run_id=str(getattr(failed_attempt, "run_id", "")),
                        request_hash=str(getattr(failed_attempt, "request_hash", "")),
                        evidence_package=evidence_package,
                        request_text=str(getattr(failed_attempt, "request_text", "")),
                        raw_response=str(getattr(failed_attempt, "raw_response", "")),
                        normalized_response=str(getattr(failed_attempt, "normalized_response", "{}")),
                        prompt_version=str(getattr(failed_attempt, "prompt_version", "research_discussion_v2")),
                        model_version=str(config.model or ""),
                        response_hash=str(getattr(failed_attempt, "response_hash", "")),
                        status="failed",
                        error_message=ai_error,
                    )
            return (
                quote,
                reference,
                technical,
                money_flow,
                sentiment,
                events,
                ai_report,
                ai_error,
                data_gaps,
                formal_result,
                evidence_aggregation,
                research_mode,
            )

        def done(result) -> None:
            if observer_version != self._observer_state_version:
                self._set_status(f"{base_quote.name} AI证据整理结果已过期，未覆盖当前股票")
                return
            (
                quote,
                reference,
                technical,
                money_flow,
                sentiment,
                events,
                ai_report,
                ai_error,
                data_gaps,
                formal_result,
                evidence_aggregation,
                research_mode,
            ) = result
            self.current_quote = quote
            self.current_trade_reference = reference
            self.current_agent_orchestration = formal_result
            self.current_evidence_aggregation = evidence_aggregation
            self._apply_agent_evidence_to_candidate(
                quote.code,
                formal_result,
                evidence_aggregation,
                base_data_gaps=data_gaps,
                research_mode=research_mode,
            )
            self._set_observation_save_block(None)
            self._render_trade_reference(quote, reference, technical, money_flow, sentiment, events, data_gaps, ai_report, ai_error)
            if ai_error:
                self._set_status(f"{quote.name} AI证据整理失败，已保留本地观察")
            else:
                self._set_status(f"{quote.name} AI证据整理已完成，费用以服务商账单为准")

        def failed(message: str) -> None:
            if observer_version != self._observer_state_version:
                return
            self._set_observation_save_block(None)
            self._set_status(f"{base_quote.name} AI证据整理失败，已保留本地观察")
            QMessageBox.warning(self, "提示", message)

        self._run_background(
            f"正在生成 {base_quote.name} AI证据整理...",
            task,
            done,
            task_key=task_key,
            duplicate_text=duplicate_text,
            failed=failed,
        )

    def save_tracking_task(self, task_type: str) -> None:
        if self._formal_candidate_research_context:
            self._set_status("正式候选研究页不写入兼容交易观察字段；请使用候选列表中的人工加入观察。")
            return
        if self._observation_save_block_message:
            QMessageBox.information(self, "提示", self._observation_save_block_message)
            self._set_status(self._observation_save_block_message)
            return
        if not self.current_quote or not self.current_trade_reference:
            QMessageBox.information(self, "提示", "请先生成本地观察")
            return
        ref = self.current_trade_reference
        created = self.review_service.create_tracking_task(
            code=self.current_quote.code,
            name=self.current_quote.name,
            task_type=task_type,
            entry_price=ref.entry_price,
            fail_price=ref.fail_price,
            target_price=ref.target_price,
            horizon_days=ref.horizon_days,
            due_date=due_date_from_now(ref.horizon_days),
            note=self.current_observation_note or ref.thesis,
            actor=getpass.getuser(),
        )
        label = "持仓" if task_type == "holding" else "观察"
        if not created.ok:
            self._set_status(f"{self.current_quote.name} 已有打开中的{label}任务，未重复保存")
        else:
            self._refresh_dashboard_snapshot()
            self._set_status(f"已保存{label}任务：{self.current_quote.name}")


def run_app(*, startup_probe=None) -> None:
    if (
        os.environ.get("STOCK_TOOL_TEST_MODE", "").strip() == "1"
        and os.environ.get("STOCK_TOOL_STARTUP_PROBE", "").strip() == "1"
        and os.environ.get("STOCK_TOOL_QT_FAULT_INJECTION", "").strip() == "init"
    ):
        raise RuntimeError("test-injected Qt initialization fault")
    app = QApplication.instance() or QApplication([])
    window = StockWorkbenchWindow()
    if startup_probe and startup_probe("window-created"):
        return
    window.show()
    if startup_probe and startup_probe("event-loop-ready"):
        return
    app.exec()
