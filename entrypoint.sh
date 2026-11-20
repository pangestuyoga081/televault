#!/bin/bash
python -u full_crawler.py &
exec python server.py
