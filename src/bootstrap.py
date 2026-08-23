"""Этап 0 — проверка окружения перед любым прогоном.
Запуск: CITY='Казань' python src/bootstrap.py"""

import os

required = {
    "YANDEX_API_KEY":     "Яндекс Search API — основной поиск",
    "YANDEX_FOLDER_ID":   "Яндекс Search API — идентификатор каталога",
    "DADATA_API_KEY":     "DaData — проверка ИНН и реквизитов",
    "DADATA_SECRET_KEY":  "DaData — секретный ключ",
}
optional = {
    "PERPLEXITY_API_KEY": "Perplexity — резервный поисковый контур",
}

missing = [f"  ✗ {k} — {v}" for k, v in required.items() if not os.environ.get(k)]
if missing:
    print("СТОП. Отсутствуют обязательные ключи:\n" + "\n".join(missing))
    print("Добавь их в GitHub Secrets (Settings → Secrets → Actions)")
    exit(1)

city = os.environ.get("CITY", "").strip()
if not city:
    print("СТОП. Город не задан. Укажи его при запуске: CITY='Казань'")
    exit(1)

print(f"✓ Окружение проверено. Город: {city}")
