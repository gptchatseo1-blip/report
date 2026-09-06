from pathlib import Path

from apps.reports.narratives import section_enabled

ROOT = Path(__file__).resolve().parents[1]


def test_distribution_summary_does_not_depend_on_keyword_tables_checkbox():
    payload = {
        "display_options": {
            "configuration_version": 3,
            "include_top_tables": False,
        }
    }

    assert section_enabled(payload, "position_distribution") is True
    assert section_enabled(payload, "top_10") is False


def test_yandex_sync_ui_reports_progress_and_does_not_claim_to_open_report():
    template = (ROOT / "templates/yandex/connection.html").read_text()
    javascript = (ROOT / "static/reports/ui-feedback.js").read_text()
    css = (ROOT / "static/reports/source-picker.css").read_text()

    assert template.count("data-yandex-sync-form") == 2
    assert "Синхронизировать и открыть отчёт" not in template
    assert "Синхронизировать Вебмастер и открыть отчёт" not in template
    assert "Идёт синхронизация" in javascript
    assert ".sync-form label.force-refresh" in css
    assert "display: flex" in css


def test_geography_is_hidden_when_disabled_and_dynamics_is_under_spoiler():
    report_list = (ROOT / "templates/reports/report_list.html").read_text()
    metrics = (ROOT / "templates/reports/_section_metrics.html").read_text()
    builder = (ROOT / "static/reports/report-builder.js").read_text()

    assert 'data-dependent-on="id_include_metrika_geography" data-hide-when-disabled' in report_list
    assert "container.hidden = disabled" in builder
    assert 'class="data-table-spoiler position-dynamics-spoiler"' in metrics


def test_success_and_delete_messages_are_presented_as_notifications():
    base = (ROOT / "templates/reports/base.html").read_text()
    javascript = (ROOT / "static/reports/ui-feedback.js").read_text()
    views = (ROOT / "apps/reports/views.py").read_text()
    yandex_views = (ROOT / "apps/yandex/views.py").read_text()

    assert "data-flash-notice" in base
    assert "data-flash-close" in javascript
    assert 'messages.success(request, f"Версия №{number} удалена.")' in views
    assert 'messages.success(request, "Счётчик Яндекс.Метрики сохранён.")' in yandex_views


def test_manual_update_explains_and_preserves_checked_rows():
    javascript = (ROOT / "static/reports/report-polish-round6.js").read_text()

    assert "Обновить автоматические значения только в строках без галочки" in javascript
    assert "Отмеченные строки сохраняются без изменений и используются в отчёте." in javascript
    assert "Очистить" not in javascript
