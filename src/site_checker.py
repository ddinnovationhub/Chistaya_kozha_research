"""Этап 6 — проверяющий сайты (consumer), по утверждённому промпту
prompts/06_site_checker.md + промпту переработки 2026-08-26.

Режимы: поиск закрытый (слои из справочника), СБОР ОТКРЫТЫЙ — с сайта
забираются ВСЕ позиции профиля, включая отсутствующие в справочнике;
справочник — разметка на выходе, не фильтр на входе.

Обязательная остановка (п.8): первые 10 клиник → стоп, промежуточная
выгрузка, ждать решения заказчика. Домен обрабатывается одним consumer'ом
ровно один раз за прогон; внутри обхода — несколько страниц.
"""

import datetime
import gzip
import json
import pathlib
import re
import sqlite3
import time

import httpx
import yaml

from src.classify import classify, load_contours
from src.dedup import normalize_domain
from src.mapper import (build_formulation_index, map_tier1, map_tier2_batch,
                        tags_reference_text)
from src.validators import validate_inn, validate_ogrn

RATE_DELAY_SEC = 3
MAX_PAGES_PER_SITE = 8
SECTION_WORDS = ["услуг", "прайс", "цены", "направлен", "врач", "специалист",
                 "лиценз", "реквизит", "контакт", "о клинике", "о центре", "о нас"]

_EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {
        "services": {"type": "array", "items": {"type": "object", "properties": {
            "name": {"type": "string"},
            "description": {"type": ["string", "null"]},
            "price": {"type": ["string", "null"]},
            "page_url": {"type": "string"}},
            "required": ["name", "description", "price", "page_url"],
            "additionalProperties": False}},
        "sections_found": {"type": "array", "items": {"type": "string"}},
        "license_evidence": {"type": "object", "properties": {
            "found": {"type": "boolean"}, "quote": {"type": ["string", "null"]},
            "url": {"type": ["string", "null"]}},
            "required": ["found", "quote", "url"], "additionalProperties": False},
        "doctor_specialties": {"type": "array", "items": {"type": "string"}},
        "nonadjacent_signs": {"type": "array", "items": {"type": "object", "properties": {
            "direction": {"type": "string"}, "quote": {"type": "string"},
            "url": {"type": "string"}},
            "required": ["direction", "quote", "url"], "additionalProperties": False}},
        "esthetic_cosmetology_present": {"type": "boolean"},
        "has_packages": {"type": "boolean"},
        "requisites": {"type": "object", "properties": {
            "inn_text": {"type": ["string", "null"]}, "ogrn_text": {"type": ["string", "null"]},
            "legal_name": {"type": ["string", "null"]}, "quote": {"type": ["string", "null"]},
            "url": {"type": ["string", "null"]}},
            "required": ["inn_text", "ogrn_text", "legal_name", "quote", "url"],
            "additionalProperties": False},
        "specialists_count": {"type": ["integer", "null"]},
    },
    "required": ["services", "sections_found", "license_evidence", "doctor_specialties",
                 "nonadjacent_signs", "esthetic_cosmetology_present", "has_packages",
                 "requisites", "specialists_count"],
    "additionalProperties": False,
}

_EXTRACT_SYSTEM = """Ты — проверяющий сайтов проекта разведки рынка дерматологических клиник.
Тебе даны страницы ОДНОГО сайта (markdown). Извлеки данные СТРОГО по правилам.

СБОР ОТКРЫТЫЙ — забирай ВСЕ услуги профиля, даже незнакомые:
+ дерматология (нозологии, диагностика, лечение), дермато-онкология
  (удаления, дерматоскопия, гистология, иссечения, биопсии), трихология,
  дерматохирургия — каждую позицию отдельно, дословно как на сайте.
ГРАНИЦЫ — НЕ забирай позициями:
- венерология/ИППП (сифилис, гонорея, анализы на ИППП) — не включать в services;
- онкология НЕ кожи (химиотерапия, маммолог, скрининг ЖКТ) — не включать;
- гинекология, терапия, УЗИ органов, стоматология и прочие несмежные — только
  в nonadjacent_signs с цитатой и URL, без позиций;
- врачебная и эстетическая косметология — только флаг
  esthetic_cosmetology_present, без детализации позиций.
ИСКЛЮЧАЕТСЯ всегда: товары (шампуни, кремы, БАДы); общелабораторные анализы
крови (биохимия, гормоны, витамины, ОАК, ОАМ) — НО профильную лабораторию
забирай (гистология, ИГХ, цитология, микроскопия на грибы/демодекс, посевы,
соскобы); пакеты и Check UP — только has_packages=true, состав не разбирать.

ЗАПРЕТЫ: ничего не додумывать сверх текста; описание/цену брать только если
они написаны; ФИО врачей НЕ извлекать (только специальности и число);
specialists_count — только если число указано явно, иначе null;
license_evidence.found=true только при явном упоминании лицензии/номера.
Каждый элемент — с page_url той страницы, где найден."""


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


def extract_clinic(clinic_title: str, pages: dict[str, str], model: str,
                   budget, client=None) -> dict:
    import anthropic
    client = client or anthropic.Anthropic()
    parts = []
    total = 0
    for u, txt in pages.items():
        chunk = txt[:60000]
        total += len(chunk)
        parts.append(f"===== СТРАНИЦА {u} =====\n{chunk}")
        if total > 350000:
            break
    user = f"Клиника: {clinic_title}\n\n" + "\n\n".join(parts)
    budget.charge("anthropic", 1)
    response = client.messages.create(
        model=model, max_tokens=32000,
        system=_EXTRACT_SYSTEM,
        output_config={"format": {"type": "json_schema", "schema": _EXTRACT_SCHEMA}},
        messages=[{"role": "user", "content": user}],
    )
    budget.charge_tokens("anthropic", response.usage.input_tokens,
                         response.usage.output_tokens)
    return json.loads(next(b.text for b in response.content if b.type == "text"))


def ensure_stage6_tables(db: sqlite3.Connection):
    db.executescript("""
    CREATE TABLE IF NOT EXISTS clinics (
        clinic_id TEXT PRIMARY KEY, title TEXT, domain TEXT, url TEXT,
        gate TEXT, gate_reason TEXT, type TEXT, rule TEXT, grade TEXT,
        esthetic_markers TEXT, nonadjacent TEXT,
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
    """)
    db.commit()


def process_clinic(cand: dict, db: sqlite3.Connection, model: str, budget,
                   contours: dict, form_index: dict, tags_ref: str,
                   client_tags: set, city: str) -> dict:
    dom = cand["domain"]
    clinic_id = f"КЛН-{dom}"
    url = cand["url"] if cand["url"].startswith("http") else f"https://{dom}"
    pages = crawl_site(url, city)
    now = datetime.datetime.now().isoformat(timespec="seconds")

    if not pages:
        db.execute("INSERT OR REPLACE INTO clinics VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (clinic_id, cand["title"], dom, url, "Требует проверки",
                    "Сайт не найден на дату проверки", "Не классифицировано", None, "C",
                    None, None, None, None, None, None, None, "Не найдено", None, None, now))
        db.commit()
        return {"clinic_id": clinic_id, "gate": "Требует проверки", "grade": "C"}

    data = extract_clinic(cand["title"], pages, model, budget)

    # ── Маппинг услуг: ступень 1 (код) → ступень 2 (модель, один вызов) ──
    mapped, unmapped = [], []
    for s in data["services"]:
        m1 = map_tier1(s["name"], form_index)
        if m1:
            mapped.append({**s, **m1})
        else:
            unmapped.append(s)
    if unmapped:
        m2 = map_tier2_batch(cand["title"], unmapped, tags_ref, model, budget)
        for s, m in zip(unmapped, m2):
            mapped.append({**s, "tag": m["tag"], "code_804n": m["code_804n"],
                           "basis": m["basis"], "tier": "модель",
                           "confidence": m["confidence"]})

    found_tags = {m["tag"] for m in mapped if m.get("tag")} & set(contours)
    if data["esthetic_cosmetology_present"]:
        found_tags.add("hardware_rejuvenation")  # агрегатный эстетический маркер

    # ── Ворота ──
    g1 = data["license_evidence"]["found"] or bool(data["doctor_specialties"])
    if not g1:
        gate, reason = "Исключён", "салон красоты / нет медицинской деятельности"
    elif not found_tags:
        gate, reason = "Исключён", "нет релевантного профиля"
    else:
        gate, reason = "Включён", "G1-G2 пройдены"

    nonadj = sorted({n["direction"] for n in data["nonadjacent_signs"]})
    cls = classify(found_tags or set(), nonadjacent_found=nonadj, contours=contours) \
        if gate == "Включён" else {"type": "Не классифицировано", "rule": None,
                                    "esthetic_markers_found": [], "nonadjacent_found": nonadj,
                                    "flag_single_nonadjacent": False,
                                    "flag_removal_outside_derm": False}
    need = {"услуги", "врачи", "прайс", "направления"}
    seen_sections = {s.lower() for s in data["sections_found"]}
    grade = "A" if need <= seen_sections else "B"

    # ── G5: реквизиты только с сайта, формат обязателен ──
    inn, inn_status = None, "Не найдено"
    raw_inn = data["requisites"]["inn_text"]
    if raw_inn:
        if validate_inn(raw_inn):
            inn = "".join(c for c in raw_inn if c.isdigit())
            inn_status = "с сайта (формат валиден)"
        else:
            inn_status = (f"Уточнить — формат отбит: «{raw_inn[:40]}»"
                          + (", формат ОГРН" if validate_ogrn(raw_inn) else ""))

    db.execute("INSERT OR REPLACE INTO clinics VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
               (clinic_id, cand["title"], dom, url, gate, reason,
                cls["type"], cls.get("rule"), grade,
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
    db.commit()
    return {"clinic_id": clinic_id, "gate": gate, "type": cls["type"], "grade": grade,
            "services": len(mapped)}


def run_stage6(city: str, budget, db: sqlite3.Connection,
               max_clinics: int = 10) -> dict:
    """ОБЯЗАТЕЛЬНАЯ ОСТАНОВКА (п.8, 2026-08-26): первые max_clinics клиник →
    стоп без вопросов, промежуточная выгрузка, ждать решения заказчика."""
    cfg = yaml.safe_load(pathlib.Path("config/thresholds.yaml").read_text(encoding="utf-8"))
    model = cfg["anthropic"]["model"]
    ensure_stage6_tables(db)
    contours = load_contours()
    form_index = build_formulation_index()
    tags_ref = tags_reference_text()
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
            r = process_clinic(cand, db, model, budget, contours, form_index,
                               tags_ref, client_tags, city)
        except Exception as exc:  # noqa: BLE001 — клиника не роняет пачку
            db.execute("INSERT OR REPLACE INTO clinics (clinic_id, title, domain, url, "
                       "gate, gate_reason, grade, checked_at) VALUES (?,?,?,?,?,?,?,?)",
                       (f"КЛН-{cand['domain']}", cand["title"], cand["domain"], cand["url"],
                        "Требует ручной проверки", f"ошибка обхода: {type(exc).__name__}",
                        "C", datetime.datetime.now().isoformat(timespec="seconds")))
            db.commit()
            r = {"clinic_id": f"КЛН-{cand['domain']}", "gate": "Требует ручной проверки"}
        results.append(r)
        processed += 1
        time.sleep(RATE_DELAY_SEC)

    return {"processed": processed, "results": results,
            "stopped_reason": f"обязательная остановка после {max_clinics} клиник — "
                              f"промежуточная выгрузка, ждём решения заказчика"}
