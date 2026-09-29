"""Таблицы БД (ТЗ, раздел 10). Машинные результаты не перезаписываются: решения инспектора — отдельная история
(decisions), протоколы — версии (protocols), повторный прогон делает старые находки неактивными (findings.active)."""
from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


TS = DateTime(timezone=True)


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    login: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(String(200))
    role: Mapped[str] = mapped_column(String(20))            # inspector | supervisor | admin | ml
    full_name: Mapped[str] = mapped_column(String(200), default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[dt.datetime] = mapped_column(TS, default=utcnow)


class ConstructionObject(Base):
    __tablename__ = "objects"
    id: Mapped[int] = mapped_column(primary_key=True)
    object_code: Mapped[str] = mapped_column(String(120), unique=True)   # OBJ-… (object_id сабмита)
    name: Mapped[str] = mapped_column(String(300), default="")
    address: Mapped[str] = mapped_column(String(500), default="")
    customer: Mapped[str] = mapped_column(String(300), default="")
    contractor: Mapped[str] = mapped_column(String(300), default="")
    permit_number: Mapped[str] = mapped_column(String(120), default="")
    created_at: Mapped[dt.datetime] = mapped_column(TS, default=utcnow)


class Process(Base):
    """Проверка (process_id ТЗ 1.4). status: PENDING|PARSING|READY|VERIFYING|COMPLETED|FINALIZED|FAILED."""
    __tablename__ = "processes"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    object_pk: Mapped[int] = mapped_column(ForeignKey("objects.id"), index=True)
    status: Mapped[str] = mapped_column(String(20), default="PENDING", index=True)
    stage: Mapped[str | None] = mapped_column(String(40), nullable=True)       # текущий этап конвейера
    progress: Mapped[dict] = mapped_column(JSON, default=dict)                 # {этап: {done,total,seconds,status}}
    scenario: Mapped[str | None] = mapped_column(String(30), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    sync_status: Mapped[str] = mapped_column(String(20), default="NONE")       # NONE|PENDING_SYNC|SYNCED|FAILED
    protocol_version: Mapped[int] = mapped_column(Integer, default=0)
    batch: Mapped[int] = mapped_column(Integer, default=1)                     # номер текущей партии загрузки
    options: Mapped[dict] = mapped_column(JSON, default=dict)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(TS, default=utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(TS, default=utcnow, onupdate=utcnow)
    finalized_at: Mapped[dt.datetime | None] = mapped_column(TS, nullable=True)

    object: Mapped[ConstructionObject] = relationship(lazy="joined")


class File(Base):
    """Файл пакета. status: pending|ok|duplicate|junk|unsupported|broken|too_large|archive|converted_source."""
    __tablename__ = "files"
    id: Mapped[int] = mapped_column(primary_key=True)
    process_id: Mapped[str] = mapped_column(ForeignKey("processes.id"), index=True)
    file_id: Mapped[str] = mapped_column(String(80), index=True)       # из манифеста организатора, иначе LOCAL-<sha8>
    original_name: Mapped[str] = mapped_column(String(500))
    rel_path: Mapped[str] = mapped_column(String(2000), default="")    # путь внутри пакета/архива
    ext: Mapped[str] = mapped_column(String(16), default="")
    size: Mapped[int] = mapped_column(Integer, default=0)
    sha256: Mapped[str] = mapped_column(String(64), index=True, default="")
    storage_path: Mapped[str] = mapped_column(String(2000), default="")
    pdf_path: Mapped[str | None] = mapped_column(String(2000), nullable=True)   # PDF для страниц (исходник или конвертация)
    declared_stage: Mapped[str | None] = mapped_column(String(4), nullable=True)
    doc_stage: Mapped[str | None] = mapped_column(String(4), nullable=True)     # PD | RD | ID
    stage_source: Mapped[str | None] = mapped_column(String(20), nullable=True)  # declared|manifest|content|folder
    discipline: Mapped[str | None] = mapped_column(String(40), nullable=True)
    document_code: Mapped[str | None] = mapped_column(String(200), nullable=True)
    revision: Mapped[str | None] = mapped_column(String(40), nullable=True)
    approval_status: Mapped[str | None] = mapped_column(String(40), nullable=True)
    approval_date: Mapped[str | None] = mapped_column(String(40), nullable=True)
    predecessor_pk: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="pending")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    parent_pk: Mapped[int | None] = mapped_column(Integer, nullable=True)       # архив, из которого извлечён
    duplicate_of_pk: Mapped[int | None] = mapped_column(Integer, nullable=True)
    has_signature: Mapped[bool] = mapped_column(Boolean, default=False)         # рядом лежал .sig
    pages: Mapped[int] = mapped_column(Integer, default=0)
    ocr_pages: Mapped[int] = mapped_column(Integer, default=0)
    batch: Mapped[int] = mapped_column(Integer, default=1)
    uploaded_at: Mapped[dt.datetime] = mapped_column(TS, default=utcnow)


class Page(Base):
    __tablename__ = "pages"
    __table_args__ = (UniqueConstraint("file_pk", "page_no"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    file_pk: Mapped[int] = mapped_column(ForeignKey("files.id"), index=True)
    page_no: Mapped[int] = mapped_column(Integer)
    width: Mapped[float] = mapped_column(Float, default=0)       # pt, как страница видна (после Rotate)
    height: Mapped[float] = mapped_column(Float, default=0)
    rotation: Mapped[int] = mapped_column(Integer, default=0)
    has_layer: Mapped[bool] = mapped_column(Boolean, default=False)
    layer_quality: Mapped[float] = mapped_column(Float, default=0)
    need_ocr: Mapped[bool] = mapped_column(Boolean, default=False)
    text_source: Mapped[str] = mapped_column(String(10), default="none")   # layer | ocr | none
    text: Mapped[str] = mapped_column(Text, default="")
    page_class: Mapped[str | None] = mapped_column(String(40), nullable=True)
    other_classes: Mapped[list] = mapped_column(JSON, default=list)
    is_first_page: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    section: Mapped[str | None] = mapped_column(String(20), nullable=True)
    title: Mapped[str | None] = mapped_column(String(300), nullable=True)
    quality_label: Mapped[str | None] = mapped_column(String(30), nullable=True)
    low_quality: Mapped[bool] = mapped_column(Boolean, default=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    document_pk: Mapped[int | None] = mapped_column(ForeignKey("documents.id"), nullable=True, index=True)


class Document(Base):
    """Документ, собранный из страниц (акт с приложениями, сертификат, том/лист проекта), и его паспорт."""
    __tablename__ = "documents"
    id: Mapped[int] = mapped_column(primary_key=True)
    process_id: Mapped[str] = mapped_column(ForeignKey("processes.id"), index=True)
    file_pk: Mapped[int] = mapped_column(ForeignKey("files.id"), index=True)
    first_page: Mapped[int] = mapped_column(Integer)
    last_page: Mapped[int] = mapped_column(Integer)
    doc_type: Mapped[str] = mapped_column(String(40), default="ПРОЧЕЕ")
    stage: Mapped[str | None] = mapped_column(String(4), nullable=True)
    section: Mapped[str | None] = mapped_column(String(20), nullable=True)
    title: Mapped[str | None] = mapped_column(String(300), nullable=True)
    document_code: Mapped[str | None] = mapped_column(String(200), nullable=True)
    sheet: Mapped[str | None] = mapped_column(String(40), nullable=True)
    revision: Mapped[str | None] = mapped_column(String(40), nullable=True)
    approval_status: Mapped[str | None] = mapped_column(String(40), nullable=True)
    is_actual: Mapped[bool] = mapped_column(Boolean, default=True)
    superseded_by_pk: Mapped[int | None] = mapped_column(Integer, nullable=True)
    clarification: Mapped[bool] = mapped_column(Boolean, default=False)
    clarification_reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class Param(Base):
    """Матрица контроля (ТЗ 8.1). Заполняется из parameter_catalog_132.jsonl; admin правит триггер/пороги/активность."""
    __tablename__ = "params"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(20), unique=True)
    section: Mapped[str] = mapped_column(String(50), default="")
    parameter_name: Mapped[str] = mapped_column(String(300), default="")
    unit: Mapped[str] = mapped_column(String(40), default="")
    source_pd: Mapped[str] = mapped_column(Text, default="")
    source_rd: Mapped[str] = mapped_column(Text, default="")
    source_id: Mapped[str] = mapped_column(Text, default="")
    trigger_logic: Mapped[str] = mapped_column(Text, default="")
    criticality: Mapped[str] = mapped_column(String(120), default="")
    review_priority: Mapped[str] = mapped_column(String(10), default="MEDIUM")
    data_type: Mapped[str] = mapped_column(String(20), default="string")
    min_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    regex_pattern: Mapped[str | None] = mapped_column(String(500), nullable=True)
    sp_reference: Mapped[str] = mapped_column(Text, default="")
    gost_reference: Mapped[str] = mapped_column(Text, default="")
    fz_reference: Mapped[str] = mapped_column(Text, default="")
    other_normative: Mapped[str] = mapped_column(Text, default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[dt.datetime] = mapped_column(TS, default=utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(TS, default=utcnow, onupdate=utcnow)


class Extraction(Base):
    """Извлечённое значение параметра на странице (со цитатой и рамкой)."""
    __tablename__ = "extractions"
    id: Mapped[int] = mapped_column(primary_key=True)
    process_id: Mapped[str] = mapped_column(ForeignKey("processes.id"), index=True)
    param_code: Mapped[str] = mapped_column(String(20), index=True)
    stage: Mapped[str] = mapped_column(String(4))
    file_pk: Mapped[int] = mapped_column(ForeignKey("files.id"), index=True)
    page_no: Mapped[int] = mapped_column(Integer)
    location: Mapped[str] = mapped_column(String(300), default="")
    value_raw: Mapped[str] = mapped_column(String(300), default="")
    snippet: Mapped[str] = mapped_column(Text, default="")
    confidence: Mapped[float] = mapped_column(Float, default=0)
    supported: Mapped[bool] = mapped_column(Boolean, default=False)
    bbox: Mapped[list | None] = mapped_column(JSON, nullable=True)          # [x0,y0,x1,y1] в долях видимой страницы
    bbox_source: Mapped[str | None] = mapped_column(String(20), nullable=True)   # layer | rapidocr | page
    batch: Mapped[int] = mapped_column(Integer, default=1)


class Finding(Base):
    """Доказательная группа (Checks + карточка). status — одно из 8 значений таблицы 9.2 ТЗ; машинные поля
    неизменны, решение инспектора — inspector_* (+ история в decisions)."""
    __tablename__ = "findings"
    id: Mapped[int] = mapped_column(primary_key=True)
    process_id: Mapped[str] = mapped_column(ForeignKey("processes.id"), index=True)
    finding_id: Mapped[str] = mapped_column(String(80), index=True)
    source: Mapped[str] = mapped_column(String(20), default="matrix")       # matrix | explication | apartments
    param_code: Mapped[str] = mapped_column(String(60), index=True)
    rule_version: Mapped[str] = mapped_column(String(80), default="")
    location: Mapped[str] = mapped_column(String(300), default="")
    expected_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    actual_rd: Mapped[str | None] = mapped_column(Text, nullable=True)
    actual_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    delta: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(30), index=True)
    review_priority: Mapped[str] = mapped_column(String(10), default="MEDIUM")
    criticality: Mapped[str | None] = mapped_column(String(120), nullable=True)
    rationale: Mapped[str] = mapped_column(Text, default="")
    verification: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    discovery_method: Mapped[str | None] = mapped_column(String(40), nullable=True)
    approved_change_ref: Mapped[str] = mapped_column(String(300), default="NONE")
    submission_label: Mapped[str | None] = mapped_column(String(40), nullable=True)
    submission_status: Mapped[str | None] = mapped_column(String(40), nullable=True)
    submission_include: Mapped[bool] = mapped_column(Boolean, default=False)
    inspector_status: Mapped[str] = mapped_column(String(30), default="PENDING")
    decided_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    decided_at: Mapped[dt.datetime | None] = mapped_column(TS, nullable=True)
    reason_code: Mapped[str | None] = mapped_column(String(40), nullable=True)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_in_version: Mapped[int] = mapped_column(Integer, default=1)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[dt.datetime] = mapped_column(TS, default=utcnow)

    evidence: Mapped[list["EvidenceFragment"]] = relationship(back_populates="finding", lazy="selectin",
                                                              order_by="EvidenceFragment.id")


class EvidenceFragment(Base):
    __tablename__ = "evidence_fragments"
    id: Mapped[int] = mapped_column(primary_key=True)
    finding_pk: Mapped[int] = mapped_column(ForeignKey("findings.id"), index=True)
    role: Mapped[str] = mapped_column(String(20))                    # expected | actual_rd | actual_id
    stage: Mapped[str] = mapped_column(String(4))
    file_pk: Mapped[int] = mapped_column(ForeignKey("files.id"))
    page_no: Mapped[int] = mapped_column(Integer)
    bbox: Mapped[list | None] = mapped_column(JSON, nullable=True)
    bbox_source: Mapped[str | None] = mapped_column(String(20), nullable=True)
    value: Mapped[str | None] = mapped_column(Text, nullable=True)
    snippet: Mapped[str | None] = mapped_column(Text, nullable=True)

    finding: Mapped[Finding] = relationship(back_populates="evidence")


class Decision(Base):
    """История решений инспектора (не перезаписывается)."""
    __tablename__ = "decisions"
    id: Mapped[int] = mapped_column(primary_key=True)
    finding_pk: Mapped[int] = mapped_column(ForeignKey("findings.id"), index=True)
    user_pk: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    decision: Mapped[str] = mapped_column(String(30))
    previous_status: Mapped[str] = mapped_column(String(30), default="PENDING")
    reason_code: Mapped[str | None] = mapped_column(String(40), nullable=True)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(TS, default=utcnow)


class ProtocolVersion(Base):
    __tablename__ = "protocols"
    __table_args__ = (UniqueConstraint("process_id", "version"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    process_id: Mapped[str] = mapped_column(ForeignKey("processes.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(20), default="machine")        # machine | incremental | final | unfinalize
    status: Mapped[str] = mapped_column(String(20), default="READY")
    matrix_version: Mapped[str] = mapped_column(String(60), default="")
    dataset_version: Mapped[str] = mapped_column(String(60), default="none")
    model_version: Mapped[str] = mapped_column(String(300), default="")
    input_manifest_hash: Mapped[str] = mapped_column(String(64), default="")
    summary: Mapped[dict] = mapped_column(JSON, default=dict)
    json_path: Mapped[str] = mapped_column(String(2000), default="")
    pdf_path: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(TS, default=utcnow)
    finalized_at: Mapped[dt.datetime | None] = mapped_column(TS, nullable=True)


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_pk: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    user_login: Mapped[str] = mapped_column(String(64), default="system")
    action: Mapped[str] = mapped_column(String(60), index=True)
    object_code: Mapped[str | None] = mapped_column(String(120), nullable=True)
    process_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(300), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(TS, default=utcnow, index=True)


class DatasetItem(Base):
    """GOLD: CONFIRMED_VIOLATION (+) и NEGATIVE_VERIFIED (−) из финализированных протоколов. dataset_version NULL —
    черновик; в версию попадает только после публикации куратором (ТЗ 9.4)."""
    __tablename__ = "dataset_items"
    id: Mapped[int] = mapped_column(primary_key=True)
    finding_pk: Mapped[int] = mapped_column(ForeignKey("findings.id"), index=True)
    evidence_group_id: Mapped[str] = mapped_column(String(80))
    object_code: Mapped[str] = mapped_column(String(120), index=True)
    param_code: Mapped[str] = mapped_column(String(60))
    gold_label: Mapped[str] = mapped_column(String(20))          # POSITIVE | NEGATIVE
    expert_pk: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    reason_code: Mapped[str | None] = mapped_column(String(40), nullable=True)
    card: Mapped[dict] = mapped_column(JSON, default=dict)        # полная карточка доказательства на момент решения
    dataset_version: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    split: Mapped[str | None] = mapped_column(String(12), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(TS, default=utcnow)


class DatasetVersion(Base):
    __tablename__ = "dataset_versions"
    id: Mapped[int] = mapped_column(primary_key=True)
    version: Mapped[str] = mapped_column(String(40), unique=True)
    items: Mapped[int] = mapped_column(Integer, default=0)
    positives: Mapped[int] = mapped_column(Integer, default=0)
    negatives: Mapped[int] = mapped_column(Integer, default=0)
    sha256: Mapped[str] = mapped_column(String(64), default="")
    split_hashes: Mapped[dict] = mapped_column(JSON, default=dict)
    split_objects: Mapped[dict] = mapped_column(JSON, default=dict)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(TS, default=utcnow)


class ModelVersion(Base):
    __tablename__ = "model_versions"
    id: Mapped[int] = mapped_column(primary_key=True)
    model_version: Mapped[str] = mapped_column(String(300), unique=True)
    artifact: Mapped[str] = mapped_column(String(500), default="")
    dataset_version: Mapped[str] = mapped_column(String(40), default="none")
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)
    approval_status: Mapped[str] = mapped_column(String(20), default="APPROVED")   # DRAFT | APPROVED | REJECTED
    approved_by: Mapped[str] = mapped_column(String(64), default="")
    deployed_at: Mapped[dt.datetime | None] = mapped_column(TS, nullable=True)
    rollback_to: Mapped[str | None] = mapped_column(String(300), nullable=True)


class PageLabel(Base):
    """История меток типа страницы: кто поставил (модель классификатора, Qwen, человек) и с какой уверенностью.
    Текущая метка — в pages; здесь — откуда она и что было до правки."""
    __tablename__ = "page_labels"
    id: Mapped[int] = mapped_column(primary_key=True)
    page_pk: Mapped[int] = mapped_column(ForeignKey("pages.id"), index=True)
    source: Mapped[str] = mapped_column(String(20))              # model | qwen | inspector
    model_version: Mapped[str | None] = mapped_column(String(300), nullable=True)
    page_class: Mapped[str | None] = mapped_column(String(40), nullable=True)
    is_first_page: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    section: Mapped[str | None] = mapped_column(String(20), nullable=True)
    quality_label: Mapped[str | None] = mapped_column(String(30), nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(TS, default=utcnow)


class PageCorrection(Base):
    """Правка разметчика: верный тип / первая страница / раздел страницы. Это обучающие метки классификатора
    страниц (дообучение, ТЗ 9.4); страница определяется по SHA-256 файла и номеру — правка переживает новую проверку."""
    __tablename__ = "page_corrections"
    id: Mapped[int] = mapped_column(primary_key=True)
    page_pk: Mapped[int] = mapped_column(ForeignKey("pages.id"), index=True)
    process_id: Mapped[str] = mapped_column(ForeignKey("processes.id"), index=True)
    object_code: Mapped[str] = mapped_column(String(120), default="")
    file_sha256: Mapped[str] = mapped_column(String(64), index=True, default="")
    page_no: Mapped[int] = mapped_column(Integer)
    user_pk: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    page_class: Mapped[str | None] = mapped_column(String(40), nullable=True)
    is_first_page: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    section: Mapped[str | None] = mapped_column(String(20), nullable=True)
    before: Mapped[dict] = mapped_column(JSON, default=dict)      # метка до правки и её источник
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    used_in_version: Mapped[str | None] = mapped_column(String(300), nullable=True)   # первая версия, учившаяся на ней
    created_at: Mapped[dt.datetime] = mapped_column(TS, default=utcnow)


class RetrainingLog(Base):
    """Журнал итераций дообучения (ML_Retraining_Log, ТЗ раздел 10): что было, на чём учили, метрики до/после, решение
    о публикации. status: QUEUED | RUNNING | ACCEPTED | REJECTED | DRAFT | FAILED | ROLLBACK | APPROVED."""
    __tablename__ = "ml_retraining_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    model_kind: Mapped[str] = mapped_column(String(40), default="page-classifier")
    status: Mapped[str] = mapped_column(String(20), default="QUEUED", index=True)
    base_version: Mapped[str | None] = mapped_column(String(300), nullable=True)
    new_version: Mapped[str | None] = mapped_column(String(300), nullable=True)
    artifact: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    n_corrections: Mapped[int] = mapped_column(Integer, default=0)
    corrections_sha256: Mapped[str] = mapped_column(String(64), default="")
    metrics_before: Mapped[dict] = mapped_column(JSON, default=dict)
    metrics_after: Mapped[dict] = mapped_column(JSON, default=dict)
    rule: Mapped[str] = mapped_column(Text, default="")            # правило приёмки, по которому принято решение
    seconds: Mapped[float] = mapped_column(Float, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    requested_by: Mapped[str] = mapped_column(String(64), default="system")
    decided_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(TS, default=utcnow)
    finished_at: Mapped[dt.datetime | None] = mapped_column(TS, nullable=True)


class Job(Base):
    """Очередь задач конвейера (вместо RabbitMQ). kind: full | incremental."""
    __tablename__ = "jobs"
    id: Mapped[int] = mapped_column(primary_key=True)
    process_id: Mapped[str] = mapped_column(ForeignKey("processes.id"), index=True)
    kind: Mapped[str] = mapped_column(String(20), default="full")
    batch: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)   # queued|running|done|failed
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(TS, default=utcnow)
    started_at: Mapped[dt.datetime | None] = mapped_column(TS, nullable=True)
    finished_at: Mapped[dt.datetime | None] = mapped_column(TS, nullable=True)
    heartbeat_at: Mapped[dt.datetime | None] = mapped_column(TS, nullable=True)


class SyncAttempt(Base):
    """Попытки отправки в ИАИС РиН (журналирование, ТЗ 9.6)."""
    __tablename__ = "sync_attempts"
    id: Mapped[int] = mapped_column(primary_key=True)
    process_id: Mapped[str] = mapped_column(ForeignKey("processes.id"), index=True)
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(20))           # SYNCED | FAILED | SCHEDULED
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_try_at: Mapped[dt.datetime | None] = mapped_column(TS, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(TS, default=utcnow)


class RinMessage(Base):
    """Что «получила» заглушка ИАИС РиН."""
    __tablename__ = "rin_messages"
    id: Mapped[int] = mapped_column(primary_key=True)
    process_id: Mapped[str] = mapped_column(String(36), index=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[dt.datetime] = mapped_column(TS, default=utcnow)


Index("ix_findings_process_active_status", Finding.process_id, Finding.active, Finding.status)
