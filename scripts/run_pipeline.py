"""Проверка объекта из папок на диске без интерфейса: создаёт процесс, кладёт файлы в хранилище, гоняет конвейер
синхронно (как worker) и печатает итог. Для предобработки объектов стенда и для замеров.

usage (из service/):
  .venv/bin/python scripts/run_pipeline.py --object-id OBJ-X --name "Объект" --pd DIR_OR_FILE... --rd ... --id ... \
      [--auto ...] [--manifest document_manifest.jsonl]
Переменные окружения — как у сервиса (DATA_DIR, DATABASE_URL, LLM_URL, LLM_MODEL, OCR_URL, OCR_MODEL, LLM_PROVIDER…).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from inspector import jobs  # noqa: E402
from inspector.config import get_settings  # noqa: E402
from inspector.db import init_engine, session_factory  # noqa: E402
from inspector.models import ConstructionObject, File, Job, Process  # noqa: E402
from inspector.pipeline.intake import store_local  # noqa: E402
from inspector.pipeline.runner import run_job  # noqa: E402
from inspector.protocol import build_protocol, build_submission, validate_submission  # noqa: E402
from inspector.seed import seed_all  # noqa: E402


def iter_files(paths: list[str]):
    for p in map(Path, paths):
        if p.is_file():
            yield p, p.name
        elif p.is_dir():
            for f in sorted(x for x in p.rglob("*") if x.is_file()):
                yield f, str(f.relative_to(p.parent))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--object-id", required=True)
    ap.add_argument("--name", default="")
    ap.add_argument("--address", default="")
    for st in ("pd", "rd", "id", "auto"):
        ap.add_argument(f"--{st}", nargs="*", default=[])
    ap.add_argument("--manifest", default="")
    ap.add_argument("--process-id", default="", help="дозагрузка в существующий процесс (incremental)")
    ap.add_argument("--fake-llm", action="store_true", help="поддельная модель (tests/fake_llm.py): проверка масштаба без GPU")
    args = ap.parse_args()
    settings = get_settings()
    init_engine()
    db = session_factory()()
    seed_all(db, settings)
    if args.process_id:
        process = db.get(Process, args.process_id)
        process.batch += 1
        kind = "incremental"
    else:
        obj = db.scalars(select(ConstructionObject).where(ConstructionObject.object_code == args.object_id)).first()
        if obj is None:
            obj = ConstructionObject(object_code=args.object_id, name=args.name or args.object_id, address=args.address)
            db.add(obj)
            db.flush()
        process = Process(object_pk=obj.id)
        db.add(process)
        db.flush()
        kind = "full"
    n = 0
    groups = [("PD", args.pd), ("RD", args.rd), ("ID", args.id), (None, args.auto)]
    if args.manifest:
        groups.append((None, [args.manifest]))
    for stage, paths in groups:
        for path, rel in iter_files(paths):
            sha, size, stored = store_local(path, settings.data_dir, path.suffix.lower())
            db.add(File(process_id=process.id, file_id="LOCAL-" + sha[:8], original_name=path.name, rel_path=rel,
                        ext=path.suffix.lower(), size=size, sha256=sha, storage_path=stored, declared_stage=stage,
                        doc_stage=stage, stage_source="declared" if stage else None, status="pending",
                        batch=process.batch))
            n += 1
    job = jobs.enqueue(db, process, kind)
    db.commit()
    jobs.claim_next(db)
    print(json.dumps({"process_id": process.id, "files": n, "kind": kind, "llm": settings.llm_model,
                      "ocr": settings.ocr_model_eff, "provider": settings.llm_provider}, ensure_ascii=False), flush=True)
    t0 = time.time()
    transport = None
    if args.fake_llm:
        from tests.fake_llm import FakeLLM
        transport = FakeLLM(ocr_text="Поддельный OCR: текст скана").transport
    error = run_job(job.id, llm_transport=transport, ocr_transport=transport)
    db.expire_all()
    jobs.finish(db, db.get(Job, job.id), error)
    process = db.get(Process, process.id)
    proto = build_protocol(db, process)
    sub = build_submission(proto)
    out = settings.data_dir / "processes" / process.id
    out.mkdir(parents=True, exist_ok=True)
    (out / "submission.json").write_text(json.dumps(sub, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({"status": process.status, "error": process.error, "seconds": round(time.time() - t0, 1),
                      "scenario": process.scenario, "summary": proto["summary"],
                      "findings": proto["findings_summary"], "schema_errors": validate_submission(sub)[:5],
                      "stages": {k: {"s": v.get("seconds"), **(v.get("stats") or {})}
                                 for k, v in (process.progress or {}).items()}},
                     ensure_ascii=False, indent=1), flush=True)
    return 0 if not error else 1


if __name__ == "__main__":
    sys.exit(main())
