"""Этап 6 — проверяющий сайты (consumer), по утверждённому промпту
prompts/06_site_checker.md + промпту исправления 2026-08-26 (п.6:
БЕЗ внешнего API — вызовов к моделям нет ни одного).

Схема (решение заказчика 2026-08-26, п.6):
1. Пайплайн скачивает страницы в raw/ — как раньше.
2. Кодовая экстракция сигналов (src/extract_site.py) + ступень 1 маппинга
   кодом (точное совпадение со справочником).
3. Всё, что ступень 1 не закрыла, помечается «на разметке» и выгружается в
   output/{город}_на_разметку_{дата}.json (src/export_stage6.export_markup).
4. Заказчик размечает батчи в Claude Code (prompts/06_markup_batch.md) →
   output/{город}_разметка_{дата}.json.
5. src/merge_markup.py подхватывает разметку, финализирует классификацию.

До слияния разметки тип клиники — ПРЕДВАРИТЕЛЬНЫЙ (по ступени 1): часть
услуг ещё не размечена, состав контуров неполон. Финальный тип ставит
merge_markup после разметки (G4: классификация после просмотра, не раньше).

Обязательная остановка (п.8): первые 10 клиник → стоп, промежуточная
выгрузка, ждать решения заказчика. Домен обрабатывается одним consumer'ом
ровно один раз за прогон; внутри обхода — несколько страниц.
"""

import datetime
import gzip
import pathlib
import re
import sqlite3
import time

import yaml

from src.classify import load_contours
from src.dedup import normalize_domain
from src.extract_site import extract_pages
from src.fetch_cascade import ensure_fetch_tables, fetch_cascade
from src.mapper import build_formulation_index, map_tier1
from src.validators import validate_inn, validate_ogrn

RATE_DELAY_SEC = 3
MAX_PAGES_PER_SITE = 40   # потолок (заказчик 2026-08-26, п.5); правится в
                          # config/thresholds.yaml cascade.max_pages_per_domain

TYPE_STATUS_PRELIM = "предварительный (ступень 1, до разметки)"
TYPE_STATUS_FINAL = "финальный (после разметки)"

# Приоритет обхода (заказчик 2026-08-26, п.5): сначала прайс/каталог услуг,
# затем направления/специалисты, затем лицензии/реквизиты/контакты
SERVICE_HINTS = ("price", "prais", "prays", "stoimost", "ceny", "cena", "uslug",
                 "servic", "прайс", "стоимост", "цены", "услуг")
DIRECTION_HINTS = ("napravlen", "specialist", "vrach", "doctor", "направлен",
                   "специалист", "врач")
OTHER_HINTS = ("licen", "rekvizit", "kontakt", "contact", "about", "o-klinike",
               "o-nas", "лиценз", "реквизит", "контакт")


def _link_priority(url: str, text: str) -> int | None:
    """0 — прайс/услуги, 1 — направления/специалисты, 2 — прочие полезные,
    None — не обходить."""
    probe = (url + " " + text).lower()
    if any(h in probe for h in SERVICE_HINTS):
        return 0
    if any(h in probe for h in DIRECTION_HINTS):
        return 1
    if any(h in probe for h in OTHER_HINTS):
        return 2
    return None


def _page_links(page_text: str, base_url: str) -> list[tuple[str, str]]:
    """(url, текст ссылки) со страницы: HTML — через DOM, markdown — регэкспом."""
    from urllib.parse import urljoin

    from src.html_text import _soup, looks_like_html
    out = []
    if looks_like_html(page_text):
        for a in _soup(page_text).find_all("a", href=True):
            out.append((urljoin(base_url + "/", a["href"]),
                        a.get_text(" ", strip=True)[:80]))
    else:
        out = [(urljoin(base_url + "/", u), "") for u in
               re.findall(r"\((https?://[^)\s]+|/[^)\s]+)\)", page_text)]
    return out


def crawl_site(url: str, city: str, form_index: dict, db=None,
               min_bytes: int = 3000,
               max_pages: int = MAX_PAGES_PER_SITE,
               page_budget_sec: float = 240,
               clinic_budget_sec: float = 600) -> tuple[dict[str, str], dict]:
    """АДАПТИВНЫЙ обход домена через каскад (заказчик 2026-08-26, п.5):
    обходятся ВСЕ страницы, похожие на прайс/каталог услуг, в порядке
    приоритета (прайс → направления/специалисты → прочее), плюс второй
    уровень вложенности внутри разделов услуг. Потолок max_pages, пауза
    между запросами. Телеметрия: pages_found / pages_fetched / cap_hit.
    Уровень клиники = уровень, взявший главную."""
    import heapq

    pages = {}
    dom = normalize_domain(url) or "unknown"
    base = url.rstrip("/")
    t0 = time.monotonic()
    home, meta = fetch_cascade(url, dom, form_index, db=db, min_bytes=min_bytes,
                               page_budget_sec=page_budget_sec)
    meta.update(pages_found=0, pages_fetched=0, cap_hit=False, timeout_hit=False)
    if home is None:
        return pages, meta
    pages[url] = home
    page_level = max(meta["level"], 2)

    def norm_link(u: str) -> str:
        return u.split("#")[0].split("?")[0].rstrip("/")

    heap, seen, order = [], {base}, 0
    def enqueue(links, from_service_page: bool, service_root: str | None):
        nonlocal order
        for lurl, ltext in links:
            key = norm_link(lurl)
            if not key.startswith("http") or normalize_domain(key) != dom or key in seen:
                continue
            prio = _link_priority(key, ltext)
            # второй уровень: подстраницы внутри раздела услуг обходятся,
            # даже если их URL не содержит подсказок (направления прайса)
            if prio is None and from_service_page and service_root \
                    and key.startswith(service_root):
                prio = 0
            if prio is None:
                continue
            seen.add(key)
            heapq.heappush(heap, (prio, order, key))
            order += 1

    enqueue(_page_links(home, base), from_service_page=False, service_root=None)
    while heap and len(pages) < max_pages:
        if time.monotonic() - t0 > clinic_budget_sec:
            # таймаут клиники (разбор 2026-08-26): прерываем, собранное остаётся
            meta["timeout_hit"] = True
            break
        prio, _, link = heapq.heappop(heap)
        time.sleep(RATE_DELAY_SEC)
        text, _m = fetch_cascade(link, dom, form_index, db=db,
                                 min_bytes=min_bytes, max_level=page_level,
                                 page_budget_sec=page_budget_sec)
        if not text:
            continue
        pages[link] = text
        if prio == 0:   # страница услуг/прайса → её подстраницы тоже в очередь
            enqueue(_page_links(text, base), from_service_page=True,
                    service_root=link)
    meta["pages_found"] = len(seen) - 1
    meta["pages_fetched"] = len(pages)
    meta["cap_hit"] = bool(heap) and len(pages) >= max_pages
    # доказательная база: gzip в raw/{city}/{date}/
    day = datetime.date.today().isoformat()
    raw_dir = pathlib.Path("raw") / city / day
    raw_dir.mkdir(parents=True, exist_ok=True)
    for i, (u, txt) in enumerate(pages.items()):
        (raw_dir / f"{dom}_{i}.md.gz").write_bytes(gzip.compress(txt.encode("utf-8")))
    return pages, meta


def ensure_stage6_tables(db: sqlite3.Connection):
    db.executescript("""
    CREATE TABLE IF NOT EXISTS clinics (
        clinic_id TEXT PRIMARY KEY, title TEXT, title_source TEXT,
        ownership_form TEXT,
        domain TEXT, url TEXT,
        gate TEXT, gate_reason TEXT, type TEXT, type_status TEXT, rule TEXT,
        grade TEXT, esthetic_markers TEXT, nonadjacent TEXT,
        flag_single_nonadjacent INTEGER, flag_removal_outside_derm INTEGER,
        flag_site_unreachable INTEGER, unreachable_note TEXT, fetch_level INTEGER,
        nonprofile_excluded INTEGER,
        crawl_pages_found INTEGER, crawl_pages_fetched INTEGER, crawl_cap_hit INTEGER,
        has_packages INTEGER, specialists_count INTEGER,
        inn TEXT, inn_status TEXT, legal_name TEXT,
        sections_found TEXT, checked_at TEXT);
    CREATE TABLE IF NOT EXISTS services_found (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        clinic_id TEXT, clinic_title TEXT,
        name_raw TEXT, description_raw TEXT, page_url TEXT, price TEXT,
        row_type TEXT, gate_anchor_hit INTEGER,
        profile TEXT, collapsed_into INTEGER,
        tag TEXT, code_804n TEXT, mapping_basis TEXT, mapping_tier TEXT,
        confidence TEXT, client_has TEXT);
    CREATE TABLE IF NOT EXISTS clinic_evidence (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        clinic_id TEXT, kind TEXT, detail TEXT, quote TEXT, url TEXT);
    """)
    # миграция таблиц, созданных кодом до 2026-08-26
    for col, typ in (("type_status", "TEXT"), ("flag_site_unreachable", "INTEGER"),
                     ("unreachable_note", "TEXT"), ("fetch_level", "INTEGER"),
                     ("title_source", "TEXT"), ("nonprofile_excluded", "INTEGER"),
                     ("crawl_pages_found", "INTEGER"),
                     ("crawl_pages_fetched", "INTEGER"),
                     ("crawl_cap_hit", "INTEGER"),
                     ("ownership_form", "TEXT"),
                     # разделение сбора и суждений + ворота-мера (2026-08-25)
                     ("gate_profile_rows", "INTEGER"),
                     ("gate_total_rows", "INTEGER"),
                     ("gate_profile_doctor", "TEXT"),
                     ("gate_esth_units", "INTEGER"),
                     ("price_positions_found", "INTEGER"),
                     ("agg_collapsed", "INTEGER"),
                     ("nonadj_skipped", "INTEGER")):
        try:
            db.execute(f"ALTER TABLE clinics ADD COLUMN {col} {typ}")
        except sqlite3.OperationalError:
            pass
    for col, typ in (("row_type", "TEXT"), ("gate_anchor_hit", "INTEGER"),
                     ("profile", "TEXT"), ("collapsed_into", "INTEGER")):
        try:
            db.execute(f"ALTER TABLE services_found ADD COLUMN {col} {typ}")
        except sqlite3.OperationalError:
            pass
    ensure_fetch_tables(db)
    db.commit()


def process_clinic(cand: dict, db: sqlite3.Connection, contours: dict,
                   form_index: dict, client_tags: set, city: str,
                   min_bytes: int = 3000,
                   max_pages: int = MAX_PAGES_PER_SITE) -> dict:
    dom = cand["domain"]
    clinic_id = f"КЛН-{dom}"
    # Обход С КОРНЯ домена (такт 3: discovery даёт глубокие URL — новость
    # sharmnsk.ru была запрещена robots, и запрет ошибочно закрывал весь сайт;
    # плюс og:site_name и шапка живут на главной)
    url = f"https://{dom}"
    _casc = yaml.safe_load(pathlib.Path("config/thresholds.yaml")
                           .read_text(encoding="utf-8")).get("cascade", {})
    pages, fetch_meta = crawl_site(
        url, city, form_index, db=db, min_bytes=min_bytes, max_pages=max_pages,
        page_budget_sec=float(_casc.get("page_budget_sec", 240)),
        clinic_budget_sec=float(_casc.get("clinic_budget_sec", 600)))
    disc_url = cand.get("url") or ""
    if pages and disc_url.startswith("http") \
            and normalize_domain(disc_url) == dom \
            and disc_url.rstrip("/") != url.rstrip("/") and disc_url not in pages:
        from src.fetch_cascade import fetch_cascade
        time.sleep(RATE_DELAY_SEC)
        extra, _ = fetch_cascade(disc_url, dom, form_index, db=db,
                                 min_bytes=min_bytes,
                                 max_level=max(fetch_meta["level"] or 2, 2))
        if extra:
            pages[disc_url] = extra
    now = datetime.datetime.now().isoformat(timespec="seconds")

    if not pages:
        # Не взял ни один уровень каскада (п.4, 2026-08-26): строка НЕ прячется —
        # название/домен из discovery, услуги «Сайт недоступен», грейд C, флаг
        note = (f"robots.txt запрещает обход" if fetch_meta["blocked_by_robots"]
                else f"последний уровень: {fetch_meta['last_level']}, "
                     f"ответ: {fetch_meta['last_status']}") + f", {now[:10]}"
        db.execute("INSERT OR REPLACE INTO clinics (clinic_id, title, title_source, "
                   "domain, url, gate, gate_reason, type, type_status, rule, grade, "
                   "flag_site_unreachable, unreachable_note, fetch_level, inn_status, "
                   "checked_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (clinic_id, cand["title"], "карточка discovery", dom, url,
                    "Требует проверки", "Сайт недоступен", "Не классифицировано", None,
                    None, "C", 1, note, None, "Сайт недоступен", now))
        db.commit()
        return {"clinic_id": clinic_id, "title": cand["title"],
                "gate": "Требует проверки", "reason": "Сайт недоступен",
                "grade": "C", "services": 0, "tier1_mapped": 0, "to_markup": 0,
                "fetch_level": None, "unreachable": True, "domain": dom}

    # Экстракция — анализ страниц в памяти; ЗАПИСЬ строк услуг — только
    # после ворот (порядок заказчика 2026-08-26: скачать → G1 → G2 →
    # только если прошла — извлекать услуги в таблицу)
    data = extract_pages(pages, form_index)

    # ── Имя организации (п.4 + разбор 2026-08-26): заголовок страницы —
    # НЕ имя. Порядок: og:site_name → шапка → юрлицо из реквизитов →
    # карточка discovery (если не заголовкоподобна) → домен с «Уточнить»
    from src.extract_site import looks_like_headline
    if data["site_name"] and data["site_name"].strip().lower() == city.strip().lower():
        data["site_name"] = None   # из шапки взялся переключатель города
    if data["site_name"] and not looks_like_headline(data["site_name"]):
        title, title_source = data["site_name"], data["site_name_source"]
    elif data["requisites"]["legal_name"]:
        title, title_source = data["requisites"]["legal_name"], "юрлицо из реквизитов сайта"
    elif cand.get("title") and not looks_like_headline(cand["title"]):
        title, title_source = cand["title"], "карточка discovery"
    else:
        title, title_source = dom, "домен — Уточнить (название-заголовок отвергнуто)"

    # ── СБОР, а не суждения (заказчик, 2026-08-25, часть 2): этап 6 пишет
    # только сырое — позиция (название/описание/цена/URL), «Тип строки»,
    # телеметрия. Маппинг, теги, свёртка в агрегаты, тип клиники, маркеры,
    # флаги, грейд — отдельный пересчитываемый шаг src/judgments.py (по базе,
    # без повторного обхода). Словарь здесь используется ТОЛЬКО для ворот
    # организации и исключения-по-словарю в фильтре страниц; результат
    # маппинга не персистится.
    # Внутри прошедшей ворота клиники не выбрасывается НИ ОДНА позиция ни по
    # какому признаку (второй фильтр отменён). Единственное отсечение —
    # фильтр СТРАНИЦ несмежных разделов (стоматология/ДНК naedine): это
    # фильтр страниц при обходе, не позиций.
    from src.extract_site import (BRAND_INJECTABLE_RE, CONSUMABLE_RE,
                                  DESCRIPTIVE_TEXT_RE, NONADJ_PAGE_URL_RE,
                                  PACKAGE_RE, PROFILE_ANCHOR_RE,
                                  _esthetic_keywords, is_esthetic_line,
                                  is_zone_or_junk_name,
                                  profile_doctor_visit_in_services)
    cfg_all = yaml.safe_load(pathlib.Path("config/thresholds.yaml").read_text(encoding="utf-8"))
    fuzzy_cutoff = float(cfg_all.get("mapping", {}).get("fuzzy_threshold", 0)) or None
    g2cfg = cfg_all.get("gate2", {})
    esth_kws = _esthetic_keywords()
    rows_to_write = []           # ВСЕ позиции клиники, прошедшей ворота
    marker_tags = set()          # только для ворот (эстетика/венерология)
    nonadj_page_skipped = []     # позиции чужих разделов (фильтр страниц)
    gate_profile_n, gate_total_n, esth_units = 0, 0, 0
    _DERM = ("derm", "oncoderm", "trich", "dermsurg")
    for s in data["services"]:
        name = s["name"]
        m1 = map_tier1(name, form_index, fuzzy_cutoff=fuzzy_cutoff)
        # фильтр СТРАНИЦ несмежных разделов (сохранён по указанию заказчика):
        # позиция со страницы чужого раздела не собирается, кроме словарного
        # совпадения профиля (профильная лаборатория КВД на /analizy-страницах)
        if NONADJ_PAGE_URL_RE.search(s.get("page_url") or "") and not m1:
            nonadj_page_skipped.append(name)
            continue
        # «Тип строки» (заказчик, п.3): расходник/служебное — В ТАБЛИЦЕ,
        # но не услуга и не участник ворот-меры
        if m1 is None and CONSUMABLE_RE.search(name):
            rows_to_write.append({**s, "row_type": "расходник"})
            continue
        if m1 is None and (is_zone_or_junk_name(name)
                           or DESCRIPTIVE_TEXT_RE.search(name)):
            rows_to_write.append({**s, "row_type": "служебное"})
            continue
        # счётчик ворот: профильная позиция = словарный дерм-тег ИЛИ якорь.
        # Якорь по названию занижает («Гистологическое исследование удаленного
        # материала» — ядро профиля без слова «кожа») — оговорено;
        # компенсируется ветвью приёма профильного врача в формуле ворот
        is_profile_hit = (contours.get(m1["tag"]) in _DERM if m1
                          else bool(PROFILE_ANCHOR_RE.search(name)))
        gate_total_n += 1
        gate_profile_n += int(is_profile_hit)
        is_esth = ((m1 and contours.get(m1["tag"]) in ("cosm_est", "cosm_med"))
                   or (not m1 and BRAND_INJECTABLE_RE.search(name)
                       and "волос" not in name.lower())
                   or (not m1 and is_esthetic_line(name, esth_kws)))
        if is_esth:
            esth_units += 1
            data["esthetic_cosmetology_present"] = True
            if m1:
                marker_tags.add(m1["tag"])
        if m1 and m1["tag"] in ("std_consult", "std_lab"):
            marker_tags.add(m1["tag"])   # маркер венерологии
        if PACKAGE_RE.search(name):
            data["has_packages"] = True   # факт: пакеты есть; строка остаётся
        rows_to_write.append({**s, "row_type": "услуга",
                              "gate_anchor_hit": int(is_profile_hit)})
    profile_tags = marker_tags & set(contours)

    # ── Ворота: стоп-лист организаций (п.1) → G1 (ужесточён) → G2-МЕРА ──
    # УЖЕСТОЧЕНИЕ (заказчик, 2026-08-25, п.4): доля профильных позиций ≥30%,
    # ЛИБО ≥15 профильных позиций при доле ≥15% (крупная клиника с полноценным
    # дерм-направлением), ЛИБО приём профильного врача в прайсе (дерматолог/
    # дерматовенеролог/онкодерматолог/трихолог/дерматохирург — конкурент,
    # даже если процедур мало: приём весит больше процедуры).
    # Косметологический контур (эстетических единиц ≥2) сохранён: Тип 1
    # (косметологический) — наш рынок по классификатору (alab54, решение
    # заказчика 2026-08-26). Пороги — config/thresholds.yaml gate2.
    doctor_visit = data["doctor_visit_line"]
    profile_doctor = profile_doctor_visit_in_services(
        [s["name"] for s in data["services"]])
    derm_rows_n, routed_total = gate_profile_n, gate_total_n
    derm_share = derm_rows_n / routed_total if routed_total else 0.0
    g2_share_min = float(g2cfg.get("share_min", 0.30))
    g2_big_rows = int(g2cfg.get("big_rows_min", 15))
    g2_big_share = float(g2cfg.get("big_share_min", 0.15))
    g2_esth_min = int(g2cfg.get("esth_units_min", 2))
    g2_derm = ((routed_total > 0 and derm_share >= g2_share_min)
               or (derm_rows_n >= g2_big_rows and derm_share >= g2_big_share)
               or bool(profile_doctor))
    g2_esth = esth_units >= g2_esth_min
    g2 = g2_derm or g2_esth
    if data["org_stoplist_type"] and not doctor_visit:
        # стоп-лист проверяется ДО маркеров: «косметолог» на сайте салона
        # найдётся всегда; приём врача в прайсе снимает стоп-лист
        gate, reason = "Исключён", data["org_stoplist_type"]
    elif data["lab_only"] and not data["profile_markers_found"] \
            and not data["nonadjacent_signs"]:
        # такт 3: «Наедине-Н» (многопрофильный медцентр, 6 несмежных приёмов)
        # ложно исключался как лаборатория, потому что взятые страницы были
        # прайсом анализов; клиника с приёмами/профилем — не лаборатория
        gate, reason = "Исключён", "лаборатория без приёма врача"
    elif not (data["license_evidence"]["found"] or doctor_visit):
        # G1: только лицензия ИЛИ приём врача в прайсе; слов в тексте мало
        gate, reason = "Исключён", "салон красоты / нет медицинской деятельности"
    elif not g2:
        fact = ", ".join(data["doctor_specialties"][:4])
        gate = "Исключён"
        if derm_rows_n or esth_units:
            reason = (f"нет релевантного профиля (профильных позиций {derm_rows_n} "
                      f"из {routed_total}, {derm_share:.0%} — ниже порога"
                      + (f"; фактический: {fact}" if fact else "") + ")")
        else:
            reason = ("нет релевантного профиля"
                      + (f" (фактический: {fact})" if fact else ""))
    elif not rows_to_write:
        gate, reason = "Требует проверки", "профильные слова есть, услуг на страницах не найдено"
    else:
        gate = "Включён"
        reason = (f"G1-G2 пройдены (профильных позиций {derm_rows_n} из "
                  f"{routed_total}, доля {derm_share:.0%}, эстетики {esth_units}"
                  + (f"; приём профильного врача: «{profile_doctor[:60]}»"
                     if profile_doctor else "") + ")")

    if gate != "Включён":
        # фильтрация на уровне ОРГАНИЗАЦИИ: не прошла — прайс не тащим вообще
        rows_to_write = []

    # ── СУЖДЕНИЯ здесь НЕ выносятся (часть 2, 2026-08-25): тип клиники,
    # правило, флаги, маркеры, грейд считает пересчитываемый шаг
    # src/judgments.py по базе — правка справочника не требует обхода ──
    nonadj = sorted({n["direction"] for n in data["nonadjacent_signs"]})
    type_status = ("до пересчёта суждений (python -m src.judgments)"
                   if gate == "Включён" else None)
    seen_sections = set(data["sections_found"])

    # ── G5: реквизиты только с сайта, формат обязателен ──
    inn, inn_status = None, "Не найдено"
    raw_inn = data["requisites"]["inn_text"]
    if raw_inn:
        if validate_inn(raw_inn):
            inn = "".join(c for c in raw_inn if c.isdigit())
            inn_status = "с сайта (формат валиден; сверка 2-из-3 — этап 7)"
        else:
            inn_status = (f"Уточнить — формат отбит: «{raw_inn[:40]}»"
                          + (", формат ОГРН" if validate_ogrn(raw_inn) else ""))

    # ── Полнота сбора (заказчик, 2026-08-25): позиций с ценой на сайте vs
    # попало в таблицу; расхождение объясняется построчно в «Полнота_сбора».
    # agg_collapsed заполняет пересчёт суждений (свёртка — там)
    price_positions_found = sum(1 for s in data["services"] if s.get("price"))
    db.execute("INSERT OR REPLACE INTO clinics (clinic_id, title, title_source, "
               "ownership_form, "
               "domain, url, gate, "
               "gate_reason, type, type_status, rule, grade, esthetic_markers, "
               "nonadjacent, flag_single_nonadjacent, flag_removal_outside_derm, "
               "flag_site_unreachable, unreachable_note, fetch_level, "
               "nonprofile_excluded, crawl_pages_found, crawl_pages_fetched, "
               "crawl_cap_hit, "
               "has_packages, specialists_count, inn, inn_status, legal_name, "
               "sections_found, checked_at, "
               "gate_profile_rows, gate_total_rows, gate_profile_doctor, "
               "gate_esth_units, price_positions_found, nonadj_skipped) "
               "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
               (clinic_id, title, title_source, data["ownership_form"],
                dom, url, gate, reason,
                None, type_status, None, None,
                None,
                "; ".join(nonadj) or None,   # факт: несмежные слова найдены
                None, None,
                0, None, fetch_meta["level"],
                None,
                fetch_meta.get("pages_found"), fetch_meta.get("pages_fetched"),
                int(bool(fetch_meta.get("cap_hit"))),
                int(data["has_packages"]), data["specialists_count"],
                inn, inn_status, data["requisites"]["legal_name"],
                "; ".join(sorted(seen_sections)) or None, now,
                gate_profile_n, gate_total_n,
                profile_doctor[:200] if profile_doctor else None,
                esth_units, price_positions_found, len(nonadj_page_skipped)))
    for s in rows_to_write:
        # только СБОР: суждения (tag/basis/tier/уверенность) ставит
        # python -m src.judgments; «Профиль» пуст до эталона заказчика
        db.execute("INSERT INTO services_found (clinic_id, clinic_title, name_raw, "
                   "description_raw, page_url, price, row_type, gate_anchor_hit) "
                   "VALUES (?,?,?,?,?,?,?,?)",
                   (clinic_id, title, s["name"], s.get("description"),
                    s["page_url"], s.get("price"), s["row_type"],
                    s.get("gate_anchor_hit")))
    # ── Доказательства: каждый факт с цитатой и URL (такт 3, Верификатор) ──
    db.execute("DELETE FROM clinic_evidence WHERE clinic_id=?", (clinic_id,))
    ev = []
    if data["org_stoplist_type"]:
        ev.append(("стоп-лист организаций", data["org_stoplist_type"],
                   data["org_stoplist_evidence"], None))
    if doctor_visit:
        ev.append(("приём врача в прайсе (G1)", None, doctor_visit[:200], None))
    if profile_doctor:
        ev.append(("приём профильного врача в прайсе (ворота)", None,
                   profile_doctor[:200], None))
    for name in nonadj_page_skipped[:5]:
        ev.append(("позиция страницы несмежного раздела (фильтр страниц)",
                   None, name[:200], None))
    if data["license_evidence"]["found"]:
        ev.append(("лицензия", None, data["license_evidence"]["quote"],
                   data["license_evidence"]["url"]))
    for n in data["nonadjacent_signs"]:
        ev.append(("несмежное направление", n["direction"], n["quote"], n["url"]))
    if data["esthetic_cosmetology_present"] and data.get("esthetic_evidence"):
        ev.append(("эстетическая косметология", None, data["esthetic_evidence"], None))
    if data["requisites"]["quote"]:
        ev.append(("реквизиты", data["requisites"]["inn_text"],
                   data["requisites"]["quote"], data["requisites"]["url"]))
    for kind, detail, quote, evurl in ev:
        db.execute("INSERT INTO clinic_evidence (clinic_id, kind, detail, quote, url) "
                   "VALUES (?,?,?,?,?)", (clinic_id, kind, detail, quote, evurl))
    db.commit()
    price_rows_written = sum(1 for x in rows_to_write if x.get("price"))

    return {"clinic_id": clinic_id, "title": title, "gate": gate, "reason": reason,
            "type_status": type_status,
            "services": len(rows_to_write),
            "tier1_mapped": 0, "to_markup": 0,   # суждения — src.judgments
            "esth_units": esth_units,
            "nonadj_skipped": len(nonadj_page_skipped),
            "derm_rows": derm_rows_n, "derm_total": routed_total,
            "derm_share": round(derm_share, 2),
            "profile_doctor": bool(profile_doctor),
            "price_positions_found": price_positions_found,
            "price_rows_written": price_rows_written,
            "dirty_names_rejected": data["dirty_names_rejected"],
            "fetch_level": fetch_meta["level"], "unreachable": False, "domain": dom}


def run_stage6(city: str, db: sqlite3.Connection, max_clinics: int = 10) -> dict:
    """ОБЯЗАТЕЛЬНАЯ ОСТАНОВКА (п.8, 2026-08-26): первые max_clinics клиник →
    стоп без вопросов, промежуточная выгрузка + файл «на разметку»,
    ждать разметки заказчика. Платных вызовов нет (п.6: без внешнего API)."""
    ensure_stage6_tables(db)
    contours = load_contours()
    form_index = build_formulation_index()
    client_tags = set(yaml.safe_load(
        pathlib.Path("data/client_profile.yaml").read_text(encoding="utf-8"))["tags"])
    cfg = yaml.safe_load(pathlib.Path("config/thresholds.yaml").read_text(encoding="utf-8"))
    min_bytes = int(cfg.get("cascade", {}).get("content_min_bytes", 3000))
    unreachable_stop = float(cfg.get("cascade", {}).get("unreachable_share_stop", 0.15))
    max_pages = int(cfg.get("cascade", {}).get("max_pages_per_domain", MAX_PAGES_PER_SITE))

    done_domains = {row[0] for row in db.execute("SELECT domain FROM clinics")}
    cands = [dict(zip(("title", "url", "domain"), row)) for row in db.execute(
        "SELECT title, url, domain FROM candidates WHERE kind='site' "
        "AND domain IS NOT NULL ORDER BY domain")]
    # СЛУЧАЙНАЯ выборка с фиксированным seed (заказчик 2026-08-26, п.2):
    # алфавитный порядок брал один край списка; shuffle детерминирован
    import random
    run_cfg = yaml.safe_load(pathlib.Path("config/run.yaml").read_text(encoding="utf-8"))
    random.Random(int(run_cfg.get("stage6_sample_seed", 42))).shuffle(cands)

    stage_budget_sec = float(cfg.get("cascade", {}).get("stage6_budget_min", 150)) * 60
    stage_t0 = time.monotonic()
    processed, results = 0, []
    stage_timeout_hit = False
    for cand in cands:
        if processed >= max_clinics:
            break
        if time.monotonic() - stage_t0 > stage_budget_sec:
            # потолок этапа целиком (разбор 2026-08-26): чекпойнт сохранён,
            # прерывание логируется — не ждём
            stage_timeout_hit = True
            print(f"⛔ потолок этапа 6: {stage_budget_sec/60:.0f} мин исчерпаны "
                  f"после {processed} клиник — прерывание, собранное сохранено")
            break
        if cand["domain"] in done_domains:
            continue  # домен обрабатывается ровно один раз за прогон
        done_domains.add(cand["domain"])
        try:
            r = process_clinic(cand, db, contours, form_index, client_tags, city,
                               min_bytes=min_bytes, max_pages=max_pages)
        except Exception as exc:  # noqa: BLE001 — клиника не роняет пачку
            db.execute("INSERT OR REPLACE INTO clinics (clinic_id, title, domain, url, "
                       "gate, gate_reason, grade, checked_at) VALUES (?,?,?,?,?,?,?,?)",
                       (f"КЛН-{cand['domain']}", cand["title"], cand["domain"], cand["url"],
                        "Требует ручной проверки", f"ошибка обхода: {type(exc).__name__}",
                        "C", datetime.datetime.now().isoformat(timespec="seconds")))
            db.commit()
            r = {"clinic_id": f"КЛН-{cand['domain']}", "gate": "Требует ручной проверки",
                 "services": 0, "tier1_mapped": 0, "to_markup": 0,
                 "fetch_level": None, "unreachable": False, "domain": cand["domain"]}
        results.append(r)
        processed += 1
        time.sleep(RATE_DELAY_SEC)

    # ── ТЕСТ заказчика (2026-08-26, п.1): клиника, не прошедшая ворота,
    # не может иметь строк в services_found. Нарушение — ошибка выполнения.
    bad = [row[0] for row in db.execute(
        "SELECT DISTINCT c.clinic_id FROM clinics c "
        "JOIN services_found s ON s.clinic_id = c.clinic_id "
        "WHERE c.gate != 'Включён'")]
    if bad:
        raise RuntimeError(
            f"нарушение ворот: клиники вне 'Включён' имеют строки услуг: {bad} — "
            f"запись услуг до прохождения G1-G2 запрещена")

    # ── Замер для решения «остаёмся ли на ручной разметке» (п.6) ──
    total = sum(r["services"] for r in results)
    tier1 = sum(r["tier1_mapped"] for r in results)
    markup = sum(r["to_markup"] for r in results)
    with_markup = [r for r in results if r["to_markup"] > 0]

    # ── Отчётность по каскаду (п.5 второго промпта исправления) ──
    taken_by_level = {lv: sum(1 for r in results if r.get("fetch_level") == lv)
                      for lv in (1, 2, 3, 4)}
    unreachable = sorted(r["domain"] for r in results if r.get("unreachable"))
    unreachable_share = len(unreachable) / processed if processed else 0.0
    if unreachable:
        out = pathlib.Path("output") / f"{city}_недоступные_{datetime.date.today().isoformat()}.txt"
        out.write_text("\n".join(unreachable) + "\n", encoding="utf-8")
    cascade_alert = None
    if processed and unreachable_share > unreachable_stop:
        cascade_alert = (f"⛔ ОСТАНОВКА: недоступных {len(unreachable)}/{processed} "
                         f"({unreachable_share:.0%}) > {unreachable_stop:.0%} — проблема "
                         f"системная, а не в отдельных сайтах. Доклад заказчику обязателен.")

    return {"processed": processed, "results": results,
            "stage_timeout_hit": stage_timeout_hit,
            "services_total": total, "tier1_mapped": tier1, "to_markup": markup,
            "avg_markup_batch": round(markup / len(with_markup), 1) if with_markup else 0,
            "taken_by_level": taken_by_level, "unreachable_domains": unreachable,
            "unreachable_share": round(unreachable_share, 3),
            "cascade_alert": cascade_alert,
            "stopped_reason": f"обязательная остановка после {max_clinics} клиник — "
                              f"промежуточная выгрузка + файл «на разметку», "
                              f"ждём разметки заказчика"}
