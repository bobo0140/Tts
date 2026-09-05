"""
core_lib.py — чистата логика, без графичен интерфейс.
Ползва се и от tkinter версията (main.py), и от уеб версията (web_main.py).
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

APP_VERSION = "52"

# Кои табове се виждат при всеки профил. Скритите пак съществуват вътрешно,
# за да не се губят настройките им — просто не се показват.
PROFILES = {
    "Само TTS":     ["home", "filters", "voice", "test"],
    "Само Live AI": ["home", "filters", "ai", "test"],
    "Пълно":        ["home", "filters", "voice", "ai", "test"],
}

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


# Цветова палитра
ACCENT = "#7C5CFF"        # лилаво-синьо, основен акцент
ACCENT_HOVER = "#6A4AE8"
OK_COLOR = "#4ADE80"
WARN_COLOR = "#FBBF24"
ERR_COLOR = "#F87171"
MUTED = "#8B8B99"
CARD_BG = "#232330"
BAR_BG = "#1C1C26"



