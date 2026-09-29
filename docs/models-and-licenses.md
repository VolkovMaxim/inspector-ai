# Модели и лицензии

Правило организаторов (Q&A №23): модели с запретом коммерческого или государственного использования допустимы только
как незачётный эксперимент. **Таких моделей в решении нет.**

| Модель | Где работает | Версия | Лицензия | Коммерческое и гос. использование |
|---|---|---|---|---|
| Qwen3.8-27B (Alibaba Cloud): веса `Qwen/Qwen3.8-27B-FP8` | `llm` (h100): vLLM, GPU | проверенная ревизия `017b9c7a…` (в compose не закреплена) | Apache-2.0 | да |
| Qwen3.8-27B: веса `unsloth/Qwen3.8-27B-NVFP4` — запасные | `llm`, если задать `LLM_MODEL_REPO` | проверенная ревизия `f0b7c9e7…` (в compose не закреплена) | Apache-2.0 | да |
| Классификатор типа страницы `page_cls_v2_lilt_rubert` (своя модель, [HF](https://huggingface.co/wolk1612/inspector-page-cls)) | worker, GPU | `@cc070ea3` | своя; базы — ниже | да |
| ├ `SCUT-DLVCLab/lilt-infoxlm-base` (макетная часть LiLT) | в модели | `9f07474e` | MIT | да |
| ├ `ai-forever/ruBert-base` | в модели | `05f37a2c` | Apache-2.0 | да |
| └ `google/siglip2-base-patch16-512` | worker, из кэша HF | `a89f5c50` (закреплена в prefetch) | Apache-2.0 | да |
| RapidOCR 3.9.2: PP-OCRv5 mobile — детектор `ch_PP-OCRv5_det_mobile`, распознаватель кириллицы `eslav_PP-OCRv5_rec_mobile`, `ch_ppocr_mobile_v2.0_cls` (PaddleOCR) | worker, GPU или CPU (ONNX Runtime GPU 1.23.2) | SHA-256 в `inspector/resources/rapidocr/SHA256SUMS` | Apache-2.0 | да |
| RapidOrientation 0.0.11 (PaddleClas PULC text_image_orientation) | worker, CPU | SHA-256 в `inspector/resources/orientation/SHA256SUMS` | Apache-2.0 | да |
| Отбор страниц F40 (своя модель: TF-IDF + логистическая регрессия, scikit-learn) | worker, CPU | `router@b6c996c9` | своя; scikit-learn — BSD-3-Clause | да |

Прочее ПО (не модели): vLLM 0.29.0 — Apache-2.0; torch 2.11 — BSD-3-Clause; transformers 5.17 — Apache-2.0;
ONNX Runtime — MIT; scikit-learn — BSD-3-Clause; LibreOffice — MPL-2.0; Swagger UI 5.33.0 — Apache-2.0; шрифты:
DejaVu — Bitstream Vera + public domain; Liberation, Carlito, Caladea, Golos Text, PT Mono — OFL-1.1. Библиотеки CUDA
в образе vLLM и в колёсах torch распространяются по лицензии NVIDIA. PyMuPDF — AGPL-3.0 (см. [ограничения](tz-deviations.md#ограничения-и-что-не-проверено)).
