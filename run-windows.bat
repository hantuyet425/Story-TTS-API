@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist .venv\Scripts\activate.bat (
  echo Chua cai dat. Hay chay install-windows.bat truoc.
  pause
  exit /b 1
)
if not exist .env (
  echo Khong tim thay .env. Hay chay install-windows.bat truoc.
  pause
  exit /b 1
)
call .venv\Scripts\activate.bat
echo Story TTS API dang chay tai http://127.0.0.1:8000
echo Giu cua so nay mo trong suot qua trinh tao audio.
echo Nhan Ctrl+C de dung dich vu.
python main.py
pause
