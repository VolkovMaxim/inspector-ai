"""Помощники для тестов и разработки: пример процесса с находками, генерация маленьких PDF с кириллицей."""
from __future__ import annotations

from pathlib import Path

from sqlalchemy.orm import Session

from inspector import statuses as S
from inspector.models import ConstructionObject, EvidenceFragment, File, Finding, Page, Process

FONT_CANDIDATES = (Path(__file__).parent / "resources/fonts/DejaVuSans.ttf",
                   Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"))


def font_path() -> str:
    for p in FONT_CANDIDATES:
        if p.exists():
            return str(p)
    raise FileNotFoundError("Нет шрифта DejaVuSans.ttf (resources/fonts или /usr/share/fonts/truetype/dejavu)")


def make_pdf(path: Path, pages: list[str], size: tuple[float, float] = (595, 842), rotate: int = 0) -> Path:
    """PDF с текстовым слоем: каждая строка списка — страница (перенос строк сохраняется)."""
    import pymupdf

    doc = pymupdf.open()
    for text in pages:
        page = doc.new_page(width=size[0], height=size[1])
        y = 60.0
        for line in text.split("\n"):
            page.insert_text((50, y), line, fontsize=11, fontfile=font_path(), fontname="dejavu")
            y += 16
        if rotate:
            page.set_rotation(rotate)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(path)
    return path


def make_scan_pdf(path: Path, pages: list[str], rotate: int = 0, size: tuple[float, float] = (595, 842)) -> Path:
    """«Скан»: страница-картинка без текстового слоя (текст отрисован и растрирован). rotate — скан повёрнутого листа:
    содержимое на картинке повёрнуто на rotate° по часовой, а /Rotate у страницы 0."""
    import pymupdf

    src = pymupdf.open(make_pdf(path.with_suffix(".src.pdf"), pages, size=size, rotate=rotate))
    out = pymupdf.open()
    for page in src:
        pix = page.get_pixmap(dpi=150)
        p = out.new_page(width=page.rect.width, height=page.rect.height)
        p.insert_image(p.rect, stream=pix.tobytes("png"))
    out.save(path)
    path.with_suffix(".src.pdf").unlink()
    return path


def build_example_process(db: Session) -> Process:
    """Процесс Новослободской с правдоподобными находками всех статусов (без реальных файлов)."""
    obj = ConstructionObject(object_code="OBJ-NOVOSLOBODSKAYA", name="Жилой дом, Новослободская",
                             address="г. Москва, ул. Новослободская", customer="ООО «Заказчик»",
                             contractor="ООО «Подрядчик»", permit_number="77-000000-000000-2025")
    db.add(obj)
    db.flush()
    p = Process(object_pk=obj.id, status=S.READY, scenario="FULL", protocol_version=1)
    db.add(p)
    db.flush()
    files = [File(process_id=p.id, file_id="F0105", original_name="НВС-2025.03-4.1-КР1.pdf", ext=".pdf",
                  sha256="a" * 64, doc_stage="PD", stage_source="declared", status="ok", pages=29,
                  document_code="НВС-2025.03-4.1-КР1", revision="0", discipline="КР"),
             File(process_id=p.id, file_id="F0137", original_name="2025-12-23 Новослободская _СВГ (1).pdf",
                  ext=".pdf", sha256="b" * 64, doc_stage="RD", stage_source="declared", status="ok", pages=14,
                  document_code="НВС-2025.03-КЖ.СВГ", revision="1", discipline="КР"),
             File(process_id=p.id, file_id="F0068", original_name="АОСР №1АФ от 11.02.2026.pdf", ext=".pdf",
                  sha256="c" * 64, doc_stage="ID", stage_source="declared", status="ok", pages=10),
             File(process_id=p.id, file_id="LOCAL-0badf11e", original_name="broken.pdf", ext=".pdf",
                  sha256="d" * 64, doc_stage="ID", stage_source="declared", status="broken",
                  error="PDF повреждён: cannot open broken document")]
    db.add_all(files)
    db.flush()
    for f in files[:3]:
        for n in range(1, f.pages + 1):
            db.add(Page(file_pk=f.id, page_no=n, width=595, height=842,
                        text_source="ocr" if f.file_id == "F0068" else "layer",
                        low_quality=(f.file_id == "F0068" and n == 7),
                        quality_label="плохой скан" if (f.file_id == "F0068" and n == 7) else "хорошее"))

    def finding(code, loc, status, exp, rd, idv, label, pstatus, include, rationale, ev, **kw):
        fd = Finding(process_id=p.id, finding_id=f"FND-{code}-{loc or 'param'}", source=kw.get("source", "matrix"),
                     param_code=code, rule_version=kw.get("rule", "matrix-132-r1.1"), location=loc, expected_value=exp,
                     actual_rd=rd, actual_id=idv, delta=kw.get("delta"), status=status,
                     review_priority=kw.get("prio", "HIGH"), criticality=kw.get("crit", "Критическое (приостановка работ)"),
                     rationale=rationale, verification=kw.get("verification"), discovery_method=kw.get("dm"),
                     submission_label=label, submission_status=pstatus, submission_include=include)
        db.add(fd)
        db.flush()
        for role, stage, fpk, page, bbox, val, snip in ev:
            db.add(EvidenceFragment(finding_pk=fd.id, role=role, stage=stage, file_pk=fpk, page_no=page, bbox=bbox,
                                    bbox_source="layer" if bbox else "page", value=val, snippet=snip))
        return fd

    pd, rd, idf = files[0].id, files[1].id, files[2].id
    finding("KR-057", "Форшахта", S.CANDIDATE, "А400", "А240", "А240С", "VIOLATION_PRESENT", "CRITICAL", True,
            "ПД: арматура форшахты А400; РД и АОСР: А240 — понижение класса арматуры, триггер выполнен.",
            [("expected", "PD", pd, 13, [0.412, 0.518, 0.468, 0.534], "А400", "Арматура форшахты класса А400"),
             ("actual_rd", "RD", rd, 2, [0.61, 0.33, 0.66, 0.35], "А240", "Ø12 А240 шаг 200"),
             ("actual_id", "ID", idf, 2, [0.2, 0.44, 0.27, 0.46], "А240С", "арматура класса А240С")],
            delta="понижение класса: А400 → А240",
            verification={"same_entity": "YES", "trigger_met": "YES", "analysis": "Одна и та же конструкция — форшахта."})
    finding("KR-055", "Фундаментная плита", S.NEGATIVE_VERIFIED, "В30", "В30", None, "NO_VIOLATION", "OK", True,
            "Класс бетона фундаментной плиты совпадает: В30 = В30.",
            [("expected", "PD", pd, 9, [0.1, 0.2, 0.3, 0.22], "В30", "бетон класса В30 W8 F150"),
             ("actual_rd", "RD", rd, 3, None, "В30", "Бетон В30")])
    finding("KR-058", "Ростверк", S.NOT_COMPARABLE, "600 мм", "900 мм", None, "COMPARISON_IMPOSSIBLE",
            "COMPARISON_IMPOSSIBLE", True, "Значения относятся к разным корпусам — сопоставить нельзя.",
            [("expected", "PD", pd, 11, None, "600 мм", "толщина ростверка 600 мм"),
             ("actual_rd", "RD", rd, 5, None, "900 мм", "ростверк h=900")],
            verification={"same_entity": "NO", "trigger_met": "UNSURE", "analysis": "Разные корпуса"})
    finding("KR-059", "Плиты перекрытия надземной части", S.CANDIDATE, "220 мм", "200 мм", None, "VIOLATION_PRESENT",
            "CRITICAL", True, "Толщина плит перекрытия уменьшена: 220 → 200 мм.",
            [("expected", "PD", pd, 15, [0.3, 0.6, 0.36, 0.62], "220 мм", "толщина плит перекрытий 220 мм"),
             ("actual_rd", "RD", rd, 7, [0.5, 0.1, 0.55, 0.12], "200 мм", "Плита П1 δ=200")],
            delta="−20 мм", verification={"same_entity": "YES", "trigger_met": "YES", "analysis": "Одни и те же плиты"})
    finding("PZ-009", "", S.MISSING_EVIDENCE, None, None, None, None, None, False,
            "Значение ПД не найдено в документах объекта", [])
    finding("POD-093", "", S.NOT_APPLICABLE, None, None, None, None, None, False,
            "Раздел ПОД отсутствует в документации объекта — параметр неприменим", [], prio="MEDIUM",
            crit="Существенное (предписание)")
    finding("FREE-AR-EXPLICATION-AREA", "104", S.SUSPICION, "12,4", "13,1", None, "VIOLATION_PRESENT", "WARNING", True,
            "Экспликация помещений: AREA_CHANGED, Δ=0.7 м²",
            [("expected", "PD", pd, 20, None, "12,4", None), ("actual_rd", "RD", rd, 4, None, "13,1", None)],
            source="explication", rule="freesearch-explication-v1", dm="SEMANTIC_DISSONANCE", prio="MEDIUM",
            crit="Существенное (предписание)")
    db.flush()
    return p
