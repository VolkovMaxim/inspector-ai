"""Классификатор типа страницы, который дообучается на правках разметчиков (ТЗ 9.4, Q&A №24).

Модель — каталог с manifest.json и predict.py (исходник кода — model_code/page_cls):
load(model_dir, device) -> Classifier; Classifier.predict(pages) -> [{cls, cls_p,
cls_top3, first, first_p, section, section_p, quality, quality_p}]; retrain_heads(model_dir, pages, out_dir) ->
{seconds, n_corrections, version}. Страница на входе: {pdf_path, page_no, width_pt, height_pt, text, words:
[[x0,y0,x1,y1,текст], ...]}. Обучающих данных в модели нет.

Этап 4 конвейера: модель размечает страницы; страницы с уверенностью в классе ниже порога размечает Qwen (как раньше).
Правка разметчика (page_corrections) сразу меняет метку страницы. Дообучение — только когда набор загружен и есть
правки (иначе отказ сразу). Итерация: страницы загруженных проверок (исправленные — с меткой правки и её весом,
остальные — с текущей меткой) -> головы исходной модели дообучаются от своих весов -> новая версия в data_dir/models ->
метрики на правках и на замороженном наборе (PAGE_CLS_ACCEPTANCE, если задан) -> версию утверждает или отклоняет
ответственный. Всё пишется в ml_retraining_log; откат — к любой утверждённой версии. Версии — model_versions
«page-cls:<версия>»."""
from __future__ import annotations

import hashlib
import importlib.util
import inspect
import json
import logging
import time
from functools import lru_cache
from pathlib import Path

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from inspector.config import Settings
from inspector.ml import registry as R
from inspector.models import File, ModelVersion, Page, PageCorrection, PageLabel, Process, RetrainingLog, utcnow
from inspector.pipeline import bbox as bbox_mod
from inspector.pipeline.pages import load_words

log = logging.getLogger("inspector.page_classifier")
KIND = "page-cls"
REG = R.Kind(KIND, "page-classifier", "page-corrections")
BATCH, TITLE_LEN, OTHER_MIN_P = 32, 140, 0.25
ClassifierError = R.ModelError


# ------------------------------------------------------------------------------------------------ версии модели
def version_name(version: str) -> str:
    return R.version_name(REG, version)


def read_manifest(model_dir: str | Path) -> dict:
    return json.loads((Path(model_dir) / "manifest.json").read_text(encoding="utf-8"))


def deployed(db: Session) -> ModelVersion | None:
    """Действующая версия: последняя опубликованная утверждённая."""
    return R.deployed(db, REG)


def register(db: Session, model_dir: str | Path, status: str, deploy: bool, metrics: dict | None = None,
             approved_by: str = "", rollback_to: str | None = None) -> ModelVersion:
    man = read_manifest(model_dir)
    return R.register(db, REG, man.get("version") or Path(model_dir).name, str(model_dir), status, deploy,
                      metrics or man.get("metrics") or {}, approved_by, rollback_to)


def active_model_dir(db: Session, settings: Settings) -> Path | None:
    """Каталог действующей модели. Начальная модель из PAGE_CLASSIFIER_DIR при первом обращении регистрируется
    опубликованной версией (чтобы к ней можно было откатиться)."""
    mv = deployed(db)
    if mv is not None and (Path(mv.artifact) / "manifest.json").exists():
        return Path(mv.artifact)
    initial = Path(settings.page_classifier_dir) if settings.page_classifier_dir else None
    if initial is None or not (initial / "manifest.json").exists():
        return None
    register(db, initial, "APPROVED", deploy=True, approved_by="system")
    return initial


@lru_cache(maxsize=4)
def load_classifier(model_dir: str, device: str):
    """-> (модуль predict.py каталога, загруженный классификатор). Модуль грузится под уникальным именем каталога."""
    path = Path(model_dir) / "predict.py"
    if not path.exists():
        raise ClassifierError(f"в каталоге модели нет predict.py: {model_dir}")
    name = "page_cls_" + hashlib.sha1(str(Path(model_dir).resolve()).encode()).hexdigest()[:12]
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, module.load(model_dir, device)


def device(settings: Settings) -> str:
    if settings.page_classifier_device != "auto":
        return settings.page_classifier_device
    try:
        import torch  # noqa: PLC0415 — torch есть только в образе с классификатором

        return "cuda" if torch.cuda.is_available() else "cpu"
    except Exception:  # noqa: BLE001
        return "cpu"


def threshold(settings: Settings, model_dir: Path) -> float:
    """Порог уверенности в классе: ниже — страница уходит к Qwen. Настройка, иначе из manifest (fallback_threshold
    или порог гибрида metrics.hybrid_threshold, выбранный при обучении на отложенном объекте), иначе 0,5."""
    if settings.page_classifier_min_conf > 0:
        return settings.page_classifier_min_conf
    man = read_manifest(model_dir)
    return float(man.get("fallback_threshold") or (man.get("metrics") or {}).get("hybrid_threshold") or 0.5)


def first_to_qwen(settings: Settings, f: File, pred: dict) -> bool:
    """Первая страница, заголовок которой нужен от Qwen (см. Settings.page_classifier_first_to_qwen)."""
    mode = settings.page_classifier_first_to_qwen
    if mode == "off" or not pred.get("first"):
        return False
    return mode == "all" or f.doc_stage in (None, "", "ID")


# ------------------------------------------------------------------------------------------------ разметка страниц
def page_words(settings: Settings, f: File, p: Page, layer_cache: dict) -> list:
    """Слова страницы с рамками в долях видимой страницы: слой PDF или кэш RapidOCR (как их видит сервис)."""
    if p.text_source == "layer":
        if f.sha256 not in layer_cache:
            layer_cache[f.sha256] = load_words(settings.data_dir, f.sha256)
        return [list(w) for w in layer_cache[f.sha256].get(p.page_no, [])]
    cf = bbox_mod.cache_file(settings.data_dir, f.sha256, p.page_no, settings.rapidocr_orient)
    if cf.exists():
        data = json.loads(cf.read_text(encoding="utf-8"))
        return data["words"] if isinstance(data, dict) else data
    return []


def page_input(settings: Settings, f: File, p: Page, layer_cache: dict) -> dict:
    return {"pdf_path": str(f.pdf_path), "page_no": p.page_no, "width_pt": p.width, "height_pt": p.height,
            "text": p.text or "", "words": page_words(settings, f, p, layer_cache)}


def predict_inputs(module_and_clf, inputs: list[dict], batch: int = BATCH) -> list[dict]:
    _, clf = module_and_clf
    out: list[dict] = []
    for i in range(0, len(inputs), batch):
        out.extend(clf.predict(inputs[i:i + batch]))
    return out


def first_line(text: str) -> str | None:
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip()[:TITLE_LEN]
    return None


def apply_prediction(page: Page, pred: dict, version: str) -> PageLabel:
    """Метка модели -> страница. Заголовок у модели нет — берётся первая строка текста (по нему работает правило
    «Приложение» при сборке ИД)."""
    page.page_class = pred["cls"]
    page.other_classes = [c for c, p in (pred.get("cls_top3") or [])[1:] if p >= OTHER_MIN_P and c != pred["cls"]][:2]
    page.is_first_page = bool(pred.get("first"))
    page.section = pred.get("section")
    page.quality_label = pred.get("quality")
    page.low_quality = pred.get("quality") in ("плохой скан", "рукописный")
    page.title = page.title or first_line(page.text)
    return PageLabel(page_pk=page.id, source="model", model_version=version, page_class=page.page_class,
                     is_first_page=page.is_first_page, section=page.section, quality_label=page.quality_label,
                     confidence=float(pred.get("cls_p") or 0))


def model_label(page: Page, pred: dict, version: str) -> PageLabel:
    """Ответ модели только в историю меток (страницу размечает Qwen): видно, что модель думала и насколько уверенно."""
    return PageLabel(page_pk=page.id, source="model", model_version=version, page_class=pred.get("cls"),
                     is_first_page=bool(pred.get("first")), section=pred.get("section"),
                     quality_label=pred.get("quality"), confidence=float(pred.get("cls_p") or 0))


def classify_with_model(session: Session, settings: Settings, pages: list[Page], files: dict[int, File],
                        progress=None) -> tuple[set[tuple[int, int]], dict]:
    """Разметить страницы моделью. -> (страницы, размеченные моделью уверенно; статистика). Остальное — Qwen:
    неуверенные и первые страницы, которым нужен заголовок (first_to_qwen); ответ модели по ним — в истории меток."""
    stats = {"model": None, "by_model": 0, "to_qwen": 0, "first_to_qwen": 0, "model_errors": 0}
    model_dir = active_model_dir(session, settings)
    if model_dir is None or not pages:
        return set(), stats
    try:
        loaded = load_classifier(str(model_dir), device(settings))
    except Exception as exc:  # noqa: BLE001 — модель не загрузилась: вся разметка уйдёт к Qwen
        log.error("page classifier load failed", extra={"model_dir": str(model_dir), "error": str(exc)[:300]})
        stats["model_errors"] = len(pages)
        return set(), stats
    version = read_manifest(model_dir).get("version") or model_dir.name
    stats["model"] = version_name(version)
    thr = threshold(settings, model_dir)
    layer_cache: dict = {}
    done: set[tuple[int, int]] = set()
    for i in range(0, len(pages), BATCH * 4):
        part = pages[i:i + BATCH * 4]
        try:
            preds = predict_inputs(loaded, [page_input(settings, files[p.file_pk], p, layer_cache) for p in part],
                                   settings.page_classifier_batch)
        except Exception as exc:  # noqa: BLE001 — сбой пачки: её страницы к Qwen
            log.warning("page classifier batch failed", extra={"error": str(exc)[:300]})
            stats["model_errors"] += len(part)
            continue
        for p, pred in zip(part, preds):
            sure = float(pred.get("cls_p") or 0) >= thr
            if sure and first_to_qwen(settings, files[p.file_pk], pred):
                stats["first_to_qwen"] += 1
                sure = False
            if sure:
                session.add(apply_prediction(p, pred, version))
                done.add((p.file_pk, p.page_no))
            else:
                session.add(model_label(p, pred, version))
        if progress:
            progress(min(i + len(part), len(pages)), len(pages))
    stats["by_model"] = len(done)
    stats["to_qwen"] = len(pages) - len(done)
    return done, stats


# ------------------------------------------------------------------------------------------------ правки
EDITABLE = ("page_class", "is_first_page", "section")


def correct_pages(db: Session, process: Process, user, items: list[dict], comment: str | None = None
                  ) -> list[PageCorrection]:
    """Правки разметчика: [{page_pk | file_pk + page_no, page_class?, is_first_page?, section?}] -> метки страниц
    меняются сразу, правка сохраняется как обучающая метка."""
    out = []
    object_code = process.object.object_code if process.object else ""
    for item in items:
        page = db.get(Page, item["page_pk"]) if item.get("page_pk") else db.scalars(
            select(Page).where(Page.file_pk == item.get("file_pk"), Page.page_no == item.get("page_no"))).first()
        f = db.get(File, page.file_pk) if page is not None else None
        if page is None or f is None or f.process_id != process.id:
            raise ClassifierError(f"страница не найдена в проверке: {item}")
        changes = {k: item[k] for k in EDITABLE if k in item and item[k] is not None}
        if not changes:
            continue
        last = db.scalars(select(PageLabel).where(PageLabel.page_pk == page.id).order_by(PageLabel.id.desc())).first()
        before = {k: getattr(page, k) for k in EDITABLE} | {"source": last.source if last else None,
                                                             "confidence": last.confidence if last else None}
        for k, v in changes.items():
            setattr(page, k, v)
        corr = PageCorrection(page_pk=page.id, process_id=process.id, object_code=object_code, file_sha256=f.sha256,
                              page_no=page.page_no, user_pk=getattr(user, "id", None), before=before,
                              comment=comment, **changes)
        db.add(corr)
        db.add(PageLabel(page_pk=page.id, source="inspector", page_class=page.page_class,
                         is_first_page=page.is_first_page, section=page.section, quality_label=page.quality_label))
        out.append(corr)
    db.flush()
    return out


def latest_corrections(db: Session) -> list[PageCorrection]:
    """Все правки; у страницы (файл по SHA-256 + номер) побеждает последняя."""
    rows = list(db.scalars(select(PageCorrection).order_by(PageCorrection.id)))
    last: dict[tuple[str, int], PageCorrection] = {}
    for r in rows:
        last[(r.file_sha256, r.page_no)] = r
    return list(last.values())


def correction_items(db: Session, settings: Settings, rows: list[PageCorrection]) -> list[dict]:
    """Правки -> вход retrain_heads: {"page": вход страницы, "cls"?, "first"?, "section"?}."""
    layer_cache: dict = {}
    items = []
    for r in rows:
        page = db.get(Page, r.page_pk)
        f = db.get(File, page.file_pk) if page is not None else None
        if page is None or f is None:
            continue
        it = {"page": page_input(settings, f, page, layer_cache)}
        if r.page_class is not None:
            it["cls"] = r.page_class
        if r.is_first_page is not None:
            it["first"] = r.is_first_page
        if r.section is not None:
            it["section"] = r.section
        items.append(it)
    return items


def _labeled_pages():
    """Страницы загруженных проверок, у которых есть метка (тип, первая страница или раздел) и PDF для отрисовки."""
    return (select(Page, File).join(File, File.id == Page.file_pk)
            .where(File.status == "ok", File.pdf_path.is_not(None),
                   or_(Page.page_class.is_not(None), Page.is_first_page.is_not(None), Page.section.is_not(None))))


def dataset_state(db: Session) -> dict:
    """Что есть для дообучения: страницы загруженных проверок с метками и правки разметчиков."""
    pages = db.scalar(select(func.count()).select_from(_labeled_pages().subquery())) or 0
    corrections = db.scalar(select(func.count()).select_from(PageCorrection)) or 0
    return {"pages": pages, "corrections": corrections}


def check_can_retrain(db: Session) -> dict:
    """Дообучение — только на загруженном наборе и только после ручных правок; иначе отказ сразу, до очереди."""
    st = dataset_state(db)
    if st["pages"] == 0:
        raise ClassifierError("дообучение недоступно: набор документов не загружен — загрузите и обработайте пакет")
    if st["corrections"] == 0:
        raise ClassifierError("дообучение недоступно: правок разметчиков нет — исправьте типы страниц во вкладке "
                              "«Страницы» проверки")
    return st


def training_items(db: Session, settings: Settings, rows: list[PageCorrection], labels: dict | None) -> list[dict]:
    """Набор итерации — страницы загруженных проверок: у страницы с правкой метка правки поверх текущей и вес правки
    (задаёт модель), у остальных — текущая метка (Qwen, модель) и вес 1. Страница файла (SHA-256 + номер) — один раз.
    labels — словари модели: метки вне них не передаются."""
    corr = {(r.file_sha256, r.page_no): r for r in rows}
    allowed = {"cls": set(labels.get("cls", [])), "section": set(labels.get("section", []))} if labels else None
    items, seen, layer_cache, cur_sha = [], set(), {}, None
    for page, f in db.execute(_labeled_pages().order_by(File.id, Page.page_no)).all():
        key = (f.sha256, page.page_no)
        if key in seen:
            continue
        seen.add(key)
        if f.sha256 != cur_sha:                  # слова слоя держим только для текущего файла
            layer_cache, cur_sha = {}, f.sha256
        c = corr.get(key)
        it: dict = {}
        for field, attr in (("cls", "page_class"), ("first", "is_first_page"), ("section", "section")):
            v = getattr(c, attr) if c is not None and getattr(c, attr) is not None else getattr(page, attr)
            if v is None or (allowed is not None and field in allowed and v not in allowed[field]):
                continue
            it[field] = v
        if not it:
            continue
        it["page"] = page_input(settings, f, page, layer_cache)
        if c is None:
            it["weight"] = 1.0
        items.append(it)
    return items


def items_sha256(rows: list[PageCorrection]) -> str:
    key = sorted((r.file_sha256, r.page_no, r.page_class, r.is_first_page, r.section) for r in rows)
    return hashlib.sha256(json.dumps(key, ensure_ascii=False, default=str).encode()).hexdigest()


# ------------------------------------------------------------------------------------------------ дообучение
def load_acceptance(settings: Settings) -> list[dict] | None:
    """Замороженный набор приёмки: JSONL {"page": вход страницы, "cls", "first"?, "section"?}; нет файла — None."""
    path = Path(settings.page_cls_acceptance) if settings.page_cls_acceptance else None
    if path is None or not path.exists():
        return None
    items = [json.loads(line) for line in open(path, encoding="utf-8") if line.strip()]
    for it in items:                     # относительный путь PDF — от каталога набора (переносимый набор)
        pdf = Path(it["page"]["pdf_path"])
        if not pdf.is_absolute():
            it["page"]["pdf_path"] = str(path.parent / pdf)
    return items


def evaluate(settings: Settings, model_dir: Path, items: list[dict]) -> dict:
    """Доля совпадений с метками по классу / первой странице / разделу (там, где метка задана)."""
    if not items:
        return {}
    preds = predict_inputs(load_classifier(str(model_dir), device(settings)), [it["page"] for it in items],
                           settings.page_classifier_batch)
    res = {}
    for key, pkey in (("cls", "cls"), ("first", "first"), ("section", "section")):
        pairs = [(it[key], pr.get(pkey)) for it, pr in zip(items, preds) if key in it]
        if pairs:
            ok = sum(1 for a, b in pairs if a == b)
            res[key] = {"ok": ok, "n": len(pairs), "acc": round(ok / len(pairs), 4)}
    return res


def request_retrain(db: Session, requested_by: str) -> RetrainingLog:
    """Поставить итерацию дообучения в очередь (одна за раз; её выполнит worker). Набор не загружен или правок нет —
    ClassifierError сразу."""
    check_can_retrain(db)
    return R.request(db, REG, requested_by)


def initial_model_dir(settings: Settings) -> Path | None:
    d = Path(settings.page_classifier_dir) if settings.page_classifier_dir else None
    return d if d is not None and (d / "manifest.json").exists() else None


def unique_version(db: Session, model_dir: Path, version: str, suffix: str) -> str:
    """Имя новой версии даёт retrain_heads модели, и оно может совпасть с уже известным реестру (у модели-заглушки —
    «итерация базы + 1», а база не меняется, пока черновик не утверждён). Совпадение склеило бы две модели в одну запись
    реестра: утвердилась бы не та модель, а отказ приёмки затёр бы принятую версию. Поэтому занятое имя получает суффикс
    итерации и записывается в manifest копии — его читают разметка, реестр и следующая итерация."""
    name = f"{version}+{suffix}" if R.taken(db, REG, version) else version
    man = read_manifest(model_dir)
    if man.get("version") != name:
        man["version"] = name
        (Path(model_dir) / "manifest.json").write_text(json.dumps(man, ensure_ascii=False, indent=1), encoding="utf-8")
    return name


def run_pending(db: Session, settings: Settings) -> int:
    rows = R.queued(db, REG)
    for row in rows:
        run_retrain(db, settings, row)
    return len(rows)


def fail_interrupted(db: Session) -> int:
    return R.fail_interrupted(db, REG)


def run_retrain(db: Session, settings: Settings, row: RetrainingLog) -> RetrainingLog:
    """Одна итерация: от исходной модели на страницах загруженных проверок и всех правках -> retrain_heads -> метрики
    до/после на правках (и на замороженном наборе) -> решение по правилу приёмки."""
    started = time.time()
    row.status = "RUNNING"
    db.commit()
    try:
        base_dir = initial_model_dir(settings) or active_model_dir(db, settings)
        if base_dir is None:
            raise ClassifierError("нет модели классификатора (PAGE_CLASSIFIER_DIR)")
        rows = latest_corrections(db)
        items = correction_items(db, settings, rows)
        if not items:
            raise ClassifierError("дообучение недоступно: правок разметчиков нет")
        train = training_items(db, settings, rows, read_manifest(base_dir).get("labels"))
        base_version = read_manifest(base_dir).get("version") or base_dir.name
        out_dir = settings.data_dir / "models" / KIND / f"{time.strftime('%Y%m%d-%H%M%S')}-{row.id}"
        acceptance = load_acceptance(settings)
        before = {"corrections": evaluate(settings, base_dir, items)}
        if acceptance:
            before["frozen"] = evaluate(settings, base_dir, acceptance)
        module, clf = load_classifier(str(base_dir), device(settings))
        takes_clf = "clf" in inspect.signature(module.retrain_heads).parameters   # модель умеет взять загруженную
        res = module.retrain_heads(str(base_dir), train, str(out_dir), **({"clf": clf} if takes_clf else {}))
        new_version = unique_version(db, out_dir, res.get("version") or read_manifest(out_dir).get("version")
                                     or out_dir.name, f"it{row.id}")
        after = {"corrections": evaluate(settings, out_dir, items)}
        if acceptance:
            after["frozen"] = evaluate(settings, out_dir, acceptance)
        row.base_version, row.new_version, row.artifact = version_name(base_version), version_name(new_version), str(out_dir)
        row.n_corrections, row.corrections_sha256 = len(items), items_sha256(rows)
        after["train"] = {"pages": len(train), "corrected": sum(1 for it in train if "weight" not in it)}
        row.metrics_before, row.metrics_after = before, after
        drop = settings.retrain_max_drop
        if acceptance:
            row.rule = (f"класс на замороженном наборе ({len(acceptance)} стр.) не ниже прежнего больше чем на "
                        f"{100 * drop:.1f} п. п.")
            ok = after["frozen"].get("cls", {}).get("acc", 0) >= before["frozen"].get("cls", {}).get("acc", 0) - drop
            # прошла приёмку — только кандидат: публикует человек (Q&A №24: автоматическая публикация запрещена)
            row.status = "ACCEPTED" if ok else "REJECTED"
            row.rule += "; прошла — ждёт утверждения ответственным" if ok else "; не прошла — публиковать нельзя"
            register(db, out_dir, "DRAFT" if ok else "REJECTED", deploy=False, metrics=after)
        else:
            row.rule = "замороженного набора нет — версия ждёт утверждения ответственным"
            row.status = "DRAFT"
            register(db, out_dir, "DRAFT", deploy=False, metrics=after)
        for r in rows:
            r.used_in_version = r.used_in_version or row.new_version
    except Exception as exc:  # noqa: BLE001 — итерация не удалась: запись в журнале, модель прежняя
        db.rollback()
        row = db.get(RetrainingLog, row.id)
        row.status, row.error = "FAILED", f"{type(exc).__name__}: {exc}"[:1000]
        log.error("retrain failed", extra={"error": row.error})
    row.seconds, row.finished_at = round(time.time() - started, 2), utcnow()
    db.commit()
    return row


def approve(db: Session, version: str, user_login: str) -> ModelVersion:
    """Утвердить черновую версию (подпись ответственного) и опубликовать её."""
    return R.approve(db, REG, version, user_login)


def reject(db: Session, version: str, user_login: str) -> ModelVersion:
    """Отклонить версию-кандидата (ручное reject, Q&A №24): её нельзя опубликовать, действующая не меняется."""
    return R.reject(db, REG, version, user_login)


def rollback(db: Session, version: str, user_login: str) -> ModelVersion:
    """Сделать действующей прежнюю утверждённую версию."""
    return R.rollback(db, REG, version, user_login)
