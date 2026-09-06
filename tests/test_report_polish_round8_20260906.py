from pathlib import Path

from apps.reports.runtime_fixes_round8 import (
    _calendar_chart_segment,
    _manual_buckets_with_yandex_tail,
)


def test_round8_export_forces_visibility_column_and_compact_distribution():
    root = Path(__file__).resolve().parents[1]
    source = (root / "apps/reports/runtime_fixes_round8.py").read_text()

    assert '("Месяц", "Видимость", "в топ 3", "в топ 10"' in source
    assert "outer_width = 4.15 if columns == 2 else 4.35" in source
    assert "size=11" in source
    assert 'label="Видимость"' in source
    assert 'exp.GENERATOR_VERSION = "mvp1.12-2026-09-06"' in source


def test_graph_uses_only_dates_checked_in_calendar():
    segment = {
        "search_engine": "yandex",
        "ranking_depth": 100,
        "chart_series": [
            {"month": "2026-06-12", "visibility": 14},
            {"month": "2026-07-10", "visibility": 16},
            {"month": "2026-08-25", "visibility": 15},
        ],
        "three_month_series": [],
    }
    payload = {
        "source_selection": {
            "topvisor": {
                "yandex": {"selected_dates": ["2026-07-10", "2026-08-25"]},
            }
        }
    }

    rendered = _calendar_chart_segment(
        lambda _payload, item: item,
        lambda _distribution, _depth: [],
        payload,
        segment,
    )

    assert [point["month"] for point in rendered["chart_series"]] == [
        "2026-07-10",
        "2026-08-25",
    ]


def test_calendar_graph_keeps_provider_yandex_buckets_instead_of_manual_three_buckets():
    automatic_distribution = {
        "total": 100,
        "top_10": 40,
        "ranges": {
            "1-3": 10,
            "4-10": 30,
            "11-20": 15,
            "21-30": 10,
            "31-50": 15,
            "51-100": 12,
        },
    }
    segment = {
        "search_engine": "yandex",
        "region": "Москва",
        "ranking_depth": 100,
        "chart_series": [
            {"month": "2026-08-25", "visibility": 15, "distribution": automatic_distribution}
        ],
        "three_month_series": [],
    }
    payload = {"source_selection": {"topvisor": {"yandex": {"selected_dates": ["2026-08-25"]}}}}

    rendered = _calendar_chart_segment(
        lambda _payload, item: {
            **item,
            "three_month_series": [
                {
                    "month": "2026-08-01",
                    "manual_override": True,
                    "distribution": {
                        "manual_buckets": {
                            "1-3": {"count": 20, "share": 20},
                            "1-10": {"count": 50, "share": 50},
                            "11-30": {"count": 30, "share": 30},
                        }
                    },
                }
            ],
        },
        lambda _distribution, _depth: [],
        payload,
        segment,
    )

    assert rendered["chart_series"][0]["distribution"] == automatic_distribution


def test_yandex_manual_buckets_keep_long_tail_ranges():
    distribution = {
        "manual_buckets": {
            "1-3": {"count": 10, "share": 10},
            "1-10": {"count": 30, "share": 30},
            "11-30": {"count": 25, "share": 25},
            "31-50": {"count": 15, "share": 15},
            "51-100": {"count": 12, "share": 12},
            "101+": {"count": 8, "share": 8},
        }
    }

    buckets = _manual_buckets_with_yandex_tail(lambda _distribution, _depth: [], distribution, 100)

    assert [bucket["label"] for bucket in buckets] == [
        "1-3",
        "1-10",
        "11-30",
        "31-50",
        "51-100",
        "101+",
    ]


def test_round8_ui_fixes_modal_actions_close_button_and_report_progress():
    root = Path(__file__).resolve().parents[1]
    css = (root / "static/reports/report-polish-round8.css").read_text()
    js = (root / "static/reports/report-polish-round8.js").read_text()

    assert "align-items:center!important" in css
    assert "height:42px!important" in css
    assert ".report-modal .report-modal-close" in css
    assert "border:0!important" in css
    assert "background:transparent!important" in css
    assert ".settings-icon" in css
    assert "clearResolvedWarning" in js
    assert "errorlist" in js
    assert "Отчёт создаётся…" in js
    assert "Создание отчёта…" in js


def test_project_settings_uses_clean_stroke_gear():
    root = Path(__file__).resolve().parents[1]
    template = (root / "templates/reports/project_list.html").read_text()

    assert 'class="settings-icon"' in template
    assert '<circle cx="12" cy="12" r="3"/>' in template
