from decimal import Decimal, InvalidOperation

from django.db import transaction

from .models import NarrativeBlock, ReportDatasetSnapshot

SECTION_ORDER = (
    "visibility",
    "position_distribution",
    "top_10",
    "top_11_20",
    "position_dynamics",
    "traffic",
    "traffic_sources",
    "clicks_impressions",
    "ctr",
    "indexing",
    "iks",
    "completed_work",
)

METRIC_LABELS = {
    "visibility": "Видимость",
    "visits": "Трафик",
    "search_clicks": "Клики",
    "search_impressions": "Показы",
    "search_ctr": "CTR",
    "indexed_pages": "Количество проиндексированных страниц",
    "iks": "ИКС",
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
    direction = "вырос" if delta > 0 else "снизился"
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
    selected = {code: changes[code] for code in codes if code in changes}
    if not selected:
        return {"section": section, "text": "Данные раздела отсутствуют.", "facts": {}}
    text = " ".join(_change_text(METRIC_LABELS[code], value) for code, value in selected.items())
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
        for section in ("visibility", "position_distribution", "top_10", "position_dynamics"):
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
                "text": " ".join(
                    f"{_segment_name(item)}: {_change_text('видимость', item['change'])}"
                    for item in visible
                )
                if visible
                else "Данные раздела отсутствуют.",
                "facts": {"segments": visible},
            }
        )
        distribution_text = []
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
        specs.append(
            {
                "section": "top_10",
                "text": " ".join(
                    f"{_segment_name(item)}: в TOP-10 находится {item['top_10']} запросов."
                    for item in top_facts
                ),
                "facts": {"segments": top_facts, "range_start": 1, "range_end": 10},
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
            _metric_block(payload, "traffic", "yandex_metrika", ("visits",)),
            _metric_block(
                payload,
                "clicks_impressions",
                "yandex_webmaster",
                ("search_clicks", "search_impressions"),
            ),
            _metric_block(payload, "ctr", "yandex_webmaster", ("search_ctr",)),
            _metric_block(payload, "indexing", "yandex_webmaster", ("indexed_pages",)),
            _metric_block(payload, "iks", "yandex_webmaster", ("iks",)),
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
                f"{key} — {_number(value)}%" for key, value in traffic.get("shares", {}).items()
            )
            + ".",
            "facts": traffic or {},
        }
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
