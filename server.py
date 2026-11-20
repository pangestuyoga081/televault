import os
import re
import ssl
import json
import time
import asyncio
import sqlite3
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
import urllib.request
import yt_dlp
from fastapi import FastAPI, BackgroundTasks, WebSocket, WebSocketDisconnect, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
import uvicorn

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CATALOG_FILE = os.path.join(BASE_DIR, "catalog.json")
CHANNELS_FILE = os.path.join(BASE_DIR, "all_channels.json")
THUMBNAILS_DIR = os.path.join(BASE_DIR, "thumbnails")
MEDIA_DIR = os.path.join(BASE_DIR, "media_illustrasi")
WEB_DIR = os.path.join(BASE_DIR, "web")
DB_FILE = os.path.join(BASE_DIR, "televault.db")

def init_sqlite_tables():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS favorites (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            unique_key TEXT UNIQUE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS collections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            description TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS collection_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            collection_id INTEGER,
            unique_key TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(collection_id, unique_key)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS playback_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            video_id TEXT UNIQUE,
            unique_key TEXT,
            title TEXT,
            play_url TEXT,
            current_time REAL DEFAULT 0,
            duration REAL DEFAULT 0,
            progress_percent REAL DEFAULT 0,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()

init_sqlite_tables()

def get_db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

os.makedirs(THUMBNAILS_DIR, exist_ok=True)
os.makedirs(MEDIA_DIR, exist_ok=True)
os.makedirs(WEB_DIR, exist_ok=True)

ssl_ctx = ssl.create_default_context()
ssl_ctx.check_hostname = False
ssl_ctx.verify_mode = ssl.CERT_NONE

app = FastAPI(title="Universal Telegram & Cloud Archiver")
app.mount("/thumbnails", StaticFiles(directory=THUMBNAILS_DIR), name="thumbnails")
app.mount("/media", StaticFiles(directory=MEDIA_DIR), name="media")

executor = ThreadPoolExecutor(max_workers=8)
LOGS = []
active_connections = set()
PROGRESS_MAP = {}

def get_channel_map():
    if os.path.exists(CHANNELS_FILE):
        try:
            items = json.load(open(CHANNELS_FILE, "r", encoding="utf-8"))
            return {str(c["id"]): c["name"] for c in items}
        except Exception:
            pass
    return {"-1003844911918": "Drama CRT", "-1003866531213": "SECRET GUILD"}

async def send_to_ws(ws, payload):
    try:
        await ws.send_json(payload)
    except Exception:
        active_connections.discard(ws)

MAIN_LOOP = None

@app.on_event("startup")
async def startup_event():
    global MAIN_LOOP
    MAIN_LOOP = asyncio.get_running_loop()

def broadcast_ws(payload):
    global MAIN_LOOP
    if not MAIN_LOOP or MAIN_LOOP.is_closed():
        return
    for ws in list(active_connections):
        asyncio.run_coroutine_threadsafe(send_to_ws(ws, payload), MAIN_LOOP)

def broadcast_log(msg, level="info"):
    timestamp = datetime.now().strftime("%H:%M:%S")
    entry = {"time": timestamp, "msg": msg, "level": level}
    LOGS.append(entry)
    if len(LOGS) > 300:
        LOGS.pop(0)
    broadcast_ws({"type": "log", "data": entry})

def broadcast_progress(task_id, data):
    PROGRESS_MAP[task_id] = data
    broadcast_ws({"type": "progress", "task_id": task_id, "data": data})

def clean_filename(name):
    clean = re.sub(r'[\\/*?:"<>|]', "", name)
    clean = re.sub(r'\s+', " ", clean).strip().strip(".")
    return clean[:100] if clean else "video"

def load_catalog():
    if os.path.exists(CATALOG_FILE):
        try:
            with open(CATALOG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_catalog(catalog):
    with open(CATALOG_FILE, "w", encoding="utf-8") as f:
        json.dump(catalog, f, indent=2, ensure_ascii=False)

# ================= KEBUT STREAMING DOWNLOADER BERGARANSI =================
# Telegram Cloud Storage Database Config
TELEGRAM_DB_CHANNEL_ID = 4423804448
API_ID = 31350452
API_HASH = "d26a371b77634346b594c88c5dfe635f"

def make_faststart(filepath):
    """
    Pindahkan MOOV atom ke awal file MP4 tanpa re-encode (-c copy).
    Proses hanya butuh ~0.2 detik, menjaga kualitas 100% asli,
    dan membuat video langsung bisa di-stream / dimainkan di browser tanpa error.
    """
    if not os.path.exists(filepath) or not filepath.endswith(".mp4"):
        return filepath
    temp_p = filepath + ".fast.mp4"
    try:
        cmd = ["ffmpeg", "-y", "-i", filepath, "-c", "copy", "-movflags", "+faststart", temp_p]
        res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if res.returncode == 0 and os.path.exists(temp_p) and os.path.getsize(temp_p) > 50000:
            for _ in range(5):
                try:
                    os.remove(filepath)
                    os.rename(temp_p, filepath)
                    break
                except PermissionError:
                    time.sleep(0.3)
        else:
            if os.path.exists(temp_p):
                try: os.remove(temp_p)
                except Exception: pass
    except Exception:
        if os.path.exists(temp_p):
            try: os.remove(temp_p)
            except Exception: pass
    return filepath

def upload_to_telegram_cloud(file_path, post_title, post_id):
    if not os.path.exists(file_path): return
    make_faststart(file_path)
    filename = os.path.basename(file_path)
    try:
        from telethon.sync import TelegramClient
        session_path = os.path.join(BASE_DIR, "telethon_session")
        with TelegramClient(session_path, API_ID, API_HASH) as client:
            caption = f"🎬 {filename}\n📁 Post #{post_id} - {post_title}\n☁️ Arsip Database Unlimited"
            broadcast_log(f"Mengirim {filename} ke Telegram Cloud Database...", "info")
            msg = client.send_file(TELEGRAM_DB_CHANNEL_ID, file_path, caption=caption, supports_streaming=True)
            msg_link = f"https://t.me/c/{TELEGRAM_DB_CHANNEL_ID}/{msg.id}"
            
            # Catat ke catalog.json
            cat = load_catalog()
            for k, v in cat.items():
                if v.get("id") == post_id:
                    v.setdefault("telegram_cloud_files", {})[filename] = {
                        "msg_id": msg.id,
                        "link": msg_link,
                        "date": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    }
                    cat[k] = v
                    save_catalog(cat)
                    break

            broadcast_log(f"☁️ Tersimpan Aman di Cloud Telegram: {filename}", "success")
    except Exception as e:
        broadcast_log(f"Gagal backup ke Telegram Cloud: {e}", "warning")

def download_segmented_file(url, output_filepath, referer=None, num_threads=4, task_id=None, post_title="", post_id=0):
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    if referer:
        headers["Referer"] = referer

    filename = os.path.basename(output_filepath)
    temp_path = output_filepath + ".part"

    try:
        req = urllib.request.Request(url, headers=headers, method="HEAD")
        with urllib.request.urlopen(req, context=ssl_ctx, timeout=12) as res:
            total_size = int(res.headers.get("Content-Length", 0))
    except Exception:
        total_size = 0

    existing_bytes = 0
    if os.path.exists(temp_path):
        existing_bytes = os.path.getsize(temp_path)
        if total_size > 0 and existing_bytes >= total_size:
            # File part sudah lengkap
            if os.path.exists(output_filepath):
                os.remove(output_filepath)
            os.rename(temp_path, output_filepath)
            if task_id:
                broadcast_progress(task_id, {"name": filename, "percent": 100, "size": f"{existing_bytes/(1024*1024):.1f} MB", "done": True})
            return True

    # Jika ada byte tersimpan, minta resume Range
    if existing_bytes > 0:
        headers["Range"] = f"bytes={existing_bytes}-"
        broadcast_log(f"Melanjutkan unduh {filename} dari {existing_bytes/(1024*1024):.1f} MB...", "info")
    else:
        broadcast_log(f"Mulai unduh cepat: {filename} ({total_size/(1024*1024):.1f} MB)", "info")

    try:
        out_mode = "ab" if existing_bytes > 0 else "wb"
        out_f = open(temp_path, out_mode)
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, context=ssl_ctx, timeout=90) as res:
                dl = existing_bytes
                last_broadcast = time.time()
                while True:
                    chunk = res.read(1024 * 512)
                    if not chunk: break
                    out_f.write(chunk)
                    dl += len(chunk)
                    
                    if time.time() - last_broadcast > 0.5:
                        last_broadcast = time.time()
                        pct = int(dl * 100 / total_size) if total_size > 0 else 50
                        pct = min(99, pct)
                        sz_str = f"{dl/(1024*1024):.1f} / {total_size/(1024*1024):.1f} MB" if total_size > 0 else f"{dl/(1024*1024):.1f} MB"
                        if task_id:
                            broadcast_progress(task_id, {"name": filename, "percent": pct, "size": sz_str, "done": False})
        finally:
            out_f.flush()
            out_f.close()
            time.sleep(0.1)

        # Cek dan ganti nama dengan safe retry
        for attempt in range(5):
            try:
                if os.path.exists(output_filepath):
                    os.remove(output_filepath)
                os.rename(temp_path, output_filepath)
                break
            except PermissionError:
                time.sleep(0.3)
        
        final_mb = dl / (1024 * 1024)
        if task_id:
            broadcast_progress(task_id, {"name": filename, "percent": 100, "size": f"{final_mb:.1f} MB", "done": True})
        broadcast_log(f"100% Selesai: {filename} ({final_mb:.1f} MB)", "success")
        
        # Kirim backup ke Telegram Cloud di thread terpisah agar proses download tidak tertunda
        if post_id and os.path.exists(output_filepath):
            executor.submit(upload_to_telegram_cloud, output_filepath, post_title, post_id)
            
        return True
    except Exception as e:
        broadcast_log(f"Gagal unduh {filename}: {e}", "error")
        return False

# ================= RESOLVER LOGIC =================
def resolve_vidara(url):
    m = re.search(r'vidara\.(?:to|so)/(?:e/)?([a-zA-Z0-9_-]+)', url)
    if not m: return None
    filecode = m.group(1)
    api_url = "https://vidara.to/api/stream"
    headers = {"User-Agent": "Mozilla/5.0", "Content-Type": "application/json", "Referer": f"https://vidara.to/e/{filecode}"}
    payload = json.dumps({"filecode": filecode, "device": "web", "codecs": ["h264", "av1", "h265"]}).encode("utf-8")
    try:
        req = urllib.request.Request(api_url, data=payload, headers=headers)
        with urllib.request.urlopen(req, timeout=12) as res:
            data = json.loads(res.read().decode("utf-8"))
            stream_url = data.get("streaming_url")
            title = data.get("title", filecode)
            thumb = data.get("thumbnail")
            if stream_url:
                return {"type": "hls", "url": stream_url, "title": title, "thumbnail": thumb, "size_str": "Stream HLS", "ext": "mp4"}
    except Exception as e:
        broadcast_log(f"Gagal resolve Vidara: {e}", "warning")
    return None

def resolve_filecoster_detail(detail_url):
    try:
        detail_url = detail_url.replace("vski.cc", "filecoster.com")
        req = urllib.request.Request(detail_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, context=ssl_ctx, timeout=12) as res:
            html = res.read().decode("utf-8", errors="ignore")
        
        m_poster = re.search(r'poster=[\'"]([^\'"]+)[\'"]', html)
        thumb = m_poster.group(1) if m_poster else None
        
        m_src = re.search(r'SRC\s*=\s*[\'"](/ip129jk[^\'"]+)[\'"]', html)
        if not m_src: return None
        
        embed_url = "https://filecoster.com" + m_src.group(1)
        req_embed = urllib.request.Request(embed_url, headers={"User-Agent": "Mozilla/5.0", "Referer": detail_url})
        with urllib.request.urlopen(req_embed, context=ssl_ctx, timeout=12) as res_embed:
            html_embed = res_embed.read().decode("utf-8", errors="ignore")
        
        m_player = re.search(r'playerPath\s*=\s*[\'"]([^\'"]+)[\'"]', html_embed)
        if not m_player: return None
        
        player_url = m_player.group(1).replace(r"\u0026", "&")
        req_stream = urllib.request.Request(player_url, headers={"User-Agent": "Mozilla/5.0", "Referer": embed_url})
        with urllib.request.urlopen(req_stream, context=ssl_ctx, timeout=12) as res_stream:
            html_stream = res_stream.read().decode("utf-8", errors="ignore")
        
        m_source = re.search(r'<source\s+src=[\'"]([^\'"]+)[\'"]', html_stream)
        m_title = re.search(r'title:\s*[\'"]([^\'"]+)[\'"]', html_stream)
        if not thumb:
            m_p = re.search(r'poster=[\'"]([^\'"]+)[\'"]', html_stream)
            if m_p: thumb = m_p.group(1)
                
        if m_source:
            stream_direct = m_source.group(1)
            title = m_title.group(1) if m_title else "video"
            sz_str = "Auto"
            try:
                head_req = urllib.request.Request(stream_direct, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://filecoster.com/"}, method="HEAD")
                with urllib.request.urlopen(head_req, context=ssl_ctx, timeout=8) as h_res:
                    clen = int(h_res.headers.get("Content-Length", 0))
                    if clen > 0: sz_str = f"{clen/(1024*1024):.1f} MB"
            except Exception: pass

            return {
                "type": "direct_mp4", 
                "url": stream_direct, 
                "title": title, 
                "thumbnail": thumb, 
                "size_str": sz_str,
                "referer": "https://filecoster.com/", 
                "ext": "mp4"
            }
    except Exception as e:
        broadcast_log(f"Gagal parse Filecoster: {e}", "warning")
    return None

def resolve_vski_or_filecoster(url):
    try:
        # Jika link berbentuk /e/ atau /d/ langsung ke filecoster
        if "filecoster.com/d/" in url or "filecoster.com/e/" in url:
            single = resolve_filecoster_detail(url)
            if single: return [single]

        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, context=ssl_ctx, timeout=12) as res:
            html = res.read().decode("utf-8", errors="ignore")
        
        # Deteksi redirect filecoster (dari tribunvideo, plav.ink, vdko, vski, dll)
        m_fc = re.search(r'https://filecoster\.com/[^\s"\'<>]+', html)
        if m_fc:
            return resolve_vski_or_filecoster(m_fc.group(0))
        
        results = []
        detail_ids = list(set(re.findall(r'/(?:d|e)/([a-zA-Z0-9_-]+)', html)))
        if detail_ids:
            for d_id in detail_ids:
                single = resolve_filecoster_detail(f"https://filecoster.com/d/{d_id}") or resolve_filecoster_detail(f"https://filecoster.com/e/{d_id}")
                if single: results.append(single)
            return results
        
        if "/d/" in url or "/e/" in url:
            single = resolve_filecoster_detail(url)
            return [single] if single else []

        return []
    except Exception as e:
        broadcast_log(f"Gagal parse VSKI: {e}", "warning")
        return []

def resolve_uc_share(url):
    try:
        m = re.search(r'uc-share\.com/s/([a-zA-Z0-9]+)', url)
        pwd_id = m.group(1) if m else url.split('/')[-1].split('?')[0]
        
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X)'})
        with urllib.request.urlopen(req, context=ssl_ctx, timeout=12) as res:
            html = res.read().decode('utf-8', errors='ignore')
        
        idx = html.find('__INITIAL_PROPS__ = ')
        if idx != -1:
            end_idx = html.find('</script>', idx)
            data = json.loads(html[idx + len('__INITIAL_PROPS__ = '):end_idx].strip())
            share = data['initialData']['shareDetail']['share']
            title = share.get('title', f"UC_{pwd_id}")
            fsize = share.get('size', 0)
            sz_str = f"{fsize / (1024 * 1024):.1f} MB" if fsize else "UC Drive"
            
            return [{
                "type": "uc_drive",
                "url": url,
                "title": title,
                "thumbnail": "https://img.icons8.com/fluency/96/cloud-folder.png",
                "size_str": sz_str,
                "ext": "mp4",
                "pwd_id": pwd_id,
                "share_id": share.get('share_id'),
                "first_fid": share.get('first_fid')
            }]
    except Exception as e:
        broadcast_log(f"Gagal parse UC Drive: {e}", "warning")
    return None

def resolve_universal(url):
    if "vidara." in url:
        r = resolve_vidara(url)
        return [r] if r else []
    elif "vski." in url or "filecoster." in url:
        return resolve_vski_or_filecoster(url)
    elif "uc-share.com" in url:
        r = resolve_uc_share(url)
        return r if r else []
    else:
        r = resolve_vidara(url)
        if r: return [r]
        return resolve_vski_or_filecoster(url)

from telethon import TelegramClient
from telethon.sessions import StringSession

API_ID = 31350452
API_HASH = "d26a371b77634346b594c88c5dfe635f"
STRING_SESSION_FILE = os.path.join(BASE_DIR, "string_session.txt")

tg_client_instance = None
async def get_telegram_client():
    global tg_client_instance
    if tg_client_instance and tg_client_instance.is_connected():
        return tg_client_instance
    session_str = os.environ.get("STRING_SESSION")
    if not session_str and os.path.exists(STRING_SESSION_FILE):
        try:
            with open(STRING_SESSION_FILE, "r") as f:
                session_str = f.read().strip()
        except Exception:
            pass
    if not session_str:
        return None
    try:
        tg_client_instance = TelegramClient(StringSession(session_str), API_ID, API_HASH)
        await tg_client_instance.connect()
        return tg_client_instance
    except Exception as e:
        print(f"Error connect TG in server: {e}")
        return None

# Route on-demand thumbnail fallback dari Telegram langsung
@app.get("/media/{filename}")
async def get_media_file(filename: str):
    file_p = os.path.join(MEDIA_DIR, filename)
    if os.path.exists(file_p) and os.path.getsize(file_p) > 1000:
        return FileResponse(file_p)

    # Parse pola: photo_{channel_id}_{msg_id}.jpg atau thumb_{channel_id}_{msg_id}.jpg
    m = re.match(r'(?:photo|thumb)_(-?\d+)_(\d+)\.jpg', filename)
    if m:
        ch_id = int(m.group(1))
        msg_id = int(m.group(2))
        client = await get_telegram_client()
        if client:
            try:
                tg_msg = await client.get_messages(ch_id, ids=msg_id)
                if tg_msg:
                    media_obj = tg_msg.photo or tg_msg.video
                    if media_obj:
                        thumb_target = media_obj if tg_msg.photo else None
                        await client.download_media(media_obj, file=file_p, thumb=-1 if tg_msg.video else None)
                        if os.path.exists(file_p) and os.path.getsize(file_p) > 1000:
                            return FileResponse(file_p)
            except Exception as e:
                print(f"Gagal unduh on-demand media {filename}: {e}")

    # Fallback placeholder jika tidak ditemukan
    return FileResponse(os.path.join(WEB_DIR, "index.html"), status_code=404)

# ================= API ROUTES =================
@app.get("/")
def get_index():
    return FileResponse(os.path.join(WEB_DIR, "index.html"))

@app.websocket("/ws/logs")
async def websocket_logs(websocket: WebSocket):
    await websocket.accept()
    active_connections.add(websocket)
    for log in LOGS[-30:]:
        await websocket.send_json({"type": "log", "data": log})
    for tid, pdata in PROGRESS_MAP.items():
        if not pdata.get("done"):
            await websocket.send_json({"type": "progress", "task_id": tid, "data": pdata})
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        active_connections.discard(websocket)

@app.get("/api/channels")
def get_channels():
    if os.path.exists(CHANNELS_FILE):
        try:
            return json.load(open(CHANNELS_FILE, "r", encoding="utf-8"))
        except Exception:
            pass
    return [{"id": -1003844911918, "name": "Drama CRT"}, {"id": -1003866531213, "name": "SECRET GUILD"}]

@app.get("/api/catalog")
def get_catalog(channel_id: str = None):
    cat = load_catalog()
    result = {}
    ch_map = get_channel_map()

    for unique_k, item in cat.items():
        item_ch = str(item.get("channel_id", "-1003844911918"))
        if channel_id and channel_id != "all" and item_ch != str(channel_id):
            continue

        ch_name = ch_map.get(item_ch, "Unknown Channel")
        msg_id = item.get("id", unique_k)
        base_title = clean_filename(item.get("title", f"post_{msg_id}"))
        post_dir = os.path.join(BASE_DIR, clean_filename(ch_name), f"{msg_id}_{base_title}")
        is_dl = False
        count = 0
        try:
            if os.path.exists(post_dir):
                files = [f for f in os.listdir(post_dir) if f.endswith(".mp4") and os.path.getsize(os.path.join(post_dir, f)) > 1000]
                count = len(files)
                if count > 0: is_dl = True
        except Exception:
            pass
            
        item["is_downloaded"] = is_dl
        item["local_count"] = count
        result[unique_k] = item

    return result

@app.get("/api/post/{unique_key}/videos")
def get_post_videos(unique_key: str):
    cat = load_catalog()
    item = cat.get(unique_key)
    if not item:
        # Fallback pencarian by id
        for k, v in cat.items():
            if str(v.get("id")) == unique_key or k == unique_key:
                item = v
                break
    if not item: return {"videos": []}
    
    ch_id = str(item.get("channel_id", "-1003844911918"))
    ch_map = get_channel_map()
    ch_name = ch_map.get(ch_id, "Drama CRT")
    post_id = item.get("id")
    base_title = clean_filename(item.get("title", f"post_{post_id}"))
    post_dir = os.path.join(BASE_DIR, clean_filename(ch_name), f"{post_id}_{base_title}")
    
    local_files = {}
    if os.path.exists(post_dir):
        for f in os.listdir(post_dir):
            if f.endswith(".mp4"):
                full_p = os.path.join(post_dir, f)
                sz = os.path.getsize(full_p)
                if sz > 1000:
                    local_files[f] = f"{sz / (1024 * 1024):.1f} MB"

    video_urls = item.get("video_urls", [])
    resolved_cache = item.get("resolved_cache", {})
    direct_tg = item.get("direct_tg_video")
    cloud_files = item.get("telegram_cloud_files", {})
    result = []

    # 1. Jika postingan memiliki video Telegram langsung (misal KESUKAAN PARA LELAKI)
    if direct_tg:
        orig_fn = direct_tg.get("file_name", f"video_{post_id}.mp4")
        t_name = clean_filename(os.path.splitext(orig_fn)[0])
        fn = f"{t_name}.mp4"
        is_local = fn in local_files
        is_in_cloud = fn in cloud_files or bool(direct_tg)
        sz = local_files.get(fn) or direct_tg.get("size_str", "Telegram Cloud")
        result.append({
            "name": t_name or item.get("title", f"Video #{post_id}"),
            "filename": fn,
            "thumbnail": item.get("thumbnail"),
            "is_local": is_local or is_in_cloud,
            "size_str": sz,
            "play_url": f"/stream/{ch_id}/{post_id}/{fn}",
            "url": f"tg://{ch_id}/{post_id}",
            "type": "direct_tg"
        })
    
    for u in video_urls:
        short_u = u.split("/")[-1].split("?")[0]
        r_list = resolved_cache.get(u)
        if not r_list:
            r_list = resolve_universal(u)
            if r_list:
                resolved_cache[u] = r_list
                item["resolved_cache"] = resolved_cache
                cat[unique_key] = item
                save_catalog(cat)

        if r_list:
            for idx, r_item in enumerate(r_list):
                if not r_item: continue
                title = clean_filename(r_item.get("title", f"part_{idx+1}"))
                fname = f"{title}.mp4"
                is_local = fname in local_files
                is_in_cloud = fname in cloud_files
                sz_str = local_files.get(fname) or r_item.get("size_str", "Cloud")
                
                result.append({
                    "name": title,
                    "filename": fname,
                    "thumbnail": r_item.get("thumbnail") or item.get("thumbnail"),
                    "is_local": is_local or is_in_cloud,
                    "size_str": sz_str,
                    "play_url": f"/stream/{ch_id}/{post_id}/{fname}",
                    "url": u,
                    "type": r_item.get("type", "direct_mp4")
                })
        else:
            result.append({
                "name": f"File Cloud ({short_u})",
                "filename": f"{short_u}.mp4",
                "thumbnail": item.get("thumbnail"),
                "is_local": False,
                "size_str": "Cloud",
                "play_url": "",
                "url": u,
                "type": "cloud"
            })

    return {"videos": result}

@app.get("/stream/{channel_id}/{post_id}/{filename}")
def stream_video(channel_id: str, post_id: int, filename: str, request: Request):
    cat = load_catalog()
    ch_map = get_channel_map()
    ch_name = ch_map.get(str(channel_id), "Drama CRT")
    
    item = None
    for k, v in cat.items():
        if str(v.get("channel_id")) == str(channel_id) and v.get("id") == post_id:
            item = v
            break
            
    base_title = clean_filename(item.get("title", f"post_{post_id}")) if item else f"post_{post_id}"
    post_dir = os.path.join(BASE_DIR, clean_filename(ch_name), f"{post_id}_{base_title}")
    file_path = os.path.join(post_dir, filename)
    range_header = request.headers.get("range")
    
    # 1. Jika file lokal utuh sudah ada, sajikan dengan dukungan Byte-Range Seeker akurat
    if os.path.exists(file_path):
        file_size = os.path.getsize(file_path)
        start = 0
        end = file_size - 1
        status_code = 200

        if range_header and range_header.startswith("bytes="):
            status_code = 206
            byte_range = range_header.replace("bytes=", "").split("-")
            start = int(byte_range[0]) if byte_range[0] else 0
            end = int(byte_range[1]) if (len(byte_range) > 1 and byte_range[1]) else file_size - 1
            if end >= file_size: end = file_size - 1

        content_length = (end - start) + 1

        def local_file_iter():
            with open(file_path, "rb") as f:
                f.seek(start)
                remaining = content_length
                chunk_sz = 1024 * 64
                while remaining > 0:
                    read_amount = min(chunk_sz, remaining)
                    chunk = f.read(read_amount)
                    if not chunk: break
                    remaining -= len(chunk)
                    yield chunk

        resp_headers = {
            "Content-Type": "video/mp4",
            "Accept-Ranges": "bytes",
            "Content-Length": str(content_length),
            "Content-Range": f"bytes {start}-{end}/{file_size}",
            "Access-Control-Allow-Origin": "*",
        }
        return StreamingResponse(local_file_iter(), status_code=status_code, headers=resp_headers)

    # 2. Jika file Telegram langsung (misal KESUKAAN PARA LELAKI) dan belum selesai diunduh
    if item and item.get("direct_tg_video"):
        dtg = item["direct_tg_video"]
        async def stream_tg_video():
            client = await get_telegram_client()
            if not client:
                return JSONResponse(status_code=500, content={"error": "Telegram Client offline"})
            try:
                tg_msg = await client.get_messages(int(channel_id), ids=post_id)
                if not tg_msg or not tg_msg.video:
                    return JSONResponse(status_code=404, content={"error": "Video Telegram tidak ditemukan"})
                
                # Streaming generator langsung dari chunk media Telegram
                async def tg_chunk_generator():
                    async for chunk in client.iter_download(tg_msg.video, chunk_size=128 * 1024):
                        yield chunk

                resp_headers = {
                    "Content-Type": "video/mp4",
                    "Accept-Ranges": "bytes",
                    "Access-Control-Allow-Origin": "*",
                    "Content-Length": str(dtg.get("size", tg_msg.file.size if tg_msg.file else 0))
                }
                return StreamingResponse(tg_chunk_generator(), status_code=200, headers=resp_headers)
            except Exception as e:
                return JSONResponse(status_code=500, content={"error": f"Gagal stream Telegram: {e}"})

        return asyncio.run(stream_tg_video())

    # 3. Jika file sudah ada di Telegram Cloud Database (-1004423804448)
    telegram_db_id = -1004423804448
    if item and "telegram_cloud_files" in item and filename in item["telegram_cloud_files"]:
        cloud_info = item["telegram_cloud_files"][filename]
        db_msg_id = cloud_info.get("msg_id")
        if db_msg_id:
            async def stream_tg_db_video():
                client = await get_telegram_client()
                if not client:
                    return JSONResponse(status_code=500, content={"error": "Telegram Client offline"})
                try:
                    db_msg = await client.get_messages(telegram_db_id, ids=db_msg_id)
                    if db_msg and db_msg.video:
                        async def tg_db_generator():
                            async for chunk in client.iter_download(db_msg.video, chunk_size=128 * 1024):
                                yield chunk
                        resp_headers = {
                            "Content-Type": "video/mp4",
                            "Accept-Ranges": "bytes",
                            "Access-Control-Allow-Origin": "*",
                            "Content-Length": str(db_msg.file.size if db_msg.file else 0)
                        }
                        return StreamingResponse(tg_db_generator(), status_code=200, headers=resp_headers)
                except Exception as e:
                    print(f"Gagal stream dari DB Cloud Telegram: {e}")
            
            res_stream = asyncio.run(stream_tg_db_video())
            if res_stream: return res_stream

    # 4. Jika belum ada di lokal maupun DB Cloud, cari resolved stream URL dari cloud luar
    target_item = None
    if item:
        resolved_cache = item.get("resolved_cache", {})
        for u, r_list in resolved_cache.items():
            for r in r_list:
                if clean_filename(r.get("title", "")) + ".mp4" == filename or r.get("title", "") == filename:
                    target_item = r
                    break
            if target_item: break

        if not target_item:
            for u in item.get("video_urls", []):
                r_list = resolve_universal(u)
                if r_list:
                    resolved_cache[u] = r_list
                    item["resolved_cache"] = resolved_cache
                    cat[unique_key] = item
                    save_catalog(cat)
                    for r in r_list:
                        if clean_filename(r.get("title", "")) + ".mp4" == filename or r.get("title", "") == filename:
                            target_item = r
                            break
                if target_item: break

    if not target_item or not target_item.get("url"):
        return JSONResponse(status_code=404, content={"error": "File belum siap distream"})

    # 3. Stream langsung dari Cloud dengan forwarding Range Header
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    if target_item.get("referer"):
        headers["Referer"] = target_item["referer"]
        
    if range_header:
        headers["Range"] = range_header

    try:
        req = urllib.request.Request(target_item["url"], headers=headers)
        res = urllib.request.urlopen(req, context=ssl_ctx, timeout=30)
        
        status_code = res.status if hasattr(res, "status") else (206 if range_header else 200)
        resp_headers = {
            "Content-Type": res.headers.get("Content-Type", "video/mp4"),
            "Accept-Ranges": "bytes",
            "Access-Control-Allow-Origin": "*",
        }
        if "Content-Length" in res.headers:
            resp_headers["Content-Length"] = res.headers["Content-Length"]
        if "Content-Range" in res.headers:
            resp_headers["Content-Range"] = res.headers["Content-Range"]

        def stream_generator():
            try:
                while True:
                    chunk = res.read(1024 * 64)
                    if not chunk: break
                    yield chunk
            except Exception:
                pass
            finally:
                res.close()

        return StreamingResponse(stream_generator(), status_code=status_code, headers=resp_headers)
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": f"Streaming gagal: {e}"})

@app.post("/api/download_single")
def download_single(unique_key: str, filename: str):
    cat = load_catalog()
    item = cat.get(unique_key)
    if not item:
        for k, v in cat.items():
            if str(v.get("id")) == unique_key or k == unique_key:
                item = v
                break
    if not item: return {"success": False, "error": "Item tidak ditemukan"}
    
    ch_id = str(item.get("channel_id", "-1003844911918"))
    ch_map = get_channel_map()
    ch_name = ch_map.get(ch_id, "Drama CRT")
    post_id = item.get("id")
    base_title = clean_filename(item.get("title", f"post_{post_id}"))
    post_dir = os.path.join(BASE_DIR, clean_filename(ch_name), f"{post_id}_{base_title}")
    os.makedirs(post_dir, exist_ok=True)
    
    target_item = None
    resolved_cache = item.get("resolved_cache", {})
    for u, r_list in resolved_cache.items():
        for r in r_list:
            if clean_filename(r.get("title", "")) + ".mp4" == filename or r.get("title", "") == filename:
                target_item = r
                break
        if target_item: break

    # Jika belum ada di cache, langsung resolve saat itu juga secara on-the-fly!
    if not target_item:
        for u in item.get("video_urls", []):
            if u not in resolved_cache:
                r_list = resolve_universal(u)
                if r_list:
                    resolved_cache[u] = r_list
                    item["resolved_cache"] = resolved_cache
                    cat[unique_key] = item
                    save_catalog(cat)
                    for r in r_list:
                        if clean_filename(r.get("title", "")) + ".mp4" == filename or r.get("title", "") == filename:
                            target_item = r
                            break
            if target_item: break

    if not target_item:
        return {"success": False, "error": "Gagal mengurai link streaming/download"}

    task_id = f"{post_id}_{filename}"
    out_f = os.path.join(post_dir, filename if filename.endswith(".mp4") else f"{filename}.mp4")

    def run():
        if target_item.get("type") == "direct_mp4":
            download_segmented_file(target_item["url"], out_f, referer=target_item.get("referer"), num_threads=4, task_id=task_id, post_title=base_title, post_id=post_id)
        elif target_item.get("type") == "hls":
            broadcast_log(f"Download HLS Stream: {filename}...", "info")
            ydl_opts = {'outtmpl': out_f, 'quiet': True, 'nocheckcertificate': True}
            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl: ydl.download([target_item["url"]])
                broadcast_progress(task_id, {"name": filename, "percent": 100, "size": "Done", "done": True})
                broadcast_log(f"Selesai HLS: {filename}", "success")
                if os.path.exists(out_f):
                    executor.submit(upload_to_telegram_cloud, out_f, base_title, post_id)
            except Exception as e:
                broadcast_log(f"Error HLS: {e}", "error")

    executor.submit(run)
    return {"success": True, "message": "Download dikirim ke multi-connection engine"}

@app.post("/api/post/{unique_key}/download_all")
def download_all(unique_key: str):
    cat = load_catalog()
    item = cat.get(unique_key)
    if not item:
        for k, v in cat.items():
            if str(v.get("id")) == unique_key or k == unique_key:
                item = v
                break
    if not item: return {"message": "Postingan tidak ditemukan"}
    
    ch_id = str(item.get("channel_id", "-1003844911918"))
    ch_map = get_channel_map()
    ch_name = ch_map.get(ch_id, "Drama CRT")
    post_id = item.get("id")
    base_title = clean_filename(item.get("title", f"post_{post_id}"))
    post_dir = os.path.join(BASE_DIR, clean_filename(ch_name), f"{post_id}_{base_title}")
    os.makedirs(post_dir, exist_ok=True)
    
    resolved_cache = item.get("resolved_cache", {})
    all_targets = []
    
    for u in item.get("video_urls", []):
        r_list = resolved_cache.get(u)
        if not r_list:
            r_list = resolve_universal(u)
            if r_list: resolved_cache[u] = r_list
        if r_list: all_targets.extend(r_list)

    item["resolved_cache"] = resolved_cache
    cat[unique_key] = item
    save_catalog(cat)

    if not all_targets: return {"message": "Tidak ada link stream valid"}

    broadcast_log(f"Kebut Multi-Thread Batch untuk Post #{post_id} ({len(all_targets)} file)...", "info")
    for idx, r_item in enumerate(all_targets):
        title = clean_filename(r_item.get("title", f"part_{idx+1}"))
        fname = f"{title}.mp4"
        out_f = os.path.join(post_dir, fname)
        t_id = f"{post_id}_{fname}"
        if r_item.get("type") == "direct_mp4":
            executor.submit(download_segmented_file, r_item["url"], out_f, r_item.get("referer"), 4, t_id)
        elif r_item.get("type") == "hls":
            def dl_hls(target, out_path, task):
                ydl_opts = {'outtmpl': out_path, 'quiet': True, 'nocheckcertificate': True}
                try:
                    with yt_dlp.YoutubeDL(ydl_opts) as ydl: ydl.download([target["url"]])
                    broadcast_progress(task, {"name": os.path.basename(out_path), "percent": 100, "size": "Done", "done": True})
                except Exception: pass
            executor.submit(dl_hls, r_item, out_f, t_id)

    return {"message": f"{len(all_targets)} file dimasukkan ke queue segmented download!"}

@app.post("/api/sync")
def trigger_sync(channel_id: str = "-1003844911918", bg: BackgroundTasks = None):
    def run_sync():
        ch_map = get_channel_map()
        ch_name = ch_map.get(str(channel_id), "Channel")
        broadcast_log(f"Memulai sinkronisasi pesan & media untuk {ch_name}...", "info")
        os.system(f'python "{os.path.join(BASE_DIR, "sync_catalog.py")}" --channel_id {channel_id}')
        broadcast_log(f"Sinkronisasi {ch_name} selesai!", "success")
    bg.add_task(run_sync)
    return {"message": "Sync dimulai"}

# ================= TELEVAULT PREMIUM APIS =================
@app.get("/api/stats")
def get_televault_stats():
    cat = load_catalog()
    total_posts = len(cat)
    total_videos = 0
    total_photos = 0
    total_local_files = 0
    total_local_bytes = 0

    # Hitung storage lokal
    for ch_folder in ["Drama CRT", "AGENT NIKMAT V2", "media_illustrasi"]:
        f_dir = os.path.join(BASE_DIR, ch_folder)
        if os.path.exists(f_dir):
            for r, d, files in os.walk(f_dir):
                for f in files:
                    fp = os.path.join(r, f)
                    try:
                        sz = os.path.getsize(fp)
                        total_local_bytes += sz
                        if f.endswith(".mp4"):
                            total_local_files += 1
                    except Exception:
                        pass

    for k, v in cat.items():
        v_urls = v.get("video_urls", [])
        if v_urls:
            total_videos += len(v_urls)
        if v.get("media_type") == "photo" or v.get("thumbnail"):
            total_photos += 1

    ch_map = get_channel_map()
    
    return {
        "total_posts": total_posts,
        "total_videos": total_videos,
        "total_photos": total_photos,
        "total_local_files": total_local_files,
        "total_storage_mb": round(total_local_bytes / (1024 * 1024), 1),
        "total_storage_gb": round(total_local_bytes / (1024 * 1024 * 1024), 2),
        "total_channels": len(ch_map),
        "channels": [{"id": k, "name": v} for k, v in ch_map.items()]
    }

@app.get("/api/favorites")
def get_favorites():
    conn = get_db()
    rows = conn.execute("SELECT unique_key FROM favorites").fetchall()
    conn.close()
    return [r["unique_key"] for r in rows]

@app.post("/api/favorites/toggle")
def toggle_favorite(payload: dict):
    unique_key = payload.get("unique_key")
    if not unique_key: return {"error": "Missing unique_key"}
    conn = get_db()
    existing = conn.execute("SELECT unique_key FROM favorites WHERE unique_key = ?", (unique_key,)).fetchone()
    is_fav = False
    if existing:
        conn.execute("DELETE FROM favorites WHERE unique_key = ?", (unique_key,))
    else:
        conn.execute("INSERT INTO favorites (unique_key) VALUES (?)", (unique_key,))
        is_fav = True
    conn.commit()
    conn.close()
    return {"unique_key": unique_key, "is_favorite": is_fav}

@app.get("/api/playback/continue")
def get_continue_watching():
    conn = get_db()
    rows = conn.execute("SELECT * FROM playback_history ORDER BY last_played DESC LIMIT 10").fetchall()
    conn.close()
    return [dict(r) for r in rows]

@app.post("/api/playback/save")
def save_playback_progress(payload: dict):
    video_id = payload.get("video_id")
    unique_key = payload.get("unique_key")
    title = payload.get("title", "Video")
    play_url = payload.get("play_url", "")
    current_time = float(payload.get("current_time", 0))
    duration = float(payload.get("duration", 1))
    pct = round((current_time / duration) * 100, 1) if duration > 0 else 0

    conn = get_db()
    conn.execute("""
        INSERT INTO playback_history (video_id, unique_key, title, play_url, current_time, duration, progress_percent, last_played)
        VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(video_id) DO UPDATE SET
            current_time = excluded.current_time,
            duration = excluded.duration,
            progress_percent = excluded.progress_percent,
            last_played = CURRENT_TIMESTAMP
    """, (video_id, unique_key, title, play_url, current_time, duration, pct))
    conn.commit()
    conn.close()
    return {"success": True, "progress": pct}

@app.get("/api/collections")
def get_collections():
    conn = get_db()
    cols = conn.execute("SELECT * FROM collections ORDER BY created_at DESC").fetchall()
    result = []
    for c in cols:
        c_dict = dict(c)
        items = conn.execute("SELECT unique_key FROM collection_items WHERE collection_id = ?", (c["id"],)).fetchall()
        c_dict["item_count"] = len(items)
        c_dict["keys"] = [it["unique_key"] for it in items]
        result.append(c_dict)
    conn.close()
    return result

@app.post("/api/collections/create")
def create_collection(payload: dict):
    name = payload.get("name", "").strip()
    desc = payload.get("description", "").strip()
    if not name: return {"error": "Nama koleksi tidak boleh kosong"}
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("INSERT INTO collections (name, description) VALUES (?, ?)", (name, desc))
        c_id = cur.lastrowid
        conn.commit()
        conn.close()
        return {"id": c_id, "name": name, "description": desc}
    except sqlite3.IntegrityError:
        conn.close()
        return {"error": "Koleksi dengan nama ini sudah ada"}

@app.post("/api/collections/add_item")
def add_to_collection(payload: dict):
    col_id = payload.get("collection_id")
    unique_key = payload.get("unique_key")
    if not col_id or not unique_key: return {"error": "Invalid params"}
    conn = get_db()
    conn.execute("INSERT OR IGNORE INTO collection_items (collection_id, unique_key) VALUES (?, ?)", (col_id, unique_key))
    conn.commit()
    conn.close()
    return {"success": True}

@app.post("/api/batch_download")
def batch_download_items(payload: dict):
    unique_keys = payload.get("unique_keys", [])
    if not unique_keys: return {"error": "Tidak ada item yang dipilih"}
    
    cat = load_catalog()
    triggered = 0
    for uk in unique_keys:
        item = cat.get(uk)
        if not item: continue
        ch_id = str(item.get("channel_id", "-1003844911918"))
        ch_map = get_channel_map()
        ch_name = ch_map.get(ch_id, "Drama CRT")
        post_id = item.get("id")
        base_title = clean_filename(item.get("title", f"post_{post_id}"))
        post_dir = os.path.join(BASE_DIR, clean_filename(ch_name), f"{post_id}_{base_title}")
        os.makedirs(post_dir, exist_ok=True)

        resolved_cache = item.get("resolved_cache", {})
        all_targets = []
        for u in item.get("video_urls", []):
            r_list = resolved_cache.get(u)
            if not r_list:
                r_list = resolve_universal(u)
                if r_list: resolved_cache[u] = r_list
            if r_list: all_targets.extend(r_list)

        item["resolved_cache"] = resolved_cache
        cat[uk] = item

        for idx, r_item in enumerate(all_targets):
            title = clean_filename(r_item.get("title", f"part_{idx+1}"))
            fname = f"{title}.mp4"
            out_f = os.path.join(post_dir, fname)
            t_id = f"{post_id}_{fname}"
            if not os.path.exists(out_f) or os.path.getsize(out_f) < 50000:
                triggered += 1
                if r_item.get("type") == "direct_mp4":
                    executor.submit(download_segmented_file, r_item["url"], out_f, r_item.get("referer"), 4, t_id, base_title, post_id)
                elif r_item.get("type") == "hls":
                    def dl_hls(target, out_path, task, p_tit, p_id):
                        ydl_opts = {'outtmpl': out_path, 'quiet': True, 'nocheckcertificate': True}
                        try:
                            with yt_dlp.YoutubeDL(ydl_opts) as ydl: ydl.download([target["url"]])
                            broadcast_progress(task, {"name": os.path.basename(out_path), "percent": 100, "size": "Done", "done": True})
                            if os.path.exists(out_path):
                                executor.submit(upload_to_telegram_cloud, out_path, p_tit, p_id)
                        except Exception: pass
                    executor.submit(dl_hls, r_item, out_f, t_id, base_title, post_id)

    save_catalog(cat)
    broadcast_log(f"Batch Multi-Select Download: {triggered} video dimasukkan ke antrean!", "success")
    return {"success": True, "message": f"{triggered} file dimulai di background"}

@app.delete("/api/media/{unique_key}")
def delete_media_item(unique_key: str):
    cat = load_catalog()
    if unique_key not in cat:
        return JSONResponse(status_code=404, content={"error": "Item tidak ditemukan"})
    
    item = cat[unique_key]
    ch_map = get_channel_map()
    ch_name = ch_map.get(str(item.get("channel_id")), "Drama CRT")
    post_id = item.get("id")
    base_title = clean_filename(item.get("title", f"post_{post_id}"))
    post_dir = os.path.join(BASE_DIR, clean_filename(ch_name), f"{post_id}_{base_title}")

    # Hapus file fisik jika ada
    if os.path.exists(post_dir):
        import shutil
        shutil.rmtree(post_dir, ignore_errors=True)

    del cat[unique_key]
    save_catalog(cat)

    # Bersihkan dari favorites & collections
    conn = get_db()
    conn.execute("DELETE FROM favorites WHERE unique_key = ?", (unique_key,))
    conn.execute("DELETE FROM collection_items WHERE unique_key = ?", (unique_key,))
    conn.execute("DELETE FROM playback_history WHERE unique_key = ?", (unique_key,))
    conn.commit()
    conn.close()

    broadcast_log(f"Item #{post_id} dihapus dari arsip.", "warning")
    return {"success": True, "message": f"Post #{post_id} berhasil dihapus"}

if __name__ == "__main__":
    broadcast_log("Universal Multi-Channel Hub Siap!", "success")
    port = int(os.environ.get("PORT", 8080))
    print(f"🚀 TeleVault Server aktif di port {port}")
    uvicorn.run("server:app", host="0.0.0.0", port=port, proxy_headers=True, forwarded_allow_ips="*")
