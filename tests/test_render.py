"""Выгрузки протокола сверки (Приложение 2 ТЗ): PDF (reportlab), DOCX (python-docx), XML (ElementTree).

Вход — словарь протокола в форме inspector.protocol.build_protocol (пример — tests/data/protocol_example.json)."""
from __future__ import annotations

import copy
import io
import json
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import docx
import pymupdf
import pytest
from docx.enum.section import WD_ORIENT

from inspector.render import render_docx, render_pdf, render_xml

EXAMPLE = Path(__file__).parent / "data" / "protocol_example.json"
UNMATCHED = ("MISSING_EVIDENCE", "NOT_APPLICABLE", "NOT_COMPARABLE", "CLARIFICATION_REQUIRED")


@pytest.fixture()
def proto() -> dict:
    return json.loads(EXAMPLE.read_text(encoding="utf-8"))


def pdf_pages(data: bytes) -> list[str]:
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        return [" ".join(page.get_text().split()) for page in doc]


def pdf_text(data: bytes) -> str:
    return " ".join(pdf_pages(data))


def section(text: str, n: int) -> str:
    """Текст раздела n: от «РАЗДЕЛ n.» до «РАЗДЕЛ n+1.» (для 9 — до конца)."""
    start = text.index(f"РАЗДЕЛ {n}.")
    end = text.find(f"РАЗДЕЛ {n + 1}.", start) if n < 9 else -1
    return text[start:] if end == -1 else text[start:end]


def appendix(text: str, n: int) -> str:
    """Текст приложения n: от «ПРИЛОЖЕНИЕ n.» до «ПРИЛОЖЕНИЕ n+1.» (у последнего — до конца)."""
    start = text.index(f"ПРИЛОЖЕНИЕ {n}.")
    end = text.find(f"ПРИЛОЖЕНИЕ {n + 1}.", start)
    return text[start:] if end == -1 else text[start:end]


def docx_text(data: bytes) -> str:
    d = docx.Document(io.BytesIO(data))
    parts = [p.text for p in d.paragraphs]
    for t in d.tables:
        for row in t.rows:
            parts.extend(cell.text for cell in row.cells)
    for s in d.sections:
        parts.extend(p.text for p in s.footer.paragraphs)
    return " ".join(" ".join(parts).split())


def finalized(proto: dict) -> dict:
    """Инспектор подтвердил KR-057, отклонил KR-059 (OCR), процесс финализирован."""
    p = copy.deepcopy(proto)
    p["process_status"] = "FINALIZED"
    for f in p["findings"]:
        if f["code"] == "KR-057":
            f["status"] = f["finding_status"] = "CONFIRMED_VIOLATION"
            f["inspector"] = {"status": "CONFIRMED_VIOLATION", "user": "inspector",
                              "decided_at": "2026-09-25T08:00:00+00:00", "reason_code": None,
                              "comment": "Подтверждаю: АОСР №1АФ, лист 2"}
        if f["code"] == "KR-059":
            f["status"] = f["finding_status"] = "NEGATIVE_VERIFIED"
            f["inspector"] = {"status": "NEGATIVE_VERIFIED", "user": "inspector",
                              "decided_at": "2026-09-25T08:05:00+00:00", "reason_code": "OCR_ERROR",
                              "comment": "В РД 220 мм, распознано неверно"}
    for prm in p["params"]:
        if prm["code"] == "KR-057":
            prm["status"] = "CONFIRMED_VIOLATION"
        if prm["code"] == "KR-059":
            prm["status"] = "NEGATIVE_VERIFIED"
    p["summary"].update({"CANDIDATE": 0, "CONFIRMED_VIOLATION": 1, "NEGATIVE_VERIFIED": 2})
    return p


EMPTY_PROTOCOLS = [
    {},
    {"protocol_id": "PRT-00000000-v0", "process_id": "00000000", "version": None, "process_status": "PENDING",
     "generated_at": None, "object": {"object_id": "", "name": "", "address": None, "customer": None,
                                      "contractor": "", "permit_number": None},
     "traceability": {}, "scenario": None, "upload_status": [], "summary": {"params_total": 0}, "params": [],
     "findings": [], "files": [], "low_quality_pages": []},
]


# ---------------------------------------------------------------- PDF

def test_pdf_has_all_sections_and_key_values(proto):
    data = render_pdf(proto)
    assert data.startswith(b"%PDF")
    text = pdf_text(data)
    positions = [text.index(f"РАЗДЕЛ {n}.") for n in range(1, 10)]
    assert positions == sorted(positions)                                   # разделы 1–9 по порядку
    for needle in ("ПРОТОКОЛ АВТОМАТИЗИРОВАННОЙ СВЕРКИ № PRT-cf976387-v1", "KR-057", "Форшахта", "SHA-256",
                   "Машинное расхождение является кандидатом", "ОЖИДАЕТ ВЕРИФИКАЦИИ (дозагрузка возможна)",
                   "77-000000-000000-2025", "ООО «Заказчик»", "ООО «Подрядчик»", "Обязательная трассируемость",
                   "07c44ecf9dd0b3f77b3afdbf43a543bb39cb603a9b72c79aeccee462eb43ebeb", "matrix-132-r1.1"):
        assert needle in text, needle


def test_pdf_pages_are_landscape_with_footer(proto):
    data = render_pdf(proto)
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        assert all(page.rect.width > page.rect.height for page in doc)
    pages = pdf_pages(data)
    assert len(pages) >= 3
    for i, page in enumerate(pages, 1):
        assert "PRT-cf976387-v1" in page and f"Стр. {i} из {len(pages)}" in page


def test_pdf_section1_upload_status(proto):
    s1 = section(pdf_text(render_pdf(proto)), 1)
    assert "Полностью" in s1 and "Частично" in s1
    assert "Не читаются: broken.pdf (broken)" in s1
    assert "FULL" in s1                                                     # сценарий загрузки
    assert "F0068" in s1 and "плохой скан" in s1                           # страницы низкого качества


def test_pdf_section2_percentages(proto):
    s2 = section(pdf_text(render_pdf(proto)), 2)
    assert "% от 132" in s2
    assert "Всего параметров в Матрице 132 100%" in s2
    assert "MISSING_EVIDENCE" in s2 and "96,2%" in s2                     # 127 из 132
    assert "не суммируется" in s2


def test_pdf_section3_lists_every_unmatched_param(proto):
    s3 = section(pdf_text(render_pdf(proto)), 3)
    unmatched = [p for p in proto["params"] if p["status"] in UNMATCHED]
    assert len(unmatched) == 129
    assert "— 129" in s3
    for p in unmatched:
        assert p["code"] in s3, p["code"]
    assert "KR-057" not in s3 and "KR-055" not in s3                        # кандидаты и отрицательные — не здесь
    assert "разным корпусам" in s3                                          # причина NOT_COMPARABLE из находки


def test_pdf_table_header_repeats_on_each_page(proto):
    pages = pdf_pages(render_pdf(proto))
    with_header = [p for p in pages if "Комментарий (причина)" in p]
    assert len(with_header) >= 3                                            # 129 строк раздела 3 на нескольких листах


def test_pdf_candidate_sections_by_priority(proto):
    text = pdf_text(render_pdf(proto))
    s4, s5 = section(text, 4), section(text, 5)
    assert "KR-057" in s4 and "KR-059" in s4 and "Форшахта" in s4
    assert "А400" in s4 and "А240" in s4 and "А240С" in s4
    assert "понижение класса" in s4 and "CANDIDATE / PENDING" in s4
    assert "KR-055" not in s4 and "KR-058" not in s4                        # не кандидаты
    assert "KR-057" not in s5


def test_pdf_section6_suspicion_is_not_violation(proto):
    s6 = section(pdf_text(render_pdf(proto)), 6)
    assert "Площадь помещения по экспликации" in s6 and "104" in s6
    assert "Семантический диссонанс" in s6
    assert "SUSPICION: не входит в число нарушений и не используется для обучения" in s6


def with_confidence(proto: dict, promoted: bool = False) -> dict:
    """Гипотезе примера — уверенность из свободного поиска; promoted — инспектор привязал её (SUSPICION → CANDIDATE)."""
    p = copy.deepcopy(proto)
    sus = next(f for f in p["findings"] if f["status"] == "SUSPICION")
    sus["verification"] = {"confidence": 0.75, "basis": "доля совпавших помещений 0,75"}
    sus["confidence"] = 0.75
    if promoted:
        sus["status"] = sus["machine_status"] = "CANDIDATE"
    return p


def test_suspicion_confidence_in_section6_and_xml(proto):
    s6 = section(pdf_text(render_pdf(with_confidence(proto))), 6)
    assert "Уверенность: 0,75 — доля совпавших помещений 0,75" in s6
    assert "Уверенность:" not in section(pdf_text(render_pdf(proto)), 6)      # старый снимок без поля — без строки
    assert "Уверенность: 0,75" in docx_text(render_docx(with_confidence(proto)))
    root = ET.fromstring(render_xml(with_confidence(proto)))
    assert root.find("findings/finding[@code='FREE-AR-EXPLICATION-AREA']").get("confidence") == "0.75"
    assert root.find("findings/finding[@code='KR-057']").get("confidence") is None


def test_promoted_hypothesis_card_shows_confidence_not_empty_verification(proto):
    s8 = section(pdf_text(render_pdf(with_confidence(proto, promoted=True))), 8)
    card = s8[s8.index("FND-FREE-AR-EXPLICATION-AREA"):]
    assert "confidence 0,75 — доля совпавших помещений 0,75" in card
    assert "same_entity: —" not in card                                      # у гипотезы нет судьи и верификатора
    assert "same_entity: YES" in s8                                          # у находок Матрицы строка осталась


def test_pdf_section7_not_formed_before_finalization(proto):
    s7 = section(pdf_text(render_pdf(proto)), 7)
    assert "Не формируется" in s7
    assert "KR-057" not in s7


def test_pdf_section7_lists_confirmed_after_finalization(proto):
    text = pdf_text(render_pdf(finalized(proto)))
    assert "ФИНАЛИЗИРОВАН" in text
    s7 = section(text, 7)
    assert "KR-057" in s7 and "Устранить расхождение: привести к значению ПД" in s7 and "А400" in s7
    assert "Подтверждаю: АОСР №1АФ, лист 2" in s7                          # комментарий инспектора
    assert "KR-059" not in s7                                               # отклонённый — не в резолютивной части
    assert "Не формируется" not in s7


def test_pdf_inspector_decisions_next_to_candidates(proto):
    s4 = section(pdf_text(render_pdf(finalized(proto))), 4)
    assert "Подтверждено" in s4 and "Отклонено" in s4
    assert "Ошибка распознавания (OCR)" in s4 and "В РД 220 мм, распознано неверно" in s4


def test_pdf_section8_evidence_cards(proto):
    s8 = section(pdf_text(render_pdf(proto)), 8)
    assert "FND-KR-057-Форшахта" in s8 and "FND-KR-059-Плиты перекрытия надземной части" in s8
    assert "FND-KR-055" not in s8 and "FND-FREE" not in s8                  # только кандидаты/подтверждённые
    for needle in ("OBJ-NOVOSLOBODSKAYA / KR-057 / matrix-132-r1.1", "source_expected", "source_actual",
                   "approved_change_ref", "completeness_status", "finding_status", "review_priority",
                   "expert_decision", "SHA-256", "a" * 64, "c" * 64, "НВС-2025.03-4.1-КР1", "UNKNOWN",
                   "[0.412, 0.518, 0.468, 0.534]", "F0105", "F0137", "F0068"):
        assert needle in s8, needle


def test_pdf_section9_counting_rules(proto):
    s9 = section(pdf_text(render_pdf(proto)), 9)
    assert "Только CONFIRMED_VIOLATION после решения инспектора" in s9
    assert "SUSPICION; не входят в итог до перевода в CANDIDATE" in s9


def test_pdf_negatives_appendix_lists_machine_negatives(proto):
    text = pdf_text(render_pdf(proto))
    assert text.index("РАЗДЕЛ 9.") < text.index("ПРИЛОЖЕНИЕ 1.") < text.index("ПРИЛОЖЕНИЕ 2. РЕЕСТР ВХОДНЫХ ФАЙЛОВ")
    a1 = appendix(text, 1)
    assert "ПРОВЕРЕННЫЕ ОТРИЦАТЕЛЬНЫЕ (NEGATIVE_VERIFIED) — 1" in a1
    assert "Параметр (код) Локация ПД РД ИД Обоснование" in a1
    assert "KR-055" in a1 and "Фундаментная плита" in a1 and "В30" in a1
    assert "Машинная сверка: расхождение не выявлено" in a1 and "совпадает: В30 = В30" in a1
    for code in ("KR-057", "KR-059", "KR-058", "PZ-009", "FREE-AR"):        # кандидаты, прочие статусы, гипотезы
        assert code not in a1, code
    assert "в приложении 2 к протоколу" in section(text, 1)


def test_pdf_negatives_appendix_includes_candidates_rejected_by_inspector(proto):
    a1 = appendix(pdf_text(render_pdf(finalized(proto))), 1)
    assert "ПРОВЕРЕННЫЕ ОТРИЦАТЕЛЬНЫЕ (NEGATIVE_VERIFIED) — 2" in a1
    assert "KR-055" in a1 and "KR-059" in a1 and "220 мм" in a1 and "200 мм" in a1
    assert "Отклонено" in a1 and "Ошибка распознавания (OCR)" in a1 and "В РД 220 мм, распознано неверно" in a1
    assert "KR-057" not in a1                                               # подтверждённое нарушение — не здесь
    assert a1.index("KR-055") < a1.index("KR-059")                          # порядок Матрицы


def test_negatives_appendix_tolerates_incomplete_findings(proto):
    p = copy.deepcopy(proto)
    p["findings"] += [{"status": "NEGATIVE_VERIFIED"}, "не словарь", {"status": "NEGATIVE_VERIFIED",
                                                                      "inspector": "мусор", "actual_values": None}]
    a1 = appendix(pdf_text(render_pdf(p)), 1)
    assert "ПРОВЕРЕННЫЕ ОТРИЦАТЕЛЬНЫЕ (NEGATIVE_VERIFIED) — 3" in a1 and "KR-055" in a1
    assert "ПРОВЕРЕННЫЕ ОТРИЦАТЕЛЬНЫЕ (NEGATIVE_VERIFIED) — 3" in docx_text(render_docx(p))


def test_pdf_render_time_under_5s(proto):
    render_pdf(proto)                                                       # прогрев: регистрация шрифтов
    t0 = time.perf_counter()
    render_pdf(proto)
    assert time.perf_counter() - t0 < 5.0


def test_pdf_long_text_and_markup_are_safe(proto):
    p = copy.deepcopy(proto)
    f = next(f for f in p["findings"] if f["code"] == "KR-057")
    f["rationale"] = "a < b & c > d <b>не тег</b> \x0c\x00конец " + "очень длинное обоснование " * 900
    f["sources"][0]["snippet"] = "<script>alert(1)</script>"
    f["location"] = "Форшахта & <секция 1>"
    text = pdf_text(render_pdf(p))
    assert "<b>не тег</b>" in text and "Форшахта & <секция 1>" in text
    assert "сокращено в PDF" in text                                         # 24 тыс. знаков — с явной пометкой


def test_pdf_is_deterministic(proto):
    assert render_pdf(proto) == render_pdf(proto)


def test_renders_are_thread_safe(proto):
    """API отдаёт выгрузки из пула потоков: параллельные вызовы дают те же байты, что последовательный."""
    from concurrent.futures import ThreadPoolExecutor

    expected = (render_pdf(proto), render_docx(proto), render_xml(proto))
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda fn: fn(proto), [render_pdf, render_docx, render_xml] * 3))
    assert results == list(expected) * 3


def big_protocol(proto: dict) -> dict:
    """Крупный объект: 60 кандидатов (HIGH/MEDIUM/LOW, часть подтверждена), 20 гипотез, 150 файлов."""
    p = finalized(proto)
    kr = next(f for f in proto["findings"] if f["code"] == "KR-057")
    sus = next(f for f in proto["findings"] if f["status"] == "SUSPICION")
    codes = [x["code"] for x in proto["params"]]
    extra = []
    for i in range(60):
        f = copy.deepcopy(kr)
        f.update(id=100 + i, code=codes[i], location=f"Секция {i % 7 + 1}, этаж {i}", finding_id=f"FND-{codes[i]}-{i}",
                 review_priority=("HIGH", "MEDIUM", "LOW")[i % 3])
        if i % 9 == 0:
            f["rationale"] = "Длинное обоснование судьи с перечислением значений. " * 40
        if i % 4 == 1:
            f["status"] = "CONFIRMED_VIOLATION"
            f["inspector"] = {"status": "CONFIRMED_VIOLATION", "user": "inspector",
                              "decided_at": "2026-09-25T08:00:00+00:00", "reason_code": None,
                              "comment": "Подтверждаю " * 15}
        extra.append(f)
    for i in range(20):
        f = copy.deepcopy(sus)
        f.update(id=1000 + i, location=str(100 + i), finding_id=f"FND-FREE-{i}")
        extra.append(f)
    p["findings"] = p["findings"] + extra
    p["files"] = [dict(proto["files"][i % 4], file_pk=i, file_id=f"F{i:04d}",
                       name=f"Длинное_имя_файла_без_пробелов_том_{i}_раздел_КР_изм_3.pdf") for i in range(150)]
    return p


def test_pdf_big_protocol_layout(proto):
    t0 = time.perf_counter()
    data = render_pdf(big_protocol(proto))
    assert time.perf_counter() - t0 < 15.0
    header = "№ Раздел Параметр (код) Локация ПД РД ИД Отклонение Решение инспектора"
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        for page in doc:
            right = page.rect.width - 14 * 72 / 25.4                            # правое поле 14 мм
            for x0, _, x1, _, text, *_ in page.get_text("blocks"):
                assert x1 <= right + 1, (page.number + 1, text[:60])             # таблицы не выходят за поле
            text = " ".join(page.get_text().split())
            start = 0
            while (i := text.find(header, start)) != -1:
                # шапка таблицы разделов 4–5 — только вверху листа (продолжение) или сразу под заголовком раздела;
                # шапка посреди листа значила бы, что строка разрезана у низа предыдущего куска таблицы
                assert i == 0 or text[:i].rstrip().endswith(tuple(f"РИСКА — {n}" for n in range(100))), \
                    (page.number + 1, text[max(0, i - 80):i])
                start = i + 1


def test_pdf_unbreakable_giant_cell_does_not_crash(proto):
    p = copy.deepcopy(proto)
    f = next(f for f in p["findings"] if f["code"] == "KR-057")
    f["location"] = "Щ" * 6000                                              # узкий столбец, слово без пробелов
    f["delta"] = "Ш" * 6000
    text = pdf_text(render_pdf(p))
    assert "сокращено в PDF" in text


# ---------------------------------------------------------------- DOCX

def test_docx_opens_and_has_sections(proto):
    data = render_docx(proto)
    d = docx.Document(io.BytesIO(data))
    assert d.sections[0].orientation == WD_ORIENT.LANDSCAPE
    assert d.sections[0].page_width > d.sections[0].page_height
    text = docx_text(data)
    for n in range(1, 10):
        assert f"РАЗДЕЛ {n}." in text
    for needle in ("ПРОТОКОЛ АВТОМАТИЗИРОВАННОЙ СВЕРКИ № PRT-cf976387-v1", "KR-057", "Форшахта", "SHA-256",
                   "Не формируется", "SUSPICION: не входит в число нарушений", "a" * 64):
        assert needle in text, needle


def test_docx_finalized_has_resolution(proto):
    text = docx_text(render_docx(finalized(proto)))
    assert "ФИНАЛИЗИРОВАН" in text and "Устранить расхождение: привести к значению ПД" in text
    assert "Ошибка распознавания (OCR)" in text


def test_docx_negatives_table_matches_pdf(proto):
    data = render_docx(finalized(proto))
    assert "ПРИЛОЖЕНИЕ 1. ПРОВЕРЕННЫЕ ОТРИЦАТЕЛЬНЫЕ (NEGATIVE_VERIFIED) — 2" in docx_text(data)
    header = ["№", "Раздел", "Параметр (код)", "Локация", "ПД", "РД", "ИД", "Обоснование"]
    table = next(t for t in docx.Document(io.BytesIO(data)).tables if [c.text for c in t.rows[0].cells] == header)
    assert table.rows[0]._tr.xpath("./w:trPr/w:tblHeader")                  # шапка повторяется на каждой странице
    rows = [[c.text for c in r.cells] for r in table.rows[1:]]
    assert [r[0] for r in rows] == ["1", "2"]
    assert "(KR-055)" in rows[0][2] and rows[0][3] == "Фундаментная плита" and rows[0][4] == "В30"
    assert "Машинная сверка: расхождение не выявлено" in rows[0][7]
    assert "(KR-059)" in rows[1][2] and rows[1][4] == "220 мм" and rows[1][5] == "200 мм" and rows[1][6] == "—"
    assert "Отклонено" in rows[1][7] and "Ошибка распознавания (OCR)" in rows[1][7]


def test_docx_repeats_table_header(proto):
    d = docx.Document(io.BytesIO(render_docx(proto)))
    big = max(d.tables, key=lambda t: len(t.rows))
    assert len(big.rows) >= 130
    assert big.rows[0]._tr.xpath("./w:trPr/w:tblHeader")


def test_docx_is_deterministic(proto):
    assert render_docx(proto) == render_docx(proto)


# ---------------------------------------------------------------- XML

def test_xml_structure(proto):
    data = render_xml(proto)
    assert data.startswith(b'<?xml version="1.0" encoding="UTF-8"?>')
    root = ET.fromstring(data)
    assert root.tag == "protocol"
    assert root.get("id") == "PRT-cf976387-v1" and root.get("version") == "1"
    assert root.get("process_id") == "cf976387-4bc6-4683-bc43-c2ef976f3ff6" and root.get("status") == "READY"
    assert root.find("object").get("object_id") == "OBJ-NOVOSLOBODSKAYA"
    assert root.find("traceability").get("matrix_version") == "matrix-132-r1.1"
    assert [s.get("code") for s in root.findall("upload_status/stage")] == ["PD", "RD", "ID"]
    assert root.find("summary").get("params_total") == "132"
    assert len(root.findall("params/param")) == 132
    assert len(root.findall("findings/finding")) == 7
    assert len(root.findall("files/file")) == 4
    f = root.find("findings/finding[@code='KR-057']")
    assert f.get("id") == "1" and f.get("finding_id") == "FND-KR-057-Форшахта"
    assert f.get("status") == "CANDIDATE" and f.get("review_priority") == "HIGH"
    assert f.findtext("location") == "Форшахта" and f.findtext("expected_value") == "А400"
    assert f.findtext("actual/rd") == "А240" and f.findtext("actual/id") == "А240С"
    assert f.findtext("delta") == "понижение класса: А400 → А240"
    assert f.find("inspector").get("status") == "PENDING"
    sources = f.findall("sources/source")
    assert [s.get("stage") for s in sources] == ["PD", "RD", "ID"]
    assert sources[0].get("file_id") == "F0105" and sources[0].get("sha256") == "a" * 64
    assert sources[0].get("page") == "13" and sources[0].get("bbox") == "0.412,0.518,0.468,0.534"


def test_xml_escapes_markup_and_drops_control_chars(proto):
    p = copy.deepcopy(proto)
    f = next(f for f in p["findings"] if f["code"] == "KR-057")
    f["rationale"] = "a < b & c > d \x0c\x00конец"
    root = ET.fromstring(render_xml(p))
    assert root.find("findings/finding[@code='KR-057']").findtext("rationale") == "a < b & c > d конец"


def test_xml_is_deterministic(proto):
    assert render_xml(proto) == render_xml(proto)


# ---------------------------------------------------------------- пустой протокол и живой протокол из БД

@pytest.mark.parametrize("empty", EMPTY_PROTOCOLS)
def test_empty_protocol_renders(empty):
    text = pdf_text(render_pdf(empty))
    assert "РАЗДЕЛ 1." in text and "РАЗДЕЛ 9." in text and "Не формируется" in text
    assert "(NEGATIVE_VERIFIED) — 0" in text and "Проверенных отрицательных нет." in text
    docx_all = docx_text(render_docx(empty))
    assert "РАЗДЕЛ 9." in docx_all and "Проверенных отрицательных нет." in docx_all
    root = ET.fromstring(render_xml(empty))
    assert root.tag == "protocol" and root.findall("findings/finding") == []


def test_live_protocol_from_db_renders(db, example_process):
    from inspector.protocol import build_protocol

    live = build_protocol(db, example_process, version=1)
    assert "KR-057" in pdf_text(render_pdf(live))
    assert "KR-057" in docx_text(render_docx(live))
    assert len(ET.fromstring(render_xml(live)).findall("findings/finding")) == 7
