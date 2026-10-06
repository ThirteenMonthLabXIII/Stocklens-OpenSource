"""A-share UniverseProfile and board-specific qualification gates.

Phase 0.5 keeps the qualification gate local and versioned.  The functions in
this module accept either mappings or the project's snapshot dataclasses so
that offline fixtures and production adapters use the same rules.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
import math
from statistics import median
from typing import Any, Iterable, Mapping

from stock_tool.core.screening_contract import stable_hash


UNIVERSE_PROFILE_VERSION = "universe.v1"
BOARD_PROFILE_VERSION = "board.v1"
UNIVERSE_MEMBER_PROFILE_VERSION = "universe_member.v1"
UNIVERSE_MODES = frozenset({"formal", "exploratory", "review_only"})
UNIVERSE_STATUSES = frozenset({"eligible", "excluded", "insufficient", "stale"})
MEMBER_STATUS_POLICIES = frozenset({"listed_non_st", "listed_verified"})


def _text(value: Any) -> str:
    return str(value or "").strip()


def _get(item: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        if isinstance(item, Mapping) and name in item:
            value = item.get(name)
        else:
            value = getattr(item, name, None)
        if value is not None and value != "":
            return value
    return default


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return _text(value).casefold() in {"1", "true", "yes", "y", "是", "停牌", "locked", "lock"}


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _parse_date(value: Any) -> date | None:
    text = _text(value)
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
        except ValueError:
            return None


def _date_list(value: Any) -> list[date]:
    if isinstance(value, str):
        values = [part.strip() for part in value.split(",") if part.strip()]
    elif isinstance(value, Iterable) and not isinstance(value, Mapping):
        values = list(value)
    else:
        values = []
    return [parsed for item in values if (parsed := _parse_date(item)) is not None]


def _unique_texts(values: Iterable[Any]) -> tuple[str, ...]:
    result: list[str] = []
    for value in values:
        text = _text(value)
        if text and text not in result:
            result.append(text)
    return tuple(result)


@dataclass(frozen=True)
class UniverseMemberProfile:
    """Strongly typed member-level identity and status facts."""

    code: str = ""
    name: str = ""
    board_id: str = ""
    exchange: str = ""
    listing_date: str = ""
    first_trade_date: str = ""
    security_status: str = ""
    security_status_provided: bool = False
    is_st: bool | None = None
    is_delisting: bool | None = None
    is_suspended: bool | None = None
    snapshot_id: str = ""
    available_at: str = ""
    as_of: str = ""
    snapshot_quality_status: str = ""
    valid_listing_trading_days: int | None = None
    valid_turnover_days_20d: int | None = None
    valid_price_volume_days: int | None = None
    median_turnover_value_20d: float | None = None
    current_price: float | None = None
    contract_version: str = UNIVERSE_MEMBER_PROFILE_VERSION

    def __post_init__(self) -> None:
        if self.contract_version != UNIVERSE_MEMBER_PROFILE_VERSION:
            raise ValueError(f"不支持的 UniverseMemberProfile 版本：{self.contract_version}")
        code = _text(self.code)
        object.__setattr__(self, "code", code.zfill(6) if code.isdigit() else code)

    @classmethod
    def from_mapping(cls, item: Any) -> "UniverseMemberProfile":
        if isinstance(item, cls):
            return item
        if isinstance(item, Mapping):
            raw = dict(item)
            provided = "security_status" in raw and _text(raw.get("security_status")) != ""
        else:
            raw = {name: getattr(item, name, None) for name in cls.__dataclass_fields__ if name != "contract_version"}
            provided = bool(raw.get("security_status"))
        return cls(
            code=raw.get("code") or raw.get("symbol") or "",
            name=raw.get("name") or raw.get("stock_name") or "",
            board_id=raw.get("board_id") or raw.get("board") or "",
            exchange=raw.get("exchange") or "",
            listing_date=raw.get("listing_date") or raw.get("listed_at") or "",
            first_trade_date=raw.get("first_trade_date") or "",
            security_status=raw.get("security_status") or raw.get("stock_status") or raw.get("status") or raw.get("listing_status") or "",
            security_status_provided=provided,
            is_st=raw.get("is_st"),
            is_delisting=raw.get("is_delisting") if raw.get("is_delisting") is not None else raw.get("is_delisted"),
            is_suspended=(
                raw.get("is_suspended")
                if raw.get("is_suspended") is not None
                else raw.get("suspended") if raw.get("suspended") is not None
                else raw.get("halted")
            ),
            snapshot_id=raw.get("snapshot_id") or raw.get("data_snapshot_id") or "",
            available_at=raw.get("available_at") or raw.get("fact_available_at") or "",
            as_of=raw.get("as_of") or raw.get("data_date") or "",
            snapshot_quality_status=raw.get("snapshot_quality_status") or raw.get("data_quality_status") or raw.get("quality_status") or "",
            valid_listing_trading_days=(
                raw.get("valid_listing_trading_days")
                if raw.get("valid_listing_trading_days") is not None
                else raw.get("listing_age_trading_days")
            ),
            valid_turnover_days_20d=(
                raw.get("valid_turnover_days_20d")
                if raw.get("valid_turnover_days_20d") is not None
                else raw.get("valid_liquidity_days")
            ),
            valid_price_volume_days=(
                raw.get("valid_price_volume_days")
                if raw.get("valid_price_volume_days") is not None
                else raw.get("price_volume_valid_days")
            ),
            median_turnover_value_20d=(
                raw.get("median_turnover_value_20d")
                if raw.get("median_turnover_value_20d") is not None
                else raw.get("turnover_value_median_20d")
            ),
            current_price=(
                raw.get("current_price")
                if raw.get("current_price") is not None
                else raw.get("price") if raw.get("price") is not None
                else raw.get("last_price")
            ),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "code": self.code,
            "name": self.name,
            "board_id": self.board_id,
            "exchange": self.exchange,
            "listing_date": self.listing_date,
            "first_trade_date": self.first_trade_date,
            "security_status": self.security_status,
            "security_status_provided": self.security_status_provided,
            "is_st": self.is_st,
            "is_delisting": self.is_delisting,
            "is_suspended": self.is_suspended,
            "snapshot_id": self.snapshot_id,
            "available_at": self.available_at,
            "as_of": self.as_of,
            "snapshot_quality_status": self.snapshot_quality_status,
            "valid_listing_trading_days": self.valid_listing_trading_days,
            "valid_turnover_days_20d": self.valid_turnover_days_20d,
            "valid_price_volume_days": self.valid_price_volume_days,
            "median_turnover_value_20d": self.median_turnover_value_20d,
            "current_price": self.current_price,
        }


@dataclass(frozen=True)
class BoardProfile:
    board_id: str
    exchange: str
    board_name: str
    listing_rule_version: str
    trading_calendar_version: str
    price_limit_rule_id: str
    liquidity_policy_id: str
    technical_policy_id: str
    industry_policy_id: str
    effective_at: str
    price_limit_rule_version: str = ""
    contract_version: str = BOARD_PROFILE_VERSION

    def __post_init__(self) -> None:
        if self.contract_version != BOARD_PROFILE_VERSION:
            raise ValueError(f"不支持的 BoardProfile 版本：{self.contract_version}")
        for name in (
            "board_id", "exchange", "board_name", "listing_rule_version",
            "trading_calendar_version", "price_limit_rule_id", "liquidity_policy_id",
            "technical_policy_id", "industry_policy_id", "effective_at",
        ):
            if not _text(getattr(self, name)):
                raise ValueError(f"BoardProfile.{name} 不能为空")
        if not _text(self.price_limit_rule_version):
            object.__setattr__(self, "price_limit_rule_version", f"{self.price_limit_rule_id}.v1")

    @property
    def profile_id(self) -> str:
        return f"{self.board_id}:{self.contract_version}"

    def as_dict(self) -> dict[str, str]:
        return {
            "contract_version": self.contract_version,
            "board_id": self.board_id,
            "exchange": self.exchange,
            "board_name": self.board_name,
            "listing_rule_version": self.listing_rule_version,
            "trading_calendar_version": self.trading_calendar_version,
            "price_limit_rule_id": self.price_limit_rule_id,
            "price_limit_rule_version": self.price_limit_rule_version,
            "liquidity_policy_id": self.liquidity_policy_id,
            "technical_policy_id": self.technical_policy_id,
            "industry_policy_id": self.industry_policy_id,
            "effective_at": self.effective_at,
            "profile_id": self.profile_id,
        }


def _board(board_id: str, exchange: str, board_name: str) -> BoardProfile:
    return BoardProfile(
        board_id=board_id,
        exchange=exchange,
        board_name=board_name,
        listing_rule_version=f"listing.{board_id}.v1",
        trading_calendar_version=f"calendar.{exchange.lower()}.v1",
        price_limit_rule_id=f"price_limit.{board_id}.v1",
        liquidity_policy_id=f"liquidity.{board_id}.v1",
        technical_policy_id=f"technical.{board_id}.v1",
        industry_policy_id=f"industry.{board_id}.v1",
        effective_at="2026-01-01T00:00:00+08:00",
        price_limit_rule_version=f"price_limit.{board_id}.v1",
    )


DEFAULT_BOARD_PROFILES = (
    _board("main_board", "SSE/SZSE", "主板"),
    _board("chinext", "SZSE", "创业板"),
    _board("star_market", "SSE", "科创板"),
    _board("beijing", "BSE", "北交所"),
)
DEFAULT_BOARD_PROFILE_MAP = {item.board_id: item for item in DEFAULT_BOARD_PROFILES}


@dataclass(frozen=True)
class UniverseProfile:
    universe_id: str = "a_share_all"
    universe_version: str = "v1"
    board_profiles: tuple[BoardProfile, ...] = DEFAULT_BOARD_PROFILES
    member_contract_version: str = UNIVERSE_MEMBER_PROFILE_VERSION
    contract_version: str = UNIVERSE_PROFILE_VERSION

    def __post_init__(self) -> None:
        if self.contract_version != UNIVERSE_PROFILE_VERSION:
            raise ValueError(f"不支持的 UniverseProfile 版本：{self.contract_version}")
        if not _text(self.universe_id) or not _text(self.universe_version):
            raise ValueError("UniverseProfile 身份不能为空")
        if self.member_contract_version != UNIVERSE_MEMBER_PROFILE_VERSION:
            raise ValueError(f"不支持的 UniverseMemberProfile 版本：{self.member_contract_version}")
        boards = tuple(self.board_profiles)
        if {item.board_id for item in boards} != {"main_board", "chinext", "star_market", "beijing"}:
            raise ValueError("UniverseProfile v1 必须包含四个独立板块规则身份")
        object.__setattr__(self, "board_profiles", boards)

    @property
    def members_digest(self) -> str:
        return stable_hash([item.as_dict() for item in sorted(self.board_profiles, key=lambda value: value.board_id)])

    @property
    def boards(self) -> dict[str, BoardProfile]:
        return {item.board_id: item for item in self.board_profiles}

    @classmethod
    def default(cls) -> "UniverseProfile":
        return cls()

    def board(self, board_id: str) -> BoardProfile | None:
        wanted = _text(board_id)
        return next((item for item in self.board_profiles if item.board_id == wanted), None)

    def as_dict(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "universe_id": self.universe_id,
            "universe_version": self.universe_version,
            "member_contract_version": self.member_contract_version,
            "members_digest": self.members_digest,
            "board_profiles": [item.as_dict() for item in sorted(self.board_profiles, key=lambda value: value.board_id)],
        }


DEFAULT_UNIVERSE_PROFILE = UniverseProfile()


@dataclass(frozen=True)
class UniverseDecision:
    code: str
    mode: str
    status: str
    research_eligible: bool
    exploratory_only: bool
    board_id: str = ""
    board_profile: BoardProfile | None = None
    security_status: str = ""
    is_st: bool = False
    is_delisting: bool = False
    is_suspended: bool = False
    valid_listing_trading_days: int | None = None
    reason_codes: tuple[str, ...] = ()
    missing_fields: tuple[str, ...] = ()
    stale_fields: tuple[str, ...] = ()
    blocking_fields: tuple[str, ...] = ()
    facts: dict[str, Any] = field(default_factory=dict)
    universe_id: str = "a_share_all"
    universe_version: str = "v1"
    member_status_policy: str = "listed_non_st"

    def __post_init__(self) -> None:
        if self.mode not in UNIVERSE_MODES:
            raise ValueError(f"未知筛选模式：{self.mode}")
        if self.status not in UNIVERSE_STATUSES:
            raise ValueError(f"未知 Universe 状态：{self.status}")
        for name in ("reason_codes", "missing_fields", "stale_fields", "blocking_fields"):
            object.__setattr__(self, name, _unique_texts(getattr(self, name)))
        object.__setattr__(self, "facts", dict(self.facts))

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "mode": self.mode,
            "status": self.status,
            "research_eligible": self.research_eligible,
            "exploratory_only": self.exploratory_only,
            "board_id": self.board_id,
            "board_profile": self.board_profile.as_dict() if self.board_profile else None,
            "security_status": self.security_status,
            "is_st": self.is_st,
            "is_delisting": self.is_delisting,
            "is_suspended": self.is_suspended,
            "valid_listing_trading_days": self.valid_listing_trading_days,
            "reason_codes": list(self.reason_codes),
            "missing_fields": list(self.missing_fields),
            "stale_fields": list(self.stale_fields),
            "blocking_fields": list(self.blocking_fields),
            "facts": dict(self.facts),
            "universe_id": self.universe_id,
            "universe_version": self.universe_version,
            "member_status_policy": self.member_status_policy,
        }


def _listing_days(item: Any, *, as_of: date | None, trading_calendar: Iterable[Any] | None) -> int | None:
    direct = _get(
        item,
        "valid_listing_trading_days", "listing_trading_days", "trading_days_since_listing",
        "listing_age_trading_days", "listing_trade_days",
    )
    number = _number(direct)
    if number is not None:
        return max(0, int(number))
    direct_dates = _date_list(direct)
    if direct_dates:
        return len(direct_dates)
    listing_date = _parse_date(_get(item, "listing_date", "listed_at"))
    calendar = _date_list(trading_calendar)
    if listing_date and as_of and calendar:
        return len({day for day in calendar if listing_date < day <= as_of})
    return None


def _liquidity_stats(item: Any) -> tuple[int | None, float | None, int | None]:
    valid_turnover = _number(_get(item, "valid_turnover_days_20d", "valid_liquidity_days", "effective_turnover_days"))
    turnover_values = _get(item, "turnover_values_20d", "turnover_value_history_20d", "turnover_history")
    values: list[float] = []
    if isinstance(turnover_values, Iterable) and not isinstance(turnover_values, (str, bytes, Mapping)):
        values = [number for value in turnover_values if (number := _number(value)) is not None]
    if valid_turnover is None and values:
        valid_turnover = len(values)
    median_value = _number(_get(item, "median_turnover_value_20d", "turnover_value_median_20d", "median_turnover_20d"))
    if median_value is None and values:
        median_value = float(median(values))
    valid_price_volume = _number(_get(item, "valid_price_volume_days", "price_volume_valid_days"))
    if valid_price_volume is None:
        price_values = _get(item, "price_volume_history_20d", "price_history_20d")
        if isinstance(price_values, Iterable) and not isinstance(price_values, (str, bytes, Mapping)):
            valid_price_volume = float(sum(_number(value) is not None for value in price_values))
    return (
        int(valid_turnover) if valid_turnover is not None else None,
        median_value,
        int(valid_price_volume) if valid_price_volume is not None else None,
    )


def _status_flags(item: Any, *, member_status_policy: str) -> tuple[bool, str]:
    if member_status_policy not in MEMBER_STATUS_POLICIES:
        raise ValueError(f"未知 member_status_policy：{member_status_policy}")
    if member_status_policy == "listed_verified" and not _bool(_get(item, "security_status_provided", default=False)):
        return True, "UNKNOWN_STOCK_STATUS"
    name = _text(_get(item, "name", "stock_name"))
    raw_status = _text(_get(item, "security_status", "stock_status", "status", "listing_status"))
    upper = f"{name} {raw_status}".upper()
    if _bool(_get(item, "is_st", default=False)) or name.upper().startswith(("*ST", "ST")) or "*ST" in upper or "ST" in upper.split():
        return True, "EXCLUDED_ST"
    if _bool(_get(item, "is_delisting", "is_delisted", default=False)):
        return True, "EXCLUDED_DELISTING"
    if any(token in raw_status.casefold() for token in ("退市整理", "delisting", "delisted", "terminated", "已确认退市")):
        return True, "EXCLUDED_DELISTING"
    if not raw_status and not _bool(_get(item, "is_active", default=False)):
        return True, "UNKNOWN_STOCK_STATUS"
    if raw_status.casefold() in {"st", "*st", "退市整理", "退市", "delisting", "delisted", "terminated"}:
        return True, "EXCLUDED_STOCK_STATUS"
    if raw_status.casefold() not in {"listed", "normal", "active", "trading", "在市", "正常", "上市"}:
        return True, "UNKNOWN_STOCK_STATUS"
    return False, ""


def evaluate_universe_member(
    item: Any,
    *,
    profile: UniverseProfile = DEFAULT_UNIVERSE_PROFILE,
    mode: str = "formal",
    as_of: str = "",
    availability_cutoff: str = "",
    trading_calendar: Iterable[Any] | None = None,
    member_status_policy: str = "listed_non_st",
) -> UniverseDecision:
    """Evaluate one stock against the frozen A-share qualification gate."""

    if mode not in UNIVERSE_MODES:
        raise ValueError(f"未知筛选模式：{mode}")
    if member_status_policy not in MEMBER_STATUS_POLICIES:
        raise ValueError(f"未知 member_status_policy：{member_status_policy}")
    member = UniverseMemberProfile.from_mapping(item)
    code = _text(_get(member, "code", "symbol"))
    board_id = _text(_get(member, "board_id", "board"))
    board = profile.board(board_id)
    security_status = _text(_get(member, "security_status", "stock_status", "status", "listing_status"))
    security_status_folded = security_status.casefold()
    is_st = (
        _bool(_get(member, "is_st", default=False))
        or _text(_get(member, "name", "stock_name")).upper().startswith(("ST", "*ST"))
        or security_status_folded in {"st", "*st"}
    )
    is_delisting = (
        _bool(_get(member, "is_delisting", "is_delisted", default=False))
        or any(token in security_status_folded for token in ("退市整理", "delisting", "delisted", "terminated", "已确认退市"))
    )
    reasons: list[str] = []
    missing: list[str] = []
    blocking: list[str] = []
    facts: dict[str, Any] = {}
    if not code or len(code) != 6 or not code.isdigit():
        reasons.append("INVALID_CODE")
    if not _text(_get(member, "name", "stock_name")):
        missing.append("name")
        reasons.append("SNAPSHOT_IDENTITY_INCOMPLETE")
    if not board_id or board is None:
        missing.append("board_id")
        reasons.append("BOARD_PROFILE_MISSING")
    snapshot_id = _text(_get(member, "snapshot_id", "data_snapshot_id"))
    if not snapshot_id:
        missing.append("snapshot_id")
        reasons.append("SNAPSHOT_IDENTITY_INCOMPLETE")
    blocking.extend(missing)
    available_at = _parse_date(_get(member, "available_at", "fact_available_at"))
    cutoff = _parse_date(availability_cutoff)
    if available_at is None:
        missing.append("available_at")
        reasons.append("AVAILABLE_AT_MISSING")
        blocking.append("available_at")
    elif cutoff and available_at > cutoff:
        reasons.append("FUTURE_AVAILABLE_AT")
        blocking.append("available_at")
    snapshot_quality = _text(_get(member, "snapshot_quality_status", "data_quality_status", "quality_status"))
    if not snapshot_quality:
        missing.append("snapshot_quality_status")
        reasons.append("SNAPSHOT_QUALITY_MISSING")
    if snapshot_quality.casefold() == "blocked":
        reasons.append("SNAPSHOT_QUALITY_BLOCKED")
        blocking.append("snapshot_quality_status")
    status_excluded, status_reason = _status_flags(member, member_status_policy=member_status_policy)
    if status_excluded:
        reasons.append(status_reason)
    suspended = _bool(_get(member, "is_suspended", "suspended", "halted", "trading_halted", default=False))
    price = _number(_get(member, "current_price", "price", "last_price"))
    if suspended and price is None:
        reasons.append("SUSPENDED_NO_VALID_PRICE")
    elif suspended:
        reasons.append("CURRENTLY_SUSPENDED")

    as_of_date = _parse_date(as_of) or cutoff or _parse_date(_get(member, "as_of", "data_date"))
    listing_days = _listing_days(member, as_of=as_of_date, trading_calendar=trading_calendar)
    if listing_days is None:
        missing.append("listing_age_trading_days")
        reasons.append("LISTING_AGE_MISSING")
    else:
        facts["listing_age_trading_days"] = listing_days
        minimum = 120 if mode == "formal" else 20
        if listing_days < minimum:
            reasons.append("NEW_LISTING_FORMAL_BLOCK" if mode == "formal" else "NEW_LISTING_EXPLORATORY_INSUFFICIENT")

    valid_turnover, median_turnover, valid_price_volume = _liquidity_stats(member)
    facts.update(
        {
            "valid_turnover_days_20d": valid_turnover,
            "median_turnover_value_20d": median_turnover,
            "valid_price_volume_days": valid_price_volume,
        }
    )
    required_turnover = 15 if mode == "formal" else 10
    required_price_volume = 15 if mode == "formal" else 10
    min_turnover_value = 20_000_000 if mode == "formal" else 5_000_000
    if valid_turnover is None or valid_price_volume is None or median_turnover is None:
        missing.extend(
            field
            for field, value in (
                ("valid_turnover_days_20d", valid_turnover),
                ("valid_price_volume_days", valid_price_volume),
                ("median_turnover_value_20d", median_turnover),
            )
            if value is None
        )
        reasons.append("LIQUIDITY_SAMPLE_INSUFFICIENT")
    else:
        if valid_turnover < required_turnover:
            reasons.append("LIQUIDITY_TURNOVER_DAYS_INSUFFICIENT")
        if valid_price_volume < required_price_volume:
            reasons.append("LIQUIDITY_PRICE_VOLUME_DAYS_INSUFFICIENT")
        if median_turnover < min_turnover_value:
            reasons.append("LIQUIDITY_MEDIAN_TURNOVER_BELOW_THRESHOLD")

    if board:
        facts.update(
            {
                "price_limit_rule_id": board.price_limit_rule_id,
                "price_limit_rule_version": board.price_limit_rule_version,
                "listing_rule_version": board.listing_rule_version,
                "trading_calendar_version": board.trading_calendar_version,
                "industry_classification_id": _text(_get(member, "industry_classification_id", "industry_classification")),
                "industry_classification_version": _text(_get(member, "industry_classification_version", "industry_version")),
            }
        )
    facts["member_status_policy"] = member_status_policy

    explicit_exclusion = {
        "INVALID_CODE", "EXCLUDED_ST", "EXCLUDED_DELISTING", "EXCLUDED_STOCK_STATUS", "UNKNOWN_STOCK_STATUS",
        "FUTURE_AVAILABLE_AT", "SNAPSHOT_QUALITY_BLOCKED", "SUSPENDED_NO_VALID_PRICE",
        "NEW_LISTING_FORMAL_BLOCK", "LIQUIDITY_TURNOVER_DAYS_INSUFFICIENT",
        "LIQUIDITY_PRICE_VOLUME_DAYS_INSUFFICIENT", "LIQUIDITY_MEDIAN_TURNOVER_BELOW_THRESHOLD",
    }
    if any(reason in explicit_exclusion for reason in reasons):
        status = "excluded"
    elif missing or reasons:
        status = "insufficient"
    else:
        status = "eligible"
    if mode == "review_only" and status == "eligible":
        status = "stale"
        reasons.append("REVIEW_ONLY_NOT_FORMAL")
    research_eligible = status == "eligible" and mode == "formal"
    return UniverseDecision(
        code=code.zfill(6) if code.isdigit() else code,
        mode=mode,
        status=status,
        research_eligible=research_eligible,
        exploratory_only=mode != "formal",
        board_id=board_id,
        board_profile=board,
        security_status=security_status,
        is_st=is_st,
        is_delisting=is_delisting,
        is_suspended=suspended,
        valid_listing_trading_days=listing_days,
        reason_codes=tuple(dict.fromkeys(reasons)),
        missing_fields=tuple(dict.fromkeys(missing)),
        blocking_fields=tuple(dict.fromkeys(blocking)),
        facts=facts,
        universe_id=profile.universe_id,
        universe_version=profile.universe_version,
        member_status_policy=member_status_policy,
    )


def evaluate_universe(
    items: Iterable[Any],
    *,
    profile: UniverseProfile = DEFAULT_UNIVERSE_PROFILE,
    mode: str = "formal",
    as_of: str = "",
    availability_cutoff: str = "",
    trading_calendar: Iterable[Any] | None = None,
    member_status_policy: str = "listed_non_st",
) -> tuple[UniverseDecision, ...]:
    """Evaluate a stable, code-sorted Universe batch."""

    decisions = [
        evaluate_universe_member(
            item,
            profile=profile,
            mode=mode,
            as_of=as_of,
            availability_cutoff=availability_cutoff,
            trading_calendar=trading_calendar,
            member_status_policy=member_status_policy,
        )
        for item in items
    ]
    return tuple(sorted(decisions, key=lambda item: item.code.zfill(6)))


# Readable aliases for adapters and future callers.
check_universe_eligibility = evaluate_universe_member
evaluate_universe_profile = evaluate_universe
evaluate_member = evaluate_universe_member


def get_board_profile(board_id: str) -> BoardProfile | None:
    return DEFAULT_UNIVERSE_PROFILE.board(board_id)


__all__ = [
    "BOARD_PROFILE_VERSION",
    "DEFAULT_BOARD_PROFILES",
    "DEFAULT_BOARD_PROFILE_MAP",
    "DEFAULT_UNIVERSE_PROFILE",
    "UNIVERSE_MODES",
    "MEMBER_STATUS_POLICIES",
    "UNIVERSE_MEMBER_PROFILE_VERSION",
    "UNIVERSE_PROFILE_VERSION",
    "BoardProfile",
    "UniverseDecision",
    "UniverseMemberProfile",
    "UniverseProfile",
    "check_universe_eligibility",
    "evaluate_universe",
    "evaluate_universe_member",
    "evaluate_universe_profile",
    "evaluate_member",
    "get_board_profile",
]
