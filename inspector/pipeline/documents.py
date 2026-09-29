"""Этап 5 — сборка документов из страниц и их паспорт.

ИД — поток: документ начинается со страницы с признаком «первая страница» (пустые обороты не рвут документ), тип —
класс первой страницы. ПД/РД — том: документ = файл, единица поиска — лист. Паспорт: шифр, лист, изменение и стадия
из штампа по ГОСТ Р 21.101 (разбор слов слоя по геометрии), номер и дата акта — из
заголовка. Стадия файла без заявленной: по содержимому (классы страниц, стадия в штампе), папка — только подсказка
с пометкой stage_source=folder. Редакции: один шифр в нескольких файлах -> старшее изменение актуально; одинаковые
листы без номера изменения -> CLARIFICATION_REQUIRED для доказательств из них. Старшее изменение вытесняет младшее, только
если это тот же том: первые листы совпадают по словам ≥ 97 % и объём отличается ≤ 20 % (в штампе часто общий шифр проекта
без марки тома — иначе разные тома считались бы редакциями одного; на корпусе 299 -> 10 отсечений,
все верные). Дата тома — самая поздняя дата в колонке «Дата» штампов (File.approval_date)."""
from __future__ import annotations

import re
from collections import Counter, defaultdict

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from inspector.config import Settings
from inspector.models import Document, File, Page, Process
from inspector.pipeline.classify import ID_CLASSES
from inspector.pipeline.pages import load_words

STAMP_KEYS = {"лист", "листов", "стадия", "изм.", "изм", "кол.уч", "кол.уч.", "подпись", "подп.", "дата"}
CODE_RE = re.compile(r"^(?=.*\d{2})(?=.*[А-ЯA-Z])[А-ЯA-Z0-9]{1,8}[-/_.][А-ЯA-Z0-9][\w./()-]{2,}$|"
                     r"^(?=.*[А-ЯA-Z]{2})\d{2,4}[-/]\d{2,6}[-/.][\w./()-]{2,}$", re.I)
CODE_BAD = re.compile(r"\.(pdf|sig|docx?|xlsx?|dwg)$|^[0-9a-f]{8}-[0-9a-f]{4}-|/\d\.[А-Я]|^\d+\.\d+-\d+\.\d+|"
                      r"^[А-Я]-\d{3}-\d{9,}", re.I)
STAGE_RE = re.compile(r"^(П|Р|РД|ПД|Э|ЭП|П1|Р1|И)$")
INT_RE = re.compile(r"^\d{1,3}$")
MAX_REVISION = 20        # «Изм.» больше 20 в корпусе — чужие числа из штампа (50, 200, 975), а не номер изменения
STAMP_DATE_FULL = re.compile(r"^(\d{1,2})[./](\d{1,2})[./](\d{4}|\d{2})$")
STAMP_DATE_MY = re.compile(r"^(\d{1,2})[./](\d{4}|\d{2})$")
STAMP_YEARS = range(2010, 2028)
FOLDER_HINTS = [("ID", re.compile(r"(^|/)(ИД|Исполнительн[а-я]*( документаци[яи])?)(/|$)", re.I)),
                ("RD", re.compile(r"(^|/)(РД|Рабоч[а-я]* документаци[яи]|Стадия Р(Д)?)(/|$)", re.I)),
                ("PD", re.compile(r"(^|/)(ПД|Проектн[а-я]* документаци[яи]|Стадия П)(/|$)", re.I))]
STAGE_LETTER = {"П": "PD", "ПД": "PD", "П1": "PD", "Р": "RD", "РД": "RD", "Р1": "RD"}


def _norm(t: str) -> str:
    return t.strip().lower().replace("ё", "е")


def is_code(t: str) -> bool:
    return bool(CODE_RE.match(t)) and len(t) >= 6 and not CODE_BAD.search(t)


def code_key(code: str | None) -> str | None:
    return re.sub(r"[^A-ZА-Я0-9]", "", code.upper()) if code else None


def parse_stamp(words: list, width: float, height: float) -> dict | None:
    """Штамп листа по словам слоя (доли страницы -> пункты). -> {stage, sheet, code, revision} или None."""
    W, H = width or 1.0, height or 1.0
    ws = [{"x0": w[0] * W, "y0": w[1] * H, "x1": w[2] * W, "y1": w[3] * H, "t": w[4]} for w in words]
    br = [w for w in ws if w["x0"] > 0.45 * W and w["y0"] > 0.55 * H]
    anchors = [w for w in br if _norm(w["t"]) in STAMP_KEYS]
    keys = {_norm(w["t"]) for w in anchors}
    if len(keys & {"лист", "листов", "стадия", "изм.", "изм", "подпись", "подп.", "дата"}) < 3:
        return None
    res = {"stage": None, "sheet": None, "code": None, "revision": None, "dates": stamp_dates(ws, W, H)}
    stadia = [w for w in anchors if _norm(w["t"]) == "стадия"]
    if not stadia:
        return res
    st = stadia[0]
    ch = (st["y1"] - st["y0"]) or 8
    stamp_x0 = min(w["x0"] for w in anchors) - 0.03 * W
    stamp_y0 = min(w["y0"] for w in anchors) - 6 * ch
    sw = [w for w in ws if w["x0"] >= stamp_x0 and w["y0"] >= stamp_y0]
    cx = lambda w: (w["x0"] + w["x1"]) / 2  # noqa: E731
    cy = lambda w: (w["y0"] + w["y1"]) / 2  # noqa: E731

    def below(label, pat):
        cands = [w for w in sw if pat.match(w["t"]) and label["y1"] - 0.3 * ch < w["y0"] < label["y1"] + 3.5 * ch
                 and abs(cx(w) - cx(label)) < (label["x1"] - label["x0"]) * 0.9 + ch]
        cands.sort(key=lambda w: w["y0"])
        return cands[0]["t"] if cands else None
    res["stage"] = below(st, STAGE_RE)
    for w in anchors:
        if _norm(w["t"]) == "лист" and w["x0"] > st["x0"] and abs(cy(w) - cy(st)) < 1.2 * ch:
            res["sheet"] = below(w, INT_RE)
    above = [w for w in sw if w["y1"] < st["y0"] - 0.2 * ch and is_code(w["t"])]
    if above:
        above.sort(key=lambda w: -(w["x1"] - w["x0"]))
        res["code"] = above[0]["t"]
    izm = [w for w in anchors if _norm(w["t"]) in ("изм.", "изм")]
    if izm:
        iz = izm[0]
        col = [w for w in sw if INT_RE.match(w["t"]) and w["y1"] < iz["y0"] and abs(cx(w) - cx(iz)) < ch * 1.5
               and w["y0"] > stamp_y0]
        res["revision"] = max([int(w["t"]) for w in col if int(w["t"]) <= MAX_REVISION], default=0)
    return res


def stamp_date(t: str) -> str | None:
    """«04.25» -> «2025-04»; «17.03.2026» / «17.03.26» -> «2026-03-17»; год 2010–2027, иначе None."""
    t = t.strip(".,;:()[]«»\"' ")
    m = STAMP_DATE_FULL.match(t)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        y = y + 2000 if y < 100 else y
        return f"{y:04d}-{mo:02d}-{d:02d}" if 1 <= d <= 31 and 1 <= mo <= 12 and y in STAMP_YEARS else None
    m = STAMP_DATE_MY.match(t)
    if m:
        mo, y = int(m.group(1)), int(m.group(2))
        y = y + 2000 if y < 100 else y
        return f"{y:04d}-{mo:02d}" if 1 <= mo <= 12 and y in STAMP_YEARS else None
    return None


def stamp_dates(ws: list[dict], W: float, H: float) -> list[str]:
    """Даты в колонке «Дата» основной надписи (строки изменений — выше заголовка колонки, подписи — ниже). У А4 штамп
    во всю ширину — колонка левее середины листа, поэтому метка ищется во всей нижней части листа."""
    out = set()
    for lb in (w for w in ws if w["y0"] > 0.55 * H and _norm(w["t"]) == "дата"):
        ch = (lb["y1"] - lb["y0"]) or 8
        cx, cy = (lb["x0"] + lb["x1"]) / 2, (lb["y0"] + lb["y1"]) / 2
        half = max(lb["x1"] - lb["x0"], 2.5 * ch) * 0.75
        for w in ws:
            if abs((w["x0"] + w["x1"]) / 2 - cx) <= half and abs((w["y0"] + w["y1"]) / 2 - cy) <= 12 * ch:
                d = stamp_date(w["t"])
                if d:
                    out.add(d)
    return sorted(out)


ACT_NO = re.compile(r"№\s*([\w\-/.]{1,30})")
ACT_DATE = re.compile(r"\b(\d{2}\.\d{2}\.(?:\d{4}|\d{2}))\b")


def stage_by_content(pages: list[Page], stamps: dict[int, dict]) -> str | None:
    classes = [p.page_class for p in pages if p.page_class and p.page_class not in ("ПУСТАЯ", "ПРОЧЕЕ")]
    if classes and sum(c in ID_CLASSES for c in classes) / len(classes) >= 0.5:
        return "ID"
    letters = Counter(STAGE_LETTER[s["stage"]] for s in stamps.values() if s and s.get("stage") in STAGE_LETTER)
    if letters:
        return letters.most_common(1)[0][0]
    head = " ".join((p.text or "")[:1500] for p in pages[:3]).lower()
    if "рабочая документация" in head:
        return "RD"
    if "проектная документация" in head:
        return "PD"
    return None


def stage_by_folder(rel_path: str) -> str | None:
    folder = "/" + (rel_path or "").rsplit("/", 1)[0] + "/"
    for stage, rx in FOLDER_HINTS:
        if rx.search(folder):
            return stage
    return None


def majority(values) -> str | None:
    c = Counter(v for v in values if v)
    return c.most_common(1)[0][0] if c else None


APPENDIX_RX = re.compile(r"^\W*приложени", re.I)


def segment_id_file(pages: list[Page]) -> list[list[Page]]:
    """Поток ИД -> документы: новый документ на странице с признаком «первая» (кроме пустых оборотов и приложений:
    заголовок страницы начинается с «Приложение» — часть предыдущего акта (ИД: +16/−0 документов);
    на ПД правило вредно — там «Приложение N» часто отдельный документ)."""
    docs: list[list[Page]] = []
    for p in pages:
        starts = bool(p.is_first_page) and p.page_class not in ("ПУСТАЯ",) and not APPENDIX_RX.match(p.title or "")
        if not docs or starts:
            docs.append([p])
        else:
            docs[-1].append(p)
    return docs


def approval_of(f: File, pages: list[Page], stage: str | None) -> str:
    if f.has_signature:
        return "SIGNED"
    text = " ".join((p.text or "")[-3000:].lower() for p in pages[:40])
    if stage == "RD" and "в производство работ" in text:
        return "IN_PRODUCTION"
    return f.approval_status or "UNKNOWN"


def run_documents(session: Session, process: Process, batch: int, settings: Settings, progress=None) -> dict:
    files = list(session.scalars(select(File).where(File.process_id == process.id, File.batch == batch,
                                                    File.status == "ok").order_by(File.id)))
    # повтор задачи / «Запустить повторно»: документы прошлой попытки этих файлов убрать, иначе остаются документы без
    # страниц, и resolve_revisions сравнивает редакции с ними (Жаккар 0 -> старая редакция снова актуальна)
    pks = [f.id for f in files] or [-1]
    session.execute(update(Page).where(Page.file_pk.in_(pks)).values(document_pk=None))
    session.execute(delete(Document).where(Document.file_pk.in_(pks)))
    stats = {"files": len(files), "documents": 0, "stage_content": 0, "stage_folder": 0, "stage_unknown": 0,
             "superseded": 0, "clarification": 0}
    for n, f in enumerate(files, start=1):
        pages = list(session.scalars(select(Page).where(Page.file_pk == f.id).order_by(Page.page_no)))
        if not pages:
            continue
        words = load_words(settings.data_dir, f.sha256)
        stamps = {p.page_no: parse_stamp(words.get(p.page_no, []), p.width, p.height)
                  for p in pages if words.get(p.page_no)}
        if not f.doc_stage:
            stage = stage_by_content(pages, stamps)
            if stage:
                f.doc_stage, f.stage_source = stage, "content"
                stats["stage_content"] += 1
            else:
                stage = stage_by_folder(f.rel_path or "")
                if stage:
                    f.doc_stage, f.stage_source = stage, "folder"
                    stats["stage_folder"] += 1
                else:
                    stats["stage_unknown"] += 1
                    f.error = "Стадия (ПД/РД/ИД) не определена ни загрузкой, ни по содержимому — файл не сравнивается"
        f.discipline = majority(p.section for p in pages if p.section and p.section != "НЕТ")
        codes = [s["code"] for s in stamps.values() if s and s.get("code")]
        if not codes:   # текстовые тома без штампа в слое: шифр стоит на титуле/обложке — первые 3 страницы
            codes = [t for p in pages[:3] for t in (x.strip(".,;:()«»\"'") for x in (p.text or "").split()) if is_code(t)]
        f.document_code = majority(codes) or f.document_code
        revs = [s["revision"] for s in stamps.values() if s and s.get("revision") is not None]
        f.revision = str(max(revs)) if revs else f.revision
        dates = [d for s in stamps.values() if s for d in s.get("dates") or ()]
        f.approval_date = max(dates) if dates else f.approval_date
        f.approval_status = approval_of(f, pages, f.doc_stage)
        segments = segment_id_file(pages) if f.doc_stage == "ID" else [pages]
        for seg in segments:
            first = seg[0]
            non_empty = [p.page_class for p in seg if p.page_class and p.page_class != "ПУСТАЯ"]
            doc_type = (first.page_class if first.page_class and first.page_class != "ПУСТАЯ" else majority(non_empty)) \
                if f.doc_stage == "ID" else (majority(non_empty) or "ПРОЧЕЕ")
            title = next((p.title for p in seg if p.title), None)
            seg_stamps = [stamps.get(p.page_no) for p in seg if stamps.get(p.page_no)]
            code = majority(s.get("code") for s in seg_stamps) or (f.document_code if f.doc_stage != "ID" else None)
            number = ACT_NO.search(title or "") if f.doc_stage == "ID" else None
            d = Document(process_id=process.id, file_pk=f.id, first_page=first.page_no, last_page=seg[-1].page_no,
                         doc_type=doc_type or "ПРОЧЕЕ", stage=f.doc_stage,
                         section=majority(p.section for p in seg if p.section and p.section != "НЕТ"),
                         title=title, document_code=(number.group(1) if number else code),
                         sheet=next((s.get("sheet") for s in seg_stamps if s.get("sheet")), None),
                         revision=str(max(r for r in (s.get("revision") for s in seg_stamps) if r is not None))
                         if any(s.get("revision") is not None for s in seg_stamps) else f.revision,
                         approval_status=f.approval_status)
            session.add(d)
            session.flush()
            for p in seg:
                p.document_pk = d.id
            stats["documents"] += 1
        if progress:
            progress(n, len(files))
    session.flush()
    sup, clar = resolve_revisions(session, process)
    stats["superseded"], stats["clarification"] = sup, clar
    return stats


DATE = re.compile(r"(?<!\d)\d{1,2}[._]\d{1,2}[._](?:\d{4}|\d{2})(?!\d)")
KEY = re.compile(r"(?<!\d)(\d{2,6}(?:[-._]\d{2,4}){1,3}|\d{6})(?!\d)")
MIN_SHARE, MIN_FAMILY = 0.3, 3
SAME_VOLUME, SAME_SIZE = 0.97, 0.8     # тот же том: слова первых 3 листов (Жаккар) и отношение числа страниц
WORD_RE = re.compile(r"[^\W\d_]{3,}")


def project_key(code: str | None) -> str | None:
    """Числовое ядро шифра проекта из штампа: 'П-2025-04.266-АР' -> '2025-04-266'; 'ЖС-РД-270121-П-АР' -> '270121'.
    Шифр берётся со страницы, а не из имени файла."""
    if not code:
        return None
    m = KEY.search(DATE.sub(" ", code))
    if not m:
        return None
    key = re.sub(r"[._]", "-", m.group(1))
    if re.fullmatch(r"\d{1,2}-\d{1,2}-(?:20)?\d{2}", key):
        return None
    return key


def superseded_editions(files: list[File]) -> tuple[str | None, list[int]]:
    """Старая редакция ПД целым комплектом (на одном из 12 объектов — 24 из 49 томов ПД 2024 года при
    РД по редакции 2025, на остальных — 0). База = доминирующий ключ шифра РД; чужой ключ —
    старая редакция, только если он у ≥ 3 томов ПД и база покрывает ≥ 30 % томов ПД; иначе ничего не исключаем."""
    rd_keys = Counter(k for f in files if f.doc_stage == "RD" for k in [project_key(f.document_code)] if k)
    if not rd_keys:
        return None, []
    base = rd_keys.most_common(1)[0][0]
    keyed = [(f, project_key(f.document_code)) for f in files if f.doc_stage == "PD"]
    keyed = [(f, k) for f, k in keyed if k]
    if not keyed or sum(k == base for _, k in keyed) / len(keyed) < MIN_SHARE:
        return base, []
    families = Counter(k for _, k in keyed)
    return base, [f.id for f, k in keyed if k != base and families[k] >= MIN_FAMILY]


def resolve_revisions(session: Session, process: Process) -> tuple[int, int]:
    """Документы ПД/РД процесса с одним шифром в разных файлах: разные номера изменения -> младшие неактуальны;
    одинаковый номер или его нет, при этом совпали раздел и номера листов -> конфликт, CLARIFICATION_REQUIRED.
    Разные тома одного проекта с общим шифром-префиксом (разные листы) конфликтом не считаются."""
    docs = list(session.scalars(select(Document).where(Document.process_id == process.id,
                                                       Document.stage.in_(("PD", "RD")))))
    groups: dict[tuple, list[Document]] = defaultdict(list)
    for d in docs:
        key = code_key(d.document_code)
        if key and len(key) >= 6:
            groups[(d.stage, key)].append(d)
    superseded = clarification = 0
    files = list(session.scalars(select(File).where(File.process_id == process.id, File.status == "ok")))
    base, old_files = superseded_editions(files)
    for d in docs:
        if d.file_pk in old_files and d.is_actual:
            d.is_actual = False
            d.clarification_reason = (f"Старая редакция ПД: ключ шифра {project_key(d.document_code)} отличается от "
                                      f"ключа рабочей документации {base}")
            superseded += 1
    words: dict[int, set[str]] = {}

    def same_volume(a: Document, b: Document) -> tuple[bool, float]:
        for d in (a, b):
            if d.id not in words:
                texts = session.scalars(select(Page.text).where(Page.document_pk == d.id).order_by(Page.page_no).limit(3))
                words[d.id] = {w for t in texts for w in WORD_RE.findall((t or "").lower().replace("ё", "е"))}
        union = words[a.id] | words[b.id]
        jac = len(words[a.id] & words[b.id]) / len(union) if union else 0.0
        na, nb = a.last_page - a.first_page + 1, b.last_page - b.first_page + 1
        return jac >= SAME_VOLUME and min(na, nb) / max(na, nb) >= SAME_SIZE, jac

    file_dates = {f.id: f.approval_date for f in files}
    for (_stage, _key), items in groups.items():
        if len({d.file_pk for d in items}) < 2:
            continue
        revs = {d.id: int(d.revision) if (d.revision or "").isdigit() else None for d in items}
        known = [v for v in revs.values() if v is not None]
        if known and len(set(known)) > 1:
            top = max(known)
            best = next(d for d in items if revs[d.id] == top)
            for d in items:
                if revs[d.id] is None or revs[d.id] >= top:
                    continue
                same, jac = same_volume(d, best)
                if same:               # разные тома с общим шифром проекта в штампе не трогаем
                    d.is_actual, d.superseded_by_pk = False, best.id
                    d.clarification_reason = (f"Старое изменение тома {d.document_code}: изм. {revs[d.id]}, в пакете есть "
                                              f"изм. {top} (первые листы совпадают на {jac:.0%})")
                    superseded += 1
            continue
        dates = {d.id: file_dates.get(d.file_pk) for d in items}
        dated = [d for d in items if dates[d.id]]
        if len({dates[d.id] for d in dated}) > 1:       # один номер изменения: тот же том с разными датами штампа
            latest = max(dated, key=lambda d: dates[d.id])
            moved = 0
            for d in dated:
                if dates[d.id] >= dates[latest.id]:
                    continue
                same, jac = same_volume(d, latest)
                if same:
                    d.is_actual, d.superseded_by_pk = False, latest.id
                    d.clarification_reason = (f"Ранняя редакция тома {d.document_code}: дата штампа {dates[d.id]}, в "
                                              f"пакете есть {dates[latest.id]} (первые листы совпадают на {jac:.0%})")
                    moved += 1
            superseded += moved
            if moved:
                continue
        pages_by_doc = {d.id: set(session.scalars(select(Page.page_no).where(Page.document_pk == d.id))) for d in items}
        sections = {d.section for d in items}
        same_size = len({len(v) for v in pages_by_doc.values()}) == 1
        if len(sections) == 1 and same_size:
            for d in items:
                d.clarification = True
                d.clarification_reason = (f"Шифр {d.document_code} встречается в {len({x.file_pk for x in items})} файлах "
                                          "без различимого номера изменения — выберите актуальную редакцию")
                clarification += 1
    session.flush()
    return superseded, clarification
