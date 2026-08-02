import calendar
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum

FORMULA_VERSION = "mvp1.1"
SEMANTICS_CHANGE_WARNING_THRESHOLD = Decimal("20")
POSITION_RANGES = ((1, 3), (4, 10), (11, 20), (21, 30), (31, 50), (51, 100))
ZERO = Decimal("0")
HUNDRED = Decimal("100")


def _decimal(value: Decimal | int | str) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _rounded(value: Decimal, places: str = "0.01") -> Decimal:
    return value.quantize(Decimal(places), rounding=ROUND_HALF_UP)


def shift_month(value: date, offset: int) -> date:
    absolute = value.year * 12 + value.month - 1 + offset
    year, month = divmod(absolute, 12)
    return date(year, month + 1, 1)


@dataclass(frozen=True)
class CalendarPeriod:
    start: date
    end: date

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1


@dataclass(frozen=True)
class ReportPeriods:
    report: CalendarPeriod
    previous: CalendarPeriod
    three_months: CalendarPeriod


def calendar_month(value: date) -> CalendarPeriod:
    start = value.replace(day=1)
    return CalendarPeriod(
        start, date(start.year, start.month, calendar.monthrange(start.year, start.month)[1])
    )


def calculate_periods(report_month: date) -> ReportPeriods:
    report = calendar_month(report_month)
    previous = calendar_month(shift_month(report.start, -1))
    return ReportPeriods(
        report, previous, CalendarPeriod(shift_month(report.start, -2), report.end)
    )


class ChangeKind(StrEnum):
    VALUE = "value"
    PERCENTAGE_POINTS = "percentage_points"


@dataclass(frozen=True)
class MetricChange:
    current: Decimal | None
    previous: Decimal | None
    absolute: Decimal | None
    relative_percent: Decimal | None
    percentage_points: Decimal | None
    relative_unavailable_reason: str | None


def calculate_change(current, previous, *, kind: ChangeKind = ChangeKind.VALUE) -> MetricChange:
    if current is None or previous is None:
        return MetricChange(current, previous, None, None, None, "missing_data")
    current, previous = _decimal(current), _decimal(previous)
    absolute = current - previous
    points = absolute if kind == ChangeKind.PERCENTAGE_POINTS else None
    if previous == ZERO:
        relative = ZERO if current == ZERO else None
        reason = None if current == ZERO else "zero_base"
    else:
        relative = _rounded(absolute / abs(previous) * HUNDRED)
        reason = None
    return MetricChange(current, previous, absolute, relative, points, reason)


@dataclass(frozen=True)
class PositionItem:
    query: str
    frequency: int
    position: int | None

    def __post_init__(self):
        if self.frequency is None or self.frequency < 0:
            raise ValueError("frequency must be a non-negative integer")


@dataclass(frozen=True)
class PositionDistribution:
    total: int
    ranges: Mapping[str, int]
    top_10: int
    top_30: int


def calculate_position_distribution(items: Iterable[PositionItem]) -> PositionDistribution:
    items = tuple(items)
    ranges = {
        f"{lower}-{upper}": sum(
            1 for item in items if item.position is not None and lower <= item.position <= upper
        )
        for lower, upper in POSITION_RANGES
    }
    return PositionDistribution(
        len(items),
        ranges,
        ranges["1-3"] + ranges["4-10"],
        sum(ranges[key] for key in ("1-3", "4-10", "11-20", "21-30")),
    )


@dataclass(frozen=True)
class SemanticsComparison:
    previous_count: int
    current_count: int
    added: tuple[str, ...]
    removed: tuple[str, ...]
    change_percent: Decimal
    warning: bool


def compare_semantics(
    previous: Iterable[str],
    current: Iterable[str],
    *,
    warning_threshold=SEMANTICS_CHANGE_WARNING_THRESHOLD,
) -> SemanticsComparison:
    old = {query.strip().casefold() for query in previous}
    new = {query.strip().casefold() for query in current}
    union = old | new
    percent = _rounded(Decimal(len(old ^ new)) / Decimal(len(union)) * HUNDRED) if union else ZERO
    return SemanticsComparison(
        len(old),
        len(new),
        tuple(sorted(new - old)),
        tuple(sorted(old - new)),
        percent,
        percent >= _decimal(warning_threshold),
    )


@dataclass(frozen=True)
class SourceTrafficFacts:
    total: Decimal | None
    shares: Mapping[str, Decimal | None]
    warning: str | None


def calculate_source_shares(total, sources: Mapping[str, Decimal | int]) -> SourceTrafficFacts:
    if total is None:
        return SourceTrafficFacts(None, {key: None for key in sources}, "missing_total")
    total = _decimal(total)
    values = {key: _decimal(value) for key, value in sources.items()}
    if total == ZERO:
        shares = {key: (ZERO if value == ZERO else None) for key, value in values.items()}
        warning = "nonzero_sources_with_zero_total" if any(values.values()) else None
    else:
        shares = {key: _rounded(value / total * HUNDRED) for key, value in values.items()}
        warning = "source_total_mismatch" if sum(values.values()) != total else None
    return SourceTrafficFacts(total, shares, warning)


@dataclass(frozen=True)
class CtrCheck:
    reported: Decimal | None
    calculated: Decimal | None
    valid: bool | None
    warning: str | None


def check_ctr(clicks, impressions, reported_ctr, *, tolerance=Decimal("0.01")) -> CtrCheck:
    if clicks is None or impressions is None or reported_ctr is None:
        return CtrCheck(reported_ctr, None, None, "missing_data")
    clicks, impressions, reported = map(_decimal, (clicks, impressions, reported_ctr))
    if impressions == ZERO:
        return CtrCheck(reported, None, None, "zero_impressions")
    calculated = _rounded(clicks / impressions * HUNDRED)
    valid = abs(calculated - reported) <= _decimal(tolerance)
    return CtrCheck(reported, calculated, valid, None if valid else "ctr_arithmetic_mismatch")


def normalize_count_per_day(value, period: CalendarPeriod) -> Decimal | None:
    return None if value is None else _rounded(_decimal(value) / Decimal(period.days), "0.0001")
