"""PDF протокола сверки (Приложение 2 ТЗ): reportlab, A4 альбомная, шрифт DejaVu Sans из inspector/resources/fonts.

Содержание — блоки render/common.py; здесь только вёрстка: таблицы с переносом текста в ячейках и повтором шапки на
каждой странице, колонтитул «протокол · стр. N из M». Выход детерминирован (invariant: без даты создания и
случайного ID документа)."""
from __future__ import annotations

import io
import threading
from functools import partial
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas as rl_canvas
from reportlab.platypus import (CondPageBreak, KeepTogether, Paragraph, SimpleDocTemplate, Table,
                                TableStyle)

from inspector.render import common as C

FONT_DIRS = (Path(__file__).resolve().parent.parent / "resources" / "fonts",
             Path("/usr/share/fonts/truetype/dejavu"))
FONT, FONT_BOLD = "DejaVuSans", "DejaVuSans-Bold"
MARGIN_X, MARGIN_TOP, MARGIN_BOTTOM = 14 * mm, 12 * mm, 16 * mm

INK = colors.HexColor("#1F2328")
MUTED_HEX = "5F6B7A"
NAVY = colors.HexColor("#1F3864")
GRID_COLOR = colors.HexColor("#A7B1BE")
HEAD_BG = colors.HexColor("#DCE4EF")
LABEL_BG = colors.HexColor("#F1F4F8")
ZEBRA = colors.HexColor("#F8FAFC")

FRAME_PAD = 6                # внутренний отступ кадра reportlab (Frame, по умолчанию)
CELL_PAD_X, CELL_PAD_TOP, CELL_PAD_BOTTOM = 3, 2, 3
CELL_SIZE = 8.0
MAX_CELL_LINES = 36          # ячейка выше этого числа строк сокращается (полный текст — в XML/JSON протокола)
TRUNCATED = " … [сокращено в PDF; полный текст — в XML-выгрузке протокола]"

# reportlab держит шрифты и часть кэшей в глобальных объектах: регистрацию и сборку документа сериализуем
_FONTS_LOCK = threading.Lock()
_BUILD_LOCK = threading.Lock()
_fonts_ready = False


def _register_fonts() -> None:
    global _fonts_ready
    with _FONTS_LOCK:
        if _fonts_ready:
            return
        for name, file in ((FONT, "DejaVuSans.ttf"), (FONT_BOLD, "DejaVuSans-Bold.ttf")):
            path = next((d / file for d in FONT_DIRS if (d / file).is_file()), None)
            if path is None:
                raise FileNotFoundError(f"Нет шрифта {file}: ожидается в {FONT_DIRS[0]}")
            pdfmetrics.registerFont(TTFont(name, str(path)))
        pdfmetrics.registerFontFamily(FONT, normal=FONT, bold=FONT_BOLD, italic=FONT, boldItalic=FONT_BOLD)
        _fonts_ready = True


def _styles() -> dict[str, ParagraphStyle]:
    def ps(name: str, **kw) -> ParagraphStyle:
        base = dict(fontName=FONT, fontSize=9, leading=11.5, textColor=INK)
        return ParagraphStyle(name, **{**base, **kw})

    return {
        "title": ps("title", fontName=FONT_BOLD, fontSize=14, leading=18, alignment=TA_CENTER, textColor=NAVY,
                    spaceAfter=5),
        "alert": ps("alert", textColor=colors.HexColor("#9C0006"), backColor=colors.HexColor("#FDECEA"),
                    borderColor=colors.HexColor("#E8B4B0"), borderWidth=0.6, borderPadding=(4, 6, 4, 6),
                    leftIndent=6, rightIndent=6, spaceBefore=6, spaceAfter=10),
        "h1": ps("h1", fontName=FONT_BOLD, fontSize=11, leading=14, textColor=NAVY, spaceBefore=11, spaceAfter=5,
                 keepWithNext=1),
        "h2": ps("h2", fontName=FONT_BOLD, fontSize=9.5, leading=12, textColor=NAVY, spaceBefore=7, spaceAfter=3,
                 keepWithNext=1),
        "h3": ps("h3", fontName=FONT_BOLD, fontSize=9, leading=11.5, spaceBefore=8, spaceAfter=3, keepWithNext=1),
        "body": ps("body", spaceBefore=2, spaceAfter=2),
        "note": ps("note", fontSize=8, leading=10, spaceBefore=2, spaceAfter=3),
        "th": ps("th", fontName=FONT_BOLD, fontSize=CELL_SIZE, leading=10),
        "td": ps("td", fontSize=CELL_SIZE, leading=10, alignment=TA_LEFT),
        "td_C": ps("td_C", fontSize=CELL_SIZE, leading=10, alignment=TA_CENTER),
        "td_R": ps("td_R", fontSize=CELL_SIZE, leading=10, alignment=TA_RIGHT),
        "td_hash": ps("td_hash", fontSize=6.5, leading=8.5),
    }


def _markup(content: tuple, size: float) -> str:
    """Rich-текст (common.Run) → разметка Paragraph; весь текст экранируется."""
    out = []
    for run in content:
        text = escape(run.text).replace("\n", "<br/>")
        if run.small:
            text = f'<font size="{max(size - 1.5, 5.5):g}">{text}</font>'
        color = run.color or (MUTED_HEX if run.muted else None)
        if color:
            text = f'<font color="#{color}">{text}</font>'
        if run.bold:
            text = f"<b>{text}</b>"
        out.append(text)
    return "".join(out)


def _truncate(content: tuple, width: float, size: float) -> tuple:
    """Страховка вёрстки: ячейка длиннее ~MAX_CELL_LINES строк (оценка по ширине столбца) сокращается с пометкой."""
    per_line = max(8, int((width - 2 * CELL_PAD_X) / (0.62 * size)))
    plain = "".join(r.text for r in content)
    lines = pos = 0
    cut = None
    for segment in plain.split("\n"):
        need = max(1, -(-len(segment) // per_line))
        if lines + need > MAX_CELL_LINES:
            cut = pos + (MAX_CELL_LINES - lines) * per_line
            break
        lines += need
        pos += len(segment) + 1
    if cut is None:
        return content
    out, used = [], 0
    for run in content:
        if used + len(run.text) >= cut:
            out.append(C.Run(run.text[:max(0, cut - used)], run.bold, run.small, run.muted, run.color))
            break
        out.append(run)
        used += len(run.text)
    return tuple(out) + (C.Run(TRUNCATED, small=True, muted=True),)


def _row_height(cells: list, col_w: list[float]) -> float:
    return max(p.wrap(w - 2 * CELL_PAD_X, 10 ** 6)[1] for p, w in zip(cells, col_w)) + CELL_PAD_TOP + CELL_PAD_BOTTOM


def _table(grid: C.Grid, width: float, height: float, st: dict[str, ParagraphStyle]) -> Table:
    col_w = [width * grid.span * w for w in grid.widths]
    head = 1 if grid.header else 0
    data = []
    if grid.header:
        data.append([Paragraph(escape(h), st["th"]) for h in grid.header])
    for row in grid.rows:
        cells = []
        for j, w in enumerate(col_w):
            content = row[j] if j < len(row) else C.rich()
            style = st["td_hash"] if j in grid.hash_cols else st.get(
                f"td_{grid.align[j]}" if j < len(grid.align) else "td", st["td"])
            cells.append(Paragraph(_markup(_truncate(content, w, style.fontSize), style.fontSize), style))
        data.append(cells)
    # Строки рвутся только между собой; разрыв внутри строки — лишь если строка не помещается даже на пустой
    # странице (иначе reportlab режет строку у каждого низа листа и повторяет шапку посреди страницы).
    head_h = _row_height(data[0], col_w) if head else 0.0
    tallest = max((_row_height(r, col_w) for r in data[head:]), default=0.0)
    split_in_row = 24 if tallest > height - head_h - 2 else 0
    table = Table(data, colWidths=col_w, repeatRows=head, splitByRow=1, splitInRow=split_in_row, hAlign="LEFT")
    cmds: list = [("GRID", (0, 0), (-1, -1), 0.4, GRID_COLOR), ("VALIGN", (0, 0), (-1, -1), "TOP"),
                  ("LEFTPADDING", (0, 0), (-1, -1), CELL_PAD_X), ("RIGHTPADDING", (0, 0), (-1, -1), CELL_PAD_X),
                  ("TOPPADDING", (0, 0), (-1, -1), CELL_PAD_TOP),
                  ("BOTTOMPADDING", (0, 0), (-1, -1), CELL_PAD_BOTTOM)]
    if head:
        cmds.append(("BACKGROUND", (0, 0), (-1, 0), HEAD_BG))
    if grid.zebra and len(grid.rows) > 3:
        cmds.append(("ROWBACKGROUNDS", (0, head), (-1, -1), [colors.white, ZEBRA]))
    for col in grid.label_cols:
        cmds.append(("BACKGROUND", (col, head), (col, -1), LABEL_BG))
    for (r, col), fill in sorted(grid.fills.items()):
        start, end = ((0, r + head), (-1, r + head)) if col < 0 else ((col, r + head), (col, r + head))
        cmds.append(("BACKGROUND", start, end, colors.HexColor("#" + fill)))
    table.setStyle(TableStyle(cmds))
    return table


def _flowables(blocks: list, width: float, height: float, st: dict[str, ParagraphStyle]) -> list:
    out: list = []
    for blk in blocks:
        if isinstance(blk, C.Title):
            out.append(Paragraph(escape(blk.text), st["title"]))
        elif isinstance(blk, C.Heading):
            if blk.level == 1:
                out.append(CondPageBreak(45 * mm))       # заголовок раздела не остаётся один внизу листа
            out.append(Paragraph(escape(blk.text), st[f"h{min(max(blk.level, 1), 3)}"]))
        elif isinstance(blk, C.Para):
            style = st.get(blk.style, st["body"])
            out.append(Paragraph(_markup(blk.content, style.fontSize), style))
        elif isinstance(blk, C.Grid):
            out.append(_table(blk, width, height, st))
        elif isinstance(blk, C.Keep):
            out.append(KeepTogether(_flowables(blk.blocks, width, height, st)))
    return out


class _NumberedCanvas(rl_canvas.Canvas):
    """Колонтитул «протокол · стр. N из M»: страницы копятся до save(), когда известно их число."""

    def __init__(self, *args, footer: str = "", **kwargs):
        super().__init__(*args, **kwargs)
        self._footer = footer
        self._page_states: list[dict] = []

    def showPage(self):  # noqa: N802 — имя метода reportlab
        self._page_states.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        total = len(self._page_states)
        for state in self._page_states:
            self.__dict__.update(state)
            self._draw_footer(total)
            super().showPage()
        super().save()

    def _draw_footer(self, total: int) -> None:
        width, _ = self._pagesize
        self.saveState()
        self.setStrokeColor(GRID_COLOR)
        self.setLineWidth(0.4)
        self.line(MARGIN_X, 11 * mm, width - MARGIN_X, 11 * mm)
        self.setFont(FONT, 7)
        self.setFillColor(colors.HexColor("#" + MUTED_HEX))
        self.drawString(MARGIN_X, 7 * mm, self._footer)
        self.drawRightString(width - MARGIN_X, 7 * mm, f"Стр. {self._pageNumber} из {total}")
        self.restoreState()


def render_pdf(protocol: dict) -> bytes:
    """Протокол (словарь build_protocol) → PDF, A4 альбомная."""
    _register_fonts()
    meta = C.meta(protocol)
    blocks = C.build_blocks(protocol)
    buf = io.BytesIO()
    with _BUILD_LOCK:
        doc = SimpleDocTemplate(buf, pagesize=landscape(A4), leftMargin=MARGIN_X, rightMargin=MARGIN_X,
                                topMargin=MARGIN_TOP, bottomMargin=MARGIN_BOTTOM, title=meta.title,
                                author="Инспектор ИИ", subject=meta.subject, creator="Инспектор ИИ",
                                invariant=1, lang="ru-RU")
        # полезная область кадра SimpleDocTemplate: поля минус внутренние отступы кадра (по 6 pt с каждой стороны)
        story = _flowables(blocks, doc.width - 2 * FRAME_PAD, doc.height - 2 * FRAME_PAD, _styles())
        doc.build(story, canvasmaker=partial(_NumberedCanvas, footer=meta.footer))
    return buf.getvalue()
