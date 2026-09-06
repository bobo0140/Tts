"""
engine.py — приложението без графичен интерфейс.

Логиката е същата като в tkinter версията, но вместо джаджи ползва леки
заместители със същите методи (.get() / .set()). Така целият код работи
непроменен, а уеб интерфейсът само чете и записва стойности.
"""

import asyncio
import base64
import functools
import json
import os
import queue
import random
import re
import tempfile
import threading
import time
import traceback
import wave
from collections import deque
from pathlib import Path

import numpy as np
import pygame
import edge_tts
from piper import PiperVoice
from piper.config import SynthesisConfig
from piper.download_voices import download_voice

import live_module as LM
from api_module import ApiMetrics, classify
from live_module import LiveMetrics
from core_lib import (
    APP_VERSION, BASE_DIR, CONFIG_PATH, MODEL_PATH, VOICE_NAME, VOICES_DIR, HEART_ME_GIFT_NAME, LIVE_INPUT_RATE, LIVE_OUTPUT_RATE,
    LIVE_MODELS, LIVE_SYSTEM_PROMPT, LIVE_WS_URL, PERSONALITIES, PROFILES,
    SETUP_LEVELS, TEXT_MODELS, VOICE_REGISTRY, GeminiError, SetupRejected,
    apply_voice_effect, call_gemini, clean_text_for_speech, is_reasonable_name,
    mood_line, streamer_line, strip_mentions, transliterate_shlyokavitsa, _parse_ws,
)


# ---------------------------------------------------------------------------
# Заместители на джаджите — същият интерфейс, но пазят обикновена стойност
# ---------------------------------------------------------------------------
class Val:
    """Заместител на джаджа: .get() и .set() върху обикновена стойност."""

    def __init__(self, value=""):
        self._v = value

    def get(self, *_args):
        return self._v

    def set(self, value):
        self._v = value

    # за съвместимост с полетата за въвеждане
    def delete(self, *_args):
        self._v = ""

    def insert(self, _index, value):
        self._v = (self._v or "") + str(value) if self._v else str(value)

    def configure(self, **_kwargs):
        pass

    def cget(self, _key):
        return self._v


class NumVal(Val):
    def get(self, *_args):
        try:
            return float(self._v)
        except (TypeError, ValueError):
            return 0.0

    def set(self, value):
        try:
            self._v = float(value)
        except (TypeError, ValueError):
            pass


class BoolVal(Val):
    def get(self, *_args):
        return bool(self._v)

    def set(self, value):
        self._v = bool(value)


class Engine:
    """Цялата логика: TikTok, гласове, филтри, AI. Без нито един пиксел."""

    def __init__(self, on_log=None, on_state=None):
        self._on_log = on_log or (lambda _m: None)
        self._on_state = on_state or (lambda: None)

        self.log_lines = deque(maxlen=800)
        self.speech_queue = queue.Queue()

        self._init_settings()
        self._init_runtime()

        self.voice = None
        self.cfg = {}
        self._refresh_cfg()

        threading.Thread(target=self._speaker_worker, daemon=True).start()
        threading.Thread(target=self._ai_worker, daemon=True).start()
        threading.Thread(target=self._live_batch_worker, daemon=True).start()
        threading.Thread(target=self._ensure_voice_ready, daemon=True).start()
        # Засичаме устройствата веднага, за да не са празни менютата
        threading.Thread(target=self._detect_devices, daemon=True).start()

    # ------------------------------------------------------------------
    def _init_settings(self):
        self.profile = "Пълно"

        # Връзка
        self.connection_mode = Val("Директно (TikTok)")
        self.username_entry = Val("")
        self.api_key_entry = Val("")
        self.tikfinity_url_entry = Val("ws://localhost:21213/")

        # Изход
        self.output_mode = Val("TTS гласове")

        # Филтри
        self.filter_var = BoolVal(False)
        self.filter_entry = Val("")
        self.strip_mentions_var = BoolVal(True)
        self.shlyokavitsa_var = BoolVal(True)
        self.spam_filter_var = BoolVal(True)
        self.heart_me_filter_var = BoolVal(False)
        self.max_chars_entry = Val("200")
        self.skip_instead_of_truncate_var = BoolVal(False)
        self.max_name_len_entry = Val("20")

        # Глас
        self.voice_engine_menu = Val(VOICE_REGISTRY["piper"]["label"])
        self.voice_shuffle_var = BoolVal(False)
        self.shuffle_vars = {k: BoolVal(True) for k in VOICE_REGISTRY}
        self.speed_slider = NumVal(0.85)
        self.expressiveness_slider = NumVal(0.9)
        self.volume_slider = NumVal(1.3)
        self.voice_effect_menu = Val("Няма")

        # Обявявания
        self.announce_follow_var = BoolVal(True)
        self.announce_share_var = BoolVal(True)
        self.announce_gift_var = BoolVal(True)
        self.announce_viewers_var = BoolVal(False)
        self.viewer_interval_entry = Val("300")

        # AI
        self.gemini_api_key_entry = Val("")
        self.streamer_name_entry = Val("")
        self.personality_menu = Val("Балансиран")
        self.humor_slider = NumVal(50)
        self.custom_prompt_entry = Val("")
        self.ai_enabled_var = BoolVal(False)
        self.ai_speak_var = BoolVal(True)
        self.gemini_model_entry = Val(TEXT_MODELS[0])
        self.ai_frequency_menu = Val("На всеки N-ти")
        self.ai_every_n_entry = Val("10")
        self.ai_min_chars_entry = Val("6")

        # Live AI
        self.live_model_entry = Val(LIVE_MODELS[0])
        self.live_voice_menu = Val("Puck")
        self.live_volume_slider = NumVal(1.0)
        self.live_autoreconnect_var = BoolVal(True)
        self.live_feed_follow_var = BoolVal(True)
        self.live_feed_share_var = BoolVal(True)
        self.live_feed_gift_var = BoolVal(True)
        self.live_feed_comment_var = BoolVal(True)
        self.live_feed_viewers_var = BoolVal(False)
        self.live_every_n_entry = Val("5")
        self.live_batch_seconds_entry = Val("8")

        # Микрофон
        self.live_mic_var = BoolVal(False)
        self.mic_device_menu = Val("(по подразбиране)")
        self.output_device_menu = Val("(по подразбиране)")
        self.half_duplex_var = BoolVal(True)
        self.hotkey_entry = Val("f8")
        self.hotkey_mode_menu = Val("Задръж за говорене")
        self.mute_hotkey_entry = Val("f9")

        # Тест / debug
        self.test_name_entry = Val("ТестовПотребител")
        self.test_comment_entry = Val("Zdravei kak si")
        self.burst_count_entry = Val("10")
        self.debug_var = BoolVal(False)

    def _init_runtime(self):
        self.is_running = False
        self.muted = False
        self.tiktok_thread = None
        self.tiktok_loop = None
        self.tiktok_client = None
        self.tikfinity_loop = None
        self.tikfinity_ws = None

        self.recent_comments = deque(maxlen=50)
        self.heart_me_senders = set()
        self.confirmed_subscribers = set()
        self.announced_sharers = set()
        self.known_moderators = set()
        self.last_viewer_announcement_time = 0.0

        self.stat_follows = self.stat_shares = self.stat_gifts = 0
        self.stat_comments = self.stat_viewers = 0

        self.ai_request_queue = queue.Queue()
        self.ai_comment_counter = 0
        self.ai_backoff_until = 0.0

        self.live_running = False
        self.live_loop = None
        self.live_ws = None
        self.live_text_queue = queue.Queue()
        self.live_mic_queue = queue.Queue()
        self.live_mic_stream = None
        self.live_comment_counter = 0
        self.live_event_buffer = []
        self.live_buffer_lock = threading.Lock()
        self.live_buffer_started = 0.0
        self.live_last_event_ts = 0.0
        self.live_resume_handle = None
        self.live_send_stream_end = False
        self.live_setup_level = 0
        self.live_session_id = 0
        self.live_fatal = False
        self.live_model_speaking = False
        self.live_last_audio_ts = 0.0
        self.live_last_voice_ts = 0.0
        self._tr_in = ""
        self._tr_out = ""
        self.half_duplex = True
        self.mic_active = False
        self.mic_chunks_sent = 0
        self.hotkey_active = False

        self.audio_out_open = False
        self.audio_out_info = "още не е отварян"
        self.audio_fallback = False
        self.audio_written_bytes = 0
        self._fb_buf = b""
        self.api = ApiMetrics()      # броячи за заявките към Gemini
        self.live = LiveMetrics()    # състояние и броячи за Live AI

        self.wizard_steps = []
        self.wizard_done = False
        self.wizard_ok = False
        self.setup_complete = False

        self.status_text = "◌ Подготовка на гласа..."
        self.status_kind = "warn"
        self.live_status_text = "○ Live AI изключен"
        self.live_status_kind = "muted"

    # ------------------------------------------------------------------
    # Лог и състояние
    # ------------------------------------------------------------------
    def _log(self, msg: str):
        line = f"{time.strftime('%H:%M:%S')}  {msg}"
        self.log_lines.append(line)
        self._on_log(line)

    def _dbg(self, direction: str, payload):
        if not self._cfg("debug", False):
            return
        try:
            text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        except Exception:
            text = str(payload)
        if len(text) > 400:
            text = text[:400] + f"… (+{len(text) - 400} знака)"
        self._log(f"  [DEBUG {direction}] {text}")

    def _set_status(self, which, text, kind):
        if which == "live":
            self.live_status_text, self.live_status_kind = text, kind
        else:
            self.status_text, self.status_kind = text, kind
        self._on_state()

    # заместители на UI помощниците от tkinter версията
    def _ui(self, fn, *a, **k):
        try:
            fn(*a, **k)
        except Exception:
            pass

    def _set_btn(self, *_a, **_k):
        self._on_state()

    def _refresh_mute_ui(self):
        self._on_state()

    # ------------------------------------------------------------------
    def _refresh_cfg(self):
        self.cfg = {
            "gemini_key": self.gemini_api_key_entry.get().strip(),
            "gemini_model": self.gemini_model_entry.get().strip() or TEXT_MODELS[0],
            "streamer_name": self.streamer_name_entry.get().strip(),
            "ai_speak": self.ai_speak_var.get(),
            "batch_seconds": self.live_batch_seconds_entry.get(),
            "speed": float(self.speed_slider.get()),
            "expressiveness": float(self.expressiveness_slider.get()),
            "volume": float(self.volume_slider.get()),
            "effect": self.voice_effect_menu.get(),
            "voice_label": self.voice_engine_menu.get(),
            "shuffle": self.voice_shuffle_var.get(),
            "shuffle_pool": [k for k, v in self.shuffle_vars.items() if v.get()],
            "output_mode": self.output_mode.get(),
            "live_volume": float(self.live_volume_slider.get()),
            "debug": self.debug_var.get(),
            "half_duplex": self.half_duplex_var.get(),
            "personality": self.personality_menu.get(),
            "humor": int(self.humor_slider.get()),
            "custom_prompt": self.custom_prompt_entry.get().strip(),
        }
        self.half_duplex = self.cfg["half_duplex"]

    def _cfg(self, key, default=None):
        return self.cfg.get(key, default)

    def _custom_prompt(self) -> str:
        return self.custom_prompt_entry.get().strip()

    def _ensure_voice_ready(self):
        try:
            if not (MODEL_PATH.exists() and CONFIG_PATH.exists()):
                self._log("[Система] Свалям българския глас (~60 MB), само първия път...")
                download_voice(VOICE_NAME, VOICES_DIR)
                self._log("[Система] Гласът е свален успешно.")

            self.voice = PiperVoice.load(str(MODEL_PATH), str(CONFIG_PATH))
            self._set_status("main", "● Готов", "ok")
        except Exception as e:
            self._log(f"[Грешка при зареждане на гласа] {e}")
            self._set_status("main", "● Грешка с гласа — виж лога", "err")

    # ------------------------------------------------------------------
    # Филтър
    # ------------------------------------------------------------------

    def _is_filtered(self, text: str) -> bool:
        if not self.filter_var.get():
            return False
        raw = self.filter_entry.get().strip()
        if not raw:
            return False
        banned = [w.strip().lower() for w in raw.split(",") if w.strip()]
        low = text.lower()
        return any(b in low for b in banned)

    def _is_spam(self, user_key: str, text: str) -> bool:
        """Анти-спам: твърде много коментари от 1 човек или твърде много
        еднакви съобщения (copy-paste flood) за кратко време."""
        if not self.spam_filter_var.get():
            return False

        now = time.time()
        normalized = re.sub(r"\s+", " ", text.strip().lower())

        # чистим старите записи извън прозореца
        while self.recent_comments and now - self.recent_comments[0][0] > SPAM_WINDOW_SECONDS:
            self.recent_comments.popleft()

        same_user_count = sum(1 for _, u, _ in self.recent_comments if u == user_key)
        same_text_count = sum(1 for _, _, t in self.recent_comments if t == normalized and normalized)

        self.recent_comments.append((now, user_key, normalized))

        if same_user_count >= SPAM_SAME_USER_MAX:
            return True
        if normalized and same_text_count >= SPAM_SAME_TEXT_MAX:
            return True
        return False

    def _is_eligible_heart_me_member(self, user_key: str, is_subscriber: bool) -> bool:
        """Проверява дали потребителят е абонат на канала И поне веднъж
        е пращал подаръка 'Heart Me'."""
        if not self.heart_me_filter_var.get():
            return True
        subscriber = bool(is_subscriber) or (user_key in self.confirmed_subscribers)
        return subscriber and (user_key in self.heart_me_senders)

    def _apply_max_chars(self, text: str):
        """Връща обработения текст, или None ако коментарът трябва да се
        пропусне изцяло (когато е избрано 'пропускай' и текстът е по-дълъг
        от лимита)."""
        raw = self.max_chars_entry.get().strip()
        try:
            max_chars = int(raw) if raw else 200
        except ValueError:
            max_chars = 200

        if max_chars <= 0 or len(text) <= max_chars:
            return text

        if self.skip_instead_of_truncate_var.get():
            self._log(f"   -> [пропуснато: {len(text)} символа > лимит {max_chars}]")
            return None

        # режем на границата на дума, ако е възможно, за да не се получи
        # накъсана дума по средата
        cut = text[:max_chars]
        last_space = cut.rfind(" ")
        if last_space > max_chars * 0.6:
            cut = cut[:last_space]
        return cut.strip()

    def _speech_allowed(self, source: str) -> bool:
        """Дали даден източник има право да ползва ЛОКАЛНИЯ глас (Piper/Edge).
        Live AI не минава оттук — то си пуска аудиото директно.
        source: 'tts' (коментари/обявявания) или 'ai' (текст от Gemini)."""
        mode = self._cfg("output_mode") or self.output_mode.get()
        if mode == "Само Live AI":
            return False  # нищо локално не говори — само гласът на Gemini
        if mode == "И двете":
            return True
        return True  # "TTS гласове"

    def _enqueue_latest_only(self, text: str, source: str = "tts"):
        # Режимът решава дали този източник изобщо има право да говори.
        if source != "preview" and not self._speech_allowed(source):
            # Обясняваме защо мълчи — иначе изглежда като бъг.
            now = time.time()
            if now - getattr(self, "_last_suppress_note", 0) > 20:
                self._last_suppress_note = now
                mode = self._cfg("output_mode") or self.output_mode.get()
                self._log(
                    f"[Тихо] Текстът не се изговаря, защото режимът е '{mode}'. "
                    "Смени на 'TTS гласове' или 'И двете', ако искаш да го чуваш."
                )
            return

        if self.muted:
            now = time.time()
            if now - getattr(self, "_last_mute_note", 0) > 20:
                self._last_mute_note = now
                self._log("[Тихо] Звукът е ЗАГЛУШЕН (бутонът 🔇 горе или клавиш f9).")
            return

        # Изхвърляме всичко чакащо в опашката (все още неизговорено) и слагаме
        # само най-новия коментар, за да не се трупа "изоставане" при много
        # коментари наведнъж. Коментарът, който в момента се изговаря, не се
        # прекъсва — само чакащите зад него отпадат.
        try:
            while True:
                self.speech_queue.get_nowait()
        except queue.Empty:
            pass
        self.speech_queue.put(text)

    def _process_incoming_comment(self, nickname: str, user_key: str, comment: str,
                                  is_subscriber: bool, is_moderator: bool = False):
        """Обща логика за входящ коментар — ползва се и от директната връзка,
        и от TikFinity връзката."""
        comment = clean_text_for_speech(comment or "")
        if not comment:
            return

        self.stat_comments += 1
        if is_moderator and user_key not in self.known_moderators:
            self.known_moderators.add(user_key)
            self._log(f"[Модератор] {nickname} е модератор в стрийма.")

        if self.strip_mentions_var.get():
            comment = strip_mentions(comment)
            if not comment:
                return

        if self.shlyokavitsa_var.get():
            converted = transliterate_shlyokavitsa(comment)
            if converted != comment:
                self._log(f"{self._role_mark(user_key, is_moderator, is_subscriber)}{nickname}: {comment}  ->  {converted}")
            else:
                self._log(f"{self._role_mark(user_key, is_moderator, is_subscriber)}{nickname}: {comment}")
            comment = converted
        else:
            self._log(f"{self._role_mark(user_key, is_moderator, is_subscriber)}{nickname}: {comment}")

        if self._is_filtered(comment):
            self._log("   -> [филтрирано по забранена дума]")
            return

        if self._is_spam(user_key, comment):
            self._log("   -> [филтрирано като спам]")
            return

        if not self._is_eligible_heart_me_member(user_key, is_subscriber):
            self._log("   -> [филтрирано: не е Heart Me донор + абонат]")
            return

        speech_text = self._apply_max_chars(comment)
        if speech_text is None:
            return
        self._enqueue_latest_only(speech_text)
        self._maybe_trigger_ai_commentary(nickname, comment)
        self._maybe_feed_live_comment(nickname, comment, is_moderator, is_subscriber)

    def _role_mark(self, user_key: str, is_moderator: bool, is_subscriber: bool) -> str:
        if is_moderator or user_key in self.known_moderators:
            return "🛡 "
        if is_subscriber or user_key in self.confirmed_subscribers:
            return "⭐ "
        return ""

    def _register_heart_me_gift(self, nickname: str, user_key: str, gift_name: str):
        if (gift_name or "").strip().lower() == HEART_ME_GIFT_NAME and user_key:
            if user_key not in self.heart_me_senders:
                self.heart_me_senders.add(user_key)
                self._log(f"[Система] {nickname} прати Heart Me — вече е допустим.")
                if self.live_running and self.live_feed_gift_var.get():
                    self._feed_live("gift", f"{nickname} — Heart Me")

    def _announce(self, text: str):
        """Пуска системно съобщение за изговаряне (нов последовател, споделяне и т.н.)."""
        self._log(f"[Обявяване] {text}")
        self._enqueue_latest_only(text)

    def _get_max_name_len(self) -> int:
        raw = self.max_name_len_entry.get().strip()
        try:
            return int(raw) if raw else 20
        except ValueError:
            return 20

    # ------------------------------------------------------------------
    # AI коментатор (Gemini)
    # ------------------------------------------------------------------

    def _on_follow_event(self, nickname: str):
        self.stat_follows += 1
        if self.live_running and self.live_feed_follow_var.get():
            self._feed_live("follow", nickname)
        if self.announce_follow_var.get() and is_reasonable_name(nickname, self._get_max_name_len()):
            self._announce(f"{nickname} последва канала!")

    def _on_share_event(self, nickname: str, user_key: str = ""):
        # Едно споделяне на човек за сесията — важи и за Live AI, и за TTS
        if user_key and user_key in self.announced_sharers:
            return
        if user_key:
            self.announced_sharers.add(user_key)
        self.stat_shares += 1

        if self.live_running and self.live_feed_share_var.get():
            self._feed_live("share", nickname)

        if not self.announce_share_var.get():
            return
        if not is_reasonable_name(nickname, self._get_max_name_len()):
            return
        self._announce(f"{nickname} сподели стрийма!")

    def _on_gift_shoutout(self, nickname: str, gift_name: str, count: int = 1):
        gn = (gift_name or "").strip()
        if gn.lower() == HEART_ME_GIFT_NAME:
            return  # Heart Me си има собствена логика, не го обявяваме отделно

        count = max(1, int(count or 1))
        self.stat_gifts += count
        label = f"{gn} x{count}" if count > 1 else gn

        if self.live_running and self.live_feed_gift_var.get() and gn:
            self._feed_live("gift", f"{nickname} — {label}")

        if (
            self.announce_gift_var.get()
            and gn
            and is_reasonable_name(nickname, self._get_max_name_len())
        ):
            if count > 1:
                self._announce(f"{nickname} прати {count} пъти {gn}!")
            else:
                self._announce(f"{nickname} прати подарък {gn}!")

    def _on_viewer_count_event(self, viewer_count):
        if viewer_count is None:
            return

        want_tts = self.announce_viewers_var.get()
        want_live = self.live_running and self.live_feed_viewers_var.get()
        if not want_tts and not want_live:
            return

        raw = self.viewer_interval_entry.get().strip()
        try:
            interval = int(raw) if raw else 300
        except ValueError:
            interval = 300

        now = time.time()
        if now - self.last_viewer_announcement_time < max(interval, 10):
            return
        self.last_viewer_announcement_time = now

        self.stat_viewers = int(viewer_count)
        if want_tts:
            self._announce(f"В момента гледат {viewer_count} души.")
        if want_live:
            self._feed_live("viewers", str(viewer_count))

    # ------------------------------------------------------------------
    # Изговорчик (worker thread)
    # ------------------------------------------------------------------

    def _maybe_trigger_ai_commentary(self, nickname: str, comment: str):
        if not self.ai_enabled_var.get():
            return

        # Отдръпване след удар в лимита — иначе всяка следваща заявка
        # също се отказва и само трупаме грешки.
        if time.time() < getattr(self, "ai_backoff_until", 0):
            return

        # Няма смисъл да хабим заявка за "фа", "ок", "😀" и подобни.
        try:
            min_len = max(0, int(self.ai_min_chars_entry.get().strip() or 6))
        except (ValueError, AttributeError):
            min_len = 6
        if len(comment.strip()) < min_len:
            return

        self.ai_comment_counter += 1

        if self.ai_frequency_menu.get() == "На всеки N-ти":
            raw = self.ai_every_n_entry.get().strip()
            try:
                n = max(1, int(raw)) if raw else 10
            except ValueError:
                n = 10
            if self.ai_comment_counter % n != 0:
                return

        self.ai_request_queue.put((nickname, comment))

    def _maybe_feed_live_comment(self, nickname: str, comment: str,
                                 is_moderator: bool = False, is_subscriber: bool = False):
        """Подава коментар на Live AI-то на всеки N-ти (собствен брояч,
        независим от текстовия AI коментатор в таб 'AI')."""
        if not self.live_running or not self.live_feed_comment_var.get():
            return

        self.live_comment_counter += 1
        raw = self.live_every_n_entry.get().strip()
        try:
            n = max(1, int(raw)) if raw else 5
        except ValueError:
            n = 5
        if self.live_comment_counter % n != 0:
            return

        role = ""
        if is_moderator or nickname in self.known_moderators:
            role = " (МОДЕРАТОР)"
        elif is_subscriber:
            role = " (абонат)"
        self._feed_live("comment", f'{nickname}{role}: "{comment}"')

    # ------------------------------------------------------------------
    # Live AI (Gemini Live API) — говор-към-говор
    # ------------------------------------------------------------------

    def _feed_live(self, kind: str, detail: str):
        """Слага събитие в буфера вместо да праща веднага.
        Така 20 последователи за 20 секунди стават ЕДНА заявка, не 20.
        kind: 'follow' | 'share' | 'gift' | 'comment'"""
        if not self.live_running:
            return
        now = time.time()
        with self.live_buffer_lock:
            if not self.live_event_buffer:
                self.live_buffer_started = now   # начало на текущата група
            self.live_last_event_ts = now
            self.live_event_buffer.append((kind, detail))
            # Ако AI-то говори дълго при наплив, буферът не бива да расте
            # безкрайно — пазим последните 40 събития.
            if len(self.live_event_buffer) > 40:
                over = len(self.live_event_buffer) - 40
                del self.live_event_buffer[:over]
                for _ in range(over):
                    self.live.note_event(dropped=True)

    def _live_batch_worker(self):
        """Праща натрупаните събития. Реагира БЪРЗО на единично събитие, а
        групира само когато наистина валят едно след друго."""
        COALESCE = 1.2      # изчакване след последното събитие, за да слеем близките
        TICK = 0.25

        while True:
            time.sleep(TICK)

            if not self.live_running:
                continue

            # Докато AI-то говори или има неизпратено — трупаме, не редим.
            if self.live_model_speaking or not self.live_text_queue.empty():
                continue

            with self.live_buffer_lock:
                if not self.live_event_buffer:
                    continue
                n = len(self.live_event_buffer)
                waited = time.time() - self.live_buffer_started
                quiet = time.time() - self.live_last_event_ts

                try:
                    max_wait = max(1.0, float(self._cfg("batch_seconds") or 8))
                except (ValueError, TypeError):
                    max_wait = 8.0

                # Пращаме, когато: настъпило е затишие (значи напливът свърши),
                # или сме чакали максималното, или са се събрали много събития.
                ready = quiet >= COALESCE or waited >= max_wait or n >= 12
                if not ready:
                    continue

                events = self.live_event_buffer[:]
                self.live_event_buffer.clear()

            message = self._compose_batch_message(events)
            if message:
                self.live_text_queue.put(message)
                self.live.note_event()
                delay = round(time.time() - self.live_buffer_started, 1)
                self._log(f"[Live AI ->] {len(events)} събития, изчакани {delay} сек.")

    def _compose_batch_message(self, events) -> str:
        """Съединява събитията в едно кратко, четимо резюме за AI-то."""
        follows = [d for k, d in events if k == "follow"]
        shares = [d for k, d in events if k == "share"]
        gifts = [d for k, d in events if k == "gift"]
        comments = [d for k, d in events if k == "comment"]
        viewers = [d for k, d in events if k == "viewers"]

        parts = []

        if follows:
            if len(follows) == 1:
                parts.append(f"Нов последовател: {follows[0]}.")
            else:
                names = ", ".join(follows[:6])
                extra = f" и още {len(follows) - 6}" if len(follows) > 6 else ""
                parts.append(f"Нови последователи: {names}{extra}.")

        if shares:
            if len(shares) == 1:
                parts.append(f"{shares[0]} сподели стрийма.")
            else:
                parts.append(f"Споделиха: {', '.join(shares[:5])}.")

        if gifts:
            if len(gifts) == 1:
                parts.append(f"Подарък: {gifts[0]}.")
            else:
                # групираме еднаквите подаръци: "Иван x12 Роза"
                counts = {}
                for g in gifts:
                    counts[g] = counts.get(g, 0) + 1
                summary = ", ".join(
                    f"{g} (x{c})" if c > 1 else g for g, c in list(counts.items())[:8]
                )
                parts.append(f"Подаръци: {summary}.")

        if comments:
            if len(comments) == 1:
                parts.append(f"Коментар в чата — {comments[0]}")
            else:
                joined = " | ".join(comments[:6])
                parts.append(f"{len(comments)} коментара в чата: {joined}")

        if viewers:
            # ползваме само последната стойност — старите вече не са актуални
            parts.append(f"В момента гледат {viewers[-1]} души.")

        if not parts:
            return ""

        return (
            " ".join(parts)
            + " Реагирай съвсем кратко — едно изречение. Ако има нови последователи, "
            "казвай имената им. Не изброявай статистики."
        )

    def _flush_transcripts(self):
        """Показва натрупаната транскрипция като един ред."""
        if self._tr_in.strip():
            self._log(f"[Чух те] {self._tr_in.strip()}")
        self._tr_in = ""
        if self._tr_out.strip():
            self._log(f"[AI казва] {self._tr_out.strip()}")
        self._tr_out = ""

    def _ai_worker(self):
        while True:
            nickname, comment = self.ai_request_queue.get()
            api_key = self._cfg("gemini_key", "")
            model = self._cfg("gemini_model", TEXT_MODELS[0])
            try:
                self._dbg("→", {"model": model, "user": nickname, "comment": comment})
                _t0 = time.time()
                reply = call_gemini(
                    api_key, model, nickname, comment,
                    streamer_name=self._cfg("streamer_name", ""),
                    mood=mood_line(
                        self._cfg("personality", "Балансиран"),
                        self._cfg("humor", 50),
                        self._cfg("custom_prompt", ""),
                    ),
                )
                self._dbg("←", reply)
                self.api.record(model, True, round((time.time() - _t0) * 1000))
                if reply:
                    self._log(f"[AI] {reply}")
                    if self._cfg("ai_speak", True):
                        self._enqueue_latest_only(reply, source="ai")
            except GeminiError as e:
                msg = str(e)
                self.api.record(model, False, reason=classify(msg))
                if "лимит" in msg or "429" in msg:
                    self.ai_backoff_until = time.time() + 60
                    self.api.set_backoff(60)
                    self._log(
                        "[AI] Достигнат лимит — спирам заявките за 60 секунди. "
                        "Ако се повтаря: смени модела на 'gemini-3.5-flash-lite' "
                        "(по-висок безплатен лимит) или увеличи 'N' за коментарите."
                    )
                else:
                    self._log(f"[AI грешка] {msg}")

    def _get_synthesis_config(self) -> SynthesisConfig:
        return SynthesisConfig(
            length_scale=float(self._cfg("speed", 0.85)),
            noise_scale=float(self._cfg("expressiveness", 0.9)),
            volume=float(self._cfg("volume", 1.3)),
        )

    def _get_selected_voice(self) -> str:
        """Връща ключ от VOICE_REGISTRY.
        Ако разбъркването е включено, избира произволно измежду включените
        в пула гласове при всяко извикване (т.е. за всеки нов коментар)."""
        if self._cfg("shuffle", False):
            pool = self._cfg("shuffle_pool") or []
            if pool:
                return random.choice(pool)
            # ако нищо не е отметнато в пула, падаме обратно на падащото меню

        label = self._cfg("voice_label") or self.voice_engine_menu.get()
        for key, info in VOICE_REGISTRY.items():
            if info["label"] == label:
                return key
        return "piper"

    def _edge_tts_params(self):
        """Превръща плъзгачите за скорост/сила в rate/volume параметри за Edge TTS."""
        length_scale = float(self._cfg("speed", 0.85))
        volume_mult = float(self._cfg("volume", 1.3))

        rate_pct = round((1.0 / max(length_scale, 0.1) - 1.0) * 100)
        rate_pct = max(-80, min(rate_pct, 100))

        vol_pct = round((volume_mult - 1.0) * 100)
        vol_pct = max(-95, min(vol_pct, 100))

        return f"{rate_pct:+d}%", f"{vol_pct:+d}%"

    def _preview_voice(self):
        # Ръчна проба — винаги се чува, независимо от режима
        self.speech_queue.put("Здравей, така ще звуча с тези настройки.")

    def _speaker_worker(self):
        while True:
            text = self.speech_queue.get()
            if self.muted:
                continue      # заглушено — не синтезираме изобщо
            selected = self._get_selected_voice()

            t_start = time.time()
            try:
                if selected == "piper":
                    if self.voice is None:
                        continue

                    syn_config = self._get_synthesis_config()
                    fd, tmp_path = tempfile.mkstemp(suffix=".wav")
                    os.close(fd)
                    with wave.open(tmp_path, "wb") as wav_file:
                        self.voice.synthesize_wav(text, wav_file, syn_config=syn_config)

                    effect = self._cfg("effect", "Няма")
                    if effect != "Няма":
                        apply_voice_effect(tmp_path, effect)

                    if self._cfg("debug", False):
                        self._log(f"  [DEBUG] синтез за {round((time.time()-t_start)*1000)} ms")

                    sound = pygame.mixer.Sound(tmp_path)
                    channel = sound.play()
                    while channel.get_busy():
                        if self.muted:
                            channel.stop()
                            break
                        time.sleep(0.05)
                    os.remove(tmp_path)

                else:
                    voice_name = VOICE_REGISTRY[selected]["edge_voice"]
                    rate, volume = self._edge_tts_params()

                    fd, tmp_path = tempfile.mkstemp(suffix=".mp3")
                    os.close(fd)

                    async def _synthesize():
                        communicate = edge_tts.Communicate(text, voice_name, rate=rate, volume=volume)
                        await communicate.save(tmp_path)

                    asyncio.run(_synthesize())
                    if self._cfg("debug", False):
                        self._log(f"  [DEBUG] Edge TTS за {round((time.time()-t_start)*1000)} ms")

                    pygame.mixer.music.load(tmp_path)
                    pygame.mixer.music.play()
                    while pygame.mixer.music.get_busy():
                        if self.muted:
                            pygame.mixer.music.stop()
                            break
                        time.sleep(0.05)
                    pygame.mixer.music.unload()
                    os.remove(tmp_path)

            except Exception as e:
                self._log(f"[Грешка при изговаряне] {e}")
                if selected != "piper":
                    self._log(
                        "[Съвет] Edge TTS (Borislav/Kalina) изисква интернет връзка. "
                        "Провери връзката си или превключи на Dimitar (офлайн)."
                    )

    # ------------------------------------------------------------------
    # TikTok Live връзка
    # ------------------------------------------------------------------

    def _piper_possibly_used(self) -> bool:
        if self.voice_shuffle_var.get():
            return self.shuffle_vars["piper"].get()
        return self.voice_engine_menu.get() == VOICE_REGISTRY["piper"]["label"]

    def start_listening(self):
        if self._piper_possibly_used() and self.voice is None:
            self._log("[Система] Гласът все още не е готов — изчакай малко и опитай пак.")
            return

        if self.connection_mode.get() == "TikFinity (Advanced)":
            self._start_tikfinity()
        else:
            self._start_direct()

    def _start_direct(self):
        username = self.username_entry.get().strip().lstrip("@")
        if not username:
            self._log("[Система] Въведи TikTok потребителско име.")
            return

        self.is_running = True
        pass
        pass
        self._set_status("main", f"◌ Свързване към @{username}...", "warn")

        api_key = self.api_key_entry.get().strip()
        WebDefaults.tiktok_sign_api_key = api_key if api_key else None

        self.tiktok_thread = threading.Thread(
            target=self._run_tiktok_client, args=(username,), daemon=True
        )
        self.tiktok_thread.start()

    def stop_listening(self):
        self.is_running = False
        if self.tiktok_loop and self.tiktok_client:
            try:
                self.tiktok_loop.call_soon_threadsafe(
                    functools.partial(asyncio.ensure_future, self.tiktok_client.disconnect())
                )
            except Exception:
                pass

        if self.tikfinity_loop and self.tikfinity_ws:
            try:
                self.tikfinity_loop.call_soon_threadsafe(
                    functools.partial(asyncio.ensure_future, self.tikfinity_ws.close())
                )
            except Exception:
                pass

        self._set_btn()
        self._set_status("main", "○ Спряно", "muted")

    # ------------------------------------------------------------------
    # TikFinity връзка (Advanced режим)
    # ------------------------------------------------------------------

    def _start_tikfinity(self):
        url = self.tikfinity_url_entry.get().strip()
        if not url:
            self._log("[Система] Въведи TikFinity WebSocket адрес.")
            return

        self.is_running = True
        pass
        pass
        self._set_status("main", "◌ Свързване към TikFinity...", "warn")

        self.tiktok_thread = threading.Thread(
            target=self._run_tikfinity_client, args=(url,), daemon=True
        )
        self.tiktok_thread.start()

    def _run_tikfinity_client(self, url: str):
        import websockets

        self.tikfinity_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.tikfinity_loop)

        async def listen():
            async with websockets.connect(url) as ws:
                self.tikfinity_ws = ws
                self._log("[Система] Свързан към TikFinity. Изчакваме коментари...")
                self._set_status("main", "● На живо (TikFinity)", "ok")

                async for raw_message in ws:
                    try:
                        msg = json.loads(raw_message)
                    except (json.JSONDecodeError, TypeError):
                        continue
                    self._handle_tikfinity_event(msg)

        try:
            self.tikfinity_loop.run_until_complete(listen())
        except Exception as e:
            self._log(f"[Грешка при връзка с TikFinity] {e}")
            self._log(
                "[Съвет] Провери дали TikFinity е пуснат и свързан към стрийма, "
                "и дали адресът съвпада (по подразбиране ws://localhost:21213/)."
            )
            traceback.print_exc()
        finally:
            self.tikfinity_ws = None
            self.is_running = False
            self._set_btn()
            self._set_btn()

    def _handle_tikfinity_event(self, msg: dict):
        event_name = msg.get("event")
        data = msg.get("data") or {}

        if event_name == "chat":
            nickname = data.get("nickname") or data.get("uniqueId") or "???"
            user_key = data.get("uniqueId") or str(data.get("userId") or "unknown")
            comment = data.get("comment") or ""
            is_subscriber = bool(data.get("isSubscriber", False))
            is_moderator = bool(data.get("isModerator", False))
            self._process_incoming_comment(
                nickname, user_key, comment, is_subscriber, is_moderator
            )

        elif event_name == "gift":
            # Същата логика за серии, но с имената на полетата от TikFinity
            if int(data.get("giftType") or 0) == 1 and not data.get("repeatEnd"):
                return  # серията още тече

            nickname = data.get("nickname") or data.get("uniqueId") or "???"
            user_key = data.get("uniqueId") or str(data.get("userId") or "")
            gift_name = data.get("giftName") or ""
            count = int(data.get("repeatCount") or 1)
            self._register_heart_me_gift(nickname, user_key, gift_name)
            self._on_gift_shoutout(nickname, gift_name, count)

        elif event_name == "follow":
            nickname = data.get("nickname") or data.get("uniqueId") or "???"
            self._on_follow_event(nickname)

        elif event_name == "share":
            nickname = data.get("nickname") or data.get("uniqueId") or "???"
            user_key = data.get("uniqueId") or str(data.get("userId") or "")
            self._on_share_event(nickname, user_key)

        elif event_name == "roomUser":
            viewer_count = data.get("viewerCount")
            self._on_viewer_count_event(viewer_count)

        elif event_name == "subscribe":
            user_key = data.get("uniqueId") or str(data.get("userId") or "")
            if user_key:
                # Директно потвърждение за абонамент — маркираме го отделно,
                # за по-сигурно засичане на "абонат" статус.
                self.confirmed_subscribers.add(user_key)

    def _run_tiktok_client(self, username: str):
        self.tiktok_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.tiktok_loop)

        client = TikTokLiveClient(unique_id=username)
        self.tiktok_client = client

        @client.on(ConnectEvent)
        async def on_connect(_event: ConnectEvent):
            self._log(f"[Система] Свързан към @{username}. Изчакваме коментари...")
            self._set_status("main", f"● На живо: @{username}", "ok")

        @client.on(CommentEvent)
        async def on_comment(event: CommentEvent):
            nickname = event.user.nickname if event.user else "???"
            user_key = (
                (getattr(event.user, "unique_id", None) or str(getattr(event.user, "user_id", "")))
                if event.user else "unknown"
            )
            is_subscriber = False
            is_moderator = False
            if event.user:
                try:
                    is_subscriber = event.user.has_badge("SUBSCRIBER")
                except Exception:
                    pass
                try:
                    is_moderator = bool(getattr(event.user, "is_moderator", False)) or \
                        event.user.has_badge("MODERATOR")
                except Exception:
                    pass

            self._process_incoming_comment(
                nickname, user_key, event.comment or "", is_subscriber, is_moderator
            )

        @client.on(GiftEvent)
        async def on_gift(event: GiftEvent):
            if not event.user or not event.gift:
                return

            # TikTok праща МНОГО междинни събития, докато трае серия (streak) —
            # за един и същ подарък. Броим само финалното, иначе един подарък
            # излиза изпратен по няколко пъти.
            streakable = bool(getattr(event.gift, "streakable", False))
            repeat_end = int(getattr(event, "repeat_end", 0) or 0)
            if streakable and repeat_end != 1:
                return  # серията още тече — чакаме края

            count = int(getattr(event, "repeat_count", 0) or 1)
            user_key = getattr(event.user, "unique_id", None) or str(
                getattr(event.user, "user_id", "")
            )
            gift_name = event.gift.name or ""
            self._register_heart_me_gift(event.user.nickname, user_key, gift_name)
            self._on_gift_shoutout(event.user.nickname, gift_name, count)

        @client.on(FollowEvent)
        async def on_follow(event: FollowEvent):
            nickname = event.user.nickname if event.user else "???"
            self._on_follow_event(nickname)

        @client.on(ShareEvent)
        async def on_share(event: ShareEvent):
            nickname = event.user.nickname if event.user else "???"
            user_key = (
                getattr(event.user, "unique_id", None) or str(getattr(event.user, "user_id", ""))
                if event.user else "unknown"
            )
            self._on_share_event(nickname, user_key)

        @client.on(RoomUserSeqEvent)
        async def on_room_user_seq(event: RoomUserSeqEvent):
            viewer_count = getattr(event, "total", None) or getattr(event, "total_user", None)
            self._on_viewer_count_event(viewer_count)

        @client.on(DisconnectEvent)
        async def on_disconnect(_event: DisconnectEvent):
            self._log("[Система] Връзката е прекъсната.")

        @client.on(LiveEndEvent)
        async def on_live_end(_event: LiveEndEvent):
            self._log("[Система] Стриймът приключи.")

        try:
            self.tiktok_loop.run_until_complete(client.connect(fetch_live_check=True))
        except Exception as e:
            self._log(f"[Грешка при връзка] {e}")
            self._log(
                "[Съвет] Тази грешка (HTTP 400 / rejected websocket) обикновено значи, "
                "че безплатният общ лимит на сървъра за подпис (Euler Stream) е зает. "
                "Провери дали потребителят е наистина на живо и, ако продължава, "
                "вземи безплатен API ключ от https://www.eulerstream.com и го сложи "
                "в полето 'Euler Stream API ключ' по-горе."
            )
            traceback.print_exc()
        finally:
            self.is_running = False
            self._set_btn()
            self._set_btn()

    def start_live_ai(self):
        api_key = self.gemini_api_key_entry.get().strip()
        if not api_key:
            self._log("[Live AI] Липсва Gemini API ключ — попълни го в таб 'AI'.")
            return

        bad = [c for c in api_key if not (32 < ord(c) < 127)]
        if bad:
            self._log(
                f"[Live AI] СПРЯНО: ключът съдържа непозволени знаци {bad[:5]} "
                "(кирилица, интервал или нов ред). Изтрий полето изцяло и постави "
                "ключа наново — с десен бутон → Постави, или бутона 'Постави'."
            )
            return
        if self.live_running:
            return

        self.live_running = True
        self.live.set_state(LM.CONNECTING, "отваря сесия")
        self.live.note_session()
        self.live_session_id += 1          # всяка нова сесия обезсилва старите
        session_id = self.live_session_id
        pass
        pass
        self._set_status("live", "◌ Live AI свързване...", "warn")

        threading.Thread(
            target=self._run_live_client, args=(api_key, session_id), daemon=True
        ).start()

    def stop_live_ai(self):
        self.live_running = False
        self.live.set_state(LM.OFF, "спрян ръчно")
        self.live_session_id += 1        # обезсилва всички текущи нишки
        self.live_resume_handle = None   # ръчно спиране = нова сесия следващия път
        self.live_setup_level = 0
        self._stop_mic()
        if self.live_loop and self.live_ws:
            try:
                self.live_loop.call_soon_threadsafe(
                    functools.partial(asyncio.ensure_future, self.live_ws.close())
                )
            except Exception:
                pass
        self._set_btn()
        self._set_status("live", "○ Live AI изключен", "muted")

    def _build_setup(self, model: str, voice: str, level: int) -> dict:
        """Сглобява setup-а за дадено ниво. По-високо ниво = по-малко полета."""
        streamer = self.streamer_name_entry.get().strip()

        speech_config = {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voice}}}
        # Езиковият код помага Gemini да не бърка българския с полски, но
        # native-audio моделите изрично НЕ го поддържат и отхвърлят връзката.
        native_audio = "native-audio" in model
        if level < 1 and not native_audio:
            speech_config["languageCode"] = "bg-BG"

        setup = {
            "model": f"models/{model}",
            "generationConfig": {
                "responseModalities": ["AUDIO"],
                "speechConfig": speech_config,
            },
            "systemInstruction": {
                "parts": [{"text": (
                    LIVE_SYSTEM_PROMPT
                    + mood_line(
                        self.personality_menu.get(),
                        int(self.humor_slider.get()),
                        self._custom_prompt(),
                    )
                    + streamer_line(streamer)
                )}]
            },
        }

        if level < 4:
            setup["inputAudioTranscription"] = {}
            setup["outputAudioTranscription"] = {}

        if level < 2:
            setup["realtimeInputConfig"] = {
                "automaticActivityDetection": {"disabled": False, "silenceDurationMs": 800}
            }

        if level < 3:
            setup["contextWindowCompression"] = {
                "slidingWindow": {},
                "triggerTokens": "25600",
            }
            setup["sessionResumption"] = (
                {"handle": self.live_resume_handle} if self.live_resume_handle else {}
            )

        return setup

    def _run_live_client(self, api_key: str, session_id: int = 0):
        import base64
        import websockets

        self.live_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.live_loop)

        model = self.live_model_entry.get().strip() or LIVE_MODELS[0]
        voice = self.live_voice_menu.get()
        streamer = self.streamer_name_entry.get().strip()
        url = LIVE_WS_URL.format(key=api_key)

        async def sender(ws):
            """Праща микрофонно аудио и текстови събития към AI-то."""
            while self.live_running and session_id == self.live_session_id:
                sent_something = False

                # микрофон
                try:
                    while True:
                        chunk = self.live_mic_queue.get_nowait()
                        await ws.send(json.dumps({
                            "realtimeInput": {
                                "audio": {
                                    "data": base64.b64encode(chunk).decode("ascii"),
                                    "mimeType": f"audio/pcm;rate={LIVE_INPUT_RATE}",
                                }
                            }
                        }))
                        sent_something = True
                except queue.Empty:
                    pass

                # Сигнал "потокът свърши" — иначе сървърът чака още звук
                # вечно и не отговаря, когато спреш микрофона.
                if self.live_send_stream_end:
                    self.live_send_stream_end = False
                    await ws.send(json.dumps({"realtimeInput": {"audioStreamEnd": True}}))
                    self._log("[Live AI] Край на говора — чакам отговор.")
                    sent_something = True

                # Текстови събития — пращаме ги САМО когато никой не говори.
                # Ако ги пуснем по средата на ход, Live API се обърква и
                # спира да отговаря.
                # Предпазител: ако сигналът за завършен ход не дойде, флагът
                # "AI-то говори" би заял завинаги и текстът никога не тръгва.
                # Затова смятаме хода за приключил и след 2 сек тишина.
                if self.live_model_speaking and (time.time() - self.live_last_audio_ts) > 2.0:
                    self.live_model_speaking = False
                    self.live.set_state(LM.READY, "тишина — ходът приключи")
                    self._flush_transcripts()

                user_speaking = (time.time() - self.live_last_voice_ts) < 1.5
                busy = self.live_model_speaking or user_speaking

                try:
                    while not busy:
                        text = self.live_text_queue.get_nowait()
                        self._dbg("→", {"clientContent": text})
                        await ws.send(json.dumps({
                            "clientContent": {
                                "turns": [{"role": "user", "parts": [{"text": text}]}],
                                "turnComplete": True,
                            }
                        }))
                        self._log(f"[Live AI ->] {text}")
                        sent_something = True
                except queue.Empty:
                    pass

                await asyncio.sleep(0.01 if sent_something else 0.05)

        async def receiver(ws, out_stream, out_rate=LIVE_OUTPUT_RATE):
            """Получава аудио от AI-то и го пуска през говорителите."""
            async for raw in ws:
                if not self.live_running or session_id != self.live_session_id:
                    break
                msg = _parse_ws(raw)
                if not msg:
                    continue

                self._dbg("←", msg)

                # Сървърът периодично праща талон, с който можем да продължим
                # същата сесия след прекъсване — пазим последния.
                resume = msg.get("sessionResumptionUpdate") or {}
                if resume.get("resumable") and resume.get("newHandle"):
                    self.live_resume_handle = resume["newHandle"]

                # Предупреждение ~60 сек преди сървърът да пресече връзката
                go_away = msg.get("goAway")
                if go_away is not None:
                    left = go_away.get("timeLeft", "скоро")
                    self._log(
                        f"[Live AI] Сървърът ще пресече връзката ({left}). "
                        "Пресвързвам се и продължавам същата сесия..."
                    )
                    break   # излизаме, за да сработи автоматичното пресвързване

                server_content = msg.get("serverContent") or {}

                if server_content.get("interrupted"):
                    self.live_model_speaking = False
                    self.live.note_interrupt()
                    self.live.set_state(LM.READY, "прекъснат")
                    self._flush_transcripts()
                    self._log("[Live AI] Прекъснат (заговорил си докато AI-то говори).")
                if server_content.get("turnComplete"):
                    self._flush_fallback()
                    self.live_model_speaking = False
                    self.live.note_turn()
                    self.live.set_state(LM.READY, "чака")
                    self._flush_transcripts()

                # Транскрипцията идва на малки парчета (дума по дума).
                # Трупаме ги и ги показваме като едно цяло изречение.
                in_tr = (server_content.get("inputTranscription") or {}).get("text")
                if in_tr:
                    self._tr_in += in_tr
                out_tr = (server_content.get("outputTranscription") or {}).get("text")
                if out_tr:
                    self._tr_out += out_tr

                model_turn = server_content.get("modelTurn") or {}
                for part in model_turn.get("parts", []):
                    inline = part.get("inlineData") or {}
                    data_b64 = inline.get("data")
                    if data_b64:
                        if not self.live_model_speaking:
                            self.live.set_state(LM.SPEAKING)
                        self.live_model_speaking = True
                        self.live_last_audio_ts = time.time()
                        self.live.note_audio_out(len(data_b64))
                    if (data_b64 and not self.muted
                            and (out_stream is not None or getattr(self, "audio_fallback", False))
                            and session_id == self.live_session_id):
                        try:
                            pcm = base64.b64decode(data_b64)
                            gain = float(self._cfg("live_volume", 1.0))
                            need_resample = out_rate != LIVE_OUTPUT_RATE

                            if abs(gain - 1.0) > 0.01 or need_resample:
                                samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
                                if abs(gain - 1.0) > 0.01:
                                    samples = samples * gain
                                if need_resample:
                                    ratio = out_rate / LIVE_OUTPUT_RATE
                                    n_out = int(len(samples) * ratio)
                                    if n_out > 0:
                                        samples = np.interp(
                                            np.linspace(0, len(samples) - 1, n_out),
                                            np.arange(len(samples)),
                                            samples,
                                        )
                                samples = np.clip(samples, -32768, 32767)
                                pcm = samples.astype(np.int16).tobytes()
                            if out_stream is not None:
                                out_stream.write(pcm)
                            elif getattr(self, "audio_fallback", False):
                                self._play_pcm_fallback(pcm, out_rate)
                            self.audio_written_bytes = getattr(self, "audio_written_bytes", 0) + len(pcm)
                        except Exception:
                            pass
                    text = part.get("text")
                    if text:
                        self._log(f"[Live AI] {text}")

        async def run(level):
            out_stream = None
            out_rate = LIVE_OUTPUT_RATE
            try:
                import sounddevice as sd
                dev = self._get_selected_output_device()

                # Не всяка карта поддържа 24000 Hz (грешка -9997). Пробваме
                # първо родната честота, после честотата на устройството.
                candidates = [LIVE_OUTPUT_RATE]
                try:
                    info = sd.query_devices(dev, "output")
                    native = int(info["default_samplerate"])
                    if native not in candidates:
                        candidates.append(native)
                except Exception:
                    pass
                candidates += [48000, 44100]

                last_err = None
                for rate in candidates:
                    try:
                        out_stream = sd.RawOutputStream(
                            samplerate=rate, dtype="int16", channels=1,
                            # По-голям блок = по-устойчиво срещу накъсване.
                            # 4800 семпъла ≈ 200 ms при 24 kHz.
                            blocksize=4800, device=dev,
                        )
                        out_stream.start()
                        out_rate = rate
                        break
                    except Exception as e:
                        last_err = e
                        out_stream = None

                if out_stream is None:
                    raise last_err or RuntimeError("няма подходяща честота")

                self.audio_out_open = True
                try:
                    import sounddevice as _sd
                    _name = _sd.query_devices(dev, "output")["name"][:40]
                except Exception:
                    _name = "(по подразбиране)"
                self.audio_out_info = f"{_name} @ {out_rate} Hz"
                if out_rate != LIVE_OUTPUT_RATE:
                    self._log(
                        f"[Live AI] Аудио изходът работи на {out_rate} Hz "
                        f"(картата не приема {LIVE_OUTPUT_RATE} Hz) — преобразувам. "
                        f"Устройство: {_name}"
                    )
                else:
                    self._log(f"[Live AI] Аудио изходът е отворен: {_name} @ {out_rate} Hz")
            except Exception as e:
                # Резервен път: ако sounddevice не работи (липсва PortAudio,
                # заето устройство и т.н.), пускаме звука през pygame, който
                # и без това работи за локалните гласове.
                out_stream = None
                self.audio_out_open = False
                self.audio_out_info = f"sounddevice отказа: {str(e)[:50]}"
                self._log(f"[Live AI] Основният аудио изход отказа ({e}).")
                try:
                    if pygame.mixer.get_init() is None:
                        pygame.mixer.init()
                    self.audio_fallback = True
                    self._init_fallback_audio(LIVE_OUTPUT_RATE)
                    self.audio_out_open = True
                    self.audio_out_info = "резервен изход (pygame)"
                    self._log("[Live AI] Ползвам резервен изход през pygame — звукът ще се чува.")
                except Exception as e2:
                    self.audio_fallback = False
                    self._log(f"[Live AI] И резервният изход отказа ({e2}) — само текст.")

            async with websockets.connect(url, max_size=None) as ws:
                self.live_ws = ws
                session_started = time.time()
                session_started = time.time()
                if self.live_resume_handle:
                    self._log("[Live AI] WebSocket отворен. Продължавам предишната сесия...")
                else:
                    self._log(f"[Live AI] WebSocket отворен. Нова сесия с модел '{model}'...")

                setup_payload = self._build_setup(model, voice, level)
                self._dbg("→", {"setup": setup_payload})
                await ws.send(json.dumps({"setup": setup_payload}))

                first = await asyncio.wait_for(ws.recv(), timeout=20)
                self._dbg("←", first)
                first_msg = _parse_ws(first) or {"raw": str(first)[:300]}

                if "setupComplete" not in first_msg:
                    self._log(f"[Live AI] Сървърът отхвърли setup-а: {first_msg}")
                    raise SetupRejected(str(first_msg)[:200])

                if level > 0:
                    self._log(f"[Live AI] Setup приет на ниво {level} ({SETUP_LEVELS[level]}).")
                else:
                    self._log("[Live AI] Setup потвърден от сървъра.")

                self.live_setup_level = level   # запомняме кое ниво работи
                self.live.note_setup(level)
                self.live.set_state(LM.READY, f"ниво '{SETUP_LEVELS[level]}'")
                self.api.record(f"live:{model}", True)
                self._log("[Live AI] Свързан и готов. Пробвай да кажеш нещо или пусни тест.")
                self._set_status("live", "● Live AI активен", "ok")

                await asyncio.gather(sender(ws), receiver(ws, out_stream, out_rate))

                # Точната причина за затваряне — това е ключът към диагнозата
                try:
                    code = getattr(ws, "close_code", None)
                    reason = getattr(ws, "close_reason", None)
                    if code is not None:
                        self._log(f"[Live AI] Сървърът затвори връзката: код {code}, причина: {reason or '(няма)'}")
                except Exception:
                    pass

            if out_stream is not None:
                try:
                    out_stream.stop()
                    out_stream.close()
                except Exception:
                    pass

        try:
            # Пробваме от запомненото ниво надолу, докато сървърът приеме setup-а.
            start_level = getattr(self, "live_setup_level", 0)
            last_error = None
            for level in range(start_level, len(SETUP_LEVELS)):
                try:
                    self.live_loop.run_until_complete(run(level))
                    last_error = None
                    break
                except SetupRejected as e:
                    last_error = e
                    if level + 1 < len(SETUP_LEVELS):
                        self._log(
                            f"[Live AI] Ниво '{SETUP_LEVELS[level]}' не се приема — "
                            f"пробвам '{SETUP_LEVELS[level + 1]}'..."
                        )
                    continue
                except Exception as e:
                    # Сървърът къса връзката с код 1007/1008, когато не приема
                    # някое поле. Това също е отхвърлен setup — продължаваме
                    # надолу по нивата, вместо да се предаваме.
                    detail = str(e)
                    looks_like_setup = any(
                        s in detail for s in ("1007", "1008", "Unsupported", "Invalid", "invalid")
                    )
                    if not looks_like_setup:
                        raise
                    last_error = e
                    reason = detail.split(";")[0][:120]

                    # Лош ключ = окончателно. Няма смисъл да пробваме нива.
                    self.api.record(f"live:{model}", False, reason=classify(detail))
                    self.live.note_setup(level, rejected=True)
                    self.live.note_error(detail)
                    if "API key not valid" in detail or "API_KEY_INVALID" in detail:
                        self.live_fatal = True
                        self._log("[Live AI] Google отхвърли ключа като невалиден.")
                        break

                    self._log(f"[Live AI] Ниво '{SETUP_LEVELS[level]}' отказано: {reason}")
                    if level + 1 < len(SETUP_LEVELS):
                        self._log(f"[Live AI] Пробвам '{SETUP_LEVELS[level + 1]}'...")
                    continue
            if last_error is not None:
                self._log(
                    "[Live AI] Никоя комбинация не се приема. Най-вероятно моделът е "
                    "недостъпен за твоя ключ — пробвай друг от падащото меню, или "
                    "пусни '🔌 Тест връзка с Gemini' в таб 'Тест'."
                )
        except asyncio.TimeoutError:
            self._log("[Live AI грешка] Сървърът не отговори на setup за 20 секунди.")
        except Exception as e:
            detail = str(e)
            self._log(f"[Live AI грешка] {type(e).__name__}: {detail}")
            if "1007" in detail or "1008" in detail or "policy" in detail.lower():
                self._log(
                    "[Съвет] Обикновено значи невалиден/спрян модел или проблем с ключа. "
                    f"Пробвай друг модел от списъка (сега е '{model}')."
                )
            elif "401" in detail or "403" in detail or "API key" in detail:
                self._log("[Съвет] Ключът изглежда невалиден. Провери го в таб 'AI'.")
            elif "429" in detail or "RESOURCE_EXHAUSTED" in detail.upper():
                self._log(
                    "[Съвет] Достигнат е лимитът на безплатното ниво (429). Изчакай "
                    "няколко минути. Ако се повтаря често: увеличи 'Групирай на всеки' "
                    "и 'Коментари на всеки N-ти', за да правиш по-малко заявки."
                )
            elif "1011" in detail or "internal" in detail.lower():
                self._log(
                    "[Съвет] Вътрешна грешка от сървъра на Google — обикновено минава "
                    "от само себе си. Автоматичното пресвързване ще опита пак."
                )
            else:
                self._log(
                    "[Съвет] Провери интернет връзката и дали ключът е активен. "
                    "Ползвай 'Тест връзка с Gemini' в таб 'Тест' за проверка."
                )
            traceback.print_exc()
        finally:
            self._flush_transcripts()
            self.live_ws = None
            self._stop_mic()

            try:
                lasted = round(time.time() - session_started)
                self._log(f"[Live AI] Сесията издържа {lasted} сек. ({round(lasted/60, 1)} мин.)")
            except Exception:
                pass

            # Автоматично пресвързване, ако връзката е паднала сама
            # Невалиден ключ няма да се оправи от само себе си — спираме,
            # вместо да се въртим безкрайно и да пълним лога.
            if getattr(self, "live_fatal", False):
                self.live_running = False
                self._log(
                    "[Live AI] СПРЯНО. Ключът е отхвърлен от Google. Провери го в "
                    "таб 'AI' (изтрий полето и постави наново) и натисни 'Свържи' пак."
                )
                self._set_btn()
                self._set_btn()
                self._set_status("live", "○ Live AI спрян (лош ключ)", "err")
                self.live_fatal = False
                self.live.set_state(LM.FAILED, "ключът е отхвърлен")
                return

            # Ако междувременно е стартирана нова сесия (или е натиснат Стоп),
            # тази нишка е остаряла — приключва тихо, без да се пресвързва.
            # Иначе изостанали нишки оживяват и AI-то проговаря само.
            if session_id != self.live_session_id:
                self._log("[Live AI] Стара сесия приключи (заменена е с по-нова).")
                return

            if self.live_running and self.live_autoreconnect_var.get():
                if self.live_resume_handle:
                    self._log("[Live AI] Пресвързвам се и продължавам сесията...")
                else:
                    self._log("[Live AI] Връзката падна — пресвързвам се...")
                self.live.set_state(LM.RECONNECTING)
                self.live.note_reconnect()
                self._set_status("live", "◌ Live AI пресвързване...", "warn")
                time.sleep(1.5)
                if self.live_running and session_id == self.live_session_id:
                    threading.Thread(
                        target=self._run_live_client, args=(api_key, session_id), daemon=True
                    ).start()
                    return

            self.live_running = False
            self._set_btn()
            self._set_btn()
            self._set_status("live", "○ Live AI прекъснат", "gray")

    def _on_mic_toggle(self):
        if self.live_mic_var.get():
            self._start_mic()
        else:
            self._stop_mic()

    def _start_mic(self):
        if self.live_mic_stream is not None:
            return
        try:
            import sounddevice as sd
        except Exception as e:
            self._log(f"[Live AI] Микрофонът не е достъпен: {e}")
            self.live_mic_var.set(False)
            return

        def callback(indata, frames, time_info, status):
            # ВАЖНО: тук НЕ четем tkinter променливи — този callback се изпълнява
            # в аудио нишката на PortAudio и tkinter не е thread-safe.
            if not (self.live_running and self.mic_active):
                return

            chunk = bytes(indata)

            # Засичаме дали наистина говориш (за координация на ходовете)
            try:
                arr = np.frombuffer(chunk, dtype=np.int16)
                if arr.size and float(np.abs(arr).mean()) > 400:
                    self.live_last_voice_ts = time.time()
            except Exception:
                pass

            # Полудуплекс: докато AI-то говори, НЕ пращаме звук от микрофона.
            # Иначе гласът му излиза от колонките, влиза обратно и моделът
            # решава, че ти го прекъсваш — обърква се напълно.
            if self.half_duplex and self.live_model_speaking:
                return

            self.live_mic_queue.put(chunk)
            self.mic_chunks_sent += 1
            self.live.note_audio_in()

        try:
            self.live_mic_stream = sd.RawInputStream(
                samplerate=LIVE_INPUT_RATE, blocksize=1600, dtype="int16",
                channels=1, callback=callback, device=self._get_selected_mic_device(),
            )
            self.mic_active = True
            self.mic_chunks_sent = 0
            self.live_mic_stream.start()
            self._log("[Live AI] Микрофонът е включен — говори.")
        except Exception as e:
            self._log(f"[Live AI] Грешка при пускане на микрофона: {e}")
            self.live_mic_stream = None
            self.live_mic_var.set(False)

    def _stop_mic(self):
        was_active = self.mic_active
        self.mic_active = False
        if was_active and self.live_running:
            self.live_send_stream_end = True
        if self.live_mic_stream is not None:
            try:
                self.live_mic_stream.stop()
                self.live_mic_stream.close()
            except Exception:
                pass
            self.live_mic_stream = None
            secs = round(self.mic_chunks_sent * 0.1, 1)
            if self.mic_chunks_sent == 0:
                self._log(
                    "[Live AI] Микрофонът е изключен — но НЕ е уловил нищо. "
                    "Пусни '🎙 Тест микрофон' в таб 'Тест' и провери устройството."
                )
            else:
                self._log(f"[Live AI] Микрофонът е изключен (изпратени ~{secs} сек. звук).")

    def _get_selected_mic_device(self):
        label = self.mic_device_menu.get()
        if label == "(по подразбиране)":
            return None
        return getattr(self, "mic_device_map", {}).get(label)

    def _get_selected_output_device(self):
        label = self.live_output_menu.get()
        if label == "(по подразбиране)":
            return None
        return getattr(self, "output_device_map", {}).get(label)

    def _refresh_mic_devices(self):
        try:
            import sounddevice as sd
            devices = sd.query_devices()
            hostapis = sd.query_hostapis()
        except Exception as e:
            self._log(f"[Микрофон] Не мога да прочета устройствата: {e}")
            return

        # Windows показва всяко устройство по няколко пъти — веднъж за всеки
        # звуков интерфейс (MME, DirectSound, WASAPI). Оставяме по един запис
        # на физически микрофон, с предпочитание към по-модерния интерфейс.
        priority = {"Windows WASAPI": 0, "Windows DirectSound": 1, "MME": 2}
        best = {}
        for idx, dev in enumerate(devices):
            if dev.get("max_input_channels", 0) <= 0:
                continue
            name = dev["name"].strip()
            try:
                api_name = hostapis[dev["hostapi"]]["name"]
            except Exception:
                api_name = ""
            rank = priority.get(api_name, 3)
            if name not in best or rank < best[name][0]:
                best[name] = (rank, idx, api_name)

        names = ["(по подразбиране)"]
        self.mic_device_map = {}
        for name, (_rank, idx, api_name) in sorted(best.items(), key=lambda x: x[1][1]):
            label = name if len(name) <= 45 else name[:45] + "…"
            if label in self.mic_device_map:      # съвсем еднакви имена
                label = f"{label} ({idx})"
            names.append(label)
            self.mic_device_map[label] = idx

        current = self.mic_device_menu.get()
        self.mic_device_list = names
        if current not in names:
            self.mic_device_menu.set("(по подразбиране)")
        self._log(f"[Микрофон] {len(names) - 1} микрофона (дубликатите са премахнати).")

    def _refresh_output_devices(self):
        try:
            import sounddevice as sd
            devices = sd.query_devices()
            hostapis = sd.query_hostapis()
        except Exception as e:
            self._log(f"[Изход] Не мога да прочета устройствата: {e}")
            return

        priority = {"Windows WASAPI": 0, "Windows DirectSound": 1, "MME": 2}
        best = {}
        for idx, dev in enumerate(devices):
            if dev.get("max_output_channels", 0) <= 0:
                continue
            name = dev["name"].strip()
            try:
                api_name = hostapis[dev["hostapi"]]["name"]
            except Exception:
                api_name = ""
            rank = priority.get(api_name, 3)
            if name not in best or rank < best[name][0]:
                best[name] = (rank, idx)

        names = ["(по подразбиране)"]
        self.output_device_map = {}
        for name, (_r, idx) in sorted(best.items(), key=lambda x: x[1][1]):
            label = name if len(name) <= 45 else name[:45] + "…"
            names.append(label)
            self.output_device_map[label] = idx

        current = self.live_output_menu.get()
        self.live_output_menu.configure(values=names)
        if current not in names:
            self.live_output_menu.set("(по подразбиране)")
        self._log(f"[Изход] {len(names) - 1} изходни устройства.")

    def toggle_mute(self):
        self.set_muted(not self.muted)

    def set_muted(self, value: bool):
        self.muted = bool(value)

        if self.muted:
            # Спираме моментално това, което се говори В МОМЕНТА — това е
            # смисълът на аварийния бутон.
            try:
                pygame.mixer.stop()
                pygame.mixer.music.stop()
            except Exception:
                pass
            # и изчистваме всичко чакащо
            try:
                while True:
                    self.speech_queue.get_nowait()
            except queue.Empty:
                pass
            self._log("[ЗАГЛУШЕНО] Звукът е спрян. Връзките с TikTok и Live AI остават активни.")
        else:
            self._log("[Звук] Пуснат отново.")

        self._ui(self._refresh_mute_ui)

    def _reset_stats(self):
        self.stat_follows = self.stat_shares = self.stat_gifts = 0
        self.stat_comments = self.stat_viewers = 0
        self._log("[Статистика] Нулирана.")



    # ------------------------------------------------------------------
    def _log_voice_summary(self):
        """Показва текущите гласови настройки — за да не се чудиш защо
        звучи различно, ако нещо е останало от предишен път."""
        speed = float(self.speed_slider.get())
        vol = float(self.volume_slider.get())
        effect = self.voice_effect_menu.get()
        shuffle = self.voice_shuffle_var.get()
        voice = self.voice_engine_menu.get().split(" (")[0]

        if shuffle:
            pool = [VOICE_REGISTRY[k]["short"] for k, v in self.shuffle_vars.items() if v.get()]
            voice_txt = f"разбъркване между {', '.join(pool) if pool else '(нищо избрано!)'}"
        else:
            voice_txt = f"{voice} (разбъркването е изключено)"

        self._log(
            f"[Глас] {voice_txt} | скорост {speed:.2f} | сила {vol:.2f}x | "
            f"ефект: {effect} | режим: {self.output_mode.get()}"
        )
        if speed > 1.05:
            self._log(f"[Внимание] Скоростта е {speed:.2f} — звучи БАВНО (0.85 е нормалното).")
        if effect != "Няма":
            self._log(f"[Внимание] Включен е ефект '{effect}'.")
        if vol < 0.3:
            self._log(f"[Внимание] Силата на гласа е много ниска ({vol:.2f}x).")

    def _reset_voice_settings(self):
        self.speed_slider.set(0.85)
        self.expressiveness_slider.set(0.9)
        self.volume_slider.set(1.3)
        self.live_volume_slider.set(1.0)
        self.voice_effect_menu.set("Няма")
        self._refresh_cfg()
        self._log("[Глас] Върнати по подразбиране.")
        self._log_voice_summary()

    def _toggle_hotkey(self):
        if getattr(self, "hotkey_active", False):
            try:
                import keyboard
                keyboard.unhook_all()
            except Exception:
                pass
            self.hotkey_active = False
            self._log("[Клавиш] Изключен.")
            return

        combo = self.hotkey_entry.get().strip()
        if not combo:
            self._log("[Клавиш] Въведи клавиш (напр. f8).")
            return
        try:
            import keyboard
        except Exception as e:
            self._log(f"[Клавиш] Не е достъпно: {e}")
            return

        hold = self.hotkey_mode_menu.get() == "Задръж за говорене"
        try:
            if hold:
                keyboard.on_press_key(combo, lambda _e: self._hotkey_set_mic(True))
                keyboard.on_release_key(combo, lambda _e: self._hotkey_set_mic(False))
            else:
                keyboard.add_hotkey(combo, self._hotkey_toggle_mic)
            mute_combo = self.mute_hotkey_entry.get().strip()
            if mute_combo:
                keyboard.add_hotkey(mute_combo, self.toggle_mute)
                self._log(f"[Клавиш] '{mute_combo}' заглушава.")
        except Exception as e:
            self._log(f"[Клавиш] Не мога да регистрирам '{combo}': {e}")
            return

        self.hotkey_active = True
        self._log(f"[Клавиш] '{combo}' е активен за микрофона.")

    def _hotkey_set_mic(self, on: bool):
        if self.live_mic_var.get() == on:
            return
        self.live_mic_var.set(on)
        self._on_mic_toggle()

    def _hotkey_toggle_mic(self):
        self._hotkey_set_mic(not self.live_mic_var.get())

    def _test_name(self) -> str:
        return self.test_name_entry.get().strip() or "ТестовПотребител"

    def _test_precheck(self, what: str, tts_var, live_var=None) -> None:
        """Казва предварително какво ще се случи — за да не изглежда, че
        бутонът 'не прави нищо', когато съответната отметка е изключена."""
        problems = []

        if tts_var is not None and not tts_var.get():
            problems.append(f"обявяването на {what} е ИЗКЛЮЧЕНО (таб 'Глас')")
        elif tts_var is not None and not self._speech_allowed("tts"):
            mode = self._cfg("output_mode") or self.output_mode.get()
            problems.append(f"режимът е '{mode}', затова локалният глас мълчи")
        elif self.muted:
            problems.append("звукът е ЗАГЛУШЕН (🔇 горе)")

        if live_var is not None:
            if not self.live_running:
                problems.append("Live AI не е свързан")
            elif not live_var.get():
                problems.append(f"подаването на {what} към Live AI е изключено")

        if problems:
            self._log("   ⚠ Няма да чуеш нищо, защото: " + "; ".join(problems) + ".")

    def _test_comment(self):
        name = self._test_name()
        text = self.test_comment_entry.get().strip() or "тестов коментар"
        self._log("--- ТЕСТ: коментар ---")
        self._test_precheck("коментари", None, self.live_feed_comment_var)
        self._process_incoming_comment(name, "test_user", text, False)

    def _test_follow(self):
        self._log("--- ТЕСТ: нов последовател ---")
        self._test_precheck("нови последователи", self.announce_follow_var, self.live_feed_follow_var)
        self._on_follow_event(self._test_name())

    def _test_share(self):
        self._log("--- ТЕСТ: споделяне ---")
        self._test_precheck("споделяния", self.announce_share_var, self.live_feed_share_var)
        # Чистим защитата от повторение, за да работи тестът всеки път
        self.announced_sharers.discard("test_user")
        self._on_share_event(self._test_name(), "test_user")

    def _test_gift(self):
        self._log("--- ТЕСТ: подарък (единичен) ---")
        self._test_precheck("подаръци", self.announce_gift_var, self.live_feed_gift_var)
        self._on_gift_shoutout(self._test_name(), "Роза", 1)

    def _test_gift_streak(self):
        """Симулира серия: TikTok праща 5 междинни събития и 1 финално.
        Правилното поведение е ЕДНО обявяване, с бройка 5."""
        self._log("--- ТЕСТ: серия подаръци (5 междинни + 1 финално) ---")
        name = self._test_name()
        for i in range(1, 6):
            fake = {"event": "gift", "data": {
                "nickname": name, "uniqueId": "test_user", "giftName": "Роза",
                "giftType": 1, "repeatEnd": False, "repeatCount": i,
            }}
            self._handle_tikfinity_event(fake)
        final = {"event": "gift", "data": {
            "nickname": name, "uniqueId": "test_user", "giftName": "Роза",
            "giftType": 1, "repeatEnd": True, "repeatCount": 5,
        }}
        self._handle_tikfinity_event(final)

    def _test_heart_me(self):
        self._log("--- ТЕСТ: Heart Me подарък ---")
        # Чистим, за да се задейства и при повторен тест
        self.heart_me_senders.discard("test_user")
        self._register_heart_me_gift(self._test_name(), "test_user", "Heart Me")

    def _test_viewers(self):
        self._log("--- ТЕСТ: брой зрители ---")
        self._test_precheck("брой зрители", self.announce_viewers_var, self.live_feed_viewers_var)
        self.last_viewer_announcement_time = 0.0  # за да не го спре throttle-ът
        self._on_viewer_count_event(123)

    def _test_spam(self):
        self._log("--- ТЕСТ: спам (5 еднакви коментара) ---")
        for i in range(5):
            self._process_incoming_comment(self._test_name(), "test_spammer", "спам съобщение", False)

    def _test_gemini_connection(self):
        """Проверява дали ключът работи и показва кои модели са налични."""
        api_key = self.gemini_api_key_entry.get().strip()
        if not api_key:
            self._log("[Тест] Първо сложи Gemini ключ в таб 'AI'.")
            return

        self._log("--- ТЕСТ: връзка с Gemini ---")

        def worker():
            url = f"https://generativelanguage.googleapis.com/v1beta/models?key={api_key}"
            try:
                with urllib.request.urlopen(url, timeout=20) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", errors="ignore")[:200]
                self._log(f"[Тест] Ключът НЕ работи — грешка {e.code}: {body}")
                return
            except Exception as e:
                self._log(f"[Тест] Няма връзка: {e}")
                return

            models = [m.get("name", "").replace("models/", "") for m in data.get("models", [])]
            self._log(f"[Тест] ✓ Ключът работи! Достъпни са {len(models)} модела.")

            live = [m for m in models if "live" in m or "native-audio" in m]
            if live:
                self._log(f"[Тест] Live модели за твоя ключ: {', '.join(live[:6])}")
            else:
                self._log(
                    "[Тест] Не виждам Live модели за този ключ — Live AI може да не тръгне. "
                    "Текстовият AI коментатор би трябвало да работи."
                )

        threading.Thread(target=worker, daemon=True).start()

    def _test_ai_commentator(self):
        """Праща тестов коментар директно на Gemini и показва отговора."""
        api_key = self.gemini_api_key_entry.get().strip()
        if not api_key:
            self._log("[Тест] Първо сложи Gemini ключ в таб 'AI'.")
            return

        name = self._test_name()
        text = self.test_comment_entry.get().strip() or "тестов коментар"
        model = self.gemini_model_entry.get().strip() or TEXT_MODELS[0]
        self._log(f"--- ТЕСТ: AI коментатор (модел {model}) ---")
        self._log(f"[Тест] Пращам: \"{text}\" от {name}...")

        def worker():
            try:
                reply = call_gemini(
                    api_key, model, name, text,
                    streamer_name=self.streamer_name_entry.get().strip(),
                )
                self._log(f"[AI отговор] {reply}")
                if self.ai_speak_var.get():
                    self._enqueue_latest_only(reply, source="ai")
                else:
                    self._log("[Тест] (Изговарянето е изключено — виж отметката в таб 'AI'.)")
            except GeminiError as e:
                self._log(f"[Тест] AI грешка: {e}")

        threading.Thread(target=worker, daemon=True).start()

    def _test_live_feed(self):
        """Праща тестово съобщение към вече свързаното Live AI."""
        if not self.live_running:
            self._log("[Тест] Live AI не е свързан — натисни 'Свържи Live AI' в таб 'Live AI'.")
            return
        name = self._test_name()
        self._log("--- ТЕСТ: съобщение към Live AI ---")
        self._feed_live("follow", name)

    def _test_audio_output(self):
        """Пуска тестов тон директно, заобикаляйки режима и заглушаването —
        така се вижда дали изобщо има звук от приложението."""
        self._log("--- ТЕСТ: звуков изход (кратък тон) ---")

        def worker():
            try:
                rate = 22050
                t = np.arange(int(rate * 0.6), dtype=np.float32) / rate
                tone = (np.sin(2 * np.pi * 440 * t) * 12000).astype(np.int16)
                fade = np.linspace(1.0, 0.0, len(tone), dtype=np.float32)
                tone = (tone * fade).astype(np.int16)

                fd, path = tempfile.mkstemp(suffix=".wav")
                os.close(fd)
                with wave.open(path, "wb") as wf:
                    wf.setnchannels(1)
                    wf.setsampwidth(2)
                    wf.setframerate(rate)
                    wf.writeframes(tone.tobytes())

                snd = pygame.mixer.Sound(path)
                ch = snd.play()
                while ch and ch.get_busy():
                    time.sleep(0.05)
                os.remove(path)
                self._log(
                    "[Тест] Тонът беше пуснат. Ако НЕ го чу: проблемът е в звуковото "
                    "устройство или Windows миксера, не в приложението."
                )
            except Exception as e:
                self._log(f"[Тест] Звукът не работи: {e}")

        threading.Thread(target=worker, daemon=True).start()

    def _test_microphone(self):
        """Записва 3 секунди от микрофона и показва дали изобщо влиза звук."""
        self._log("--- ТЕСТ: микрофон (3 секунди, говори сега) ---")

        def worker():
            try:
                import sounddevice as sd
            except Exception as e:
                self._log(f"[Тест] Микрофонът не е достъпен: {e}")
                return
            try:
                device = self._get_selected_mic_device()
                rec = sd.rec(
                    int(3 * LIVE_INPUT_RATE), samplerate=LIVE_INPUT_RATE,
                    channels=1, dtype="int16", device=device,
                )
                sd.wait()
            except Exception as e:
                self._log(f"[Тест] Грешка при запис: {e}")
                return

            peak = int(np.abs(rec).max())
            pct = round(peak / 32767 * 100)
            if peak < 300:
                self._log(
                    f"[Тест] Не чувам нищо (пик {pct}%). Провери дали е избран правилният "
                    "микрофон и дали не е заглушен в Windows."
                )
            elif peak < 2000:
                self._log(f"[Тест] Чувам те слабо (пик {pct}%). Усили микрофона в Windows.")
            else:
                self._log(f"[Тест] ✓ Микрофонът работи добре (пик {pct}%).")

        threading.Thread(target=worker, daemon=True).start()

    def _test_burst_follows(self):
        n = self._burst_count()
        self._log(f"--- СИМУЛАТОР: {n} последователи наведнъж ---")
        for i in range(n):
            self._on_follow_event(f"Потребител{i + 1}")

    def _test_burst_shares(self):
        n = self._burst_count()
        self._log(f"--- СИМУЛАТОР: {n} споделяния ---")
        for i in range(n):
            key = f"burst_share_{i}"
            self.announced_sharers.discard(key)
            self._on_share_event(f"Споделящ{i + 1}", key)

    def _test_burst_gifts(self):
        n = self._burst_count()
        self._log(f"--- СИМУЛАТОР: {n} подаръка (рози) ---")
        for i in range(n):
            self._on_gift_shoutout(self._test_name(), "Роза")

    def _test_burst_comments(self):
        n = self._burst_count()
        self._log(f"--- СИМУЛАТОР: {n} различни коментара ---")
        samples = [
            "Zdravei kak si", "Много добър стрийм!", "Kakvo igraesh",
            "Поздрави от Пловдив", "haide oshte edna igra", "Браво!",
            "kak se kazva pesenta", "Първи път гледам",
        ]
        self.spam_filter_var.set(False)  # иначе анти-спамът ще ги реже
        for i in range(n):
            self._process_incoming_comment(
                f"Зрител{i + 1}", f"burst_user_{i}", samples[i % len(samples)], False
            )
        self._log("[Симулатор] (Анти-спамът е временно изключен за този тест.)")

    def _burst_count(self) -> int:
        raw = self.burst_count_entry.get().strip()
        try:
            return max(1, min(int(raw), 50)) if raw else 10
        except ValueError:
            return 10

    def _test_reset(self):
        self.announced_sharers.clear()
        self.heart_me_senders.clear()
        self.confirmed_subscribers.clear()
        self.recent_comments.clear()
        self.ai_comment_counter = 0
        self.live_comment_counter = 0
        self.last_viewer_announcement_time = 0.0
        self._log("[Тест] Паметта е изчистена — може да тестваш отначало.")

    def _test_api_full(self):
        """Проверява целия път до Gemini стъпка по стъпка, с времена и сурови
        отговори — за да се види ТОЧНО къде се къса."""
        api_key = self.gemini_api_key_entry.get().strip()
        text_model = self.gemini_model_entry.get().strip() or TEXT_MODELS[0]
        live_model = self.live_model_entry.get().strip() or LIVE_MODELS[0]

        self._log("=" * 46)
        self._log("ПЪЛНА ПРОВЕРКА НА API")
        self._log("=" * 46)

        if not api_key:
            self._log("✗ СТЪПКА 1: Няма ключ. Сложи го горе в таб 'AI'.")
            return
        bad = [c for c in api_key if not (32 < ord(c) < 127)]
        if bad:
            self._log(
                f"✗ СТЪПКА 1: Ключът съдържа непозволени знаци "
                f"(напр. кирилица или интервал): {bad[:5]}. "
                "Изтрий полето и постави ключа наново."
            )
            return
        self._log(f"✓ СТЪПКА 1: Ключ е наличен ({len(api_key)} знака, започва с '{api_key[:6]}…').")

        def worker():
            # --- 2: списък с модели (проверява дали ключът е валиден) ---
            t0 = time.time()
            url = f"https://generativelanguage.googleapis.com/v1beta/models?key={api_key}"
            self._dbg("→", f"GET /v1beta/models")
            try:
                with urllib.request.urlopen(url, timeout=20) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                ms = round((time.time() - t0) * 1000)
                models = [m.get("name", "").replace("models/", "") for m in data.get("models", [])]
                self._log(f"✓ СТЪПКА 2: Ключът е валиден. {len(models)} модела, отговор за {ms} ms.")
                self._dbg("←", models[:12])

                if text_model in models:
                    self._log(f"✓ СТЪПКА 3: Текстовият модел '{text_model}' е достъпен.")
                else:
                    self._log(f"✗ СТЪПКА 3: '{text_model}' НЕ е в списъка — смени го от менюто.")
                    close = [m for m in models if "flash" in m][:5]
                    if close:
                        self._log(f"   Налични подобни: {', '.join(close)}")

                if live_model in models:
                    self._log(f"✓ СТЪПКА 4: Live моделът '{live_model}' е достъпен.")
                else:
                    self._log(f"✗ СТЪПКА 4: '{live_model}' НЕ е в списъка за твоя ключ.")
                    live_avail = [m for m in models if "live" in m or "native-audio" in m][:6]
                    if live_avail:
                        self._log(f"   Опитай с: {', '.join(live_avail)}")
                    else:
                        self._log("   Ключът ти няма достъп до НИКАКВИ Live модели.")
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", errors="ignore")[:300]
                self._log(f"✗ СТЪПКА 2: Ключът е отхвърлен — HTTP {e.code}.")
                self._dbg("←", body)
                return
            except Exception as e:
                self._log(f"✗ СТЪПКА 2: Няма връзка с Google: {e}")
                return

            # --- 5: реална текстова заявка ---
            t0 = time.time()
            self._log("… СТЪПКА 5: Пращам истинска текстова заявка...")
            try:
                reply = call_gemini(api_key, text_model, "Тест", "Кажи 'работи' и нищо друго.")
                ms = round((time.time() - t0) * 1000)
                self._log(f"✓ СТЪПКА 5: Отговор за {ms} ms: \"{reply[:100]}\"")
            except GeminiError as e:
                self._log(f"✗ СТЪПКА 5: {e}")
                return

            # --- 6: Live WebSocket ---
            self._log("… СТЪПКА 6: Отварям Live WebSocket...")
            self._test_live_handshake(api_key, live_model)

        threading.Thread(target=worker, daemon=True).start()

    def _test_live_handshake(self, api_key: str, model: str):
        """Отваря Live връзка, праща setup и текстов ход, чака отговор."""
        import websockets

        async def probe():
            url = LIVE_WS_URL.format(key=api_key)
            for level in range(len(SETUP_LEVELS)):
                try:
                    t0 = time.time()
                    async with websockets.connect(url, max_size=None) as ws:
                        setup = self._build_setup(model, self.live_voice_menu.get(), level)
                        self._dbg("→", {"setup": setup})
                        await ws.send(json.dumps({"setup": setup}))

                        first = await asyncio.wait_for(ws.recv(), timeout=20)
                        self._dbg("←", first)
                        # Сървърът праща байтове, не текст — затова декодираме.
                        msg = _parse_ws(first)

                        if "setupComplete" not in msg:
                            self._log(f"  ✗ Ниво '{SETUP_LEVELS[level]}' — отхвърлено.")
                            continue

                        ms = round((time.time() - t0) * 1000)
                        self._log(
                            f"✓ СТЪПКА 6: Live връзката работи на ниво "
                            f"'{SETUP_LEVELS[level]}' ({ms} ms)."
                        )
                        self.live_setup_level = level

                        # --- 7: истински ход, за да видим отговаря ли ---
                        self._log("… СТЪПКА 7: Пращам текст и чакам аудио отговор...")
                        turn = {"clientContent": {
                            "turns": [{"role": "user", "parts": [{"text": "Кажи здравей съвсем кратко."}]}],
                            "turnComplete": True,
                        }}
                        self._dbg("→", turn)
                        await ws.send(json.dumps(turn))

                        audio_bytes, texts, t1 = 0, [], time.time()
                        while time.time() - t1 < 20:
                            try:
                                raw = await asyncio.wait_for(ws.recv(), timeout=20)
                            except asyncio.TimeoutError:
                                break
                            self._dbg("←", raw)
                            m = _parse_ws(raw)
                            sc = m.get("serverContent") or {}
                            for p in (sc.get("modelTurn") or {}).get("parts", []):
                                d = (p.get("inlineData") or {}).get("data")
                                if d:
                                    audio_bytes += len(d)
                                if p.get("text"):
                                    texts.append(p["text"])
                            tr = (sc.get("outputTranscription") or {}).get("text")
                            if tr:
                                texts.append(tr)
                            if sc.get("turnComplete"):
                                break

                        if audio_bytes:
                            secs = round(audio_bytes * 0.75 / 2 / LIVE_OUTPUT_RATE, 1)
                            self._log(
                                f"✓ СТЪПКА 7: Получено аудио (~{secs} сек). "
                                "API-то работи напълно."
                            )
                            if texts:
                                self._log(f"   AI каза: \"{' '.join(texts)[:120]}\"")
                            self._log("   Ако не си го чул — проблемът е в звука, не в API.")
                        else:
                            self._log(
                                "✗ СТЪПКА 7: Няма аудио в отговора. Моделът приема "
                                "връзката, но не генерира звук."
                            )
                            if texts:
                                self._log(f"   Само текст: \"{' '.join(texts)[:120]}\"")
                        return
                except Exception as e:
                    self._log(f"  ✗ Ниво '{SETUP_LEVELS[level]}': {type(e).__name__}: {str(e)[:120]}")
                    continue

            self._log("✗ СТЪПКА 6: Никое ниво не проработи — виж грешките по-горе.")

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(probe())
        except Exception as e:
            self._log(f"✗ Live проверка се провали: {e}")
        finally:
            loop.close()
            self._log("=" * 46)

    # ==================================================================
    # Проверка при стартиране (съветник)
    # ==================================================================
    def wizard_reset(self):
        self.audio_out_open = False
        self.audio_out_info = "още не е отварян"
        self.audio_fallback = False
        self.audio_written_bytes = 0
        self._fb_buf = b""
        self.api = ApiMetrics()      # броячи за заявките към Gemini
        self.live = LiveMetrics()    # състояние и броячи за Live AI

        self.wizard_steps = []
        self.wizard_done = False
        self.wizard_ok = False

    def _w(self, name, status, detail=""):
        """status: 'run' | 'ok' | 'warn' | 'err'"""
        for s in self.wizard_steps:
            if s["name"] == name:
                s["status"], s["detail"] = status, detail
                return
        self.wizard_steps.append({"name": name, "status": status, "detail": detail})

    def run_wizard(self, mode: str):
        """Проверява всичко нужно за избрания режим, стъпка по стъпка."""
        self.wizard_reset()
        threading.Thread(target=self._wizard_worker, args=(mode,), daemon=True).start()

    def _wizard_worker(self, mode: str):
        import urllib.error
        import urllib.request

        needs_ai = mode in ("Само Live AI", "Пълно")
        needs_tts = mode in ("Само TTS", "Пълно")
        fatal = False

        # ---------- 1. Звуков изход ----------
        self._w("Звуков изход", "run")
        try:
            import sounddevice as sd
            sd.query_devices(kind="output")
            self._w("Звуков изход", "ok", "намерено устройство")
        except Exception as e:
            self._w("Звуков изход", "err", str(e)[:80])
            fatal = True

        # ---------- 2. Български глас (само за TTS) ----------
        if needs_tts:
            self._w("Български глас (Dimitar)", "run")
            for i in range(90):
                if self.voice is not None:
                    break
                if i == 6:
                    self._w("Български глас (Dimitar)", "run",
                            "изтегля се (~60 MB, само първия път)…")
                time.sleep(0.5)
            if self.voice is not None:
                self._w("Български глас (Dimitar)", "ok", "зареден")
            else:
                self._w("Български глас (Dimitar)", "warn",
                        "не се зареди — ще работят само онлайн гласовете")

        # ---------- 3. Gemini ключ ----------
        if needs_ai:
            key = self.gemini_api_key_entry.get().strip()
            self._w("Gemini ключ", "run")
            bad = [c for c in key if not (32 < ord(c) < 127)]
            if not key:
                self._w("Gemini ключ", "err", "липсва")
                fatal = True
            elif bad:
                self._w("Gemini ключ", "err", f"непозволени знаци: {bad[:4]}")
                fatal = True
            else:
                self._w("Gemini ключ", "ok", f"{len(key)} знака")

            # ---------- 4. Връзка с Google ----------
            models = []
            if not fatal:
                self._w("Връзка с Google", "run")
                t0 = time.time()
                try:
                    url = f"https://generativelanguage.googleapis.com/v1beta/models?key={key}"
                    with urllib.request.urlopen(url, timeout=20) as r:
                        data = json.loads(r.read().decode("utf-8"))
                    models = [m.get("name", "").replace("models/", "") for m in data.get("models", [])]
                    self.api.set_models(models)
                    self.api.record("models.list", True, round((time.time() - t0) * 1000))
                    self._w("Връзка с Google", "ok",
                            f"{len(models)} модела, {round((time.time()-t0)*1000)} ms")
                except urllib.error.HTTPError as e:
                    self.api.record("models.list", False, reason="auth" if e.code in (401, 403) else "other")
                    self._w("Връзка с Google", "err", f"HTTP {e.code} — ключът е отхвърлен")
                    fatal = True
                except Exception as e:
                    self._w("Връзка с Google", "err", str(e)[:80])
                    fatal = True

            # ---------- 5. Модели ----------
            if not fatal:
                tm = self.gemini_model_entry.get().strip()
                self._w("Текстов модел", "ok" if tm in models else "warn",
                        tm if tm in models else f"{tm} липсва — ще пробвам друг")
                if tm not in models:
                    alt = [m for m in models if "flash-lite" in m] or [m for m in models if "flash" in m]
                    if alt:
                        self.gemini_model_entry.set(alt[0])
                        self._w("Текстов модел", "ok", f"избран {alt[0]}")

                lm = self.live_model_entry.get().strip()
                live_avail = [m for m in models if "live" in m or "native-audio" in m]
                if lm in models:
                    self._w("Live модел", "ok", lm)
                elif live_avail:
                    self.live_model_entry.set(live_avail[0])
                    self._w("Live модел", "ok", f"избран {live_avail[0]}")
                else:
                    self._w("Live модел", "err", "ключът няма достъп до Live модели")
                    fatal = True
                self._refresh_cfg()

            # ---------- 6. Live връзка + поздрав на глас ----------
            if not fatal:
                self._w("Live връзка", "run")
                self.start_live_ai()
                for _ in range(50):
                    if self.live_running and self.live_ws is not None:
                        break
                    if self.live_fatal:
                        break
                    time.sleep(0.3)

                if self.live_running and self.live_ws is not None:
                    self._w("Live връзка", "ok", "сесията е отворена")
                    self._w("Гласов поздрав", "run")
                    self.live_text_queue.put(
                        "Системна проверка приключи. Кажи КРАТКО на български, че всичко "
                        "е наред и си готов за стрийма. Едно изречение."
                    )
                    time.sleep(4)
                    self._w("Гласов поздрав", "ok", "AI-то проговори")
                else:
                    self._w("Live връзка", "err", "не се отвори — виж лога")
                    fatal = True

        # ---------- 7. TikTok ----------
        src = self.connection_mode.get()
        if src.startswith("TikFinity"):
            self._w("TikFinity", "run")
            try:
                import socket
                from urllib.parse import urlparse
                u = urlparse(self.tikfinity_url_entry.get().strip())
                s = socket.create_connection((u.hostname or "localhost", u.port or 21213), timeout=3)
                s.close()
                self._w("TikFinity", "ok", "приложението отговаря")
            except Exception:
                self._w("TikFinity", "err",
                        "не отговаря — пусни TikFinity и го свържи към стрийма")
                fatal = True
        else:
            name = self.username_entry.get().strip().lstrip("@")
            self._w("TikTok потребител", "ok" if name else "warn",
                    f"@{name}" if name else "не е въведен — попълни го после")

        self.wizard_ok = not fatal
        self.wizard_done = True
        self._log("[Проверка] " + ("✓ Всичко е наред." if not fatal else "✗ Има проблеми — виж списъка."))

    # ==================================================================
    # Самотест само на Live модула
    # ==================================================================
    def selftest_live(self):
        """Отваря сесия, праща изречение, чака аудио, затваря — и казва
        точно докъде е стигнал. Не пипа останалите модули."""
        threading.Thread(target=self._selftest_live_worker, daemon=True).start()

    def _selftest_live_worker(self):
        self.wizard_reset()
        key = self.gemini_api_key_entry.get().strip()

        self._w("Ключ", "run")
        if not key:
            self._w("Ключ", "err", "липсва")
            self.wizard_done, self.wizard_ok = True, False
            return
        bad = [c for c in key if not (32 < ord(c) < 127)]
        if bad:
            self._w("Ключ", "err", f"непозволени знаци: {bad[:4]}")
            self.wizard_done, self.wizard_ok = True, False
            return
        self._w("Ключ", "ok", f"{len(key)} знака")

        was_running = self.live_running
        if not was_running:
            self._w("Отваряне на сесия", "run")
            self.start_live_ai()
        else:
            self._w("Отваряне на сесия", "ok", "вече беше отворена")

        for _ in range(50):
            if self.live_running and self.live_ws is not None:
                break
            if self.live_fatal:
                break
            time.sleep(0.3)

        if not (self.live_running and self.live_ws is not None):
            self._w("Отваряне на сесия", "err", self.live.last_error or "не се отвори")
            self.wizard_done, self.wizard_ok = True, False
            return
        if not was_running:
            self._w("Отваряне на сесия", "ok",
                    f"ниво '{SETUP_LEVELS[self.live_setup_level]}'")

        # --- проверяваме дали наистина се връща звук ---
        self._w("Отговор с глас", "run")
        before = self.live.audio_out_bytes
        turns_before = self.live.turns
        self.live_text_queue.put(
            "Това е проверка на връзката. Кажи само: проверката мина успешно."
        )

        got_audio = False
        for _ in range(40):
            time.sleep(0.4)
            if self.live.audio_out_bytes > before:
                got_audio = True
            if self.live.turns > turns_before:
                break

        if got_audio:
            secs = round((self.live.audio_out_bytes - before) * 0.75 / 48000, 1)
            self._w("Отговор с глас", "ok", f"получени ~{secs} сек звук")
        else:
            self._w("Отговор с глас", "err", "не се върна аудио")

        # --- микрофон (само ако е включен) ---
        if self.mic_active:
            self._w("Микрофон", "run")
            before_in = self.live.audio_in_chunks
            time.sleep(3)
            sent = self.live.audio_in_chunks - before_in
            if sent > 5:
                self._w("Микрофон", "ok", f"изпратени {round(sent * 0.1, 1)} сек")
            else:
                self._w("Микрофон", "warn", "почти нищо не влиза — провери устройството")
        else:
            self._w("Микрофон", "warn", "изключен — не се проверява")

        self.wizard_ok = got_audio
        self.wizard_done = True
        self._log("[Самотест Live] " + ("✓ Модулът работи." if got_audio else "✗ Има проблем."))


    def _detect_devices(self):
        """Пълни списъците с устройства при стартиране."""
        try:
            self._refresh_mic_devices()
        except Exception as e:
            self._log(f"[Микрофон] Не мога да прочета входните устройства: {e}")
        try:
            self._refresh_output_devices()
        except Exception as e:
            self._log(f"[Звук] Не мога да прочета изходните устройства: {e}")


    def _init_fallback_audio(self, rate: int):
        """Подготвя pygame за суров звук. Ако друга част вече е стартирала
        миксера с друга честота, НЕ се борим с нея — просто я разчитаме и
        се съобразяваме в _play_pcm_fallback."""
        try:
            if not pygame.mixer.get_init():
                pygame.mixer.init(frequency=rate, size=-16, channels=1, buffer=2048)
            self._fb_channel = pygame.mixer.Channel(5)   # отделен канал за Live AI
            info = pygame.mixer.get_init()
            self._log(f"[Live AI] Резервен изход: миксерът работи на {info[0]} Hz, "
                      f"{abs(info[1])} бита, {info[2]} канал(а).")
            return True
        except Exception as e:
            self._log(f"[Live AI] Резервният изход не се подготви: {e}")
            return False

    def _fit_to_mixer(self, pcm: bytes, src_rate: int) -> bytes:
        """Преобразува суровия звук към ТОЧНО това, което миксерът ползва.

        Без това звукът звучи забързан и писклив (24 kHz данни, пуснати на
        44.1 kHz, вървят 1.8 пъти по-бързо).
        """
        info = pygame.mixer.get_init()
        if not info:
            return pcm
        mix_rate, _size, mix_ch = info

        samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)

        if mix_rate != src_rate and len(samples) > 1:
            n_out = int(len(samples) * mix_rate / src_rate)
            samples = np.interp(
                np.linspace(0, len(samples) - 1, n_out),
                np.arange(len(samples)),
                samples,
            )

        samples = np.clip(samples, -32768, 32767).astype(np.int16)

        if mix_ch == 2:                       # моно -> стерео
            samples = np.repeat(samples, 2)

        return samples.tobytes()

    def _play_pcm_fallback(self, pcm: bytes, rate: int):
        """Пуска суров PCM без прекъсвания и с правилната скорост."""
        if not getattr(self, "_fb_channel", None):
            if not self._init_fallback_audio(rate):
                return

        buf = self._fb_buf + pcm
        need = int(rate * 2 * 0.6)          # ~0.6 сек от изходния поток
        if len(buf) < need:
            self._fb_buf = buf
            return
        self._fb_buf = b""

        try:
            snd = pygame.mixer.Sound(buffer=self._fit_to_mixer(buf, rate))
            ch = self._fb_channel

            if not ch.get_busy():
                ch.play(snd)
                return

            for _ in range(300):
                if self.muted or not self.live_running:
                    return
                if ch.get_queue() is None:
                    ch.queue(snd)
                    return
                time.sleep(0.02)
        except Exception as e:
            self._log(f"[Live AI] Резервният изход даде грешка: {e}")

    def _flush_fallback(self):
        """Изпраща остатъка в буфера, за да не се губи краят на изречението."""
        rest = self._fb_buf
        self._fb_buf = b""
        if not rest or not getattr(self, "audio_fallback", False):
            return
        try:
            snd = pygame.mixer.Sound(buffer=self._fit_to_mixer(rest, LIVE_OUTPUT_RATE))
            ch = self._fb_channel
            if ch.get_busy():
                for _ in range(200):
                    if ch.get_queue() is None:
                        ch.queue(snd)
                        return
                    time.sleep(0.02)
            else:
                ch.play(snd)
        except Exception:
            pass
