"""Кодовая экстракция сигналов со страниц сайта — этап 6 БЕЗ внешнего API
(решение заказчика 2026-08-26, п.6: Anthropic не используется).

Что извлекает код (проверяемые механические сигналы):
- кандидаты строк услуг: строки прайс-таблиц и строки с ценой, плюс строки,
  дословно совпавшие со справочником (ступень 1);
- ИНН/ОГРН/юрлицо — регулярками, формат проверяется воротами G5;
- признак лицензии (G1), заявленные специальности врачей (G1);
- маркеры несмежных направлений (по classifier.yaml, с цитатой и URL);
- признак эстетической косметологии (по формулировкам cosm_est-тегов);
- признак пакетов, найденные разделы.

Чего код НЕ делает: не додумывает, не перефразирует, не классифицирует
услуги по смыслу — всё несопоставленное уходит на ручную разметку
батчей в Claude Code (prompts/06_markup_batch.md).
"""

import pathlib
import re

import yaml

from src.html_text import html_to_text, page_title, site_name_from_html
from src.mapper import normalize_service_name

_CLASSIFIER = pathlib.Path("dictionaries/classifier.yaml")
_SERVICES = pathlib.Path("dictionaries/services.yaml")

PRICE_RE = re.compile(r"(\d[\d\s]{0,8}(?:[.,]\d{2})?)\s*(?:₽|руб\.?)", re.IGNORECASE)
INN_RE = re.compile(r"ИНН\s*[:№]?\s*(\d{9,15})")
OGRN_RE = re.compile(r"ОГРН(?:ИП)?\s*[:№]?\s*(\d{12,16})")
LEGAL_RE = re.compile(r"(?:ООО|АО|ЗАО|ПАО|АНО|ИП)\s*[«\"][^»\"]{2,80}[»\"]")
MD_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
PACKAGE_RE = re.compile(r"check\s*-?\s*up|чек-?ап|под\s+ключ|комплекс", re.IGNORECASE)

# Специальности для G1 «заявленный приём врача» — профильные и несмежные
SPECIALTY_WORDS = [
    "дерматолог", "дерматовенеролог", "трихолог", "онкодерматолог", "дерматохирург",
    "косметолог", "онколог", "терапевт", "педиатр", "гинеколог", "уролог",
    "кардиолог", "невролог", "офтальмолог", "эндокринолог", "хирург",
    "оториноларинголог", "отоларинголог", "стоматолог", "психотерапевт", "флеболог",
    "аллерголог", "иммунолог", "гастроэнтеролог", "пульмонолог", "ревматолог",
]

# G2 «релевантный профиль» (CLAUDE.md): дерматология · дерматовенерология ·
# онкодерматология · трихология · удаление новообразований · косметология.
# Ищутся в видимом тексте; решение ворот — в site_checker.
PROFILE_MARKERS = [
    "дерматолог", "дерматовенеролог", "онкодерматолог", "трихолог", "дерматохирург",
    "косметолог", "дерматология", "дерматовенерология", "онкодерматология",
    "трихология", "косметология", "удаление новообразований", "удаление родинок",
    "удаление папиллом", "удаление бородавок", "дерматоскопия",
]

# п.2 промпта 2026-08-26: сырой HTML в названии — строка НЕ записывается
_BAD_NAME_RE = re.compile(r"[<>]|class=|href=|style=|&#|\bspan\b|\bdiv\b", re.IGNORECASE)


def is_clean_name(name: str) -> bool:
    return not _BAD_NAME_RE.search(name or "")


# ── Стоп-лист типов организаций (заказчик 2026-08-26, п.1): проверяется
# ДО поиска профильных маркеров — слово «косметолог» на сайте салона
# найдётся всегда, признаком клиники не является ──────────────────────────
# Такт 3 (2026-08-26): маркеры — регэкспы со словограницами (подстрока
# «маникюр» ловила «маникюрных принадлежностей» в медицинской статье про
# грибок), решение по типу — при ≥2 РАЗНЫХ маркерах («действующее вещество»
# в описании ботулинотерапии ложно исключало клинику косметологии как аптеку).
ORG_STOPLIST = {
    "производитель препарата / аптека / интернет-магазин": [
        r"инструкция по применению", r"действующее вещество", r"купить в аптеке",
        r"состав препарата", r"форма выпуска", r"безрецептурн", r"добавить в корзину",
    ],
    "салон красоты / парикмахерская / ногтевая студия": [
        r"\bстрижк[аи]\b", r"окрашивание волос", r"\bманикюр[ау]?\b",
        r"\bпедикюр[ау]?\b", r"наращивание ресниц", r"\bукладк[аи]\b",
        r"барбершоп", r"парикмахерск",
    ],
    "агрегатор / справочник / доска объявлений": [
        # только однозначные признаки: «отзывы о врачах» встречается и на
        # сайтах клиник — в маркеры не берём (домены агрегаторов и так
        # отфильтрованы на discovery по AGGREGATOR_DOMAINS)
        r"доска объявлений", r"каталог организаций", r"все клиники города",
        r"сравните цены клиник",
    ],
}
ORG_STOPLIST_MIN_MARKERS = 2   # один маркер типом не считается (ложные хиты)

# G1 ужесточён (п.1): «приём врача с медицинской специальностью в прайсе» —
# строка услуг вида приём/консультация/осмотр + врач/специальность.
_DOCTOR_VISIT_RE = re.compile(
    r"(приём|прием|консультаци|осмотр)\S*\s+(врач|" + "|".join(SPECIALTY_WORDS) + ")",
    re.IGNORECASE)

# Лаборатория без приёма врача (п.1): все услуги — анализы/исследования
_LAB_SERVICE_RE = re.compile(
    r"анализ|исследовани|соскоб|посев|пцр|мазок|тест\b|панель|микроскопи|забор ",
    re.IGNORECASE)


def doctor_visit_in_services(names: list[str]) -> str | None:
    """Первая строка прайса, подтверждающая приём врача, или None."""
    for n in names:
        if _DOCTOR_VISIT_RE.search(n):
            return n
    return None


def looks_like_lab_only(names: list[str]) -> bool:
    named = [n for n in names if len(n) >= 4]
    return bool(named) and all(_LAB_SERVICE_RE.search(n) for n in named) \
        and doctor_visit_in_services(named) is None


# ── Границы сбора (промпт этапа 6, п.1 + такт-3 критерии 2026-08-26):
# в таблицу попадают только услуги профиля (дерматология, дермато-онкология,
# трихология, дерматохирургия); остальное не собирается позициями ──────────
NONPROFILE_SERVICE_RE = re.compile(
    r"вакцин|прививк|иммуниза|экг\b|спирограф|спирометр|справк|больничн|медкнижк"
    r"|медосмотр|флюорограф|рентген|мрт\b|фгдс|гастроскоп|колоноскоп|капельниц"
    r"|анализ крови|биохими|гормон|коагулограм|общий анализ|витамин\b"
    r"|гинеколог|уролог|андролог|стоматолог|офтальмолог|окулист|кардиолог|невролог"
    r"|оториноларинголог|отоларинголог|лор-|терапевт|педиатр|эндокринолог"
    r"|аллерголог|иммунолог|гастроэнтеролог|пульмонолог|ревматолог|психотерапевт"
    r"|психиатр|нарколог|флеболог|проктолог|маммолог|логопед|остеопат|мануальн"
    r"|массаж спины|массаж тела|узи (брюшной|органов|щитовидн|молочн|малого)"
    r"|клонорхоз|описторхоз|паразит|гельминт|лямбли|щитовидн"
    r"|пластическ\w+ хирург|ринопластик|маммопластик|липосакц|абдоминопласт"
    r"|отопластик|подтяжка груди",
    re.IGNORECASE)

# Зонные и служебные строки прайса — не услуги (такт 3: «Щеки | 5 940 ₽»,
# «Один импульс», «Минимальная стоимость» с сайта elix.pro шли строками)
ZONE_OR_JUNK_NAME_RE = re.compile(
    r"^(щ[её]ки|нос\b|крылья носа|подбородок|лоб\b|виски|лицо|шея|декольте"
    r"|спина|живот|бикини|подмышк|голень|голени|бедр[оа]|руки|ноги|кисти"
    r"|верхняя губа|межъягодичн|ягодиц|зона \S+|1 зона|одна зона"
    r"|((один|1) )?импульс|за 1 ?(см|ед|шт|импульс)"
    r"|минимальная стоимость|цена|стоимость|скидк|акция|абонемент"
    r"|каждая последующая|доплата)",
    re.IGNORECASE)
PROCEDURE_WORD_RE = re.compile(
    r"удален|лечен|при[её]м|консульт|осмотр|терап|чистк|пилинг|массаж|биопс"
    r"|дерматоскоп|диагност|исследован|анализ|коррекц|инъекц|введен|обработк"
    r"|эпиляц|шлифовк|плазмо|мезотерап|иссечен|криодеструкц|коагуляц|соскоб",
    re.IGNORECASE)


# служебные строки прайса, которые режутся ДАЖЕ при процедурном слове
# («Цена консультации без скидки», «Программы рассрочки платежа…»)
STRICT_JUNK_RE = re.compile(
    r"^(цена|стоимость|прайс)\b|скидк|рассрочк|кредитован|подарочн\w+ сертификат",
    re.IGNORECASE)


def is_zone_or_junk_name(name: str) -> bool:
    """Строка-зона/служебная строка прайса — не услуга. Помимо явного словаря
    зон: короткая строка (≤3 слов) без единого процедурного слова — зона
    («Бакенбарды», «Фаланги пальцев», «50 р/1см 2» с сайта elix.pro/alab54;
    словарные услуги проверяются ступенью 1 ДО этого правила)."""
    s = name.strip().lower()
    if STRICT_JUNK_RE.search(s):
        return True
    if PROCEDURE_WORD_RE.search(s):
        return False
    if ZONE_OR_JUNK_NAME_RE.match(s):
        return True
    return len(s.split()) <= 3

# Мера 2 (заказчик 2026-08-26): бренды инъекционной эстетики латиницей
# с объёмом в мл (Juvederm, Stylage, Belotero, Radiesse…) — сворачиваются
# в ОДНУ строку-агрегат на клинику, не выбрасываются. «волос» — guard:
# мезотерапия волос латинских брендов относится к трихологии, не сюда.
BRAND_INJECTABLE_RE = re.compile(
    r"(?=.*[A-Za-z]{3,})(?=.*\d[.,]?\d?\s*(мл|ml)\b)", re.IGNORECASE)

# венерология: ищем, но позициями не собираем (решение заказчика, промпт этапа 6)
VENEREOLOGY_RE = re.compile(
    r"иппп|сифилис|гоноре|хламиди|трихомониаз|уреаплазм|микоплазм"
    r"|социально[- ]значимые инфекции|(?<!дермато)венеролог",
    re.IGNORECASE)


def is_esthetic_line(name: str, esth_kws: list[str]) -> bool:
    low = name.lower()
    return any(kw in low for kw in esth_kws)


def detect_org_stoplist(pages_text: list[str]) -> tuple[str | None, str | None]:
    """(тип организации из стоп-листа, цитаты-признаки) или (None, None).
    Тип присваивается при ≥ ORG_STOPLIST_MIN_MARKERS РАЗНЫХ маркерах."""
    full = "\n".join(pages_text)
    low = full.lower()
    best = None
    for org_type, markers in ORG_STOPLIST.items():
        hits = []
        for pat in markers:
            m = re.search(pat, low)
            if m:
                line = full[max(0, m.start() - 50):m.end() + 70].replace("\n", " ").strip()
                hits.append(f"«…{line}…»")
        if len(hits) >= ORG_STOPLIST_MIN_MARKERS:
            if best is None or len(hits) > best[2]:
                best = (org_type, " | ".join(hits[:2]), len(hits))
    return (best[0], best[1]) if best else (None, None)

_NAV_JUNK = ("корзин", "записаться", "запись на", "режим работы", "поиск по",
             "меню", "наверх", "cookie", "политик", "конфиденциальн", "войти",
             "личный кабинет", "телефон", "©", "все права")

_SECTION_CANON = [
    ("услуги", ("услуг", "uslug", "servic")),
    ("прайс", ("прайс", "цены", "price", "ceny", "стоимост")),
    ("врачи", ("врач", "специалист", "vrach", "doctor")),
    ("направления", ("направлен", "napravlen")),
    ("лицензии", ("лиценз", "licen")),
    ("реквизиты", ("реквизит", "rekvizit")),
    ("о клинике", ("о клинике", "о центре", "о нас", "about", "o-klinike", "o-nas")),
    ("контакты", ("контакт", "kontakt", "contact")),
]


def _esthetic_keywords(services: dict | None = None) -> list[str]:
    services = services or yaml.safe_load(_SERVICES.read_text(encoding="utf-8"))
    kws = set()
    for t in services["tags"]:
        if t["contour"] != "cosm_est":
            continue
        for phrase in [t["name_ru"], *t.get("formulations_site", [])]:
            kw = phrase.split("(")[0].strip().lower()
            if len(kw) >= 5:
                kws.add(kw)
    kws.update(["ботокс", "филлер", "фотоомоложение", "шлифовк", "омоложени",
                "мезонит", "блефаропласт", "ревитализац", "лифтинг",
                "фотоэпиляц", "электроэпиляц", "шугаринг", "депиляц",
                "нитевой", "смас", "контурная пластик",
                "механическая чистка", "ультразвуковая чистка", "чистка спины",
                "полимолочная", "коллагеностимул", "плацентотерап"])
    return sorted(kws)


def _nonadjacent_directions(classifier: dict | None = None) -> list[dict]:
    classifier = classifier or yaml.safe_load(_CLASSIFIER.read_text(encoding="utf-8"))
    # информационные маркеры (decisive: false) на решение R1 не влияют —
    # в кодовой экстракции не собираются, чтобы не раздувать предварительный тип
    return [d for d in classifier["nonadjacent_directions"] if d.get("decisive", True)]


def _clean_line(line: str) -> str:
    s = MD_LINK_RE.sub(r"\1", line)
    s = re.sub(r"[#*_>`]+", " ", s)
    return re.sub(r"\s+", " ", s).strip(" -–—·|;:.,")


_PRICE_ONLY_RE = re.compile(
    r"^(?:цена|стоимость)?[:\s]*(?:от|до)?\s*\d[\d\s]{0,8}(?:[.,]\d{2})?\s*(?:₽|руб\.?)"
    r"(?:\s*/\s*\S{1,12})?$", re.IGNORECASE)


def _plausible_name(s: str) -> bool:
    return (4 <= len(s) <= 200 and re.search(r"[а-яА-ЯёЁ]", s) is not None
            and not PRICE_RE.search(s)
            and not any(j in s.lower() for j in _NAV_JUNK))


def _service_candidates_from_line(line: str, form_index: dict,
                                  prev_line: str = "") -> dict | None:
    """Одна строка → кандидат услуги или None. Без домысливания: имя —
    дословный текст строки без ценового хвоста. Если строка — ТОЛЬКО цена
    (вёрстка «название и цена в соседних блоках» — прогон 2026-08-26:
    178 цен на странице, 1 в таблице), именем берётся предыдущая строка."""
    s = _clean_line(line)
    if not s or not re.search(r"[а-яА-ЯёЁ0-9]", s):
        return None
    if _PRICE_ONLY_RE.match(s):
        prev = _clean_line(prev_line)
        if _plausible_name(prev):
            return {"name": prev, "price": PRICE_RE.search(s).group(0).strip()}
        return None
    if not (4 <= len(s) <= 200):
        return None
    low = s.lower()
    if any(j in low for j in _NAV_JUNK):
        return None
    m = PRICE_RE.search(s)
    if m:
        name = s[:m.start()].strip(" -–—:·.…")
        if len(name) < 4 or not re.search(r"[а-яА-ЯёЁ]", name):
            return None
        return {"name": name, "price": m.group(0).strip()}
    if normalize_service_name(s) in form_index:
        return {"name": s, "price": None}
    return None


def _table_row_candidate(line: str, form_index: dict) -> dict | None:
    cells = [_clean_line(c) for c in line.split("|")]
    cells = [c for c in cells if c]
    if len(cells) < 2:
        return None
    price_cell = next((c for c in cells if PRICE_RE.search(c) or re.fullmatch(r"\d[\d\s]{1,8}", c)), None)
    name_cell = next((c for c in cells
                      if c != price_cell and re.search(r"[а-яА-ЯёЁ]", c) and len(c) >= 4), None)
    if not name_cell:
        return None
    low = name_cell.lower()
    if any(j in low for j in _NAV_JUNK):
        return None
    if price_cell is None and normalize_service_name(name_cell) not in form_index:
        return None
    return {"name": name_cell, "price": price_cell}


def extract_pages(pages: dict[str, str], form_index: dict,
                  services: dict | None = None,
                  classifier: dict | None = None) -> dict:
    """{url: markdown} → сигналы в формате, совместимом с process_clinic."""
    esth_kws = _esthetic_keywords(services)
    nonadj_dirs = _nonadjacent_directions(classifier)

    result = {
        "services": [], "sections_found": set(),
        "license_evidence": {"found": False, "quote": None, "url": None},
        "doctor_specialties": set(), "nonadjacent_signs": [],
        "profile_markers_found": set(),   # для ворот G2 в site_checker
        "esthetic_cosmetology_present": False, "esthetic_evidence": None,
        "has_packages": False, "dirty_names_rejected": 0,
        "site_name": None, "site_name_source": None, "page_title": None,
        "requisites": {"inn_text": None, "ogrn_text": None, "legal_name": None,
                       "quote": None, "url": None},
        "specialists_count": None,  # только явное число; код его не выводит
    }
    seen_names: dict[str, dict] = {}
    seen_nonadj = set()
    visible_texts = []

    first = True
    for url, raw in pages.items():
        if first:   # имя организации — с главной (п.4: og:site_name → шапка)
            result["site_name"], result["site_name_source"] = site_name_from_html(raw)
            result["page_title"] = page_title(raw)
            first = False
        text = html_to_text(raw)   # п.2: только видимый текст из DOM, не разметка
        visible_texts.append(text)
        low_url = url.lower()
        prev_line = ""
        for canon, keys in _SECTION_CANON:
            if any(k in low_url for k in keys):
                result["sections_found"].add(canon)

        for line in text.splitlines():
            s = _clean_line(line)
            if not s:
                continue
            low = s.lower()

            for canon, keys in _SECTION_CANON:
                if line.lstrip().startswith("#") and any(k in low for k in keys):
                    result["sections_found"].add(canon)

            cand = (_table_row_candidate(line, form_index) if "|" in line
                    else _service_candidates_from_line(line, form_index, prev_line))
            prev_line = line
            if cand:
                if not is_clean_name(cand["name"]):
                    result["dirty_names_rejected"] += 1   # п.2: HTML-мусор не пишется
                else:
                    key = normalize_service_name(cand["name"])
                    if key and key not in seen_names:
                        seen_names[key] = {"name": cand["name"], "description": None,
                                           "price": cand["price"], "page_url": url}
                        result["services"].append(seen_names[key])
                    elif key and cand["price"] and seen_names[key]["price"] is None:
                        # название уже поймано без цены (словарное совпадение),
                        # цена пришла соседней строкой — дописываем, не дублируем
                        seen_names[key]["price"] = cand["price"]
                    if PACKAGE_RE.search(cand["name"]):
                        result["has_packages"] = True

            if not result["license_evidence"]["found"] and "лиценз" in low:
                result["license_evidence"] = {"found": True, "quote": s[:200], "url": url}

            for w in SPECIALTY_WORDS:
                if w in low:
                    result["doctor_specialties"].add(w)
            for w in PROFILE_MARKERS:
                if w in low:
                    result["profile_markers_found"].add(w)

            for d in nonadj_dirs:
                if d["name"] in seen_nonadj:
                    continue
                if any(kw.lower() in low for kw in d["keywords"]):
                    seen_nonadj.add(d["name"])
                    result["nonadjacent_signs"].append(
                        {"direction": d["name"], "quote": s[:200], "url": url})

            if not result["esthetic_cosmetology_present"]:
                hit = next((kw for kw in esth_kws if kw in low), None)
                if hit:
                    result["esthetic_cosmetology_present"] = True
                    result["esthetic_evidence"] = f"«{s[:160]}» ({url})"

            if result["requisites"]["inn_text"] is None:
                m = INN_RE.search(s)
                if m:
                    result["requisites"]["inn_text"] = m.group(1)
                    result["requisites"]["quote"] = s[:200]
                    result["requisites"]["url"] = url
            if result["requisites"]["ogrn_text"] is None:
                m = OGRN_RE.search(s)
                if m:
                    result["requisites"]["ogrn_text"] = m.group(1)
            if result["requisites"]["legal_name"] is None:
                m = LEGAL_RE.search(s)
                if m:
                    result["requisites"]["legal_name"] = m.group(0)

    result["sections_found"] = sorted(result["sections_found"])
    result["doctor_specialties"] = sorted(result["doctor_specialties"])
    result["profile_markers_found"] = sorted(result["profile_markers_found"])
    # ── Сигналы типа организации (стоп-лист, п.1) и приёма врача (G1) ──
    names = [s["name"] for s in result["services"]]
    result["org_stoplist_type"], result["org_stoplist_evidence"] = \
        detect_org_stoplist(visible_texts)
    result["doctor_visit_line"] = doctor_visit_in_services(names)
    result["lab_only"] = looks_like_lab_only(names)
    return result
