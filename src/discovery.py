"""Discovery-исполнитель (этап 5) — реализация утверждённого промпта
prompts/05_discovery_executor.md (утв. заказчиком 2026-08-24).

Producer из схемы producer/consumer: исполняет запросы, пишет кандидатов
в очередь с дедупликацией ПРИ ЗАПИСИ, ведёт журнал queries в osint.db.
Сайты кандидатов НЕ открывает. Бюджет списывается ДО каждого запроса.

Журнал различает сигналы (дополнение заказчика 2026-08-24):
  0_results — выдача пустая (запрос плохой);
  0_new     — результаты есть, ни одного нового после дедупликации (насыщение).
"""

import base64
import datetime
import json
import pathlib
import sqlite3
import time
import xml.etree.ElementTree as ET

from src.api_client import handle_api_response, yandex_search_raw
from src.budget import BudgetTracker
from src.dedup import normalize_domain, normalize_name
from src.saturation import check_saturation

# Домены-агрегаторы: их страницы — указатели на карточки, не сайты клиник.
# Ключ дедупликации для них — домен+путь (иначе все карточки схлопнутся в одну).
AGGREGATOR_DOMAINS = {
    "prodoctorov.ru", "napopravku.ru", "zoon.ru", "2gis.ru", "yandex.ru",
    "maps.yandex.ru", "vk.com", "avito.ru", "flamp.ru", "yell.ru",
    "docdoc.ru", "sberhealth.ru", "irecommend.ru", "otzovik.com",
}


def parse_yandex_xml(xml_text: str) -> list[dict]:
    """Достаёт url/domain/title из XML-выдачи Яндекс Search API.
    Сниппеты сознательно не извлекаются (sources.yaml: не храним)."""
    docs = []
    root = ET.fromstring(xml_text)
    for doc in root.iter("doc"):
        url = (doc.findtext("url") or "").strip()
        domain = (doc.findtext("domain") or "").strip() or normalize_domain(url)
        title_el = doc.find("title")
        title = "".join(title_el.itertext()).strip() if title_el is not None else ""
        if url:
            docs.append({"url": url, "domain": normalize_domain(domain), "title": title})
    return docs


class CandidateQueue:
    """Очередь кандидатов с дедупликацией при записи (G0)."""

    def __init__(self, db: sqlite3.Connection):
        self.db = db
        self._keys = {row[0] for row in db.execute("SELECT dedup_key FROM candidates")}

    @staticmethod
    def dedup_key(cand: dict) -> str:
        domain = cand.get("domain")
        if domain and domain in AGGREGATOR_DOMAINS:
            path = (cand.get("url") or "").split("://")[-1]
            return f"agg:{path.rstrip('/')}"
        if domain:
            return f"dom:{domain}"
        return f"name:{normalize_name(cand.get('title') or '')}"

    def add(self, cand: dict, query_id: str, source_id: str) -> bool:
        key = self.dedup_key(cand)
        if key in self._keys:
            return False
        self._keys.add(key)
        self.db.execute(
            "INSERT INTO candidates (dedup_key, title, url, domain, kind, "
            "discovered_by_query, source_id, discovered_at) VALUES (?,?,?,?,?,?,?,?)",
            (key, cand.get("title"), cand.get("url"), cand.get("domain"),
             "aggregator" if (cand.get("domain") in AGGREGATOR_DOMAINS) else "site",
             query_id, source_id, datetime.date.today().isoformat()))
        return True


def open_db(path: pathlib.Path = pathlib.Path("data/osint.db")) -> sqlite3.Connection:
    path.parent.mkdir(exist_ok=True)
    db = sqlite3.connect(path)
    db.executescript("""
    CREATE TABLE IF NOT EXISTS queries (
        query_id TEXT PRIMARY KEY, layer INTEGER, template_id TEXT, city TEXT,
        text TEXT, source TEXT, executed_at TEXT, n_results INTEGER,
        n_new_candidates INTEGER, wordstat_freq REAL, status TEXT);
    CREATE TABLE IF NOT EXISTS candidates (
        dedup_key TEXT PRIMARY KEY, title TEXT, url TEXT, domain TEXT, kind TEXT,
        discovered_by_query TEXT, source_id TEXT, discovered_at TEXT);
    """)
    return db


def run_discovery(city: str, queries: list[dict], limit: int = 0,
                  budget: BudgetTracker | None = None,
                  db: sqlite3.Connection | None = None) -> dict:
    budget = budget or BudgetTracker()
    db = db or open_db()
    queue = CandidateQueue(db)
    done = {row[0] for row in db.execute("SELECT query_id FROM queries WHERE status IS NOT NULL")}

    todo = [q for q in queries
            if "yandex_search_api" in q["target_sources"] and q["query_id"] not in done]
    skipped_catalog = [q for q in queries if "yandex_search_api" not in q["target_sources"]]
    total = len(todo)
    new_per_query, executed, errors = [], 0, 0
    saturation_hit = None

    for q in todo:
        if limit and executed >= limit:
            break
        budget.charge("yandex_search_api", 1)
        resp = yandex_search_raw(q["text"], n=10)
        handled = handle_api_response(resp, "Яндекс Search API")
        if handled is None:  # 503/504 после паузы или прочий код — один повтор
            resp = yandex_search_raw(q["text"], n=10)
            handled = handle_api_response(resp, "Яндекс Search API")
            if handled is None:
                errors += 1
                db.execute("INSERT OR REPLACE INTO queries VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                           (q["query_id"], q["layer"], q["template_id"], city, q["text"],
                            q["source"], datetime.datetime.now().isoformat(timespec="seconds"),
                            None, None, q.get("wordstat_freq"), "error"))
                db.commit()
                if errors / max(1, executed + errors) > 0.20 and executed + errors >= 10:
                    raise RuntimeError(
                        f"источник отдаёт ошибки >20% ({errors}/{executed + errors}) — стоп по ТЗ")
                continue

        docs = parse_yandex_xml(
            base64.b64decode(resp.json()["rawData"]).decode("utf-8"))
        n_new = sum(queue.add(d, q["query_id"], "yandex_search_api") for d in docs)
        executed += 1
        new_per_query.append(n_new)

        status = "ok" if n_new else ("0_results" if not docs else "0_new")
        db.execute("INSERT OR REPLACE INTO queries VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                   (q["query_id"], q["layer"], q["template_id"], city, q["text"],
                    q["source"], datetime.datetime.now().isoformat(timespec="seconds"),
                    len(docs), n_new, q.get("wordstat_freq"), status))
        db.commit()

        sat = check_saturation(new_per_query, total_list_len=total)
        if sat["stopped"]:
            saturation_hit = sat
            break
        time.sleep(1)  # ≤1 RPS (sources.yaml)

    n_candidates = db.execute("SELECT COUNT(*) FROM candidates").fetchone()[0]
    # правило «подозрительный ноль» (заказчик 2026-08-25) на уровне слоя:
    # слой исполнялся без ошибок, но не дал ни одного нового кандидата
    layer_stats, suspicious_layers = {}, []
    for layer, n_exec, n_new, n_err in db.execute(
            "SELECT layer, COUNT(*), COALESCE(SUM(n_new_candidates),0), SUM(status='error') "
            "FROM queries WHERE executed_at IS NOT NULL AND layer != 1 GROUP BY layer"):
        layer_stats[f"L{layer}"] = {"executed": n_exec, "new_candidates": n_new, "errors": n_err}
        if n_exec > 0 and not n_err and n_new == 0:
            suspicious_layers.append(f"L{layer}")
    return {
        "layer_stats": layer_stats,
        "suspicious_zero_layers": suspicious_layers,
        "city": city,
        "queries_total_api": total,
        "queries_executed": executed,
        "queries_errors": errors,
        "queries_catalog_deferred": len(skipped_catalog),
        "candidates_unique": n_candidates,
        "saturation": saturation_hit or {"stopped": False,
                                         "reason": "не сработал" if executed >= total
                                         else f"остановка по лимиту {limit}"},
        "budget": budget.report(),
    }
