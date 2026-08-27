"""Судьи-нейронки по «паспорту сайта» (заказчик, 2026-08-27: «мы используем
и яндекс и все прочие нейронки — это не опция, а обязательное условие»).

Судья получает ПАСПОРТ (только дословные фрагменты сайта, src/passport) и
возвращает строгий JSON: суждение А (4 исхода), профиль по специальностям,
основание-цитату. Правила судьи повторяют правила проекта: нет данных →
«не определено», основание — только цитата из паспорта, ничего не
домысливать.

Провайдеры:
- yandexgpt — Yandex Cloud Foundation Models (биллинг уже оплачен для
  Search API; подойдёт ли тот же Api-Key — проверяется пробой из Actions,
  при 401/403 модуль честно печатает «ключ не подходит»);
- groq / openrouter / cerebras — OpenAI-совместимые бесплатные тиры
  (каталог free-coding-models; ключи бесплатны, кладутся в Secrets).

ГЕЙТ (правило проекта): судья не допускается к прому, пока не прогнан на
эталоне (строки с уже проверенными суждениями) с приемлемым совпадением.
Ключей нет в среде → модуль работает только из GitHub Actions.
"""

import json
import os
import re
import sqlite3
import sys
import time

import httpx

JUDGE_PROMPT = """Ты — аналитик разведки рынка медицинских услуг. Ниже — \
«паспорт сайта» компании: дословные фрагменты её сайта (меню, заголовки, \
контакты, позиции прайса). Определи по НЕМУ:

1. "суждение_А" — ровно одно из: "медорганизация" | "управляющая компания \
сети клиник" | "не медорганизация" | "не определено".
   Правила: медорганизация — только при явных медицинских признаках \
(лицензия на МЕДИЦИНСКУЮ деятельность, приём врача, медицинские услуги в \
прайсе). Слово «лицензия» без медицинского контекста — НЕ признак. \
Недостаточно данных → "не определено", не догадка.
2. "профиль" — список направлений, явно видимых в паспорте (например: \
дерматология, косметология, стоматология, общая практика). Пустой список, \
если не видно.
3. "основание" — ДОСЛОВНАЯ цитата из паспорта (до 200 символов), \
подтверждающая суждение.

Ответ — ТОЛЬКО валидный JSON с этими тремя ключами, без пояснений.

Компания: {name}, город {city}.
ПАСПОРТ:
{passport}"""

PROVIDERS = {
    # OpenAI-совместимый chat.completions; модели — бесплатные тиры.
    # keyless: работают ВООБЩЕ без ключа (проверено живьём 2026-08-27:
    # kilo-auto/free и llm7 minimax-m2.7 ответили 200 с корректным JSON
    # из песочницы) — общие пулы с плавающими лимитами, поэтому пауза
    # больше и обязателен гейт на эталоне.
    "kilo": {
        "url": "https://api.kilo.ai/api/gateway/chat/completions",
        "key_env": None,                      # ключ не нужен
        "model": "kilo-auto/free",
    },
    "llm7": {
        "url": "https://api.llm7.io/v1/chat/completions",
        "key_env": "LLM7_API_KEY",            # опционален (token.llm7.io)
        "keyless_ok": True,
        "model": "minimax-m2.7",
        # рассуждающая модель: «размышления» тарифицируются в max_tokens;
        # 700 съедалось reasoning'ом и контент приходил пустым (тест-40)
        "max_tokens": 4000,
    },
    "groq": {
        "url": "https://api.groq.com/openai/v1/chat/completions",
        "key_env": "GROQ_API_KEY",
        "model": "llama-3.3-70b-versatile",
    },
    "openrouter": {
        "url": "https://openrouter.ai/api/v1/chat/completions",
        "key_env": "OPENROUTER_API_KEY",
        "model": "meta-llama/llama-3.3-70b-instruct:free",
    },
    "cerebras": {
        "url": "https://api.cerebras.ai/v1/chat/completions",
        "key_env": "CEREBRAS_API_KEY",
        "model": "llama-3.3-70b",
    },
}

VALID_A = {"медорганизация", "управляющая компания сети клиник",
           "не медорганизация", "не определено"}


def _parse_judge_json(text: str) -> dict | None:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        j = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    if j.get("суждение_А") not in VALID_A:
        return None
    return {"суждение_А": j["суждение_А"],
            "профиль": j.get("профиль") or [],
            "основание": str(j.get("основание") or "")[:300]}


def judge_openai_compat(provider: str, name: str, city: str,
                        passport: str) -> dict | None:
    cfg = PROVIDERS[provider]
    key = os.environ.get(cfg["key_env"]) if cfg["key_env"] else None
    if not key and cfg["key_env"] and not cfg.get("keyless_ok"):
        print(f"⛔ {provider}: нет ключа {cfg['key_env']} в окружении")
        return None
    r = httpx.post(cfg["url"], timeout=90,
                   headers={"Authorization": f"Bearer {key}"} if key else {},
                   json={"model": cfg["model"], "temperature": 0,
                         "max_tokens": cfg.get("max_tokens", 700),
                         "messages": [{"role": "user", "content":
                                       JUDGE_PROMPT.format(
                                           name=name, city=city,
                                           passport=passport[:12000])}]})
    if r.status_code in (401, 403):
        print(f"⛔ {provider}: ключ не подходит (код {r.status_code})")
        return None
    if r.status_code == 429:
        print(f"⚠ {provider}: лимит, жду 30 с"); time.sleep(30)
        return judge_openai_compat(provider, name, city, passport)
    if r.status_code != 200:
        print(f"⚠ {provider}: код {r.status_code}")
        return None
    content = r.json()["choices"][0]["message"]["content"] or ""
    res = _parse_judge_json(content)
    if res is None:   # диагностика: молчаливый отказ парсера скрывал причину
        print(f"⚠ {provider}: ответ не распознан ({len(content)} симв.): "
              f"{content[:120]!r}")
    return res


def judge_yandexgpt(name: str, city: str, passport: str) -> dict | None:
    key = os.environ.get("YANDEX_API_KEY")
    folder = os.environ.get("YANDEX_FOLDER_ID")
    if not (key and folder):
        print("⛔ yandexgpt: нет YANDEX_API_KEY/YANDEX_FOLDER_ID")
        return None
    r = httpx.post(
        "https://llm.api.cloud.yandex.net/foundationModels/v1/completion",
        timeout=90,
        headers={"Authorization": f"Api-Key {key}"},
        json={"modelUri": f"gpt://{folder}/yandexgpt-lite",
              "completionOptions": {"temperature": 0, "maxTokens": "600"},
              "messages": [{"role": "user", "text": JUDGE_PROMPT.format(
                  name=name, city=city, passport=passport[:12000])}]})
    if r.status_code in (401, 403):
        print(f"⛔ yandexgpt: ключ Search API не подходит для Foundation "
              f"Models (код {r.status_code}) — нужен ключ сервисного "
              f"аккаунта с ролью ai.languageModels.user")
        return None
    if r.status_code != 200:
        print(f"⚠ yandexgpt: код {r.status_code}: {r.text[:200]}")
        return None
    alts = r.json().get("result", {}).get("alternatives", [])
    return _parse_judge_json(alts[0]["message"]["text"]) if alts else None


def judge(provider: str, name: str, city: str, passport: str) -> dict | None:
    if provider == "yandexgpt":
        return judge_yandexgpt(name, city, passport)
    return judge_openai_compat(provider, name, city, passport)


def ensure_tables(db: sqlite3.Connection):
    db.execute("""CREATE TABLE IF NOT EXISTS llm_judgments (
        inn TEXT, provider TEXT, judgment TEXT, profile TEXT, basis TEXT,
        judged_at TEXT, PRIMARY KEY (inn, provider))""")
    db.commit()


def run_provider(db: sqlite3.Connection, provider: str,
                 table: str = "t40_companies", limit: int = 1000) -> dict:
    """Судья по всем строкам таблицы с паспортом. Пауза 2 с (бесплатные
    тиры ограничены по RPM)."""
    import datetime
    ensure_tables(db)
    rows = list(db.execute(
        f"SELECT c.inn, c.name, c.city, c.passport FROM {table} c "
        f"LEFT JOIN llm_judgments j ON j.inn=c.inn AND j.provider=? "
        f"WHERE c.passport IS NOT NULL AND j.inn IS NULL LIMIT ?",
        (provider, limit)))
    stats = {"done": 0, "failed": 0}
    for inn, name, city, passport in rows:
        res = judge(provider, name, city, passport)
        if res is None:
            stats["failed"] += 1
            if stats["failed"] >= 3 and stats["done"] == 0:
                stats["стоп"] = "провайдер не отвечает/ключ не подходит"
                break
            continue
        db.execute("INSERT OR REPLACE INTO llm_judgments VALUES (?,?,?,?,?,?)",
                   (inn, provider, res["суждение_А"],
                    ", ".join(map(str, res["профиль"])), res["основание"],
                    datetime.datetime.now().isoformat(timespec="seconds")))
        db.commit()
        stats["done"] += 1
        # бесключевые — общие пулы: пауза больше, чтобы не выжигать лимит
        time.sleep(5 if provider in ("kilo", "llm7") else 2)
    return stats


def gate_report(db: sqlite3.Connection, provider: str,
                table: str = "t40_companies") -> dict:
    """ГЕЙТ: совпадение судьи с проверенными суждениями таблицы.
    Совпадение по суждению А; расхождения — списком на разбор."""
    rows = list(db.execute(
        f"SELECT c.inn, c.name, c.med_judgment, j.judgment, j.basis "
        f"FROM {table} c JOIN llm_judgments j ON j.inn=c.inn "
        f"WHERE j.provider=? AND c.med_judgment IS NOT NULL", (provider,)))
    same = [r for r in rows if r[2] == r[3]]
    diff = [r for r in rows if r[2] != r[3]]
    print(f"ГЕЙТ {provider}: сравнимо {len(rows)}, совпало {len(same)}, "
          f"разошлось {len(diff)}")
    for r in diff:
        print(f"  РАСХОЖДЕНИЕ {r[1][:34]}: правила «{r[2]}» vs судья «{r[3]}» "
              f"— {r[4][:100]}")
    return {"compared": len(rows), "same": len(same), "diff": len(diff)}


if __name__ == "__main__":
    con = sqlite3.connect("data/osint.db")
    con.execute("PRAGMA busy_timeout=15000")
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "run":
        prov = sys.argv[2]
        print(f"судья {prov}:", run_provider(con, prov))
    elif cmd == "gate":
        prov = sys.argv[2]
        gate_report(con, prov)
    else:
        print("команды: run <kilo|llm7|yandexgpt|groq|openrouter|cerebras> | "
              "gate <провайдер>  (kilo и llm7 — без ключа)")
