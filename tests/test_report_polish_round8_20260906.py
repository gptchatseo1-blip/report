from pathlib import Path


def test_round8_export_forces_visibility_column_and_compact_distribution():
    root = Path(__file__).resolve().parents[1]
    source = (root / "apps/reports/runtime_fixes_round8.py").read_text()

    assert "current_monthly_renderer(doc, segment, show_visibility=True)" in source
    assert 'outer_width = 4.15 if columns == 2 else 4.35' in source
    assert "size=11" in source
    assert 'exp.GENERATOR_VERSION = "mvp1.11-2026-09-06"' in source


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
