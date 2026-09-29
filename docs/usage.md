# Работа с сервисом: интерфейс, API, результат

## Интерфейс, роли и демо-учётки

Разделы интерфейса:

- **Проверки** — список с фильтрами, «Новая проверка».
- **Проверка** — вкладки «Сводка», «Кандидаты», «Подтверждённые», «Проверенные отрицательные», «Комплектность»,
  «Гипотезы», «Документы», «Страницы», «Журнал»; прогресс по этапам; выгрузки PDF, DOCX, XML, сабмит JSON.
- **Рабочее место** — карточка кандидата слева, страницы ПД | РД | ИД с подсветкой значения справа. Решение —
  клавишами 1 / 2 / 3. Здесь же виден тип открытой страницы, и его можно исправить.
- **Вкладка «Страницы»** — миниатюры всех страниц проверки: тип, первая страница, раздел, кто поставил метку (модель,
  Qwen, человек) и уверенность модели. Фильтры: стадия, файл, тип, источник метки, уверенность модели ниже порога,
  только исправленные, только первые страницы; порядок — по файлу или по уверенности. Выделение и массовая правка;
  клавиши: T — тип, 1 / 0 — первая страница да / нет, R — повторить последнюю правку, A — выделить всё. Каждая правка
  идёт в дообучение классификатора.
- **Нормативная база** — матрица 132 параметров (правка — admin).
- **GOLD и дообучение** (ml, admin) — «Набор GOLD», «Отчёт по дообучению», «Классификатор страниц» и «Отбор
  страниц» (итерация, утверждение, откат, журнал итераций), «Реестр моделей».
- **Журнал** (admin, supervisor, ml) — журнал действий.

| Логин / пароль | Роль | Права (`inspector/api/deps.py`) |
|---|---|---|
| `inspector` / `inspector` | инспектор | просмотр; загрузка и запуск; решения по кандидатам, финализация; отправка в ИАИС РиН; правка типа страниц; журнал отдельной проверки |
| `supervisor` / `supervisor` | супервизор | всё, что у инспектора, + отмена финализации (с причиной) и весь журнал действий |
| `ml` / `ml` | ML-инженер | просмотр; GOLD и его версии; отчёт по дообучению; весь журнал; правка типа страниц; дообучение классификатора и отбора страниц, утверждение и откат их версий. Загружать файлы и решать по кандидатам не может |
| `admin` / `admin` | администратор | все права, в том числе правка нормативной базы и импорт с диска сервера |

Пароли задаются в `.env` (`INSPECTOR_PASSWORD`, `SUPERVISOR_PASSWORD`, `ADMIN_PASSWORD`, `ML_PASSWORD`) **до первого
запуска**. Учётки создаются при первом старте, если их ещё нет в БД, поэтому поздняя правка `.env` пароль не меняет.

`JWT_SECRET`: если он пустой или начинается с `change-me`, при старте создаётся случайный ключ и сохраняется в томе
данных (`/var/inspector/.jwt_secret`). Свой ключ: `openssl rand -hex 32`. `POSTGRES_PASSWORD` тоже задаётся до первого
старта: БД запоминает его при создании тома. Допустимы латиница, цифры и дефис, без `@ : / ? # %` и пробелов: пароль
подставляется в URL.

**Защита входа от перебора.** 5 неудачных входов одной пары (логин, IP) за 300 с → ответ 429 с заголовком
`Retry-After`. Успешный вход сбрасывает счёт. Настройки — `LOGIN_MAX_FAILURES`, `LOGIN_WINDOW_S`. Счётчик хранится в
памяти процесса api: если заблокировали себя, подождите 5 минут или выполните `docker compose restart api`.

## Как загрузить пакет

Лимиты ТЗ 9.1: 50 МБ на файл, 200 МБ на запрос. Форматы: pdf, docx, doc, xml, xls, xlsx, rtf, odt, jpg, jpeg,
png, tif, tiff, zip, 7z, rar, sig, jsonl (манифест организатора). Файл больше 50 МБ, пустой или неподдерживаемого
формата отклоняется с причиной, остальные файлы запроса принимаются. Запрос больше 200 МБ отклоняется целиком
(413), ничего не сохраняется.

**1. Интерфейс.** «Новая проверка» → поля объекта → файлы в зоны ПД / РД / ИД (или «Определить автоматически» — стадия
по содержимому страниц) → «Запустить проверку». Прогресс по этапам виден на странице проверки. После готовности
открывается рабочее место.

**2. API (curl).**

```bash
API=http://localhost:8080/api/v1
TOKEN=$(curl -s $API/auth/login -H 'Content-Type: application/json' \
  -d '{"login":"inspector","password":"inspector"}' | python3 -c 'import sys,json; print(json.load(sys.stdin)["access_token"])')
AUTH="Authorization: Bearer $TOKEN"

# новая проверка + файлы ПД (поле files — сколько угодно раз; stage: PD | RD | ID | AUTO)
curl -s $API/documents/upload -H "$AUTH" \
  -F name='ЖК «Пример», корпус 1' -F address='Москва, ул. Примерная, 1' \
  -F stage=PD -F files=@pd/КР.pdf -F files=@pd/АР.pdf
# -> {"process_id": "…", "accepted": [...], "rejected": [{"name", "reason_code", "reason"}]}
PID=<process_id из ответа>

# остальные стадии — в тот же процесс
curl -s $API/documents/upload -H "$AUTH" -F process_id=$PID -F stage=RD -F files=@rd/КЖ.pdf
curl -s $API/documents/upload -H "$AUTH" -F process_id=$PID -F stage=ID -F files=@id/АОСР.zip

curl -s -X POST $API/processes/$PID/start -H "$AUTH"          # -> 202 {"job_id": …}
curl -s $API/processes/$PID -H "$AUTH"                         # статус, этап, прогресс по этапам, счётчики
```

Если протокол уже есть, файлы, загруженные в процесс, обрабатываются инкрементально: разбираются только новые файлы,
сравниваются только затронутые параметры, прежняя версия протокола остаётся в истории.

**3. Большой пакет — импорт с диска сервера** (роль admin, без лимита 200 МБ на запрос). В `.env` укажите каталог
хоста с пакетами: `IMPORT_DIR=/srv/inspector-import`. Каталог должен существовать; в контейнерах он доступен только
на чтение как `/import`.

```bash
cp -r /путь/к/пакету /srv/inspector-import/obj1
ADMIN=$(curl -s $API/auth/login -H 'Content-Type: application/json' \
  -d '{"login":"admin","password":"admin"}' | python3 -c 'import sys,json; print(json.load(sys.stdin)["access_token"])')
PID=$(curl -s $API/processes -H "Authorization: Bearer $ADMIN" -H 'Content-Type: application/json' \
  -d '{"name":"Объект 1"}' | python3 -c 'import sys,json; print(json.load(sys.stdin)["id"])')
for st in PD RD ID; do
  curl -s -X POST $API/processes/$PID/import-local -H "Authorization: Bearer $ADMIN" \
    -H 'Content-Type: application/json' -d "{\"path\":\"/import/obj1/$st\",\"stage\":\"$st\"}"
done
curl -s -X POST $API/processes/$PID/start -H "Authorization: Bearer $ADMIN"
```

Путь вне `/import` отклоняется (403). Манифест организатора (JSONL: sha256 → file_id, стадия) подключается
переменной `MANIFEST_PATH=/import/manifest.jsonl`. Тогда `file_id` в протоколе и выходном JSON совпадают с
идентификаторами организаторов.

## API и OpenAPI

- Swagger UI: `http://<хост>:8080/api/docs`. Работает без интернета: swagger-ui 5.33.0 лежит в
  `inspector/resources/swagger` и отдаётся самим api. Кнопка **Authorize**: вставьте `access_token` из
  `POST /api/v1/auth/login`, после этого «Try it out» работает для защищённых маршрутов.
- Спецификация OpenAPI 3: `http://<хост>:8080/api/v1/openapi.json`.
- Авторизация: `POST /api/v1/auth/login {login, password}` → `{access_token, …}`, затем заголовок
  `Authorization: Bearer <token>`. Перебор паролей → 429 (см. [защиту входа от перебора](#интерфейс-роли-и-демо-учётки) выше).
- Ошибки: `{"error": {"code": "…", "message": "…", "details": …}}`. Без токена — 401, роль без права — 403. Ответ
  содержит заголовок `X-Request-ID`, по нему запрос ищется в логах.
- `GET /health` — БД, доступность модели (и отдельного `OCR_URL`, если он задан), очередь. Ответ 200 `ok`;
  200 `degraded`, если модель не отвечает; 503, если недоступна БД. `GET /metrics` — метрики Prometheus: HTTP-запросы,
  очередь задач, загрузка CPU (load average), память хоста, место на томе данных, активные пользователи за 15 минут.

Основные маршруты (`/api/v1`, полный перечень — в OpenAPI):

| Группа | Маршруты |
|---|---|
| Вход | `auth/login`, `auth/me` |
| Проверки и файлы | `documents/upload`, `processes`, `processes/{id}` (+ `/start`, `/files`, `/documents`, `/import-local`), `files/{pk}/download`, `files/{pk}/pages/{n}` (+ `/text`, `/image`) |
| Находки и решения | `processes/{id}/findings`, `findings/{ref}`, `findings/{ref}/decision`, `findings/bulk-confirm`, `findings/{ref}/promote` (гипотеза → кандидат), `processes/{id}/finalize`, `processes/{id}/unfinalize` |
| Протокол | `processes/{id}/protocol` (+ `/versions`), `protocol.pdf`, `protocol.docx`, `protocol.xml`, `submission.json` |
| Страницы и классификатор | `processes/{id}/pages` (метка, источник, уверенность; фильтры), `processes/{id}/pages/corrections` (правка, право `label`), `ml/page-taxonomy`, `ml/page-classifier` (состояние, версии, правки, итерации), `ml/page-classifier/retrain`, `…/approve`, `…/reject`, `…/rollback` (право `retrain`), `ml/retraining-log` |
| Отбор страниц | `ml/page-router` (состояние, версии, метки из решений, итерации), `ml/page-router/retrain`, `…/approve`, `…/reject`, `…/rollback` (право `retrain`) |
| GOLD и модели | `dataset/items`, `dataset/versions` (+ `/{version}/export`), `reports/retraining`, `models` |
| Прочее | `inspection/{process_id}` (отправка в ИАИС РиН и её статус), `mock-rin/receive` (заглушка РиН), `params`, `params/{code}`, `audit` |

## Результат: протокол и JSON по схеме организаторов

- **Выходной JSON** по схеме организаторов (`inspector/resources/submission_schema.json`, побайтно совпадает со
  схемой организаторов): `GET /api/v1/processes/{id}/submission.json`. Перед отдачей JSON проверяется валидатором
  схемы. Пример формата — [`tests/data/submission_example.json`](../tests/data/submission_example.json):

  ```json
  {"object_id": "OBJ-NOVOSLOBODSKAYA",
   "checks": [{"parameter_code": "KR-057", "location": "Форшахта",
               "pd_value": "А400", "rd_value": "А240", "id_value": "А240С",
               "violation_label": "VIOLATION_PRESENT", "protocol_status": "CRITICAL",
               "criticality": "Критическое (приостановка работ)",
               "evidence": [{"stage": "PD", "file_id": "F0105", "pdf_page_number": 13},
                            {"stage": "RD", "file_id": "F0137", "pdf_page_number": 2},
                            {"stage": "ID", "file_id": "F0068", "pdf_page_number": 2}]}]}
  ```

- **Протокол** по Приложению 2, разделы 1–9: `GET /api/v1/processes/{id}/protocol` (JSON, пример —
  [`tests/data/protocol_example.json`](../tests/data/protocol_example.json)), выгрузки `protocol.pdf`, `protocol.docx`,
  `protocol.xml`, история версий `protocol/versions`. Резолютивная часть формируется только после финализации и
  только по подтверждённым нарушениям.
- Пример выхода на мини-пакете Новослободской — [`examples/`](../examples/) (сабмит, протокол JSON и PDF), сделан
  предыдущей версией конвейера; формат файлов тот же.
- **GOLD** — эталон из решений инспектора по финализированным протоколам: подтверждённое нарушение → POSITIVE,
  отклонённый кандидат → NEGATIVE, карточка доказательства сохраняется целиком. Версия набора делит объекты на
  train / validation / test (70 / 15 / 15 % по хешу кода объекта: все элементы объекта — в одной части), хранит
  SHA-256 набора и каждой части; выгрузка — JSONL.
