@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ========================================
echo       CAI DAT STORY TTS API
echo ========================================
where pymanager >nul 2>nul
if not errorlevel 1 (
  echo Su dung Python Install Manager de tao moi truong Python 3.11...
  pymanager exec -V:3.11 -m venv .venv
) else (
  where py >nul 2>nul
  if errorlevel 1 (
    echo Khong tim thay Python Install Manager hoac Python Launcher.
    echo Hay cai Python Install Manager va runtime 3.11 truoc.
    pause
    exit /b 1
  )
  py -3.11 -m venv .venv
)
if errorlevel 1 (
  echo Khong tao duoc moi truong Python 3.11. Kiem tra lai Python.
  pause
  exit /b 1
)
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
if errorlevel 1 (
  echo Cai thu vien that bai.
  pause
  exit /b 1
)
if not exist .env (
  python -c "import secrets; from pathlib import Path; Path('.env').write_text('TTS_API_KEY='+secrets.token_urlsafe(48)+'\nAUDIO_URL_SECRET='+secrets.token_urlsafe(48)+'\nTTS_DATA_DIR=./data\nTTS_CHUNK_CHAR_LIMIT=5000\nMAX_PARALLEL_CHUNKS=100\nMAX_INPUT_CHARACTERS=5000000\nMAX_QUEUED_JOBS=50\nAUDIO_URL_TTL_SECONDS=86400\n',encoding='utf-8')"
  echo Da tao .env va tu sinh API key rieng tren may nay.
)
echo.
for /f "tokens=1,* delims==" %%A in ('findstr /b "TTS_API_KEY=" .env') do set "LOCAL_TTS_API_KEY=%%B"
echo ========================================
echo API KEY RIENG CUA MAY NAY - HAY COPY VAO SANH TRUYEN:
echo %LOCAL_TTS_API_KEY%
echo Khong gui key nay cho nguoi khac va khong commit file .env.
echo ========================================
echo Cai dat xong. Chay run-windows.bat de mo Story TTS API.
pause
