"""Выгрузки протокола сверки (Приложение 2 ТЗ): PDF, DOCX, XML. Вход — словарь inspector.protocol.build_protocol
(или снимок версии), выход — байты файла; функции чистые, без сети и без обращения к БД."""
from inspector.render.docx import render_docx
from inspector.render.pdf import render_pdf
from inspector.render.xml import render_xml

__all__ = ["render_docx", "render_pdf", "render_xml"]
