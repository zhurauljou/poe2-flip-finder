@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

rem Программа лежит в папке app (в версии 1.0 — прямо здесь).
set "APP=app\server.py"
if not exist "%APP%" set "APP=server.py"
if not exist "%APP%" (
  echo [!] Не найдена папка app с программой рядом с этим файлом.
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

for /f "delims=" %%v in ('%PY% --version 2^>^&1') do echo Найден %%v
echo Запускаю программу... Это окно не закрывайте, пока пользуетесь программой.
echo.
%PY% "%APP%"
echo.
echo [!] Программа остановилась. Если выше есть сообщение об ошибке - сделайте скриншот этого окна.
pause
