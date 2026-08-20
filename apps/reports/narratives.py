from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from django.db import transaction

from .models import NarrativeBlock, ReportDatasetSnapshot

SECTION_ORDER = (
    "visibility",
    "position_distribution",
    "position_dynamics",
    "top_5",
    "top_10",
    "top_20",
    "top_11_30",
    "top_30",
    "top_11_20",
    "clicks_impressions",
    "ctr",
    "indexing",
    "iks",
    "webmaster_popular_queries",
    "traffic",
    "traffic_sources",
    "metrika_search_engines",
    "geography",
    "metrika_landing_pages",
    "metrika_landing_page_comparison",
    "metrika_url_groups",
    "metrika_sections",
    "metrika_categories",
    "metrika_goals",
    "completed_work",
)

TOP_SECTION_RANGES = {
    "top_5": (1, 5),
    "top_10": (1, 10),
    "top_20": (1, 20),
    "top_11_30": (11, 30),
    "top_30": (1, 30),
    "top_11_20": (11, 20),
}


def section_enabled(payload, code):
    """Apply frozen report options while keeping legacy snapshots exportable."""
    options = payload.get("display_options", {})
    if options.get("configuration_version") not in {2, 3}:
        return code not in {"top_5", "top_20", "top_11_30", "top_30", "geography"}
    if code == "visibility":
        return options.get("include_visibility", True)
    if code == "position_dynamics":
        return options.get("include_monthly_dynamics", True)
    if code == "top_11_20":
        return False
    if code in TOP_SECTION_RANGES:
        return options.get("include_top_tables", True) and options.get(f"include_{code}", False)
    if code in {"clicks_impressions", "ctr", "indexing", "iks"}:
        return options.get("include_webmaster", True)
    if code == "webmaster_popular_queries":
        return options.get("include_webmaster", True) and options.get(
            "include_webmaster_popular_queries", True
        )
    if code in {"traffic", "traffic_sources"}:
        return options.get("include_metrika", True)
    if code == "geography":
        return options.get("include_metrika", True) and options.get(
            "include_metrika_geography", False
        )
    if code.startswith("metrika_"):
        return options.get("include_metrika", True) and options.get(f"include_{code}", False)
    if code == "completed_work":
        return options.get("include_completed_work", True)
    return True


METRIC_LABELS = {
    "visibility": "Видимость",
    "visits": "Трафик",
    "search_clicks": "Клики",
    "search_impressions": "Показы",
    "search_ctr": "CTR",
    "indexed_pages": "Количество проиндексированных страниц",
    "iks": "ИКС",
    "quality_index": "ИКС",
    "geography_moscow_visits": "Москва",
    "geography_saint_petersburg_visits": "Санкт-Петербург",
    "geography_undefined_visits": "Не определено",
    "geography_area_undefined_visits": "Область не определена",
}
CHANGE_VERBS = {
    "видимость": ("выросла", "снизилась"),
    "клики": ("выросли", "снизились"),
    "показы": ("выросли", "снизились"),
    "количество проиндексированных страниц": ("выросло", "снизилось"),
}
SECTION_INTROS = {
    "clicks_impressions": (
        "Данные по показам и кликам сайта в поиске Яндекса за выбранные месяцы."
    ),
    "ctr": "CTR показывает долю кликов от общего количества показов в поиске Яндекса.",
    "indexing": "Индексация сайта по данным Яндекс.Вебмастера.",
    "iks": "Динамика индекса качества сайта по данным Яндекс.Вебмастера.",
    "traffic": (
        "Сводная информация по переходам на сайт по данным Яндекс.Метрики. "
        "Показатели представлены месячными итогами."
    ),
    "geography": (
        "Распределение месячного количества визитов по выбранным географическим группам."
    ),
}


def _number(value):
    if value is None:
        return "—"
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return str(value)
    rendered = format(number.normalize(), "f")
    return rendered.replace(".", ",")


def _share(value):
    if value is None:
        return "—"
    try:
        number = Decimal(str(value)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    except InvalidOperation:
        return str(value)
    return format(number, ".1f").replace(".", ",")


def _query_count(value):
    word = (
        "запрос"
        if value % 10 == 1 and value % 100 != 11
        else "запроса"
        if value % 10 in {2, 3, 4} and value % 100 not in {12, 13, 14}
        else "запросов"
    )
    verb = "находится" if word == "запрос" else "находятся"
    return f"{verb} {value} {word}"


def _change_text(label, change, *, unit=""):
    """Render already calculated snapshot facts; this function performs no metric calculation."""
    current = change.get("current")
    previous = change.get("previous")
    points = change.get("percentage_points")
    display_unit = "%" if points is not None else unit
    if current is None:
        return f"{label}: данные отсутствуют."
    if previous is None:
        return f"{label}: {_number(current)}{display_unit}; отсутствует база сравнения."
    absolute = change.get("absolute")
    relative = change.get("relative_percent")
    if absolute is None:
        return f"{label}: {_number(current)}{display_unit}; отсутствует база сравнения."
    delta = Decimal(str(absolute))
    if delta == 0:
        return f"{label}: {_number(current)}{display_unit}; существенного изменения нет."
    increased, decreased = CHANGE_VERBS.get(label.casefold(), ("вырос", "снизился"))
    direction = increased if delta > 0 else decreased
    magnitude = _number(abs(delta))
    if points is not None:
        return (
            f"{label} {direction} до {_number(current)}%: абсолютное изменение — "
            f"{magnitude} процентного пункта."
        )
    if relative is None:
        return (
            f"{label} {direction} до {_number(current)}{unit}: абсолютное изменение — "
            f"{magnitude}{unit}; относительное изменение не рассчитано из-за нулевой базы."
        )
    return (
        f"{label} {direction} до {_number(current)}{unit}: абсолютное изменение — "
        f"{magnitude}{unit}, относительное изменение — {_number(abs(Decimal(str(relative))))}%."
    )


def _source_changes(payload, source):
    return (
        payload.get("calculated", {})
        .get("sources", {})
        .get("sources", {})
        .get(source, {})
        .get("normalized_changes", {})
    )


def _metric_block(payload, section, source, codes):
    changes = _source_changes(payload, source)
    if section == "iks":
        codes = tuple(
            code
            for code in codes
            if code in changes
            and any(changes[code].get(field) is not None for field in ("current", "previous"))
        )[:1]
    selected = {code: changes[code] for code in codes if code in changes}
    intro = SECTION_INTROS.get(section, "")
    if not selected:
        text = f"{intro} Данные раздела отсутствуют.".strip()
        return {"section": section, "text": text, "facts": {}}
    text = " ".join(
        part
        for part in (
            intro,
            " ".join(_change_text(METRIC_LABELS[code], value) for code, value in selected.items()),
        )
        if part
    )
    return {"section": section, "text": text, "facts": {"changes": selected}}


def _segment_name(segment):
    engines = {"google": "Google", "yandex": "Яндекс"}
    engine = engines.get(segment.get("search_engine"), segment.get("search_engine") or "Поиск")
    region = segment.get("region") or "регион не указан"
    return f"{engine}, {region}"


def _google_depth_text(depth_notes):
    if not depth_notes:
        return None
    depths = {item["ranking_depth"] for item in depth_notes}
    if len(depths) == 1:
        depth = next(iter(depths))
        return (
            f"Проверка позиций в Google выполнена до TOP-{depth}. Для запросов, по которым сайт "
            "не найден в пределах этой глубины, точная позиция не определена."
        )
    configurations = "; ".join(
        f"{_segment_name(item)} — до TOP-{item['ranking_depth']}" for item in depth_notes
    )
    return (
        "Глубина проверки позиций в Google различается по конфигурациям: "
        f"{configurations}. Для запросов вне указанной для конфигурации глубины точная позиция "
        "не определена."
    )


def build_narrative_specs(payload):
    """Build Russian copy exclusively from the immutable JSON payload supplied by a snapshot."""
    specs = []
    segments = payload.get("calculated", {}).get("positions", {}).get("segments", [])
    if not segments:
        for section in (
            "visibility",
            "position_distribution",
            "position_dynamics",
            *TOP_SECTION_RANGES,
        ):
            specs.append({"section": section, "text": "Данные раздела отсутствуют.", "facts": {}})
    else:
        visibility_facts = []
        distribution_facts = []
        top_facts = []
        dynamic_facts = []
        top_11_facts = []
        depth_notes = []
        for segment in segments:
            identity = {
                "search_engine": segment.get("search_engine"),
                "region": segment.get("region"),
                "ranking_depth": segment.get("ranking_depth"),
            }
            distribution = segment.get("distribution", {})
            visibility_facts.append({**identity, "change": segment.get("visibility_change")})
            distribution_facts.append({**identity, "distribution": distribution})
            top_facts.append(
                {
                    **identity,
                    "top_10": distribution.get("top_10"),
                    "top_30": distribution.get("top_30"),
                }
            )
            dynamic_facts.append(
                {
                    **identity,
                    "comparison_depth": segment.get("comparison_depth"),
                    "comparison_distributions": segment.get("comparison_distributions"),
                    "semantics": segment.get("semantics"),
                    "warnings": segment.get("warnings", []),
                }
            )
            rows = segment.get("top_11_20", [])
            if rows:
                top_11_facts.append({**identity, "rows": rows})
            if segment.get("search_engine") == "google" and segment.get("ranking_depth"):
                depth_notes.append(identity)

        visible = [fact for fact in visibility_facts if fact["change"]]
        specs.append(
            {
                "section": "visibility",
                "text": (
                    "Видимость сайта — доля показов сайта в поисковых системах, которая "
                    "зависит от частотности и позиций отслеживаемых запросов. "
                )
                + (
                    " ".join(
                        f"{_segment_name(item)}: {_change_text('видимость', item['change'])}"
                        for item in visible
                    )
                    if visible
                    else "Данные для расчёта видимости отсутствуют."
                ),
                "facts": {"segments": visible},
            }
        )
        distribution_text = [
            "График показывает распределение основных ключевых запросов по диапазонам "
            "позиций за отчётный период. Он отражает количество запросов в каждом TOP и "
            "не учитывает их частотность. Данные по количеству и доле запросов в TOP:"
        ]
        for item in distribution_facts:
            ranges = item["distribution"].get("ranges", {})
            rendered = ", ".join(f"{key}: {value}" for key, value in ranges.items())
            distribution_text.append(
                f"{_segment_name(item)}: распределение запросов по диапазонам — {rendered}."
            )
        depth_text = _google_depth_text(depth_notes)
        if depth_text:
            # Exactly one aggregate qualification is appended to the positional section.
            distribution_text.append(depth_text)
        specs.append(
            {
                "section": "position_distribution",
                "text": " ".join(distribution_text) or "Данные раздела отсутствуют.",
                "facts": {"segments": distribution_facts, "depth_notes": depth_notes},
            }
        )
        for section, (start, end) in TOP_SECTION_RANGES.items():
            if section == "top_11_20":
                continue
            specs.append(
                {
                    "section": section,
                    "text": " ".join(
                        (
                            f"{_segment_name(item)}: таблица запросов в диапазоне "
                            f"TOP-{start}–{end} сформирована."
                            if start != 1
                            else (
                                f"{_segment_name(item)}: таблица запросов в TOP-{end} сформирована."
                            )
                        )
                        for item in top_facts
                    )
                    or "Данные раздела отсутствуют.",
                    "facts": {
                        "segments": top_facts,
                        "range_start": start,
                        "range_end": end,
                    },
                }
            )
        if top_11_facts:
            specs.append(
                {
                    "section": "top_11_20",
                    "text": " ".join(
                        f"{_segment_name(item)}: в диапазоне TOP-11–20 находятся запросы: "
                        + ", ".join(f"{row['query']} ({row['position']})" for row in item["rows"])
                        + "."
                        for item in top_11_facts
                    ),
                    "facts": {"segments": top_11_facts, "range_start": 11, "range_end": 20},
                }
            )
        dynamics_text = []
        for fact in dynamic_facts:
            prefix = f"{_segment_name(fact)}: "
            if fact["comparison_distributions"] is None:
                dynamics_text.append(prefix + "отсутствует предыдущий период или база сравнения.")
            elif fact["warnings"]:
                dynamics_text.append(
                    prefix
                    + "изменилась глубина проверки; динамика ограничена сопоставимой глубиной."
                )
            else:
                dynamics_text.append(
                    prefix + "динамика позиций рассчитана на сопоставимой глубине."
                )
            if fact.get("semantics", {}).get("warning"):
                dynamics_text.append(
                    prefix + "существенно изменился состав отслеживаемых запросов."
                )
        specs.append(
            {
                "section": "position_dynamics",
                "text": " ".join(dynamics_text),
                "facts": {"segments": dynamic_facts},
            }
        )

    specs.extend(
        (
            _metric_block(
                payload,
                "clicks_impressions",
                "yandex_webmaster",
                ("search_clicks", "search_impressions"),
            ),
            _metric_block(payload, "ctr", "yandex_webmaster", ("search_ctr",)),
            _metric_block(payload, "indexing", "yandex_webmaster", ("indexed_pages",)),
            _metric_block(payload, "iks", "yandex_webmaster", ("iks", "quality_index")),
            _metric_block(payload, "traffic", "yandex_metrika", ("visits",)),
        )
    )
    traffic = (
        payload.get("calculated", {})
        .get("sources", {})
        .get("sources", {})
        .get("yandex_metrika", {})
        .get("traffic_sources")
    )
    specs.append(
        {
            "section": "traffic_sources",
            "text": "Данные раздела отсутствуют."
            if not traffic or traffic.get("total") is None
            else "Источники трафика: "
            + ", ".join(
                f"{key} — {_share(value)}%" for key, value in traffic.get("shares", {}).items()
            )
            + ".",
            "facts": (
                {
                    **traffic,
                    "display_shares": {
                        key: _share(value) for key, value in traffic.get("shares", {}).items()
                    },
                }
                if traffic
                else {}
            ),
        }
    )
    specs.append(
        _metric_block(
            payload,
            "geography",
            "yandex_metrika",
            (
                "geography_moscow_visits",
                "geography_saint_petersburg_visits",
                "geography_undefined_visits",
                "geography_area_undefined_visits",
            ),
        )
    )
    options = payload.get("display_options", {})
    specs.extend(
        (
            {
                "section": "webmaster_popular_queries",
                "text": options.get("webmaster_queries_comment")
                or (
                    "Запросы в таблице отсортированы по кликабельности. Красным шрифтом "
                    "представлен спад относительно предыдущего периода, зелёным — рост, "
                    "серым — отсутствие изменений."
                ),
                "facts": {},
            },
            {
                "section": "metrika_search_engines",
                "text": "Сравнение поискового трафика за два последних месяца.",
                "facts": {},
            },
            {
                "section": "metrika_landing_pages",
                "text": "Популярные страницы входа из поисковых систем.",
                "facts": {},
            },
            {
                "section": "metrika_landing_page_comparison",
                "text": "Сравнение страниц входа из Яндекса и Google.",
                "facts": {},
            },
            {
                "section": "metrika_url_groups",
                "text": "Сравнение трафика по URL-группам проекта.",
                "facts": {},
            },
            {
                "section": "metrika_sections",
                "text": "Данные по разделам сайта.",
                "facts": {},
            },
            {
                "section": "metrika_categories",
                "text": "Данные по основным прорабатываемым категориям.",
                "facts": {},
            },
            {
                "section": "metrika_goals",
                "text": "Цели по поисковому трафику с выбранной роботностью.",
                "facts": {},
            },
        )
    )
    works = payload.get("completed_work", [])
    specs.append(
        {
            "section": "completed_work",
            "text": "Выполненные работы отсутствуют."
            if not works
            else "Выполненные работы: " + "; ".join(item["title"] for item in works) + ".",
            "facts": {"items": works},
        }
    )
    order = {code: index for index, code in enumerate(SECTION_ORDER)}
    return sorted(specs, key=lambda item: order[item["section"]])


@transaction.atomic
def generate_narratives(version):
    snapshot = ReportDatasetSnapshot.objects.only("payload").get(version=version)
    existing = {
        (block.section_code, block.sort_order): block
        for block in version.narrative_blocks.select_for_update()
    }
    result = []
    for spec in build_narrative_specs(snapshot.payload):
        key = (spec["section"], 0)
        block = existing.pop(key, None)
        if block is None:
            block = NarrativeBlock(
                report_version=version, section_code=spec["section"], sort_order=0
            )
            block.generated_text = spec["text"]
            block.facts = spec["facts"]
            block.kind = NarrativeBlock.Kind.DETERMINISTIC
            block.save()
        result.append(block)
    return result
