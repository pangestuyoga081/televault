from concurrent.futures import ThreadPoolExecutor
import os
import re
import ssl
import json
import time
import asyncio
import subprocess
import urllib.request
from datetime import datetime
import yt_dlp
from telethon import TelegramClient
from telethon.sessions import StringSession

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CATALOG_FILE = os.path.join(BASE_DIR, "catalog.json")
MEDIA_DIR = os.path.join(BASE_DIR, "media_illustrasi")
STRING_SESSION_FILE = os.path.join(BASE_DIR, "string_session.txt")

TELEGRAM_DB_CHANNEL_ID = -1004423804448
CHANNELS_TO_CRAWL = [
    {"id": -1003844911918, "folder": "Drama CRT"},
    {"id": -1004485856967, "folder": "KESUKAAN PARA LELAKI X SEKTORPLAY88"}
]
API_ID = 31350452
API_HASH = "d26a371b77634346b594c88c5dfe635f"

os.makedirs(MEDIA_DIR, exist_ok=True)

ssl_ctx = ssl.create_default_context()
ssl_ctx.check_hostname = False
ssl_ctx.verify_mode = ssl.CERT_NONE

def make_faststart(filepath):
    """
    Pindahkan MOOV atom ke awal file MP4 tanpa re-encode (-c copy).
    Selesai dalam 0.2 detik tanpa beban CPU/kualitas berkurang.
    Membuat video langsung bisa di-stream / di-seek di browser tanpa error.
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

def clean_filename(name):
    name = re.sub(r'[\\/*?:"<>|]', "", name)
    return name.strip()[:60]

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
    if not text: return []
    urls = re.findall(r'https?://[^\s<>"]+', text)
    valid_urls = []
    for u in urls:
        if any(bad in u.lower() for bad in ['shopee.', 'tokopedia.', 'tiktok.com', 'lazada.', 's.id']):
            continue
        valid_urls.append(u)
    return valid_urls

def resolve_vidara(url):
    m = re.search(r'vidara\.(?:to|so)/(?:e/)?([a-zA-Z0-9_-]+)', url)
    if not m: return None
    filecode = m.group(1)
    api_url = "https://vidara.to/api/stream"
    headers = {"User-Agent": "Mozilla/5.0", "Content-Type": "application/json", "Referer": f"https://vidara.to/e/{filecode}"}
    payload = json.dumps({"filecode": filecode, "device": "web", "codecs": ["h264", "av1", "h265"]}).encode("utf-8")
    try:
        req = urllib.request.Request(api_url, data=payload, headers=headers)
        with urllib.request.urlopen(req, context=ssl_ctx, timeout=12) as res:
            data = json.loads(res.read().decode("utf-8"))
            stream_url = data.get("result", {}).get("url")
            if stream_url:
                title = data.get("result", {}).get("file", {}).get("title", f"vidara_{filecode}")
                return {"type": "hls", "url": stream_url, "title": title, "referer": "https://vidara.to/"}
    except Exception:
        pass
    return None

def resolve_filecoster_detail(url):
    try:
        url = url.replace("vski.cc", "filecoster.com")
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, context=ssl_ctx, timeout=12) as res:
            html = res.read().decode("utf-8", errors="ignore")
        m_title = re.search(r'<title>(.*?)</title>', html, re.IGNORECASE)
        page_title = m_title.group(1).replace("Filecoster", "").replace("vski", "").strip() if m_title else "video"
        m_sz = re.search(r'(\d+(?:\.\d+)?\s*(?:MB|GB|KB))', html, re.IGNORECASE)
        sz_str = m_sz.group(1) if m_sz else "Unknown"

        m_poster = re.search(r'poster=[\'"]([^\'"]+)[\'"]', html)
        thumb = m_poster.group(1) if m_poster else None

        m_src = re.search(r'SRC\s*=\s*[\'"](/ip129jk[^\'"]+)[\'"]', html)
        iframe_src = None
        if m_src:
            iframe_src = "https://filecoster.com" + m_src.group(1)
        else:
            m_iframe = re.search(r'<iframe[^>]+src=[\'"]([^\'"]+)[\'"]', html, re.IGNORECASE)
            if m_iframe:
                iframe_src = m_iframe.group(1)
                if iframe_src.startswith("/"): iframe_src = "https://filecoster.com" + iframe_src

        if not iframe_src: return None

        req_if = urllib.request.Request(iframe_src, headers={"User-Agent": "Mozilla/5.0", "Referer": url})
        with urllib.request.urlopen(req_if, context=ssl_ctx, timeout=12) as res_if:
            if_html = res_if.read().decode("utf-8", errors="ignore")

        m_pp = re.search(r'playerPath\s*=\s*[\'"]([^\'"]+)[\'"]', if_html)
        if not m_pp: return None
        player_path = m_pp.group(1).replace(r"\u0026", "&")
        if player_path.startswith("/"): player_path = "https://filecoster.com" + player_path

        req_p = urllib.request.Request(player_path, headers={"User-Agent": "Mozilla/5.0", "Referer": iframe_src})
        with urllib.request.urlopen(req_p, context=ssl_ctx, timeout=12) as res_p:
            p_html = res_p.read().decode("utf-8", errors="ignore")

        m_video = re.search(r'<source[^>]+src=[\'"]([^\'"]+)[\'"]', p_html)
        if not thumb:
            m_p = re.search(r'poster=[\'"]([^\'"]+)[\'"]', p_html)
            if m_p: thumb = m_p.group(1)

        if m_video:
            return {"type": "direct_mp4", "url": m_video.group(1), "title": page_title, "thumbnail": thumb, "size_str": sz_str, "referer": player_path}
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
        if m_fc: return resolve_vski(m_fc.group(0))

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

def resolve_universal(url):
    if "vidara." in url:
        r = resolve_vidara(url)
        return [r] if r else []
    else:
        return resolve_vski(url)
        return resolve_vski(url)
    return []

def download_file(url, out_path, referer=None):
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    if referer: headers["Referer"] = referer
    temp_path = out_path + ".part"

    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, context=ssl_ctx, timeout=90) as res, open(temp_path, "wb") as out_f:
            while True:
                chunk = res.read(1024 * 512)
                if not chunk: break
                out_f.write(chunk)
        
        for _ in range(5):
            try:
                if os.path.exists(out_path): os.remove(out_path)
                os.rename(temp_path, out_path)
                break
            except PermissionError:
                time.sleep(0.3)
        return True
    except Exception as e:
        print(f"      [!] Error download: {e}")
        return False

def download_hls(url, out_path):
    ydl_opts = {'outtmpl': out_path, 'quiet': True, 'nocheckcertificate': True}
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
        return True
    except Exception:
        return False

async def full_crawler_and_downloader():
    session_str = os.environ.get("STRING_SESSION")
    if not session_str and os.path.exists(STRING_SESSION_FILE):
        try:
            with open(STRING_SESSION_FILE, "r") as f:
                session_str = f.read().strip()
        except Exception:
            pass

    if not session_str:
        print("[!] STRING_SESSION tidak ditemukan di Environment Secret maupun file lokal.")
        return

    client = TelegramClient(StringSession(session_str), API_ID, API_HASH)
    await client.connect()
    
    db_entity = await client.get_entity(TELEGRAM_DB_CHANNEL_ID)
    print(f"🚀 Memulai Multi-Channel Auto-Downloader TeleVault!")
    print(f"Cloud DB Target: {db_entity.title}\n")

    catalog = load_catalog()

    for ch_info in CHANNELS_TO_CRAWL:
        target_ch_id = ch_info["id"]
        folder_name = ch_info["folder"]
        
        try:
            entity = await client.get_entity(target_ch_id)
            print(f"\n==================================================")
            print(f"📡 Memproses Saluran: {entity.title} (ID: {target_ch_id})")
            print(f"==================================================")
        except Exception as e:
            print(f"[!] Gagal akses channel {target_ch_id}: {e}")
            continue

        count_scanned = 0
        async for msg in client.iter_messages(entity):
            count_scanned += 1
            msg_id = msg.id
            unique_key = f"{target_ch_id}_{msg_id}"
            text = msg.text or ""
            video_urls = extract_video_urls(text)
            has_tg_video = bool(msg.video)

            # Khusus Saluran KESUKAAN PARA LELAKI (-1004485856967):
            # Hanya terima postingan yang MURNI memiliki attachment video Telegram native.
            # Semua pesan teks/link/promosi di-skip total.
            if str(target_ch_id) == "-1004485856967":
                if not has_tg_video:
                    continue
                video_urls = [] # Abaikan link teks eksternal sama sekali
            else:
                # Saluran Drama CRT: butuh link video atau attachment video
                if not video_urls and not has_tg_video:
                    continue

            # Tentukan judul post
            title = ""
            if text.strip():
                title = clean_filename(text.strip().split("\n")[0])
            elif has_tg_video and msg.file and msg.file.name:
                title = clean_filename(os.path.splitext(msg.file.name)[0])
            if not title:
                title = f"video_{msg_id}"

            post_dir = os.path.join(BASE_DIR, folder_name, f"{msg_id}_{title}")
            os.makedirs(post_dir, exist_ok=True)

            # 1. Update/Pastikan Postingan Tercatat di Catalog
            existing_item = catalog.get(unique_key, {})
            media_type = existing_item.get("media_type")
            media_url = existing_item.get("media_url")
            thumb_url = existing_item.get("thumbnail")

            # Unduh thumbnail telegram jika belum ada
            if not thumb_url:
                if msg.photo:
                    photo_fn = f"photo_{target_ch_id}_{msg_id}.jpg"
                    photo_p = os.path.join(MEDIA_DIR, photo_fn)
                    if not os.path.exists(photo_p):
                        try: await client.download_media(msg.photo, file=photo_p)
                        except Exception: pass
                    if os.path.exists(photo_p):
                        media_type = "photo"
                        media_url = f"/media/{photo_fn}"
                        thumb_url = f"/media/{photo_fn}"
                elif msg.video:
                    thumb_fn = f"thumb_{target_ch_id}_{msg_id}.jpg"
                    thumb_p = os.path.join(MEDIA_DIR, thumb_fn)
                    if not os.path.exists(thumb_p):
                        try: await client.download_media(msg.video, file=thumb_p, thumb=-1)
                        except Exception: pass
                    if os.path.exists(thumb_p):
                        media_type = "video"
                        media_url = f"/media/{thumb_fn}"
                        thumb_url = f"/media/{thumb_fn}"

            direct_tg_info = existing_item.get("direct_tg_video")
            if has_tg_video and not direct_tg_info:
                file_sz = msg.file.size if msg.file else (msg.video.size if hasattr(msg.video, 'size') else 0)
                file_sz_mb = f"{file_sz / (1024 * 1024):.1f} MB" if file_sz else "Unknown"
                orig_name = msg.file.name if (msg.file and msg.file.name) else f"tg_video_{msg_id}.mp4"
                direct_tg_info = {
                    "file_name": orig_name,
                    "size": file_sz,
                    "size_str": file_sz_mb,
                    "msg_id": msg_id
                }

            item_data = {
                "id": msg_id,
                "unique_key": unique_key,
                "channel_id": str(target_ch_id),
                "channel_name": entity.title,
                "title": title,
                "full_text": text or title,
                "date": msg.date.strftime("%Y-%m-%d %H:%M:%S") if msg.date else "",
                "media_type": media_type or ("video" if has_tg_video else "photo"),
                "media_url": media_url or thumb_url,
                "thumbnail": thumb_url,
                "video_urls": video_urls,
                "direct_tg_video": direct_tg_info,
                "resolved_cache": existing_item.get("resolved_cache", {}),
                "telegram_cloud_files": existing_item.get("telegram_cloud_files", {})
            }
            catalog[unique_key] = item_data
            save_catalog(catalog)

            print(f"[*] Post #{msg_id} ({item_data['date']}): {title}")

            # KASUS A: Video Telegram Langsung (seperti di KESUKAAN PARA LELAKI)
            if has_tg_video and direct_tg_info:
                fname = clean_filename(os.path.splitext(direct_tg_info["file_name"])[0]) + ".mp4"
                out_f = os.path.join(post_dir, fname)

                if os.path.exists(out_f) and os.path.getsize(out_f) > 50000:
                    print(f"   [Skip] Video Telegram langsung sudah ada: {fname} ({os.path.getsize(out_f)/(1024*1024):.1f} MB)")
                else:
                    print(f"   [Unduh TG] Mengunduh video langsung Telegram: {fname} ({direct_tg_info.get('size_str')})...")
                    temp_f = out_f + ".part"
                    try:
                        await client.download_media(msg.video, file=temp_f)
                        if os.path.exists(temp_f) and os.path.getsize(temp_f) > 50000:
                            if os.path.exists(out_f): os.remove(out_f)
                            os.rename(temp_f, out_f)
                            make_faststart(out_f)
                            print(f"   [V] Unduh video TG selesai: {fname} ({os.path.getsize(out_f)/(1024*1024):.1f} MB)")
                    except Exception as e:
                        print(f"   [!] Gagal unduh media Telegram: {e}")
                        if os.path.exists(temp_f):
                            try: os.remove(temp_f)
                            except Exception: pass

            # KASUS B: Video Link External (seperti di Drama CRT)
            resolved_cache = item_data.get("resolved_cache", {})
            all_targets = []

            for u in video_urls:
                r_list = resolved_cache.get(u)
                if not r_list:
                    r_list = resolve_universal(u)
                    if r_list:
                        resolved_cache[u] = r_list
                if r_list:
                    all_targets.extend(r_list)

            if resolved_cache:
                catalog[unique_key]["resolved_cache"] = resolved_cache
                save_catalog(catalog)

            if not all_targets:
                continue

            for idx, target in enumerate(all_targets):
                fname = clean_filename(target.get("title", f"part_{idx+1}")) + ".mp4"
                out_f = os.path.join(post_dir, fname)

                # Cek jika lokal sudah ada
                if os.path.exists(out_f) and os.path.getsize(out_f) > 50000:
                    print(f"   [Skip] File sudah ada: {fname} ({os.path.getsize(out_f)/(1024*1024):.1f} MB)")
                    if fname not in item_data.get("telegram_cloud_files", {}):
                        try:
                            print(f"   [Telegram Cloud] Mengunggah cepat (512KB chunks): {fname}...")
                            caption = f"🎬 {fname}\n📁 Post #{msg_id} - {title}\n☁️ Arsip Unlimited Cloud"
                            input_f = await client.upload_file(out_f, part_size_kb=512)
                            up_msg = await client.send_file(db_entity, input_f, caption=caption, supports_streaming=True)
                            catalog[unique_key].setdefault("telegram_cloud_files", {})[fname] = {
                                "msg_id": up_msg.id,
                                "date": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                            }
                            save_catalog(catalog)
                            print(f"   [+] Sukses tersimpan di Telegram Cloud!")
                        except Exception as e:
                            print(f"   [!] Gagal upload Telegram: {e}")
                    continue

                # Unduh file jika belum ada
                print(f"   [Unduh] Mengunduh: {fname} ({target.get('size_str', 'Cloud')})...")
                success = False
                if target.get("type") == "direct_mp4":
                    success = download_file(target["url"], out_f, target.get("referer"))
                elif target.get("type") == "hls":
                    success = download_hls(target["url"], out_f)

                if success and os.path.exists(out_f) and os.path.getsize(out_f) > 50000:
                    print(f"   [V] Unduh selesai: {fname} ({os.path.getsize(out_f)/(1024*1024):.1f} MB)")
                    make_faststart(out_f)

                    try:
                        print(f"   [Telegram Cloud] Mengunggah cepat (512KB chunks): {fname} ({os.path.getsize(out_f)/(1024*1024):.1f} MB)...")
                        caption = f"🎬 {fname}\n📁 Post #{msg_id} - {title}\n☁️ Arsip Unlimited Cloud"
                        input_f = await client.upload_file(out_f, part_size_kb=512)
                        up_msg = await client.send_file(db_entity, input_f, caption=caption, supports_streaming=True)
                        catalog[unique_key].setdefault("telegram_cloud_files", {})[fname] = {
                            "msg_id": up_msg.id,
                            "date": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        }
                        save_catalog(catalog)
                        print(f"   [+] Sukses tersimpan di Telegram Cloud!")
                    except Exception as e:
                        print(f"   [!] Gagal upload Telegram: {e}")

            await asyncio.sleep(0.3)

    print(f"\n🎉 SINKRONISASI & DOWNLOAD LENGKAP SEMUA SALURAN SELESAI!")
    await client.disconnect()

if __name__ == "__main__":
    asyncio.run(full_crawler_and_downloader())
