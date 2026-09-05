"""Compact Topvisor distribution summary table for DOCX/PDF exports."""

from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT, WD_ROW_HEIGHT_RULE, WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Cm, Pt

_APPLIED = False


def _style_text(exp, cell, *, size=11, color="3D4655", bold=False, align=None):
    cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
    for paragraph in cell.paragraphs:
        paragraph.paragraph_format.space_before = Pt(0)
        paragraph.paragraph_format.space_after = Pt(0)
        paragraph.paragraph_format.line_spacing = 1
        if align is not None:
            paragraph.alignment = align
        for run in paragraph.runs:
            exp._style_run(run, size=size, color=color, bold=bold)


def _render_distribution_cards_table(exp, doc, distribution, depth, *, engine="yandex"):
    """Render a compact flat table without nested-table blank paragraphs."""
    buckets = exp._topvisor_buckets(distribution, 20 if engine == "google" else depth)
    if engine == "google":
        buckets = [bucket for bucket in buckets if bucket["label"] in {"1-3", "1-10", "11-20"}]
    if not buckets:
        return None

    groups = 1 if engine == "google" else 2
    row_count = (len(buckets) + groups - 1) // groups
    table = doc.add_table(rows=row_count, cols=groups * 3)
    table.autofit = False
    table.alignment = WD_TABLE_ALIGNMENT.LEFT

    group_widths = (1.35, 0.9, 1.25)
    widths = group_widths * groups
    for grid_column, width in zip(table._tbl.tblGrid.gridCol_lst, widths, strict=True):
        grid_column.w = Cm(width)

    for row in table.rows:
        exp._prevent_row_split(row)
        row.height = Cm(0.62)
        row.height_rule = WD_ROW_HEIGHT_RULE.AT_LEAST
        for cell, width in zip(row.cells, widths, strict=True):
            exp._set_cell_width(cell, width)
            exp._set_cell_margins(cell, top=8, bottom=8, left=14, right=14)
            exp._shade_cell(cell, "F0F2F5")
            cell.text = ""

    for index, bucket in enumerate(buckets):
        group = index // row_count
        row_index = index % row_count
        start = group * 3
        label_cell, share_cell, count_cell = table.rows[row_index].cells[start : start + 3]

        label_cell.text = str(bucket["label"])
        share_cell.text = exp._number(bucket.get("share"), "%", decimal_places=0)
        count_cell.text = exp._number(bucket.get("count"), decimal_places=0)

        exp._shade_cell(label_cell, exp.TOPVISOR_COLORS[bucket["label"]])
        _style_text(
            exp,
            label_cell,
            size=11,
            color="FFFFFF",
            bold=True,
            align=WD_ALIGN_PARAGRAPH.CENTER,
        )
        _style_text(
            exp,
            share_cell,
            size=11,
            color="8491A5",
            align=WD_ALIGN_PARAGRAPH.CENTER,
        )
        _style_text(
            exp,
            count_cell,
            size=11,
            color="3D4655",
            align=WD_ALIGN_PARAGRAPH.CENTER,
        )

    occupied = len(buckets)
    capacity = row_count * groups
    if occupied < capacity:
        for index in range(occupied, capacity):
            group = index // row_count
            row_index = index % row_count
            start = group * 3
            for cell in table.rows[row_index].cells[start : start + 3]:
                exp._shade_cell(cell, "FFFFFF")
                exp._set_cell_margins(cell, top=0, bottom=0, left=0, right=0)
                cell.text = ""

    exp._set_table_borders(table, "FFFFFF", size="6")
    exp._keep_small_table_together(table)
    gap = doc.add_paragraph()
    gap.paragraph_format.space_before = Pt(0)
    gap.paragraph_format.space_after = Pt(1)
    gap.paragraph_format.line_spacing = Pt(1)
    gap.add_run("\u200b").font.size = Pt(1)
    return table


def apply():
    global _APPLIED
    if _APPLIED:
        return
    _APPLIED = True

    from . import exporting as exp

    exp._render_distribution_cards_table = lambda doc, distribution, depth, *, engine="yandex": (
        _render_distribution_cards_table(exp, doc, distribution, depth, engine=engine)
    )
    exp.GENERATOR_VERSION = "mvp1.12-2026-09-05"
