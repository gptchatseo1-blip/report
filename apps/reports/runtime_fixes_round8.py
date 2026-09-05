"""Final report/table polish requested on 2026-09-06."""

import math

_APPLIED = False


def _compact_distribution_cards_table(exp, doc, distribution, depth, *, engine="yandex"):
    buckets = exp._topvisor_buckets(distribution, 20 if engine == "google" else depth)
    if engine == "google":
        buckets = [bucket for bucket in buckets if bucket["label"] in {"1-3", "1-10", "11-20"}]
    if not buckets:
        return None

    columns = 1 if engine == "google" else 2
    row_count = math.ceil(len(buckets) / columns)
    table = doc.add_table(rows=row_count, cols=columns)
    table.autofit = False
    outer_width = 4.15 if columns == 2 else 4.35

    for row in table.rows:
        exp._prevent_row_split(row)
        for cell in row.cells:
            exp._set_cell_width(cell, outer_width)
            exp._set_cell_margins(cell, top=5, bottom=5, left=18, right=18)
            exp._shade_cell(cell, "F0F2F5")
            cell.vertical_alignment = exp.WD_CELL_VERTICAL_ALIGNMENT.CENTER
            cell.text = ""

    for index, bucket in enumerate(buckets):
        column = index // row_count
        row_index = index % row_count
        outer = table.rows[row_index].cells[column]
        nested = outer.add_table(rows=1, cols=3)
        nested.autofit = False
        widths = (1.35, 0.95, 1.45)
        for grid_column, width in zip(nested._tbl.tblGrid.gridCol_lst, widths, strict=True):
            grid_column.w = exp.Cm(width)
        label_cell, share_cell, count_cell = nested.rows[0].cells
        for cell, width in zip((label_cell, share_cell, count_cell), widths, strict=True):
            exp._set_cell_width(cell, width)
            exp._set_cell_margins(cell, top=4, bottom=4, left=10, right=10)
            exp._shade_cell(cell, "F0F2F5")
            cell.vertical_alignment = exp.WD_CELL_VERTICAL_ALIGNMENT.CENTER
        exp._shade_cell(label_cell, exp.TOPVISOR_COLORS[bucket["label"]])
        label_cell.text = str(bucket["label"])
        share_cell.text = exp._number(bucket.get("share"), "%", decimal_places=0)
        count_cell.text = exp._number(bucket.get("count"), decimal_places=0)
        for paragraph in label_cell.paragraphs:
            paragraph.alignment = exp.WD_ALIGN_PARAGRAPH.CENTER
            paragraph.paragraph_format.space_before = exp.Pt(0)
            paragraph.paragraph_format.space_after = exp.Pt(0)
            for run in paragraph.runs:
                exp._style_run(run, size=11, color="FFFFFF", bold=True)
        for paragraph in share_cell.paragraphs:
            paragraph.alignment = exp.WD_ALIGN_PARAGRAPH.CENTER
            paragraph.paragraph_format.space_before = exp.Pt(0)
            paragraph.paragraph_format.space_after = exp.Pt(0)
            for run in paragraph.runs:
                exp._style_run(run, size=11, color="8491A5")
        for paragraph in count_cell.paragraphs:
            paragraph.alignment = exp.WD_ALIGN_PARAGRAPH.CENTER
            paragraph.paragraph_format.space_before = exp.Pt(0)
            paragraph.paragraph_format.space_after = exp.Pt(0)
            for run in paragraph.runs:
                exp._style_run(run, size=11, color="3D4655")
        exp._set_table_borders(nested, "F0F2F5", size="0")
        for paragraph in outer.paragraphs:
            paragraph.paragraph_format.space_before = exp.Pt(0)
            paragraph.paragraph_format.space_after = exp.Pt(0)

    # Empty cells must not look like placeholder rows/blocks.
    occupied = {(index % row_count, index // row_count) for index in range(len(buckets))}
    for row_index, row in enumerate(table.rows):
        for column, cell in enumerate(row.cells):
            if (row_index, column) in occupied:
                continue
            exp._shade_cell(cell, "FFFFFF")
            exp._set_cell_margins(cell, top=0, bottom=0, left=0, right=0)

    exp._set_table_borders(table, "FFFFFF", size="4")
    exp._keep_small_table_together(table)
    gap = doc.add_paragraph()
    gap.paragraph_format.space_before = exp.Pt(0)
    gap.paragraph_format.space_after = exp.Pt(1)
    gap.add_run("\u200b").font.size = exp.Pt(1)
    return table


def apply():
    global _APPLIED
    if _APPLIED:
        return
    _APPLIED = True

    from . import exporting as exp

    current_monthly_renderer = exp._render_monthly_topvisor_table

    def render_monthly_table(doc, segment, *, show_visibility=True):
        # The monthly dynamics table always includes visibility immediately after month.
        # Its active rows/manual values are already supplied through monthly_table_series.
        return current_monthly_renderer(doc, segment, show_visibility=True)

    exp.GENERATOR_VERSION = "mvp1.11-2026-09-06"
    exp._render_monthly_topvisor_table = render_monthly_table
    exp._render_distribution_cards_table = (
        lambda doc, distribution, depth, *, engine="yandex": _compact_distribution_cards_table(
            exp, doc, distribution, depth, engine=engine
        )
    )
