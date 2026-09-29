"""Общая часть выгрузок протокола PDF и DOCX: русские подписи статусов, отбор находок по разделам Приложения 2 ТЗ
и сам текст протокола — последовательность блоков (заголовок, абзац, таблица).

Всё — чистые функции над словарём протокола (форма — inspector.protocol.build_protocol, раздел 4 спеки). PDF и DOCX
только рисуют блоки, поэтому содержание двух выгрузок совпадает. Протокол может быть неполным (старый снимок, пустой
процесс): отсутствующее поле показывается прочерком и не роняет выгрузку."""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from inspector import statuses as S

DASH = "—"
_INVALID_XML = re.compile("[^\u0009\u000a\u000d\u0020-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]")
MSK = dt.timezone(dt.timedelta(hours=3), "МСК")
MONTHS = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября",
          "ноября", "декабря")

# ---------------------------------------------------------------- подписи

MARK_TEXT = ("Машинное расхождение является кандидатом; отсутствие документа и гипотеза свободного поиска "
             "не считаются нарушениями; подтверждение выполняет инспектор.")
SUSPICION_AI_COMMENT = ("SUSPICION: не входит в число нарушений и не используется для обучения. Для перевода в "
                        "CANDIDATE требуются точные источники и координаты доказательств.")
NOT_FORMED_TEXT = ("Не формируется. В финализированный протокол включаются только действия, одобренные инспектором "
                   "по CONFIRMED_VIOLATION.")
CARD_INTRO = ("Карточка формируется для каждого finding_id и позволяет воспроизвести вывод без поиска по всему "
              "комплекту. Запись без обязательных источников и координат не может иметь статус CANDIDATE или "
              "CONFIRMED_VIOLATION.")
COUNTING_RULES = (
    ("Подтверждённые нарушения", "Только CONFIRMED_VIOLATION после решения инспектора"),
    ("Предварительные кандидаты", "Показываются отдельно; не суммируются с подтверждёнными"),
    ("Отсутствующие/непригодные документы", "Показатель комплектности; не нарушение"),
    ("Проверенные отрицательные", "Отдельный показатель качества и источник отрицательных GOLD-примеров"),
    ("Гипотезы свободного поиска",
     "SUSPICION; не входят в итог до перевода в CANDIDATE и последующего подтверждения"),
)

PROCESS_STATUS_LABELS = {
    S.PENDING: "ОЖИДАЕТ ОБРАБОТКИ",
    S.PARSING: "ИДЁТ ОБРАБОТКА ДОКУМЕНТОВ",
    S.READY: "ОЖИДАЕТ ВЕРИФИКАЦИИ",
    S.VERIFYING: "ИДЁТ ВЕРИФИКАЦИЯ",
    S.COMPLETED: "ВЕРИФИКАЦИЯ ЗАВЕРШЕНА, ОЖИДАЕТ ФИНАЛИЗАЦИИ",
    S.FINALIZED: "ФИНАЛИЗИРОВАН",
    S.FAILED: "ОШИБКА ОБРАБОТКИ",
}
STATUS_LABELS = {
    S.NEGATIVE_VERIFIED: "проверено, расхождение не выявлено",
    S.CANDIDATE: "кандидат — ожидает решения инспектора",
    S.CONFIRMED_VIOLATION: "нарушение подтверждено инспектором",
    S.MISSING_EVIDENCE: "нет доказательств: документ или значение не найдены",
    S.NOT_APPLICABLE: "неприменимо к объекту",
    S.NOT_COMPARABLE: "сопоставление невозможно",
    S.CLARIFICATION_REQUIRED: "требует уточнения",
    S.SUSPICION: "гипотеза свободного поиска",
}
# решение инспектора: значок, подпись, цвет подписи
DECISION_LABELS = {
    S.CONFIRMED_VIOLATION: ("✔", "Подтверждено", "9C0006"),
    S.NEGATIVE_VERIFIED: ("✖", "Отклонено", "3A3F47"),
    S.CLARIFICATION_REQUIRED: ("⚠", "Требует уточнения", "8A5A00"),
}
DECISION_FILLS = {S.CONFIRMED_VIOLATION: "FBE4E4", S.NEGATIVE_VERIFIED: "EFEFEF", S.CLARIFICATION_REQUIRED: "FFF4CE"}
STAGE_LABELS = {"PD": "ПД", "RD": "РД", "ID": "ИД"}
STAGE_SOURCE_LABELS = {"declared": "при загрузке", "manifest": "манифест", "content": "по содержимому",
                       "folder": "по папке (подсказка)"}
# статус загрузки стадии (суффикс кода из statuses.upload_status): значок-текст и заливка ячейки
UPLOAD_STATES = {"UPLOADED": ("✔ Полностью", "E2F0D9"), "PARTIAL": ("◐ Частично", "FFF2CC"),
                 "MISSING": ("✖ Отсутствует", "F8D7DA")}
SCENARIO_LABELS = {
    "FULL": "полный комплект: ПД, РД и ИД",
    "PD_RD_ONLY": "ПД и РД (ИД не загружена)",
    "PD_ID_ONLY": "ПД и ИД (РД не загружена)",
    "RD_ID_ONLY": "РД и ИД (ПД не загружена)",
    "SINGLE_ONLY": "загружена только одна стадия",
    "PARTIALLY_LOADED": "частичная загрузка",
}
FILE_STATUS_LABELS = {"ok": "читается", "pending": "ожидает обработки", "broken": "повреждён",
                      "unsupported": "формат не поддерживается", "too_large": "превышен размер",
                      "duplicate": "дубликат", "junk": "служебный файл", "archive": "архив (распакован)",
                      "converted_source": "исходник конвертирован"}
DISCOVERY_LABELS = {"SEMANTIC_DISSONANCE": "Семантический диссонанс", "LOGICAL_ANALYSIS": "Логический анализ",
                    "NORMATIVE_ANALYSIS": "Нормативный анализ", "VISUAL_DIFF": "Парное сравнение листов"}
BBOX_SOURCE_LABELS = {"layer": "текстовый слой PDF", "rapidocr": "OCR-рамки RapidOCR", "page": "вся страница"}
PRIORITY_RANK = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
TRACE_FIELDS = (("process_id", "идентификатор процесса проверки"),
                ("matrix_version", "версия Матрицы параметров"),
                ("dataset_version", "версия GOLD-набора"),
                ("model_version", "версии моделей и промптов"),
                ("run_timestamp", "время прогона, ISO 8601"),
                ("input_manifest_sha256", "SHA-256 реестра входных файлов"))
UNMATCHED = (S.MISSING_EVIDENCE, S.NOT_APPLICABLE, S.NOT_COMPARABLE, S.CLARIFICATION_REQUIRED)


# ---------------------------------------------------------------- текст и числа

def clean(value) -> str:
    """Строка для выгрузки: None → "", прочее → str; символы, недопустимые в XML 1.0 (управляющие), удаляются."""
    if value is None:
        return ""
    return _INVALID_XML.sub("", value if isinstance(value, str) else str(value))


def show(value) -> str:
    """Значение для таблицы: пустое → «—»."""
    return clean(value).strip() or DASH


def as_dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def as_list(value) -> list:
    return value if isinstance(value, list) else []


def as_int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def pct(count, total) -> str:
    """Процент с одним знаком и запятой (84,8%), целые — без дроби (100%, 0%); при total = 0 — прочерк."""
    total = as_int(total)
    if total <= 0:
        return DASH
    q = (Decimal(as_int(count)) * 100 / Decimal(total)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    text = format(q, "f")
    return (text[:-2] if text.endswith(".0") else text).replace(".", ",") + "%"


def parse_ts(value) -> dt.datetime | None:
    """ISO 8601 → время МСК (без зоны считаем UTC, как inspector.protocol._iso)."""
    if not value:
        return None
    if isinstance(value, dt.datetime):
        ts = value
    else:
        try:
            ts = dt.datetime.fromisoformat(clean(value).strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=dt.timezone.utc)
    return ts.astimezone(MSK)


def date_long(value) -> str:
    """«25 сентября 2026 г., 00:36 МСК»; нераспознанное значение — как есть."""
    ts = parse_ts(value)
    if ts is None:
        return show(value)
    return f"{ts.day} {MONTHS[ts.month - 1]} {ts.year} г., {ts:%H:%M} МСК"


def date_short(value) -> str:
    ts = parse_ts(value)
    return f"{ts:%d.%m.%Y %H:%M} МСК" if ts else show(value)


def natural(text: str) -> list:
    """Ключ «естественной» сортировки: «2» < «10» (номера помещений, листов)."""
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", text or "")]


def fmt_bbox(bbox) -> str | None:
    """Рамка в долях страницы [0;1] с 3 знаками: [x0, y0, x1, y1] или многоугольник [(x, y), …]."""
    if not isinstance(bbox, (list, tuple)) or not bbox:
        return None
    try:
        if all(isinstance(pt, (list, tuple)) for pt in bbox):
            return "[" + ", ".join("(" + ", ".join(f"{float(v):.3f}" for v in pt) + ")" for pt in bbox) + "]"
        return "[" + ", ".join(f"{float(v):.3f}" for v in bbox) + "]"
    except (TypeError, ValueError):
        return None


def process_status_label(status: str | None) -> str:
    """READY → «ОЖИДАЕТ ВЕРИФИКАЦИИ (дозагрузка возможна)»; пометка о дозагрузке — по statuses.UPLOAD_ALLOWED."""
    code = clean(status)
    label = PROCESS_STATUS_LABELS.get(code, code or DASH)
    return f"{label} (дозагрузка возможна)" if code in S.UPLOAD_ALLOWED else label


def version_label(protocol: dict) -> str:
    version = protocol.get("version")
    kind = "финальная" if protocol.get("process_status") == S.FINALIZED else "предварительная"
    return f"{version} ({kind})" if version not in (None, "") else f"{DASH} ({kind})"


def reason_label(code) -> str:
    code = clean(code)
    return f"{S.REASON_CODES[code]} · {code}" if code in S.REASON_CODES else show(code)


def confidence_label(finding: dict) -> str:
    """Уверенность гипотезы свободного поиска «0,75 — <из чего сложилась>»; нет или не число — пустая строка."""
    value = finding.get("confidence")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return ""
    basis = clean(as_dict(finding.get("verification")).get("basis")).strip()
    return f"{value:.2f}".replace(".", ",") + (f" — {basis}" if basis else "")


def machine_status(finding: dict) -> str:
    return clean(finding.get("machine_status") or finding.get("status"))


def priority(finding: dict) -> str:
    return clean(finding.get("review_priority")).upper()


def is_candidate(finding: dict) -> bool:
    """Разделы 4, 5 и 8: кандидаты, подтверждённые и кандидаты, решённые инспектором (машинный статус CANDIDATE)."""
    return finding.get("status") in (S.CANDIDATE, S.CONFIRMED_VIOLATION) or machine_status(finding) == S.CANDIDATE


# ---------------------------------------------------------------- модель документа

@dataclass(frozen=True)
class Run:
    """Кусок текста с оформлением; «\\n» внутри — перенос строки."""
    text: str
    bold: bool = False
    small: bool = False
    muted: bool = False
    color: str | None = None        # RRGGBB


def b(text, color: str | None = None) -> Run:
    return Run(clean(text), bold=True, color=color)


def sm(text, muted: bool = True) -> Run:
    return Run(clean(text), small=True, muted=muted)


def rich(*parts) -> tuple[Run, ...]:
    """Содержимое ячейки/абзаца из строк, Run и вложенных кортежей; пустое → «—»."""
    out: list[Run] = []
    for part in parts:
        if isinstance(part, Run):
            if part.text:
                out.append(part)
        elif isinstance(part, tuple):
            out.extend(r for r in part if isinstance(r, Run) and r.text)
        elif part is not None and part != "":
            out.append(Run(clean(part)))
    return tuple(out) or (Run(DASH),)


@dataclass
class Title:
    text: str


@dataclass
class Heading:
    text: str
    level: int = 1                   # 1 — «РАЗДЕЛ N», 2 — подраздел, 3 — карточка


@dataclass
class Para:
    content: tuple
    style: str = "body"              # body | note | alert


@dataclass
class Grid:
    header: tuple[str, ...] | None
    widths: tuple[float, ...]        # доли ширины полосы набора (сумма 1)
    rows: list[tuple]                # строки — кортежи ячеек (tuple[Run, ...])
    align: tuple[str, ...] = ()      # L | C | R по столбцам (по умолчанию L)
    label_cols: tuple[int, ...] = ()  # столбцы-подписи («поле | значение»): серый фон
    fills: dict = field(default_factory=dict)   # (строка, столбец; -1 — вся строка) → RRGGBB
    span: float = 1.0                # доля ширины полосы набора под таблицу
    zebra: bool = True
    hash_cols: tuple[int, ...] = ()  # столбцы с SHA-256 — мелкий шрифт, чтобы хеш не рвался


@dataclass
class Keep:
    blocks: list                     # держать на одной странице, если помещается (карточка доказательства)


@dataclass(frozen=True)
class Meta:
    protocol_id: str
    title: str
    subject: str
    footer: str
    generated_at: dt.datetime | None


def meta(protocol: dict) -> Meta:
    p = as_dict(protocol)
    obj = as_dict(p.get("object"))
    pid = show(p.get("protocol_id"))
    title = f"Протокол автоматизированной сверки № {pid}"
    subject = " · ".join(x for x in (clean(obj.get("name")).strip(), clean(obj.get("object_id")).strip()) if x)
    footer = f"Инспектор ИИ · {title} · версия {version_label(p)}"
    if clean(obj.get("object_id")).strip():
        footer += f" · {clean(obj.get('object_id')).strip()}"
    return Meta(pid, title, subject or title, footer, parse_ts(p.get("generated_at")))


# ---------------------------------------------------------------- протокол → блоки

class _Ctx:
    """Нормализованный протокол и производные списки, общие для разделов."""

    def __init__(self, protocol: dict):
        self.p = as_dict(protocol)
        self.obj = as_dict(self.p.get("object"))
        self.summary = as_dict(self.p.get("summary"))
        self.params = [x for x in as_list(self.p.get("params")) if isinstance(x, dict)]
        self.findings = [x for x in as_list(self.p.get("findings")) if isinstance(x, dict)]
        self.param_pos = {x.get("code"): i for i, x in enumerate(self.params)}
        self.by_fid = {x.get("finding_id"): x for x in self.findings}
        self.finalized = self.p.get("process_status") == S.FINALIZED
        candidates = sorted((f for f in self.findings if is_candidate(f)), key=self.order)
        high = [f for f in candidates if priority(f) == "HIGH"]
        rest = sorted((f for f in candidates if priority(f) != "HIGH"),
                      key=lambda f: (PRIORITY_RANK.get(priority(f), 3), self.order(f)))
        # сквозная нумерация разделов 4 и 5 (как в образце); тот же номер — у карточки в разделе 8
        self.numbered = list(enumerate(high + rest, 1))
        self.n_high = len(high)
        self.suspicions = sorted((f for f in self.findings if f.get("status") == S.SUSPICION), key=self.order)
        # машинные «расхождения нет» и кандидаты, отклонённые инспектором (как вкладка «Проверенные отрицательные»)
        self.negatives = sorted((f for f in self.findings if f.get("status") == S.NEGATIVE_VERIFIED), key=self.order)

    def order(self, f: dict) -> tuple:
        """Порядок Матрицы (как в params), затем код и локация; находки вне Матрицы (FREE-*) — в конце."""
        return (self.param_pos.get(f.get("code"), len(self.param_pos)), natural(clean(f.get("code"))),
                natural(clean(f.get("location"))), clean(f.get("finding_id")))

    def object_id(self, f: dict | None = None) -> str:
        return show((f or {}).get("object_id") or self.obj.get("object_id"))


def build_blocks(protocol: dict) -> list:
    c = _Ctx(protocol)
    return [*_header(c), *_section1(c), *_section2(c), *_section3(c), *_candidates(c, high=True),
            *_candidates(c, high=False), *_section6(c), *_section7(c), *_section8(c), *_section9(),
            *_appendix_negatives(c), *_appendix_files(c)]


def _header(c: _Ctx) -> list:
    p, o = c.p, c.obj
    status = clean(p.get("process_status"))
    icon, color = (("✔", "1E6B30") if status == S.FINALIZED else ("✖", "9C0006") if status == S.FAILED
                   else ("⚠", "8A5A00"))
    rows = [
        (rich(b("Объект:")), rich(show(o.get("name")), sm(f"\nobject_id: {show(o.get('object_id'))}")),
         rich(b("Адрес:")), rich(show(o.get("address")))),
        (rich(b("Номер надзорного дела:")), rich(show(o.get("permit_number"))),
         rich(b("Застройщик:")), rich(show(o.get("customer")))),
        (rich(b("Подрядчик:")), rich(show(o.get("contractor"))),
         rich(b("Дата формирования:")), rich(date_long(p.get("generated_at")))),
        (rich(b("Версия протокола:")), rich(version_label(p)),
         rich(b("Статус:")), rich(b(f"{icon} {process_status_label(status)}", color=color))),
    ]
    trace = as_dict(p.get("traceability"))
    trace_rows = [(rich(b(key), sm(f"\n{hint}")),
                   rich(show(trace.get(key) or (p.get("process_id") if key == "process_id" else None))))
                  for key, hint in TRACE_FIELDS]
    return [Title(f"ПРОТОКОЛ АВТОМАТИЗИРОВАННОЙ СВЕРКИ № {show(p.get('protocol_id'))}"),
            Para(rich(MARK_TEXT), style="alert"),
            Grid(None, (0.15, 0.35, 0.15, 0.35), rows, label_cols=(0, 2), zebra=False),
            Heading("Обязательная трассируемость", level=2),
            Grid(None, (0.24, 0.76), trace_rows, label_cols=(0,), zebra=False)]


def _section1(c: _Ctx) -> list:
    rows, fills = [], {}
    for i, u in enumerate(x for x in as_list(c.p.get("upload_status")) if isinstance(x, dict)):
        code = clean(u.get("status"))
        state, fill = UPLOAD_STATES.get(code.rsplit("_", 1)[-1], (show(code), None))
        ok, problems = as_int(u.get("files")), as_int(u.get("problem_files"))
        files = rich(str(ok + problems), sm(f"\nиз них не читаются: {problems}", muted=False) if problems else None)
        rows.append((rich(b(STAGE_LABELS.get(u.get("stage"), show(u.get("stage"))))), rich(state, sm(f"\n{code}")),
                     files, rich("—*"), rich(str(as_int(u.get("pages")))), rich(str(as_int(u.get("ocr_pages")))),
                     rich(show(u.get("comment")))))
        if fill:
            fills[(i, 1)] = fill
    if not rows:
        rows = [(rich(DASH), rich("нет данных"), rich("0"), rich(DASH), rich("0"), rich("0"),
                 rich("Статус загрузки не сформирован: файлы не загружены или не обработаны"))]
    blocks: list = [
        Heading("РАЗДЕЛ 1. СТАТУС ЗАГРУЗКИ ДОКУМЕНТОВ"),
        Grid(("Тип документа", "Статус", "Загружено файлов", "Ожидается", "Страниц", "OCR-страниц", "Комментарий"),
             (0.09, 0.13, 0.12, 0.08, 0.07, 0.09, 0.42), rows, align=("L", "L", "C", "C", "C", "C", "L"),
             fills=fills, zebra=False),
        Para(rich(sm("* Реестр ожидаемых файлов (ИАИС РиН) не передан: комплектность оценивается по загруженным "
                     "файлам; «Частично» — часть файлов стадии не читается.")), style="note"),
    ]
    scen = clean(c.p.get("scenario"))
    blocks.append(Para(rich(b("Сценарий загрузки: "),
                            f"{scen} — {SCENARIO_LABELS[scen]}" if scen in SCENARIO_LABELS else show(scen))))
    low: dict[tuple[str, str], list] = {}
    for item in (x for x in as_list(c.p.get("low_quality_pages")) if isinstance(x, dict)):
        low.setdefault((show(item.get("file_id")), show(item.get("reason"))), []).append(item.get("page"))
    if low:
        parts = [f"{fid} — {why}: стр. " + ", ".join(show(n) for n in sorted(pages, key=lambda n: natural(clean(n))))
                 for (fid, why), pages in sorted(low.items())]
        blocks.append(Para(rich(b("Страницы низкого качества (проверить вручную): "), "; ".join(parts) + ".")))
    n_files = len([x for x in as_list(c.p.get("files")) if isinstance(x, dict)])
    blocks.append(Para(rich(b("Реестр входных файлов: "),
                            f"{n_files} — file_id, SHA-256, стадия и статус чтения каждого файла приведены "
                            "в приложении 2 к протоколу.")))
    return blocks


def _appendix_files(c: _Ctx) -> list:
    """Реестр входных файлов — после раздела 9: в пакете объекта их бывают сотни, разделы 1–9 не раздуваются."""
    files = [x for x in as_list(c.p.get("files")) if isinstance(x, dict)]
    blocks: list = [Heading(f"ПРИЛОЖЕНИЕ 2. РЕЕСТР ВХОДНЫХ ФАЙЛОВ — {len(files)}")]
    if not files:
        return blocks + [Para(rich("Файлы не загружены."))]
    rows = []
    for n, f in enumerate(files, 1):
        doc = ", ".join(x for x in (f"шифр {clean(f.get('document_code'))}" if f.get("document_code") else "",
                                    f"ред. {clean(f.get('revision'))}" if f.get("revision") else "") if x)
        rel = clean(f.get("rel_path")).strip()
        src = clean(f.get("stage_source"))
        status = clean(f.get("status"))
        rows.append((
            rich(str(n)), rich(show(f.get("file_id"))),
            rich(show(f.get("name")), sm(f"\n{rel}") if rel and rel != clean(f.get("name")) else None,
                 sm(f"\n{doc}") if doc else None),
            rich(STAGE_LABELS.get(f.get("stage"), show(f.get("stage"))),
                 sm(f"\n{STAGE_SOURCE_LABELS.get(src, src)}") if src else None),
            rich(str(as_int(f.get("pages")))), rich(str(as_int(f.get("ocr_pages")))),
            rich(FILE_STATUS_LABELS.get(status, show(status)),
                 sm(f"\n{clean(f.get('error'))}", muted=False) if f.get("error") else None),
            rich(show(f.get("sha256")))))
    blocks.append(Grid(("№", "file_id", "Файл", "Стадия", "Стр.", "OCR", "Статус", "SHA-256"),
                       (0.035, 0.10, 0.22, 0.10, 0.04, 0.04, 0.10, 0.365), rows,
                       align=("C", "L", "L", "L", "C", "C", "L", "L"), hash_cols=(7,)))
    return blocks


def _section2(c: _Ctx) -> list:
    s = c.summary
    total = as_int(s.get("params_total"))
    confirmed = ("CONFIRMED_VIOLATION — подтверждённые нарушения" if c.finalized
                 else "CONFIRMED_VIOLATION в предварительной версии")
    lines = [
        ("Всего параметров в Матрице", total, "100%" if total else DASH),
        ("Выполнено сопоставление актуальных редакций", s.get("compared"), None),
        ("NEGATIVE_VERIFIED — проверено, расхождение не выявлено", s.get(S.NEGATIVE_VERIFIED), None),
        ("CANDIDATE — ожидает решения инспектора", s.get(S.CANDIDATE), None),
        (confirmed, s.get(S.CONFIRMED_VIOLATION), None),
        ("MISSING_EVIDENCE — нет документа или значения", s.get(S.MISSING_EVIDENCE), None),
        ("NOT_APPLICABLE — неприменимо к объекту", s.get(S.NOT_APPLICABLE), None),
        ("NOT_COMPARABLE / технические ошибки", s.get(S.NOT_COMPARABLE), None),
        ("CLARIFICATION_REQUIRED — требуется уточнение (редакции, привязка)", s.get(S.CLARIFICATION_REQUIRED), None),
        ("SUSPICION — вне итогов нарушений", s.get(S.SUSPICION), "не суммируется"),
    ]
    rows = [(rich(b(name) if i == 0 else name), rich(str(as_int(n))), rich(share or pct(n, total)))
            for i, (name, n, share) in enumerate(lines)]
    return [Heading("РАЗДЕЛ 2. СВОДНАЯ СТАТИСТИКА (С ПРОЦЕНТАМИ)"),
            Grid(("Показатель", "Количество", f"% от {total}"), (0.64, 0.18, 0.18), rows, align=("L", "C", "C"),
                 span=0.72),
            Para(rich(sm(f"Проценты — от числа параметров Матрицы ({total}). Статусы параметров взаимоисключающие: "
                         "каждый параметр учтён в одной строке по итоговому статусу с учётом решений инспектора. "
                         "SUSPICION — гипотезы свободного поиска вне Матрицы, в итог нарушений не входят.")),
                 style="note")]


def _param_comment(c: _Ctx, prm: dict) -> str:
    """Комментарий параметра; если пуст — обоснования его находок с тем же статусом (ссылки finding_ids)."""
    text = clean(prm.get("comment")).strip()
    if text:
        return text
    reasons = []
    for fid in as_list(prm.get("finding_ids")):
        f = c.by_fid.get(fid)
        if f and f.get("status") == prm.get("status") and clean(f.get("rationale")).strip():
            loc = clean(f.get("location")).strip()
            reasons.append(f"{loc}: {clean(f.get('rationale')).strip()}" if loc else clean(f.get("rationale")).strip())
    return "; ".join(reasons) or DASH


def _section3(c: _Ctx) -> list:
    params = [x for x in c.params if x.get("status") in UNMATCHED]
    blocks: list = [Heading("РАЗДЕЛ 3. ПАРАМЕТРЫ, НЕ ПРОВЕРЕННЫЕ ИЗ-ЗА ОТСУТСТВИЯ ИЛИ НЕПРИГОДНОСТИ ДОКУМЕНТОВ — "
                            f"{len(params)}")]
    if not params:
        return blocks + [Para(rich("Параметров без сопоставления нет."))]
    # подписи статусов — один раз в легенде, строки таблицы короткие (параметров здесь бывает больше сотни)
    counts = "; ".join(f"{st} ({STATUS_LABELS[st]}) — {sum(x.get('status') == st for x in params)}"
                       for st in UNMATCHED)
    blocks.append(Para(rich(sm(f"Из них: {counts}. Отсутствие документа или значения — показатель комплектности, "
                               "не нарушение.", muted=False)), style="note"))
    rows = [(rich(str(n)), rich(b(show(x.get("code")))), rich(show(x.get("section"))), rich(show(x.get("name"))),
             rich(show(x.get("status"))), rich(_param_comment(c, x)))
            for n, x in enumerate(params, 1)]
    blocks.append(Grid(("№", "Код", "Раздел", "Параметр", "Статус", "Комментарий (причина)"),
                       (0.04, 0.08, 0.06, 0.35, 0.16, 0.31), rows, align=("C", "L", "L", "L", "L", "L")))
    return blocks


def decision_cell(f: dict, suspicion: bool = False) -> tuple[Run, ...]:
    """Решение инспектора русской подписью + код, кто/когда, причина (reason_code) и комментарий."""
    insp = as_dict(f.get("inspector"))
    st = clean(insp.get("status")) or S.INSPECTOR_PENDING
    if st == S.INSPECTOR_PENDING or st not in DECISION_LABELS:
        if suspicion:
            return rich(b("Ожидает"), sm("\nSUSPICION / PENDING"))
        return rich(b(f"{machine_status(f) or S.CANDIDATE} / {st}"), sm("\nожидает решения инспектора"))
    icon, label, color = DECISION_LABELS[st]
    who = " · ".join(x for x in (st, clean(insp.get("user")).strip(),
                                 date_short(insp.get("decided_at")) if insp.get("decided_at") else "") if x)
    return rich(b(f"{icon} {label}", color=color), sm(f"\n{who}"),
                f"\nПричина: {reason_label(insp.get('reason_code'))}" if insp.get("reason_code") else None,
                f"\nКомментарий: {clean(insp.get('comment')).strip()}" if clean(insp.get("comment")).strip() else None)


def _deviation(f: dict) -> tuple[Run, ...]:
    prefix = {S.CANDIDATE: "Предварительное расхождение", S.CONFIRMED_VIOLATION: "Подтверждённое расхождение"}.get(
        f.get("status"), "Машинное расхождение")
    delta = clean(f.get("delta")).strip()
    if delta:
        return rich(f"{prefix}: ", b(delta))
    why = clean(f.get("rationale")).strip()
    return rich(f"{prefix}: ", sm(f"\n{why}", muted=False) if why else DASH)


def _values(f: dict) -> tuple[tuple, tuple, tuple]:
    actual = as_dict(f.get("actual_values"))
    return rich(show(f.get("expected_value"))), rich(show(actual.get("RD"))), rich(show(actual.get("ID")))


def _candidates(c: _Ctx, high: bool) -> list:
    items = c.numbered[:c.n_high] if high else c.numbered[c.n_high:]
    prefix = "" if c.finalized else "ПРЕДВАРИТЕЛЬНЫЕ "
    title = (f"РАЗДЕЛ 4. {prefix}КАНДИДАТЫ ВЫСОКОГО РИСКА — {len(items)}" if high
             else f"РАЗДЕЛ 5. {prefix}КАНДИДАТЫ СРЕДНЕГО И НИЗКОГО РИСКА — {len(items)}")
    blocks: list = [Heading(title)]
    if not items:
        what = "высокого риска" if high else "среднего и низкого риска"
        return blocks + [Para(rich(f"Кандидатов {what} нет."))]
    rows, fills = [], {}
    for i, (n, f) in enumerate(items):
        pd, rd, idv = _values(f)
        name = rich(show(f.get("parameter_name")), " ", b(f"({show(f.get('code'))})"),
                    sm(f"\nприоритет {priority(f) or DASH}") if not high else None)
        rows.append((rich(str(n)), rich(show(f.get("section"))), name, rich(show(f.get("location"))), pd, rd, idv,
                     _deviation(f), decision_cell(f)))
        fill = DECISION_FILLS.get(clean(as_dict(f.get("inspector")).get("status")))
        if fill:
            fills[(i, -1)] = fill
    blocks.append(Grid(("№", "Раздел", "Параметр (код)", "Локация", "ПД", "РД", "ИД", "Отклонение",
                        "Решение инспектора"),
                       (0.03, 0.06, 0.16, 0.11, 0.09, 0.09, 0.09, 0.17, 0.20), rows, fills=fills,
                       align=("C",) + ("L",) * 8))
    return blocks


def _section6(c: _Ctx) -> list:
    items = c.suspicions
    blocks: list = [Heading(f"РАЗДЕЛ 6. ГИПОТЕЗЫ СВОБОДНОГО ПОИСКА — {len(items)} (НЕ ВХОДЯТ В ЧИСЛО НАРУШЕНИЙ)")]
    if not items:
        return blocks + [Para(rich("Гипотез свободного поиска нет."))]
    rows = []
    for n, f in enumerate(items, 1):
        method = clean(f.get("discovery_method"))
        insp = as_dict(f.get("inspector"))
        rejected = insp.get("status") == S.NEGATIVE_VERIFIED
        loc = clean(f.get("location")).strip()
        confidence = confidence_label(f)
        description = rich(b(show(f.get("parameter_name"))), f"\nЛокация: {loc}" if loc else None,
                           sm(f"\n{clean(f.get('rationale')).strip()}", muted=False)
                           if clean(f.get("rationale")).strip() else None,
                           sm(f"\nУверенность: {confidence}", muted=False) if confidence else None,
                           sm(f"\n{show(f.get('code'))}"))
        reason = (rich(reason_label(insp.get("reason_code")) if insp.get("reason_code") else DASH,
                       f"\n{clean(insp.get('comment')).strip()}" if clean(insp.get("comment")).strip() else None)
                  if rejected else rich(DASH))
        pd, rd, idv = _values(f)
        rows.append((rich(str(n)), rich(DISCOVERY_LABELS.get(method, method or DASH)), description, pd, rd, idv,
                     decision_cell(f, suspicion=True), reason, rich(sm(SUSPICION_AI_COMMENT, muted=False))))
    blocks.append(Grid(("№", "Метод", "Описание", "ПД", "РД", "ИД", "Решение инспектора", "Причина отклонения",
                        "Комментарий ИИ"),
                       (0.03, 0.10, 0.21, 0.08, 0.08, 0.08, 0.11, 0.12, 0.19), rows, align=("C",) + ("L",) * 8))
    return blocks


def _expected_source(f: dict) -> dict | None:
    return next((s for s in as_list(f.get("sources")) if isinstance(s, dict) and s.get("role") == "expected"), None)


def _doc_ref(s: dict | None) -> str:
    """«НВС-2025.03-4.1-КР1, ред. 0, стр. 13» — откуда взято значение."""
    if not s:
        return ""
    parts = [clean(s.get("document_code")).strip() or clean(s.get("file_name")).strip() or clean(s.get("file_id"))]
    if clean(s.get("revision")).strip():
        parts.append(f"ред. {clean(s.get('revision')).strip()}")
    if clean(s.get("sheet")).strip():
        parts.append(f"лист {clean(s.get('sheet')).strip()}")
    if s.get("page") not in (None, ""):
        parts.append(f"стр. {clean(s.get('page'))}")
    return ", ".join(x for x in parts if x)


def _section7(c: _Ctx) -> list:
    blocks: list = [Heading("РАЗДЕЛ 7. РЕЗОЛЮТИВНАЯ ЧАСТЬ — ДОСТУПНА ТОЛЬКО ПОСЛЕ ВЕРИФИКАЦИИ И ФИНАЛИЗАЦИИ")]
    card_no = {id(f): n for n, f in c.numbered}
    groups = (("7.1. По подтверждённым нарушениям высокого риска", "Вид работ", True),
              ("7.2. По подтверждённым нарушениям среднего и низкого риска", "Вид нарушения", False))
    for title, kind, high in groups:
        blocks.append(Heading(title, level=2))
        if not c.finalized:
            rows = [(rich(DASH), rich("До завершения верификации"), rich(NOT_FORMED_TEXT))]
        else:
            confirmed = [f for _, f in c.numbered
                         if f.get("status") == S.CONFIRMED_VIOLATION and (priority(f) == "HIGH") == high]
            rows = []
            for n, f in enumerate(confirmed, 1):
                expected = clean(f.get("expected_value")).strip()
                comment = clean(as_dict(f.get("inspector")).get("comment")).strip()
                basis = _doc_ref(_expected_source(f))
                work = rich(f"{show(f.get('section'))} · {show(f.get('parameter_name'))} ",
                            b(f"({show(f.get('code'))})"), f"\nЛокация: {show(f.get('location'))}",
                            f"\nНарушение: {clean(f.get('delta')).strip()}" if clean(f.get("delta")).strip() else None,
                            sm(f"\nкарточка № {card_no.get(id(f), DASH)} · {show(f.get('finding_id'))}"))
                advice = rich(b(f"Устранить расхождение: привести к значению ПД «{expected}»." if expected
                                else "Устранить расхождение: привести в соответствие с ПД."),
                              f"\nОснование (ПД): {basis}" if basis else None,
                              f"\nКомментарий инспектора: {comment}" if comment else "\nКомментарий инспектора: —")
                rows.append((rich(str(n)), work, advice))
            if not rows:
                what = "высокого риска" if high else "среднего и низкого риска"
                rows = [(rich(DASH), rich(f"Подтверждённых нарушений {what} нет"), rich(DASH))]
        blocks.append(Grid(("№", kind, "Конкретная рекомендация"), (0.05, 0.42, 0.53), rows,
                           align=("C", "L", "L"), zebra=False))
    return blocks


def source_cell(s: dict) -> tuple[Run, ...]:
    """Источник доказательства: file_id, SHA-256, стадия, шифр, редакция, approval_status, лист/страница, bbox."""
    stage = STAGE_LABELS.get(s.get("stage"), show(s.get("stage")))
    bbox = fmt_bbox(s.get("bbox"))
    bsrc = clean(s.get("bbox_source"))
    where = (f"{bbox} ({BBOX_SOURCE_LABELS.get(bsrc, bsrc)})" if bbox and bsrc else bbox
             if bbox else "нет — доказательство на уровне страницы")
    value, snippet = clean(s.get("value")).strip(), clean(s.get("snippet")).strip()
    quote = " · ".join(x for x in (f"значение: «{value}»" if value else "", f"цитата: «{snippet}»" if snippet else "")
                       if x)
    return rich(b(f"{stage} · file_id: {show(s.get('file_id'))}"), f" · «{show(s.get('file_name'))}»",
                f"\nSHA-256: {show(s.get('sha256'))}",
                f"\nстадия: {stage} · шифр: {show(s.get('document_code'))} · редакция: {show(s.get('revision'))}"
                f" · approval_status: {show(s.get('approval_status'))}",
                f"\nлист: {show(s.get('sheet'))} · страница: {show(s.get('page'))} · bbox [0;1]: {where}",
                f"\n{quote}" if quote else None)


def _card(c: _Ctx, n: int, f: dict) -> Keep:
    sources = [s for s in as_list(f.get("sources")) if isinstance(s, dict)]
    expected = [s for s in sources if s.get("role") == "expected"]
    actual = [s for s in sources if s.get("role") != "expected"]
    actual_values = as_dict(f.get("actual_values"))
    exp_src = _doc_ref(expected[0]) if expected else ""
    act_text = "; ".join(f"{STAGE_LABELS.get(k, k)}: {show(v)}" for k, v in actual_values.items()) or DASH
    verification = as_dict(f.get("verification"))
    insp = as_dict(f.get("inspector"))
    decision = clean(insp.get("status")) or S.INSPECTOR_PENDING
    label = DECISION_LABELS.get(decision, (None, "ожидает решения инспектора", None))[1].lower()
    finalized = ("протокол финализирован (FINALIZED)" if c.finalized
                 else "протокол не финализирован — решение предварительное")
    rows = [
        (rich(b("finding_id")), rich(b(show(f.get("finding_id"))))),
        (rich(b("object_id / matrix_code / rule_version")),
         rich(f"{c.object_id(f)} / {show(f.get('code'))} / {show(f.get('rule_version'))}")),
        (rich(b("Параметр / локация")),
         rich(f"{show(f.get('section'))} · {show(f.get('parameter_name'))} · {show(f.get('location'))}")),
        (rich(b("expected_value")), rich(b(show(f.get("expected_value"))),
                                         f" — из ПД: {exp_src}" if exp_src else " — источник ПД не указан")),
        (rich(b("actual_value")), rich(act_text)),
        (rich(b("Отклонение / обоснование")),
         rich(show(f.get("delta")), f"\n{clean(f.get('rationale')).strip()}" if clean(f.get("rationale")).strip()
              else None)),
    ]
    if any(verification.get(k) for k in ("same_entity", "trigger_met", "analysis")):      # судья и верификатор
        rows.append((rich(b("Верификация")),
                     rich(f"same_entity: {show(verification.get('same_entity'))} · "
                          f"trigger_met: {show(verification.get('trigger_met'))}",
                          f"\n{clean(verification.get('analysis')).strip()}"
                          if clean(verification.get("analysis")).strip() else None)))
    if confidence_label(f):                          # гипотеза свободного поиска, привязанная инспектором
        rows.append((rich(b("confidence")), rich(confidence_label(f))))
    rows.append((rich(b("source_expected")), rich(*_join_sources(expected))))
    if actual:
        for s in actual:
            rows.append((rich(b("source_actual"), sm(f"\n{STAGE_LABELS.get(s.get('stage'), show(s.get('stage')))}")),
                         source_cell(s)))
    else:
        rows.append((rich(b("source_actual")), rich("нет")))
    rows += [
        (rich(b("approved_change_ref")), rich(show(f.get("approved_change_ref")))),
        (rich(b("completeness_status")), rich(show(f.get("completeness_status")))),
        (rich(b("finding_status")), rich(show(f.get("finding_status") or f.get("status")),
                                         sm(f"\nмашинный статус: {show(machine_status(f))}"))),
        (rich(b("review_priority")), rich(b(show(priority(f))), f" — {show(f.get('criticality'))}",
                                          sm("\nтолько порядок рассмотрения"))),
        (rich(b("expert_decision")),
         rich(f"user_id: {show(insp.get('user'))} · timestamp: {show(insp.get('decided_at'))}",
              f"\nрешение: {decision} — {label}",
              f"\nreason_code: {reason_label(insp.get('reason_code'))}",
              f"\nкомментарий: {show(insp.get('comment'))}",
              f"\nподпись/атрибут финализации: {finalized}")),
    ]
    head = f"Карточка № {n} · {show(f.get('code'))} · {show(f.get('location'))}"
    return Keep([Heading(head, level=3), Grid(("Поле", "Содержание"), (0.24, 0.76), rows, label_cols=(0,),
                                               zebra=False)])


def _join_sources(sources: list[dict]) -> list:
    out: list = []
    for i, s in enumerate(sources):
        if i:
            out.append("\n\n")
        out.append(source_cell(s))
    return out or ["нет"]


def _section8(c: _Ctx) -> list:
    blocks: list = [Heading("РАЗДЕЛ 8. ОБЯЗАТЕЛЬНАЯ КАРТОЧКА ДОКАЗАТЕЛЬСТВА"), Para(rich(CARD_INTRO))]
    if not c.numbered:
        return blocks + [Para(rich("Кандидатов и подтверждённых нарушений нет — карточки не формируются."))]
    return blocks + [_card(c, n, f) for n, f in c.numbered]


def _section9() -> list:
    return [Heading("РАЗДЕЛ 9. ПРАВИЛА ПОДСЧЁТА"),
            Grid(("Категория", "Правило включения в итог"), (0.30, 0.70),
                 [(rich(b(k)), rich(v)) for k, v in COUNTING_RULES], zebra=False)]


def _negative_basis(f: dict) -> tuple[Run, ...]:
    """Обоснование отрицательного результата: решение инспектора по кандидату (кто, когда, причина, комментарий)
    или вывод машинной сверки (rationale)."""
    if machine_status(f) == S.CANDIDATE:
        return decision_cell(f)
    why = clean(f.get("rationale")).strip()
    return rich(b("Машинная сверка: расхождение не выявлено"), sm(f"\n{why}", muted=False) if why else None)


def _appendix_negatives(c: _Ctx) -> list:
    """Проверенные отрицательные — после раздела 9: их бывает больше сотни (по локациям), разделы 1–9 не
    раздуваются. Число в разделе 2 — по параметрам, здесь — по находкам: у параметра их бывает несколько."""
    items = c.negatives
    blocks: list = [Heading(f"ПРИЛОЖЕНИЕ 1. ПРОВЕРЕННЫЕ ОТРИЦАТЕЛЬНЫЕ (NEGATIVE_VERIFIED) — {len(items)}")]
    if not items:
        return blocks + [Para(rich("Проверенных отрицательных нет."))]
    blocks.append(Para(rich(sm("Совпавшие значения по результатам машинной сверки и кандидаты, отклонённые "
                               "инспектором. Не нарушения; отклонённые инспектором кандидаты — источник "
                               "отрицательных GOLD-примеров.", muted=False)), style="note"))
    rows, fills = [], {}
    for i, f in enumerate(items):
        pd, rd, idv = _values(f)
        rows.append((rich(str(i + 1)), rich(show(f.get("section"))),
                     rich(show(f.get("parameter_name")), " ", b(f"({show(f.get('code'))})")),
                     rich(show(f.get("location"))), pd, rd, idv, _negative_basis(f)))
        fill = DECISION_FILLS.get(clean(as_dict(f.get("inspector")).get("status")))
        if fill:
            fills[(i, -1)] = fill
    blocks.append(Grid(("№", "Раздел", "Параметр (код)", "Локация", "ПД", "РД", "ИД", "Обоснование"),
                       (0.03, 0.06, 0.20, 0.14, 0.10, 0.10, 0.10, 0.27), rows, fills=fills,
                       align=("C",) + ("L",) * 7))
    return blocks
