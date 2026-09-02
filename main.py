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
import json
import os
import queue
import random
import re
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.request
import wave
import webbrowser
from collections import deque
from pathlib import Path

import numpy as np
import customtkinter as ctk
import tkinter
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
# Gemini AI коментатор (по избор, ползва безплатен/платен Gemini API ключ)
# --------------------------------------------------------------------------

GEMINI_SYSTEM_PROMPT = (
    "Ти си остроумен AI съ-водещ на български TikTok Live стрийм. "
    "Зрител написа коментар в чата. Реагирай на него кратко (най-много 15-20 думи), "
    "разговорно, на български. Понякога вметни лека шега, но не бъди обиден или груб. "
    "Не повтаряй коментара дословно — реагирай на смисъла му. "
    "Отговори само с репликата, без обяснения, без кавички."
)


class GeminiError(Exception):
    pass


# --------------------------------------------------------------------------
# Gemini Live API (говор-към-говор, реално време)
# --------------------------------------------------------------------------

LIVE_WS_URL = (
    "wss://generativelanguage.googleapis.com/ws/"
    "google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent?key={key}"
)
LIVE_INPUT_RATE = 16000   # Live API изисква 16kHz вход
LIVE_OUTPUT_RATE = 24000  # и връща 24kHz изход

# Актуални Live модели за Gemini Developer API.
# ВНИМАНИЕ: gemini-live-2.5-flash-preview беше спрян на 09.12.2025 — не го ползвай.
# Текстови модели за AI коментатора.
# ВНИМАНИЕ: gemini-2.5-flash-lite вече не се дава на нови потребители.
TEXT_MODELS = [
    "gemini-3.5-flash-lite",   # най-евтин и бърз, препоръчан
    "gemini-3.1-flash",
    "gemini-3.5-flash",
]

LIVE_MODELS = [
    "gemini-3.1-flash-live-preview",                  # препоръчан от Google
    "gemini-2.5-flash-native-audio-preview-12-2025",
    "gemini-2.5-flash-native-audio-preview-09-2025",
]

LIVE_SYSTEM_PROMPT = (
    "Ти си енергичен български AI съ-водещ на TikTok Live стрийм. "
    "Говориш САМО на български, кратко и разговорно — по 1-2 изречения. "
    "Ще получаваш съобщения за случки в стрийма (нови последователи, споделяния, "
    "коментари от чата) и понякога стриймърът ще ти говори директно. "
    "Реагирай живо: поздравявай новите последователи, благодари на тези, които "
    "споделят, коментирай коментарите с лека шега. Ако име звучи смешно или "
    "странно, може да се пошегуваш добронамерено с него, но никога обидно. "
    "Когато стриймърът те пита нещо, отговаряй му директно и кратко."
)


def call_gemini(api_key: str, model: str, nickname: str, comment: str, timeout: int = 15) -> str:
    """Праща коментар на Gemini и връща кратка AI реакция на български.
    Хвърля GeminiError с четимо съобщение при проблем."""
    if not api_key:
        raise GeminiError("Липсва Gemini API ключ.")

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
    prompt = f"{GEMINI_SYSTEM_PROMPT}\n\nПотребител \"{nickname}\" написа: \"{comment}\""
    payload = json.dumps({"contents": [{"parts": [{"text": prompt}]}]}).encode("utf-8")

    req = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="ignore")
        if e.code == 429:
            raise GeminiError("Достигнат е лимитът на Gemini API (твърде много заявки).") from e
        raise GeminiError(f"Gemini API грешка {e.code}: {body[:200]}") from e
    except urllib.error.URLError as e:
        raise GeminiError(f"Няма връзка с Gemini API: {e.reason}") from e
    except TimeoutError:
        raise GeminiError("Gemini API не отговори навреме.")

    try:
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        return text.strip()
    except (KeyError, IndexError, TypeError) as e:
        raise GeminiError(f"Неочакван отговор от Gemini: {data}") from e


# --------------------------------------------------------------------------
# Приложение
# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# Поправка за копиране/поставяне при кирилична подредба на клавиатурата
# --------------------------------------------------------------------------
# При българска подредба Ctrl+V праща кирилски символ и tkinter не разпознава
# вградената команда за поставяне. Затова връзваме по keycode (който не зависи
# от подредбата) и добавяме меню с десен бутон.

_KEYCODE_A, _KEYCODE_C, _KEYCODE_V, _KEYCODE_X = 65, 67, 86, 88


def enable_clipboard(widget):
    """Прави Ctrl+C/V/X/A да работят в полето независимо от езика на клавиатурата,
    и добавя меню с десен бутон (Постави / Копирай / Изрежи / Избери всичко)."""

    def do_paste(_event=None):
        try:
            text = widget.clipboard_get()
        except Exception:
            return "break"
        try:
            if widget.selection_present():
                widget.delete("sel.first", "sel.last")
        except Exception:
            pass
        widget.insert("insert", text.strip())
        return "break"

    def do_copy(_event=None):
        try:
            if widget.selection_present():
                widget.clipboard_clear()
                widget.clipboard_append(widget.selection_get())
        except Exception:
            pass
        return "break"

    def do_cut(_event=None):
        do_copy()
        try:
            if widget.selection_present():
                widget.delete("sel.first", "sel.last")
        except Exception:
            pass
        return "break"

    def do_select_all(_event=None):
        try:
            widget.select_range(0, "end")
            widget.icursor("end")
        except Exception:
            pass
        return "break"

    def on_ctrl_key(event):
        # keycode не зависи от подредбата на клавиатурата (V винаги е 86 и т.н.)
        if event.keycode == _KEYCODE_V:
            return do_paste()
        if event.keycode == _KEYCODE_C:
            return do_copy()
        if event.keycode == _KEYCODE_X:
            return do_cut()
        if event.keycode == _KEYCODE_A:
            return do_select_all()
        return None

    widget.bind("<Control-KeyPress>", on_ctrl_key)
    widget.bind("<Shift-Insert>", do_paste)

    menu = tkinter.Menu(widget, tearoff=0)
    menu.add_command(label="Постави", command=do_paste)
    menu.add_command(label="Копирай", command=do_copy)
    menu.add_command(label="Изрежи", command=do_cut)
    menu.add_separator()
    menu.add_command(label="Избери всичко", command=do_select_all)

    def show_menu(event):
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    widget.bind("<Button-3>", show_menu)
    return widget


# Цветова палитра
ACCENT = "#7C5CFF"        # лилаво-синьо, основен акцент
ACCENT_HOVER = "#6A4AE8"
OK_COLOR = "#4ADE80"
WARN_COLOR = "#FBBF24"
ERR_COLOR = "#F87171"
MUTED = "#8B8B99"
CARD_BG = "#232330"
BAR_BG = "#1C1C26"

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")


class App(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.title("TikTok Live TTS Reader (BG)")
        self.geometry("900x760")
        self.minsize(780, 620)

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
        # Потребители, чието споделяне вече е било обявено (за да не спамим при 2+ споделяния)
        self.announced_sharers: set[str] = set()

        # AI коментатор (Gemini) - опашка + брояч за throttling "на всеки N-ти"
        self.ai_request_queue: "queue.Queue" = queue.Queue()
        self.ai_comment_counter = 0

        # Live AI (Gemini Live API)
        self.live_running = False
        self.live_loop: asyncio.AbstractEventLoop | None = None
        self.live_ws = None
        self.live_text_queue: "queue.Queue" = queue.Queue()   # събития -> AI
        self.live_mic_queue: "queue.Queue" = queue.Queue()    # микрофон -> AI
        self.live_mic_stream = None
        self.live_comment_counter = 0
        # Групиране на събития: буфер + ключалка, за да не правим заявка за всяко събитие
        self.live_event_buffer = []
        self.live_buffer_lock = threading.Lock()

        self._build_ui()
        self._enable_clipboard_everywhere()
        self._load_settings()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(60000, self._autosave_settings)

        threading.Thread(target=self._ai_worker, daemon=True).start()
        threading.Thread(target=self._live_batch_worker, daemon=True).start()

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
    # ==================================================================
    # Интерфейс
    # ==================================================================
    def _section(self, parent, title, subtitle=None):
        """Заглавие на секция с разделител — за визуална подредба."""
        wrap = ctk.CTkFrame(parent, fg_color="transparent")
        wrap.pack(fill="x", padx=4, pady=(18, 8))

        head = ctk.CTkFrame(wrap, fg_color="transparent")
        head.pack(fill="x")
        ctk.CTkFrame(head, width=4, height=20, fg_color=ACCENT, corner_radius=2).pack(
            side="left", padx=(0, 10)
        )
        ctk.CTkLabel(
            head, text=title, font=ctk.CTkFont(size=15, weight="bold"), anchor="w"
        ).pack(side="left")

        if subtitle:
            ctk.CTkLabel(
                wrap, text=subtitle, font=ctk.CTkFont(size=11), text_color=MUTED,
                anchor="w", justify="left", wraplength=640,
            ).pack(fill="x", padx=(14, 0), pady=(4, 0))
        ctk.CTkFrame(wrap, height=1, fg_color="gray25").pack(fill="x", pady=(8, 0))
        return wrap

    def _row(self, parent, label_text=None, label_width=200):
        """Един ред в секция, по избор със заглавие вляво."""
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", padx=4, pady=5)
        if label_text:
            ctk.CTkLabel(row, text=label_text, width=label_width, anchor="w").pack(side="left")
        return row

    def _hint(self, parent, text):
        ctk.CTkLabel(
            parent, text=text, font=ctk.CTkFont(size=11), text_color=MUTED,
            anchor="w", justify="left", wraplength=620,
        ).pack(fill="x", padx=4, pady=(2, 6))

    def _build_ui(self):
        # ---------------- Лента за състояние (винаги видима) ----------------
        bar = ctk.CTkFrame(self, height=62, fg_color=BAR_BG, corner_radius=12)
        bar.pack(fill="x", padx=14, pady=(14, 8))
        bar.pack_propagate(False)

        brand = ctk.CTkFrame(bar, fg_color="transparent")
        brand.pack(side="left", padx=(18, 22))
        ctk.CTkLabel(
            brand, text="TikTok TTS", font=ctk.CTkFont(size=18, weight="bold"), anchor="w"
        ).pack(anchor="w")
        ctk.CTkLabel(
            brand, text="глас и AI за твоя лайв", font=ctk.CTkFont(size=10),
            text_color=MUTED, anchor="w",
        ).pack(anchor="w")

        self.status_label = ctk.CTkLabel(
            bar, text="● Подготовка на гласа...", text_color=WARN_COLOR,
            font=ctk.CTkFont(size=12, weight="bold"),
        )
        self.status_label.pack(side="left", padx=(0, 20))

        self.live_status_label = ctk.CTkLabel(
            bar, text="○ Live AI изключен", text_color=MUTED, font=ctk.CTkFont(size=12)
        )
        self.live_status_label.pack(side="left")

        self.start_btn = ctk.CTkButton(
            bar, text="▶  Старт", width=110, height=36, corner_radius=8,
            font=ctk.CTkFont(size=13, weight="bold"),
            fg_color=ACCENT, hover_color=ACCENT_HOVER, command=self.start_listening,
        )
        self.start_btn.pack(side="right", padx=(10, 18))
        self.stop_btn = ctk.CTkButton(
            bar, text="■  Стоп", width=100, height=36, corner_radius=8, state="disabled",
            fg_color="transparent", border_width=1, border_color="gray40",
            hover_color="gray25", text_color=MUTED, command=self.stop_listening,
        )
        self.stop_btn.pack(side="right")

        # ---------------- Режим на изхода ----------------
        mode_bar = ctk.CTkFrame(self, fg_color=CARD_BG, corner_radius=10)
        mode_bar.pack(fill="x", padx=14, pady=(0, 10))
        ctk.CTkLabel(
            mode_bar, text="Кой говори", font=ctk.CTkFont(size=12, weight="bold")
        ).pack(side="left", padx=(18, 14), pady=10)
        self.output_mode = ctk.CTkSegmentedButton(
            mode_bar,
            values=["TTS гласове", "Само Live AI", "И двете"],
            height=32, font=ctk.CTkFont(size=12),
            selected_color=ACCENT, selected_hover_color=ACCENT_HOVER,
            command=self._on_output_mode_changed,
        )
        self.output_mode.set("TTS гласове")
        self.output_mode.pack(side="left", fill="x", expand=True, padx=(0, 18), pady=10)

        # ---------------- Табове ----------------
        self.tabview = ctk.CTkTabview(
            self, anchor="w", corner_radius=12,
            segmented_button_selected_color=ACCENT,
            segmented_button_selected_hover_color=ACCENT_HOVER,
        )
        self.tabview.pack(fill="both", expand=True, padx=14, pady=(0, 14))

        t_home = ctk.CTkScrollableFrame(self.tabview.add("  Начало  "), fg_color="transparent")
        t_filters = ctk.CTkScrollableFrame(self.tabview.add("  Филтри  "), fg_color="transparent")
        t_voice = ctk.CTkScrollableFrame(self.tabview.add("  Глас  "), fg_color="transparent")
        t_ai = ctk.CTkScrollableFrame(self.tabview.add("  AI  "), fg_color="transparent")
        t_test = ctk.CTkScrollableFrame(self.tabview.add("  Тест  "), fg_color="transparent")
        for f in (t_home, t_filters, t_voice, t_ai, t_test):
            f.pack(fill="both", expand=True)

        self._build_home_tab(t_home)
        self._build_filters_tab(t_filters)
        self._build_voice_tab(t_voice)
        self._build_ai_tab(t_ai)
        self._build_test_tab(t_test)

    # ------------------------------------------------------------------
    def _build_home_tab(self, tab):
        self._section(tab, "Връзка с TikTok", "Директно или през TikFinity, ако имаш проблеми.")

        row = self._row(tab, "Начин на свързване:")
        self.connection_mode = ctk.CTkSegmentedButton(
            row, values=["Директно (TikTok)", "TikFinity (Advanced)"],
            command=self._on_connection_mode_changed,
        )
        self.connection_mode.set("Директно (TikTok)")
        self.connection_mode.pack(side="left", fill="x", expand=True)

        self.direct_username_frame = self._row(tab, "TikTok потребител:")
        self.username_entry = ctk.CTkEntry(
            self.direct_username_frame, placeholder_text="напр. someusername (без @)"
        )
        self.username_entry.pack(side="left", fill="x", expand=True)

        self.direct_api_key_frame = self._row(tab, "Euler Stream ключ:")
        self.api_key_entry = ctk.CTkEntry(
            self.direct_api_key_frame, placeholder_text="по избор — оставяш празно за общия лимит"
        )
        self.api_key_entry.pack(side="left", fill="x", expand=True)

        self.tikfinity_frame = ctk.CTkFrame(tab, fg_color="transparent")
        ctk.CTkLabel(self.tikfinity_frame, text="TikFinity адрес:", width=200, anchor="w").pack(side="left")
        self.tikfinity_url_entry = ctk.CTkEntry(self.tikfinity_frame)
        self.tikfinity_url_entry.insert(0, "ws://localhost:21213/")
        self.tikfinity_url_entry.pack(side="left", fill="x", expand=True)
        self.tikfinity_note = ctk.CTkLabel(
            tab, text="Изисква пуснат и свързан TikFinity на компютъра ти.",
            font=ctk.CTkFont(size=11), text_color="gray", anchor="w",
        )

        self._section(tab, "Лог", "Тук виждаш всичко: коментари, филтри, AI отговори, грешки.")
        self.log_box = ctk.CTkTextbox(
            tab, wrap="word", height=300, corner_radius=8, fg_color=CARD_BG,
            font=ctk.CTkFont(family="Consolas", size=12),
        )
        self.log_box.pack(fill="both", expand=True, padx=4, pady=(0, 8))
        self.log_box.configure(state="disabled")

        btns = ctk.CTkFrame(tab, fg_color="transparent")
        btns.pack(fill="x", padx=4, pady=(0, 10))
        ctk.CTkButton(
            btns, text="Изчисти лога", width=120, fg_color="gray30", hover_color="gray25",
            command=self._clear_log,
        ).pack(side="left")

    # ------------------------------------------------------------------
    def _build_filters_tab(self, tab):
        self._section(tab, "Съдържание на коментарите")

        row = self._row(tab)
        self.filter_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(row, text="Забранени думи:", variable=self.filter_var, width=150).pack(side="left", padx=(0, 8))
        self.filter_entry = ctk.CTkEntry(row, placeholder_text="дума1, дума2, дума3")
        self.filter_entry.pack(side="left", fill="x", expand=True)

        self.strip_mentions_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(
            tab, text="Пропускай @споменавания (напр. @ivan123)", variable=self.strip_mentions_var
        ).pack(anchor="w", padx=8, pady=5)

        self.shlyokavitsa_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(
            tab, text="Конвертирай шльокавица (Zdravei → Здравей)", variable=self.shlyokavitsa_var
        ).pack(anchor="w", padx=8, pady=5)

        self._section(tab, "Спам и злоупотреби")

        self.spam_filter_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(
            tab, text="Анти-спам защита (реже flood и copy-paste)", variable=self.spam_filter_var
        ).pack(anchor="w", padx=8, pady=5)

        self.heart_me_filter_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            tab, text="Чети само от Heart Me донори + абонати", variable=self.heart_me_filter_var
        ).pack(anchor="w", padx=8, pady=5)

        self._section(tab, "Дължина")

        row = self._row(tab, "Макс. символи в коментар:")
        self.max_chars_entry = ctk.CTkEntry(row, width=80)
        self.max_chars_entry.insert(0, "200")
        self.max_chars_entry.pack(side="left", padx=(0, 16))
        self.skip_instead_of_truncate_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            row, text="Пропускай изцяло по-дългите", variable=self.skip_instead_of_truncate_var
        ).pack(side="left")

        row = self._row(tab, "Макс. дължина на име:")
        self.max_name_len_entry = ctk.CTkEntry(row, width=80)
        self.max_name_len_entry.insert(0, "20")
        self.max_name_len_entry.pack(side="left")
        self._hint(tab, "Имена по-дълги от това (или само от символи и цифри) не се обявяват на глас.")

    # ------------------------------------------------------------------
    def _build_voice_tab(self, tab):
        self._section(
            tab, "Избор на глас",
            "Dimitar работи офлайн. Borislav и Kalina са през Microsoft Edge TTS (безплатно, но с интернет).",
        )

        row = self._row(tab, "Глас:")
        self.voice_engine_menu = ctk.CTkOptionMenu(
            row, values=[v["label"] for v in VOICE_REGISTRY.values()]
        )
        self.voice_engine_menu.set(VOICE_REGISTRY["piper"]["label"])
        self.voice_engine_menu.pack(side="left", fill="x", expand=True)

        self.voice_shuffle_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            tab, text="Разбъркай гласовете (произволен глас за всеки коментар)",
            variable=self.voice_shuffle_var,
        ).pack(anchor="w", padx=8, pady=(8, 4))

        pool = self._row(tab, "В разбъркването:")
        self.shuffle_vars = {}
        for key in VOICE_REGISTRY:
            var = ctk.BooleanVar(value=True)
            self.shuffle_vars[key] = var
            ctk.CTkCheckBox(pool, text=VOICE_REGISTRY[key]["short"], variable=var, width=90).pack(
                side="left", padx=(0, 12)
            )

        self._section(tab, "Как звучи", "По-ниска скорост = по-бърз и енергичен говор.")

        def slider(label_text, frm, to, default, fmt="{:.2f}"):
            row = self._row(tab, label_text, label_width=220)
            value_label = ctk.CTkLabel(row, text=fmt.format(default), width=55)
            value_label.pack(side="right")
            s = ctk.CTkSlider(
                row, from_=frm, to=to,
                command=lambda v: value_label.configure(text=fmt.format(float(v))),
            )
            s.set(default)
            s.pack(side="left", fill="x", expand=True, padx=10)
            return s

        self.speed_slider = slider("Скорост на говор:", 0.6, 1.4, 0.85)
        self.expressiveness_slider = slider("Изразителност:", 0.3, 1.3, 0.9)
        self.volume_slider = slider("Сила на звука:", 0.05, 3.0, 1.3, fmt="{:.2f}x")

        row = self._row(tab, "Ефект:", label_width=220)
        self.voice_effect_menu = ctk.CTkOptionMenu(
            row, values=["Няма", "Дълбок глас", "Чипмънк", "Ехо", "Робот", "Реверберация"]
        )
        self.voice_effect_menu.set("Няма")
        self.voice_effect_menu.pack(side="left", fill="x", expand=True)
        self._hint(tab, "Ефектите работят само за Dimitar — другите два гласа идват готови от облака.")

        ctk.CTkButton(
            tab, text="🔊  Пробвай гласа", command=self._preview_voice, width=180, height=36,
            corner_radius=8, fg_color=ACCENT, hover_color=ACCENT_HOVER,
            font=ctk.CTkFont(size=13, weight="bold"),
        ).pack(anchor="w", padx=8, pady=(6, 12))

        self._section(tab, "Гласови обявявания", "Освен коментарите, какво друго да казва на глас.")

        self.announce_follow_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(tab, text="Нови последователи", variable=self.announce_follow_var).pack(
            anchor="w", padx=8, pady=5
        )
        self.announce_share_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(tab, text="Споделяния (веднъж на човек)", variable=self.announce_share_var).pack(
            anchor="w", padx=8, pady=5
        )
        self.announce_gift_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(tab, text="Подаръци (без Heart Me)", variable=self.announce_gift_var).pack(
            anchor="w", padx=8, pady=5
        )

        row = self._row(tab)
        self.announce_viewers_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            row, text="Брой зрители на всеки", variable=self.announce_viewers_var, width=190
        ).pack(side="left", padx=(0, 8))
        self.viewer_interval_entry = ctk.CTkEntry(row, width=70)
        self.viewer_interval_entry.insert(0, "300")
        self.viewer_interval_entry.pack(side="left")
        ctk.CTkLabel(row, text="секунди").pack(side="left", padx=(6, 0))

    # ------------------------------------------------------------------
    def _build_ai_tab(self, tab):
        self._section(
            tab, "Gemini ключ",
            "Един ключ обслужва и текстовия AI коментатор, и Live AI. Взима се безплатно, без карта.",
        )

        row = self._row(tab, "API ключ:")
        self.gemini_api_key_entry = ctk.CTkEntry(row, show="*")
        self.gemini_api_key_entry.pack(side="left", fill="x", expand=True, padx=(0, 8))
        ctk.CTkButton(
            row, text="Постави", width=80,
            command=lambda: self._paste_into(self.gemini_api_key_entry),
        ).pack(side="left", padx=(0, 8))
        ctk.CTkButton(
            row, text="Вземи ключ ↗", width=120,
            command=lambda: webbrowser.open("https://aistudio.google.com/apikey"),
        ).pack(side="left")

        self._section(
            tab, "AI коментатор (текст)",
            "След прочитане на коментар, AI-то реагира кратко — понякога с шега.",
        )

        self.ai_enabled_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(tab, text="Активирай AI коментатор", variable=self.ai_enabled_var).pack(
            anchor="w", padx=8, pady=5
        )
        self.ai_speak_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(
            tab, text="Изговаряй AI отговорите на глас (иначе само в лога)",
            variable=self.ai_speak_var,
        ).pack(anchor="w", padx=8, pady=5)

        row = self._row(tab, "Модел:")
        self.gemini_model_entry = ctk.CTkOptionMenu(row, values=TEXT_MODELS)
        self.gemini_model_entry.set(TEXT_MODELS[0])
        self.gemini_model_entry.pack(side="left", fill="x", expand=True)

        row = self._row(tab, "Реагирай на:")
        self.ai_frequency_menu = ctk.CTkSegmentedButton(
            row, values=["Всеки коментар", "На всеки N-ти"]
        )
        self.ai_frequency_menu.set("На всеки N-ти")
        self.ai_frequency_menu.pack(side="left", fill="x", expand=True)

        row = self._row(tab, "N =")
        self.ai_every_n_entry = ctk.CTkEntry(row, width=70)
        self.ai_every_n_entry.insert(0, "10")
        self.ai_every_n_entry.pack(side="left")
        self._hint(
            tab,
            "Безплатният лимит е ~10-15 заявки в минута. 'На всеки N-ти' те пази в тези граници.",
        )

        self._section(
            tab, "Live AI (говор в реално време)",
            "AI-то говори със собствен глас, поздравява последователи и може да те слуша.",
        )

        row = self._row(tab)
        self.live_start_btn = ctk.CTkButton(
            row, text="Свържи Live AI", command=self.start_live_ai, width=160, height=36,
            corner_radius=8, fg_color=ACCENT, hover_color=ACCENT_HOVER,
            font=ctk.CTkFont(size=13, weight="bold"),
        )
        self.live_start_btn.pack(side="left", padx=(0, 8))
        self.live_stop_btn = ctk.CTkButton(
            row, text="Спри", command=self.stop_live_ai, state="disabled", width=100,
            fg_color="gray30", hover_color="gray25",
        )
        self.live_stop_btn.pack(side="left")

        row = self._row(tab, "Live модел:")
        self.live_model_entry = ctk.CTkOptionMenu(row, values=LIVE_MODELS)
        self.live_model_entry.set(LIVE_MODELS[0])
        self.live_model_entry.pack(side="left", fill="x", expand=True)

        row = self._row(tab, "Глас на AI-то:")
        self.live_voice_menu = ctk.CTkOptionMenu(row, values=["Puck", "Charon", "Kore", "Fenrir", "Aoede"])
        self.live_voice_menu.set("Puck")
        self.live_voice_menu.pack(side="left", fill="x", expand=True)

        self.live_autoreconnect_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(
            tab, text="Свързвай се автоматично при прекъсване", variable=self.live_autoreconnect_var
        ).pack(anchor="w", padx=8, pady=5)

        ctk.CTkLabel(
            tab, text="Какво да подава на Live AI-то:", font=ctk.CTkFont(size=11),
            text_color="gray", anchor="w",
        ).pack(fill="x", padx=8, pady=(8, 2))

        self.live_feed_follow_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(tab, text="Нови последователи", variable=self.live_feed_follow_var).pack(
            anchor="w", padx=8, pady=4
        )
        self.live_feed_share_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(tab, text="Споделяния", variable=self.live_feed_share_var).pack(
            anchor="w", padx=8, pady=4
        )
        self.live_feed_gift_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(tab, text="Подаръци (вкл. Heart Me)", variable=self.live_feed_gift_var).pack(
            anchor="w", padx=8, pady=4
        )

        row = self._row(tab)
        self.live_feed_comment_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(
            row, text="Коментари, на всеки", variable=self.live_feed_comment_var, width=180
        ).pack(side="left", padx=(0, 8))
        self.live_every_n_entry = ctk.CTkEntry(row, width=60)
        self.live_every_n_entry.insert(0, "5")
        self.live_every_n_entry.pack(side="left")
        ctk.CTkLabel(row, text="-ти").pack(side="left", padx=(6, 0))

        row = self._row(tab)
        self.live_feed_viewers_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            row, text="Брой зрители (на интервала от таб 'Глас')",
            variable=self.live_feed_viewers_var,
        ).pack(side="left")

        row = self._row(tab, "Групирай на всеки:")
        self.live_batch_seconds_entry = ctk.CTkEntry(row, width=70)
        self.live_batch_seconds_entry.insert(0, "8")
        self.live_batch_seconds_entry.pack(side="left")
        ctk.CTkLabel(row, text="секунди").pack(side="left", padx=(6, 0))
        self._hint(
            tab,
            "Събитията се трупат и се пращат наведнъж. Ако 20 души те последват за "
            "20 секунди, това е ЕДНА заявка вместо 20 — пести лимита и AI-то реагира "
            "смислено ('20 нови последователи!'), вместо да ги изрежда едно по едно.",
        )

        self._section(tab, "Микрофон", "За да те слуша Live AI-то и да ти отговаря.")

        self.live_mic_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            tab, text="Пусни микрофона", variable=self.live_mic_var, command=self._on_mic_toggle
        ).pack(anchor="w", padx=8, pady=5)

        row = self._row(tab, "Устройство:")
        self.mic_device_menu = ctk.CTkOptionMenu(row, values=["(по подразбиране)"])
        self.mic_device_menu.set("(по подразбиране)")
        self.mic_device_menu.pack(side="left", fill="x", expand=True, padx=(0, 8))
        ctk.CTkButton(row, text="Опресни", width=90, command=self._refresh_mic_devices).pack(side="left")

        row = self._row(tab, "Клавиш:")
        self.hotkey_entry = ctk.CTkEntry(row, width=110)
        self.hotkey_entry.insert(0, "f8")
        self.hotkey_entry.pack(side="left", padx=(0, 8))
        self.hotkey_mode_menu = ctk.CTkOptionMenu(
            row, values=["Задръж за говорене", "Вкл./изкл. с натискане"], width=190
        )
        self.hotkey_mode_menu.set("Задръж за говорене")
        self.hotkey_mode_menu.pack(side="left", padx=(0, 8))
        self.hotkey_btn = ctk.CTkButton(row, text="Активирай", width=100, command=self._toggle_hotkey)
        self.hotkey_btn.pack(side="left")
        self._hint(
            tab,
            "Клавишът работи и когато прозорецът не е на фокус. Примери: f8, ctrl+shift+m, alt+v.",
        )

    # ------------------------------------------------------------------
    def _build_test_tab(self, tab):
        self._section(
            tab, "Тествай без истински лайв",
            "Всички бутони минават през същите филтри и гласове като реалните събития.",
        )

        row = self._row(tab, "Име за тест:", label_width=150)
        self.test_name_entry = ctk.CTkEntry(row)
        self.test_name_entry.insert(0, "ТестовПотребител")
        self.test_name_entry.pack(side="left", fill="x", expand=True)

        row = self._row(tab, "Коментар за тест:", label_width=150)
        self.test_comment_entry = ctk.CTkEntry(row)
        self.test_comment_entry.insert(0, "Zdravei kak si")
        self.test_comment_entry.pack(side="left", fill="x", expand=True)

        self._section(tab, "Симулирай събитие")

        grid = ctk.CTkFrame(tab, fg_color="transparent")
        grid.pack(fill="x", padx=4, pady=4)
        tests = [
            ("💬 Коментар", self._test_comment),
            ("➕ Нов последовател", self._test_follow),
            ("🔁 Споделяне", self._test_share),
            ("🎁 Подарък (Роза)", self._test_gift),
            ("❤️ Heart Me", self._test_heart_me),
            ("👁 Брой зрители", self._test_viewers),
            ("⚠️ Спам (5 бързи)", self._test_spam),
            ("🧹 Изчисти паметта", self._test_reset),
        ]
        for i, (text, cmd) in enumerate(tests):
            ctk.CTkButton(grid, text=text, command=cmd, width=200).grid(
                row=i // 2, column=i % 2, padx=6, pady=5, sticky="ew"
            )
        grid.grid_columnconfigure((0, 1), weight=1)

        self._section(tab, "AI тестове", "Изискват Gemini ключ. Започни с проверката на връзката.")

        ai_grid = ctk.CTkFrame(tab, fg_color="transparent")
        ai_grid.pack(fill="x", padx=4, pady=(4, 6))
        ai_tests = [
            ("🔌 Тест връзка с Gemini", self._test_gemini_connection),
            ("🤖 Тест AI коментатор", self._test_ai_commentator),
            ("🎙 Тест микрофон (3 сек)", self._test_microphone),
            ("📡 Изпрати към Live AI", self._test_live_feed),
        ]
        for i, (text, cmd) in enumerate(ai_tests):
            ctk.CTkButton(ai_grid, text=text, command=cmd, height=34).grid(
                row=i // 2, column=i % 2, padx=6, pady=5, sticky="ew"
            )
        ai_grid.grid_columnconfigure((0, 1), weight=1)

        self._section(
            tab, "Симулатор на наплив",
            "Симулира какво става, когато много хора реагират наведнъж — точно случаят, "
            "заради който групираме заявките.",
        )

        row = self._row(tab, "Брой събития:", label_width=150)
        self.burst_count_entry = ctk.CTkEntry(row, width=80)
        self.burst_count_entry.insert(0, "10")
        self.burst_count_entry.pack(side="left")
        ctk.CTkLabel(row, text="(макс. 50)", text_color="gray").pack(side="left", padx=(8, 0))

        burst_grid = ctk.CTkFrame(tab, fg_color="transparent")
        burst_grid.pack(fill="x", padx=4, pady=(4, 14))
        bursts = [
            ("👥 Наплив последователи", self._test_burst_follows),
            ("🔁 Наплив споделяния", self._test_burst_shares),
            ("🌹 Наплив подаръци", self._test_burst_gifts),
            ("💬 Наплив коментари", self._test_burst_comments),
        ]
        for i, (text, cmd) in enumerate(bursts):
            ctk.CTkButton(burst_grid, text=text, command=cmd, height=34).grid(
                row=i // 2, column=i % 2, padx=6, pady=5, sticky="ew"
            )
        burst_grid.grid_columnconfigure((0, 1), weight=1)

        self._section(tab, "Настройки")
        srow = ctk.CTkFrame(tab, fg_color="transparent")
        srow.pack(fill="x", padx=4, pady=(4, 14))
        ctk.CTkButton(
            srow, text="💾 Запази настройките сега", command=self._save_settings, height=34
        ).pack(side="left", padx=(0, 8))
        self._hint(
            tab,
            "Настройките се запазват автоматично при затваряне и на всяка минута, "
            "във файл settings.json до приложението.",
        )

    # ------------------------------------------------------------------
    # Тестови симулации
    # ------------------------------------------------------------------
    def _test_name(self) -> str:
        return self.test_name_entry.get().strip() or "ТестовПотребител"

    def _test_comment(self):
        name = self._test_name()
        text = self.test_comment_entry.get().strip() or "тестов коментар"
        self._log("--- ТЕСТ: коментар ---")
        self._process_incoming_comment(name, "test_user", text, False)

    def _test_follow(self):
        self._log("--- ТЕСТ: нов последовател ---")
        self._on_follow_event(self._test_name())

    def _test_share(self):
        self._log("--- ТЕСТ: споделяне ---")
        # Чистим защитата от повторение, за да работи тестът всеки път
        self.announced_sharers.discard("test_user")
        self._on_share_event(self._test_name(), "test_user")

    def _test_gift(self):
        self._log("--- ТЕСТ: подарък ---")
        self._on_gift_shoutout(self._test_name(), "Роза")

    def _test_heart_me(self):
        self._log("--- ТЕСТ: Heart Me подарък ---")
        # Чистим, за да се задейства и при повторен тест
        self.heart_me_senders.discard("test_user")
        self._register_heart_me_gift(self._test_name(), "test_user", "Heart Me")

    def _test_viewers(self):
        self._log("--- ТЕСТ: брой зрители ---")
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
                reply = call_gemini(api_key, model, name, text)
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

    def _ui(self, fn, *args, **kwargs):
        """Изпълнява UI промяна в главната нишка (tkinter не е thread-safe).
        Без това обновяване от фонов процес хвърля 'main thread is not in main loop'."""
        try:
            self.after(0, lambda: fn(*args, **kwargs))
        except Exception:
            pass

    def _set_status(self, label, text, color):
        self._ui(label.configure, text=text, text_color=color)

    def _set_btn(self, button, state):
        self._ui(button.configure, state=state)

    def _paste_into(self, entry):
        """Поставя от клипборда в подаденото поле (за бутона 'Постави')."""
        try:
            text = self.clipboard_get().strip()
        except Exception:
            self._log("[Клипборд] Клипбордът е празен или недостъпен.")
            return
        entry.delete(0, "end")
        entry.insert(0, text)

    def _enable_clipboard_everywhere(self):
        """Прилага поправката за копиране/поставяне върху всички полета в
        приложението (важно при кирилична подредба на клавиатурата)."""
        count = 0

        def walk(widget):
            nonlocal count
            for child in widget.winfo_children():
                if isinstance(child, ctk.CTkEntry):
                    try:
                        enable_clipboard(child)
                        count += 1
                    except Exception:
                        pass
                walk(child)

        walk(self)
        return count

    # ==================================================================
    # Запазване и зареждане на настройките
    # ==================================================================
    SETTINGS_ENTRIES = [
        "username_entry", "api_key_entry", "tikfinity_url_entry", "filter_entry",
        "max_chars_entry", "max_name_len_entry", "viewer_interval_entry",
        "gemini_api_key_entry", "ai_every_n_entry", "live_every_n_entry",
        "live_batch_seconds_entry", "hotkey_entry", "test_name_entry",
        "test_comment_entry",
    ]
    SETTINGS_BOOLS = [
        "filter_var", "strip_mentions_var", "shlyokavitsa_var", "spam_filter_var",
        "heart_me_filter_var", "skip_instead_of_truncate_var", "voice_shuffle_var",
        "announce_follow_var", "announce_share_var", "announce_gift_var",
        "announce_viewers_var", "ai_enabled_var", "ai_speak_var",
        "live_feed_follow_var", "live_feed_share_var", "live_feed_gift_var",
        "live_feed_comment_var", "live_feed_viewers_var", "live_autoreconnect_var",
    ]
    SETTINGS_MENUS = [
        "connection_mode", "output_mode", "voice_engine_menu", "voice_effect_menu",
        "gemini_model_entry", "ai_frequency_menu", "live_model_entry",
        "live_voice_menu", "mic_device_menu", "hotkey_mode_menu",
    ]
    SETTINGS_SLIDERS = ["speed_slider", "expressiveness_slider", "volume_slider"]

    def _settings_path(self) -> Path:
        return BASE_DIR / "settings.json"

    def _save_settings(self, silent: bool = False):
        data = {"entries": {}, "bools": {}, "menus": {}, "sliders": {}, "shuffle": {}}
        try:
            for name in self.SETTINGS_ENTRIES:
                w = getattr(self, name, None)
                if w is not None:
                    data["entries"][name] = w.get()
            for name in self.SETTINGS_BOOLS:
                v = getattr(self, name, None)
                if v is not None:
                    data["bools"][name] = bool(v.get())
            for name in self.SETTINGS_MENUS:
                w = getattr(self, name, None)
                if w is not None:
                    data["menus"][name] = w.get()
            for name in self.SETTINGS_SLIDERS:
                w = getattr(self, name, None)
                if w is not None:
                    data["sliders"][name] = float(w.get())
            for key, var in getattr(self, "shuffle_vars", {}).items():
                data["shuffle"][key] = bool(var.get())

            self._settings_path().write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            if not silent:
                self._log(f"[Настройки] Запазени в {self._settings_path().name}")
        except Exception as e:
            if not silent:
                self._log(f"[Настройки] Грешка при запазване: {e}")

    def _load_settings(self):
        path = self._settings_path()
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            self._log(f"[Настройки] Не мога да прочета {path.name}: {e}")
            return

        for name, value in data.get("entries", {}).items():
            w = getattr(self, name, None)
            if w is not None:
                try:
                    w.delete(0, "end")
                    w.insert(0, value)
                except Exception:
                    pass
        for name, value in data.get("bools", {}).items():
            v = getattr(self, name, None)
            if v is not None:
                try:
                    v.set(bool(value))
                except Exception:
                    pass
        for name, value in data.get("menus", {}).items():
            w = getattr(self, name, None)
            if w is not None:
                try:
                    w.set(value)
                except Exception:
                    pass
        for name, value in data.get("sliders", {}).items():
            w = getattr(self, name, None)
            if w is not None:
                try:
                    w.set(float(value))
                except Exception:
                    pass
        for key, value in data.get("shuffle", {}).items():
            var = getattr(self, "shuffle_vars", {}).get(key)
            if var is not None:
                try:
                    var.set(bool(value))
                except Exception:
                    pass

        # прилагаме заредения режим на свързване (показва/скрива правилните полета)
        try:
            self._on_connection_mode_changed(self.connection_mode.get())
        except Exception:
            pass

        self._log("[Настройки] Заредени от предишния път.")

    def _autosave_settings(self):
        self._save_settings(silent=True)
        self.after(60000, self._autosave_settings)

    def _on_close(self):
        self._save_settings(silent=True)
        try:
            self._unregister_hotkey()
        except Exception:
            pass
        self.destroy()

    def _clear_log(self):
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.configure(state="disabled")

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
            self._set_status(self.status_label, "● Готов", OK_COLOR)
        except Exception as e:
            self._log(f"[Грешка при зареждане на гласа] {e}")
            self._set_status(self.status_label, "● Грешка с гласа — виж лога", ERR_COLOR)

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

    def _on_output_mode_changed(self, value: str):
        if value == "Само Live AI":
            self._log(
                "[Режим] Само Live AI — локалните гласове (вкл. четенето на AI текста) "
                "са заглушени. Чува се само гласът на Gemini."
            )
        elif value == "TTS гласове":
            self._log("[Режим] TTS гласове — Dimitar/Borislav/Kalina четат всичко.")
        else:
            self._log("[Режим] И двете — локалните гласове и Live AI говорят заедно.")

    def _speech_allowed(self, source: str) -> bool:
        """Дали даден източник има право да ползва ЛОКАЛНИЯ глас (Piper/Edge).
        Live AI не минава оттук — то си пуска аудиото директно.
        source: 'tts' (коментари/обявявания) или 'ai' (текст от Gemini)."""
        mode = self.output_mode.get()
        if mode == "Само Live AI":
            return False  # нищо локално не говори — само гласът на Gemini
        if mode == "И двете":
            return True
        return True  # "TTS гласове"

    def _enqueue_latest_only(self, text: str, source: str = "tts"):
        # Режимът решава дали този източник изобщо има право да говори.
        if source != "preview" and not self._speech_allowed(source):
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
        self._maybe_trigger_ai_commentary(nickname, comment)
        self._maybe_feed_live_comment(nickname, comment)

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
    def _maybe_trigger_ai_commentary(self, nickname: str, comment: str):
        if not self.ai_enabled_var.get():
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

    def _maybe_feed_live_comment(self, nickname: str, comment: str):
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

        self._feed_live("comment", f'{nickname}: "{comment}"')

    # ------------------------------------------------------------------
    # Live AI (Gemini Live API) — говор-към-говор
    # ------------------------------------------------------------------
    def start_live_ai(self):
        api_key = self.gemini_api_key_entry.get().strip()
        if not api_key:
            self._log("[Live AI] Липсва Gemini API ключ — попълни го в таб 'AI'.")
            return
        if self.live_running:
            return

        self.live_running = True
        self.live_start_btn.configure(state="disabled")
        self.live_stop_btn.configure(state="normal")
        self.live_status_label.configure(text="◌ Live AI свързване...", text_color="orange")

        threading.Thread(target=self._run_live_client, args=(api_key,), daemon=True).start()

    def stop_live_ai(self):
        self.live_running = False
        self._stop_mic()
        if self.live_loop and self.live_ws:
            try:
                self.live_loop.call_soon_threadsafe(
                    functools.partial(asyncio.ensure_future, self.live_ws.close())
                )
            except Exception:
                pass
        self.live_start_btn.configure(state="normal")
        self.live_stop_btn.configure(state="disabled")
        self.live_status_label.configure(text="○ Live AI изключен", text_color="gray")

    def _feed_live(self, kind: str, detail: str):
        """Слага събитие в буфера вместо да праща веднага.
        Така 20 последователи за 20 секунди стават ЕДНА заявка, не 20.
        kind: 'follow' | 'share' | 'gift' | 'comment'"""
        if not self.live_running:
            return
        with self.live_buffer_lock:
            self.live_event_buffer.append((kind, detail))

    def _live_batch_worker(self):
        """Периодично събира натрупаните събития в едно съобщение и го праща."""
        while True:
            try:
                interval = max(2, int(self.live_batch_seconds_entry.get().strip() or 8))
            except (ValueError, AttributeError):
                interval = 8

            time.sleep(interval)

            if not self.live_running:
                continue

            with self.live_buffer_lock:
                events = self.live_event_buffer[:]
                self.live_event_buffer.clear()

            if not events:
                continue

            message = self._compose_batch_message(events)
            if message:
                self.live_text_queue.put(message)
                self._log(f"[Live AI ->] ({len(events)} събития в 1 заявка)")

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
                names = ", ".join(follows[:8])
                extra = f" и още {len(follows) - 8}" if len(follows) > 8 else ""
                parts.append(f"{len(follows)} нови последователи: {names}{extra}.")

        if shares:
            if len(shares) == 1:
                parts.append(f"{shares[0]} сподели стрийма.")
            else:
                parts.append(f"{len(shares)} души споделиха стрийма: {', '.join(shares[:8])}.")

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
            + " Реагирай общо и кратко на всичко това наведнъж, в едно изказване."
        )

    def _on_mic_toggle(self):
        if self.live_mic_var.get():
            self._start_mic()
        else:
            self._stop_mic()

    def _refresh_mic_devices(self):
        try:
            import sounddevice as sd
            devices = sd.query_devices()
        except Exception as e:
            self._log(f"[Микрофон] Не мога да прочета устройствата: {e}")
            return

        names = ["(по подразбиране)"]
        self.mic_device_map = {}
        for idx, dev in enumerate(devices):
            if dev.get("max_input_channels", 0) > 0:
                label = f"{idx}: {dev['name']}"[:60]
                names.append(label)
                self.mic_device_map[label] = idx

        current = self.mic_device_menu.get()
        self.mic_device_menu.configure(values=names)
        if current not in names:
            self.mic_device_menu.set("(по подразбиране)")
        self._log(f"[Микрофон] Намерени {len(names) - 1} входни устройства.")

    def _get_selected_mic_device(self):
        label = self.mic_device_menu.get()
        if label == "(по подразбиране)":
            return None
        return getattr(self, "mic_device_map", {}).get(label)

    def _toggle_hotkey(self):
        if getattr(self, "hotkey_active", False):
            self._unregister_hotkey()
        else:
            self._register_hotkey()

    def _register_hotkey(self):
        combo = self.hotkey_entry.get().strip()
        if not combo:
            self._log("[Клавиш] Въведи клавиш (напр. f8).")
            return
        try:
            import keyboard
        except Exception as e:
            self._log(f"[Клавиш] Библиотеката за глобални клавиши не е достъпна: {e}")
            return

        hold_mode = self.hotkey_mode_menu.get() == "Задръж за говорене"

        try:
            if hold_mode:
                keyboard.on_press_key(combo, lambda _e: self._hotkey_set_mic(True))
                keyboard.on_release_key(combo, lambda _e: self._hotkey_set_mic(False))
            else:
                keyboard.add_hotkey(combo, self._hotkey_toggle_mic)
        except Exception as e:
            self._log(f"[Клавиш] Не мога да регистрирам '{combo}': {e}")
            return

        self.hotkey_active = True
        self.hotkey_combo = combo
        self.hotkey_btn.configure(text="Изключи")
        mode_text = "задържане" if hold_mode else "превключване"
        self._log(f"[Клавиш] '{combo}' е активен ({mode_text}).")

    def _unregister_hotkey(self):
        try:
            import keyboard
            keyboard.unhook_all()
        except Exception:
            pass
        self.hotkey_active = False
        self.hotkey_btn.configure(text="Активирай")
        self._log("[Клавиш] Изключен.")

    def _hotkey_set_mic(self, on: bool):
        if self.live_mic_var.get() == on:
            return
        self.live_mic_var.set(on)
        self._on_mic_toggle()

    def _hotkey_toggle_mic(self):
        self._hotkey_set_mic(not self.live_mic_var.get())

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
            if self.live_running and self.live_mic_var.get():
                self.live_mic_queue.put(bytes(indata))

        try:
            self.live_mic_stream = sd.RawInputStream(
                samplerate=LIVE_INPUT_RATE, blocksize=1600, dtype="int16",
                channels=1, callback=callback, device=self._get_selected_mic_device(),
            )
            self.live_mic_stream.start()
            self._log("[Live AI] Микрофонът е включен.")
        except Exception as e:
            self._log(f"[Live AI] Грешка при пускане на микрофона: {e}")
            self.live_mic_stream = None
            self.live_mic_var.set(False)

    def _stop_mic(self):
        if self.live_mic_stream is not None:
            try:
                self.live_mic_stream.stop()
                self.live_mic_stream.close()
            except Exception:
                pass
            self.live_mic_stream = None
            self._log("[Live AI] Микрофонът е изключен.")

    def _run_live_client(self, api_key: str):
        import base64
        import websockets

        self.live_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.live_loop)

        model = self.live_model_entry.get().strip() or LIVE_MODELS[0]
        voice = self.live_voice_menu.get()
        url = LIVE_WS_URL.format(key=api_key)

        async def sender(ws):
            """Праща микрофонно аудио и текстови събития към AI-то."""
            while self.live_running:
                sent_something = False

                # микрофон
                try:
                    while True:
                        chunk = self.live_mic_queue.get_nowait()
                        await ws.send(json.dumps({
                            "realtimeInput": {
                                "mediaChunks": [{
                                    "mimeType": f"audio/pcm;rate={LIVE_INPUT_RATE}",
                                    "data": base64.b64encode(chunk).decode("ascii"),
                                }]
                            }
                        }))
                        sent_something = True
                except queue.Empty:
                    pass

                # текстови събития от стрийма
                try:
                    while True:
                        text = self.live_text_queue.get_nowait()
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

        async def receiver(ws, out_stream):
            """Получава аудио от AI-то и го пуска през говорителите."""
            async for raw in ws:
                if not self.live_running:
                    break
                try:
                    msg = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    continue

                server_content = msg.get("serverContent") or {}
                model_turn = server_content.get("modelTurn") or {}
                for part in model_turn.get("parts", []):
                    inline = part.get("inlineData") or {}
                    data_b64 = inline.get("data")
                    if data_b64 and out_stream is not None:
                        try:
                            out_stream.write(base64.b64decode(data_b64))
                        except Exception:
                            pass
                    text = part.get("text")
                    if text:
                        self._log(f"[Live AI] {text}")

        async def run():
            out_stream = None
            try:
                import sounddevice as sd
                out_stream = sd.RawOutputStream(
                    samplerate=LIVE_OUTPUT_RATE, dtype="int16", channels=1
                )
                out_stream.start()
            except Exception as e:
                self._log(f"[Live AI] Няма изход за звук ({e}) — ще виждаш само текста.")

            async with websockets.connect(url, max_size=None) as ws:
                self.live_ws = ws
                self._log(f"[Live AI] WebSocket отворен. Изпращам setup за модел '{model}'...")
                await ws.send(json.dumps({
                    "setup": {
                        "model": f"models/{model}",
                        "generationConfig": {
                            "responseModalities": ["AUDIO"],
                            "speechConfig": {
                                "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voice}}
                            },
                        },
                        "systemInstruction": {"parts": [{"text": LIVE_SYSTEM_PROMPT}]},
                    }
                }))

                first = await asyncio.wait_for(ws.recv(), timeout=20)
                try:
                    first_msg = json.loads(first)
                except (json.JSONDecodeError, TypeError):
                    first_msg = {"raw": str(first)[:300]}

                if "setupComplete" not in first_msg:
                    self._log(f"[Live AI] Сървърът отговори неочаквано: {first_msg}")
                else:
                    self._log("[Live AI] Setup потвърден от сървъра.")

                self._log("[Live AI] Свързан и готов. Пробвай да кажеш нещо или пусни тест.")
                self._set_status(self.live_status_label, "● Live AI активен", OK_COLOR)

                await asyncio.gather(sender(ws), receiver(ws, out_stream))

            if out_stream is not None:
                try:
                    out_stream.stop()
                    out_stream.close()
                except Exception:
                    pass

        try:
            self.live_loop.run_until_complete(run())
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
            elif "429" in detail:
                self._log("[Съвет] Достигнат лимит на безплатното ниво. Изчакай малко.")
            else:
                self._log(
                    "[Съвет] Провери интернет връзката и дали ключът е активен. "
                    "Ползвай 'Тест връзка с Gemini' в таб 'Тест' за проверка."
                )
            traceback.print_exc()
        finally:
            self.live_ws = None
            self._stop_mic()

            # Автоматично пресвързване, ако връзката е паднала сама
            if self.live_running and self.live_autoreconnect_var.get():
                self._log("[Live AI] Връзката падна — пресвързвам се след 3 секунди...")
                self._set_status(self.live_status_label, "◌ Live AI пресвързване...", "orange")
                time.sleep(3)
                if self.live_running:
                    threading.Thread(
                        target=self._run_live_client, args=(api_key,), daemon=True
                    ).start()
                    return

            self.live_running = False
            self._set_btn(self.live_start_btn, "normal")
            self._set_btn(self.live_stop_btn, "disabled")
            self._set_status(self.live_status_label, "○ Live AI прекъснат", "gray")

    def _ai_worker(self):
        while True:
            nickname, comment = self.ai_request_queue.get()
            api_key = self.gemini_api_key_entry.get().strip()
            model = self.gemini_model_entry.get().strip() or TEXT_MODELS[0]
            try:
                reply = call_gemini(api_key, model, nickname, comment)
                if reply:
                    self._log(f"[AI] {reply}")
                    if self.ai_speak_var.get():
                        self._enqueue_latest_only(reply, source="ai")
            except GeminiError as e:
                self._log(f"[AI грешка] {e}")

    def _on_follow_event(self, nickname: str):
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

        if self.live_running and self.live_feed_share_var.get():
            self._feed_live("share", nickname)

        if not self.announce_share_var.get():
            return
        if not is_reasonable_name(nickname, self._get_max_name_len()):
            return
        self._announce(f"{nickname} сподели стрийма!")

    def _on_gift_shoutout(self, nickname: str, gift_name: str):
        gn = (gift_name or "").strip()
        if gn.lower() == HEART_ME_GIFT_NAME:
            return  # Heart Me си има собствена логика, не го обявяваме отделно

        if self.live_running and self.live_feed_gift_var.get() and gn:
            self._feed_live("gift", f"{nickname} — {gn}")

        if (
            self.announce_gift_var.get()
            and gn
            and is_reasonable_name(nickname, self._get_max_name_len())
        ):
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

        if want_tts:
            self._announce(f"В момента гледат {viewer_count} души.")
        if want_live:
            self._feed_live("viewers", str(viewer_count))

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
        # Ръчна проба — винаги се чува, независимо от режима
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
        self.status_label.configure(text=f"◌ Свързване към @{username}...", text_color="orange")

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
        self.status_label.configure(text="○ Спряно", text_color="gray")

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
        self.status_label.configure(text="◌ Свързване към TikFinity...", text_color="orange")

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
                self._set_status(self.status_label, "● На живо (TikFinity)", OK_COLOR)

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
            self._set_btn(self.start_btn, "normal")
            self._set_btn(self.stop_btn, "disabled")

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
            self._set_status(self.status_label, f"● На живо: @{username}", OK_COLOR)

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
            self._set_btn(self.start_btn, "normal")
            self._set_btn(self.stop_btn, "disabled")


if __name__ == "__main__":
    app = App()
    app.mainloop()
