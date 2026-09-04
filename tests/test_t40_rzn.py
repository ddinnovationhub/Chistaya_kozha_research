"""Тест 20-40 и реестр лицензий РЗН (2026-08-27): извлечение специальностей
из перечня работ, сохранение лицензий, паспорт сайта, импорт t40."""

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def test_specialties_from_activity():
    from src.rzn_licenses import specialties_from_activity
    acts = ["При оказании первичной медико-санитарной помощи организуются и "
            "выполняются следующие работы (услуги): при оказании первичной "
            "специализированной медико-санитарной помощи в амбулаторных "
            "условиях по: дерматовенерологии, косметологии, онкологии"]
    specs = specialties_from_activity(acts)
    assert "дерматовенерологи" in specs
    assert "косметологи" in specs
    assert "онкологи" in specs
    assert "стоматологи" not in specs
    assert specialties_from_activity([]) == []


def test_save_licenses_and_negative():
    from src.rzn_licenses import ensure_tables, save_licenses
    db = sqlite3.connect(":memory:")
    ensure_tables(db)
    lic = {"number": "Л041-01170-02/00362563", "date": "25.07.2017",
           "licensee": "АО Фармленд", "authority": "МЗ РБ", "ogrn": "102",
           "inn": "0273028277", "valid_to": "Бессрочно", "annulled": None,
           "terminated": None, "is_med": True,
           "objects": [{"address": "Уфа, ул. Менделеева 128/3", "city": "Уфа",
                        "region": "РБ",
                        "activity": "…в амбулаторных условиях по: косметологии"}]}
    res = save_licenses(db, "0273028277", [lic])
    assert res == {"status": "проверен", "licenses": 1, "med": 1}
    row = db.execute("SELECT is_med, objects_n, specialties FROM rzn_licenses "
                     "WHERE inn='0273028277'").fetchone()
    assert row == (1, 1, "косметологи")
    # отрицательный результат — тоже запись
    save_licenses(db, "1234567890", [])
    assert db.execute("SELECT status, licenses_n FROM rzn_checked "
                      "WHERE inn='1234567890'").fetchone() == ("проверен", 0)
    # неудача запроса ≠ «лицензий нет»
    save_licenses(db, "1111111111", None)
    assert db.execute("SELECT status FROM rzn_checked WHERE inn='1111111111'"
                      ).fetchone() == ("запрос не удался",)


def test_med_number_detection():
    from src.rzn_licenses import _MED_NUM_RE
    assert _MED_NUM_RE.match("Л041-01170-02/00362563")
    assert _MED_NUM_RE.match("ЛО-54-01-000001")
    assert not _MED_NUM_RE.match("Л042-00110-77/00286463")   # фарма


def test_passport_build():
    from src.extract_site import extract_pages
    from src.mapper import build_formulation_index
    from src.passport import build_passport, contact_lines, menu_texts
    html = """<html><head><title>Клиника Пример — дерматология в Уфе</title>
    <meta name="description" content="Приём дерматолога и косметолога"></head>
    <body><nav><a href="/derm">Дерматология</a><a href="/stom">Стоматология</a>
    <a href="/price">Цены</a><a href="/kontakty">Контакты</a></nav>
    <h1>Медицинский центр Пример</h1>
    <p>ИНН 0273028277, ОГРН 1020202392121</p>
    <p>г. Уфа, ул. Ленина, д. 1</p>
    <p>Приём дерматолога — 1 500 ₽</p></body></html>"""
    pages = {"https://example.ru": html}
    data = extract_pages(pages, build_formulation_index())
    p = build_passport("example.ru", pages, data)
    assert "Дерматология | Стоматология" in p        # меню целиком, по порядку
    assert "ИНН 0273028277" in p                     # контакт-блок дословно
    assert "TITLE: Клиника Пример" in p
    assert "дерматолог" in p                          # специальности
    menu = menu_texts(html)
    assert menu[:2] == ["Дерматология", "Стоматология"]
    assert contact_lines("г. Уфа, ул. Ленина, д. 1\nпросто текст") == \
        ["г. Уфа, ул. Ленина, д. 1"]


def test_import_t40_first_n(tmp_path):
    import openpyxl

    from src.test40 import import_t40
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["№", "Наименование", "Регистрационный номер",
               "Сайт в сети Интернет", "Код налогоплательщика",
               "Регион регистрации", "Вид деятельности/отрасль",
               "Маркер ОКВЭД", "2025, Выручка, RUB"])
    for i in range(1, 6):
        ws.append([i, f"КОМПАНИЯ-{i}, ООО", f"10{i}", f"site{i}.ru",
                   f"027302827{i % 10}" if i != 3 else "badinn",
                   "Башкортостан (Республика)", "Деятельность", "2", 100])
    f = tmp_path / "base.xlsx"
    wb.save(f)
    db = sqlite3.connect(":memory:")
    st = import_t40(str(f), db, first_n=4)
    assert st["total"] == 4                # первые N, не весь файл
    assert st["bad_inn"] == 1              # невалидный ИНН не проходит
    assert db.execute("SELECT COUNT(*) FROM t40_companies").fetchone()[0] == 3
    assert db.execute("SELECT city FROM t40_companies LIMIT 1"
                      ).fetchone()[0] == "Уфа"


def test_parse_judge_json():
    from src.llm_judge import _parse_judge_json
    ok = _parse_judge_json('Вот ответ: {"суждение_А": "медорганизация", '
                           '"профиль": ["дерматология"], "основание": "Приём '
                           'дерматолога — 1 500 ₽"}')
    assert ok["суждение_А"] == "медорганизация"
    assert ok["профиль"] == ["дерматология"]
    # невалидный исход отбрасывается, а не пишется
    assert _parse_judge_json('{"суждение_А": "наверное клиника"}') is None
    assert _parse_judge_json("не json") is None


def test_pdf_url_extraction():
    from src.rzn_licenses import RZN_URL
    import re
    label = ('<a class="getfile" href="?downloadlic=753079&pdf=1">PDF</a>')
    m = re.search(r"downloadlic=(\d+)", label)
    assert f"{RZN_URL}?downloadlic={m.group(1)}&pdf=1" == \
        "https://roszdravnadzor.gov.ru/services/licenses?downloadlic=753079&pdf=1"


def test_import_resume_does_not_clobber(tmp_path):
    """Краш-перезапуск: повторный импорт НЕ затирает добытые результаты."""
    import openpyxl

    from src.test40 import import_t40
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["№", "Наименование", "Рег", "Сайт", "ИНН", "Регион",
               "Вид", "Маркер", "Выручка"])
    ws.append([1, "КОМПАНИЯ, ООО", "1", "a.ru", "0273028277",
               "Башкортостан (Республика)", "Мед", "1", 1])
    f = tmp_path / "b.xlsx"
    wb.save(f)
    db = sqlite3.connect(":memory:")
    import_t40(str(f), db, 40)
    db.execute("UPDATE t40_companies SET found_site='a.ru', "
               "med_judgment='медорганизация'")
    import_t40(str(f), db, 40)   # перезапуск шага импорта
    assert db.execute("SELECT found_site, med_judgment FROM t40_companies"
                      ).fetchone() == ("a.ru", "медорганизация")


def test_license_addr_confirmation():
    """Адрес точки из лицензии РЗН подтверждает сайт без ИНН (2026-08-27)."""
    from src.site_finder import (license_addr_in_text, license_addr_patterns,
                                 triple_check)
    addrs = ["443013, Самарская область, г. Самара, Ленинский район, "
             "улица Дачная, дом 24, 6 этаж, нежилое помещение № 33",
             "443112, Самарская область, г. Самара, ул. Георгия Димитрова, "
             "д. 90, 1 этаж, квартира 39"]
    pats = license_addr_patterns(addrs)
    assert ("дачная", "24") in pats and ("георгия димитрова", "90") in pats
    page = "Наши клиники: г. Самара, ул. Дачная, 24. Телефон +7..."
    assert license_addr_in_text(page, pats) == ("дачная", "24")
    assert license_addr_in_text("ул. Дачная — история улицы", pats) is None
    chk = triple_check("x.ru", "6315023806", "Самара", pages_hint=[page],
                       license_addrs=addrs)
    assert chk["verdict"] == "адрес лицензии"
    assert "дачная, 24" in chk["evidence"]


def test_gis2_parse_urls_and_brands():
    """Карточка 2ГИС: сайт из контактов; без разрешения contact_groups —
    бренд/организация (обходной путь для демо-ключа, 2026-08-27)."""
    from src.map_candidates import parse_gis2_items
    full = [{"contact_groups": [{"contacts": [
                {"type": "website", "url": "https://invitro.ru"},
                {"type": "phone", "value": "+7..."}]}],
             "brand": {"name": "ИНВИТРО"}}]
    urls, brands = parse_gis2_items(full)
    assert urls == ["https://invitro.ru"] and brands == ["ИНВИТРО"]
    # демо-ключ: contact_groups вырезаны сервером, бренд остаётся
    demo = [{"brand": {"name": "ИНВИТРО"}, "org": {"name": "ООО Инвитро-Т"}}]
    urls2, brands2 = parse_gis2_items(demo)
    assert urls2 == [] and brands2 == ["ИНВИТРО", "ООО Инвитро-Т"]


def test_foreign_inn_blocks_address_confirmation():
    """Кейс АДРЕМ→smitra.ru (заказчик, 2026-08-27): чужой ИНН на сайте
    блокирует адресные подтверждения — соседняя клиника не сливается."""
    from src.site_finder import triple_check
    page = ("Клиника Смитра, г. Новосибирск, ул. Геодезическая, 2/1. "
            "Реквизиты: ООО «Смитра», ИНН 5403334455")
    chk = triple_check("smitra.ru", "5404465184", "Новосибирск",
                       pages_hint=[page],
                       license_addrs=["г. Новосибирск, ул. Геодезическая, д. 2/1"])
    assert chk["verdict"] is None
    assert "другого юрлица" in chk["evidence"]
    # свой ИНН на сайте — подтверждение работает как раньше
    chk2 = triple_check("x.ru", "5404465184", "Новосибирск",
                        pages_hint=["ИНН 5404465184, ул. Геодезическая, 2/1"])
    assert chk2["verdict"] == "ИНН"


def test_quota_daily_limit(tmp_path, monkeypatch):
    """Суточный счётчик: учёт до запроса, жёсткая отсечка на лимите."""
    import src.quota as q
    monkeypatch.setattr(q, "DB_PATH", str(tmp_path / "q.db"))
    monkeypatch.setitem(q.LIMITS, "yandex_geosearch", 3)
    assert all(q.spend("yandex_geosearch") for _ in range(3))
    assert q.spend("yandex_geosearch") is False      # 4-й не проходит
    used, lim = q.status("yandex_geosearch")
    assert (used, lim) == (3, 3)                     # отказ не расходует


def test_yandex_doublecheck_matching(monkeypatch):
    """Даблчек карточкой: совпадение, поддомен сети, расхождение — флаг."""
    import src.map_candidates as mc
    monkeypatch.setattr(mc, "yandex_map_urls",
                        lambda name, city, n=5: ["https://a2med.ru/about"])
    assert "совпадает" in mc.yandex_doublecheck("X", "Самара", "a2med.ru")
    # поддомен сети считается совпадением
    assert "домен сети" in mc.yandex_doublecheck("X", "Самара",
                                                 "samara.a2med.ru")
    r = mc.yandex_doublecheck("X", "Самара", "smitra.ru")
    assert "РАСХОЖДЕНИЕ" in r and "a2med.ru" in r
    monkeypatch.setattr(mc, "yandex_map_urls", lambda name, city, n=5: [])
    assert "не найдена" in mc.yandex_doublecheck("X", "Самара", "smitra.ru")


def test_catalog_minisites_are_aggregators():
    """Мини-сайты каталогов (заказчик, пачка 1, 2026-08-28): clients.site и
    inni.info публикуют ИНН организации — «подтверждён ИНН» срабатывал ложно.
    9 строк из 200. Это агрегаторные карточки, не официальные сайты."""
    from src.discovery import is_aggregator_domain
    assert is_aggregator_domain("rpkavrora.clients.site")
    assert is_aggregator_domain("avk-med.inni.info")
    assert is_aggregator_domain("bananadent.clients.site")
    assert not is_aggregator_domain("a2med.ru")
    assert not is_aggregator_domain("smitra.ru")


def test_search_waits_for_rzn(tmp_path):
    """Гвард (заказчик, пачка 2, 2026-08-28): РЗН остановился предохранителем,
    а поиск шёл дальше с лестницей без лицензий (2 из 3 ступеней слепы).
    Теперь sites/search не берут строку, пока её ИНН не проверен реестром."""
    import sqlite3

    from src.rzn_licenses import ensure_tables
    from src.test40 import ensure_t40_tables
    db = sqlite3.connect(":memory:")
    ensure_t40_tables(db)
    ensure_tables(db)
    db.execute("INSERT INTO t40_companies (row_no, inn, name, city, sites_raw)"
               " VALUES (1, '1111111111', 'А', 'Уфа', 'a.ru')")
    db.execute("INSERT INTO t40_companies (row_no, inn, name, city, sites_raw)"
               " VALUES (2, '2222222222', 'Б', 'Уфа', 'b.ru')")
    db.execute("INSERT INTO rzn_checked (inn, status) "
               "VALUES ('1111111111', 'проверен')")
    rows = db.execute(
        "SELECT c.inn FROM t40_companies c "
        "WHERE c.sites_raw IS NOT NULL AND c.found_site IS NULL "
        "AND c.site_source IS NULL "
        "AND EXISTS (SELECT 1 FROM rzn_checked r WHERE r.inn=c.inn "
        "AND r.status='проверен')").fetchall()
    assert rows == [("1111111111",)]      # Б ждёт реестра


def test_rzn_import_dump(tmp_path):
    """Вливка локального сбора (2026-08-29): rzn_dump.jsonl проходит тот же
    парсер, что онлайн-путь; чужой ИНН отбрасывается; битые строки не валят."""
    import json
    import sqlite3

    from src.rzn_import import import_dump
    row = {"col1": {"label": "Л041-01170-02/00362563"},
           "col2": {"label": "25.07.2017"}, "col3": {"label": "АО Тест"},
           "col7": {"label": "0273028277"},
           "objects": [{"address_fact": "Уфа, ул. Ленина, д. 1",
                        "city": "Уфа", "region": "РБ",
                        "activity": "…по: косметологии"}]}
    foreign = {"col1": {"label": "ЛО-77-01-000001"},
               "col7": {"label": "9999999999"}}
    f = tmp_path / "dump.jsonl"
    f.write_text(
        json.dumps({"inn": "0273028277", "data": {"data": [row, foreign]}},
                   ensure_ascii=False) + "\n"
        + "битая строка\n"
        + json.dumps({"inn": "1234567890", "data": {"data": []}}) + "\n",
        encoding="utf-8")
    db = sqlite3.connect(":memory:")
    st = import_dump(db, str(f))
    assert st["влито ИНН"] == 2 and st["битых строк"] == 1
    assert db.execute("SELECT COUNT(*) FROM rzn_licenses "
                      "WHERE inn='0273028277'").fetchone()[0] == 1  # чужая — нет
    assert db.execute("SELECT specialties FROM rzn_licenses "
                      "WHERE inn='0273028277'").fetchone()[0] == "косметологи"
    assert db.execute("SELECT status, licenses_n FROM rzn_checked "
                      "WHERE inn='1234567890'").fetchone() == ("проверен", 0)


def test_legal_registries_are_aggregators():
    """Разбор конверсии пачки 2 (2026-08-29): юр-справочники съедали все
    слоты кандидатов у всех 85 ненайденных — в чёрный список."""
    from src.discovery import is_aggregator_domain
    for d in ("checko.ru", "rusprofile.ru", "audit-it.ru", "list-org.com",
              "zachestnyibiznes.ru", "focus.kontur.ru", "companies.rbc.ru"):
        assert is_aggregator_domain(d), d
    assert not is_aggregator_domain("ava-kazan.ru")


def test_no_intermediate_verdicts(monkeypatch):
    """Заказчик, 2026-08-31: «мне нужен сайт именно той компании, ИНН и
    название которой стоит в строке. И точка». Кандидат, не прошедший
    лестницу ИНН → номер лицензии → адрес лицензии, НЕ порождает никакого
    промежуточного вердикта — только None (строка получит «сайт не найден»)."""
    from src import test40
    monkeypatch.setattr("src.site_finder.flexible_contact_texts",
                        lambda d, **k: ["<html><body>Клиника «Скандинавия», "
                                        "оператор АО «АВА-КАЗАНЬ», приём "
                                        "врачей, запись на приём</body></html>"])
    monkeypatch.setattr("src.site_finder.triple_check",
                        lambda *a, **k: {"verdict": None, "evidence": ""})
    assert test40._check_candidates_flex(
        "1655146267", "АВА-КАЗАНЬ, АО", "Казань", ["ava-kazan.ru"]) is None


def test_page_links_survive_broken_href():
    """run 33249946208: href «http://[…» (незакрытая скобка → Invalid IPv6
    URL) ронял весь этап обхода. Битая ссылка пропускается."""
    from src.site_checker import _page_links
    html = ('<html><a href="http://[bad">кривая</a>'
            '<a href="/price/">Цены</a></html>')
    links = _page_links(html, "https://x.ru")
    assert ("https://x.ru/price/", "Цены") in links
    assert all("[bad" not in u for u, _ in links)


def test_license_addr_parser_real_rzn_formats():
    """Разбор 2026-09-03 (ложные отрицания 5406346898, 6670254298): 140 из
    371 строки «сайт не найден» имели адреса лицензий, которые старый regex
    не превращал ни в один паттерн. Реальные форматы РЗН из выборки."""
    from src.site_finder import license_addr_patterns
    cases = [
        ("630004, Новосибирская область, г. Новосибирск, пр-кт Димитрова, "
         "зд. 1, помещения: 1-11 (первый этаж)", ("димитрова", "1")),
        ("620049, Свердловская область, г. Екатеринбург, ул. Техническая, "
         "д. 18-б", ("техническая", "18б")),
        ("620050, Свердловская область, г. Екатеринбург, ул. Техническая, "
         "дом 14, корпус 1", ("техническая", "14к1")),
        ("420111, Республика Татарстан, г.Казань, ул.Профсоюзная, зд.19/15",
         ("профсоюзная", "19/15")),
        ("344068, Ростовская область, г. Ростов-на-Дону, ул. Герасименко, "
         "дом №5", ("герасименко", "5")),
        ("454030, Челябинская область, г. Челябинск, ул. Бейвеля, 72, "
         "пом. 144", ("бейвеля", "72")),
        ("454021, Челябинская область, г. Челябинск, пр. Победы, д. 356",
         ("победы", "356")),
        ("625000, Тюменская область, г. Тюмень, Урицкого, 36",
         ("урицкого", "36")),
        ("633204, Новосибирская область, г. Искитим, Молдавская, дом 50",
         ("молдавская", "50")),
        ("350001, Краснодарский край, г. Краснодар, ул. Ким, дом 143",
         ("ким", "143")),
        ("344037, Ростовская область, г. Ростов-на-Дону, ул. Буйнакская. 2",
         ("буйнакская", "2")),
        ("625048, Тюменская область, г. Тюмень, ул. 4-я Челюскинцев, д. 1",
         ("4-я челюскинцев", "1")),
        ("420100, Республика Татарстан, г. Казань, Проспект Победы, "
         "д. 182 Б.", ("победы", "182б")),
    ]
    for addr, want in cases:
        assert want in license_addr_patterns([addr]), addr


def test_license_addr_match_house_letter_and_corpus():
    """Литера дома обязана совпасть (18 ≠ 18б); корпус из лицензии при
    указанном НА САЙТЕ другом корпусе отвергает совпадение (кейс mkm66:
    Техническая 14 к.2 — другое юрлицо, чем лицензия на 14 к.1)."""
    from src.site_finder import license_addr_in_text
    assert license_addr_in_text("ул. Техническая, 18б",
                                [("техническая", "18б")])
    assert license_addr_in_text("ул. Техническая, 18-Б офис 5",
                                [("техническая", "18б")])
    assert license_addr_in_text("ул. Техническая, 18",
                                [("техническая", "18б")]) is None
    assert license_addr_in_text("ул. Техническая, д. 14, к. 2",
                                [("техническая", "14к1")]) is None
    assert license_addr_in_text("ул. Техническая, д. 14, корп. 1",
                                [("техническая", "14к1")])
    assert license_addr_in_text("ул. Техническая, 14",
                                [("техническая", "14к1")])


def test_license_addr_match_only_after_street():
    """Дом ищется ПОСЛЕ улицы до следующего уличного маркера (кейс
    detdoc.ru: список филиалов «…техническая, 18б ул. шварца, 14» ловил
    дом соседнего адреса окном ±120 в обе стороны)."""
    from src.site_finder import license_addr_in_text
    branches = ("ул. циолковского, 29 ул. техническая, 18б "
                "ул. академика шварца, 14")
    assert license_addr_in_text(branches, [("техническая", "14")]) is None
    assert license_addr_in_text(branches, [("техническая", "18б")])
    # короткая улица не находится внутри чужого слова
    assert license_addr_in_text("не иском доме 143",
                                [("ким", "143")]) is None


def test_head_tail_keeps_footer():
    """Обрезка страницы: одностраничник 1.88 МБ держал адрес в футере —
    прежний text[:200000] его отрезал (novomedclinic.ru, 2026-09-03)."""
    from src.site_finder import _head_tail
    page = "x" * 500000 + "АДРЕС: пр-т Димитрова, 1"
    assert "Димитрова" in _head_tail(page)
    assert _head_tail("y" * 1000) == "y" * 1000


def test_clean_map_name_drops_opf_segment():
    """Запрос в карты без ОПФ-сегментов СПАРК: «НОВЫЕ МЕТОДЫ, ООО КДЛ»
    притягивал карточку федеральной сети KDL (2026-09-03, 6670254298)."""
    from src.map_candidates import clean_map_name
    assert clean_map_name("НОВЫЕ МЕТОДЫ, ООО КДЛ") == "НОВЫЕ МЕТОДЫ"
    assert clean_map_name("НОВОМЕД, ООО") == "НОВОМЕД"
    assert clean_map_name("АИСТ") == "АИСТ"
    assert clean_map_name("ООО ГАРМОНИЯ") == "ООО ГАРМОНИЯ"  # некуда резать


def test_fio_masked_but_meaning_kept():
    """152-ФЗ, принцип минимизации (2026-09-04): правило «ФИО не собираем»
    было записано в CLAUDE.md и sources.yaml, но кода не имело — паспорт
    дословно копировал блок «Наши врачи», и 421 паспорт из 1134 содержал
    настоящие имена, уезжавшие к судьям-нейронкам."""
    from src.depersonalize import has_fio, mask_fio
    # маскируем
    for txt in ("Нигманов Финат Гилевич Директор",
                "ШАЙХУТДИНОВА ГУЛЬНАЗ КАМИЛОВНА",
                "Квашнина Светлана Анатольевна | Врач акушер-гинеколог",
                "Приём ведёт Иванов П. С.",
                "Соколов Иван Кузьмич"):
        assert "[ФИО]" in mask_fio(txt), txt
        assert not has_fio(mask_fio(txt)), txt
    # НЕ трогаем то, на чём стоит классификация
    for txt in ("Дерматология | Косметология | Трихология | Цены",
                "гинеколог, дерматовенеролог, косметолог, онколог",
                "Удаление новообразований кожи радиохирургическим методом",
                "г. Нижний Новгород, ул. Максима Горького, д. 65а",
                "ООО Клиника Доктора Петрова",
                "Проспект Победы, Зеленодольск"):
        assert mask_fio(txt) == txt, txt
    # специальность рядом с именем переживает маскировку
    assert "онколог" in mask_fio("Петров Иван Сергеевич | онколог")


def test_passport_is_depersonalized():
    """Паспорт отдаётся судьям уже без имён — фильтр стоит на выходе
    build_passport, а не в вызывающем коде."""
    from src.passport import build_passport
    html = ("<html><head><title>Клиника</title></head><body>"
            "<a href='/derma'>Дерматология</a><a href='/doctors'>Врачи</a>"
            "<h1>Наши врачи</h1><h2>Квашнина Светлана Анатольевна</h2>"
            "<h2>Дерматовенеролог</h2></body></html>")
    p = build_passport("x.ru", {"https://x.ru/": html}, {})
    assert "Квашнина" not in p and "Анатольевна" not in p
    assert "[ФИО]" in p
    assert "Дерматология" in p and "Дерматовенеролог" in p
