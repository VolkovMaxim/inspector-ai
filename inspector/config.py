"""Настройки сервиса из переменных окружения (см. service/.env.example)."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

SERVICE_ROOT = Path(__file__).resolve().parent.parent
RESOURCES = Path(__file__).resolve().parent / "resources"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore")

    # хранилище и БД
    data_dir: Path = SERVICE_ROOT / "var"
    database_url: str = ""                      # пусто -> sqlite:///<data_dir>/inspector.db

    # модели (OpenAI-совместимый API; в решении — vLLM локально)
    llm_url: str = "http://localhost:8000/v1"
    llm_model: str = "qwen"
    llm_provider: str = "vllm"                  # vllm | openrouter
    llm_api_key: str = ""
    ocr_url: str = ""                           # пусто -> llm_url (профиль single: одна Qwen3.8 на всё)
    ocr_model: str = ""                         # пусто -> llm_model
    ocr_provider: str = ""                      # пусто -> llm_provider
    ocr_engine: str = "rapidocr"                # rapidocr (текст сканов — RapidOCR) | vlm (модель читает все) | none
    reocr_selected: bool = True                 # при rapidocr: страницы-кандидаты извлечения перечитывает модель
    bbox_engine: str = "rapidocr"               # rapidocr | none
    rapidocr_gpu: bool = False
    rapidocr_gpu_mem_gb: float = 3.0            # предел памяти onnxruntime на сессию (у процесса их три: ~5 ГБ на деле)
    rapidocr_gpu_workers: int = 2               # процессов RapidOCR на GPU (у каждого свой движок, ~5 ГБ): 2 — запас
                                                # ~12 ГБ рядом с Qwen FP8 на H100; 4 — быстрее, но с FP8 не помещаются
    rapidocr_workers: int = 0                   # процессов RapidOCR на CPU; 0 — число ядер / 4
    rapidocr_orient: bool = True                # выпрямлять повёрнутые сканы перед RapidOCR (pipeline/orient.py)
    extract_with_image: bool = False            # извлечение: на OCR-страницах вместе с текстом картинка страницы
    llm_concurrency: int = 16
    ocr_concurrency: int = 32
    classify_concurrency: int = 12
    llm_timeout_s: float = 600.0
    llm_retries: int = 3
    ocr_max_tokens: int = 4000
    select_mode: str = "fused"                  # fused (обучаемый отбор F40 + слова, pipeline/router.py) | lexical
    top_k_pages: int = 40                       # страниц на (параметр, стадия)
    # классификатор типа страницы с дообучением (inspector/ml/page_classifier.py); пусто — тип даёт только Qwen
    page_classifier_dir: str = ""               # начальная модель (каталог с manifest.json и predict.py)
    page_classifier_device: str = "auto"        # auto | cuda | cpu
    page_classifier_min_conf: float = 0.0       # ниже — страница к Qwen; 0 — порог из manifest (иначе 0,5)
    # первые страницы каких файлов всё равно к Qwen: у модели нет заголовка, а в ИД из него берутся номер акта и
    # правило «Приложение» (без заголовка Qwen номер акта находится у 100 из 949 документов против 736 из 953).
    # id — ИД и файлы с неизвестной стадией; all — все; off — никакие
    page_classifier_first_to_qwen: str = "id"
    page_classifier_batch: int = 32             # страниц на вызов модели; меньше — меньше видеопамяти
    page_cls_acceptance: str = ""               # замороженный набор приёмки (JSONL); пусто — версии утверждает человек
    retrain_max_drop: float = 0.01              # приёмка: класс на наборе не ниже прежнего больше чем на столько
    model_version_label: str = ""               # пусто -> собирается из имён моделей

    # загрузка (ТЗ 9.1: 50 МБ на файл, 200 МБ на пакет)
    max_file_mb: int = 50
    max_upload_mb: int = 200
    import_roots: str = ""                      # через «:» — где admin может импортировать пакеты с диска сервера
    manifest_path: str = ""                     # JSONL организатора (sha256 -> file_id, stage), по желанию

    # безопасность (демо-учётки; в эксплуатации — сменить)
    jwt_secret: str = "change-me-in-production"
    jwt_ttl_hours: int = 12
    admin_password: str = "admin"
    inspector_password: str = "inspector"
    supervisor_password: str = "supervisor"
    ml_password: str = "ml"

    # интеграция с ИАИС РиН (заглушка)
    rin_url: str = "http://localhost:8080/api/v1/mock-rin/receive"
    rin_retry_delays_s: str = "60,300,900"       # ТЗ 9.6: 1, 5, 15 минут

    # служебное
    log_level: str = "INFO"
    worker_poll_s: float = 2.0
    web_dist: Path = SERVICE_ROOT / "web" / "dist"

    @property
    def db_url(self) -> str:
        return self.database_url or f"sqlite:///{self.data_dir / 'inspector.db'}"

    @property
    def ocr_url_eff(self) -> str:
        return self.ocr_url or self.llm_url

    @property
    def ocr_model_eff(self) -> str:
        return self.ocr_model or self.llm_model

    @property
    def ocr_provider_eff(self) -> str:
        return self.ocr_provider or self.llm_provider

    @property
    def model_version(self) -> str:
        return self.model_version_label or (
            f"llm={self.llm_model};ocr={self.ocr_label};bbox={self.bbox_label}")

    @property
    def ocr_label(self) -> str:
        if self.ocr_engine == "vlm":
            return self.ocr_model_eff
        if self.ocr_engine == "rapidocr":
            return "rapidocr" + (f"+reocr:{self.ocr_model_eff}" if self.reocr_selected else "")
        return "none"

    @property
    def bbox_label(self) -> str:
        if self.bbox_engine != "rapidocr":
            return "none"
        return "rapidocr-ppocrv5-mobile" + ("+orient-ab" if self.rapidocr_orient else "")

    # защита интеграции и входа (секреты — только в .env)
    rin_token: str = ""                         # общий токен ИАИС РиН (Bearer); пусто -> случайный в data_dir
    login_max_failures: int = 5                 # неудачных входов пары (логин, IP) за окно -> 429; 0 — без защиты
    login_window_s: int = 300                   # окно подсчёта неудачных входов, с


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    s.data_dir.mkdir(parents=True, exist_ok=True)
    return s
