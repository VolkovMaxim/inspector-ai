"""Этап 4 — класс страницы (вариант rules_json: 333/409 = 81,4 % против 74,1 % у базы).
Картинка страницы 1300 px + вырезка штампа для листов крупнее А3, текст (слой или OCR) до 3000 символов, JSON-схема:
заголовок -> класс из закрытого словаря, дополнительные виды на листе, «первая страница документа», раздел, качество."""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from inspector.config import Settings
from inspector.llm import ChatClient, LLMError, image_part
from inspector.models import File, Page, PageLabel, Process
from inspector.pipeline.textutils import pool_context

PROMPT_VERSION = "pc1r"
MAX_SIDE, STAMP_SIDE, BIG_SHEET_PT, TEXT_CHARS = 1300, 1100, 1300, 3000

CLASSES = {
    "ТИТУЛ": "титульный лист, обложка тома или комплекта",
    "СОДЕРЖАНИЕ": "содержание тома, состав проектной документации, ведомость документов без общих указаний",
    "ТЕКСТ": "текстовая часть: пояснительная записка, описания, требования, ППР, технологические карты, регламенты",
    "ТЭП": "страница с таблицей технико-экономических показателей",
    "ОБЩИЕ_ДАННЫЕ": "лист «Общие данные»: ведомость рабочих чертежей, ссылочные документы, общие указания",
    "ТАБЛИЦА": "лист, занятый таблицей: спецификация, ведомость, экспликация, кабельный журнал, таблица нагрузок",
    "ПЛАН": "план этажа, кровли, генплан, стройгенплан, план благоустройства, план сетей, кладочный или разбивочный план",
    "РАЗРЕЗ_ФАСАД": "разрез, фасад, сечение здания",
    "УЗЕЛ": "узел, деталь, детальный чертёж отдельного элемента",
    "СХЕМА": "принципиальная, структурная, однолинейная схема, аксонометрия, схема расположения",
    "ЧЕРТЁЖ_КОНСТРУКЦИЙ": "опалубочный чертёж, схема армирования, чертёж шахты, закладных",
    "РАСЧЁТ": "расчёт с формулами и результатами",
    "ГРАФИК": "календарный план, график работ",
    "СМЕТА": "сметный расчёт, смета",
    "АОСР": "акт освидетельствования скрытых работ",
    "АКТ_ПРОЧИЙ": "другой акт: ответственных конструкций, ПНР, испытаний, приёмки, допуска, монтажа, входного контроля",
    "ИСПОЛНИТЕЛЬНАЯ_СХЕМА": "исполнительная схема, исполнительная геодезическая съёмка, исполнительный чертёж",
    "ДОКУМЕНТ_О_КАЧЕСТВЕ": "паспорт качества, документ о качестве партии материала, сертификат качества на металл",
    "СЕРТИФИКАТ_ДЕКЛАРАЦИЯ": "сертификат соответствия, пожарный сертификат, декларация о соответствии и приложения к ним",
    "ПАСПОРТ_ИЗДЕЛИЯ": "технический паспорт изделия или оборудования, руководство по эксплуатации",
    "ПРОТОКОЛ": "протокол испытаний, измерений, лабораторное заключение",
    "ЖУРНАЛ": "общий или специальный журнал работ",
    "ТЕХПЛАН_БТИ": "технический план, документы БТИ",
    "РАЗРЕШИТЕЛЬНЫЙ": "ЗОС, разрешение на строительство или ввод, заключение экспертизы или надзорного органа, справка",
    "ЭНЕРГОПАСПОРТ": "энергетический паспорт",
    "ДОГОВОР": "договор, дополнительное соглашение",
    "УЧЁТНЫЙ": "накладная, КС-2, КС-3, талон, справка об утилизации",
    "ФОТО": "фотография объекта, фотофиксация",
    "РЕЕСТР": "реестр или опись документов",
    "ПИСЬМО_ПРИКАЗ": "письмо, приказ, распоряжение, доверенность, выписка СРО, лицензия, свидетельство, аттестат",
    "ПРОЧЕЕ": "не подходит ни одно описание",
    "ПУСТАЯ": "пустая или почти пустая страница, оборот листа, только штамп или печать",
}
SECTIONS = ["ПЗ", "ГП", "АР", "КР", "ЭОМ", "ВК", "ОВ", "СС", "ГС", "ТХ", "ПОС", "ПОД", "ООС", "ПБ", "ОДИ", "ЭЭ", "СМ",
            "ИЗЫСКАНИЯ", "НЕТ"]
ID_CLASSES = {"АОСР", "АКТ_ПРОЧИЙ", "ИСПОЛНИТЕЛЬНАЯ_СХЕМА", "ДОКУМЕНТ_О_КАЧЕСТВЕ", "СЕРТИФИКАТ_ДЕКЛАРАЦИЯ",
              "ПАСПОРТ_ИЗДЕЛИЯ", "ПРОТОКОЛ", "ЖУРНАЛ", "ТЕХПЛАН_БТИ", "РАЗРЕШИТЕЛЬНЫЙ", "ЭНЕРГОПАСПОРТ", "ДОГОВОР",
              "УЧЁТНЫЙ", "ФОТО"}
SHEET_CLASSES = {"ТИТУЛ", "СОДЕРЖАНИЕ", "ТЕКСТ", "ТЭП", "ОБЩИЕ_ДАННЫЕ", "ТАБЛИЦА", "ПЛАН", "РАЗРЕЗ_ФАСАД", "УЗЕЛ",
                 "СХЕМА", "ЧЕРТЁЖ_КОНСТРУКЦИЙ", "РАСЧЁТ", "ГРАФИК", "СМЕТА"}
SCHEMA = {"type": "object", "additionalProperties": False,
          "required": ["title", "main_class", "other_classes", "is_first_page", "section", "quality"],
          "properties": {
              "title": {"type": "string", "maxLength": 140},
              "main_class": {"enum": list(CLASSES)},
              "other_classes": {"type": "array", "maxItems": 2, "items": {"enum": list(CLASSES)}},
              "is_first_page": {"type": "boolean"},
              "section": {"enum": SECTIONS},
              "quality": {"enum": ["хорошее", "плохой скан", "рукописный"]}}}
SYSTEM = (
    "Ты разбираешь поток строительной документации (проектная, рабочая, исполнительная). Дана одна страница: картинка и, "
    "если есть, её текстовый слой. Для больших листов вторая картинка — увеличенный правый нижний угол со штампом. "
    "Определи:\n"
    "title — заголовок документа или название листа из штампа, дословно; пустая строка, если заголовка на странице нет.\n"
    "main_class — что это за страница, по словарю ниже. Страница-продолжение многостраничного документа получает класс "
    "этого документа (вторая страница акта — тот же акт; приложение к сертификату — СЕРТИФИКАТ_ДЕКЛАРАЦИЯ).\n"
    "other_classes — что ещё есть на этом же листе (например, спецификация на листе с планом); иначе пустой список.\n"
    "is_first_page — true, если с этой страницы начинается документ (акт, сертификат, паспорт, протокол, журнал, том) "
    "или это отдельный лист чертежа; false для страниц-продолжений.\n"
    "section — раздел/марка для листов проектной и рабочей документации (АР, КР, ОВ и т. д.); для остального НЕТ.\n"
    "quality — читаемость страницы.\nСловарь классов:\n" + "\n".join(f"- {k}: {v}" for k, v in CLASSES.items())
)
RULES = (  # «частые трудные случаи» из инструкции стажёров — дали +30 стр. из 409 (p < 0,001)
    "\nТрудные случаи:\n"
    "- ПЛАН или СХЕМА: если видны стены и помещения — ПЛАН, даже если на нём нарисованы трубы или кабели.\n"
    "- УЗЕЛ или ЧЕРТЁЖ_КОНСТРУКЦИЙ: арматура, сетки, опалубка — ЧЕРТЁЖ_КОНСТРУКЦИЙ; остальные детали — УЗЕЛ.\n"
    "- ДОКУМЕНТ_О_КАЧЕСТВЕ или СЕРТИФИКАТ_ДЕКЛАРАЦИЯ: про конкретную партию с номером и датой отгрузки — "
    "ДОКУМЕНТ_О_КАЧЕСТВЕ; государственный бланк на вид продукции со сроком действия — СЕРТИФИКАТ_ДЕКЛАРАЦИЯ.\n"
    "- Вторая страница акта без заголовка — всё равно АОСР или АКТ_ПРОЧИЙ.\n"
    "- ГРАФИК — только календарный план работ (полоски по месяцам); кривые и диаграммы из расчётов — РАСЧЁТ.\n"
    "- Листы подбора оборудования (характеристики вентилятора, насоса, кривая «расход — давление», логотип "
    "производителя) — ПАСПОРТ_ИЗДЕЛИЯ, даже если вшиты в том проекта.\n"
    "- Лист «Разрешение на внесение изменений» (таблица: Изм., Лист, Содержание изменения) — ПРОЧЕЕ.\n"
    "- Коммерческое предложение — ПИСЬМО_ПРИКАЗ; снимок экрана интернет-магазина — ПРОЧЕЕ; ни то ни другое не СМЕТА."
)


def render_page(pdf_path: str, page_no: int) -> list[bytes]:
    """Картинка страницы (1300 px по длинной стороне) + вырезка штампа для листов крупнее А3."""
    import pymupdf

    page = pymupdf.open(pdf_path)[page_no - 1]
    w, h = page.rect.width, page.rect.height
    zoom = MAX_SIDE / max(w, h)
    images = [page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), colorspace=pymupdf.csRGB).tobytes("jpeg", jpg_quality=80)]
    if max(w, h) > BIG_SHEET_PT:
        clip = pymupdf.Rect(w * 0.55, h * 0.72, w, h) + tuple(page.rect[:2]) * 2
        z = STAMP_SIDE / clip.width
        images.append(page.get_pixmap(matrix=pymupdf.Matrix(z, z), clip=clip, colorspace=pymupdf.csRGB)
                      .tobytes("jpeg", jpg_quality=85))
    return images


def build_messages(images: list[bytes], text: str) -> list[dict]:
    layer = (text or "").strip()[:TEXT_CHARS]
    content = [image_part(i) for i in images]
    content.append({"type": "text", "text": "ТЕКСТОВЫЙ СЛОЙ СТРАНИЦЫ:\n" + layer if layer else "Текстового слоя нет."})
    return [{"role": "system", "content": SYSTEM + RULES}, {"role": "user", "content": content}]


def cache_path(data_dir: Path, sha: str, model: str) -> Path:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", model)[:60]
    return data_dir / "cache" / "classify" / f"{sha}.{slug}.{PROMPT_VERSION}.jsonl"


def qwen_label(page: Page, model: str) -> PageLabel:
    return PageLabel(page_pk=page.id, source="qwen", model_version=f"{model}:{PROMPT_VERSION}",
                     page_class=page.page_class, is_first_page=page.is_first_page, section=page.section,
                     quality_label=page.quality_label)


def apply_label(page: Page, label: dict) -> None:
    page.page_class = label.get("main_class")
    page.other_classes = [c for c in (label.get("other_classes") or []) if c != page.page_class]
    page.is_first_page = bool(label.get("is_first_page"))
    page.section = label.get("section")
    page.title = (label.get("title") or "")[:300] or None
    page.quality_label = label.get("quality")
    page.low_quality = label.get("quality") in ("плохой скан", "рукописный")


async def classify_all(client: ChatClient, todo: list[tuple[int, str, int, str]], on_page, parallel: int = 24) -> None:
    loop = asyncio.get_running_loop()
    sem = asyncio.Semaphore(parallel)
    workers = max(1, min(8, (os.cpu_count() or 2) - 1))
    with ProcessPoolExecutor(max_workers=workers, mp_context=pool_context()) as pool:
        async def one(file_pk: int, pdf: str, page_no: int, text: str) -> None:
            async with sem:
                try:
                    images = await loop.run_in_executor(pool, render_page, pdf, page_no)
                    res = await client.chat(build_messages(images, text), max_tokens=400,
                                            json_schema={"name": "page", "schema": SCHEMA})
                    on_page(file_pk, page_no, res.json(), None)
                except (LLMError, ValueError) as exc:
                    on_page(file_pk, page_no, None, f"класс страницы: {exc}"[:300])
                except Exception as exc:  # noqa: BLE001 — рендер битой страницы
                    on_page(file_pk, page_no, None, f"класс страницы: {type(exc).__name__}: {exc}"[:300])
        async with client:
            await asyncio.gather(*[one(*t) for t in todo])


def run_classify(session: Session, process: Process, batch: int, settings: Settings, client: ChatClient,
                 progress=None) -> dict:
    files = {f.id: f for f in session.scalars(select(File).where(File.process_id == process.id, File.batch == batch,
                                                                 File.status == "ok"))}
    pages = list(session.scalars(select(Page).where(Page.file_pk.in_(list(files) or [-1]), Page.page_class.is_(None))
                                 .order_by(Page.file_pk, Page.page_no)))
    stats = {"pages": len(pages), "from_cache": 0, "classified": 0, "errors": 0, "seconds": 0.0}
    if not pages:
        return stats
    started = time.time()
    model = settings.llm_model
    paths = {pk: cache_path(settings.data_dir, f.sha256, model) for pk, f in files.items()}
    cached: dict[int, dict[int, dict]] = {}
    for pk, path in paths.items():
        cached[pk] = {}
        if path.exists():
            for line in open(path, encoding="utf-8"):
                try:
                    r = json.loads(line)
                    cached[pk][r["page"]] = r["label"]
                except (json.JSONDecodeError, KeyError):
                    continue
        path.parent.mkdir(parents=True, exist_ok=True)
    by_key = {(p.file_pk, p.page_no): p for p in pages}
    from inspector.ml.page_classifier import classify_with_model

    by_model, model_stats = classify_with_model(session, settings, pages, files, progress)
    stats.update(model_stats)
    todo = []
    for p in pages:
        if (p.file_pk, p.page_no) in by_model:
            continue
        label = cached[p.file_pk].get(p.page_no)
        if label:
            apply_label(p, label)
            session.add(qwen_label(p, model))
            stats["from_cache"] += 1
        else:
            todo.append((p.file_pk, files[p.file_pk].pdf_path, p.page_no, p.text or ""))
    done = 0

    def on_page(file_pk: int, page_no: int, label: dict | None, error: str | None) -> None:
        nonlocal done
        page = by_key[(file_pk, page_no)]
        if label:
            apply_label(page, label)
            session.add(qwen_label(page, model))
            with open(paths[file_pk], "a", encoding="utf-8") as f:
                f.write(json.dumps({"page": page_no, "label": label}, ensure_ascii=False) + "\n")
            stats["classified"] += 1
        else:
            page.page_class, page.error = "ПРОЧЕЕ", error
            stats["errors"] += 1
        done += 1
        if progress and (done % 10 == 0 or done == len(todo)):
            progress(stats["from_cache"] + stats["by_model"] + done, len(pages))

    if todo:
        asyncio.run(classify_all(client, todo, on_page, parallel=max(4, settings.classify_concurrency * 2)))
    session.flush()
    stats["seconds"] = round(time.time() - started, 1)
    return stats
