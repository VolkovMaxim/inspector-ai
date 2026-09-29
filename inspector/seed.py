"""Начальное заполнение: матрица 132 параметров, демо-учётки по ролям, реестр текущей версии моделей."""
from __future__ import annotations

import json

import bcrypt
from sqlalchemy import select
from sqlalchemy.orm import Session

from inspector.config import RESOURCES, Settings
from inspector.models import ModelVersion, Param, User
from inspector.statuses import review_priority

SECTION_CODES = {"PZ": "ПЗ", "SPZU": "СПЗУ", "AR": "АР", "KR": "КР", "IOS1": "ИОС1", "IOS2": "ИОС2", "IOS3": "ИОС3",
                 "IOS4": "ИОС4", "IOS5": "ИОС5", "POS": "ПОС", "POD": "ПОД", "OOS": "ООС", "PPM": "ППМ", "ODI": "ОДИ",
                 "ZU": "ЗУ", "SM": "СМ", "TX": "ТХ", "TH": "ТХ", "PB": "ППМ"}
NON_NUMERIC_UNITS = {"—", "-", "", "Статус", "Буква", None}


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=10)).decode()


def check_password(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode(), hashed.encode())
    except ValueError:
        return False


def load_catalog() -> list[dict]:
    return [json.loads(l) for l in open(RESOURCES / "parameter_catalog_132.jsonl", encoding="utf-8") if l.strip()]


def section_of(code: str) -> str:
    return SECTION_CODES.get(code.split("-")[0], code.split("-")[0])


def seed_params(session: Session) -> int:
    existing = {p.code for p in session.scalars(select(Param))}
    added = 0
    for p in load_catalog():
        if p["parameter_code"] in existing:
            continue
        session.add(Param(
            code=p["parameter_code"], section=section_of(p["parameter_code"]), parameter_name=p["parameter_name"],
            unit=p.get("unit") or "", source_pd=p.get("source_pd") or "", source_rd=p.get("source_rd") or "",
            source_id=p.get("source_id") or "", trigger_logic=p.get("trigger") or "",
            criticality=p.get("criticality") or "", review_priority=review_priority(p.get("criticality")),
            data_type="string" if p.get("unit") in NON_NUMERIC_UNITS else "number"))
        added += 1
    session.flush()
    return added


def seed_users(session: Session, settings: Settings) -> int:
    wanted = [("inspector", settings.inspector_password, "inspector", "Инспектор (демо)"),
              ("supervisor", settings.supervisor_password, "supervisor", "Супервизор (демо)"),
              ("admin", settings.admin_password, "admin", "Администратор (демо)"),
              ("ml", settings.ml_password, "ml", "ML-инженер (демо)")]
    existing = {u.login for u in session.scalars(select(User))}
    added = 0
    for login, password, role, name in wanted:
        if login not in existing:
            session.add(User(login=login, password_hash=hash_password(password), role=role, full_name=name))
            added += 1
    session.flush()
    return added


def seed_model_version(session: Session, model_version: str) -> None:
    if session.scalars(select(ModelVersion).where(ModelVersion.model_version == model_version)).first() is None:
        session.add(ModelVersion(model_version=model_version, artifact="open weights (HF), см. docker-compose.yml",
                                 dataset_version="none", approval_status="APPROVED", approved_by="team"))
        session.flush()


def seed_all(session: Session, settings: Settings) -> None:
    from inspector.protocol import model_version

    seed_params(session)
    seed_users(session, settings)
    seed_model_version(session, model_version())
