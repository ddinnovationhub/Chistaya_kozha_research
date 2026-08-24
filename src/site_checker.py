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

import httpx
import yaml

from src.classify import classify, load_contours
from src.dedup import normalize_domain
from src.extract_site import extract_pages
from src.mapper import build_formulation_index, map_tier1
from src.validators import validate_inn, validate_ogrn

RATE_DELAY_SEC = 3
MAX_PAGES_PER_SITE = 8

TYPE_STATUS_PRELIM = "предварительный (ступень 1, до разметки)"
TYPE_STATUS_FINAL = "финальный (после разметки)"


def fetch_page(url: str, timeout: float = 60.0) -> str | None:
    """Jina Reader → прямой GET. Playwright — отдельным фолбэком в crawl_site."""
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/126.0"}
    try:
        r = httpx.get(f"https://r.jina.ai/{url}", timeout=timeout,
                      headers=headers, follow_redirects=True)
        if r.status_code == 200 and len(r.text) > 300:
            return r.text
    except Exception:  # noqa: BLE001
        pass
    try:
        r = httpx.get(url, timeout=30, headers=headers, follow_redirects=True)
        if r.status_code == 200:
            return r.text
    except Exception:  # noqa: BLE001
        return None
    return None


def _find_section_links(home_text: str, base_url: str) -> list[str]:
    links = re.findall(r"\((https?://[^)\s]+)\)", home_text) + \
            re.findall(r'href="(https?://[^"]+|/[^"]+)"', home_text)
    base_dom = normalize_domain(base_url)
    out, seen = [], set()
    for link in links:
        if link.startswith("/"):
            link = base_url.rstrip("/") + link
        if normalize_domain(link) != base_dom:
            continue
        low = link.lower()
        if any(w in low for w in ("uslug", "price", "prais", "napravlen", "vrach",
                                  "doctor", "licen", "rekvizit", "contact", "kontakt",
                                  "about", "o-klinike", "o-nas", "ceny", "services")):
            key = link.split("#")[0].rstrip("/")
            if key not in seen:
                seen.add(key)
                out.append(key)
    return out[:MAX_PAGES_PER_SITE - 1]


def crawl_site(url: str, city: str) -> dict[str, str]:
    """Один обход домена: главная + профильные разделы. Возвращает {url: text}."""
    pages = {}
    home = fetch_page(url)
    if home is None:
        return pages
    pages[url] = home
    for link in _find_section_links(home, url):
        time.sleep(RATE_DELAY_SEC)
        text = fetch_page(link)
        if text:
            pages[link] = text
    # доказательная база: gzip в raw/{city}/{date}/
    day = datetime.date.today().isoformat()
    raw_dir = pathlib.Path("raw") / city / day
    raw_dir.mkdir(parents=True, exist_ok=True)
    dom = normalize_domain(url) or "unknown"
    for i, (u, txt) in enumerate(pages.items()):
        (raw_dir / f"{dom}_{i}.md.gz").write_bytes(gzip.compress(txt.encode("utf-8")))
    return pages


def ensure_stage6_tables(db: sqlite3.Connection):
    db.executescript("""
    CREATE TABLE IF NOT EXISTS clinics (
        clinic_id TEXT PRIMARY KEY, title TEXT, domain TEXT, url TEXT,
        gate TEXT, gate_reason TEXT, type TEXT, type_status TEXT, rule TEXT,
        grade TEXT, esthetic_markers TEXT, nonadjacent TEXT,
        flag_single_nonadjacent INTEGER, flag_removal_outside_derm INTEGER,
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
    try:  # миграция таблицы, созданной кодом до 2026-08-26 (без type_status)
        db.execute("ALTER TABLE clinics ADD COLUMN type_status TEXT")
    except sqlite3.OperationalError:
        pass
    db.commit()


def process_clinic(cand: dict, db: sqlite3.Connection, contours: dict,
                   form_index: dict, client_tags: set, city: str) -> dict:
    dom = cand["domain"]
    clinic_id = f"КЛН-{dom}"
    url = cand["url"] if cand["url"].startswith("http") else f"https://{dom}"
    pages = crawl_site(url, city)
    now = datetime.datetime.now().isoformat(timespec="seconds")

    if not pages:
        db.execute("INSERT OR REPLACE INTO clinics (clinic_id, title, domain, url, "
                   "gate, gate_reason, type, type_status, rule, grade, inn_status, "
                   "checked_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                   (clinic_id, cand["title"], dom, url, "Требует проверки",
                    "Сайт не найден на дату проверки", "Не классифицировано", None,
                    None, "C", "Не найдено", now))
        db.commit()
        return {"clinic_id": clinic_id, "gate": "Требует проверки", "grade": "C",
                "services": 0, "tier1_mapped": 0, "to_markup": 0}

    data = extract_pages(pages, form_index)

    # ── Ступень 1 (код): точное совпадение; остальное — «на разметке» ──
    mapped, to_markup = [], []
    for s in data["services"]:
        m1 = map_tier1(s["name"], form_index)
        if m1:
            mapped.append({**s, **m1})
        else:
            to_markup.append(s)

    found_tags = {m["tag"] for m in mapped if m.get("tag")} & set(contours)
    if data["esthetic_cosmetology_present"]:
        found_tags.add("hardware_rejuvenation")  # агрегатный эстетический маркер

    # ── Ворота ──
    g1 = data["license_evidence"]["found"] or bool(data["doctor_specialties"])
    if not g1:
        gate, reason = "Исключён", "салон красоты / нет медицинской деятельности"
    elif not found_tags and not to_markup:
        gate, reason = "Исключён", "нет релевантного профиля"
    else:
        gate, reason = "Включён", "G1-G2 пройдены"

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

    db.execute("INSERT OR REPLACE INTO clinics (clinic_id, title, domain, url, gate, "
               "gate_reason, type, type_status, rule, grade, esthetic_markers, "
               "nonadjacent, flag_single_nonadjacent, flag_removal_outside_derm, "
               "has_packages, specialists_count, inn, inn_status, legal_name, "
               "sections_found, checked_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
               (clinic_id, cand["title"], dom, url, gate, reason,
                cls["type"], type_status, cls.get("rule"), grade,
                "; ".join(cls["esthetic_markers_found"]) or None,
                "; ".join(nonadj) or None,
                int(cls["flag_single_nonadjacent"]), int(cls["flag_removal_outside_derm"]),
                int(data["has_packages"]), data["specialists_count"],
                inn, inn_status, data["requisites"]["legal_name"],
                "; ".join(sorted(seen_sections)) or None, now))
    for m in mapped:
        db.execute("INSERT INTO services_found (clinic_id, clinic_title, name_raw, "
                   "description_raw, page_url, price, tag, code_804n, mapping_basis, "
                   "mapping_tier, confidence, client_has) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                   (clinic_id, cand["title"], m["name"], m.get("description"),
                    m["page_url"], m.get("price"), m.get("tag"), m.get("code_804n"),
                    m.get("basis"), m.get("tier"), m.get("confidence"),
                    "Да" if (m.get("tag") in client_tags) else "Нет"))
    for s in to_markup:
        db.execute("INSERT INTO services_found (clinic_id, clinic_title, name_raw, "
                   "description_raw, page_url, price, tag, code_804n, mapping_basis, "
                   "mapping_tier, confidence, client_has) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                   (clinic_id, cand["title"], s["name"], s.get("description"),
                    s["page_url"], s.get("price"), None, None,
                    "ступень 1: точного совпадения со справочником нет",
                    "на разметке", None, None))
    # ── Доказательства: каждый факт с цитатой и URL (такт 3, Верификатор) ──
    db.execute("DELETE FROM clinic_evidence WHERE clinic_id=?", (clinic_id,))
    ev = []
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
    return {"clinic_id": clinic_id, "gate": gate, "type": cls["type"],
            "type_status": type_status, "grade": grade,
            "services": len(mapped) + len(to_markup),
            "tier1_mapped": len(mapped), "to_markup": len(to_markup)}


def run_stage6(city: str, db: sqlite3.Connection, max_clinics: int = 10) -> dict:
    """ОБЯЗАТЕЛЬНАЯ ОСТАНОВКА (п.8, 2026-08-26): первые max_clinics клиник →
    стоп без вопросов, промежуточная выгрузка + файл «на разметку»,
    ждать разметки заказчика. Платных вызовов нет (п.6: без внешнего API)."""
    ensure_stage6_tables(db)
    contours = load_contours()
    form_index = build_formulation_index()
    client_tags = set(yaml.safe_load(
        pathlib.Path("data/client_profile.yaml").read_text(encoding="utf-8"))["tags"])

    done_domains = {row[0] for row in db.execute("SELECT domain FROM clinics")}
    cands = [dict(zip(("title", "url", "domain"), row)) for row in db.execute(
        "SELECT title, url, domain FROM candidates WHERE kind='site' "
        "AND domain IS NOT NULL ORDER BY discovered_at, domain")]

    processed, results = 0, []
    for cand in cands:
        if processed >= max_clinics:
            break
        if cand["domain"] in done_domains:
            continue  # домен обрабатывается ровно один раз за прогон
        done_domains.add(cand["domain"])
        try:
            r = process_clinic(cand, db, contours, form_index, client_tags, city)
        except Exception as exc:  # noqa: BLE001 — клиника не роняет пачку
            db.execute("INSERT OR REPLACE INTO clinics (clinic_id, title, domain, url, "
                       "gate, gate_reason, grade, checked_at) VALUES (?,?,?,?,?,?,?,?)",
                       (f"КЛН-{cand['domain']}", cand["title"], cand["domain"], cand["url"],
                        "Требует ручной проверки", f"ошибка обхода: {type(exc).__name__}",
                        "C", datetime.datetime.now().isoformat(timespec="seconds")))
            db.commit()
            r = {"clinic_id": f"КЛН-{cand['domain']}", "gate": "Требует ручной проверки",
                 "services": 0, "tier1_mapped": 0, "to_markup": 0}
        results.append(r)
        processed += 1
        time.sleep(RATE_DELAY_SEC)

    # ── Замер для решения «остаёмся ли на ручной разметке» (п.6) ──
    total = sum(r["services"] for r in results)
    tier1 = sum(r["tier1_mapped"] for r in results)
    markup = sum(r["to_markup"] for r in results)
    with_markup = [r for r in results if r["to_markup"] > 0]
    return {"processed": processed, "results": results,
            "services_total": total, "tier1_mapped": tier1, "to_markup": markup,
            "avg_markup_batch": round(markup / len(with_markup), 1) if with_markup else 0,
            "stopped_reason": f"обязательная остановка после {max_clinics} клиник — "
                              f"промежуточная выгрузка + файл «на разметку», "
                              f"ждём разметки заказчика"}
