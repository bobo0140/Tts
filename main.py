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
from pathlib import Path

import customtkinter as ctk
import pygame

from piper import PiperVoice
from piper.download_voices import download_voice

from TikTokLive import TikTokLiveClient
from TikTokLive.events import ConnectEvent, DisconnectEvent, CommentEvent, LiveEndEvent

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
        self.is_running = False

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

        top = ctk.CTkFrame(self)
        top.pack(fill="x", **pad)

        ctk.CTkLabel(top, text="TikTok потребителско име:").pack(side="left", padx=(0, 8))
        self.username_entry = ctk.CTkEntry(top, placeholder_text="напр. someusername (без @)")
        self.username_entry.pack(side="left", fill="x", expand=True)

        btns = ctk.CTkFrame(self)
        btns.pack(fill="x", **pad)

        self.start_btn = ctk.CTkButton(btns, text="Старт", command=self.start_listening)
        self.start_btn.pack(side="left", padx=(0, 8))

        self.stop_btn = ctk.CTkButton(btns, text="Стоп", command=self.stop_listening, state="disabled")
        self.stop_btn.pack(side="left")

        self.status_label = ctk.CTkLabel(self, text="Подготовка на българския глас...", text_color="orange")
        self.status_label.pack(fill="x", padx=14)

        opts = ctk.CTkFrame(self)
        opts.pack(fill="x", **pad)

        self.read_username_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(
            opts, text="Чети и потребителското име преди коментара",
            variable=self.read_username_var
        ).pack(side="left")

        filt = ctk.CTkFrame(self)
        filt.pack(fill="x", **pad)

        self.filter_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(filt, text="Филтър на думи:", variable=self.filter_var).pack(side="left", padx=(0, 8))
        self.filter_entry = ctk.CTkEntry(
            filt, placeholder_text="дума1, дума2, дума3 (разделени със запетая)"
        )
        self.filter_entry.pack(side="left", fill="x", expand=True)

        ctk.CTkLabel(self, text="Лог на коментарите:").pack(anchor="w", padx=14)

        self.log_box = ctk.CTkTextbox(self, wrap="word")
        self.log_box.pack(fill="both", expand=True, padx=14, pady=(0, 14))
        self.log_box.configure(state="disabled")

    def _log(self, msg: str):
        self.log_queue.put(msg)

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

        username = self.username_entry.get().strip().lstrip("@")
        if not username:
            self._log("[Система] Въведи TikTok потребителско име.")
            return

        self.is_running = True
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.status_label.configure(text=f"Свързване към @{username} ...", text_color="orange")

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

        self.start_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        self.status_label.configure(text="Спряно.", text_color="gray")

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
            comment = clean_text_for_speech(event.comment or "")
            if not comment:
                return

            self._log(f"{nickname}: {comment}")

            if self._is_filtered(comment):
                self._log("   -> [филтрирано, не се изговаря]")
                return

            speech_text = f"{nickname} каза: {comment}" if self.read_username_var.get() else comment
            self.speech_queue.put(speech_text)

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
            traceback.print_exc()
        finally:
            self.is_running = False
            self.start_btn.configure(state="normal")
            self.stop_btn.configure(state="disabled")


if __name__ == "__main__":
    app = App()
    app.mainloop()
