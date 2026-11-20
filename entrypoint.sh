#!/bin/bash
# Prioritas tertinggi: Web Streaming & Uraian Informasi (FastAPI Server)
python server.py &
SERVER_PID=$!

# Prioritas latar belakang (nice 19 = idle CPU): Auto-Crawler pengarsipan
sleep 2
nice -n 19 python -u full_crawler.py &

# Tunggu server utama
wait $SERVER_PID
