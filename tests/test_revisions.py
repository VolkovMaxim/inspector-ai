"""Редакции ПД/РД (pipeline/documents.py): дата и «Изм.» из штампа; старшее изменение вытесняет младшее только у того
же тома (первые листы совпадают, объём близок) — разные тома с общим шифром проекта в штампе не трогаются."""
from __future__ import annotations

import pytest

from inspector.pipeline.documents import parse_stamp, resolve_revisions, stamp_date

GENERAL = ("Общие данные. Ведомость рабочих чертежей основного комплекта. Ведомость ссылочных и прилагаемых "
           "документов. Общие указания. Проект разработан в соответствии с заданием на проектирование ")


def w(x: float, y: float, t: str, width: float = 0.04) -> tuple:
    return (x, y, x + width, y + 0.012, t)


def test_stamp_date_formats():
    assert stamp_date("04.25") == "2025-04"
    assert stamp_date("17.03.2026") == "2026-03-17"
    assert stamp_date("17.03.26,") == "2026-03-17"
    assert stamp_date("13.25") is None          # месяц 13
    assert stamp_date("12.50") is None          # 2050 — не дата штампа
    assert stamp_date("А500С") is None


def test_parse_stamp_reads_dates_and_ignores_stray_revision_numbers():
    """Лист А3 (альбомный): основная надпись целиком правее середины листа. (У А4 она во всю ширину — «Изм.» и
    «Дата» левее середины, и номер изменения разбор штампа сейчас не читает; даты — читает.)"""
    words = [w(0.535, 0.86, "1", 0.01), w(0.53, 0.845, "200", 0.02),          # колонка «Изм.»: 1 и чужое число
             w(0.52, 0.88, "Изм."), w(0.56, 0.88, "Кол.уч"), w(0.60, 0.88, "Лист"), w(0.63, 0.88, "№док."),
             w(0.66, 0.88, "Подп."), w(0.69, 0.88, "Дата"),
             w(0.69, 0.90, "04.25"), w(0.685, 0.92, "17.03.2026", 0.05),      # даты подписей под «Дата»
             w(0.76, 0.865, "П-2025-04.266-АР", 0.12),
             w(0.86, 0.895, "Стадия"), w(0.91, 0.895, "Лист"), w(0.95, 0.895, "Листов"),
             w(0.875, 0.912, "П", 0.01), w(0.925, 0.912, "3", 0.01)]
    st = parse_stamp(words, 1190, 842)
    assert st["stage"] == "П" and st["code"] == "П-2025-04.266-АР"
    assert st["revision"] == 1
    assert st["dates"] == ["2025-04", "2026-03-17"]


def test_stamp_dates_on_a4_left_column():
    """А4: колонка «Дата» левее середины листа — даты всё равно читаются."""
    words = [w(0.10, 0.88, "Изм."), w(0.30, 0.88, "Подп."), w(0.37, 0.88, "Дата"), w(0.37, 0.90, "04.25"),
             w(0.75, 0.895, "Стадия"), w(0.83, 0.895, "Лист"), w(0.90, 0.895, "Листов"), w(0.77, 0.912, "П", 0.01)]
    st = parse_stamp(words, 595, 842)
    assert st["stage"] == "П" and st["dates"] == ["2025-04"]


@pytest.fixture()
def process(db):
    from inspector.models import ConstructionObject, Process

    obj = ConstructionObject(object_code="OBJ-REV", name="Редакции")
    db.add(obj)
    db.flush()
    p = Process(object_pk=obj.id)
    db.add(p)
    db.flush()
    return p


def add_volume(db, process, name: str, code: str, revision: str | None, first_pages: list[str], n_pages: int,
               date: str | None = None):
    from inspector.models import Document, File, Page

    f = File(process_id=process.id, file_id=name, original_name=name, doc_stage="RD", status="ok",
             document_code=code, revision=revision, approval_date=date, pages=n_pages)
    db.add(f)
    db.flush()
    d = Document(process_id=process.id, file_pk=f.id, first_page=1, last_page=n_pages, stage="RD", section="КЖ",
                 document_code=code, revision=revision)
    db.add(d)
    db.flush()
    for i in range(1, n_pages + 1):
        text = first_pages[i - 1] if i <= len(first_pages) else f"Лист {i} армирование плиты"
        db.add(Page(file_pk=f.id, page_no=i, text=text, document_pk=d.id))
    db.flush()
    return d


def test_newer_change_supersedes_only_the_same_volume(db, process):
    ckd = [GENERAL + "Система контроля доступа СКД", "Схема структурная СКД", "План расположения оборудования СКД"]
    old = add_volume(db, process, "СКД изм.5.pdf", "130-1222-ОК-1/Н-СКД", "5", ckd, 20)
    new = add_volume(db, process, "СКД изм.6.pdf", "130-1222-ОК-1/Н-СКД", "6", ckd, 22)
    # разные тома КЖ с одним шифром проекта в штампе: одинаковые общие данные, но объём разный — не редакции
    kzh = [GENERAL + "Конструкции железобетонные", "Общие указания КЖ", "Ведомость расхода стали"]
    k41 = add_volume(db, process, "КЖ4.1.pdf", "01-07/23-14-РД-К1", "3", kzh, 21)
    k51 = add_volume(db, process, "КЖ5.1.pdf", "01-07/23-14-РД-К1", "0", kzh, 16)
    # разные тома с общим шифром и разным текстом
    ov1 = add_volume(db, process, "ОВ1.pdf", "130-1222-ОК-1/Н", "8", ["Отопление и вентиляция ОВ1 план 1 этажа"], 10)
    kzh3 = add_volume(db, process, "КЖ3.pdf", "130-1222-ОК-1/Н", "2", ["Монолитные конструкции КЖ3 плита"], 10)
    superseded, _ = resolve_revisions(db, process)
    assert superseded == 1
    assert old.is_actual is False and old.superseded_by_pk == new.id and "изм. 6" in old.clarification_reason
    assert new.is_actual and k41.is_actual and k51.is_actual and ov1.is_actual and kzh3.is_actual


def test_same_revision_later_stamp_date_wins_for_same_volume(db, process):
    pages = [GENERAL + "Отопление ОВ2 технические решения", "План ОВ2", "Схема ОВ2"]
    early = add_volume(db, process, "ОВ2_ТР3-.pdf", "04-П.СКБ-ПИР-Р-ОВ2", None, pages, 12, "2024-12-16")
    late = add_volume(db, process, "ОВ2_ТР3.pdf", "04-П.СКБ-ПИР-Р-ОВ2", None, pages, 12, "2025-02-27")
    superseded, _ = resolve_revisions(db, process)
    assert superseded == 1 and early.is_actual is False and early.superseded_by_pk == late.id and late.is_actual


@pytest.mark.parametrize("old_spec,new_spec", [(("5", None), ("6", None)),                    # по номеру «Изм.»
                                               ((None, "2024-12-16"), (None, "2025-02-27"))])   # по дате штампа
def test_rerun_of_documents_stage_keeps_older_revision_superseded(db, process, settings, old_spec, new_spec):
    """Повтор задачи (worker, до 3 попыток) или «Запустить повторно» снова выполняет этап documents той же партии.
    Документы прошлой попытки не должны оставаться без страниц: иначе один из них становится эталоном старшей
    редакции, первые листы «не совпадают» (Жаккар 0) и старая редакция тома снова актуальна."""
    from sqlalchemy import select

    from inspector.models import Document, File, Page
    from inspector.pipeline.documents import run_documents

    ckd = [GENERAL + "Система контроля доступа СКД", "Схема структурная СКД", "План расположения оборудования СКД"]

    def volume(name: str, revision: str | None, n_pages: int, date: str | None) -> File:
        f = File(process_id=process.id, file_id=name, original_name=name, doc_stage="RD", status="ok", batch=1,
                 sha256=name.encode().hex()[:64].ljust(64, "0"), document_code="130-1222-ОК-1/Н-СКД",
                 revision=revision, approval_date=date, pages=n_pages)
        db.add(f)
        db.flush()
        for i in range(1, n_pages + 1):
            db.add(Page(file_pk=f.id, page_no=i, text=ckd[i - 1] if i <= 3 else f"Лист {i} СКД", page_class="ТЕКСТ",
                        text_source="layer", has_layer=True))
        db.flush()
        return f

    def actual(f: File) -> bool:
        page = db.scalars(select(Page).where(Page.file_pk == f.id, Page.page_no == 1)).one()
        return db.get(Document, page.document_pk).is_actual

    old = volume("СКД старая.pdf", old_spec[0], 20, old_spec[1])
    new = volume("СКД новая.pdf", new_spec[0], 22, new_spec[1])
    for attempt in (1, 2):                                      # попытка и её повтор
        run_documents(db, process, 1, settings)
        db.commit()
        assert (actual(old), actual(new)) == (False, True), f"попытка {attempt}"
    assert len(db.scalars(select(Document).where(Document.process_id == process.id)).all()) == 2
