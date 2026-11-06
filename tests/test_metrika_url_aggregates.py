import copy
import re
from datetime import date
from decimal import Decimal
from urllib.parse import urlsplit

import pytest
from docx import Document

from apps.metrics.url_groups import combined_url_group, url_group_filter, url_group_key
from apps.reports import exporting
from apps.yandex.services import _direct_landing_report


def page(url, visits, users=None, *, engine="all", level=None):
    row = {
        "dimensions": [{"id": engine, "name": engine}, {"id": url, "name": url}],
        "visits": str(visits),
        "users": str(visits if users is None else users),
        "bounce_rate": "10",
    }
    if level is not None:
        row["hierarchy_level"] = level
    return row


def provider_filter_matches(expression, url):
    # Evaluate only the URL comparisons generated for the API, not its AND/OR parser.
    clauses = re.findall(r"(ym:s:startURL(?:Path)?)=~'((?:\\.|[^'])*)'", expression)
    return any(
        re.search(
            re.sub(r"\\([\\'])", r"\1", pattern),
            urlsplit(url).path if dimension.endswith("Path") else url,
        )
        is not None
        for dimension, pattern in clauses
    )


@pytest.mark.parametrize(
    "pattern",
    [
        "https://site.test/medicine/*",
        "/medicine/",
        "site.test/medicine/",
        "*ginekologiya*",
        "/medicine/item?.html",
        "/medicine/item[12]*",
        {"type": "starts_with", "value": "https://SITE.test/medicine/"},
        {"type": "contains", "value": "GINEKOLOGIYA"},
        {"type": "regex", "value": r"/medicine/(?:ginekologiya|item1)/"},
        "/medicine/doctor's/",
    ],
)
def test_api_filters_preserve_existing_url_mask_semantics(pattern):
    group = {"patterns": [pattern]}
    for url in (
        "https://site.test/medicine/ginekologiya/item/",
        "https://SITE.test/medicine/item1.html?ref=test",
        "https://site.test/medicine/item2/",
        "https://site.test/medicine/doctor's/item/",
        "https://site.test/medicine/",
        "https://site.test/diseases/ginekologiya/",
        "https://other.test/medicine/item1.html",
    ):
        assert provider_filter_matches(
            url_group_filter(group), url
        ) == exporting._url_matches_pattern(url, pattern)


def test_group_identity_ignores_mask_order_and_duplicate_masks():
    group = {"name": "Гинекология", "patterns": ["/medicine/", "/medicine/ginekologiya/"]}
    reordered = {
        "name": "Другое название",
        "patterns": [*reversed(group["patterns"]), "/medicine/"],
    }
    assert url_group_key(group) == url_group_key(reordered)


def test_direct_page_pagination_keeps_provider_totals_and_advances_by_received_rows():
    class Client:
        def __init__(self):
            self.offsets = []

        def stat(self, **params):
            self.offsets.append(params.get("offset", 1))
            urls = ["/one/", "/two/"] if self.offsets[-1] == 1 else ["/three/"]
            return {
                "data": [
                    {"dimensions": [{"name": f"https://site.test{url}"}], "metrics": [10, 8, 5]}
                    for url in urls
                ],
                "total_rows": 3,
                "totals": [30, 20, 5],
            }

    client = Client()
    rows, total = _direct_landing_report(client, "ym:s:isRobot=='No'", {})
    assert client.offsets == [1, 3]
    assert len(rows) == 3
    assert total == {"visits": "30", "users": "20", "bounce_rate": "5"}


@pytest.mark.parametrize("flag", ["total_rows_rounded", "contains_sensitive_data"])
def test_privacy_limited_or_rounded_row_count_does_not_request_hidden_rows(flag):
    class Client:
        def stat(self, **params):
            assert "offset" not in params
            return {
                "data": [
                    {"dimensions": [{"name": "https://site.test/"}], "metrics": [1984, 1493, 10]}
                ],
                "total_rows": 1000,
                flag: True,
                "totals": [17463, 14954, 10],
            }

    rows, total = _direct_landing_report(Client(), None, {})
    assert len(rows) == 1
    assert total["visits"] == "17463"


def test_section_chart_and_text_use_provider_totals_when_leaf_pages_are_hidden(monkeypatch):
    group = {"name": "Красота", "patterns": ["https://site.test/beauty/"]}
    periods = [
        {
            "period_start": "2026-09-01",
            "rows": [page("https://site.test/beauty/", 43)],
            "group_totals": {url_group_key(group): {"visits": "327", "users": "300"}},
        },
        {
            "period_start": "2026-10-01",
            "rows": [page("https://site.test/beauty/", 38)],
            "group_totals": {url_group_key(group): {"visits": "351", "users": "310"}},
        },
    ]
    original = copy.deepcopy(periods)
    figures = []
    monkeypatch.setattr(exporting, "_save_figure", lambda figure: figures.append(figure))

    exporting._metrika_groups_chart(periods, [group])
    assert list(figures[0].axes[0].lines[0].get_ydata()) == [327, 351]
    assert figures[0].axes[0].lines[0].get_color() == exporting.METRIKA_COLORS[0]
    assert "+7,3%" in exporting._group_dynamics_text(periods, [group])
    assert "увеличение" in exporting._group_overview_text(periods, [group], commercial=True)
    assert periods == original


def test_deep_category_uses_direct_union_total_without_double_counting_overlapping_masks():
    group = {
        "name": "Гинекология",
        "patterns": ["/medicine/obshchaya-ginekologiya/", "*ginekologiya/operatsii/*"],
    }
    periods = [
        {
            "period_start": "2026-10-01",
            "rows": [page("https://site.test/medicine/obshchaya-ginekologiya/operatsii/item/", 20)],
            "group_totals": {url_group_key(group): {"visits": "200", "users": "150"}},
        }
    ]
    assert exporting._group_period_values(periods, [group])[0]["values"]["Гинекология"] == {
        "visits": Decimal(200),
        "users": Decimal(150),
        "bounce_rate": None,
    }


def test_overview_uses_union_across_overlapping_named_groups():
    groups = [{"name": "A", "patterns": ["/medicine/"]}, {"name": "B", "patterns": ["*gyn*"]}]
    union = url_group_key(combined_url_group(groups))
    periods = [
        {
            "group_totals": {
                url_group_key(groups[0]): {"visits": "100"},
                url_group_key(groups[1]): {"visits": "50"},
                union: {"visits": "140"},
            }
        },
        {
            "group_totals": {
                url_group_key(groups[0]): {"visits": "110"},
                url_group_key(groups[1]): {"visits": "60"},
                union: {"visits": "130"},
            }
        },
    ]
    assert "снижение" in exporting._group_overview_text(periods, groups, commercial=True)


def test_comparison_table_uses_direct_hierarchy_and_never_sums_its_levels():
    doc = Document()
    exporting._configure_document(doc, "site.test", date(2026, 10, 1))
    current = [
        page("https://site.test/", 17463, 14954, level=1),
        page("https://site.test/beauty/", 351, 310, level=2),
        page("https://site.test/beauty/", 10, 9, level=3),
    ]
    previous = [
        page("https://site.test/", 14830, 12541, level=1),
        page("https://site.test/beauty/", 327, 300, level=2),
    ]
    table = exporting._configured_groups_table(
        doc,
        {},
        current,
        previous,
        [],
        provider_hierarchy=True,
        total_values=({"visits": "17463", "users": "14954"}, {"visits": "14830", "users": "12541"}),
    )
    beauty = next(row for row in table.rows if "beauty" in row.cells[0].text)
    assert "351" in beauty.cells[2].text
    assert "7,34%" in beauty.cells[2].text
    assert "310" in beauty.cells[4].text


def test_information_conclusion_uses_direct_engine_total_and_preserves_commercial_change():
    info = {"name": "Статьи", "patterns": ["https://site.test/articles/"]}
    commercial = {"name": "Лечение", "patterns": ["https://site.test/medicine/"]}
    current = [page("https://site.test/medicine/", 250, engine="Яндекс", level=2)]
    previous = [page("https://site.test/medicine/", 200, engine="Яндекс", level=2)]
    result = exporting._landing_comparison_conclusions(
        engine="Яндекс",
        current_rows=[],
        previous_rows=[],
        current_hierarchy_rows=current,
        previous_hierarchy_rows=previous,
        total_values=({"visits": "1000"}, {"visits": "900"}),
        commercial_groups=[commercial],
        information_groups=[info],
        provider_hierarchy=True,
        current_information_totals={
            url_group_key(info): [
                {"dimensions": [{"id": "yandex", "name": "Яндекс"}], "visits": "600"}
            ]
        },
        previous_information_totals={
            url_group_key(info): [
                {"dimensions": [{"id": "yandex", "name": "Яндекс"}], "visits": "500"}
            ]
        },
    )
    assert "60%" in result[0]
    assert "увеличилось на 20%" in result[0]
    assert result[-1] == "Лечение — +25% по сравнению с прошлым месяцем."


def test_render_routes_direct_data_to_the_agreed_blocks_only(monkeypatch):
    group = {"name": "Красота", "patterns": ["https://site.test/beauty/"]}
    details = []
    for month, visits, root_visits, legacy_root, legacy_beauty in (
        (9, 327, 1922, 1914, 43),
        (10, 351, 1984, 1970, 38),
    ):
        variant = {
            "landing_pages": [
                page("https://site.test/", legacy_root, engine="Яндекс"),
                page("https://site.test/beauty/", legacy_beauty, engine="Яндекс"),
            ],
            "landing_pages_direct": [
                page("https://site.test/", root_visits, 1493),
                page("https://site.test/beauty/", legacy_beauty),
            ],
            "landing_pages_direct_total": {"visits": "17463", "users": "14954"},
            "landing_hierarchy_direct": [
                page("https://site.test/", 17463, 14954, level=1),
                page("https://site.test/beauty/", visits, 310, level=2),
                page("https://other.test/beauty/", 9999, 9000, level=2),
            ],
            "landing_group_totals": {url_group_key(group): {"visits": str(visits)}},
        }
        details.append(
            {
                "period_start": f"2026-{month:02}-01",
                "period_end": f"2026-{month:02}-30",
                "payload": {"detail_variants": {"search": {"humans": variant}}},
            }
        )
    payload = {
        "project": {"domain": "site.test"},
        "display_options": {
            "configuration_version": 3,
            "include_metrika_landing_pages": True,
            "include_metrika_url_groups": True,
            "include_metrika_sections": True,
            "include_metrika_categories": True,
            "metrika_url_segments": {"commercial": [group], "categories": [group]},
        },
        "calculated": {"sources": {"sources": {"yandex_metrika": {"period_details": details}}}},
    }
    original = copy.deepcopy(payload)
    monkeypatch.setattr(exporting, "_add_report_picture", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(exporting, "_metrika_groups_chart", lambda *_args, **_kwargs: None)
    doc = Document()
    exporting._configure_document(doc, "site.test", date(2026, 10, 1))
    exporting._render_metrika(doc, payload, {})

    root_rows = [
        row
        for table in doc.tables
        for row in table.rows
        if row.cells[0].text.strip().endswith("https://site.test/")
    ]
    assert any("1984" in row.cells[2].text and "1493" in row.cells[4].text for row in root_rows)
    beauty_rows = [
        row for table in doc.tables for row in table.rows if "beauty/" in row.cells[0].text
    ]
    assert any("351" in row.cells[2].text for row in beauty_rows)
    paragraphs = "\n".join(paragraph.text for paragraph in doc.paragraphs)
    assert paragraphs.count("Раздел «Красота» (https://site.test/beauty/) — +7,3%.") == 2
    assert "Коммерческий раздел «Красота» (https://site.test/beauty/) — +7,3%." in paragraphs
    assert "1984 визитов за месяц" in paragraphs
    assert all("other.test" not in row.cells[0].text for table in doc.tables for row in table.rows)
    assert payload == original
