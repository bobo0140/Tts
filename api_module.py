"""
api_module.py — наблюдение на заявките към Gemini.

Брои какво изпращаме, какво се връща и колко бързо. Всичко е измерено
на място, не предположено. Единственото, което НЕ можем да знаем, е
официалният остатък от квотата — Google не го дава през API. Затова
показваме нашето потребление и отказите, които сме получили.
"""

import threading
import time
from collections import deque


class ApiMetrics:
    """Броячи за един доставчик (тук: Gemini)."""

    WINDOW_MIN = 60          # секунди за "последна минута"
    WINDOW_HOUR = 3600
    KEEP = 600               # колко записа пазим

    def __init__(self):
        self._lock = threading.Lock()
        self.events = deque(maxlen=self.KEEP)   # (време, вид, модел, ms)
        self.total = 0
        self.ok = 0
        self.failed = 0
        self.by_reason = {}                     # 429 / auth / network / other
        self.by_model = {}                      # модел -> {ok, fail}
        self.last_error = ""
        self.last_error_ts = 0.0
        self.last_ok_ts = 0.0
        self.latencies = deque(maxlen=50)
        self.available_models = []
        self.models_checked_ts = 0.0
        self.backoff_until = 0.0

    # ------------------------------------------------------------------
    def record(self, model: str, ok: bool, ms: int = 0, reason: str = ""):
        now = time.time()
        with self._lock:
            self.total += 1
            self.events.append((now, "ok" if ok else "fail", model, ms))
            m = self.by_model.setdefault(model, {"ok": 0, "fail": 0})
            if ok:
                self.ok += 1
                m["ok"] += 1
                self.last_ok_ts = now
                if ms:
                    self.latencies.append(ms)
            else:
                self.failed += 1
                m["fail"] += 1
                self.by_reason[reason] = self.by_reason.get(reason, 0) + 1
                self.last_error = reason
                self.last_error_ts = now

    def set_backoff(self, seconds: float):
        self.backoff_until = time.time() + seconds

    def set_models(self, models):
        with self._lock:
            self.available_models = list(models)
            self.models_checked_ts = time.time()

    # ------------------------------------------------------------------
    def _count(self, window: int) -> tuple:
        cut = time.time() - window
        ok = fail = 0
        for ts, kind, _m, _ms in self.events:
            if ts >= cut:
                if kind == "ok":
                    ok += 1
                else:
                    fail += 1
        return ok, fail

    def snapshot(self) -> dict:
        with self._lock:
            lat = list(self.latencies)
        ok_min, fail_min = self._count(self.WINDOW_MIN)
        ok_h, fail_h = self._count(self.WINDOW_HOUR)

        avg = round(sum(lat) / len(lat)) if lat else 0
        slowest = max(lat) if lat else 0

        # Състоянието се извежда от реалните числа, не се предполага
        now = time.time()
        if now < self.backoff_until:
            state, note = "err", f"отдръпване още {int(self.backoff_until - now)} сек"
        elif self.by_reason.get("auth"):
            state, note = "err", "ключът е отхвърлян"
        elif fail_min and fail_min >= ok_min:
            state, note = "err", "повече откази, отколкото успехи"
        elif fail_min:
            state, note = "warn", f"{fail_min} отказа за последната минута"
        elif self.total == 0:
            state, note = "idle", "още няма заявки"
        else:
            state, note = "ok", "работи нормално"

        return {
            "state": state, "note": note,
            "total": self.total, "ok": self.ok, "failed": self.failed,
            "min_ok": ok_min, "min_fail": fail_min,
            "hour_ok": ok_h, "hour_fail": fail_h,
            "avg_ms": avg, "slowest_ms": slowest,
            "reasons": dict(self.by_reason),
            "models": {k: dict(v) for k, v in self.by_model.items()},
            "available": self.available_models,
            "models_age": int(now - self.models_checked_ts) if self.models_checked_ts else -1,
            "last_error": self.last_error,
            "last_error_ago": int(now - self.last_error_ts) if self.last_error_ts else -1,
            "backoff_left": max(0, int(self.backoff_until - now)),
        }


def classify(error_text: str) -> str:
    """Превръща текста на грешката в кратка причина за броене."""
    t = (error_text or "").lower()
    if "429" in t or "resource_exhausted" in t or "лимит" in t:
        return "429"
    if "api key" in t or "401" in t or "403" in t or "невалид" in t:
        return "auth"
    if "timeout" in t or "не отговори" in t:
        return "timeout"
    if "връзка" in t or "network" in t or "urlopen" in t or "1006" in t:
        return "network"
    if "1007" in t or "1008" in t or "unsupported" in t:
        return "setup"
    return "other"
