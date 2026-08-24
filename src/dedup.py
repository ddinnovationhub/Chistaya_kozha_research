"""Нормализация и дедупликация G0.

Ключ: нормализованное название + нормализованный адрес/домен.
Нормализация: нижний регистр, удаление ОПФ (ООО/ИП/АО...) и слов
«клиника/центр/медицинский», латиница→кириллица (омоглифы).
Совпадение по названию без совпадения адреса ≠ дубль (статус «Требует
разрешения» присваивает entity resolution, этап 7).
"""

import re

_OPF = re.compile(r"\b(ооо|ип|ао|зао|оао|пао|нко|анo|ano)\b", re.IGNORECASE)
_STOPWORDS = re.compile(r"\b(клиника|центр|медицинский|медицинская|медицинское|мц|«|»)\b",
                        re.IGNORECASE)
# Латинские омоглифы → кириллица (ТЗ G0)
_HOMOGLYPHS = str.maketrans({
    "a": "а", "b": "в", "c": "с", "e": "е", "h": "н", "k": "к", "m": "м",
    "o": "о", "p": "р", "t": "т", "x": "х", "y": "у",
    "A": "а", "B": "в", "C": "с", "E": "е", "H": "н", "K": "к", "M": "м",
    "O": "о", "P": "р", "T": "т", "X": "х", "Y": "у",
})


def normalize_name(name: str) -> str:
    s = (name or "").lower().strip()
    s = s.translate(_HOMOGLYPHS)
    s = _OPF.sub(" ", s)
    s = _STOPWORDS.sub(" ", s)
    s = re.sub(r"[\"'«»„“”()\[\],.]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def normalize_domain(url_or_domain: str) -> str | None:
    if not url_or_domain:
        return None
    d = url_or_domain.lower().strip()
    d = re.sub(r"^https?://", "", d)
    d = d.split("/")[0].split(":")[0]
    return d.removeprefix("www.") or None
