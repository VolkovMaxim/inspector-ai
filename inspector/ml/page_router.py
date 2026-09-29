"""Отбор страниц F40 (pipeline/router.py), который дообучается на решениях эксперта (ТЗ 9.4, Q&A №24).

Метка — страница значения из карточки находки (EvidenceFragment) по решению эксперта в рабочем месте:
  подтверждено (CONFIRMED_VIOLATION) или отклонено по причине, при которой значение на странице верное (есть
  согласованное изменение, ошибка OCR, не та локация, неактуальная редакция) -> «на странице есть значение параметра»;
  отклонено как «значение извлечено неверно» -> «на странице нет значения параметра».
У параметра с метками остальные страницы с текстом той же проверки и тех же стадий — «нет значения», кроме страниц, где
извлечение нашло значение этого параметра, а решения по нему нет: про них неизвестно, в обучение они не идут.

Итерация: исходная модель (resources/router) + все метки к этому моменту. Логистическая регрессия параметра с метками
дообучается от своих весов: штраф L2 тянет веса к исходным, а не к нулю (MAP-адаптация; Chelba, Acero. Adaptation of
Maximum Entropy Capitalizer: Little Data Can Help a Lot, 2004); словари и idf — исходные. Параметр без модели, у которого
страниц «есть» не меньше MIN_POS, получает новую модель; параметры без меток не меняются. До и после считается, сколько
страниц «есть» попадает в первые BASE_K страниц модели в своей проверке и стадии и сколько страниц «нет» из них выпадает.
Новая версия — каталог в data_dir/models/page-router; её утверждает или отклоняет ответственный, откат — к любой
утверждённой. Версии — model_versions «page-router:router@…», журнал — ml_retraining_log (model_kind page-router)."""
from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from inspector import statuses as S
from inspector.config import Settings
from inspector.ml import registry as R
from inspector.models import EvidenceFragment, Extraction, Finding, ModelVersion, Process, RetrainingLog, utcnow
from inspector.pipeline import router as RT

log = logging.getLogger("inspector.page_router")
KIND = "page-router"
REG = R.Kind(KIND, "page-router", "expert-decisions")
RouterError = R.ModelError
POSITIVE_REASONS = frozenset({"APPROVED_CHANGE", "OCR_ERROR", "BINDING_ERROR", "ACTUAL_REVISION_WRONG"})
NEGATIVE_REASONS = frozenset({"EXTRACTION_ERROR"})
C, MIN_POS, MAX_ITER = 4.0, 3, 300          # C и MIN_POS — как в исходном обучении
STAGES = ("PD", "RD", "ID")


def page_verdict(inspector_status: str | None, reason_code: str | None) -> bool | None:
    """Решение эксперта -> есть ли на страницах карточки значение параметра (None — решение об этом не говорит)."""
    if inspector_status == S.CONFIRMED_VIOLATION:
        return True
    if inspector_status == S.NEGATIVE_VERIFIED:
        if reason_code in POSITIVE_REASONS:
            return True
        if reason_code in NEGATIVE_REASONS:
            return False
    return None


@dataclass
class Labels:
    pages: dict[tuple[str, str], dict[tuple[int, int], bool]] = field(default_factory=dict)  # (проверка, код) -> стр.
    decisions: int = 0

    def counts(self) -> dict:
        vals = [v for d in self.pages.values() for v in d.values()]
        return {"decisions": self.decisions, "positive": sum(vals), "negative": len(vals) - sum(vals),
                "params": len({c for _, c in self.pages}), "processes": len({p for p, _ in self.pages})}

    def sha256(self) -> str:
        key = sorted((p, c, f, n, v) for (p, c), d in self.pages.items() for (f, n), v in d.items())
        return hashlib.sha256(json.dumps(key, ensure_ascii=False).encode("utf-8")).hexdigest()


def collect_labels(db: Session) -> Labels:
    """Метки из решений эксперта по актуальным карточкам матрицы; «есть» у той же страницы побеждает «нет»."""
    rows = db.execute(select(Finding, EvidenceFragment).join(EvidenceFragment, EvidenceFragment.finding_pk == Finding.id)
                      .where(Finding.active.is_(True), Finding.source == "matrix",
                             Finding.inspector_status.in_((S.CONFIRMED_VIOLATION, S.NEGATIVE_VERIFIED)))
                      .order_by(Finding.id, EvidenceFragment.id)).all()
    out, seen = Labels(), set()
    for f, ev in rows:
        v = page_verdict(f.inspector_status, f.reason_code)
        if v is None or ev.page_no < 1:
            continue
        seen.add(f.id)
        d = out.pages.setdefault((f.process_id, f.param_code), {})
        d[(ev.file_pk, ev.page_no)] = d.get((ev.file_pk, ev.page_no), False) or v
    out.decisions = len(seen)
    return out


# ------------------------------------------------------------------------------------------------ версии
def read_manifest(model_dir: str | Path) -> dict:
    return json.loads((Path(model_dir) / "manifest.json").read_text(encoding="utf-8"))


def base_dir() -> Path:
    """Исходная модель — артефакт в образе (resources/router); от неё идёт каждая итерация."""
    return Path(RT.ROUTER_DIR)


def deployed(db: Session) -> ModelVersion | None:
    return R.deployed(db, REG)


def active_dir(db: Session) -> Path | None:
    """Каталог действующей версии: утверждённая опубликованная, пока её каталог на месте; иначе исходная модель.
    Чтение ничего не пишет в реестр (одновременные запросы не спорят за запись исходной версии)."""
    mv = deployed(db)
    if mv is not None and (Path(mv.artifact) / "manifest.json").exists():
        return Path(mv.artifact)
    base = base_dir()
    return base if (base / "manifest.json").exists() else None


def base_name() -> str | None:
    base = base_dir()
    if not (base / "manifest.json").exists():
        return None
    return R.version_name(REG, read_manifest(base).get("version") or "router")


def current_name(db: Session) -> str | None:
    mv = deployed(db)
    if mv is not None and (Path(mv.artifact) / "manifest.json").exists():
        return mv.model_version
    return base_name()


def ensure_base(db: Session) -> None:
    """Исходная модель — в реестр опубликованной версией, если действующей ещё нет: после утверждения новой к ней
    можно откатиться."""
    base = base_dir()
    if deployed(db) is None and (base / "manifest.json").exists():
        R.register(db, REG, read_manifest(base).get("version") or "router", str(base), "APPROVED", deploy=True,
                   approved_by="system")


def versions(db: Session) -> list[dict]:
    """Версии реестра; пока ни одна не утверждена, действующая — исходная модель (строкой в начале списка)."""
    rows = R.versions(db, REG)
    name = base_name()
    if deployed(db) is None and name and name not in {v["model_version"] for v in rows}:
        rows.append({"model_version": name, "approval_status": "APPROVED", "approved_by": "system",
                     "deployed_at": None, "rollback_to": None, "metrics": {}, "artifact": str(base_dir()),
                     "is_current": True})
    return rows


def current_model(db: Session, settings: Settings) -> RT.RouterModel | None:
    """Модель отбора для этапа select: действующая версия; None — отбор только по словам."""
    if settings.select_mode != "fused":
        return None
    d = active_dir(db)
    return RT.load_router(str(d)) if d is not None else None


# ------------------------------------------------------------------------------------------------ данные
@dataclass
class Group:
    """Метки одного параметра в одной проверке: строки матрицы признаков проверки и ответы."""
    process_id: str
    rows: np.ndarray                 # индексы страниц проверки, идущих в обучение
    y: np.ndarray                    # есть значение (bool)
    positive: list[int]              # страницы «есть» и «нет» от решений — для метрик
    negative: list[int]


@dataclass
class TrainData:
    X: dict[str, object] = field(default_factory=dict)                 # проверка -> csr [страницы × признаки]
    stage_rows: dict[str, dict[str, np.ndarray]] = field(default_factory=dict)   # проверка -> стадия -> строки
    stage_of: dict[str, list[str]] = field(default_factory=dict)
    groups: dict[str, list[Group]] = field(default_factory=dict)       # код -> группы


def build_data(db: Session, model: RT.RouterModel, labels: Labels) -> TrainData:
    from inspector.pipeline.runner import _page_infos

    data = TrainData()
    by_process: dict[str, list[str]] = {}
    for pid, code in labels.pages:
        by_process.setdefault(pid, []).append(code)
    for pid, codes in sorted(by_process.items()):
        process = db.get(Process, pid)
        if process is None:
            continue
        pages = [p for p in _page_infos(db, process, None) if p.text and p.stage in STAGES]
        if not pages:
            continue
        data.X[pid] = model.features([p.text for p in pages],
                                     [RT.meta_tokens(p.stage, p.page_class, p.other_classes, p.section, p.text)
                                      for p in pages])
        data.stage_of[pid] = [p.stage for p in pages]
        data.stage_rows[pid] = {st: np.array([i for i, p in enumerate(pages) if p.stage == st], dtype=np.int64)
                                for st in STAGES}
        pos = {(p.file_pk, p.page_no): i for i, p in enumerate(pages)}
        unknown: dict[str, set] = {}
        for code, fpk, pno in db.execute(select(Extraction.param_code, Extraction.file_pk, Extraction.page_no).where(
                Extraction.process_id == pid, Extraction.param_code.in_(codes), Extraction.supported.is_(True))).all():
            unknown.setdefault(code, set()).add((fpk, pno))
        for code in sorted(codes):
            lab = {pos[k]: v for k, v in labels.pages[(pid, code)].items() if k in pos}
            if not lab:
                continue
            stages = {pages[i].stage for i in lab}
            skip = {pos[k] for k in unknown.get(code, ()) if k in pos} - set(lab)
            rows = np.array([i for i, p in enumerate(pages) if p.stage in stages and i not in skip], dtype=np.int64)
            y = np.array([lab.get(int(i), False) for i in rows], dtype=bool)
            data.groups.setdefault(code, []).append(Group(pid, rows, y, sorted(i for i, v in lab.items() if v),
                                                          sorted(i for i, v in lab.items() if not v)))
    return data


# ------------------------------------------------------------------------------------------------ обучение
def adapt(X, y: np.ndarray, w0: np.ndarray, b0: float, c: float = C, max_iter: int = MAX_ITER) -> tuple[np.ndarray, float]:
    """Логистическая регрессия от весов w0, b0: min C·Σ вес·log(1 + e^(−s·z)) + ½‖w − w0‖² + ½(b − b0)²; веса классов —
    «balanced», как в исходном обучении. X — csr [n × признаки], y — bool."""
    from scipy.optimize import minimize
    from scipy.special import expit

    n, npos = len(y), int(y.sum())
    nneg = n - npos
    both = npos > 0 and nneg > 0
    cw = np.where(y, n / (2 * npos) if both else 1.0, n / (2 * nneg) if both else 1.0)
    s = np.where(y, 1.0, -1.0)
    z0 = np.asarray(X @ w0, dtype=np.float64) + b0
    Xt = X.T.tocsr()

    def f(theta: np.ndarray):
        d, db_ = theta[:-1], theta[-1]
        m = s * (z0 + X @ d + db_)
        obj = c * float(np.sum(cw * np.logaddexp(0.0, -m))) + 0.5 * float(d @ d + db_ * db_)
        g = -c * cw * s * expit(-m)
        return obj, np.concatenate([Xt @ g + d, [g.sum() + db_]])

    res = minimize(f, np.zeros(X.shape[1] + 1), jac=True, method="L-BFGS-B", options={"maxiter": max_iter})
    return w0 + res.x[:-1], float(b0 + res.x[-1])


def prune_row(w: np.ndarray, keep: int) -> np.ndarray:
    """Оставить keep весов с наибольшим модулем (как отсечение исходного артефакта)."""
    if keep <= 0 or np.count_nonzero(w) <= keep:
        return w
    out = np.zeros_like(w)
    idx = np.argpartition(-np.abs(w), keep - 1)[:keep]
    out[idx] = w[idx]
    return out


def fit(model: RT.RouterModel, data: TrainData, keep: int) -> tuple[object, np.ndarray, list[str], dict]:
    """-> новые coef (csr), intercept, коды, {updated: [...], new: [...]}."""
    from scipy.sparse import csr_matrix, vstack

    col = {c: j for j, c in enumerate(model.codes)}
    rows = [model.coef[j] for j in range(len(model.codes))]
    intercept = [float(b) for b in model.intercept]
    codes, updated, new = list(model.codes), [], []
    for code in sorted(data.groups):
        gs = data.groups[code]
        X = vstack([data.X[g.process_id][g.rows] for g in gs]).tocsr()
        y = np.concatenate([g.y for g in gs])
        if code in col:
            j = col[code]
            w0 = np.asarray(model.coef[j].todense(), dtype=np.float64).ravel()
            w, b = adapt(X, y, w0, intercept[j])
            rows[j], intercept[j] = csr_matrix(prune_row(w, keep).astype(np.float32)), b
            updated.append(code)
        elif int(y.sum()) >= MIN_POS:
            w, b = adapt(X, y, np.zeros(X.shape[1]), 0.0)
            rows.append(csr_matrix(prune_row(w, keep).astype(np.float32)))
            intercept.append(b)
            codes.append(code)
            new.append(code)
    return vstack(rows).tocsr(), np.array(intercept, dtype=np.float32), codes, {"updated": updated, "new": new}


def evaluate(data: TrainData, coef, intercept: np.ndarray, codes: list[str]) -> dict:
    """Страницы «есть» в первых BASE_K страницах модели своей проверки и стадии; страницы «нет» вне их. Параметр без
    модели — список модели пуст: все его «есть» не найдены, все «нет» отсеяны."""
    col = {c: j for j, c in enumerate(codes)}
    found = [0, 0]
    dropped = [0, 0]
    for code, gs in data.groups.items():
        j = col.get(code)
        for g in gs:
            X = data.X[g.process_id]
            s = np.asarray((X @ coef[j].T).todense()).ravel() + intercept[j] if j is not None else None
            stage_of = data.stage_of[g.process_id]
            top: set[int] = set()
            if s is not None:
                for st in {stage_of[i] for i in g.positive + g.negative}:
                    idx = data.stage_rows[g.process_id][st]
                    top |= {int(i) for i in idx[np.argsort(-s[idx], kind="stable")[:RT.BASE_K]]}
            found[0] += sum(1 for i in g.positive if i in top)
            found[1] += len(g.positive)
            dropped[0] += sum(1 for i in g.negative if i not in top)
            dropped[1] += len(g.negative)

    def part(ok_n: list[int]) -> dict:
        return {"ok": ok_n[0], "n": ok_n[1], "acc": round(ok_n[0] / ok_n[1], 4) if ok_n[1] else 0.0}
    return {"found": part(found), "dropped": part(dropped)}


def build_version(base: Path, out: Path, coef, intercept: np.ndarray, codes: list[str], manifest: dict) -> dict:
    vocab = json.loads((base / "vocab.json").read_text(encoding="utf-8"))
    idf = np.load(base / "idf.npz")
    return RT.save_router(out, {k: vocab[k] for k in ("words", "chars", "meta")},
                          {k: idf[k] for k in ("words", "chars", "meta")}, coef, intercept, codes, manifest)


# ------------------------------------------------------------------------------------------------ итерация
def request_retrain(db: Session, requested_by: str) -> RetrainingLog:
    """В очередь — только когда есть решения экспертов по загруженным проверкам; иначе RouterError сразу."""
    if not collect_labels(db).pages:
        raise RouterError("дообучение недоступно: решений экспертов по кандидатам нет — загрузите пакет, дождитесь "
                          "протокола и примите решения в рабочем месте")
    return R.request(db, REG, requested_by)


def run_pending(db: Session, settings: Settings) -> int:
    rows = R.queued(db, REG)
    for row in rows:
        run_retrain(db, settings, row)
    return len(rows)


def fail_interrupted(db: Session) -> int:
    return R.fail_interrupted(db, REG)


def last_trained_sha(db: Session) -> str | None:
    """SHA-256 меток последней итерации, дошедшей до новой версии."""
    row = db.scalars(select(RetrainingLog).where(RetrainingLog.model_kind == REG.log_kind,
                                                 RetrainingLog.status.in_(("ACCEPTED", "REJECTED", "DRAFT")))
                     .order_by(RetrainingLog.id.desc())).first()
    return row.corrections_sha256 if row is not None else None


def run_retrain(db: Session, settings: Settings, row: RetrainingLog) -> RetrainingLog:
    """Одна итерация: все метки экспертов -> дообучение от исходной модели -> метрики до/после -> правило приёмки ->
    черновик, ждущий утверждения ответственным (или отказ приёмки)."""
    started = time.time()
    row.status = "RUNNING"
    db.commit()
    try:
        base = base_dir()
        if not (base / "manifest.json").exists():
            raise RouterError(f"нет исходной модели отбора ({base})")
        labels = collect_labels(db)
        if not labels.pages:
            raise RouterError("нет решений экспертов по кандидатам — учить нечему")
        model = RT.load_router(str(base))
        data = build_data(db, model, labels)
        if not data.groups:
            raise RouterError("страницы из решений экспертов не найдены среди страниц с текстом")
        base_man = model.manifest
        keep = int(base_man.get("keep_per_param") or 20000)
        before = evaluate(data, model.coef, model.intercept, model.codes)
        coef, intercept, codes, changed = fit(model, data, keep)
        after = evaluate(data, coef, intercept, codes)
        counts = labels.counts()
        man = {k: v for k, v in base_man.items() if k not in ("version", "files")}
        man.update(parent_version=base_man.get("version"), created=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                   expert_labels={**counts, "sha256": labels.sha256(),
                                  "rule": "подтверждено или отклонено с верным значением -> есть; «значение извлечено "
                                          "неверно» -> нет; остальные страницы той же проверки и стадии -> нет"},
                   adaptation={"from": base_man.get("version"), "c": C, "min_pos": MIN_POS, "max_iter": MAX_ITER,
                               "updated_params": changed["updated"], "new_params": changed["new"]},
                   n_models=len(codes))
        out_dir = settings.data_dir / "models" / KIND / f"{time.strftime('%Y%m%d-%H%M%S')}-{row.id}"
        man = build_version(base, out_dir, coef, intercept, codes, man)
        version = man["version"]
        if R.taken(db, REG, version):
            version = f"{version}+it{row.id}"
            man["version"] = version
            (out_dir / "manifest.json").write_text(json.dumps(man, ensure_ascii=False, indent=1), encoding="utf-8")
        row.base_version = R.version_name(REG, base_man.get("version") or "router")
        row.new_version, row.artifact = R.version_name(REG, version), str(out_dir)
        row.n_corrections, row.corrections_sha256 = counts["positive"] + counts["negative"], labels.sha256()
        row.metrics_before, row.metrics_after = {"labels": before}, {"labels": after}
        drop = settings.retrain_max_drop
        ok = after["found"]["acc"] >= before["found"]["acc"] - drop
        row.rule = (f"страниц со значением в первых {RT.BASE_K} страницах модели не меньше прежнего больше чем на "
                    f"{100 * drop:.1f} п. п.; " + ("прошла — ждёт утверждения ответственным" if ok
                                                   else "не прошла — публиковать нельзя"))
        row.status = "ACCEPTED" if ok else "REJECTED"
        R.register(db, REG, version, str(out_dir), "DRAFT" if ok else "REJECTED", deploy=False,
                   metrics={"labels": after, "labels_before": before})
    except Exception as exc:  # noqa: BLE001 — итерация не удалась: запись в журнале, модель прежняя
        db.rollback()
        row = db.get(RetrainingLog, row.id)
        row.status, row.error = "FAILED", f"{type(exc).__name__}: {exc}"[:1000]
        log.error("router retrain failed", extra={"error": row.error})
    row.seconds, row.finished_at = round(time.time() - started, 2), utcnow()
    db.commit()
    return row


def approve(db: Session, version: str, user_login: str) -> ModelVersion:
    ensure_base(db)
    return R.approve(db, REG, version, user_login)


def reject(db: Session, version: str, user_login: str) -> ModelVersion:
    return R.reject(db, REG, version, user_login)


def rollback(db: Session, version: str, user_login: str) -> ModelVersion:
    return R.rollback(db, REG, version, user_login)
