"""Поток ИД -> документы (pipeline/documents.py): новый документ на «первой странице», кроме пустых оборотов и
страниц-приложений («Приложение N к акту» — часть акта)."""
from __future__ import annotations

from inspector.models import Page
from inspector.pipeline.documents import segment_id_file


def page(no: int, first: bool, cls: str = "АОСР", title: str | None = None) -> Page:
    return Page(file_pk=1, page_no=no, is_first_page=first, page_class=cls, title=title)


def spans(docs: list[list[Page]]) -> list[tuple[int, int]]:
    return [(d[0].page_no, d[-1].page_no) for d in docs]


def test_new_document_on_first_page():
    pages = [page(1, True), page(2, False), page(3, True, "ИСПОЛНИТЕЛЬНАЯ_СХЕМА"), page(4, False)]
    assert spans(segment_id_file(pages)) == [(1, 2), (3, 4)]


def test_appendix_page_marked_first_stays_in_the_act():
    pages = [page(1, True, title="Акт освидетельствования скрытых работ № 12"), page(2, False),
             page(3, True, "ИСПОЛНИТЕЛЬНАЯ_СХЕМА", title="Приложение 1 к акту № 12"),
             page(4, True, "ДОКУМЕНТ_О_КАЧЕСТВЕ", title="  ПРИЛОЖЕНИЕ №2. Паспорт качества"),
             page(5, True, title="Акт освидетельствования скрытых работ № 13")]
    assert spans(segment_id_file(pages)) == [(1, 4), (5, 5)]


def test_appendix_as_first_page_of_file_still_opens_a_document():
    pages = [page(1, True, title="Приложение 3"), page(2, False)]
    assert spans(segment_id_file(pages)) == [(1, 2)]


def test_blank_page_and_word_inside_title_do_not_split():
    pages = [page(1, True), page(2, True, "ПУСТАЯ"),
             page(3, True, "ДОКУМЕНТ_О_КАЧЕСТВЕ", title="Паспорт. См. приложение 1")]
    assert spans(segment_id_file(pages)) == [(1, 2), (3, 3)]
