"""Программный счётчик бюджета — единственная реальная защита от перерасхода
(решение заказчика 2026-08-24: жёсткого лимита затрат в Yandex Cloud нет,
бюджет облака только уведомляет; квоты Search API меняются лишь через
техподдержку).

Контракт:
- счётчик запросов и расчётной стоимости живёт в data/budget.json,
  переживает перезапуск и суммируется ПО ВСЕМУ ПРОЕКТУ, не по одному прогону;
- 80% потолка → предупреждение в лог;
- 100% → жёсткая остановка (BudgetExceededError), всё собранное уже на диске;
- стоимость запроса берётся из config/thresholds.yaml (правится без кода).
"""

import json
import pathlib
import threading

import yaml

from src.errors import BudgetExceededError

_THRESHOLDS = pathlib.Path("config/thresholds.yaml")
_STATE = pathlib.Path("data/budget.json")


class BudgetTracker:
    def __init__(self, thresholds_path: pathlib.Path = _THRESHOLDS,
                 state_path: pathlib.Path = _STATE):
        cfg = yaml.safe_load(thresholds_path.read_text(encoding="utf-8"))
        self.ceiling_rub = float(cfg["budget_rub"])
        self.warn_share = float(cfg["budget_warn_share"])
        self.cost_per_request = {k: float(v) for k, v in cfg["cost_per_request_rub"].items()}
        self.state_path = state_path
        if state_path.exists():
            self.state = json.loads(state_path.read_text(encoding="utf-8"))
        else:
            self.state = {"spent_rub": 0.0, "requests": {}}
        self._warned = self.spent >= self.ceiling_rub * self.warn_share

    @property
    def spent(self) -> float:
        return float(self.state["spent_rub"])

    def _save(self):
        self.state_path.parent.mkdir(exist_ok=True)
        self.state_path.write_text(
            json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8")

    _LOCK = threading.Lock()   # параллельный поиск (2026-09-02): счётчик общий

    def charge(self, service: str, n_requests: int = 1):
        """Списать n запросов сервиса. Вызывается ПЕРЕД запросом:
        при пробитом потолке запрос не уходит. Потокобезопасно."""
        with self._LOCK:
            return self._charge(service, n_requests)

    def _charge(self, service: str, n_requests: int = 1):
        cost = self.cost_per_request.get(service, 0.0) * n_requests
        projected = self.spent + cost
        if projected > self.ceiling_rub:
            self._save()
            raise BudgetExceededError(
                f"⛔ БЮДЖЕТ ИСЧЕРПАН: {projected:.0f} ₽ из {self.ceiling_rub:.0f} ₽ "
                f"(попытка {service} × {n_requests}). Прогон остановлен, "
                f"всё собранное сохранено. Счётчик: {self.state_path}")
        self.state["spent_rub"] = projected
        self.state["requests"][service] = self.state["requests"].get(service, 0) + n_requests
        self._save()
        if not self._warned and projected >= self.ceiling_rub * self.warn_share:
            self._warned = True
            print(f"⚠ ПРЕДУПРЕЖДЕНИЕ: израсходовано {projected:.0f} ₽ из "
                  f"{self.ceiling_rub:.0f} ₽ ({projected / self.ceiling_rub:.0%})")

    # charge_tokens удалён (решение заказчика 2026-08-26, п.6): внешние
    # модельные API не используются, токенных списаний в проекте нет.

    def report(self) -> str:
        reqs = ", ".join(f"{k}: {v}" for k, v in sorted(self.state["requests"].items())) or "—"
        return (f"Бюджет проекта: {self.spent:.2f} ₽ из {self.ceiling_rub:.0f} ₽ "
                f"({self.spent / self.ceiling_rub:.1%}) · запросы: {reqs}")
