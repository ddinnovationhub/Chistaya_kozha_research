"""Keenable — второй источник кандидатов (заказчик, 2026-09-02).
Правила: только URL/домен на выходе; отказ авторизации и сетевые сбои
источник глушат, но этап не валят; квота чтится до запроса."""

import httpx

from src import keenable


class _Resp:
    def __init__(self, code, payload=None, text="", headers=None):
        self.status_code, self._p, self.text = code, payload or {}, text
        self.headers = headers or {}

    def json(self):
        return self._p


def _patch_post(monkeypatch, responses):
    calls = []

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append({"url": url, "headers": headers, "json": json})
        return responses.pop(0)
    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.setattr(keenable, "spend", lambda service, n=1: True)
    return calls


def test_search_returns_only_url_domain_title(monkeypatch):
    monkeypatch.setenv("KEENABLE_API_KEY", "keen_test")
    calls = _patch_post(monkeypatch, [_Resp(200, {"results": [
        {"url": "https://www.med-aura.ru/kontakty", "title": "Аура",
         "snippet": "ДОЛЖЕН БЫТЬ ОТБРОШЕН", "description": "тоже"},
        {"url": "", "title": "пусто"}]})])
    out = keenable.keenable_search("АУРА Челябинск", n=20)
    assert out == [{"url": "https://www.med-aura.ru/kontakty",
                    "domain": "med-aura.ru", "title": "Аура"}]
    assert calls[0]["url"] == keenable.API                     # ключевой путь
    assert calls[0]["headers"]["X-API-Key"] == "keen_test"
    assert calls[0]["json"] == {"query": "АУРА Челябинск", "max_results": 20}
    assert "snippet" not in str(out)                           # не храним


def test_public_endpoint_without_key(monkeypatch):
    monkeypatch.delenv("KEENABLE_API_KEY", raising=False)
    calls = _patch_post(monkeypatch, [_Resp(200, {"results": []})])
    keenable.keenable_search("тест")
    assert calls[0]["url"].endswith("/public")
    assert calls[0]["headers"]["X-Keenable-Title"] == keenable.APP_TITLE


def test_auth_failure_is_silent_skip_not_crash(monkeypatch):
    monkeypatch.setenv("KEENABLE_API_KEY", "bad")
    _patch_post(monkeypatch, [_Resp(403, text="forbidden")])
    assert keenable.keenable_search("тест") == []


def test_429_retries_once_with_retry_after(monkeypatch):
    monkeypatch.setenv("KEENABLE_API_KEY", "k")
    monkeypatch.setattr(keenable.time, "sleep", lambda s: None)
    calls = _patch_post(monkeypatch, [
        _Resp(429, headers={"Retry-After": "1"}),
        _Resp(200, {"results": [{"url": "https://a.ru", "title": "A"}]})])
    assert keenable.keenable_search("тест")[0]["domain"] == "a.ru"
    assert len(calls) == 2


def test_quota_exhausted_means_no_request(monkeypatch):
    monkeypatch.setenv("KEENABLE_API_KEY", "k")
    calls = _patch_post(monkeypatch, [])
    monkeypatch.setattr(keenable, "spend", lambda service, n=1: False)
    assert keenable.keenable_search("тест") == []
    assert calls == []


def test_keenable_registered_as_source_and_quota():
    import yaml

    from src.quota import LIMITS
    cfg = yaml.safe_load(open("config/sources.yaml", encoding="utf-8"))
    ids = {s["id"] for s in cfg["sources"]}
    assert "keenable" in ids
    assert LIMITS["keenable"] <= 3300          # ≈100 000/мес, не больше
    costs = yaml.safe_load(open("config/thresholds.yaml", encoding="utf-8"))
    assert costs["cost_per_request_rub"]["keenable"] == 0.0
