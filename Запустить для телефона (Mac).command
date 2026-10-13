#!/bin/bash
cd "$(dirname "$0")"
# Сервер доступен и с телефона/планшета в той же Wi-Fi сети.
export POE2FLIP_HOST=0.0.0.0
exec python3 server.py
