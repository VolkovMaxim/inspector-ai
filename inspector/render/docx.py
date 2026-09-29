"""DOCX протокола сверки (Приложение 2 ТЗ): python-docx, A4 альбомная; те же блоки, что у PDF (render/common.py).

Таблицы — с фиксированными ширинами столбцов и повтором строки заголовка на каждой странице; колонтитул с полями
PAGE/NUMPAGES. Выход детерминирован: метаданные берутся из протокола, время записей ZIP фиксировано."""
from __future__ import annotations

import datetime as dt
import io
import zipfile

from docx import Document
from docx.enum.section import WD_ORIENT
from docx.enum.style import WD_STYLE_TYPE
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_TAB_ALIGNMENT
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Mm, Pt, RGBColor

from inspector.render import common as C

FONT = "Arial"
PAGE_W, PAGE_H = 297.0, 210.0            # мм, альбомная
MARGIN_X, MARGIN_TOP, MARGIN_BOTTOM = 14.0, 12.0, 14.0
TEXT_W = PAGE_W - 2 * MARGIN_X
NAVY = "1F3864"
MUTED = "5F6B7A"
HEAD_BG, LABEL_BG, ZEBRA = "DCE4EF", "F1F4F8", "F8FAFC"
ALERT_TEXT, ALERT_BG = "9C0006", "FDECEA"
CELL_SIZE = 8.0
ALIGN = {"L": WD_ALIGN_PARAGRAPH.LEFT, "C": WD_ALIGN_PARAGRAPH.CENTER, "R": WD_ALIGN_PARAGRAPH.RIGHT}
ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)
FALLBACK_TS = dt.datetime(2000, 1, 1, tzinfo=dt.timezone.utc)


def _set_font(style, size: float, bold: bool | None = None, color: str | None = None) -> None:
    """Шрифт стиля без тем Office: явные rFonts (иначе Word берёт шрифт темы) и язык ru-RU."""
    style.font.name = FONT
    style.font.size = Pt(size)
    if bold is not None:
        style.font.bold = bold
    if color:
        style.font.color.rgb = RGBColor.from_string(color)
    rpr = style.element.get_or_add_rPr()
    fonts = rpr.find(qn("w:rFonts"))
    if fonts is None:
        fonts = OxmlElement("w:rFonts")
        rpr.insert(0, fonts)
    for attr in ("w:asciiTheme", "w:hAnsiTheme", "w:eastAsiaTheme", "w:cstheme"):
        fonts.attrib.pop(qn(attr), None)
    for attr in ("w:ascii", "w:hAnsi", "w:cs", "w:eastAsia"):
        fonts.set(qn(attr), FONT)
    lang = rpr.find(qn("w:lang"))
    if lang is None:
        lang = OxmlElement("w:lang")
        rpr.append(lang)
    lang.set(qn("w:val"), "ru-RU")


def _setup(doc) -> None:
    sec = doc.sections[0]
    sec.orientation = WD_ORIENT.LANDSCAPE
    sec.page_width, sec.page_height = Mm(PAGE_W), Mm(PAGE_H)
    sec.left_margin = sec.right_margin = Mm(MARGIN_X)
    sec.top_margin, sec.bottom_margin = Mm(MARGIN_TOP), Mm(MARGIN_BOTTOM)
    sec.header_distance, sec.footer_distance = Mm(6), Mm(6)
    styles = doc.styles
    _set_font(styles["Normal"], 9)
    styles["Normal"].paragraph_format.space_after = Pt(2)
    for name, size, before, after in (("Heading 1", 11, 10, 4), ("Heading 2", 9.5, 7, 3), ("Heading 3", 9, 7, 2)):
        st = styles[name]
        _set_font(st, size, bold=True, color=NAVY if name != "Heading 3" else "1F2328")
        st.font.italic = False
        st.paragraph_format.space_before, st.paragraph_format.space_after = Pt(before), Pt(after)
        st.paragraph_format.keep_with_next = True
    for name, size, bold in (("Cell", CELL_SIZE, False), ("CellHead", CELL_SIZE, True), ("CellHash", 6.5, False)):
        st = styles.add_style(name, WD_STYLE_TYPE.PARAGRAPH)
        st.base_style = styles["Normal"]
        _set_font(st, size, bold=bold)
        st.paragraph_format.space_before = st.paragraph_format.space_after = Pt(0)
        st.paragraph_format.line_spacing = 1.0


def _add_runs(paragraph, content: tuple, size: float) -> None:
    for run in content:
        r = paragraph.add_run(run.text)          # «\n» python-docx превращает в перенос строки <w:br/>
        if run.bold:
            r.bold = True
        r.font.size = Pt(max(size - 1.5, 5.5) if run.small else size)
        color = run.color or (MUTED if run.muted else None)
        if color:
            r.font.color.rgb = RGBColor.from_string(color)


def _shade(cell, fill: str) -> None:
    tc_pr = cell._tc.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), fill)
    tc_pr.append(shd)


def _compact_margins(table) -> None:
    mar = OxmlElement("w:tblCellMar")
    for side, w in (("top", 20), ("left", 57), ("bottom", 20), ("right", 57)):
        el = OxmlElement(f"w:{side}")
        el.set(qn("w:w"), str(w))
        el.set(qn("w:type"), "dxa")
        mar.append(el)
    tbl_pr = table._tbl.tblPr
    look = tbl_pr.find(qn("w:tblLook"))
    if look is not None:
        look.addprevious(mar)                    # порядок элементов tblPr по схеме: tblCellMar перед tblLook
    else:
        tbl_pr.append(mar)


def _keep_on_page(table) -> None:
    """Таблица целиком на одной странице, если помещается: строки не рвутся, абзацы «не отрывать от следующего»."""
    rows = list(table.rows)
    for i, row in enumerate(rows):
        tr_pr = row._tr.get_or_add_trPr()
        tr_pr.insert(0, OxmlElement("w:cantSplit"))  # по схеме cantSplit идёт раньше tblHeader
        if i < len(rows) - 1:
            for cell in row.cells:
                for par in cell.paragraphs:
                    par.paragraph_format.keep_with_next = True


def _table(doc, grid: C.Grid, keep: bool = False) -> None:
    sid = {name: doc.styles[name].style_id for name in ("Cell", "CellHead", "CellHash")}
    widths = [Mm(TEXT_W * grid.span * w) for w in grid.widths]
    table = doc.add_table(rows=0, cols=len(widths))
    table.style = doc.styles["Table Grid"]
    table.autofit = False
    table.alignment = WD_TABLE_ALIGNMENT.LEFT
    for col, w in zip(table.columns, widths):
        col.width = w
    _compact_margins(table)
    if grid.header:
        row = table.add_row()
        tr_pr = row._tr.get_or_add_trPr()
        repeat = OxmlElement("w:tblHeader")      # повтор строки заголовка на каждой странице
        repeat.set(qn("w:val"), "true")
        tr_pr.append(repeat)
        for cell, text in zip(row.cells, grid.header):
            par = cell.paragraphs[0]
            par._p.style = sid["CellHead"]         # id стиля напрямую: сеттер python-docx на ячейку медленный
            par.add_run(C.clean(text))
            _shade(cell, HEAD_BG)
    for i, values in enumerate(grid.rows):
        row = table.add_row()
        row_fill = grid.fills.get((i, -1))
        for j, cell in enumerate(row.cells):
            content = values[j] if j < len(values) else C.rich()
            par = cell.paragraphs[0]
            hashed = j in grid.hash_cols
            par._p.style = sid["CellHash" if hashed else "Cell"]
            if j < len(grid.align):
                par.alignment = ALIGN.get(grid.align[j], WD_ALIGN_PARAGRAPH.LEFT)
            _add_runs(par, content, 6.5 if hashed else CELL_SIZE)
            fill = (grid.fills.get((i, j)) or row_fill or (LABEL_BG if j in grid.label_cols else None)
                    or (ZEBRA if grid.zebra and len(grid.rows) > 3 and i % 2 else None))
            if fill:
                _shade(cell, fill)
    if keep:
        _keep_on_page(table)
    doc.add_paragraph().paragraph_format.space_after = Pt(2)   # отступ после таблицы


def _alert(doc, content: tuple) -> None:
    """Пометка под заголовком: рамка с заливкой (таблица из одной ячейки), красный текст."""
    table = doc.add_table(rows=1, cols=1)
    table.style = doc.styles["Table Grid"]
    table.autofit = False
    table.columns[0].width = Mm(TEXT_W)
    cell = table.rows[0].cells[0]
    cell.width = Mm(TEXT_W)
    par = cell.paragraphs[0]
    _add_runs(par, tuple(C.Run(r.text, r.bold, r.small, r.muted, r.color or ALERT_TEXT) for r in content), 9)
    _shade(cell, ALERT_BG)
    doc.add_paragraph().paragraph_format.space_after = Pt(2)


def _field(paragraph, instr: str) -> None:
    """Поле Word (PAGE, NUMPAGES): begin / instrText / separate / значение / end."""
    def run_with(el):
        r = paragraph.add_run()
        r._r.append(el)
        return r

    begin = OxmlElement("w:fldChar")
    begin.set(qn("w:fldCharType"), "begin")
    run_with(begin)
    code = OxmlElement("w:instrText")
    code.set(qn("xml:space"), "preserve")
    code.text = f" {instr} "
    run_with(code)
    sep = OxmlElement("w:fldChar")
    sep.set(qn("w:fldCharType"), "separate")
    run_with(sep)
    paragraph.add_run("1")
    end = OxmlElement("w:fldChar")
    end.set(qn("w:fldCharType"), "end")
    run_with(end)


def _footer(doc, text: str) -> None:
    par = doc.sections[0].footer.paragraphs[0]
    par.style = doc.styles["Normal"]             # у стиля Footer шаблона свои табуляции (6,5″) — номер уехал бы
    par.paragraph_format.tab_stops.add_tab_stop(Mm(TEXT_W), WD_TAB_ALIGNMENT.RIGHT)
    par.add_run(C.clean(text))
    par.add_run("\tСтр. ")
    _field(par, "PAGE")
    par.add_run(" из ")
    _field(par, "NUMPAGES")
    for r in par.runs:
        r.font.size = Pt(7)
        r.font.color.rgb = RGBColor.from_string(MUTED)


def _core_properties(doc, protocol: dict, meta: C.Meta) -> None:
    cp = doc.core_properties
    cp.title = meta.title
    cp.subject = meta.subject
    cp.author = cp.last_modified_by = "Инспектор ИИ"
    cp.comments = "Протокол автоматизированной сверки ПД/РД/ИД (Приложение 2 ТЗ)"
    cp.category = "Протокол сверки"
    cp.keywords = meta.protocol_id
    cp.language = "ru-RU"
    ts = (meta.generated_at or FALLBACK_TS).astimezone(dt.timezone.utc).replace(microsecond=0)
    cp.created = cp.modified = ts            # python-docx пишет значение как UTC («…Z»)
    cp.revision = max(1, C.as_int(protocol.get("version")) if isinstance(protocol, dict) else 1)


def _normalize_zip(data: bytes) -> bytes:
    """Фиксированное время записей ZIP — одинаковый протокол даёт побайтно одинаковый DOCX."""
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as src, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        for info in src.infolist():
            item = zipfile.ZipInfo(info.filename, date_time=ZIP_EPOCH)
            item.compress_type = zipfile.ZIP_DEFLATED
            item.external_attr = 0o644 << 16
            dst.writestr(item, src.read(info.filename))
    return out.getvalue()


def _emit(doc, blk, keep: bool = False) -> None:
    if isinstance(blk, C.Title):
        par = doc.add_paragraph()
        par.alignment = WD_ALIGN_PARAGRAPH.CENTER
        run = par.add_run(C.clean(blk.text))
        run.bold, run.font.size = True, Pt(14)
        run.font.color.rgb = RGBColor.from_string(NAVY)
    elif isinstance(blk, C.Heading):
        doc.add_heading(C.clean(blk.text), level=min(max(blk.level, 1), 3))
    elif isinstance(blk, C.Para):
        if blk.style == "alert":
            _alert(doc, blk.content)
        else:
            _add_runs(doc.add_paragraph(), blk.content, 8 if blk.style == "note" else 9)
    elif isinstance(blk, C.Grid):
        _table(doc, blk, keep=keep)
    elif isinstance(blk, C.Keep):
        for inner in blk.blocks:
            _emit(doc, inner, keep=True)


def render_docx(protocol: dict) -> bytes:
    """Протокол (словарь build_protocol) → DOCX, A4 альбомная."""
    meta = C.meta(protocol)
    doc = Document()
    _setup(doc)
    for blk in C.build_blocks(protocol):
        _emit(doc, blk)
    _footer(doc, meta.footer)
    _core_properties(doc, protocol, meta)
    buf = io.BytesIO()
    doc.save(buf)
    return _normalize_zip(buf.getvalue())
