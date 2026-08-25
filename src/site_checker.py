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

from src.classify import classify, load_contours
from src.dedup import normalize_domain
from src.extract_site import extract_pages
from src.fetch_cascade import ensure_fetch_tables, fetch_cascade
from src.mapper import build_formulation_index, map_tier1
from src.validators import validate_inn, validate_ogrn

RATE_DELAY_SEC = 3
MAX_PAGES_PER_SITE = 8

TYPE_STATUS_PRELIM = "предварительный (ступень 1, до разметки)"
TYPE_STATUS_FINAL = "финальный (после разметки)"


def _find_section_links(home_text: str, base_url: str) -> list[str]:
    """Ссылки разделов: HTML — через DOM (a[href] + urljoin, а не регулярка —
    прогон 2026-08-26 показал шторм 404 на криво склеенных путях),
    markdown от Jina — по (url) в скобках."""
    from urllib.parse import urljoin

    from src.html_text import looks_like_html
    if looks_like_html(home_text):
        from src.html_text import _soup
        links = [urljoin(base_url + "/", a["href"])
                 for a in _soup(home_text).find_all("a", href=True)]
    else:
        links = [urljoin(base_url + "/", u) for u in
                 re.findall(r"\((https?://[^)\s]+|/[^)\s]+)\)", home_text)]
    base_dom = normalize_domain(base_url)
    out, seen = [], set()
    for link in links:
        if not link.startswith("http") or normalize_domain(link) != base_dom:
            continue
        low = link.lower()
        if any(w in low for w in ("uslug", "price", "prais", "napravlen", "vrach",
                                  "doctor", "licen", "rekvizit", "contact", "kontakt",
                                  "about", "o-klinike", "o-nas", "ceny", "services")):
            key = link.split("#")[0].split("?")[0].rstrip("/")
            if key not in seen and key.rstrip("/") != base_url.rstrip("/"):
                seen.add(key)
                out.append(key)
    return out[:MAX_PAGES_PER_SITE - 1]


def crawl_site(url: str, city: str, form_index: dict, db=None,
               min_bytes: int = 3000) -> tuple[dict[str, str], dict]:
    """Один обход домена через КАСКАД (заказчик 2026-08-26): главная +
    профильные разделы. Возвращает ({url: text}, meta каскада главной).
    Уровень клиники = уровень, взявший главную; разделы стартуют с этого
    уровня как максимума (телеметрия каждой попытки — в fetch_attempts)."""
    pages = {}
    dom = normalize_domain(url) or "unknown"
    home, meta = fetch_cascade(url, dom, form_index, db=db, min_bytes=min_bytes)
    if home is None:
        return pages, meta
    pages[url] = home
    for link in _find_section_links(home, url):
        time.sleep(RATE_DELAY_SEC)
        # разделам достаточно уровня главной: сайт уже показал, чем он берётся
        text, _ = fetch_cascade(link, dom, form_index, db=db, min_bytes=min_bytes,
                                max_level=max(meta["level"], 2))
        if text:
            pages[link] = text
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
        domain TEXT, url TEXT,
        gate TEXT, gate_reason TEXT, type TEXT, type_status TEXT, rule TEXT,
        grade TEXT, esthetic_markers TEXT, nonadjacent TEXT,
        flag_single_nonadjacent INTEGER, flag_removal_outside_derm INTEGER,
        flag_site_unreachable INTEGER, unreachable_note TEXT, fetch_level INTEGER,
        nonprofile_excluded INTEGER,
        has_packages INTEGER, specialists_count INTEGER,
        inn TEXT, inn_status TEXT, legal_name TEXT,
        sections_found TEXT, checked_at TEXT);
    CREATE TABLE IF NOT EXISTS services_found (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        clinic_id TEXT, clinic_title TEXT,
        name_raw TEXT, description_raw TEXT, page_url TEXT, price TEXT,
        tag TEXT, code_804n TEXT, mapping_basis TEXT, mapping_tier TEXT,
        confidence TEXT, client_has TEXT);
    CREATE TABLE IF NOT EXISTS clinic_evidence (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        clinic_id TEXT, kind TEXT, detail TEXT, quote TEXT, url TEXT);
    """)
    # миграция таблиц, созданных кодом до 2026-08-26
    for col, typ in (("type_status", "TEXT"), ("flag_site_unreachable", "INTEGER"),
                     ("unreachable_note", "TEXT"), ("fetch_level", "INTEGER"),
                     ("title_source", "TEXT"), ("nonprofile_excluded", "INTEGER")):
        try:
            db.execute(f"ALTER TABLE clinics ADD COLUMN {col} {typ}")
        except sqlite3.OperationalError:
            pass
    ensure_fetch_tables(db)
    db.commit()


def process_clinic(cand: dict, db: sqlite3.Connection, contours: dict,
                   form_index: dict, client_tags: set, city: str,
                   min_bytes: int = 3000) -> dict:
    dom = cand["domain"]
    clinic_id = f"КЛН-{dom}"
    # Обход С КОРНЯ домена (такт 3: discovery даёт глубокие URL — новость
    # sharmnsk.ru была запрещена robots, и запрет ошибочно закрывал весь сайт;
    # плюс og:site_name и шапка живут на главной)
    url = f"https://{dom}"
    pages, fetch_meta = crawl_site(url, city, form_index, db=db, min_bytes=min_bytes)
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

    # ── Имя организации (п.4): og:site_name → шапка → discovery → title ──
    if data["site_name"] and data["site_name"].strip().lower() == city.strip().lower():
        data["site_name"] = None   # из шапки взялся переключатель города
    if data["site_name"]:
        title, title_source = data["site_name"], data["site_name_source"]
    elif cand.get("title"):
        title, title_source = cand["title"], "карточка discovery"
    elif data["page_title"]:
        title, title_source = data["page_title"], "title страницы — Уточнить"
    else:
        title, title_source = dom, "домен — Уточнить"

    # ── Ступень 1 (код) + маршрутизация по профилю ──────────────────────
    # В таблицу собираются ТОЛЬКО услуги профиля (дерматология, дермато-
    # онкология, трихология, дерматохирургия) и неопознанные строки без
    # признаков чужого профиля. Косметология — агрегатом (тег в классификацию,
    # позицией не пишется); венерология — ищем, не собираем; непрофильные
    # (вакцинация, ЭКГ, справки, несмежные специальности) — не собираются.
    from src.extract_site import (NONPROFILE_SERVICE_RE, PACKAGE_RE,
                                  VENEREOLOGY_RE, _esthetic_keywords,
                                  is_esthetic_line, is_zone_or_junk_name)
    esth_kws = _esthetic_keywords()
    mapped, to_markup = [], []
    marker_tags = set()          # теги для классификации без строки в таблице
    skipped_nonprofile, skipped_vener, skipped_esth = [], 0, 0
    for s in data["services"]:
        name = s["name"]
        # порядок: чужой профиль/пакет/зона/эстетика — ДО словаря (такт 3:
        # «Фототерапия (фотоэпиляция бедра)» мапилась в мед-тег phototherapy,
        # потому что нормализация выбрасывала скобочное уточнение)
        if NONPROFILE_SERVICE_RE.search(name):
            skipped_nonprofile.append(name)
            continue
        if VENEREOLOGY_RE.search(name):
            skipped_vener += 1   # венерология: ищем, но не собираем
            m1 = map_tier1(name, form_index)
            if m1 and m1["tag"] in ("std_consult", "std_lab"):
                marker_tags.add(m1["tag"])
            continue
        if PACKAGE_RE.search(name):
            data["has_packages"] = True   # пакеты флагом, состав не разбираем
            continue
        if is_zone_or_junk_name(name):
            continue   # «Щеки», «Один импульс», «Цена…» — не услуги
        if is_esthetic_line(name, esth_kws):
            skipped_esth += 1
            data["esthetic_cosmetology_present"] = True
            m1 = map_tier1(name, form_index)
            if m1 and contours.get(m1["tag"]) in ("cosm_est", "cosm_med"):
                marker_tags.add(m1["tag"])   # состав маркеров для Типа 2
            continue
        m1 = map_tier1(name, form_index)
        if m1:
            c = contours.get(m1["tag"])
            if c in ("cosm_est", "cosm_med") or m1["tag"] in ("std_consult", "std_lab"):
                marker_tags.add(m1["tag"])
                skipped_esth += int(c in ("cosm_est", "cosm_med"))
                skipped_vener += int(m1["tag"] in ("std_consult", "std_lab"))
            else:
                mapped.append({**s, **m1})
            continue
        to_markup.append(s)
    profile_tags = ({m["tag"] for m in mapped} | marker_tags) & set(contours)

    # ── Ворота: стоп-лист организаций (п.1) → G1 (ужесточён) → G2 ───────
    doctor_visit = data["doctor_visit_line"]
    g2 = bool(profile_tags or data["profile_markers_found"]
              or data["esthetic_cosmetology_present"])
    if data["org_stoplist_type"] and not doctor_visit:
        # стоп-лист проверяется ДО маркеров: «косметолог» на сайте салона
        # найдётся всегда; приём врача в прайсе снимает стоп-лист
        gate, reason = "Исключён", data["org_stoplist_type"]
    elif data["lab_only"]:
        gate, reason = "Исключён", "лаборатория без приёма врача"
    elif not (data["license_evidence"]["found"] or doctor_visit):
        # G1: только лицензия ИЛИ приём врача в прайсе; слов в тексте мало
        gate, reason = "Исключён", "салон красоты / нет медицинской деятельности"
    elif not g2:
        fact = ", ".join(data["doctor_specialties"][:4])
        gate = "Исключён"
        reason = ("нет релевантного профиля"
                  + (f" (фактический: {fact})" if fact else ""))
    elif not mapped and not to_markup and not marker_tags:
        gate, reason = "Требует проверки", "профильные слова есть, услуг на страницах не найдено"
    else:
        gate, reason = "Включён", "G1-G2 пройдены"

    found_tags = set(profile_tags)
    if data["esthetic_cosmetology_present"]:
        found_tags.add("hardware_rejuvenation")  # агрегатный эстетический маркер
    if gate != "Включён":
        mapped, to_markup = [], []   # ТЕСТ заказчика: Исключён → ноль строк услуг

    nonadj = sorted({n["direction"] for n in data["nonadjacent_signs"]})
    if gate == "Включён" and found_tags:
        cls = classify(found_tags, nonadjacent_found=nonadj, contours=contours)
        type_status = TYPE_STATUS_PRELIM
    else:
        cls = {"type": "Не классифицировано", "rule": None,
               "esthetic_markers_found": [], "nonadjacent_found": nonadj,
               "flag_single_nonadjacent": False, "flag_removal_outside_derm": False}
        type_status = ("ожидает разметки (ступень 1 тегов не дала)"
                       if gate == "Включён" else None)
    need = {"услуги", "врачи", "прайс", "направления"}
    seen_sections = set(data["sections_found"])
    grade = "A" if need <= seen_sections else "B"

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

    db.execute("INSERT OR REPLACE INTO clinics (clinic_id, title, title_source, "
               "domain, url, gate, "
               "gate_reason, type, type_status, rule, grade, esthetic_markers, "
               "nonadjacent, flag_single_nonadjacent, flag_removal_outside_derm, "
               "flag_site_unreachable, unreachable_note, fetch_level, "
               "nonprofile_excluded, "
               "has_packages, specialists_count, inn, inn_status, legal_name, "
               "sections_found, checked_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
               (clinic_id, title, title_source, dom, url, gate, reason,
                cls["type"], type_status, cls.get("rule"), grade,
                "; ".join(cls["esthetic_markers_found"]) or None,
                "; ".join(nonadj) or None,
                int(cls["flag_single_nonadjacent"]), int(cls["flag_removal_outside_derm"]),
                0, None, fetch_meta["level"],
                len(skipped_nonprofile),
                int(data["has_packages"]), data["specialists_count"],
                inn, inn_status, data["requisites"]["legal_name"],
                "; ".join(sorted(seen_sections)) or None, now))
    for m in mapped:
        db.execute("INSERT INTO services_found (clinic_id, clinic_title, name_raw, "
                   "description_raw, page_url, price, tag, code_804n, mapping_basis, "
                   "mapping_tier, confidence, client_has) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                   (clinic_id, title, m["name"], m.get("description"),
                    m["page_url"], m.get("price"), m.get("tag"), m.get("code_804n"),
                    m.get("basis"), m.get("tier"), m.get("confidence"),
                    "Да" if (m.get("tag") in client_tags) else "Нет"))
    for s in to_markup:
        db.execute("INSERT INTO services_found (clinic_id, clinic_title, name_raw, "
                   "description_raw, page_url, price, tag, code_804n, mapping_basis, "
                   "mapping_tier, confidence, client_has) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                   (clinic_id, title, s["name"], s.get("description"),
                    s["page_url"], s.get("price"), None, None,
                    "ступень 1: точного совпадения со справочником нет",
                    "на разметке", None, None))
    # ── Доказательства: каждый факт с цитатой и URL (такт 3, Верификатор) ──
    db.execute("DELETE FROM clinic_evidence WHERE clinic_id=?", (clinic_id,))
    ev = []
    if data["org_stoplist_type"]:
        ev.append(("стоп-лист организаций", data["org_stoplist_type"],
                   data["org_stoplist_evidence"], None))
    if doctor_visit:
        ev.append(("приём врача в прайсе (G1)", None, doctor_visit[:200], None))
    for name in skipped_nonprofile[:5]:
        ev.append(("непрофильная услуга (не собрана)", None, name[:200], None))
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
    # замер п.3 (промпт 2026-08-26): строк с ценами на страницах vs в таблице
    from src.fetch_cascade import PRICE_RE
    from src.html_text import html_to_text
    price_lines_on_pages = sum(
        1 for txt in pages.values() for ln in html_to_text(txt).splitlines()
        if PRICE_RE.search(ln))
    price_rows_written = sum(1 for x in (*mapped, *to_markup) if x.get("price"))

    return {"clinic_id": clinic_id, "title": title, "gate": gate, "reason": reason,
            "type": cls["type"], "type_status": type_status, "grade": grade,
            "services": len(mapped) + len(to_markup),
            "tier1_mapped": len(mapped), "to_markup": len(to_markup),
            "skipped_nonprofile": len(skipped_nonprofile),
            "skipped_venereology": skipped_vener, "skipped_esthetic": skipped_esth,
            "price_lines_on_pages": price_lines_on_pages,
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

    done_domains = {row[0] for row in db.execute("SELECT domain FROM clinics")}
    cands = [dict(zip(("title", "url", "domain"), row)) for row in db.execute(
        "SELECT title, url, domain FROM candidates WHERE kind='site' "
        "AND domain IS NOT NULL ORDER BY domain")]
    # СЛУЧАЙНАЯ выборка с фиксированным seed (заказчик 2026-08-26, п.2):
    # алфавитный порядок брал один край списка; shuffle детерминирован
    import random
    run_cfg = yaml.safe_load(pathlib.Path("config/run.yaml").read_text(encoding="utf-8"))
    random.Random(int(run_cfg.get("stage6_sample_seed", 42))).shuffle(cands)

    processed, results = 0, []
    for cand in cands:
        if processed >= max_clinics:
            break
        if cand["domain"] in done_domains:
            continue  # домен обрабатывается ровно один раз за прогон
        done_domains.add(cand["domain"])
        try:
            r = process_clinic(cand, db, contours, form_index, client_tags, city,
                               min_bytes=min_bytes)
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
            "services_total": total, "tier1_mapped": tier1, "to_markup": markup,
            "avg_markup_batch": round(markup / len(with_markup), 1) if with_markup else 0,
            "taken_by_level": taken_by_level, "unreachable_domains": unreachable,
            "unreachable_share": round(unreachable_share, 3),
            "cascade_alert": cascade_alert,
            "stopped_reason": f"обязательная остановка после {max_clinics} клиник — "
                              f"промежуточная выгрузка + файл «на разметку», "
                              f"ждём разметки заказчика"}
