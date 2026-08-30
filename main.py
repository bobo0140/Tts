"""
TikTok Live TTS Reader (Bulgarian) — MVP
-----------------------------------------
Чете на глас коментарите от TikTok Live стрийм на зададен потребител,
използвайки Piper TTS с българския глас "dimitar".

Стартиране (за разработка):
    pip install -r requirements.txt
    python main.py

За готов .exe виж README.md / build.bat.
"""

import asyncio
import functools
import os
import queue
import random
import re
import sys
import tempfile
import threading
import time
import traceback
import wave
from collections import deque
from pathlib import Path

import numpy as np
import customtkinter as ctk
import pygame

from piper import PiperVoice
from piper.config import SynthesisConfig
from piper.download_voices import download_voice
import edge_tts

from TikTokLive import TikTokLiveClient
from TikTokLive.events import (
    ConnectEvent,
    DisconnectEvent,
    CommentEvent,
    LiveEndEvent,
    GiftEvent,
    FollowEvent,
    ShareEvent,
    RoomUserSeqEvent,
)
from TikTokLive.client.web.web_settings import WebDefaults

HEART_ME_GIFT_NAME = "heart me"  # сравнява се без главни/малки букви

# Централен регистър на гласовете: key -> (кратко име за показване, engine, edge voice id или None)
VOICE_REGISTRY = {
    "piper": {"short": "Dimitar", "label": "Dimitar (Piper, офлайн, мъжки)", "engine": "piper"},
    "edge_borislav": {
        "short": "Borislav", "label": "Borislav (Edge TTS, онлайн, мъжки)",
        "engine": "edge", "edge_voice": "bg-BG-BorislavNeural",
    },
    "edge_kalina": {
        "short": "Kalina", "label": "Kalina (Edge TTS, онлайн, женски)",
        "engine": "edge", "edge_voice": "bg-BG-KalinaNeural",
    },
}

# Анти-спам настройки
SPAM_WINDOW_SECONDS = 12          # прозорец за проверка
SPAM_SAME_USER_MAX = 3            # макс. коментари от 1 човек в прозореца
SPAM_SAME_TEXT_MAX = 2            # макс. пъти един и същ текст (от всякакви хора) в прозореца

# --------------------------------------------------------------------------
# Настройки
# --------------------------------------------------------------------------

VOICE_NAME = "bg_BG-dimitar-medium"  # българският глас в Piper

if getattr(sys, "frozen", False):
    # Когато е стартирано като .exe (PyInstaller), пазим гласа до самия .exe,
    # а не в тъмната временна папка на PyInstaller.
    BASE_DIR = Path(sys.executable).parent
else:
    BASE_DIR = Path(__file__).parent

VOICES_DIR = BASE_DIR / "voices"
VOICES_DIR.mkdir(exist_ok=True)

MODEL_PATH = VOICES_DIR / f"{VOICE_NAME}.onnx"
CONFIG_PATH = VOICES_DIR / f"{VOICE_NAME}.onnx.json"

# Символи, които махаме преди TTS (емотикони и др. непроизносими неща)
EMOJI_PATTERN = re.compile(
    "["
    "\U0001F300-\U0001FAFF"
    "\U00002600-\U000027BF"
    "\U0001F1E6-\U0001F1FF"
    "\U00002190-\U000021FF"
    "]+",
    flags=re.UNICODE,
)


def clean_text_for_speech(text: str) -> str:
    text = EMOJI_PATTERN.sub("", text)
    text = re.sub(r"http\S+", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


MENTION_PATTERN = re.compile(r"@[\w.]+", re.UNICODE)


def strip_mentions(text: str) -> str:
    text = MENTION_PATTERN.sub("", text)
    return re.sub(r"\s+", " ", text).strip()


# --------------------------------------------------------------------------
# "Шльокавица" -> кирилица (евристична транслитерация)
# --------------------------------------------------------------------------
# Няма перфектен алгоритъм за това (шльокавицата не е стандартизирана), но
# покриваме най-честите случаи: букви + цифрите 4 ("ч") и 6 ("ш"), които са
# емблематични точно за шльокавицата.

# По-дълги последователности се проверяват първи (най-дългият печели).
_SHL_MULTI = [
    ("sht", "щ"), ("6t", "щ"),
    ("sh", "ш"), ("ch", "ч"), ("zh", "ж"),
    ("yu", "ю"), ("ya", "я"), ("yo", "йо"), ("jo", "йо"),
]
_SHL_SINGLE = {
    "a": "а", "b": "б", "v": "в", "g": "г", "d": "д", "e": "е",
    "z": "з", "i": "и", "k": "к", "l": "л", "m": "м", "n": "н",
    "o": "о", "p": "п", "r": "р", "s": "с", "t": "т", "u": "у",
    "f": "ф", "h": "х", "c": "ц", "j": "ж", "y": "ъ", "w": "ъ",
    "q": "я", "x": "х",
    "4": "ч", "6": "ш",
}

# Кратки латински думи/съкращения, които НЕ искаме да превеждаме
_SHL_WHITELIST = {
    "lol", "gg", "wp", "ok", "okay", "hi", "hey", "bye", "yes", "no",
    "wow", "nice", "cool", "omg", "wtf", "lmao", "xd", "haha", "hahaha",
    "hahahaha", "pog", "poggers", "love", "tiktok", "youtube", "instagram",
    "facebook", "live", "stream", "pro", "top", "fail", "win",
}


def _convert_shlyokavitsa_word(word: str) -> str:
    lower = word.lower()
    if lower in _SHL_WHITELIST:
        return word
    if lower.isupper() and len(word) <= 4:
        return word  # вероятно съкращение (BG, EU, USA...)

    result = []
    i = 0
    n = len(lower)
    vowels_for_semivowel = {"а", "е", "о", "у", "ъ"}
    while i < n:
        matched = False
        for seq, repl in _SHL_MULTI:
            if lower.startswith(seq, i):
                result.append(repl)
                i += len(seq)
                matched = True
                break
        if matched:
            continue
        ch = lower[i]
        if ch == "i" and result and result[-1] in vowels_for_semivowel:
            # "ei"/"ai"/"oi"/"ui" в края или средата обикновено значи "й", не "и"
            # (напр. "zdravei" -> "здравей", "moi" -> "мой")
            result.append("й")
        else:
            result.append(_SHL_SINGLE.get(ch, ch))
        i += 1

    converted = "".join(result)
    if word[:1].isupper():
        converted = converted[:1].upper() + converted[1:]
    return converted


_WORD_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9]+|[^A-Za-z0-9]+", re.UNICODE)
_HAS_CYRILLIC = re.compile(r"[А-Яа-яЁё]")
_LATIN_OR_DIGIT_ONLY = re.compile(r"^[A-Za-z0-9]+$")
_PURE_DIGITS = re.compile(r"^[0-9]+$")


def transliterate_shlyokavitsa(text: str) -> str:
    """Преобразува думи, писани на 'шльокавица' (латиница/цифри), в кирилица.
    Пропуска думи, които вече съдържат кирилски букви, или са чисто числа."""
    out = []
    for token in _WORD_TOKEN_PATTERN.findall(text):
        if _LATIN_OR_DIGIT_ONLY.match(token) and not _PURE_DIGITS.match(token) and not _HAS_CYRILLIC.search(token):
            out.append(_convert_shlyokavitsa_word(token))
        else:
            out.append(token)
    return "".join(out)


# --------------------------------------------------------------------------
# Гласови ефекти (работят само върху суров PCM WAV, т.е. Piper изхода)
# --------------------------------------------------------------------------

def _read_wav_as_array(path: str):
    with wave.open(path, "rb") as wf:
        n_channels = wf.getnchannels()
        sampwidth = wf.getsampwidth()
        framerate = wf.getframerate()
        n_frames = wf.getnframes()
        raw = wf.readframes(n_frames)
    dtype = {1: np.int8, 2: np.int16, 4: np.int32}.get(sampwidth, np.int16)
    samples = np.frombuffer(raw, dtype=dtype).astype(np.float32)
    if n_channels > 1:
        samples = samples.reshape(-1, n_channels)
    return samples, framerate, n_channels, sampwidth


def _write_wav_from_array(path: str, samples, framerate: int, n_channels: int, sampwidth: int):
    dtype = {1: np.int8, 2: np.int16, 4: np.int32}.get(sampwidth, np.int16)
    max_val = float(2 ** (8 * sampwidth - 1) - 1)
    samples = np.clip(samples, -max_val, max_val).astype(dtype)
    with wave.open(path, "wb") as wf:
        wf.setnchannels(n_channels)
        wf.setsampwidth(sampwidth)
        wf.setframerate(framerate)
        wf.writeframes(samples.tobytes())


def apply_voice_effect(wav_path: str, effect: str):
    """Прилага прост звуков ефект директно върху WAV файла (in-place)."""
    if effect == "Няма":
        return

    samples, framerate, n_channels, sampwidth = _read_wav_as_array(wav_path)

    if effect in ("Дълбок глас", "Чипмънк"):
        # Трик с честотата на семплиране: не пипаме данните, само декларираме
        # различна честота при запис -> плейърът го изпълнява по-бавно/дълбоко
        # или по-бързо/писклИво.
        factor = 0.78 if effect == "Дълбок глас" else 1.35
        new_rate = max(4000, int(framerate * factor))
        _write_wav_from_array(wav_path, samples, new_rate, n_channels, sampwidth)
        return

    if effect == "Ехо":
        delay_ms, decay, repeats = 220, 0.45, 3
        delay_samples = int(framerate * delay_ms / 1000)
        out = samples.copy()
        for i in range(1, repeats + 1):
            shift = delay_samples * i
            if shift >= len(samples):
                break
            echo = np.zeros_like(samples)
            echo[shift:] = samples[: len(samples) - shift] * (decay ** i)
            out += echo
        _write_wav_from_array(wav_path, out, framerate, n_channels, sampwidth)
        return

    if effect == "Реверберация":
        # Няколко близки, тихи повторения -> усещане за "стая", вместо ясно ехо
        out = samples.copy()
        for delay_ms, decay in ((15, 0.35), (35, 0.25), (60, 0.18), (95, 0.12)):
            shift = int(framerate * delay_ms / 1000)
            if shift >= len(samples):
                continue
            tap = np.zeros_like(samples)
            tap[shift:] = samples[: len(samples) - shift] * decay
            out += tap
        _write_wav_from_array(wav_path, out, framerate, n_channels, sampwidth)
        return

    if effect == "Робот":
        n = len(samples)
        t = np.arange(n, dtype=np.float32) / framerate
        carrier = np.sin(2 * np.pi * 45.0 * t)
        if n_channels > 1:
            carrier = carrier[:, None]
        modulated = samples * carrier
        # смес от оригинала и модулирания сигнал, за да остане разбираемо
        out = 0.5 * samples + 0.5 * modulated
        _write_wav_from_array(wav_path, out, framerate, n_channels, sampwidth)
        return


_NAME_LETTER_PATTERN = re.compile(r"[^\W\d_]", re.UNICODE)  # букви от всякаква азбука


def is_reasonable_name(name: str, max_len: int = 20) -> bool:
    """Груба проверка дали едно потребителско име е разумно за произнасяне на
    глас — не твърде дълго и не съставено предимно от символи/емоджита/цифри."""
    if not name:
        return False
    cleaned = clean_text_for_speech(name).strip()
    if not cleaned:
        return False
    if len(cleaned) > max_len:
        return False
    letters = len(_NAME_LETTER_PATTERN.findall(cleaned))
    if letters == 0 or letters < len(cleaned) * 0.5:
        return False
    return True


# --------------------------------------------------------------------------
# Приложение
# --------------------------------------------------------------------------

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")


class App(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.title("TikTok Live TTS Reader (BG)")
        self.geometry("640x560")
        self.minsize(560, 480)

        self.voice: PiperVoice | None = None
        self.speech_queue: "queue.Queue[str]" = queue.Queue()
        self.log_queue: "queue.Queue[str]" = queue.Queue()

        self.tiktok_thread: threading.Thread | None = None
        self.tiktok_loop: asyncio.AbstractEventLoop | None = None
        self.tiktok_client: TikTokLiveClient | None = None
        self.tikfinity_loop: asyncio.AbstractEventLoop | None = None
        self.tikfinity_ws = None
        self.is_running = False

        # Анти-спам: последните коментари (време, потребител, нормализиран текст)
        self.recent_comments: "deque" = deque(maxlen=50)
        # Потребители (unique_id), които поне веднъж са пратили подаръка "Heart Me"
        self.heart_me_senders: set[str] = set()
        # Потребители, за които имаме директно потвърждение за абонамент (през TikFinity "subscribe" събитие)
        self.confirmed_subscribers: set[str] = set()
        # За хвърляне на "брой зрители" обявявания на интервал, не при всяко събитие
        self.last_viewer_announcement_time = 0.0

        self._build_ui()

        try:
            pygame.mixer.init()
        except Exception as e:
            self._log(f"[Грешка] Не може да се инициализира звукът: {e}")

        # Изговарящ (speaker) worker — работи през целия живот на приложението
        threading.Thread(target=self._speaker_worker, daemon=True).start()

        # Подготовка/сваляне на българския глас, без да блокираме прозореца
        threading.Thread(target=self._ensure_voice_ready, daemon=True).start()

        self.after(100, self._poll_log_queue)

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------
    def _build_ui(self):
        pad = {"padx": 14, "pady": 8}

        mode_frame = ctk.CTkFrame(self)
        mode_frame.pack(fill="x", **pad)
        ctk.CTkLabel(mode_frame, text="Начин на свързване:").pack(side="left", padx=(0, 8))
        self.connection_mode = ctk.CTkSegmentedButton(
            mode_frame,
            values=["Директно (TikTok)", "TikFinity (Advanced)"],
            command=self._on_connection_mode_changed,
        )
        self.connection_mode.set("Директно (TikTok)")
        self.connection_mode.pack(side="left", fill="x", expand=True)

        self.tabview = ctk.CTkTabview(self)
        self.tabview.pack(fill="both", expand=True, padx=14, pady=(0, 14))
        tab_main = self.tabview.add("Основно")
        tab_filters = self.tabview.add("Филтри")
        tab_events = self.tabview.add("Събития")
        tab_voice = self.tabview.add("Глас")

        # ---------------- Таб "Основно" ----------------
        top = ctk.CTkFrame(tab_main)
        top.pack(fill="x", **pad)
        self.direct_username_frame = top

        ctk.CTkLabel(top, text="TikTok потребителско име:").pack(side="left", padx=(0, 8))
        self.username_entry = ctk.CTkEntry(top, placeholder_text="напр. someusername (без @)")
        self.username_entry.pack(side="left", fill="x", expand=True)

        key_frame = ctk.CTkFrame(tab_main)
        key_frame.pack(fill="x", **pad)
        self.direct_api_key_frame = key_frame
        ctk.CTkLabel(key_frame, text="Euler Stream API ключ (по избор, виж README):").pack(
            side="left", padx=(0, 8)
        )
        self.api_key_entry = ctk.CTkEntry(
            key_frame, placeholder_text="оставяш празно за безплатен общ лимит"
        )
        self.api_key_entry.pack(side="left", fill="x", expand=True)

        tikfinity_frame = ctk.CTkFrame(tab_main)
        self.tikfinity_frame = tikfinity_frame
        ctk.CTkLabel(tikfinity_frame, text="TikFinity WebSocket адрес:").pack(side="left", padx=(0, 8))
        self.tikfinity_url_entry = ctk.CTkEntry(tikfinity_frame)
        self.tikfinity_url_entry.insert(0, "ws://localhost:21213/")
        self.tikfinity_url_entry.pack(side="left", fill="x", expand=True)
        self.tikfinity_note = ctk.CTkLabel(
            tab_main,
            text="(Изисква пуснат и свързан TikFinity на компютъра ти)",
            text_color="gray",
        )

        btns = ctk.CTkFrame(tab_main)
        btns.pack(fill="x", **pad)

        self.start_btn = ctk.CTkButton(btns, text="Старт", command=self.start_listening)
        self.start_btn.pack(side="left", padx=(0, 8))

        self.stop_btn = ctk.CTkButton(btns, text="Стоп", command=self.stop_listening, state="disabled")
        self.stop_btn.pack(side="left")

        self.status_label = ctk.CTkLabel(
            tab_main, text="Подготовка на българския глас...", text_color="orange"
        )
        self.status_label.pack(fill="x", padx=14)

        ctk.CTkLabel(tab_main, text="Лог:").pack(anchor="w", padx=14)

        self.log_box = ctk.CTkTextbox(tab_main, wrap="word")
        self.log_box.pack(fill="both", expand=True, padx=14, pady=(0, 14))
        self.log_box.configure(state="disabled")

        # ---------------- Таб "Филтри" ----------------
        filt = ctk.CTkFrame(tab_filters)
        filt.pack(fill="x", **pad)

        self.filter_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(filt, text="Филтър на думи:", variable=self.filter_var).pack(side="left", padx=(0, 8))
        self.filter_entry = ctk.CTkEntry(
            filt, placeholder_text="дума1, дума2, дума3 (разделени със запетая)"
        )
        self.filter_entry.pack(side="left", fill="x", expand=True)

        extra = ctk.CTkFrame(tab_filters)
        extra.pack(fill="x", **pad)

        self.spam_filter_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(extra, text="Анти-спам защита", variable=self.spam_filter_var).pack(
            side="left", padx=(0, 16)
        )

        self.heart_me_filter_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            extra,
            text="Само от Heart Me донори + абонати на канала",
            variable=self.heart_me_filter_var,
        ).pack(side="left", padx=(0, 16))

        extra2 = ctk.CTkFrame(tab_filters)
        extra2.pack(fill="x", **pad)

        self.strip_mentions_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(
            extra2, text="Пропускай @споменавания (напр. @ivan123)", variable=self.strip_mentions_var
        ).pack(side="left", padx=(0, 16))

        self.shlyokavitsa_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(
            extra2,
            text="Конвертирай 'шльокавица' (Zdravei → Здравей) в кирилица",
            variable=self.shlyokavitsa_var,
        ).pack(side="left", padx=(0, 16))

        maxlen = ctk.CTkFrame(tab_filters)
        maxlen.pack(fill="x", **pad)
        ctk.CTkLabel(maxlen, text="Макс. брой символи за четене:").pack(side="left", padx=(0, 8))
        self.max_chars_entry = ctk.CTkEntry(maxlen, width=80, placeholder_text="200")
        self.max_chars_entry.insert(0, "200")
        self.max_chars_entry.pack(side="left", padx=(0, 16))
        self.skip_instead_of_truncate_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            maxlen,
            text="Пропускай изцяло по-дългите (вместо да ги съкращава)",
            variable=self.skip_instead_of_truncate_var,
        ).pack(side="left")

        # ---------------- Таб "Събития" ----------------
        ctk.CTkLabel(
            tab_events,
            text="Допълнителни гласови обявявания (извън коментарите):",
            text_color="gray",
        ).pack(anchor="w", padx=14, pady=(8, 0))

        self.announce_follow_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            tab_events, text="Обявявай нови последователи", variable=self.announce_follow_var
        ).pack(anchor="w", padx=14, pady=6)

        self.announce_share_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            tab_events, text="Обявявай споделяния на стрийма", variable=self.announce_share_var
        ).pack(anchor="w", padx=14, pady=6)

        gift_frame = ctk.CTkFrame(tab_events)
        gift_frame.pack(fill="x", padx=14, pady=6)
        self.announce_gift_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            gift_frame,
            text="Обявявай подаръци (различни от Heart Me)",
            variable=self.announce_gift_var,
        ).pack(side="left")

        viewers_frame = ctk.CTkFrame(tab_events)
        viewers_frame.pack(fill="x", padx=14, pady=6)
        self.announce_viewers_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            viewers_frame, text="Обявявай брой зрители на всеки", variable=self.announce_viewers_var
        ).pack(side="left", padx=(0, 8))
        self.viewer_interval_entry = ctk.CTkEntry(viewers_frame, width=70)
        self.viewer_interval_entry.insert(0, "300")
        self.viewer_interval_entry.pack(side="left")
        ctk.CTkLabel(viewers_frame, text="секунди").pack(side="left", padx=(6, 0))

        name_len_frame = ctk.CTkFrame(tab_events)
        name_len_frame.pack(fill="x", padx=14, pady=(10, 6))
        ctk.CTkLabel(
            name_len_frame, text="Пропускай странни/твърде дълги имена в обявяванията по-горе:",
            text_color="gray",
        ).pack(anchor="w")
        name_len_row = ctk.CTkFrame(name_len_frame)
        name_len_row.pack(fill="x", pady=(4, 0))
        ctk.CTkLabel(name_len_row, text="Макс. дължина на име:").pack(side="left", padx=(0, 8))
        self.max_name_len_entry = ctk.CTkEntry(name_len_row, width=70)
        self.max_name_len_entry.insert(0, "20")
        self.max_name_len_entry.pack(side="left")

        # ---------------- Таб "Глас" ----------------
        ctk.CTkLabel(
            tab_voice,
            text="Избери глас — Dimitar е офлайн (Piper), Borislav и Kalina са през "
            "Microsoft Edge TTS (безплатно, изисква интернет).",
            text_color="gray",
            wraplength=560,
            justify="left",
        ).pack(anchor="w", padx=14, pady=(8, 12))

        voice_row = ctk.CTkFrame(tab_voice)
        voice_row.pack(fill="x", padx=14, pady=(0, 12))
        ctk.CTkLabel(voice_row, text="Глас:", width=200, anchor="w").pack(side="left")
        self.voice_engine_menu = ctk.CTkOptionMenu(
            voice_row, values=[v["label"] for v in VOICE_REGISTRY.values()]
        )
        self.voice_engine_menu.set(VOICE_REGISTRY["piper"]["label"])
        self.voice_engine_menu.pack(side="left", fill="x", expand=True)

        self.voice_shuffle_var = ctk.BooleanVar(value=False)
        shuffle_row = ctk.CTkFrame(tab_voice)
        shuffle_row.pack(fill="x", padx=14, pady=(0, 4))
        ctk.CTkCheckBox(
            shuffle_row,
            text="Разбъркай гласовете (произволен глас за всеки коментар)",
            variable=self.voice_shuffle_var,
        ).pack(side="left")

        shuffle_pool_frame = ctk.CTkFrame(tab_voice)
        shuffle_pool_frame.pack(fill="x", padx=14, pady=(0, 12))
        ctk.CTkLabel(shuffle_pool_frame, text="Включени в разбъркването:", text_color="gray").pack(
            anchor="w", padx=(20, 0), pady=(4, 2)
        )
        pool_row1 = ctk.CTkFrame(shuffle_pool_frame)
        pool_row1.pack(fill="x", padx=(20, 0))

        self.shuffle_vars: dict[str, ctk.BooleanVar] = {}
        for key in VOICE_REGISTRY:
            var = ctk.BooleanVar(value=True)
            self.shuffle_vars[key] = var
            ctk.CTkCheckBox(pool_row1, text=VOICE_REGISTRY[key]["short"], variable=var).pack(
                side="left", padx=(0, 10)
            )

        def _make_slider(parent, label_text, frm, to, default, fmt="{:.2f}"):
            row = ctk.CTkFrame(parent)
            row.pack(fill="x", padx=14, pady=8)
            ctk.CTkLabel(row, text=label_text, width=200, anchor="w").pack(side="left")
            value_label = ctk.CTkLabel(row, text=fmt.format(default), width=50)
            value_label.pack(side="right")

            def _on_change(v):
                value_label.configure(text=fmt.format(float(v)))

            slider = ctk.CTkSlider(row, from_=frm, to=to, command=_on_change)
            slider.set(default)
            slider.pack(side="left", fill="x", expand=True, padx=10)
            return slider

        ctk.CTkLabel(
            tab_voice,
            text="Настройките отдолу важат и за трите гласа "
            "(за Edge TTS 'изразителност' се пренася като скорост/сила):",
            text_color="gray",
        ).pack(anchor="w", padx=14)

        ctk.CTkLabel(
            tab_voice, text="По-агресивен / енергичен звук ⟵⟶ по-спокоен, провлачен звук",
            text_color="gray",
        ).pack(anchor="w", padx=14)
        self.speed_slider = _make_slider(
            tab_voice, "Скорост на говор (по-ниско = по-бързо):", 0.6, 1.4, 0.85
        )
        self.expressiveness_slider = _make_slider(
            tab_voice, "Изразителност (повече = по-жив звук):", 0.3, 1.3, 0.9
        )
        self.volume_slider = _make_slider(
            tab_voice, "Сила на звука (0.05 = почти тихо, 3.0 = силно усилване):",
            0.05, 3.0, 1.3, fmt="{:.2f}x"
        )

        # ---------------- Гласови ефекти (само за Dimitar/Piper) ----------------
        ctk.CTkLabel(
            tab_voice,
            text="Гласови ефекти — работят само за Dimitar (Piper), защото Borislav/Kalina "
            "идват компресирани от облака и не могат да се обработват допълнително:",
            text_color="gray",
            wraplength=560,
            justify="left",
        ).pack(anchor="w", padx=14, pady=(16, 4))

        effect_row = ctk.CTkFrame(tab_voice)
        effect_row.pack(fill="x", padx=14, pady=(0, 8))
        ctk.CTkLabel(effect_row, text="Ефект:", width=200, anchor="w").pack(side="left")
        self.voice_effect_menu = ctk.CTkOptionMenu(
            effect_row,
            values=["Няма", "Дълбок глас", "Чипмънк", "Ехо", "Робот", "Реверберация"],
        )
        self.voice_effect_menu.set("Няма")
        self.voice_effect_menu.pack(side="left", fill="x", expand=True)

        ctk.CTkButton(
            tab_voice, text="Пробвай гласа с тези настройки", command=self._preview_voice
        ).pack(anchor="w", padx=14, pady=(10, 8))

    def _log(self, msg: str):
        self.log_queue.put(msg)

    def _on_connection_mode_changed(self, selected_value: str):
        is_tikfinity = selected_value == "TikFinity (Advanced)"
        if is_tikfinity:
            self.direct_username_frame.pack_forget()
            self.direct_api_key_frame.pack_forget()
            self.tikfinity_frame.pack(fill="x", padx=14, pady=8)
            self.tikfinity_note.pack(fill="x", padx=14)
        else:
            self.tikfinity_frame.pack_forget()
            self.tikfinity_note.pack_forget()
            self.direct_username_frame.pack(fill="x", padx=14, pady=8)
            self.direct_api_key_frame.pack(fill="x", padx=14, pady=8)

    def _poll_log_queue(self):
        try:
            while True:
                msg = self.log_queue.get_nowait()
                self.log_box.configure(state="normal")
                self.log_box.insert("end", msg + "\n")
                self.log_box.see("end")
                self.log_box.configure(state="disabled")
        except queue.Empty:
            pass
        self.after(100, self._poll_log_queue)

    # ------------------------------------------------------------------
    # Глас (Piper) — сваляне при първо стартиране
    # ------------------------------------------------------------------
    def _ensure_voice_ready(self):
        try:
            if not (MODEL_PATH.exists() and CONFIG_PATH.exists()):
                self._log("[Система] Свалям българския глас (~60 MB), само първия път...")
                download_voice(VOICE_NAME, VOICES_DIR)
                self._log("[Система] Гласът е свален успешно.")

            self.voice = PiperVoice.load(str(MODEL_PATH), str(CONFIG_PATH))
            self.status_label.configure(text="Готово. Въведи потребителско име и натисни Старт.", text_color="lightgreen")
        except Exception as e:
            self._log(f"[Грешка при зареждане на гласа] {e}")
            self.status_label.configure(
                text="Грешка при подготовка на гласа — виж лога. Провери интернет връзката.",
                text_color="red",
            )

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

    def _enqueue_latest_only(self, text: str):
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

    def _process_incoming_comment(self, nickname: str, user_key: str, comment: str, is_subscriber: bool):
        """Обща логика за входящ коментар — ползва се и от директната връзка,
        и от TikFinity връзката."""
        comment = clean_text_for_speech(comment or "")
        if not comment:
            return

        if self.strip_mentions_var.get():
            comment = strip_mentions(comment)
            if not comment:
                return

        if self.shlyokavitsa_var.get():
            converted = transliterate_shlyokavitsa(comment)
            if converted != comment:
                self._log(f"{nickname}: {comment}  ->  {converted}")
            else:
                self._log(f"{nickname}: {comment}")
            comment = converted
        else:
            self._log(f"{nickname}: {comment}")

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

    def _register_heart_me_gift(self, nickname: str, user_key: str, gift_name: str):
        if (gift_name or "").strip().lower() == HEART_ME_GIFT_NAME and user_key:
            if user_key not in self.heart_me_senders:
                self.heart_me_senders.add(user_key)
                self._log(f"[Система] {nickname} прати Heart Me — вече е допустим.")

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

    def _on_follow_event(self, nickname: str):
        if self.announce_follow_var.get() and is_reasonable_name(nickname, self._get_max_name_len()):
            self._announce(f"{nickname} последва канала!")

    def _on_share_event(self, nickname: str):
        if self.announce_share_var.get() and is_reasonable_name(nickname, self._get_max_name_len()):
            self._announce(f"{nickname} сподели стрийма!")

    def _on_gift_shoutout(self, nickname: str, gift_name: str):
        gn = (gift_name or "").strip()
        if gn.lower() == HEART_ME_GIFT_NAME:
            return  # Heart Me си има собствена логика, не го обявяваме отделно
        if (
            self.announce_gift_var.get()
            and gn
            and is_reasonable_name(nickname, self._get_max_name_len())
        ):
            self._announce(f"{nickname} прати подарък {gn}!")

    def _on_viewer_count_event(self, viewer_count):
        if not self.announce_viewers_var.get() or viewer_count is None:
            return
        raw = self.viewer_interval_entry.get().strip()
        try:
            interval = int(raw) if raw else 300
        except ValueError:
            interval = 300
        now = time.time()
        if now - self.last_viewer_announcement_time >= max(interval, 10):
            self.last_viewer_announcement_time = now
            self._announce(f"В момента гледат {viewer_count} души.")

    # ------------------------------------------------------------------
    # Изговорчик (worker thread)
    # ------------------------------------------------------------------
    def _get_synthesis_config(self) -> SynthesisConfig:
        return SynthesisConfig(
            length_scale=float(self.speed_slider.get()),
            noise_scale=float(self.expressiveness_slider.get()),
            volume=float(self.volume_slider.get()),
        )

    def _get_selected_voice(self) -> str:
        """Връща ключ от VOICE_REGISTRY.
        Ако разбъркването е включено, избира произволно измежду включените
        в пула гласове при всяко извикване (т.е. за всеки нов коментар)."""
        if self.voice_shuffle_var.get():
            pool = [key for key, var in self.shuffle_vars.items() if var.get()]
            if pool:
                return random.choice(pool)
            # ако нищо не е отметнато в пула, падаме обратно на падащото меню

        label = self.voice_engine_menu.get()
        for key, info in VOICE_REGISTRY.items():
            if info["label"] == label:
                return key
        return "piper"

    def _edge_tts_params(self):
        """Превръща плъзгачите за скорост/сила в rate/volume параметри за Edge TTS."""
        length_scale = float(self.speed_slider.get())
        volume_mult = float(self.volume_slider.get())

        rate_pct = round((1.0 / max(length_scale, 0.1) - 1.0) * 100)
        rate_pct = max(-80, min(rate_pct, 100))

        vol_pct = round((volume_mult - 1.0) * 100)
        vol_pct = max(-95, min(vol_pct, 100))

        return f"{rate_pct:+d}%", f"{vol_pct:+d}%"

    def _preview_voice(self):
        self.speech_queue.put("Здравей, така ще звуча с тези настройки.")

    def _speaker_worker(self):
        while True:
            text = self.speech_queue.get()
            selected = self._get_selected_voice()

            try:
                if selected == "piper":
                    if self.voice is None:
                        continue

                    syn_config = self._get_synthesis_config()
                    fd, tmp_path = tempfile.mkstemp(suffix=".wav")
                    os.close(fd)
                    with wave.open(tmp_path, "wb") as wav_file:
                        self.voice.synthesize_wav(text, wav_file, syn_config=syn_config)

                    effect = self.voice_effect_menu.get()
                    if effect != "Няма":
                        apply_voice_effect(tmp_path, effect)

                    sound = pygame.mixer.Sound(tmp_path)
                    channel = sound.play()
                    while channel.get_busy():
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

                    pygame.mixer.music.load(tmp_path)
                    pygame.mixer.music.play()
                    while pygame.mixer.music.get_busy():
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
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.status_label.configure(text=f"Свързване към @{username} ...", text_color="orange")

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

        self.start_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        self.status_label.configure(text="Спряно.", text_color="gray")

    # ------------------------------------------------------------------
    # TikFinity връзка (Advanced режим)
    # ------------------------------------------------------------------
    def _start_tikfinity(self):
        url = self.tikfinity_url_entry.get().strip()
        if not url:
            self._log("[Система] Въведи TikFinity WebSocket адрес.")
            return

        self.is_running = True
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.status_label.configure(text=f"Свързване към TikFinity ({url}) ...", text_color="orange")

        self.tiktok_thread = threading.Thread(
            target=self._run_tikfinity_client, args=(url,), daemon=True
        )
        self.tiktok_thread.start()

    def _run_tikfinity_client(self, url: str):
        import json
        import websockets

        self.tikfinity_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.tikfinity_loop)

        async def listen():
            async with websockets.connect(url) as ws:
                self.tikfinity_ws = ws
                self._log("[Система] Свързан към TikFinity. Изчакваме коментари...")
                self.status_label.configure(text="На живо (през TikFinity)", text_color="lightgreen")

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
            self.start_btn.configure(state="normal")
            self.stop_btn.configure(state="disabled")

    def _handle_tikfinity_event(self, msg: dict):
        event_name = msg.get("event")
        data = msg.get("data") or {}

        if event_name == "chat":
            nickname = data.get("nickname") or data.get("uniqueId") or "???"
            user_key = data.get("uniqueId") or str(data.get("userId") or "unknown")
            comment = data.get("comment") or ""
            is_subscriber = bool(data.get("isSubscriber", False))
            self._process_incoming_comment(nickname, user_key, comment, is_subscriber)

        elif event_name == "gift":
            nickname = data.get("nickname") or data.get("uniqueId") or "???"
            user_key = data.get("uniqueId") or str(data.get("userId") or "")
            gift_name = data.get("giftName") or ""
            self._register_heart_me_gift(nickname, user_key, gift_name)
            self._on_gift_shoutout(nickname, gift_name)

        elif event_name == "follow":
            nickname = data.get("nickname") or data.get("uniqueId") or "???"
            self._on_follow_event(nickname)

        elif event_name == "share":
            nickname = data.get("nickname") or data.get("uniqueId") or "???"
            self._on_share_event(nickname)

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
            self.status_label.configure(text=f"На живо: @{username}", text_color="lightgreen")

        @client.on(CommentEvent)
        async def on_comment(event: CommentEvent):
            nickname = event.user.nickname if event.user else "???"
            user_key = (
                (getattr(event.user, "unique_id", None) or str(getattr(event.user, "user_id", "")))
                if event.user else "unknown"
            )
            is_subscriber = False
            if event.user:
                try:
                    is_subscriber = event.user.has_badge("SUBSCRIBER")
                except Exception:
                    pass

            self._process_incoming_comment(nickname, user_key, event.comment or "", is_subscriber)

        @client.on(GiftEvent)
        async def on_gift(event: GiftEvent):
            if not event.user or not event.gift:
                return
            user_key = getattr(event.user, "unique_id", None) or str(
                getattr(event.user, "user_id", "")
            )
            gift_name = event.gift.name or ""
            self._register_heart_me_gift(event.user.nickname, user_key, gift_name)
            self._on_gift_shoutout(event.user.nickname, gift_name)

        @client.on(FollowEvent)
        async def on_follow(event: FollowEvent):
            nickname = event.user.nickname if event.user else "???"
            self._on_follow_event(nickname)

        @client.on(ShareEvent)
        async def on_share(event: ShareEvent):
            nickname = event.user.nickname if event.user else "???"
            self._on_share_event(nickname)

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
            self.start_btn.configure(state="normal")
            self.stop_btn.configure(state="disabled")


if __name__ == "__main__":
    app = App()
    app.mainloop()
