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
import re
import sys
import tempfile
import threading
import time
import traceback
from collections import deque
from pathlib import Path

import customtkinter as ctk
import pygame

from piper import PiperVoice
from piper.download_voices import download_voice

from TikTokLive import TikTokLiveClient
from TikTokLive.events import ConnectEvent, DisconnectEvent, CommentEvent, LiveEndEvent, GiftEvent
from TikTokLive.client.web.web_settings import WebDefaults

HEART_ME_GIFT_NAME = "heart me"  # сравнява се без главни/малки букви

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

        top = ctk.CTkFrame(self)
        top.pack(fill="x", **pad)
        self.direct_username_frame = top

        ctk.CTkLabel(top, text="TikTok потребителско име:").pack(side="left", padx=(0, 8))
        self.username_entry = ctk.CTkEntry(top, placeholder_text="напр. someusername (без @)")
        self.username_entry.pack(side="left", fill="x", expand=True)

        key_frame = ctk.CTkFrame(self)
        key_frame.pack(fill="x", **pad)
        self.direct_api_key_frame = key_frame
        ctk.CTkLabel(key_frame, text="Euler Stream API ключ (по избор, виж README):").pack(
            side="left", padx=(0, 8)
        )
        self.api_key_entry = ctk.CTkEntry(
            key_frame, placeholder_text="оставяш празно за безплатен общ лимит"
        )
        self.api_key_entry.pack(side="left", fill="x", expand=True)

        tikfinity_frame = ctk.CTkFrame(self)
        self.tikfinity_frame = tikfinity_frame
        ctk.CTkLabel(tikfinity_frame, text="TikFinity WebSocket адрес:").pack(side="left", padx=(0, 8))
        self.tikfinity_url_entry = ctk.CTkEntry(tikfinity_frame)
        self.tikfinity_url_entry.insert(0, "ws://localhost:21213/")
        self.tikfinity_url_entry.pack(side="left", fill="x", expand=True)
        self.tikfinity_note = ctk.CTkLabel(
            self,
            text="(Изисква пуснат и свързан TikFinity на компютъра ти)",
            text_color="gray",
        )

        btns = ctk.CTkFrame(self)
        btns.pack(fill="x", **pad)

        self.start_btn = ctk.CTkButton(btns, text="Старт", command=self.start_listening)
        self.start_btn.pack(side="left", padx=(0, 8))

        self.stop_btn = ctk.CTkButton(btns, text="Стоп", command=self.stop_listening, state="disabled")
        self.stop_btn.pack(side="left")

        self.status_label = ctk.CTkLabel(self, text="Подготовка на българския глас...", text_color="orange")
        self.status_label.pack(fill="x", padx=14)

        filt = ctk.CTkFrame(self)
        filt.pack(fill="x", **pad)

        self.filter_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(filt, text="Филтър на думи:", variable=self.filter_var).pack(side="left", padx=(0, 8))
        self.filter_entry = ctk.CTkEntry(
            filt, placeholder_text="дума1, дума2, дума3 (разделени със запетая)"
        )
        self.filter_entry.pack(side="left", fill="x", expand=True)

        extra = ctk.CTkFrame(self)
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

        maxlen = ctk.CTkFrame(self)
        maxlen.pack(fill="x", **pad)
        ctk.CTkLabel(maxlen, text="Макс. брой символи за четене:").pack(side="left", padx=(0, 8))
        self.max_chars_entry = ctk.CTkEntry(maxlen, width=80, placeholder_text="200")
        self.max_chars_entry.insert(0, "200")
        self.max_chars_entry.pack(side="left")

        ctk.CTkLabel(self, text="Лог на коментарите:").pack(anchor="w", padx=14)

        self.log_box = ctk.CTkTextbox(self, wrap="word")
        self.log_box.pack(fill="both", expand=True, padx=14, pady=(0, 14))
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

    def _truncate_to_max_chars(self, text: str) -> str:
        raw = self.max_chars_entry.get().strip()
        try:
            max_chars = int(raw) if raw else 200
        except ValueError:
            max_chars = 200
        if max_chars <= 0 or len(text) <= max_chars:
            return text
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

        speech_text = self._truncate_to_max_chars(comment)
        self._enqueue_latest_only(speech_text)

    def _register_heart_me_gift(self, nickname: str, user_key: str, gift_name: str):
        if (gift_name or "").strip().lower() == HEART_ME_GIFT_NAME and user_key:
            if user_key not in self.heart_me_senders:
                self.heart_me_senders.add(user_key)
                self._log(f"[Система] {nickname} прати Heart Me — вече е допустим.")

    # ------------------------------------------------------------------
    # Изговорчик (worker thread)
    # ------------------------------------------------------------------
    def _speaker_worker(self):
        while True:
            text = self.speech_queue.get()
            if self.voice is None:
                continue
            try:
                import wave

                fd, tmp_path = tempfile.mkstemp(suffix=".wav")
                os.close(fd)
                with wave.open(tmp_path, "wb") as wav_file:
                    self.voice.synthesize_wav(text, wav_file)

                sound = pygame.mixer.Sound(tmp_path)
                channel = sound.play()
                while channel.get_busy():
                    time.sleep(0.05)

                os.remove(tmp_path)
            except Exception as e:
                self._log(f"[Грешка при изговаряне] {e}")

    # ------------------------------------------------------------------
    # TikTok Live връзка
    # ------------------------------------------------------------------
    def start_listening(self):
        if self.voice is None:
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
            self._register_heart_me_gift(event.user.nickname, user_key, event.gift.name or "")

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
