"""HTML → чистый текст через DOM (BeautifulSoup), не регулярками
(промпт заказчика 2026-08-26, п.2: в таблицу попадает только чистый текст).

- .get_text() узлов, схлопывание пробелов, декодирование HTML-сущностей
  (unescape делает сам BeautifulSoup);
- строки таблиц <tr> → «ячейка | ячейка», чтобы табличный разбор
  экстрактора работал на DOM-таблицах прайсов;
- script/style/noscript/svg/template выбрасываются целиком.

Плюс имя организации (п.4): og:site_name → шапка сайта → (карточка
discovery и title — на стороне вызывающего).
"""

import re

from bs4 import BeautifulSoup

_WS_RE = re.compile(r"[ \t\xa0]+")
_DROP_TAGS = ("script", "style", "noscript", "svg", "template", "iframe", "head")


def looks_like_html(text: str) -> bool:
    head = text[:2000].lower()
    return "<html" in head or "<!doctype" in head or "<body" in head or "<div" in head


def _soup(html: str) -> BeautifulSoup:
    try:
        return BeautifulSoup(html, "lxml")
    except Exception:  # noqa: BLE001 — lxml не установлен/сломан
        return BeautifulSoup(html, "html.parser")


def html_to_text(content: str) -> str:
    """Текст, как его видит посетитель: построчно, таблицы — «a | b | c».
    НЕ-HTML (markdown от Jina, inner_text от Playwright) возвращается как есть."""
    if not looks_like_html(content):
        return content
    soup = _soup(content)
    for tag in soup.find_all(_DROP_TAGS):
        tag.decompose()
    for tr in soup.find_all("tr"):
        cells = [_WS_RE.sub(" ", c.get_text(" ", strip=True))
                 for c in tr.find_all(["td", "th"])]
        cells = [c for c in cells if c]
        if cells:
            tr.replace_with("\n" + " | ".join(cells) + "\n")
    # границы строк — ТОЛЬКО по блочным элементам: инлайновые (span, a, b)
    # не рвут строку, иначе цена отрывается от названия услуги
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for tag in soup.find_all(["p", "li", "h1", "h2", "h3", "h4", "h5", "h6",
                              "div", "dt", "dd", "section", "article",
                              "header", "footer", "nav", "ul", "ol", "table"]):
        tag.insert_before("\n")
        tag.append("\n")
    lines = []
    for raw in soup.get_text(" ").split("\n"):
        line = _WS_RE.sub(" ", raw).strip()
        if line:
            lines.append(line)
    return "\n".join(lines)


def site_name_from_html(content: str) -> tuple[str | None, str | None]:
    """(имя, источник): og:site_name → видимый текст/alt логотипа в шапке.
    Ничего не найдено → (None, None); title сюда НЕ входит — он последний
    вариант на стороне вызывающего и идёт с пометкой."""
    if not looks_like_html(content):
        return None, None
    soup = _soup(content)
    og = soup.find("meta", attrs={"property": "og:site_name"})
    if og and og.get("content", "").strip():
        return _WS_RE.sub(" ", og["content"]).strip()[:120], "og:site_name"
    header = soup.find("header") or soup.find(class_=re.compile(r"\bheader\b", re.I))
    if header:
        logo_img = header.find("img", alt=True)
        if logo_img and len(logo_img["alt"].strip()) >= 3:
            return _WS_RE.sub(" ", logo_img["alt"]).strip()[:120], "шапка сайта (alt логотипа)"
        for a in header.find_all("a", href=True):
            txt = _WS_RE.sub(" ", a.get_text(" ", strip=True))
            if 3 <= len(txt) <= 80 and a["href"].rstrip("/") in ("", "/", "#", "."):
                return txt, "шапка сайта"
    return None, None


def page_title(content: str) -> str | None:
    if looks_like_html(content):
        t = _soup(content).find("title")
        return _WS_RE.sub(" ", t.get_text(strip=True)).strip()[:200] if t else None
    m = re.match(r"Title:\s*(.+)", content)   # markdown от Jina Reader
    return m.group(1).strip()[:200] if m else None
