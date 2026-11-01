@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist .env (
  echo Khong tim thay .env. Hay chay install-windows.bat truoc.
  pause
  exit /b 1
)
for /f "tokens=1,* delims==" %%A in ('findstr /b "TTS_API_KEY=" .env') do set "LOCAL_TTS_API_KEY=%%B"
echo API KEY RIENG CUA MAY NAY:
echo %LOCAL_TTS_API_KEY%
echo.
echo Chi dan key nay vao SẢNH TRUYỆN tren may cua ban.
echo Khong gui key cho nguoi khac, khong commit .env len GitHub.
pause
