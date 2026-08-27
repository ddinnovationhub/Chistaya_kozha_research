"""«Паспорт сайта» — компактное досье компании для ИНТЕЛЛЕКТУАЛЬНОГО
суждения (заказчик, 2026-08-27: «меня не устраивает, что работает
исключительно код… задача весьма интеллектуальная»).

Паспорт собирается кодом, но НЕ выносит суждений: только дословные
фрагменты сайта с URL. Судья (агент в сессии или нейронка с бесплатным
API-тиром) читает паспорт вместо сырого сайта — ~4К токенов вместо
десятков страниц. Состав (разбор шагов 1-4, 2026-08-27):

- title + meta description главной;
- ПОЛНОЕ МЕНЮ — тексты всех ссылок навигации (главный сигнал профиля:
  «Дерматология / Стоматология / УЗИ» видно из меню без обхода разделов);
- заголовки h1-h2 всех скачанных страниц;
- контакт-блок: строки с ИНН/ОГРН/адресом/лицензией — дословно;
- ссылки на прайс-файлы (pdf/xls);
- специальности врачей и профиль-маркеры, найденные экстрактором;
- первые позиции прайса (название — цена).

Все фрагменты — цитаты. Паспорт без единого фрагмента честно говорит
«пусто», судья обязан вернуть «не определено», не догадку.
"""

import re

from src.html_text import _soup, html_to_text, looks_like_html, page_title

_MENU_JUNK_RE = re.compile(
    r"^(|#|›|»|→|\.{2,}|ru|en|войти|личный кабинет|поиск|наверх|меню"
    r"|версия для слабовидящих|карта сайта|подробнее|ещё|еще|читать)$",
    re.IGNORECASE)

_CONTACT_LINE_RE = re.compile(
    r"ИНН|ОГРН|КПП|лиценз|\bг\.\s|\bул\.|улица|проспект|переулок|шоссе"
    r"|бульвар|офис \d|режим работы", re.IGNORECASE)

_PRICE_FILE_RE = re.compile(r"\.(pdf|xlsx?|docx?)(\?|$)", re.IGNORECASE)
_PRICE_WORD_RE = re.compile(r"прайс|цен[ыа]|стоимост|price", re.IGNORECASE)


def _meta_description(html: str) -> str | None:
    if not looks_like_html(html):
        return None
    m = _soup(html).find("meta", attrs={"name": "description"})
    return (m.get("content") or "").strip()[:300] or None if m else None


def menu_texts(html: str, cap: int = 120) -> list[str]:
    """Тексты ссылок главной: сперва nav/header/menu, затем остальные.
    Меню — главный сигнал профиля; NAV_JUNK экстрактора его режет,
    здесь оно собирается ЦЕЛИКОМ (решение разбора 2026-08-27)."""
    if not looks_like_html(html):
        return []
    soup = _soup(html)
    zones = soup.find_all(["nav", "header"]) + soup.find_all(
        class_=re.compile(r"menu|nav", re.I))
    out, seen = [], set()

    def _take(a_tags):
        for a in a_tags:
            t = re.sub(r"\s+", " ", a.get_text(" ", strip=True)).strip()
            key = t.lower()
            if (2 <= len(t) <= 60 and key not in seen
                    and not _MENU_JUNK_RE.match(key)):
                seen.add(key)
                out.append(t)

    for z in zones:
        _take(z.find_all("a", href=True))
    _take(soup.find_all("a", href=True))
    return out[:cap]


def headings(html: str, cap: int = 20) -> list[str]:
    if not looks_like_html(html):
        return []
    out = []
    for h in _soup(html).find_all(["h1", "h2"]):
        t = re.sub(r"\s+", " ", h.get_text(" ", strip=True)).strip()
        if 3 <= len(t) <= 150:
            out.append(t)
        if len(out) >= cap:
            break
    return out


def contact_lines(text: str, cap: int = 12) -> list[str]:
    out = []
    for line in text.splitlines():
        s = line.strip()
        if 5 <= len(s) <= 250 and _CONTACT_LINE_RE.search(s):
            out.append(s)
        if len(out) >= cap:
            break
    return out


def price_file_links(html: str, base_domain: str, cap: int = 5) -> list[str]:
    if not looks_like_html(html):
        return []
    out, seen = [], set()
    for a in _soup(html).find_all("a", href=True):
        href = a["href"]
        label = a.get_text(" ", strip=True) or ""
        if _PRICE_FILE_RE.search(href) and (
                _PRICE_WORD_RE.search(label) or _PRICE_WORD_RE.search(href)):
            if href not in seen:
                seen.add(href)
                out.append(f"{label[:60]} → {href[:200]}")
        if len(out) >= cap:
            break
    return out


def build_passport(domain: str, pages: dict[str, str], data: dict,
                   max_chars: int = 16000) -> str:
    """{url: html} + сигналы extract_pages → текст паспорта. Дословно, с URL."""
    urls = list(pages)
    home_url = urls[0] if urls else ""
    home = pages.get(home_url, "")
    lines = [f"САЙТ: {domain} (страниц скачано: {len(pages)})"]
    t = page_title(home)
    if t:
        lines.append(f"TITLE: {t}")
    d = _meta_description(home)
    if d:
        lines.append(f"DESCRIPTION: {d}")

    menu = menu_texts(home)
    lines.append("\nМЕНЮ САЙТА (тексты ссылок главной, дословно):")
    lines.append(" | ".join(menu) if menu else "(ссылок не найдено)")

    lines.append("\nЗАГОЛОВКИ h1-h2 (по страницам):")
    any_h = False
    for u, raw in pages.items():
        hs = headings(raw)
        if hs:
            any_h = True
            lines.append(f"  {u}: " + " | ".join(hs))
    if not any_h:
        lines.append("  (заголовков не найдено)")

    lines.append("\nКОНТАКТ-БЛОК (строки с ИНН/ОГРН/адресом/лицензией, дословно):")
    any_c = False
    for u, raw in pages.items():
        cl = contact_lines(html_to_text(raw))
        if cl:
            any_c = True
            lines.append(f"  {u}:")
            lines.extend(f"    «{s}»" for s in cl)
    if not any_c:
        lines.append("  (не найдено)")

    pf = []
    for u, raw in pages.items():
        pf.extend(price_file_links(raw, domain))
    if pf:
        lines.append("\nПРАЙС-ФАЙЛЫ (ссылки):")
        lines.extend(f"  {s}" for s in pf[:5])

    if data.get("doctor_specialties"):
        lines.append("\nСПЕЦИАЛЬНОСТИ ВРАЧЕЙ (слова, найденные на страницах): "
                     + ", ".join(data["doctor_specialties"]))
    if data.get("profile_markers_found"):
        lines.append("ПРОФИЛЬ-МАРКЕРЫ ДЕРМ-КОНТУРА: "
                     + ", ".join(data["profile_markers_found"]))
    lic = data.get("license_evidence") or {}
    if lic.get("found"):
        lines.append(f"ЛИЦЕНЗИЯ (строка с сайта): «{lic['quote']}» ({lic['url']})")
    svcs = data.get("services") or []
    if svcs:
        lines.append(f"\nПОЗИЦИИ ПРАЙСА (первые {min(len(svcs), 30)} из {len(svcs)}):")
        for s in svcs[:30]:
            price = f" — {s['price']}" if s.get("price") else ""
            lines.append(f"  {s['name'][:120]}{price}")
    text = "\n".join(lines)
    return text[:max_chars]
