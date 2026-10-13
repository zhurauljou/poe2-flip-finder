@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

if not exist "server.py" (
  echo [!] Не найден server.py рядом с этим файлом.
  echo     Скорее всего, программа запущена прямо из архива.
  echo     Закройте окно, нажмите на архив правой кнопкой - "Извлечь все",
  echo     и запускайте этот файл уже из распакованной папки.
  echo.
  pause
  exit /b 1
)

set "PY="
py -3 --version >nul 2>&1 && set "PY=py -3"
if not defined PY (
  python --version >nul 2>&1 && set "PY=python"
)
if not defined PY (
  echo [!] Не найден Python - без него программа не работает.
  echo     1. Сейчас откроется сайт python.org - скачайте и запустите установщик.
  echo     2. ВАЖНО: на первом экране установщика отметьте галочку "Add python.exe to PATH".
  echo     3. После установки запустите этот файл снова.
  echo.
  start "" "https://www.python.org/downloads/"
  pause
  exit /b 1
)

echo Собираю архив для передачи другому человеку...
%PY% server.py --pack
echo.
echo Отправляй файл poe2-flip.zip из этой папки. Твои журнал сделок, токен бота и подписчики в архив не попадают.
pause
