import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from urllib.parse import urlparse

from django.db import transaction

from .models import ReportDatasetSnapshot, ValidationIssue
from .services import snapshot_checksum

NUMBER_RE = re.compile(r"(?<![\w])[-+]?\d+(?:[.,]\d+)?")
PLACEHOLDER_RE = re.compile(r"(?:\{\{?[^{}]+\}?\}|\[\[[^]]+]])")
SECRET_RE = re.compile(
    r"(?i)(?:authorization\s*:\s*(?:bearer|basic|oauth)\s+\S{8,}|"
    r"sk-[a-z0-9_-]{12,}|gh[pousr]_[a-z0-9]{20,})"
)
SECRET_FIELD_RE = re.compile(
    r"(?i)^(?:api[_ -]?key|oauth[_ -]?token|access[_ -]?token|"
    r"refresh[_ -]?token|authorization)$"
)
SAFE_SECRET_VALUES = {"", "disabled", "none", "null", "redacted", "masked", "не настроено"}
RANGES = {
    "1-3": (1, 3),
    "4-10": (4, 10),
    "11-20": (11, 20),
    "21-30": (21, 30),
    "31-50": (31, 50),
    "51-100": (51, 100),
}
REQUIRED_FIELDS = (
    "schema_version",
    "formula_version",
    "project",
    "periods",
    "ranking_sources",
    "source_snapshots",
    "calculated",
    "completed_work",
)


@dataclass(frozen=True)
class PublicationReadiness:
    has_errors: bool
    has_warnings: bool
    can_export_draft: bool
    can_publish: bool


def _decimal(value):
    try:
        return Decimal(str(value).replace(",", "."))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _all_scalars(value):
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from _all_scalars(item)
    elif isinstance(value, list):
        for item in value:
            yield from _all_scalars(item)
    else:
        yield value


def _all_values(value):
    if isinstance(value, dict):
        for item in value.values():
            yield from _all_values(item)
    elif isinstance(value, list):
        for item in value:
            yield from _all_values(item)
    else:
        yield value


def _contains_secret(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if SECRET_FIELD_RE.fullmatch(str(key).strip()):
                rendered = str(item or "").strip()
                if rendered.casefold() not in SAFE_SECRET_VALUES and len(rendered) >= 8:
                    return True
            if _contains_secret(item):
                return True
        return False
    if isinstance(value, list | tuple | set):
        return any(_contains_secret(item) for item in value)
    return value is not None and bool(SECRET_RE.search(str(value)))


def _host_matches(url, domain):
    if not url:
        return True
    candidate = str(url)
    if not candidate.startswith(("http://", "https://")):
        return True
    host = (urlparse(candidate).hostname or "").lower().rstrip(".")
    expected = str(domain or "").lower().rstrip(".")
    return host == expected or host.endswith("." + expected)


def _numbers(value):
    result = set()
    for scalar in _all_scalars(value):
        if isinstance(scalar, bool) or scalar is None:
            continue
        for token in NUMBER_RE.findall(str(scalar)):
            number = _decimal(token)
            if number is not None:
                result.add(abs(number))
    return result


def _issue(items, code, severity, message, *, section="", details=None):
    items.append(
        ValidationIssue(
            code=code,
            severity=severity,
            section_code=section,
            message=message,
            details=details or {},
        )
    )


def _validate_positions(payload, issues):
    segments = payload.get("calculated", {}).get("positions", {}).get("segments", [])
    if not segments:
        _issue(
            issues,
            "section_data_missing",
            "warning",
            "Отсутствуют данные для раздела позиций.",
            section="position_distribution",
        )
    for index, segment in enumerate(segments):
        depth = segment.get("ranking_depth")
        distribution = segment.get("distribution") or {}
        ranges = distribution.get("ranges") or {}
        details = {"segment": index, "ranking_depth": depth, "ranges": ranges}
        for name in ranges:
            upper = RANGES.get(name, (0, 10**9))[1]
            if depth is not None and upper > depth:
                _issue(
                    issues,
                    "range_exceeds_ranking_depth",
                    "error",
                    "Диапазон позиций глубже фактической глубины проверки.",
                    section="position_distribution",
                    details={**details, "range": name},
                )
        if depth is not None and depth < 30 and distribution.get("top_30") is not None:
            _issue(
                issues,
                "top_30_unconfirmed",
                "error",
                "TOP-30 присутствует при глубине проверки меньше 30.",
                section="top_10",
                details=details,
            )
        total_in_ranges = sum(value for value in ranges.values() if isinstance(value, int))
        total = distribution.get("total")
        if total is not None and (
            total_in_ranges > total or any(not isinstance(v, int) or v < 0 for v in ranges.values())
        ):
            _issue(
                issues,
                "position_ranges_arithmetic",
                "error",
                "Сумма позиционных диапазонов арифметически некорректна.",
                section="position_distribution",
                details={**details, "range_sum": total_in_ranges, "total": total},
            )
        expected_10 = ranges.get("1-3", 0) + ranges.get("4-10", 0)
        if distribution.get("top_10") != expected_10:
            _issue(
                issues,
                "top_10_mismatch",
                "error",
                "TOP-10 не совпадает с суммой соответствующих диапазонов.",
                section="top_10",
                details={**details, "expected": expected_10, "actual": distribution.get("top_10")},
            )
        if depth is not None and depth >= 30:
            expected_30 = expected_10 + ranges.get("11-20", 0) + ranges.get("21-30", 0)
            if distribution.get("top_30") != expected_30:
                _issue(
                    issues,
                    "top_30_mismatch",
                    "error",
                    "TOP-30 не совпадает с суммой соответствующих диапазонов.",
                    section="top_10",
                    details={
                        **details,
                        "expected": expected_30,
                        "actual": distribution.get("top_30"),
                    },
                )
        for warning in segment.get("warnings", []):
            if warning.get("code") == "ranking_depth_changed":
                _issue(
                    issues,
                    "ranking_depth_changed",
                    "warning",
                    "Изменилась глубина проверки позиций.",
                    section="position_dynamics",
                    details=warning,
                )
        semantics = segment.get("semantics") or {}
        if semantics.get("warning"):
            _issue(
                issues,
                "tracked_queries_changed",
                "warning",
                "Существенно изменился состав отслеживаемых запросов.",
                section="position_dynamics",
                details=semantics,
            )
        if segment.get("comparison_distributions") is None:
            _issue(
                issues,
                "previous_period_missing",
                "warning",
                "Отсутствует предыдущий период.",
                section="position_dynamics",
                details={"segment": index},
            )


def _validate_sources(payload, issues):
    raw_sources = payload.get("source_snapshots", [])
    present = {item.get("source") for item in raw_sources}
    for required in ("yandex_metrika", "yandex_webmaster"):
        if required not in present:
            _issue(
                issues,
                "required_source_missing",
                "warning",
                "Отсутствуют данные обязательного источника.",
                section=required,
                details={"source": required},
            )
    calculated = payload.get("calculated", {}).get("sources", {}).get("sources", {})
    webmaster = calculated.get("yandex_webmaster", {})
    check = webmaster.get("ctr_check") or {}
    reported = _decimal(check.get("reported"))
    if reported is not None and not Decimal("0") <= reported <= Decimal("100"):
        _issue(
            issues,
            "ctr_out_of_range",
            "error",
            "CTR находится вне диапазона 0–100%.",
            section="ctr",
            details=check,
        )
    if check and check.get("valid") is False:
        _issue(
            issues,
            "ctr_mismatch",
            "error",
            "CTR не соответствует кликам и показам с допустимым округлением.",
            section="ctr",
            details=check,
        )
    traffic = calculated.get("yandex_metrika", {}).get("traffic_sources") or {}
    shares = traffic.get("shares") or {}
    share_values = [_decimal(value) for value in shares.values()]
    total = _decimal(traffic.get("total"))
    shares_sum = (
        sum(share_values, Decimal("0"))
        if all(value is not None for value in share_values)
        else None
    )
    invalid_sum = (
        total is not None
        and total > 0
        and (shares_sum is None or abs(shares_sum - Decimal("100")) > Decimal("0.1"))
    )
    if traffic.get("warning") == "missing_total":
        _issue(
            issues,
            "section_data_missing",
            "warning",
            "Отсутствуют данные для раздела источников трафика.",
            section="traffic_sources",
            details=traffic,
        )
    elif (
        traffic.get("warning")
        or invalid_sum
        or any(value is None or value < 0 or value > 100 for value in share_values)
    ):
        _issue(
            issues,
            "traffic_shares_arithmetic",
            "error",
            "Доли источников трафика арифметически некорректны.",
            section="traffic_sources",
            details={**traffic, "shares_sum": str(shares_sum) if shares_sum is not None else None},
        )


def _rule_matches(rule, url):
    if not rule.get("active", True):
        return False
    pattern = rule.get("pattern", "")
    kind = rule.get("type")
    if kind == "starts_with":
        return url.startswith(pattern)
    if kind == "contains":
        return pattern in url
    if kind == "regex":
        try:
            return re.search(pattern, url) is not None
        except re.error:
            return False
    return False


def _validate_url_groups(payload, issues):
    groups = payload.get("project", {}).get("url_groups", [])
    urls = []
    for source in payload.get("ranking_sources", []):
        urls.extend(row.get("target_url") for row in source.get("positions", []))
    for work in payload.get("completed_work", []):
        urls.extend((work.get("url"), work.get("result_url")))
    for url in sorted({url for url in urls if url}):
        matched = [
            group.get("slug")
            for group in groups
            if group.get("active", True)
            and any(_rule_matches(rule, url) for rule in group.get("rules", []))
        ]
        if len(matched) > 1:
            _issue(
                issues,
                "url_multiple_groups",
                "warning",
                "URL попал в несколько URL-групп.",
                section="position_distribution",
                details={"url": url, "groups": matched},
            )


def _validate_provenance_and_urls(payload, issues):
    domain = payload.get("project", {}).get("normalized_domain") or payload.get("project", {}).get(
        "domain"
    )
    project_provenance = payload.get("project", {}).get("provenance")
    if not project_provenance or not project_provenance.get("method"):
        _issue(
            issues,
            "provenance_missing",
            "error",
            "Источник данных проекта не имеет provenance.",
            details={"source": "project"},
        )
    for collection in ("ranking_sources", "source_snapshots"):
        for index, source in enumerate(payload.get(collection, [])):
            provenance = source.get("provenance")
            if not provenance or not provenance.get("method"):
                _issue(
                    issues,
                    "provenance_missing",
                    "error",
                    "Показатель или источник не имеет provenance.",
                    details={"collection": collection, "index": index},
                )
            values = (
                source.get("positions", [])
                if collection == "ranking_sources"
                else source.get("metrics", [])
            )
            for value_index, value in enumerate(values):
                if collection == "ranking_sources" and not _host_matches(
                    value.get("target_url"), domain
                ):
                    _issue(
                        issues,
                        "foreign_metric_domain",
                        "error",
                        "URL показателя принадлежит чужому домену.",
                        section="position_distribution",
                        details={
                            "url": value.get("target_url"),
                            "domain": domain,
                            "source_index": index,
                            "value_index": value_index,
                        },
                    )
                if collection == "ranking_sources" and value.get("frequency") is None:
                    _issue(
                        issues,
                        "frequency_missing",
                        "error",
                        "Отсутствует обязательная частотность запроса.",
                        section="position_distribution",
                        details={"source_index": index, "value_index": value_index},
                    )
    for index, work in enumerate(payload.get("completed_work", [])):
        if not work.get("provenance", {}).get("method"):
            _issue(
                issues,
                "provenance_missing",
                "error",
                "Показатель или источник не имеет provenance.",
                section="completed_work",
                details={"index": index},
            )
        for field in ("url", "result_url"):
            if not _host_matches(work.get(field), domain):
                _issue(
                    issues,
                    "foreign_metric_domain",
                    "error",
                    "URL показателя принадлежит чужому домену.",
                    section="completed_work",
                    details={"field": field, "url": work.get(field), "domain": domain},
                )


def _validate_narratives(version, issues):
    for block in version.narrative_blocks.all():
        text = block.effective_text
        unsupported = sorted(_numbers(text) - _numbers(block.facts))
        if unsupported:
            _issue(
                issues,
                "narrative_unsupported_number",
                "error",
                "В тексте присутствует число, отсутствующее в связанных фактах.",
                section=block.section_code,
                details={
                    "numbers": [str(value) for value in unsupported],
                    "block_id": str(block.id),
                },
            )
        placeholders = PLACEHOLDER_RE.findall(text)
        if placeholders:
            _issue(
                issues,
                "narrative_placeholder",
                "error",
                "В тексте остался незаполненный плейсхолдер.",
                section=block.section_code,
                details={"placeholders": placeholders, "block_id": str(block.id)},
            )
        if _contains_secret(text):
            _issue(
                issues,
                "secret_detected",
                "error",
                "В тексте обнаружен секрет, API key, Authorization header или OAuth token.",
                section=block.section_code,
                details={"location": "narrative", "block_id": str(block.id)},
            )


@transaction.atomic
def validate_report_version(version):
    snapshot = ReportDatasetSnapshot.objects.only("payload", "checksum").get(version=version)
    payload = snapshot.payload
    issues = []
    if snapshot_checksum(payload) != snapshot.checksum:
        _issue(
            issues,
            "snapshot_checksum_mismatch",
            "error",
            "Checksum snapshot не совпадает с payload.",
            details={"stored": snapshot.checksum, "calculated": snapshot_checksum(payload)},
        )
    missing = [field for field in REQUIRED_FIELDS if field not in payload]
    if missing:
        _issue(
            issues,
            "snapshot_required_fields_missing",
            "error",
            "Отсутствуют обязательные поля snapshot.",
            details={"fields": missing},
        )
    if _contains_secret(payload):
        _issue(
            issues,
            "secret_detected",
            "error",
            "В snapshot обнаружен секрет, API key, Authorization header или OAuth token.",
            details={"location": "payload"},
        )
    _validate_positions(payload, issues)
    _validate_sources(payload, issues)
    _validate_provenance_and_urls(payload, issues)
    _validate_url_groups(payload, issues)
    _validate_narratives(version, issues)
    ValidationIssue.objects.filter(version=version).delete()
    for issue in issues:
        issue.version = version
    ValidationIssue.objects.bulk_create(issues)
    return issues


def get_publication_readiness(version):
    # Readiness is never inferred from an empty/stale issue set: effective narratives and the
    # frozen payload are validated immediately before the decision.
    validate_report_version(version)
    severities = set(version.validation_issues.values_list("severity", flat=True))
    has_errors = ValidationIssue.Severity.ERROR in severities
    has_warnings = ValidationIssue.Severity.WARNING in severities
    return PublicationReadiness(
        has_errors=has_errors,
        has_warnings=has_warnings,
        can_export_draft=True,
        can_publish=not has_errors,
    )
