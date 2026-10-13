#!/bin/bash
cd "$(dirname "$0")"
# Программа лежит в папке app (в версии 1.0 — прямо здесь).
if [ -f app/server.py ]; then exec python3 app/server.py; else exec python3 server.py; fi
