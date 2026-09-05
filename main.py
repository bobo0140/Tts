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


PERSONALITIES = {
    "Балансиран": "Дръж се приятелски и естествено.",
    "Шегаджия": "Ти си голям шегаджия — почти всяка реплика има закачка или каламбур.",
    "Спокоен": "Говориш спокойно и топло, без излишна екзалтация.",
    "Енергичен": "Ти си много енергичен и възторжен, като спортен коментатор.",
    "Саркастичен": "Имаш сух, саркастичен хумор — закачаш добронамерено, без да обиждаш.",
    "Геймърски": "Говориш на геймърски жаргон и разбираш от игри.",
}


def mood_line(personality: str, humor: int, extra: str = "") -> str:
    """Сглобява добавка към промпта според избраното настроение."""
    parts = [PERSONALITIES.get(personality, PERSONALITIES["Балансиран"])]

    if humor <= 20:
        parts.append("Почти не се шегувай — бъди по-скоро информативен.")
    elif humor <= 45:
        parts.append("Шегувай се рядко, само когато е много подходящо.")
    elif humor <= 70:
        parts.append("Шегувай се умерено — в около половината от репликите.")
    elif humor <= 90:
        parts.append("Шегувай се често, почти във всяка реплика.")
    else:
        parts.append("Шегувай се максимално — всяка реплика да е закачка или майтап.")

    parts.append("Никога не обиждай и не се подигравай на хора.")

    if extra.strip():
        parts.append(extra.strip())

    return " " + " ".join(parts)


def streamer_line(streamer_name: str) -> str:
    """Добавка към промпта, с която AI-то знае как се казва стриймърът."""
    name = (streamer_name or "").strip()
    if not name:
        return ""
    return (
        f" Стриймърът, чийто лайв водиш, се казва {name}. "
        f"От време на време се обръщай към него по име ({name}) — например когато "
        "съобщаваш нови последователи или благодариш за подаръци. Не прекалявай: "
        "използвай името му от време на време, не във всяко изречение."
    )


class GeminiError(Exception):
    pass


def _parse_ws(raw):
    """Разчита съобщение от WebSocket — идва като bytes или str."""
    try:
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8", errors="replace")
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        return {}


class SetupRejected(Exception):
    """Сървърът отхвърли setup-а — пробваме с по-малко допълнителни полета."""
    pass


# Нивата се пробват отгоре надолу. Ако модел не поддържа някое поле, целият
# setup се отхвърля — затова смъкваме постепенно, вместо да гадаем.
SETUP_LEVELS = [
    "всичко включено",
    "без езиков код",
    "без засичане на говор",
    "без компресия и продължаване",
    "минимален",
]


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
    "gemini-2.5-flash-native-audio-latest",           # най-често достъпен
    "gemini-3.1-flash-live-preview",
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


def call_gemini(api_key: str, model: str, nickname: str, comment: str,
                streamer_name: str = "", mood: str = "", timeout: int = 15) -> str:
    """Праща коментар на Gemini и връща кратка AI реакция на български.
    Хвърля GeminiError с четимо съобщение при проблем."""
    if not api_key:
        raise GeminiError("Липсва Gemini API ключ.")
    if any(not (32 < ord(c) < 127) for c in api_key):
        raise GeminiError(
            "Ключът съдържа непозволени знаци (кирилица, интервал или нов ред). "
            "Изтрий полето и го постави наново."
        )

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
    prompt = (
        GEMINI_SYSTEM_PROMPT + mood + streamer_line(streamer_name)
        + f'\n\nПотребител "{nickname}" написа: "{comment}"'
    )
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
        self.live_resume_handle = None   # талон за продължаване на същата Live сесия
        self.live_send_stream_end = False
        self.live_setup_level = 0        # кое ниво на setup работи за този ключ/модел
        # Координация на ходовете — кой говори в момента
        # Статистика за сесията
        self.stat_follows = 0
        self.stat_shares = 0
        self.stat_gifts = 0
        self.stat_comments = 0
        self.stat_viewers = 0
        self.known_moderators = set()
        self.half_duplex = True            # обикновен флаг за аудио нишката
        self.live_model_speaking = False   # AI-то говори
        self.live_last_voice_ts = 0.0      # кога за последно се чу глас в микрофона
        self.muted = False               # заглушаване: спира звука, но НЕ къса връзките
        self.mic_active = False          # обикновен флаг — чете се от аудио нишката
        self.mic_chunks_sent = 0
        self.live_comment_counter = 0
        # Групиране на събития: буфер + ключалка, за да не правим заявка за всяко събитие
        self.live_event_buffer = []
        self.live_buffer_lock = threading.Lock()

        self._build_ui()
        self._enable_clipboard_everywhere()
        self._defaults = self._snapshot_settings()   # фабричните стойности
        self._load_settings()
        self.cfg = {}
        self._closing = False
        self._refresh_cfg()   # пълни кеша и се преизпълнява на всеки 300ms
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

        self.mute_status_label = ctk.CTkLabel(
            bar, text="", text_color=ERR_COLOR, font=ctk.CTkFont(size=13, weight="bold")
        )
        self.mute_status_label.pack(side="left", padx=(16, 0))

        self.stats_label = ctk.CTkLabel(
            bar, text="", text_color=MUTED, font=ctk.CTkFont(size=12)
        )
        self.stats_label.pack(side="left", padx=(20, 0))

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
        self.stop_btn.pack(side="right", padx=(0, 8))

        self.mute_btn = ctk.CTkButton(
            bar, text="🔊  Заглуши", width=130, height=36, corner_radius=8,
            fg_color="transparent", border_width=1, border_color="gray40",
            hover_color="gray25", command=self.toggle_mute,
        )
        self.mute_btn.pack(side="right", padx=(0, 8))

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
        ).pack(side="left", padx=(0, 8))
        ctk.CTkButton(
            btns, text="📋 Копирай лога", width=150, fg_color="gray30", hover_color="gray25",
            command=self._copy_log,
        ).pack(side="left", padx=(0, 8))
        ctk.CTkButton(
            btns, text="💾 Запази лога", width=140, fg_color="gray30", hover_color="gray25",
            command=self._save_log,
        ).pack(side="left", padx=(0, 8))
        ctk.CTkButton(
            btns, text="🔄 Нулирай статистиката", width=190, fg_color="gray30",
            hover_color="gray25", command=self._reset_stats,
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

        self.announce_follow_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(tab, text="Нови последователи", variable=self.announce_follow_var).pack(
            anchor="w", padx=8, pady=5
        )
        self.announce_share_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(tab, text="Споделяния (веднъж на човек)", variable=self.announce_share_var).pack(
            anchor="w", padx=8, pady=5
        )
        self.announce_gift_var = ctk.BooleanVar(value=True)
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

        row = self._row(tab, "Твоето име:")
        self.streamer_name_entry = ctk.CTkEntry(
            row, placeholder_text="напр. Пешо — с това име ще се обръща AI-то към теб"
        )
        self.streamer_name_entry.pack(side="left", fill="x", expand=True)
        self._hint(
            tab,
            "AI-то ще те заговаря по име от време на време — напр. 'Пешо, 10 нови "
            "последователи!'. Остави празно, ако не искаш.",
        )

        self._section(
            tab, "Характер на AI-то",
            "Важи и за текстовия коментатор, и за Live AI.",
        )

        row = self._row(tab, "Характер:")
        self.personality_menu = ctk.CTkOptionMenu(row, values=list(PERSONALITIES.keys()))
        self.personality_menu.set("Балансиран")
        self.personality_menu.pack(side="left", fill="x", expand=True)

        row = self._row(tab, "Колко да се шегува:")
        self.humor_label = ctk.CTkLabel(row, text="50%", width=55)
        self.humor_label.pack(side="right")
        self.humor_slider = ctk.CTkSlider(
            row, from_=0, to=100,
            command=lambda v: self.humor_label.configure(text=f"{int(float(v))}%"),
        )
        self.humor_slider.set(50)
        self.humor_slider.pack(side="left", fill="x", expand=True, padx=10)

        row = self._row(tab, "Свои инструкции:")
        self.custom_prompt_entry = ctk.CTkEntry(
            row, placeholder_text="напр. Играя Fortnite, споменавай това от време на време"
        )
        self.custom_prompt_entry.pack(side="left", fill="x", expand=True)

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

        row = self._row(tab, "Изход за звука на AI-то:", label_width=200)
        self.live_output_menu = ctk.CTkOptionMenu(row, values=["(по подразбиране)"])
        self.live_output_menu.set("(по подразбиране)")
        self.live_output_menu.pack(side="left", fill="x", expand=True, padx=(0, 8))
        ctk.CTkButton(row, text="Опресни", width=90, command=self._refresh_output_devices).pack(
            side="left", padx=(0, 8)
        )
        ctk.CTkButton(
            row, text="🔔 Тест", width=80, command=self._test_output_device
        ).pack(side="left")
        self._hint(
            tab,
            "Ако AI-то пише в лога, но не го чуваш, натисни '🔔 Тест' — пуска тон през "
            "избрания изход. Ако не чуеш тона, проблемът е в изхода, не в AI-то.",
        )

        row = self._row(tab, "Сила на звука на AI-то:", label_width=200)
        self.live_volume_label = ctk.CTkLabel(row, text="1.00x", width=55)
        self.live_volume_label.pack(side="right")
        self.live_volume_slider = ctk.CTkSlider(
            row, from_=0.05, to=2.5,
            command=lambda v: self.live_volume_label.configure(text=f"{float(v):.2f}x"),
        )
        self.live_volume_slider.set(1.0)
        self.live_volume_slider.pack(side="left", fill="x", expand=True, padx=10)
        self._hint(
            tab,
            "Регулира само гласа на Gemini. Плъзгачът в таб 'Глас' важи за "
            "Dimitar/Borislav/Kalina и не влияе тук.",
        )

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

        self.half_duplex_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(
            tab, text="Избягвай ехо (спира микрофона, докато AI-то говори)",
            variable=self.half_duplex_var,
        ).pack(anchor="w", padx=8, pady=5)
        self._hint(
            tab,
            "Ако слушаш през КОЛОНКИ, гласът на AI-то влиза обратно в микрофона, "
            "моделът чува сам себе си и се обърква. Тази отметка го предотвратява. "
            "Със слушалки може да я изключиш и да прекъсваш AI-то, докато говори.",
        )

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

        row = self._row(tab, "Клавиш за заглушаване:")
        self.mute_hotkey_entry = ctk.CTkEntry(row, width=110)
        self.mute_hotkey_entry.insert(0, "f9")
        self.mute_hotkey_entry.pack(side="left")
        self._hint(
            tab,
            "Заглушаването спира звука МОМЕНТАЛНО (и това, което се говори в момента), "
            "но НЕ къса връзката с TikTok и Live AI — те продължават да работят и "
            "коментарите се записват в лога. Активира се със същия бутон 'Активирай'.",
        )
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

        self._section(
            tab, "Диагностика на API",
            "Debug режимът показва суровите заявки и отговори към Gemini — това дава "
            "точната причина, вместо да гадаем.",
        )

        self.debug_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            tab, text="Debug режим (показвай сурови API заявки и отговори)",
            variable=self.debug_var,
        ).pack(anchor="w", padx=8, pady=6)

        ctk.CTkButton(
            tab, text="🔬  Пълна проверка на API (стъпка по стъпка)",
            command=self._test_api_full, height=38, fg_color=ACCENT, hover_color=ACCENT_HOVER,
            font=ctk.CTkFont(size=13, weight="bold"),
        ).pack(anchor="w", padx=8, pady=(4, 12))

        self._section(tab, "AI тестове", "Изискват Gemini ключ. Започни с проверката на връзката.")

        ai_grid = ctk.CTkFrame(tab, fg_color="transparent")
        ai_grid.pack(fill="x", padx=4, pady=(4, 6))
        ai_tests = [
            ("🔌 Тест връзка с Gemini", self._test_gemini_connection),
            ("🤖 Тест AI коментатор", self._test_ai_commentator),
            ("🎙 Тест микрофон (3 сек)", self._test_microphone),
            ("🔈 Тест на звука (тон)", self._test_audio_output),
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
            ("🔂 Серия подаръци (streak)", self._test_gift_streak),
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
        ctk.CTkButton(
            srow, text="↺ Върни фабричните настройки", command=self.restore_defaults,
            height=34, fg_color=ERR_COLOR, hover_color="#DC5555",
        ).pack(side="left")
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
        "gemini_api_key_entry", "streamer_name_entry", "custom_prompt_entry",
        "ai_every_n_entry", "live_every_n_entry",
        "live_batch_seconds_entry", "hotkey_entry", "mute_hotkey_entry",
        "test_name_entry",
        "test_comment_entry",
    ]
    SETTINGS_BOOLS = [
        "filter_var", "strip_mentions_var", "shlyokavitsa_var", "spam_filter_var",
        "heart_me_filter_var", "skip_instead_of_truncate_var", "voice_shuffle_var",
        "announce_follow_var", "announce_share_var", "announce_gift_var",
        "announce_viewers_var", "ai_enabled_var", "ai_speak_var",
        "live_feed_follow_var", "live_feed_share_var", "live_feed_gift_var",
        "live_feed_comment_var", "live_feed_viewers_var", "live_autoreconnect_var",
        "debug_var", "half_duplex_var",
    ]
    SETTINGS_MENUS = [
        "connection_mode", "output_mode", "voice_engine_menu", "voice_effect_menu",
        "gemini_model_entry", "ai_frequency_menu", "live_model_entry",
        "live_voice_menu", "mic_device_menu", "live_output_menu",
        "hotkey_mode_menu",
    ]
    SETTINGS_SLIDERS = [
        "speed_slider", "expressiveness_slider", "volume_slider", "live_volume_slider",
        "humor_slider",
    ]

    def _settings_path(self) -> Path:
        return BASE_DIR / "settings.json"

    def _snapshot_settings(self) -> dict:
        """Прочита текущото състояние на всички контроли."""
        data = {"entries": {}, "bools": {}, "menus": {}, "sliders": {}, "shuffle": {}}
        for name in self.SETTINGS_ENTRIES:
            w = getattr(self, name, None)
            if w is not None:
                try:
                    data["entries"][name] = w.get()
                except Exception:
                    pass
        for name in self.SETTINGS_BOOLS:
            v = getattr(self, name, None)
            if v is not None:
                try:
                    data["bools"][name] = bool(v.get())
                except Exception:
                    pass
        for name in self.SETTINGS_MENUS:
            w = getattr(self, name, None)
            if w is not None:
                try:
                    data["menus"][name] = w.get()
                except Exception:
                    pass
        for name in self.SETTINGS_SLIDERS:
            w = getattr(self, name, None)
            if w is not None:
                try:
                    data["sliders"][name] = float(w.get())
                except Exception:
                    pass
        for key, var in getattr(self, "shuffle_vars", {}).items():
            try:
                data["shuffle"][key] = bool(var.get())
            except Exception:
                pass
        return data

    def _apply_settings(self, data: dict):
        """Прилага подаден набор настройки върху контролите."""
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

        # прилагаме режима на свързване и обновяваме етикетите на плъзгачите
        try:
            self._on_connection_mode_changed(self.connection_mode.get())
        except Exception:
            pass
        try:
            self.live_volume_label.configure(text=f"{self.live_volume_slider.get():.2f}x")
        except Exception:
            pass

    def restore_defaults(self):
        """Връща всичко към фабричните настройки и трие запазения файл."""
        defaults = getattr(self, "_defaults", None)
        if not defaults:
            self._log("[Настройки] Няма запазени фабрични стойности.")
            return

        # спираме всичко активно, за да няма изненади
        try:
            if self.live_running:
                self.stop_live_ai()
            if self.is_running:
                self.stop_listening()
        except Exception:
            pass

        self.set_muted(False)
        self._apply_settings(defaults)

        # чистим и натрупаното състояние от сесията
        self.announced_sharers.clear()
        self.heart_me_senders.clear()
        self.confirmed_subscribers.clear()
        self.recent_comments.clear()
        with self.live_buffer_lock:
            self.live_event_buffer.clear()
        self.ai_comment_counter = 0
        self.live_comment_counter = 0
        self.live_resume_handle = None
        self.last_viewer_announcement_time = 0.0
        self.stat_follows = self.stat_shares = self.stat_gifts = 0
        self.stat_comments = self.stat_viewers = 0
        self.known_moderators.clear()

        try:
            path = self._settings_path()
            if path.exists():
                path.unlink()
        except Exception as e:
            self._log(f"[Настройки] Не можах да изтрия файла: {e}")

        self._log(
            "[Настройки] ✓ Върнати към фабричните. Всичко е чисто. "
            "ВНИМАНИЕ: API ключовете също са изтрити — въведи ги наново."
        )

    def _save_settings(self, silent: bool = False):
        try:
            data = self._snapshot_settings()
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

        self._apply_settings(data)
        self._log("[Настройки] Заредени от предишния път.")

        try:
            v = float(self.volume_slider.get())
            if v < 0.3:
                self._log(f"[Внимание] Силата на TTS гласа е много ниска ({v:.2f}x) — таб 'Глас'.")
            lv = float(self.live_volume_slider.get())
            if lv < 0.3:
                self._log(f"[Внимание] Силата на Live AI е много ниска ({lv:.2f}x) — таб 'AI'.")
        except Exception:
            pass

    def _autosave_settings(self):
        if getattr(self, "_closing", False):
            return
        self._save_settings(silent=True)
        self.after(60000, self._autosave_settings)

    def _on_close(self):
        self._closing = True     # спира периодичните задачи, преди да махнем прозореца
        self._save_settings(silent=True)
        try:
            self._unregister_hotkey()
        except Exception:
            pass
        self.destroy()

    # ==================================================================
    # Кеш на настройките (за фоновите процеси)
    # ==================================================================
    # Tkinter не е thread-safe — четенето на поле от фонов процес може да хвърли
    # "main thread is not in main loop". Затова главната нишка обновява този кеш,
    # а работните нишки четат само от него.
    def _refresh_cfg(self):
        if getattr(self, "_closing", False):
            return
        try:
            self.cfg = {
                "gemini_key": self.gemini_api_key_entry.get().strip(),
                "gemini_model": self.gemini_model_entry.get().strip() or TEXT_MODELS[0],
                "streamer_name": self.streamer_name_entry.get().strip(),
                "ai_speak": self.ai_speak_var.get(),
                "batch_seconds": self.live_batch_seconds_entry.get().strip(),
                "speed": float(self.speed_slider.get()),
                "expressiveness": float(self.expressiveness_slider.get()),
                "volume": float(self.volume_slider.get()),
                "effect": self.voice_effect_menu.get(),
                "voice_label": self.voice_engine_menu.get(),
                "shuffle": self.voice_shuffle_var.get(),
                "shuffle_pool": [k for k, v in self.shuffle_vars.items() if v.get()],
                "output_mode": self.output_mode.get(),
                "live_volume": float(self.live_volume_slider.get()),
                "debug": bool(self.debug_var.get()),
                "half_duplex": bool(self.half_duplex_var.get()),
                "personality": self.personality_menu.get(),
                "humor": int(self.humor_slider.get()),
                "custom_prompt": self.custom_prompt_entry.get().strip(),
            }
            self.half_duplex = self.cfg["half_duplex"]
            self.stats_label.configure(
                text=(
                    f"👥 {self.stat_viewers}   ➕ {self.stat_follows}   "
                    f"🔁 {self.stat_shares}   🎁 {self.stat_gifts}   💬 {self.stat_comments}"
                )
            )
        except Exception:
            pass  # прозорецът се затваря — кешът остава последно известния
        self.after(300, self._refresh_cfg)

    def _cfg(self, key, default=None):
        return getattr(self, "cfg", {}).get(key, default)

    # ------------------------------------------------------------------
    # Заглушаване (спира звука, но НЕ къса нищо)
    # ------------------------------------------------------------------
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

    def _refresh_mute_ui(self):
        try:
            if self.muted:
                self.mute_btn.configure(
                    text="🔇  Заглушено", fg_color=ERR_COLOR, hover_color="#DC5555"
                )
                self.mute_status_label.configure(text="🔇 ЗАГЛУШЕНО", text_color=ERR_COLOR)
            else:
                self.mute_btn.configure(
                    text="🔊  Заглуши", fg_color="transparent", hover_color="gray25"
                )
                self.mute_status_label.configure(text="")
        except Exception:
            pass

    def _copy_log(self):
        try:
            text = self.log_box.get("1.0", "end").strip()
            if not text:
                self._log("[Лог] Няма нищо за копиране.")
                return
            self.clipboard_clear()
            self.clipboard_append(text)
            self._log(f"[Лог] Копиран в клипборда ({len(text)} знака) — може да го поставиш.")
        except Exception as e:
            self._log(f"[Лог] Грешка при копиране: {e}")

    def _save_log(self):
        try:
            text = self.log_box.get("1.0", "end").strip()
            if not text:
                self._log("[Лог] Няма нищо за запазване.")
                return
            path = BASE_DIR / f"log-{time.strftime('%Y%m%d-%H%M%S')}.txt"
            path.write_text(text, encoding="utf-8")
            self._log(f"[Лог] Запазен във {path.name} (до приложението).")
        except Exception as e:
            self._log(f"[Лог] Грешка при запазване: {e}")

    def _dbg(self, direction: str, payload):
        """Логва сурова API заявка/отговор, ако debug режимът е включен.
        direction: '→' (изпратено) или '←' (получено)."""
        # чете се от кеша, защото се вика и от фонови нишки
        if not self._cfg("debug", False):
            return
        try:
            text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        except Exception:
            text = str(payload)
        limit = 400
        if len(text) > limit:
            text = text[:limit] + f"… (+{len(text) - limit} знака)"
        self._log(f"  [DEBUG {direction}] {text}")

    def _reset_stats(self):
        self.stat_follows = self.stat_shares = self.stat_gifts = 0
        self.stat_comments = self.stat_viewers = 0
        self._log("[Статистика] Нулирана.")

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
        if getattr(self, "_closing", False):
            return
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
                interval = max(2, int(self._cfg("batch_seconds") or 8))
            except (ValueError, TypeError):
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

        parts.append(
            f"(Общо тази сесия: {self.stat_follows} нови последователи, "
            f"{self.stat_shares} споделяния, {self.stat_gifts} подаръка.)"
        )

        return (
            " ".join(parts)
            + " Реагирай общо и кратко на всичко това наведнъж, в едно изказване."
        )

    def _on_mic_toggle(self):
        if self.live_mic_var.get():
            self._start_mic()
        else:
            self._stop_mic()

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

    def _get_selected_output_device(self):
        label = self.live_output_menu.get()
        if label == "(по подразбиране)":
            return None
        return getattr(self, "output_device_map", {}).get(label)

    def _test_output_device(self):
        """Пуска кратък тон през избрания изход — за да разберем дали
        проблемът е в звуковия път или в AI-то."""
        self._log("--- ТЕСТ: изход за звука на AI-то ---")

        def worker():
            try:
                import sounddevice as sd
            except Exception as e:
                self._log(f"[Изход] sounddevice не е достъпен: {e}")
                return
            try:
                dur, rate = 1.0, LIVE_OUTPUT_RATE
                t = np.arange(int(dur * rate), dtype=np.float32) / rate
                gain = float(self._cfg("live_volume", 1.0))
                tone = (np.sin(2 * np.pi * 440 * t) * 8000 * gain)
                tone = np.clip(tone, -32768, 32767).astype(np.int16)

                device = self._get_selected_output_device()
                stream = sd.RawOutputStream(
                    samplerate=rate, dtype="int16", channels=1, device=device
                )
                stream.start()
                stream.write(tone.tobytes())
                stream.stop()
                stream.close()
                self._log(
                    f"[Изход] Тонът е изпратен (сила {gain:.2f}x). Ако НЕ го чу — "
                    "избери друго устройство от списъка и пробвай пак."
                )
            except Exception as e:
                self._log(f"[Изход] ГРЕШКА при пускане на звук: {e}")

        threading.Thread(target=worker, daemon=True).start()

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
        self.mic_device_menu.configure(values=names)
        if current not in names:
            self.mic_device_menu.set("(по подразбиране)")
        self._log(f"[Микрофон] {len(names) - 1} микрофона (дубликатите са премахнати).")

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

        # клавиш за заглушаване (по избор, отделен от този за микрофона)
        mute_combo = self.mute_hotkey_entry.get().strip()
        if mute_combo:
            try:
                keyboard.add_hotkey(mute_combo, self.toggle_mute)
                self._log(f"[Клавиш] '{mute_combo}' заглушава/пуска звука.")
            except Exception as e:
                self._log(f"[Клавиш] Не мога да регистрирам '{mute_combo}': {e}")

        self.hotkey_active = True
        self.hotkey_combo = combo
        self.hotkey_btn.configure(text="Изключи")
        mode_text = "задържане" if hold_mode else "превключване"
        self._log(f"[Клавиш] '{combo}' е активен за микрофона ({mode_text}).")

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
                        self.custom_prompt_entry.get().strip(),
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

    def _run_live_client(self, api_key: str):
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
            while self.live_running:
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

        async def receiver(ws, out_stream):
            """Получава аудио от AI-то и го пуска през говорителите."""
            async for raw in ws:
                if not self.live_running:
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
                    self._log("[Live AI] Прекъснат (заговорил си докато AI-то говори).")
                if server_content.get("turnComplete"):
                    self.live_model_speaking = False
                    self._log("[Live AI] Ходът е завършен.")

                in_tr = (server_content.get("inputTranscription") or {}).get("text")
                if in_tr:
                    self._log(f"[Чух те] {in_tr}")
                out_tr = (server_content.get("outputTranscription") or {}).get("text")
                if out_tr:
                    self._log(f"[AI казва] {out_tr}")

                model_turn = server_content.get("modelTurn") or {}
                for part in model_turn.get("parts", []):
                    inline = part.get("inlineData") or {}
                    data_b64 = inline.get("data")
                    if data_b64:
                        self.live_model_speaking = True
                    if data_b64 and out_stream is not None and not self.muted:
                        try:
                            pcm = base64.b64decode(data_b64)
                            gain = float(self._cfg("live_volume", 1.0))
                            if abs(gain - 1.0) > 0.01:
                                samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
                                samples = np.clip(samples * gain, -32768, 32767)
                                pcm = samples.astype(np.int16).tobytes()
                            out_stream.write(pcm)
                        except Exception:
                            pass
                    text = part.get("text")
                    if text:
                        self._log(f"[Live AI] {text}")

        async def run(level):
            out_stream = None
            try:
                import sounddevice as sd
                out_stream = sd.RawOutputStream(
                    samplerate=LIVE_OUTPUT_RATE, dtype="int16", channels=1,
                    device=self._get_selected_output_device(),
                )
                out_stream.start()
                self._log("[Live AI] Аудио изходът е отворен.")
            except Exception as e:
                self._log(
                    f"[Live AI] НЯМА ИЗХОД ЗА ЗВУК ({e}) — ще виждаш само текста. "
                    "Пробвай друго устройство от 'Изход за звука на AI-то'."
                )

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
                self._log("[Live AI] Свързан и готов. Пробвай да кажеш нещо или пусни тест.")
                self._set_status(self.live_status_label, "● Live AI активен", OK_COLOR)

                await asyncio.gather(sender(ws), receiver(ws, out_stream))

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
            self.live_ws = None
            self._stop_mic()

            try:
                lasted = round(time.time() - session_started)
                self._log(f"[Live AI] Сесията издържа {lasted} сек. ({round(lasted/60, 1)} мин.)")
            except Exception:
                pass

            # Автоматично пресвързване, ако връзката е паднала сама
            if self.live_running and self.live_autoreconnect_var.get():
                if self.live_resume_handle:
                    self._log("[Live AI] Пресвързвам се и продължавам сесията...")
                else:
                    self._log("[Live AI] Връзката падна — пресвързвам се...")
                self._set_status(self.live_status_label, "◌ Live AI пресвързване...", WARN_COLOR)
                time.sleep(1.5)
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
            api_key = self._cfg("gemini_key", "")
            model = self._cfg("gemini_model", TEXT_MODELS[0])
            try:
                self._dbg("→", {"model": model, "user": nickname, "comment": comment})
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
                if reply:
                    self._log(f"[AI] {reply}")
                    if self._cfg("ai_speak", True):
                        self._enqueue_latest_only(reply, source="ai")
            except GeminiError as e:
                self._log(f"[AI грешка] {e}")

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
            self._set_status(self.status_label, f"● На живо: @{username}", OK_COLOR)

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
            self._set_btn(self.start_btn, "normal")
            self._set_btn(self.stop_btn, "disabled")


if __name__ == "__main__":
    app = App()
    app.mainloop()
