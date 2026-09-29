#!/usr/bin/env bash
# Скачать веса моделей профиля в кэш Hugging Face хоста — тот, что docker-compose.yml монтирует в vLLM.
#
#   scripts/prefetch_models.sh [h100|single|qwen36-ocr] [--dry-run]   # по умолчанию h100; --dry-run — только показать
#
# h100:         LLM_MODEL_REPO (Qwen/Qwen3.8-27B-FP8, ~29 ГБ) — одна Qwen3.8 на всё
# single:       SINGLE_MODEL_REPO (unsloth/Qwen3.8-27B-NVFP4, ~23,4 ГБ) — то же на GPU 32 ГБ
# qwen36-ocr:   + OCR_MODEL_REPO (Qwen/Qwen3.6-35B-A3B-FP8, ~37,5 ГБ) — для --profile h100 --profile qwen36-ocr
# Всем профилям: SIGLIP_MODEL_REPO (google/siglip2-base-patch16-512 @ ревизия модели, ~1,5 ГБ) — вектор картинки
# классификатора типа страницы; сам классификатор PAGE_CLS_MODEL_REPO (wolk1612/inspector-page-cls @ ревизия, ~0,74 ГБ:
# веса и код, без обучающих данных) — не в кэш HF, а каталогом PAGE_CLASSIFIER_HOST_DIR (./models/page-cls).
# Значения HF_HOME, *_MODEL_REPO, *_MODEL_REVISION, PAGE_CLASSIFIER_HOST_DIR: окружение > service/.env > по умолчанию.
# Нужен `hf` (pip install -U huggingface_hub) или `uv` (тогда запускается `uvx --from huggingface_hub hf`).
# Повторный запуск докачивает только недостающее. Для закрытых репозиториев — HF_TOKEN в окружении.
# Запускайте от того же пользователя, что и docker compose: иначе HF_HOME по умолчанию (~) будет другим.
set -euo pipefail

profile="${1:-h100}"
dry_run=()
if [[ "${2:-}" == "--dry-run" ]]; then dry_run=(--dry-run); fi
cd "$(dirname "$0")/.."        # service/

# значение ключа: окружение > .env > по умолчанию (.env не исполняется как shell — читается построчно)
setting() {
  local key="$1" default="$2" value="${!1:-}"
  if [[ -z "$value" && -f .env ]]; then
    value="$(sed -n "s/^[[:space:]]*${key}[[:space:]]*=[[:space:]]*//p" .env | tail -n1)"
    value="${value%%[[:space:]]#*}"                       # комментарий в конце строки
    value="${value%"${value##*[![:space:]]}"}"             # хвостовые пробелы
    value="${value#\"}"; value="${value%\"}"; value="${value#\'}"; value="${value%\'}"
  fi
  printf '%s' "${value:-$default}"
}

HF_HOME="$(setting HF_HOME "$HOME/.cache/huggingface")"
HF_HOME="${HF_HOME/#\~/$HOME}"
export HF_HOME
LLM_REPO="$(setting LLM_MODEL_REPO Qwen/Qwen3.8-27B-FP8)"
SINGLE_REPO="$(setting SINGLE_MODEL_REPO unsloth/Qwen3.8-27B-NVFP4)"
OCR_REPO="$(setting OCR_MODEL_REPO Qwen/Qwen3.6-35B-A3B-FP8)"
SIGLIP_REPO="$(setting SIGLIP_MODEL_REPO google/siglip2-base-patch16-512)"
SIGLIP_REV="$(setting SIGLIP_MODEL_REVISION a89f5c5093f902bf39d3cd4d81d2c09867f0724b)"
CLS_REPO="$(setting PAGE_CLS_MODEL_REPO wolk1612/inspector-page-cls)"
CLS_REV="$(setting PAGE_CLS_MODEL_REVISION d55fc9acdf1b327a09a360e61c602cace3cee874)"
CLS_DIR="$(setting PAGE_CLASSIFIER_HOST_DIR ./models/page-cls)"
CLS_DIR="${CLS_DIR/#\~/$HOME}"

case "$profile" in
  h100)        repos=("$LLM_REPO" "$SIGLIP_REPO@$SIGLIP_REV"); need_gb=32; up="--profile h100" ;;
  single)      repos=("$SINGLE_REPO" "$SIGLIP_REPO@$SIGLIP_REV"); need_gb=26; up="--profile single" ;;
  qwen36-ocr)  repos=("$LLM_REPO" "$OCR_REPO" "$SIGLIP_REPO@$SIGLIP_REV"); need_gb=70; up="--profile h100 --profile qwen36-ocr" ;;
  -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
  *) echo "неизвестный профиль: $profile (нужно h100, single или qwen36-ocr)" >&2; exit 2 ;;
esac

if command -v hf >/dev/null 2>&1; then
  HF=(hf)
elif command -v uvx >/dev/null 2>&1; then
  HF=(uvx --from huggingface_hub hf)
else
  echo "нет ни hf, ни uvx: pip install -U huggingface_hub  или  curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
  exit 1
fi

mkdir -p "$HF_HOME/hub"
if [[ -e "$HF_HOME/xet" && ! -w "$HF_HOME/xet" ]]; then    # каталог создал контейнер от root — кэш xet рядом
  export HF_XET_CACHE="${TMPDIR:-/tmp}/hf-xet-$(id -u)"
  echo "кэш xet $HF_HOME/xet недоступен на запись — временный: $HF_XET_CACHE" >&2
fi
free_gb="$(df -Pk "$HF_HOME" | awk 'NR==2 {print int($4 / 1024 / 1024)}')"
echo "профиль: $profile; кэш HF: $HF_HOME (свободно ~${free_gb} ГБ, нужно до ~${need_gb} ГБ без уже скачанного)"
if (( free_gb < need_gb )); then
  echo "ВНИМАНИЕ: места может не хватить (уже скачанные файлы повторно не качаются)" >&2
fi

for entry in "${repos[@]}"; do
  repo="${entry%@*}"; rev=()                             # repo@ревизия — закреплённая версия весов
  if [[ "$entry" == *@* ]]; then rev=(--revision "${entry##*@}"); fi
  echo "== $entry"
  # HF_HUB_OFFLINE=0: скачивание разрешено, даже если в окружении стоит офлайн-режим
  if (( ${#dry_run[@]} )); then
    HF_HUB_OFFLINE=0 "${HF[@]}" download "$repo" "${rev[@]}" "${dry_run[@]}"
    continue
  fi
  path="$(HF_HUB_OFFLINE=0 "${HF[@]}" download "$repo" "${rev[@]}" --quiet)"
  echo "   -> $path ($(du -shL "$path" | cut -f1))"
done
echo "== $CLS_REPO@$CLS_REV -> $CLS_DIR"
if (( ${#dry_run[@]} )); then
  HF_HUB_OFFLINE=0 "${HF[@]}" download "$CLS_REPO" --revision "$CLS_REV" --local-dir "$CLS_DIR" "${dry_run[@]}"
  exit 0
fi
mkdir -p "$CLS_DIR"
HF_HUB_OFFLINE=0 "${HF[@]}" download "$CLS_REPO" --revision "$CLS_REV" --local-dir "$CLS_DIR" --quiet >/dev/null
chmod -R a+rX "$CLS_DIR"                                  # worker (uid 1000) читает модель только на чтение
echo "   -> $CLS_DIR ($(du -sh "$CLS_DIR" | cut -f1); $(grep -o '"version": "[^"]*"' "$CLS_DIR/manifest.json"))"

echo "Готово. Запуск: docker compose $up up -d   (HF_HUB_OFFLINE=1 — vLLM берёт веса только из кэша)"
