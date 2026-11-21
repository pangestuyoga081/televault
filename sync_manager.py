# Auto background resolver & real-time Telegram channel watcher
import os
import re
import ssl
import json
import time
import asyncio
import subprocess
import urllib.request
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CATALOG_FILE = os.path.join(BASE_DIR, "catalog.json")
STRING_SESSION_FILE = os.path.join(BASE_DIR, "string_session.txt")

CHANNELS_TO_WATCH = [
    {"id": -1003844911918, "folder": "Drama CRT"},
    {"id": -1004485856967, "folder": "KESUKAAN PARA LELAKI X SEKTORPLAY88"}
]
API_ID = 31350452
API_HASH = "d26a371b77634346b594c88c5dfe635f"

ssl_ctx = ssl.create_default_context()
ssl_ctx.check_hostname = False
ssl_ctx.verify_mode = ssl.CERT_NONE

def clean_filename(name):
    clean = re.sub(r'[\\/*?:"<>|]', "", name)
    clean = re.sub(r'\s+', " ", clean).strip().strip(".")
    return clean[:60] if clean else "video"

def load_catalog():
    if os.path.exists(CATALOG_FILE):
        try:
            with open(CATALOG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_catalog(cat):
    with open(CATALOG_FILE, "w", encoding="utf-8") as f:
        json.dump(cat, f, indent=2, ensure_ascii=False)

def extract_video_urls(text):
    if not text:
        return []
    urls = re.findall(r'https?://[^\s<>"]+', text)
    valid_urls = []
    for u in urls:
        if any(bad in u.lower() for bad in ['shopee.', 'tokopedia.', 'tiktok.com', 'lazada.', 's.id']):
            continue
        valid_urls.append(u)
    return valid_urls

def resolve_filecoster_detail(detail_url):
    try:
        detail_url = detail_url.replace("vski.cc", "filecoster.com")
        req = urllib.request.Request(detail_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, context=ssl_ctx, timeout=12) as res:
            html = res.read().decode("utf-8", errors="ignore")
        
        m_poster = re.search(r'poster=[\'"]([^\'"]+)[\'"]', html)
        thumb = m_poster.group(1) if m_poster else None
        
        m_src = re.search(r'SRC\s*=\s*[\'"](/ip129jk[^\'"]+)[\'"]', html)
        if not m_src:
            return None
        
        embed_url = "https://filecoster.com" + m_src.group(1)
        req_embed = urllib.request.Request(embed_url, headers={"User-Agent": "Mozilla/5.0", "Referer": detail_url})
        with urllib.request.urlopen(req_embed, context=ssl_ctx, timeout=12) as res_embed:
            html_embed = res_embed.read().decode("utf-8", errors="ignore")
        
        m_player = re.search(r'playerPath\s*=\s*[\'"]([^\'"]+)[\'"]', html_embed)
        if not m_player:
            return None
        
        player_url = m_player.group(1).replace(r"\u0026", "&")
        req_stream = urllib.request.Request(player_url, headers={"User-Agent": "Mozilla/5.0", "Referer": embed_url})
        with urllib.request.urlopen(req_stream, context=ssl_ctx, timeout=12) as res_stream:
            html_stream = res_stream.read().decode("utf-8", errors="ignore")
        
        m_source = re.search(r'<source\s+src=[\'"]([^\'"]+)[\'"]', html_stream)
        m_title = re.search(r'title:\s*[\'"]([^\'"]+)[\'"]', html_stream)
        if not thumb:
            m_p = re.search(r'poster=[\'"]([^\'"]+)[\'"]', html_stream)
            if m_p:
                thumb = m_p.group(1)
                
        if m_source:
            stream_direct = m_source.group(1)
            title = m_title.group(1) if m_title else "video"
            return {
                "type": "direct_mp4", 
                "url": stream_direct, 
                "title": title, 
                "thumbnail": thumb, 
                "size_str": "Auto",
                "referer": "https://filecoster.com/", 
                "ext": "mp4",
                "origin_url": detail_url
            }
    except Exception:
        pass
    return None

def resolve_vski(url):
    try:
        if "filecoster.com/d/" in url or "filecoster.com/e/" in url:
            single = resolve_filecoster_detail(url)
            return [single] if single else []

        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, context=ssl_ctx, timeout=12) as res:
            html = res.read().decode("utf-8", errors="ignore")
        
        m_fc = re.search(r'https://filecoster\.com/[^\s"\'<>]+', html)
        if m_fc:
            return resolve_vski(m_fc.group(0))
        
        detail_ids = list(dict.fromkeys(re.findall(r'/(?:d|e)/([a-zA-Z0-9_-]+)', html)))
        if detail_ids:
            with ThreadPoolExecutor(max_workers=8) as ex:
                futs = [ex.submit(resolve_filecoster_detail, f"https://filecoster.com/e/{d_id}") for d_id in detail_ids]
                results = [f.result() for f in futs if f.result()]
            return results
        
        if "/d/" in url or "/e/" in url:
            single = resolve_filecoster_detail(url)
            return [single] if single else []
    except Exception:
        pass
    return []

def resolve_vidara(url):
    try:
        filecode = url.split("/")[-1].split("?")[0]
        api_url = f"https://vidara.to/api/file?filecode={filecode}"
        req = urllib.request.Request(api_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, context=ssl_ctx, timeout=12) as res:
            data = json.loads(res.read().decode("utf-8"))
            stream_url = data.get("result", {}).get("url")
            if stream_url:
                title = data.get("result", {}).get("file", {}).get("title", f"vidara_{filecode}")
                return {"type": "hls", "url": stream_url, "title": title, "referer": "https://vidara.to/"}
    except Exception:
        pass
    return None

def resolve_universal(url):
    if "vidara." in url:
        r = resolve_vidara(url)
        return [r] if r else []
    return resolve_vski(url)

def commit_and_push_catalog():
    """Commit dan push catalog.json ke GitHub jika ada perubahan"""
    try:
        # Periksa apakah ada perubahan di catalog.json
        diff_proc = subprocess.run(["git", "diff", "--quiet", "catalog.json"], cwd=BASE_DIR)
        if diff_proc.returncode != 0:
            print("[GitHub Auto-Sync] Mendeteksi URL baru terurai, committing catalog.json...")
            subprocess.run(["git", "add", "catalog.json"], cwd=BASE_DIR, check=True)
            subprocess.run(["git", "commit", "-m", "chore: auto-update resolved stream URLs in catalog"], cwd=BASE_DIR, check=True)
            push_res = subprocess.run(["git", "push", "origin", "main"], cwd=BASE_DIR, capture_output=True, text=True)
            if push_res.returncode == 0:
                print("[GitHub Auto-Sync] Berhasil push catalog.json ke GitHub!")
            else:
                print(f"[GitHub Auto-Sync] Note push (perlu kredensial/token jika di cloud): {push_res.stderr}")
    except Exception as e:
        print(f"[GitHub Auto-Sync] Error: {e}")


# ================= BACKGROUND BATCH RESOLVER =================
async def batch_resolve_unresolved_posts():
    """Mengurai video_urls yang belum terurai di background dan commit ke GitHub"""
    while True:
        try:
            catalog = load_catalog()
            unresolved = []
            for k, v in catalog.items():
                if v.get("video_urls") and not v.get("resolved_cache"):
                    unresolved.append(k)

            if unresolved:
                print(f"[Batch Resolver] Ditemukan {len(unresolved)} post belum terurai. Memulai penguraian...")
                count_resolved = 0
                for k in unresolved:
                    item = catalog.get(k)
                    if not item:
                        continue
                    urls = item.get("video_urls", [])
                    resolved_cache = {}
                    for u in urls:
                        r_list = resolve_universal(u)
                        if r_list:
                            resolved_cache[u] = r_list
                    
                    if resolved_cache:
                        catalog[k]["resolved_cache"] = resolved_cache
                        save_catalog(catalog)
                        count_resolved += 1
                        print(f"  [+] Terurai #{k}: {item.get('title')} ({len(resolved_cache)} URL)")

                    if count_resolved > 0 and count_resolved % 10 == 0:
                        commit_and_push_catalog()

                    await asyncio.sleep(0.5)

                if count_resolved > 0:
                    commit_and_push_catalog()
                    print(f"[Batch Resolver] Selesai 1 siklus batch: {count_resolved} post berhasil diurai!")
        except Exception as e:
            print(f"[Batch Resolver] Error: {e}")

        # Istirahat 30 detik sebelum cek postingan yang belum terurai berikutnya
        await asyncio.sleep(30)


# ================= REALTIME TELEGRAM LISTENER =================
from telethon import TelegramClient, events
from telethon.sessions import StringSession

async def start_realtime_crawler_and_listener():
    session_str = os.environ.get("STRING_SESSION")
    if not session_str and os.path.exists(STRING_SESSION_FILE):
        try:
            with open(STRING_SESSION_FILE, "r") as f:
                session_str = f.read().strip()
        except Exception:
            pass

    if not session_str:
        print("[Realtime Watcher] STRING_SESSION tidak ditemukan. Menjalankan fallback batch resolver saja.")
        await batch_resolve_unresolved_posts()
        return

    client = TelegramClient(StringSession(session_str), API_ID, API_HASH)
    await client.connect()

    print("[Realtime Watcher] Terhubung ke Telegram Client! Memasang listener pesan baru...")

    channel_ids = [ch["id"] for ch in CHANNELS_TO_WATCH]

    @client.on(events.NewMessage(chats=channel_ids))
    async def handler_new_message(event):
        try:
            msg = event.message
            chat_id = event.chat_id
            msg_id = msg.id
            unique_key = f"{chat_id}_{msg_id}"
            text = msg.text or ""
            video_urls = extract_video_urls(text)
            has_tg_video = bool(msg.video)

            # Channel khusus video attachment langsung
            if str(chat_id) == "-1004485856967":
                if not has_tg_video:
                    return
                video_urls = []
            else:
                if not video_urls and not has_tg_video:
                    return

            title = clean_filename(text.strip().split("\n")[0]) if text.strip() else f"video_{msg_id}"

            print(f"\n[⚡ Realtime Update] Pesan baru di channel {chat_id}! ID: {msg_id} - {title}")

            catalog = load_catalog()

            # Langsung urai URL streaming seketika jika ada video_urls
            resolved_cache = {}
            for u in video_urls:
                r = resolve_universal(u)
                if r:
                    resolved_cache[u] = r

            item_data = {
                "id": msg_id,
                "unique_key": unique_key,
                "channel_id": str(chat_id),
                "channel_name": "Drama CRT" if str(chat_id) == "-1003844911918" else "Channel",
                "title": title,
                "full_text": text or title,
                "date": msg.date.strftime("%Y-%m-%d %H:%M:%S") if msg.date else datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "media_type": "video" if has_tg_video else "photo",
                "media_url": None,
                "thumbnail": None,
                "video_urls": video_urls,
                "direct_tg_video": None,
                "resolved_cache": resolved_cache,
                "telegram_cloud_files": {}
            }

            catalog[unique_key] = item_data
            save_catalog(catalog)
            print(f"[⚡ Realtime Update] Post #{unique_key} langsung tersimpan di catalog.json (Terurai: {len(resolved_cache)} URL)!")

            # Auto push ke GitHub
            commit_and_push_catalog()

        except Exception as e:
            print(f"[Realtime Watcher] Error handle pesan baru: {e}")

    # Jalankan background batch resolver berdampingan dengan event listener
    asyncio.create_task(batch_resolve_unresolved_posts())

    print("[Realtime Watcher] Listener aktif & mendengarkan pesan Drama CRT seketika!")
    await client.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(start_realtime_crawler_and_listener())
