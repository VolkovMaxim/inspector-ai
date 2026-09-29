import pytest
from sqlalchemy import select

from inspector import statuses as S
from inspector.models import ConstructionObject, File, Finding, Page, Process
from inspector.pipeline.freesearch import (best_pairs, compare_flats, compare_rooms, explication_confidence,
                                           flat_confidence, parse_quads, parse_tables, run_freesearch)


def table(rows, total):
    body = "".join(f"{n}\n{name}\n{area}\n" for n, name, area in rows)
    return f"Экспликация помещений\n{body}Итого:\n{total}\n"


PD = table([("1", "Тамбур", "12,40"), ("2", "Коридор", "30,10"), ("3", "Помещение", "18,00"), ("4", "Санузел", "4,50")], "65,00")
RD = table([("1", "Тамбур", "12,40"), ("2", "Коридор", "30,10"), ("3", "Раздевалка женская", "18,00"),
            ("4", "Санузел", "5,20")], "65,70")


def test_explication_diff_finds_rename_area_and_total():
    pd_t, rd_t = parse_tables(PD), parse_tables(RD)
    assert pd_t[0]["parsed_ok"] and rd_t[0]["total"] == 65.7
    pairs = best_pairs(pd_t, rd_t)
    assert len(pairs) == 1
    found = {(f["room"], f["type"]) for f in compare_rooms(pairs[0][0]["rooms"], pairs[0][1]["rooms"])}
    assert found == {("3", "RENAMED"), ("4", "AREA_CHANGED")}


def test_apartment_quads():
    pd = parse_quads("21,34\n38,50\n40,10\n1.1.1\n")
    rd = parse_quads("15,64\n38,50\n40,10\n1.1.1\n")
    found = compare_flats([{**q, "file_pk": 1, "page_no": 1} for q in pd], [{**q, "file_pk": 2, "page_no": 3} for q in rd])
    assert len(found) == 1 and found[0]["changed"] == ["living"]


# ---------------------------------------------------------------- уверенность гипотез


@pytest.mark.parametrize("similarity, pd_ok, rd_ok, expected", [
    (1.0, True, True, 1.0),                  # все помещения совпали, обе таблицы сошлись с итогом
    (0.75, True, True, 0.75),
    (0.75, True, False, 0.56),               # одна таблица разобрана не полностью — ×0,75
    (0.4, False, False, 0.2),                # порог пары и ни одна таблица не сошлась — ×0,5
    (1.3, True, True, 1.0), (-0.2, True, True, 0.0),     # вне [0; 1] — к границе
])
def test_explication_confidence(similarity, pd_ok, rd_ok, expected):
    confidence, basis = explication_confidence(similarity, pd_ok, rd_ok)
    assert confidence == expected
    assert basis.startswith("доля совпавших помещений") and "ПД — " in basis and "РД — " in basis


@pytest.mark.parametrize("pd_n, rd_n, expected", [(1, 1, 1.0), (2, 1, 0.5), (1, 3, 0.33), (0, 0, 1.0)])
def test_flat_confidence(pd_n, rd_n, expected):
    confidence, basis = flat_confidence(pd_n, rd_n)
    assert confidence == expected and f"ПД — {pd_n}, РД — {rd_n}" in basis


def _process_with_ar_pages(db, pd_text: str, rd_text: str) -> Process:
    obj = ConstructionObject(object_code="OBJ-FREE", name="Экспликации")
    db.add(obj)
    db.flush()
    process = Process(object_pk=obj.id)
    db.add(process)
    db.flush()
    for stage, text in (("PD", pd_text), ("RD", rd_text)):
        f = File(process_id=process.id, file_id=f"F-{stage}", original_name=f"АР_{stage}.pdf", ext=".pdf",
                 sha256=stage * 32, doc_stage=stage, status="ok", discipline="АР", pages=1)
        db.add(f)
        db.flush()
        db.add(Page(file_pk=f.id, page_no=1, text=text, text_source="layer", section="АР"))
    db.flush()
    return process


def test_suspicions_carry_confidence_into_protocol_card(db):
    from inspector.protocol import build_protocol

    process = _process_with_ar_pages(db, PD, RD)
    stats = run_freesearch(db, process, 1)
    assert stats["reliable_pairs"] == 1
    found = {f.param_code: f for f in db.scalars(select(Finding).where(Finding.process_id == process.id))}
    assert set(found) == {"FREE-AR-EXPLICATION-FUNCTION", "FREE-AR-EXPLICATION-AREA", "FREE-AR-EXPLICATION-TOTAL"}
    for f in found.values():
        assert f.status == S.SUSPICION
        assert f.verification["confidence"] == 0.75                          # 3 из 4 помещений совпали, итоги сошлись
        assert "сумма площадей сошлась с итогом: ПД — да, РД — да" in f.verification["basis"]
    cards = {c["code"]: c for c in build_protocol(db, process)["findings"]}
    assert cards["FREE-AR-EXPLICATION-AREA"]["confidence"] == 0.75


def test_unreliable_table_lowers_confidence(db):
    broken_rd = RD.replace("Итого:\n65,70", "Итого:\n99,00")              # сумма площадей не сходится с итогом
    process = _process_with_ar_pages(db, PD, broken_rd)
    run_freesearch(db, process, 1)
    f = db.scalars(select(Finding).where(Finding.process_id == process.id,
                                         Finding.param_code == "FREE-AR-EXPLICATION-AREA")).one()
    assert f.verification["confidence"] == 0.56 and "РД — нет" in f.verification["basis"]
    assert f.submission_include is False                                     # ненадёжная пара — не в сабмит


def test_matrix_findings_have_no_confidence(db, example_process):
    from inspector.protocol import build_protocol

    cards = {c["code"]: c for c in build_protocol(db, example_process)["findings"]}
    assert cards["KR-057"]["confidence"] is None and cards["KR-057"]["verification"]["same_entity"] == "YES"
    assert cards["PZ-009"]["confidence"] is None and cards["PZ-009"]["verification"] is None
