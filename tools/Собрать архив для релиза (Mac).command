#!/bin/bash
cd "$(dirname "$0")/.."
python3 app/server.py --pack
echo
echo "Файл poe2-flip.zip — в корне папки программы. Прикрепи его к релизу на GitHub (см. RELEASING.md)."
read -n 1 -s -r -p "Нажми любую клавишу, чтобы закрыть..."
