"""Дообучение отбора страниц F40 на решениях экспертов (inspector/ml/page_router.py, api/ml.py): решение по кандидату
даёт метки страниц; итерация дообучает от исходной модели только параметры с метками; новая версия ждёт утверждения,
после него этап отбора и строка моделей прогона берут её; отклонение и откат. Исходная модель в тестах — маленькая,
собранная на текстах страниц примера (тот же формат артефакта, что resources/router)."""
from __future__ import annotations

import numpy as np
import pytest
from fastapi.testclient import TestClient
from scipy.sparse import csr_matrix
from sklearn.feature_extraction.text import TfidfVectorizer
from sqlalchemy import select

from inspector import config as config_module
from inspector import statuses as S
from inspector.ml import page_router as PR
from inspector.models import Extraction, File, Finding, ModelVersion, Page
from inspector.pipeline import router as RT

TEXTS = {("PD", 13): "Арматура форшахты класса А400, защитный слой 50 мм",
         ("PD", 15): "Толщина плит перекрытий 220 мм, бетон В30",
         ("PD", 20): "Экспликация помещений, площадь 12,4",
         ("PD", 3): "Ведомость рабочих чертежей основного комплекта",
         ("PD", 4): "Ведомость ссылочных и прилагаемых документов",
         ("PD", 5): "Ведомость спецификаций и ведомость расхода",
         ("RD", 2): "Арматура форшахты Ø12 А240 шаг 200",
         ("RD", 7): "Плита П1 δ=200, ведомость деталей",
         ("RD", 1): "Ведомость рабочих чертежей марки КЖ",
         ("ID", 2): "Акт: арматура класса А240С форшахты",
         ("ID", 1): "Акт освидетельствования скрытых работ, ведомость"}


def page_texts(db, process_id) -> dict:
    files = {f.doc_stage: f for f in db.scalars(select(File).where(File.process_id == process_id, File.status == "ok"))}
    for (stage, n), text in TEXTS.items():
        p = db.scalars(select(Page).where(Page.file_pk == files[stage].id, Page.page_no == n)).one()
        p.text = text
    for p in db.scalars(select(Page).where(Page.file_pk.in_([f.id for f in files.values()]), Page.text == "")):
        p.text = f"Лист {p.page_no}: общие указания"
    db.flush()
    return files


def base_router(path, db, process_id) -> None:
    """KR-057 и KR-059: модель ставит наверх страницы со словом «ведомость», а не страницы значений."""
    texts = [p.text for p in db.scalars(select(Page).join(File).where(File.process_id == process_id))]
    metas = [RT.meta_tokens(st, None, [], None, "x") for st in ("PD", "RD", "ID")]
    wv = TfidfVectorizer(**RT.WORD_PARAMS, dtype=np.float32).fit(texts)
    cv = TfidfVectorizer(**RT.CHAR_PARAMS, dtype=np.float32).fit(texts)
    mv = TfidfVectorizer(**RT.META_PARAMS, dtype=np.float32).fit(metas)
    n = len(wv.vocabulary_) + len(cv.vocabulary_) + len(mv.vocabulary_)
    coef = np.zeros((2, n), dtype=np.float32)
    coef[:, wv.vocabulary_["ведомость"]] = 5.0
    vocab = {name: [t for t, _ in sorted(v.vocabulary_.items(), key=lambda x: x[1])]
             for name, v in (("words", wv), ("chars", cv), ("meta", mv))}
    RT.save_router(path, vocab, {"words": wv.idf_, "chars": cv.idf_, "meta": mv.idf_}, csr_matrix(coef),
                   np.array([-1.0, -1.0]), ["KR-057", "KR-059"], {"name": "test-router", "keep_per_param": 5000,
                                                                  "n_pages": 53, "n_models": 2})


@pytest.fixture()
def fused(settings, monkeypatch):
    monkeypatch.setenv("SELECT_MODE", "fused")
    monkeypatch.setattr(RT, "BASE_K", 3)                    # в примере мало страниц: «первые 3» вместо 40
    config_module.get_settings.cache_clear()
    RT.load_router.cache_clear()
    yield config_module.get_settings()
    RT.load_router.cache_clear()


@pytest.fixture()
def world(fused, db, example_process, tmp_path, monkeypatch):
    files = page_texts(db, example_process.id)
    base = tmp_path / "router-base"
    base_router(base, db, example_process.id)
    monkeypatch.setattr(RT, "ROUTER_DIR", base)
    db.commit()
    return example_process, files, base


def finding(db, process_id, code) -> Finding:
    return db.scalars(select(Finding).where(Finding.process_id == process_id, Finding.param_code == code)).one()


def decide(db, process_id, code, status, reason=None):
    f = finding(db, process_id, code)
    f.inspector_status, f.reason_code = status, reason
    db.commit()


def test_page_verdict_by_decision_and_reason():
    assert PR.page_verdict(S.CONFIRMED_VIOLATION, None) is True
    assert PR.page_verdict(S.NEGATIVE_VERIFIED, "APPROVED_CHANGE") is True
    assert PR.page_verdict(S.NEGATIVE_VERIFIED, "OCR_ERROR") is True
    assert PR.page_verdict(S.NEGATIVE_VERIFIED, "EXTRACTION_ERROR") is False
    assert PR.page_verdict(S.NEGATIVE_VERIFIED, "NOT_APPLICABLE") is None
    assert PR.page_verdict(S.CLARIFICATION_REQUIRED, None) is None
    assert PR.page_verdict("PENDING", None) is None


def test_labels_from_decisions(world, db):
    process, files, _ = world
    decide(db, process.id, "KR-057", S.CONFIRMED_VIOLATION)
    decide(db, process.id, "KR-059", S.NEGATIVE_VERIFIED, "EXTRACTION_ERROR")
    lab = PR.collect_labels(db)
    pd, rd, idf = files["PD"].id, files["RD"].id, files["ID"].id
    assert lab.pages[(process.id, "KR-057")] == {(pd, 13): True, (rd, 2): True, (idf, 2): True}
    assert lab.pages[(process.id, "KR-059")] == {(pd, 15): False, (rd, 7): False}
    assert lab.counts() == {"decisions": 2, "positive": 3, "negative": 2, "params": 2, "processes": 1}
    before = lab.sha256()
    decide(db, process.id, "KR-059", S.NEGATIVE_VERIFIED, "NOT_APPLICABLE")      # решение не про страницы
    lab = PR.collect_labels(db)
    assert (process.id, "KR-059") not in lab.pages and lab.sha256() != before


def test_pages_with_undecided_values_are_left_out(world, db):
    process, files, base = world
    decide(db, process.id, "KR-057", S.CONFIRMED_VIOLATION)
    db.add(Extraction(process_id=process.id, param_code="KR-057", stage="PD", file_pk=files["PD"].id, page_no=3,
                      value_raw="А500", supported=True))
    db.commit()
    data = PR.build_data(db, RT.load_router(str(base)), PR.collect_labels(db))
    g = {grp.process_id: grp for grp in data.groups["KR-057"]}[process.id]
    pages = [p for p in db.scalars(select(Page).where(Page.file_pk.in_([f.id for f in files.values()]))
                                   .order_by(Page.file_pk, Page.page_no))]
    kept = {(pages[i].file_pk, pages[i].page_no) for i in g.rows}
    assert (files["PD"].id, 3) not in kept and (files["PD"].id, 13) in kept and (files["PD"].id, 4) in kept
    assert int(g.y.sum()) == 3 and len(g.positive) == 3


def test_adapt_moves_from_prior_toward_labels():
    rng = np.random.default_rng(0)
    X = csr_matrix((rng.random((40, 30)) < 0.2).astype(np.float32))
    y = np.asarray(X[:, 0].todense()).ravel() > 0
    w0 = np.zeros(30)
    w0[1] = 3.0
    w, b = PR.adapt(X, y, w0, -1.0)
    assert w[0] > 1.0 and abs(w[1] - 3.0) < abs(w[0])          # признак меток вырос, чужой вес остался около априорного
    w_same, b_same = PR.adapt(X, y, w0, -1.0, c=0.0)           # без данных — ровно исходные веса
    assert np.allclose(w_same, w0) and b_same == pytest.approx(-1.0)


def test_prune_row_keeps_largest():
    assert PR.prune_row(np.array([0.1, -5.0, 2.0, 0.0]), 2).tolist() == [0.0, -5.0, 2.0, 0.0]


@pytest.fixture()
def client(world):
    from inspector.api.app import create_app

    with TestClient(create_app()) as c:
        yield c


def login(client, user: str) -> dict:
    r = client.post("/api/v1/auth/login", json={"login": user, "password": user})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def iteration(client, db) -> dict:
    h = login(client, "ml")
    assert client.post("/api/v1/ml/page-router/retrain", headers=h).status_code == 202
    assert PR.run_pending(db, config_module.get_settings()) == 1
    return client.get("/api/v1/ml/page-router", headers=h).json()


def test_inspector_cannot_retrain_router(client):
    assert client.post("/api/v1/ml/page-router/retrain", headers=login(client, "inspector")).status_code == 403


def test_no_decisions_retrain_refused_at_once(client, db):
    h = login(client, "ml")
    r = client.post("/api/v1/ml/page-router/retrain", headers=h)
    assert r.status_code == 409 and "решений экспертов по кандидатам нет" in r.json()["error"]["message"]
    st = client.get("/api/v1/ml/page-router", headers=h).json()
    assert st["log"] == [] and st["current"].startswith("page-router:router@") and len(st["versions"]) == 1


def test_retrain_approve_select_uses_new_version_then_rollback(client, db, world):
    from inspector.protocol import model_version

    process, files, base = world
    h = login(client, "ml")
    st = client.get("/api/v1/ml/page-router", headers=h).json()
    base_version = st["current"]
    assert st["labels"]["positive"] == 0 and st["labels"]["new"] is False
    decide(db, process.id, "KR-057", S.CONFIRMED_VIOLATION)
    decide(db, process.id, "KR-059", S.NEGATIVE_VERIFIED, "EXTRACTION_ERROR")
    assert client.get("/api/v1/ml/page-router", headers=h).json()["labels"]["new"] is True

    st = iteration(client, db)
    it = st["log"][0]
    assert it["status"] == "ACCEPTED" and it["model_kind"] == "page-router" and it["n_corrections"] == 5
    before, after = it["metrics_before"]["labels"], it["metrics_after"]["labels"]
    assert before["found"]["ok"] < after["found"]["ok"] == 3            # страницы значений поднялись в первые 3
    assert after["dropped"]["n"] == 2
    assert st["labels"]["new"] is False and st["current"] == base_version
    new = it["new_version"]
    assert [v["approval_status"] for v in st["versions"] if v["model_version"] == new] == ["DRAFT"]

    old, fresh = RT.load_router(str(base)), RT.load_router(it["artifact"])
    assert fresh.codes == old.codes and fresh.manifest["parent_version"] == old.version
    assert sorted(fresh.manifest["adaptation"]["updated_params"]) == ["KR-057", "KR-059"]

    r = client.post("/api/v1/ml/page-router/approve", headers=h, json={"version": new})
    assert r.status_code == 200 and r.json()["rollback_to"] == base_version
    assert client.get("/api/v1/ml/page-router", headers=h).json()["current"] == new
    assert f"select=f{config_module.get_settings().top_k_pages}:{new.split(':', 1)[1]}" in model_version(db)
    assert PR.current_model(db, config_module.get_settings()).version == fresh.version

    r = client.post("/api/v1/ml/page-router/rollback", headers=h, json={"version": base_version})
    assert r.status_code == 200
    st = client.get("/api/v1/ml/page-router", headers=h).json()
    assert st["current"] == base_version
    assert [x["status"] for x in st["log"]][:3] == ["ROLLBACK", "APPROVED", "ACCEPTED"]
    cls_log = client.get("/api/v1/ml/page-classifier", headers=h).json()["log"]
    assert all(x["model_kind"] == "page-classifier" for x in cls_log)       # журналы моделей не смешиваются


def test_untouched_params_keep_weights_and_new_param_gets_model(client, db, world):
    process, files, base = world
    decide(db, process.id, "KR-057", S.CONFIRMED_VIOLATION)
    f = finding(db, process.id, "KR-059")          # у KR-059 ни одного решения; значения — новый параметр AR-001
    f.param_code, f.inspector_status, f.reason_code = "AR-001", S.NEGATIVE_VERIFIED, "APPROVED_CHANGE"
    db.commit()
    # у AR-001 в карточке 2 страницы — меньше MIN_POS: своей модели не будет
    it = iteration(client, db)["log"][0]
    fresh = RT.load_router(it["artifact"])
    old = RT.load_router(str(base))
    assert fresh.codes == ["KR-057", "KR-059"]
    assert (fresh.coef[1] != old.coef[1]).nnz == 0 and fresh.intercept[1] == pytest.approx(old.intercept[1])
    assert fresh.manifest["adaptation"]["new_params"] == []

    kr = finding(db, process.id, "KR-058")        # третья страница «есть» у AR-001 -> модель появляется
    kr.param_code, kr.status, kr.inspector_status = "AR-001", S.CANDIDATE, S.CONFIRMED_VIOLATION
    db.commit()
    it = iteration(client, db)["log"][0]
    fresh = RT.load_router(it["artifact"])
    assert fresh.codes == ["KR-057", "KR-059", "AR-001"] and fresh.manifest["adaptation"]["new_params"] == ["AR-001"]


def test_reject_draft_keeps_current(client, db, world):
    process, _, _ = world
    decide(db, process.id, "KR-057", S.CONFIRMED_VIOLATION)
    st = iteration(client, db)
    new, cur = st["log"][0]["new_version"], st["current"]
    h = login(client, "ml")
    assert client.post("/api/v1/ml/page-router/reject", headers=h, json={"version": new}).status_code == 200
    assert client.post("/api/v1/ml/page-router/approve", headers=h, json={"version": new}).status_code == 409
    st = client.get("/api/v1/ml/page-router", headers=h).json()
    assert st["current"] == cur
    assert db.scalars(select(ModelVersion).where(ModelVersion.model_version == new)).one().approval_status == "REJECTED"


def test_worker_runs_router_iterations(world, db, monkeypatch):
    from inspector import worker
    from inspector.ml import registry as R

    process, _, _ = world
    decide(db, process.id, "KR-057", S.CONFIRMED_VIOLATION)
    row = PR.request_retrain(db, "ml")
    db.commit()
    assert PR.request_retrain(db, "ml").id == row.id                 # одна итерация за раз
    monkeypatch.setattr(worker, "session_scope", lambda: _Scope(db))
    assert worker.retrain_pending() == 1
    db.expire_all()
    assert R.log_rows(db, PR.REG)[0].status == "ACCEPTED"


class _Scope:
    def __init__(self, db):
        self.db = db

    def __enter__(self):
        return self.db

    def __exit__(self, *exc):
        self.db.commit()
        return False


def test_status_reads_without_writing_registry(client, db):
    """Два одновременных открытия вкладки не спорят за запись исходной версии: чтение реестр не меняет."""
    h = login(client, "ml")
    for _ in range(2):
        st = client.get("/api/v1/ml/page-router", headers=h).json()
        assert st["current"].startswith("page-router:router@") and st["versions"][0]["is_current"]
    assert db.scalars(select(ModelVersion).where(ModelVersion.model_version.like("page-router:%"))).first() is None
