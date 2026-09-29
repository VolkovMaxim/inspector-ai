# Эксплуатация

```bash
docker compose logs -f api worker               # логи в JSON (timestamp, level, service, message, request_id)
docker compose --profile h100 logs -f llm       # загрузка весов, ошибки vLLM
docker compose --profile h100 ps                # состояние и healthcheck
docker compose --profile h100 down              # остановить (данные и БД сохраняются в томах)
docker compose --profile h100 up -d --build     # обновить после git pull
docker compose exec worker python scripts/verify_store.py      # сверка SHA-256 хранилища по требованию
```

- Тома: `inspector_pgdata` (БД), `inspector_data` (файлы, кэш разбора, протоколы, версии классификатора),
  `inspector_backups` (копии БД), `inspector_import` (пустой, если `IMPORT_DIR` не задан), `inspector_vllm-cache`
  (кэш компиляции vLLM). `docker compose down -v` удаляет их вместе с данными.
- Резервные копии БД делает служба `backup`: `pg_dump` каждые `BACKUP_INTERVAL_MIN` минут (по умолчанию 15) в том
  `inspector_backups` (или каталог хоста `BACKUP_DIR`). Копии старше `BACKUP_KEEP_DAYS` дней (30) удаляются только
  после удачной новой копии и только свои (`inspector_*.dump`): пока `pg_dump` падает, прежние копии остаются. При 15
  минутах и 30 днях хранится до 2880 копий — место оценивайте по размеру одной копии (`docker compose logs backup`).
  Файлы тома данных (`inspector_data`) служба не копирует. Восстановление:

  ```bash
  docker compose exec backup ls /backups                        # список копий, время в имени — UTC
  docker compose stop api worker
  docker compose exec -T backup pg_restore --clean --if-exists --no-owner -d inspector /backups/inspector_<время>.dump
  docker compose start api worker
  ```

- Очередь задач — таблица в БД. Задача без heartbeat дольше 15 мин считается упавшей и перезапускается: всего 3
  попытки (запуск и до 2 повторов, ТЗ 9.1). Итерация дообучения, прерванная остановкой worker, при следующем старте
  получает статус «ошибка», модель остаётся прежней. В compose один worker.
- Периодические задачи выполняет `worker` (`inspector/maintenance.py`, расписание — `/var/inspector/maintenance.json`):
  раз в сутки сверка SHA-256 хранилища (итог — `STORE_INTEGRITY_OK` / `STORE_INTEGRITY_FAILED` в журнале аудита), раз
  в неделю отчёт по дообучению (`/var/inspector/reports/retraining_<дата>.json` и `RETRAINING_REPORT` в журнале).
  Сбой — `MAINTENANCE_FAILED`, повтор через час.
- Классификатор страниц: версии после дообучения лежат в `inspector_data` (`/var/inspector/models/page-cls/`),
  действующая версия — в реестре (`GET /api/v1/ml/page-classifier`). Отбор страниц — там же
  (`/var/inspector/models/page-router/`, `GET /api/v1/ml/page-router`).
- Логи контейнеров ротируются (5 × 50 МБ на сервис).
- TLS — обратным прокси (nginx, Caddy) перед `api`. Задайте `API_BIND=127.0.0.1`, чтобы api слушал только
  localhost, и `FORWARDED_ALLOW_IPS` — адрес прокси. Порт api на хосте — `API_PORT` (8080).
- Порты моделей наружу не публикуются: api и worker обращаются к ним по внутренней сети compose.
  Проверка изнутри: `docker compose --profile h100 exec llm curl -s localhost:8000/v1/models`.
- Контейнер vLLM работает от root. Если разрешить ему докачку весов (`HF_HUB_OFFLINE=0`), новые файлы в кэше HF
  хоста будут принадлежать root. Лучше качать заранее `scripts/prefetch_models.sh` от того же пользователя, что
  запускает compose. Worker монтирует кэш HF только на чтение.
