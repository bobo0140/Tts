"""
live_module.py — състояние и броячи за Live AI.

Досега състоянието беше разпръснато из няколко отделни флага
(live_running, live_ws, live_model_speaking, live_fatal). Точно затова
се получаваха заяждания: един флаг оставаше вдигнат, а другите не.

Тук състоянието е ЕДНО и преходите са явни.
"""

import threading
import time
from collections import deque

# Възможните състояния. Само едно е вярно във всеки момент.
OFF = "off"                  # изключен
CONNECTING = "connecting"    # отваря сесия
READY = "ready"              # готов, чака
SPEAKING = "speaking"        # AI-то говори в момента
LISTENING = "listening"      # приема звук от микрофона
RECONNECTING = "reconnecting"
FAILED = "failed"            # окончателна грешка (напр. лош ключ)

LABELS = {
    OFF: "изключен",
    CONNECTING: "свързва се",
    READY: "готов",
    SPEAKING: "AI-то говори",
    LISTENING: "слуша те",
    RECONNECTING: "пресвързва се",
    FAILED: "спрян след грешка",
}

COLORS = {
    OFF: "idle", CONNECTING: "warn", READY: "ok", SPEAKING: "ok",
    LISTENING: "ok", RECONNECTING: "warn", FAILED: "err",
}


class LiveMetrics:
    def __init__(self):
        self._lock = threading.Lock()
        self.state = OFF
        self.state_since = time.time()
        self.detail = ""

        self.sessions = 0
        self.reconnects = 0
        self.turns = 0
        self.interrupts = 0
        self.setup_level = 0
        self.setup_rejections = 0

        self.audio_out_bytes = 0     # звук ОТ AI-то
        self.audio_in_chunks = 0     # звук КЪМ AI-то (по 100 ms)
        self.events_sent = 0
        self.events_dropped = 0

        self.session_started = 0.0
        self.durations = deque(maxlen=20)
        self.last_error = ""
        self.last_error_ts = 0.0
        self.last_turn_ts = 0.0

    # ------------------------------------------------------------------
    def set_state(self, state, detail=""):
        with self._lock:
            if state != self.state:
                # затваряме предишната сесия за статистиката
                if self.state in (READY, SPEAKING, LISTENING) and state in (OFF, FAILED, RECONNECTING, CONNECTING):
                    if self.session_started:
                        self.durations.append(time.time() - self.session_started)
                        self.session_started = 0.0
                if state in (READY,) and self.state == CONNECTING:
                    self.session_started = time.time()
                self.state = state
                self.state_since = time.time()
            self.detail = detail

    def note_session(self):
        with self._lock:
            self.sessions += 1

    def note_reconnect(self):
        with self._lock:
            self.reconnects += 1

    def note_turn(self):
        with self._lock:
            self.turns += 1
            self.last_turn_ts = time.time()

    def note_interrupt(self):
        with self._lock:
            self.interrupts += 1

    def note_setup(self, level, rejected=False):
        with self._lock:
            self.setup_level = level
            if rejected:
                self.setup_rejections += 1

    def note_audio_out(self, n_bytes):
        with self._lock:
            self.audio_out_bytes += n_bytes

    def note_audio_in(self):
        with self._lock:
            self.audio_in_chunks += 1

    def note_event(self, dropped=False):
        with self._lock:
            if dropped:
                self.events_dropped += 1
            else:
                self.events_sent += 1

    def note_error(self, text):
        with self._lock:
            self.last_error = (text or "")[:160]
            self.last_error_ts = time.time()

    # ------------------------------------------------------------------
    def snapshot(self) -> dict:
        now = time.time()
        with self._lock:
            durs = list(self.durations)
            avg = round(sum(durs) / len(durs)) if durs else 0
            live_now = round(now - self.session_started) if self.session_started else 0
            return {
                "state": self.state,
                "label": LABELS.get(self.state, self.state),
                "color": COLORS.get(self.state, "idle"),
                "detail": self.detail,
                "in_state_sec": round(now - self.state_since),
                "sessions": self.sessions,
                "reconnects": self.reconnects,
                "turns": self.turns,
                "interrupts": self.interrupts,
                "setup_level": self.setup_level,
                "setup_rejections": self.setup_rejections,
                # 24 kHz, 16 бита -> 48000 байта в секунда; данните са base64 (~3/4)
                "audio_out_sec": round(self.audio_out_bytes * 0.75 / 48000, 1),
                "audio_in_sec": round(self.audio_in_chunks * 0.1, 1),
                "events_sent": self.events_sent,
                "events_dropped": self.events_dropped,
                "session_sec": live_now,
                "avg_session_sec": avg,
                "last_error": self.last_error,
                "last_error_ago": round(now - self.last_error_ts) if self.last_error_ts else -1,
                "last_turn_ago": round(now - self.last_turn_ts) if self.last_turn_ts else -1,
            }
