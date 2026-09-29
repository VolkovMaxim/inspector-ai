"""Поддельная модель (OpenAI-совместимый /chat/completions через httpx.MockTransport) для тестов конвейера без GPU.
Отвечает правдоподобно по содержимому запроса: класс страницы, извлечение классов арматуры/бетона, судья, верификатор, OCR."""
from __future__ import annotations

import json
import re
from collections import Counter

import httpx

REBAR = re.compile(r"(?<![А-ЯA-Z])[АA]\s?(240|400|500)С?")
CONCRETE = re.compile(r"(?<![А-ЯA-Z])[ВB](\d{2})(?!\d)")


class FakeLLM:
    def __init__(self, ocr_text: str = "", fail: bool = False, fail_on: set[str] | None = None):
        self.ocr_text = ocr_text
        self.fail = fail
        self.fail_on = fail_on or set()
        self.calls: Counter = Counter()

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.fail:
            return httpx.Response(503, json={"error": "down"})
        body = json.loads(request.content)
        rf = body.get("response_format")
        name = rf["json_schema"]["name"] if rf else "ocr"
        self.calls[name] += 1
        if name in self.fail_on:
            return httpx.Response(503, json={"error": "down"})
        content = {"page": self.page, "extraction": self.extract, "judge": self.judge, "verify": self.verify,
                   "ocr": self.ocr}[name](body)
        return httpx.Response(200, json={"choices": [{"message": {"content": content}, "finish_reason": "stop"}],
                                         "usage": {"prompt_tokens": 100, "completion_tokens": 20}})

    @staticmethod
    def _text(body: dict) -> str:
        parts = body["messages"][-1]["content"]
        if isinstance(parts, str):
            return parts
        return "\n".join(p.get("text", "") for p in parts if p.get("type") == "text")

    def page(self, body: dict) -> str:
        text = self._text(body)
        low = text.lower()
        if "акт" in low and "освидетельствован" in low:
            label = ("АОСР", True, "НЕТ")
        elif "экспликаци" in low:
            label = ("ТАБЛИЦА", True, "АР")
        elif "арматур" in low or "бетон" in low:
            label = ("ТАБЛИЦА", True, "КР")
        else:
            label = ("ТЕКСТ", True, "ПЗ" if "пояснительн" in low else "НЕТ")
        title = next((l.strip() for l in text.splitlines()[1:] if l.strip()), "")[:100]
        return json.dumps({"title": title, "main_class": label[0], "other_classes": [], "is_first_page": label[1],
                           "section": label[2], "quality": "хорошее"}, ensure_ascii=False)

    def extract(self, body: dict) -> str:
        user = self._text(body)                              # с картинкой страницы content — список частей
        codes = re.findall(r"^- ([A-Z0-9]+-\d{3}):", user, flags=re.M)
        page = user.split("СТРАНИЦА:\n", 1)[-1]
        items = []
        for line in page.splitlines():
            loc = "Форшахта" if "форшахт" in line.lower() or "форшахт" in page.lower() else "Объект в целом"
            if "KR-057" in codes:
                for m in REBAR.finditer(line):
                    items.append({"parameter_code": "KR-057", "location": loc, "value_raw": m.group(0),
                                  "snippet": line.strip()[:120], "confidence": 0.9})
            if "KR-055" in codes:
                for m in CONCRETE.finditer(line):
                    items.append({"parameter_code": "KR-055", "location": "Фундаментная плита",
                                  "value_raw": m.group(0), "snippet": line.strip()[:120], "confidence": 0.9})
        return json.dumps({"items": items}, ensure_ascii=False)

    def judge(self, body: dict) -> str:
        user = body["messages"][-1]["content"]
        rows = re.findall(r"^\[(\d+)\] (PD|RD|ID)[^|]*\| ([^|]*)\| ([^|]*)\|", user, flags=re.M)
        first = {}
        for idx, stage, loc, value in rows:
            first.setdefault(stage, (int(idx), loc.strip(), value.strip()))
        if "PD" not in first:
            return json.dumps({"groups": []})
        num = lambda v: int(re.search(r"\d+", v).group(0)) if re.search(r"\d+", v) else 0  # noqa: E731
        pd = first["PD"]
        worse = [s for s in ("RD", "ID") if s in first and num(first[s][2]) < num(pd[2])]
        verdict = "VIOLATION" if worse else "NO_VIOLATION"
        return json.dumps({"groups": [{"location": pd[1], "pd_index": pd[0],
                                       "rd_index": first["RD"][0] if "RD" in first else None,
                                       "id_index": first["ID"][0] if "ID" in first else None,
                                       "reason": f"ПД {pd[2]}; сравнение по триггеру", "verdict": verdict}]},
                          ensure_ascii=False)

    def verify(self, body: dict) -> str:
        return json.dumps({"pd_entity": "форшахта", "other_entity": "форшахта", "analysis": "та же конструкция",
                           "same_entity": "YES", "trigger_met": "YES"}, ensure_ascii=False)

    def ocr(self, body: dict) -> str:
        return self.ocr_text
