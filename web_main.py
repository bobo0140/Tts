"""
web_main.py — уеб интерфейсът.

Прозорецът е WebView (на Windows 11 вграденият WebView2), а цялата логика
седи в engine.py. Този файл е само мостът между двете.
"""

import json
import os
import sys
import threading
import webbrowser
from pathlib import Path

import webview

from core_lib import (
    APP_VERSION, BASE_DIR, LIVE_MODELS, PERSONALITIES, PROFILES,
    TEXT_MODELS, VOICE_REGISTRY,
)
from engine import Engine

SETTINGS_PATH = BASE_DIR / "settings.json"

# кои настройки се пазят във файла
PERSIST = [
    "connection_mode", "username_entry", "api_key_entry", "tikfinity_url_entry",
    "output_mode", "filter_var", "filter_entry", "strip_mentions_var",
    "shlyokavitsa_var", "spam_filter_var", "heart_me_filter_var", "max_chars_entry",
    "skip_instead_of_truncate_var", "max_name_len_entry", "voice_engine_menu",
    "voice_shuffle_var", "speed_slider", "expressiveness_slider", "volume_slider",
    "voice_effect_menu", "announce_follow_var", "announce_share_var",
    "announce_gift_var", "announce_viewers_var", "viewer_interval_entry",
    "gemini_api_key_entry", "streamer_name_entry", "personality_menu",
    "humor_slider", "custom_prompt_entry", "ai_enabled_var", "ai_speak_var",
    "gemini_model_entry", "ai_frequency_menu", "ai_every_n_entry",
    "ai_min_chars_entry", "live_model_entry", "live_voice_menu",
    "live_volume_slider", "live_autoreconnect_var", "live_feed_follow_var",
    "live_feed_share_var", "live_feed_gift_var", "live_feed_comment_var",
    "live_feed_viewers_var", "live_every_n_entry", "live_batch_seconds_entry",
    "mic_device_menu", "output_device_menu", "half_duplex_var", "hotkey_entry",
    "hotkey_mode_menu", "mute_hotkey_entry", "test_name_entry",
    "test_comment_entry", "burst_count_entry", "debug_var",
]


def resource(name: str) -> str:
    """Пътят до файл, който е опакован вътре в .exe-то."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


class Api:
    """Всичко тук се вика директно от JavaScript."""

    def __init__(self):
        self.engine = Engine()
        self._defaults = self._snapshot()
        self._load()

    # ---------------- настройки ----------------
    def _snapshot(self) -> dict:
        e = self.engine
        data = {k: getattr(e, k).get() for k in PERSIST if hasattr(e, k)}
        data["profile"] = e.profile
        data["_shuffle"] = {k: v.get() for k, v in e.shuffle_vars.items()}
        return data

    def _apply(self, data: dict):
        e = self.engine
        for k, v in data.items():
            if k in PERSIST and hasattr(e, k):
                try:
                    getattr(e, k).set(v)
                except Exception:
                    pass
        if data.get("profile") in PROFILES:
            e.profile = data["profile"]
        for k, v in (data.get("_shuffle") or {}).items():
            if k in e.shuffle_vars:
                e.shuffle_vars[k].set(bool(v))
        e._refresh_cfg()

    def _load(self):
        if not SETTINGS_PATH.exists():
            return
        try:
            data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        except Exception as ex:
            self.engine._log(f"[Настройки] Повреден файл: {ex}")
            return
        self._apply(data)
        ver = data.get("app_version", "неизвестна")
        if ver != APP_VERSION:
            self.engine._log(
                f"[Настройки] Заредени от версия {ver} (сегашната е {APP_VERSION})."
            )
        else:
            self.engine._log("[Настройки] Заредени от предишния път.")
        self.engine._log_voice_summary()

    def save_settings(self, silent=False):
        try:
            data = self._snapshot()
            data["app_version"] = APP_VERSION
            SETTINGS_PATH.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            if not silent:
                self.engine._log("[Настройки] Запазени.")
        except Exception as ex:
            self.engine._log(f"[Настройки] Грешка при запазване: {ex}")

    def restore_defaults(self):
        e = self.engine
        if e.live_running:
            e.stop_live_ai()
        if e.is_running:
            e.stop_listening()
        e.set_muted(False)
        self._apply(self._defaults)
        e.announced_sharers.clear(); e.heart_me_senders.clear()
        e.confirmed_subscribers.clear(); e.recent_comments.clear()
        e.known_moderators.clear()
        with e.live_buffer_lock:
            e.live_event_buffer.clear()
        e.ai_comment_counter = e.live_comment_counter = 0
        e.stat_follows = e.stat_shares = e.stat_gifts = 0
        e.stat_comments = e.stat_viewers = 0
        e.live_resume_handle = None
        try:
            SETTINGS_PATH.unlink()
        except Exception:
            pass
        e._log("[Настройки] ✓ Върнати към фабричните. ВНИМАНИЕ: ключовете също.")

    # ---------------- към интерфейса ----------------
    def get_settings(self) -> dict:
        e = self.engine
        d = self._snapshot()
        d["_voices"] = [v["label"] for v in VOICE_REGISTRY.values()]
        d["_voice_keys"] = list(VOICE_REGISTRY.keys())
        d["_voice_short"] = {k: v["short"] for k, v in VOICE_REGISTRY.items()}
        d["_personalities"] = list(PERSONALITIES.keys())
        d["_text_models"] = TEXT_MODELS
        d["_live_models"] = LIVE_MODELS
        d["_mic_devices"] = getattr(e, "mic_device_list", ["(по подразбиране)"])
        d["_out_devices"] = getattr(e, "output_device_list", ["(по подразбиране)"])
        return d

    def get_state(self) -> dict:
        e = self.engine
        return {
            "status": e.status_text, "status_kind": e.status_kind,
            "live_status": e.live_status_text, "live_status_kind": e.live_status_kind,
            "running": e.is_running, "muted": e.muted, "live": e.live_running,
            "viewers": e.stat_viewers, "follows": e.stat_follows,
            "shares": e.stat_shares, "gifts": e.stat_gifts, "comments": e.stat_comments,
            "log_len": len(e.log_lines),
        }

    def get_log(self):
        return list(self.engine.log_lines)

    def clear_log(self):
        self.engine.log_lines.clear()

    def log_copied(self):
        self.engine._log("[Лог] Копиран в клипборда.")

    def save_log(self):
        import time as _t
        try:
            p = BASE_DIR / f"log-{_t.strftime('%Y%m%d-%H%M%S')}.txt"
            p.write_text("\n".join(self.engine.log_lines), encoding="utf-8")
            self.engine._log(f"[Лог] Запазен във {p.name}.")
        except Exception as ex:
            self.engine._log(f"[Лог] Грешка: {ex}")

    def set_setting(self, key, value):
        e = self.engine
        if hasattr(e, key):
            try:
                getattr(e, key).set(value)
                e._refresh_cfg()
            except Exception:
                pass

    def set_shuffle(self, key, value):
        if key in self.engine.shuffle_vars:
            self.engine.shuffle_vars[key].set(bool(value))
            self.engine._refresh_cfg()

    def set_profile(self, value):
        self.engine.profile = value
        if value == "Само TTS":
            self.engine.output_mode.set("TTS гласове")
        elif value == "Само Live AI":
            self.engine.output_mode.set("Само Live AI")
        self.engine._refresh_cfg()
        self.engine._log(f"[Профил] {value}")

    def open_key_page(self):
        webbrowser.open("https://aistudio.google.com/apikey")

    # ---------------- съветник при стартиране ----------------
    def needs_setup(self) -> bool:
        """Показваме съветника при първо пускане или ако няма ключ/потребител."""
        if not SETTINGS_PATH.exists():
            return True
        e = self.engine
        if e.profile != "Само TTS" and not e.gemini_api_key_entry.get().strip():
            return True
        if e.connection_mode.get().startswith("Директно") and not e.username_entry.get().strip():
            return True
        return False

    def run_wizard(self, mode):
        self.engine.profile = mode
        if mode == "Само TTS":
            self.engine.output_mode.set("TTS гласове")
        elif mode == "Само Live AI":
            self.engine.output_mode.set("Само Live AI")
        self.engine._refresh_cfg()
        self.engine.run_wizard(mode)

    def wizard_state(self):
        e = self.engine
        return {"steps": e.wizard_steps, "done": e.wizard_done, "ok": e.wizard_ok}

    def finish_setup(self):
        self.engine.setup_complete = True
        self.save_settings(silent=True)
        self.engine._log("[Проверка] Готово — приятен стрийм!")

    def apply_optimal_settings(self):
        e = self.engine
        e.voice_engine_menu.set(VOICE_REGISTRY["piper"]["label"])
        e.voice_effect_menu.set("Няма")
        e.speed_slider.set(0.85); e.expressiveness_slider.set(0.9)
        e.volume_slider.set(1.3); e.live_volume_slider.set(1.0)
        e.spam_filter_var.set(True); e.strip_mentions_var.set(True)
        e.shlyokavitsa_var.set(True); e.skip_instead_of_truncate_var.set(True)
        e.max_chars_entry.set("180"); e.max_name_len_entry.set("20")
        e.gemini_model_entry.set(TEXT_MODELS[0])
        e.ai_frequency_menu.set("На всеки N-ти")
        e.ai_every_n_entry.set("12"); e.ai_min_chars_entry.set("8")
        e.live_batch_seconds_entry.set("8"); e.live_every_n_entry.set("6")
        e.live_autoreconnect_var.set(True); e.half_duplex_var.set(True)
        e.announce_follow_var.set(True); e.announce_share_var.set(True)
        e.announce_gift_var.set(True); e.announce_viewers_var.set(False)
        e._refresh_cfg()
        e._log("[Настройки] ✓ Приложени оптимални стойности.")
        e._log_voice_summary()


def _delegate(name):
    def call(self, *a):
        fn = getattr(self.engine, name, None)
        if fn:
            try:
                return fn(*a)
            except Exception as ex:
                self.engine._log(f"[Грешка] {name}: {ex}")
    return call


# методите на ядрото, които интерфейсът вика директно
for _m in [
    "start_listening", "stop_listening", "start_live_ai", "stop_live_ai",
    "toggle_mute", "preview_voice", "reset_stats", "reset_voice_settings",
    "refresh_mic_devices", "refresh_output_devices", "toggle_hotkey",
    "test_comment", "test_follow", "test_share", "test_gift", "test_heart_me",
    "test_viewers", "test_spam", "test_gift_streak", "test_reset",
    "test_burst_follows", "test_burst_shares", "test_burst_gifts",
    "test_burst_comments", "test_gemini_connection", "test_ai_commentator",
    "test_microphone", "test_audio_output", "test_api_full", "test_live_feed",
]:
    setattr(Api, _m, _delegate("_" + _m if not _m.startswith("_") else _m))


def main():
    api = Api()

    def on_closing():
        api.save_settings(silent=True)
        try:
            api.engine.stop_live_ai()
            api.engine.stop_listening()
        except Exception:
            pass

    window = webview.create_window(
        f"TikTok TTS {APP_VERSION}",
        resource("ui.html"),
        js_api=api,
        width=1180, height=820, min_size=(940, 650),
        background_color="#0E0E13",
    )
    window.events.closing += on_closing

    # автоматично запазване на всяка минута
    def autosave():
        while True:
            import time as _t
            _t.sleep(60)
            api.save_settings(silent=True)

    threading.Thread(target=autosave, daemon=True).start()
    webview.start()


if __name__ == "__main__":
    main()
