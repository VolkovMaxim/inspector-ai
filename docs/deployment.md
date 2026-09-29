# Развёртывание

## Требования к хосту

- GPU: 1× H100 80 ГБ; драйвер NVIDIA ≥ 580 (образ vLLM собран с CUDA 13.0); NVIDIA Container Toolkit.
- Диск ≥ 100 ГБ: веса ~31 ГБ (Qwen3.8 FP8 29 + SigLIP2 ~1,5), модель классификатора ~0,74 ГБ, образ vLLM 21,5 ГБ,
  образ сервиса с torch, данные.
- Размер образа сервиса с torch — в [замерах](benchmarks.md).
- ОЗУ от 32 ГБ. CPU загружают LibreOffice, PyMuPDF и RapidOCR (`RAPIDOCR_WORKERS=0` — ядер / 4 процессов): чем больше
  ядер, тем быстрее разбор сканов.
- Linux x86_64, Docker Engine ≥ 24 и Docker Compose ≥ 2.24 (проверено на Docker 29.8 и Compose 5.5), BuildKit (включён по
  умолчанию).
- Драйвер в образ не входит, он ставится на хост. Проверка: `nvidia-smi` на хосте, затем
  `docker run --rm --gpus all --entrypoint nvidia-smi vllm/vllm-openai:v0.29.0`.
- На серверных GPU с драйвером 535–575 может помочь прямая совместимость CUDA: `VLLM_ENABLE_CUDA_COMPATIBILITY=1` в
  `.env` (в образе vLLM есть `cuda-compat` 580). Этот путь не проверен.
- Интернет нужен только для сборки образа и скачивания весов. В работе сервис в сеть не ходит (`HF_HUB_OFFLINE=1`).

## Быстрый старт (стенд жюри, 1×H100)

Все команды — из корня репозитория (каталог с `docker-compose.yml`): там лежит `.env`, а в нём строка `COMPOSE_FILE`.

**1. Код и настройки.**

```bash
git clone https://github.com/VolkovMaxim/inspector-ai.git && cd inspector-ai
cp .env.example .env     # до первого запуска смените пароли учёток, POSTGRES_PASSWORD и JWT_SECRET
```

`.env.example` уже настроен на профиль `h100`: одна Qwen3.8, `OCR_URL` пустой (перечитывание сканов идёт в ту же
модель), GPU для worker через `COMPOSE_FILE`.

**2. Веса моделей (~31 ГБ).**

```bash
scripts/prefetch_models.sh h100        # нужен hf (pip install -U huggingface_hub) или uvx; --dry-run — только список
```

Скрипт скачивает:

- Qwen3.8 FP8 и SigLIP2 (~30,5 ГБ) — в кэш Hugging Face хоста: `HF_HOME` из `.env` (по умолчанию `~/.cache/huggingface`);
- классификатор страниц (~0,74 ГБ: веса и код, без обучающих данных) —
  [wolk1612/inspector-page-cls](https://huggingface.co/wolk1612/inspector-page-cls), ревизия
  `d55fc9acdf1b327a09a360e61c602cace3cee874` (версия `page_cls_v2_lilt_rubert@cc070ea3`), — каталогом
  `models/page-cls` и открывает его на чтение для worker (uid 1000).

Запускайте скрипт от того же пользователя, что и `docker compose`. Повторный запуск докачивает только недостающее.
Классификатор вручную: `hf download wolk1612/inspector-page-cls --revision d55fc9acdf1b327a09a360e61c602cace3cee874
--local-dir models/page-cls && chmod -R a+rX models/page-cls`. Без классификатора сервис тоже работает: тип всех
страниц даёт Qwen, кнопка дообучения заблокирована с надписью «Модель не подключена».

**3. Сборка и запуск.**

```bash
docker compose --profile h100 up -d --build
```

Сервисы: `db`, `api`, `worker`, `backup`, `llm`. `api` доступен сразу. vLLM загружает веса и компилирует граф;
проверка готовности `llm` ждёт до 15 мин. `worker` ждёт ответа модели до `WAIT_FOR_MODELS_S` (30 мин), чтобы задачи,
поставленные во время загрузки весов, не падали. Время первого старта на H100 — в [замерах](benchmarks.md).

**4. Проверки.**

```bash
docker compose --profile h100 ps                 # llm → healthy после загрузки весов
curl -s localhost:8080/health
# {"status":"ok","db":"ok","llm":{"url":"http://llm:8000/v1","reachable":true,"status_code":200},"worker":{...}}
# пока llm грузится — "status":"degraded"

docker compose exec worker python -c "import torch; print(torch.cuda.is_available())"     # True — worker видит GPU

TOKEN=$(curl -s localhost:8080/api/v1/auth/login -H 'Content-Type: application/json' \
  -d '{"login":"ml","password":"ml"}' | python3 -c 'import sys,json; print(json.load(sys.stdin)["access_token"])')
curl -s localhost:8080/api/v1/ml/page-classifier -H "Authorization: Bearer $TOKEN" \
  | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d["enabled"], d["current"], d["threshold"])'
# True page-cls:page_cls_v2_lilt_rubert@cc070ea3 0.86
```

Интерфейс: `http://<хост>:8080` (учётки — [интерфейс, роли и демо-учётки](usage.md#интерфейс-роли-и-демо-учётки)). Swagger:
`http://<хост>:8080/api/docs`. Если порт 8080 занят — `API_PORT` в `.env`.

**Память GPU на H100** (по замерам на стенде H100 PCIe, 81,5 ГБ):

| | Что занимает | Настройка | Память |
|---|---|---|---|
| `llm` Qwen3.8-27B FP8 | веса 29 ГБ + KV-кэш + рабочая память vLLM | `LLM_KV_BYTES=16G` (16 ГиБ, задан абсолютно) | 48,7 ГБ после загрузки, 49,5 после разметки 300 страниц; к концу прогона ~53 (NVFP4 за полный прогон вырос с 39,7 до 44,1) |
| `worker`: основной процесс | классификатор LiLT-ruBert + SigLIP2 | `PAGE_CLASSIFIER_DEVICE=auto` | ~5,6 ГБ |
| `worker`: RapidOCR | пул процессов, у каждого свой движок | `RAPIDOCR_GPU_WORKERS=2` | до 5,6 ГБ на процесс |
| Всего | | | ~70 ГБ, запас ~12 ГБ |

С 4 процессами RapidOCR (как с весами NVFP4) вышло бы ~81 ГБ — впритык ко всей памяти карты; с 3 — ~75 ГБ. vLLM на
старте требует свободными `LLM_GPU_UTIL` × объём GPU: 0,60 × 79,6 ≈ 48 ГиБ. При нехватке KV-кэша — `LLM_KV_BYTES` или `LLM_MAX_SEQS`.
Фактическая память на H100 — в [замерах](benchmarks.md).

**Почему на H100 — FP8.** У Hopper нет вычислений FP4: веса NVFP4 vLLM держит сжатыми, а считает в BF16 (Marlin) —
на H100 PCIe вход Qwen идёт 3–5 тыс. токенов/с. Официальные веса
`Qwen/Qwen3.8-27B-FP8` (Apache-2.0, ревизия `017b9c7af6b5689d5dd426a76e0bc077eb5ca20a`, 29 ГБ) Hopper считает
напрямую: разметка типа 300 страниц — 130 с против 245 с у NVFP4. Тип страницы у двух весов совпал на 267 из 300
страниц. На проверочном наборе 409 страниц тип верен у NVFP4 на 333 страницах, у той же модели в весах FP8
или BF16 — на 336; разница не значима (p = 0,51). Полный прогон Алтуфьевского и остальные замеры
качества ([замеры](benchmarks.md)) сделаны на NVFP4 с 4 процессами RapidOCR.

**Запасной вариант — NVFP4.** Если `llm` на FP8 не стартует или не хватает памяти, в `.env` задайте
`LLM_MODEL_REPO=unsloth/Qwen3.8-27B-NVFP4`, `RAPIDOCR_GPU_WORKERS=4` и метку NVFP4 (она есть в `.env.example`), затем
`scripts/prefetch_models.sh h100 && docker compose --profile h100 up -d`. С этими весами сделан полный прогон на H100
([замеры](benchmarks.md)).

## Профили

Профиль Compose выбирает, какая модель поднимается рядом с `db`, `api`, `worker` и `backup`. Веса для профиля
скачивает `scripts/prefetch_models.sh <профиль>`.

| Профиль | Команда | Модели | GPU |
|---|---|---|---|
| `h100` (стенд жюри) | `docker compose --profile h100 up -d` | Qwen3.8-27B FP8; текст сканов — RapidOCR | 1× H100 80 ГБ |
| `single` | `docker compose --profile single up -d` | одна Qwen3.8-27B NVFP4 на всё (`SINGLE_MODEL_REPO`; FP8 в 32 ГБ не помещается). В `.env`: метка весов NVFP4 (есть в `.env.example`) и `RAPIDOCR_GPU=false`; веса — `scripts/prefetch_models.sh single` | 32 ГБ |
| без профиля | `docker compose up -d` | модель не поднимается: `worker` ходит по `LLM_URL` из `.env` | не нужна, если убрать строку `COMPOSE_FILE` из `.env` |

Все замеры ([benchmarks.md](benchmarks.md)) сделаны на профиле `h100`.

## Настройки .env

Адреса моделей для `api` и `worker` задаются в `.env`. Остальные переменные с пояснениями — в `.env.example`.

Строки моделей из `.env.example`:

```dotenv
LLM_URL=http://llm:8000/v1
LLM_MODEL=qwen
OCR_URL=
OCR_MODEL=
MODEL_VERSION_LABEL=llm=Qwen/Qwen3.8-27B-FP8;ocr=rapidocr-ppocrv5-mobile+reocr:Qwen/Qwen3.8-27B-FP8;bbox=rapidocr-ppocrv5-mobile+orient-ab
COMPOSE_FILE=docker-compose.yml:docker-compose.gpu.yml
LLM_KV_BYTES=16G
```

Пустые `OCR_*` — этап reocr идёт в `LLM_URL`, в ту же Qwen3.8.

**Сборка без интерфейса.** Если `web/` не собирается или не нужен, вместо интерфейса кладётся заглушка со ссылкой на
`/api/docs`: `SKIP_WEB=1 docker compose build`.

**Переменные OCR и отбора** (значения по умолчанию подходят стенду):

| Переменная | По умолчанию | Что делает |
|---|---|---|
| `OCR_ENGINE` | `rapidocr` | текст сканов: `rapidocr`; `vlm` — модель читает все сканы; `none` — без OCR |
| `REOCR_SELECTED` | `true` | при `rapidocr`: отобранные для извлечения сканы перечитывает модель (этап reocr) |
| `RAPIDOCR_WORKERS` | `0` | процессов RapidOCR на CPU; 0 — ядер / 4 |
| `RAPIDOCR_GPU` | `false` (в `.env.example` — `true`) | RapidOCR на GPU пулом процессов |
| `RAPIDOCR_GPU_WORKERS` | `2` | процессов RapidOCR на GPU (~5 ГБ видеопамяти каждый); 2 — запас рядом с Qwen FP8, с NVFP4 — можно 4 |
| `RAPIDOCR_GPU_MEM_GB` | `3` | предел памяти onnxruntime на сессию (в процессе их три) |
| `RAPIDOCR_ORIENT` | `true` | выпрямлять повёрнутые сканы |
| `SELECT_MODE` | `fused` | `fused` — модель F40 + слова; `lexical` — только слова |
| `TOP_K_PAGES` | `40` | страниц-кандидатов на параметр × стадию |
| `EXTRACT_WITH_IMAGE` | `false` | картинка OCR-страницы в извлечении |

`MODEL_VERSION_LABEL` попадает в протокол (`traceability.model_version`) и в реестр моделей. Код дописывает к метке
версии промптов и отбора, например `…;classify=pc1r;extract=g2;judge=j1;verify=v1;ocr=plain1;select=f40:router@b6c996c9`.
Версия классификатора страниц видна в реестре моделей (`page-cls:…`), в метке каждой страницы и в прогрессе этапа
`classify`. Сменили модель — поправьте метку.
