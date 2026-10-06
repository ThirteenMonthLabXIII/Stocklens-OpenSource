"""Small, fixed A-share sample for Phase A data-source probes.

The list is intentionally not optimized for returns. It is a stratified probe
set covering large/mid/small caps, several industries, ChiNext/STAR-adjacent
style names, and delisted candidates for survivor-bias checks.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SampleStock:
    code: str
    name: str
    exchange: str
    bucket: str
    industry_hint: str
    active_hint: bool
    note: str

    @property
    def ak_symbol(self) -> str:
        return self.code

    @property
    def bs_code(self) -> str:
        return f"{self.exchange}.{self.code}"

    @property
    def ts_code(self) -> str:
        suffix = "SH" if self.exchange == "sh" else "SZ"
        return f"{self.code}.{suffix}"


SAMPLE_UNIVERSE: tuple[SampleStock, ...] = (
    # Large/liquid names with long histories and many corporate actions.
    SampleStock("600519", "贵州茅台", "sh", "large", "食品饮料", True, "dividend/blue-chip hfq check"),
    SampleStock("000858", "五粮液", "sz", "large", "食品饮料", True, "dividend/blue-chip hfq check"),
    SampleStock("601318", "中国平安", "sh", "large", "非银金融", True, "large financial"),
    SampleStock("600036", "招商银行", "sh", "large", "银行", True, "large financial"),
    SampleStock("000333", "美的集团", "sz", "large", "家用电器", True, "large manufacturing"),
    SampleStock("300750", "宁德时代", "sz", "large", "电力设备", True, "ChiNext large cap"),
    SampleStock("601899", "紫金矿业", "sh", "large", "有色金属", True, "cyclical/resource"),
    SampleStock("601088", "中国神华", "sh", "large", "煤炭", True, "high dividend resource"),
    SampleStock("600276", "恒瑞医药", "sh", "large", "医药生物", True, "healthcare"),
    SampleStock("002415", "海康威视", "sz", "large", "计算机", True, "technology hardware"),
    # Mid-cap and sector coverage.
    SampleStock("600309", "万华化学", "sh", "mid", "基础化工", True, "cyclical manufacturing"),
    SampleStock("600438", "通威股份", "sh", "mid", "电力设备", True, "solar chain"),
    SampleStock("002594", "比亚迪", "sz", "mid", "汽车", True, "auto/new energy"),
    SampleStock("000725", "京东方A", "sz", "mid", "电子", True, "panel/electronics"),
    SampleStock("002230", "科大讯飞", "sz", "mid", "计算机", True, "software/AI"),
    SampleStock("600887", "伊利股份", "sh", "mid", "食品饮料", True, "consumer staple"),
    SampleStock("600031", "三一重工", "sh", "mid", "机械设备", True, "machinery"),
    SampleStock("601012", "隆基绿能", "sh", "mid", "电力设备", True, "solar leader"),
    SampleStock("300760", "迈瑞医疗", "sz", "mid", "医药生物", True, "ChiNext healthcare"),
    SampleStock("603259", "药明康德", "sh", "mid", "医药生物", True, "CXO"),
    # Smaller or more volatile names to expose missingness and liquidity issues.
    SampleStock("300059", "东方财富", "sz", "small_mid", "非银金融", True, "internet broker"),
    SampleStock("002555", "三七互娱", "sz", "small_mid", "传媒", True, "gaming/media"),
    SampleStock("600079", "人福医药", "sh", "small_mid", "医药生物", True, "healthcare"),
    SampleStock("603019", "中科曙光", "sh", "small_mid", "计算机", True, "compute hardware"),
    SampleStock("002463", "沪电股份", "sz", "small_mid", "电子", True, "PCB/electronics"),
    SampleStock("000661", "长春高新", "sz", "small_mid", "医药生物", True, "biotech"),
    SampleStock("002352", "顺丰控股", "sz", "small_mid", "交通运输", True, "logistics"),
    SampleStock("300015", "爱尔眼科", "sz", "small_mid", "医药生物", True, "medical services"),
    SampleStock("600745", "闻泰科技", "sh", "small_mid", "电子", True, "semiconductor"),
    SampleStock("601615", "明阳智能", "sh", "small_mid", "电力设备", True, "wind power"),
    # Delisted/probe candidates. Names/codes are fixed to test source coverage,
    # not to imply tradability today.
    SampleStock("600087", "退市长油", "sh", "delisted", "交通运输", False, "delisted candidate"),
    SampleStock("000979", "中弘退", "sz", "delisted", "房地产", False, "delisted candidate"),
    SampleStock("600432", "退市吉恩", "sh", "delisted", "有色金属", False, "delisted candidate"),
    SampleStock("000585", "东电退", "sz", "delisted", "电力设备", False, "delisted candidate"),
    SampleStock("600485", "退市信威", "sh", "delisted", "通信", False, "delisted candidate"),
    SampleStock("000587", "金洲退", "sz", "delisted", "纺织服饰", False, "delisted candidate"),
    SampleStock("002260", "德奥退", "sz", "delisted", "家用电器", False, "delisted candidate"),
    SampleStock("300090", "盛运退", "sz", "delisted", "环保", False, "delisted candidate"),
    SampleStock("600146", "退市环球", "sh", "delisted", "商贸零售", False, "delisted candidate"),
    SampleStock("600701", "退市工新", "sh", "delisted", "综合", False, "delisted candidate"),
)


def active_sample(limit: int | None = None) -> list[SampleStock]:
    rows = [stock for stock in SAMPLE_UNIVERSE if stock.active_hint]
    return rows if limit is None else rows[:limit]


def delisted_sample(limit: int | None = None) -> list[SampleStock]:
    rows = [stock for stock in SAMPLE_UNIVERSE if not stock.active_hint]
    return rows if limit is None else rows[:limit]


def all_sample() -> list[SampleStock]:
    return list(SAMPLE_UNIVERSE)
