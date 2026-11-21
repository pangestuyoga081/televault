#!/bin/bash
# Prioritas tertinggi: Web Streaming & Uraian Informasi (FastAPI Server)
python server.py &
SERVER_PID=$!

# Prioritas latar belakang: Real-time Telegram Listener & Auto-Resolver Sync
sleep 2
nice -n 19 python -u sync_manager.py &

# Tunggu server utama
wait $SERVER_PID
