@echo off
REM ============================================================
REM  Build script - creates TikTokTTS.exe from main.py
REM  Run this ON WINDOWS, with Python 3.10-3.12 installed.
REM  Double-click this file, or run it from cmd.
REM ============================================================

echo [1/4] Creating virtual environment...
python -m venv build_venv
call build_venv\Scripts\activate.bat

echo [2/4] Installing dependencies (this can take a few minutes)...
pip install --upgrade pip
pip install -r requirements.txt

echo [3/4] Building .exe with PyInstaller...
pyinstaller --noconfirm --onefile --windowed --name TikTokTTS ^
    --collect-all customtkinter ^
    --collect-data piper ^
    --collect-submodules piper ^
    --collect-data certifi ^
    --collect-all edge_tts ^
    --collect-all TikTokLive ^
    --collect-all sounddevice ^
    main.py

echo [4/4] Done!
echo.
echo Your app is at: dist\TikTokTTS.exe
echo First run will download the Bulgarian voice (~60 MB) - needs internet.
echo.
pause
