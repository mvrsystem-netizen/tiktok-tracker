#!/usr/bin/env python3
"""
TikTok Activity Tracker - Background Checker
Production-ready background worker for GitHub Actions & standalone server.
Zero-dependency implementation: runs on any standard Python 3.8+ environment without requiring pip.
Accurately detects:
- LIVE_HOST: Target broadcasting live on TikTok
- TAMU: Target joined as guest/co-host on active live room
- PENONTON: Target watching active live room
- OFFLINE: Target not active
"""

import os
import sys
import json
import re
import time
import urllib.request
import urllib.parse
import urllib.error
import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple, Set

# Konfigurasi Logging Ringkas & Bersih
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger("TikTokChecker")

# Kredensial Environment dari GitHub Secrets atau Default
SUPABASE_URL = os.getenv("SUPABASE_URL", "https://kxdiqgjeqjrlvxwscavy.supabase.co").rstrip("/")
SUPABASE_KEY = os.getenv(
    "SUPABASE_KEY",
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Imt4ZGlxZ2plcWpybHZ4d3NjYXZ5Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODk1NzUwMzIsImV4cCI6MjEwNTE1MTAzMn0.DeYH7w_fx9XQIpTx5IJccJBFKPOmlccloWbscpuD1eU"
)
NTFY_TOPIC = os.getenv("NTFY_TOPIC")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

if not SUPABASE_URL or not SUPABASE_KEY:
    logger.error("Error: SUPABASE_URL dan SUPABASE_KEY harus disetel.")
    sys.exit(1)


class SupabaseRestClient:
    """Klien REST Supabase mandiri berbasis urllib tanpa dependensi eksternal."""
    def __init__(self, base_url: str, api_key: str):
        self.base_url = base_url
        self.headers = {
            "apikey": api_key,
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }

    def get(self, endpoint: str) -> List[Dict]:
        url = f"{self.base_url}/rest/v1/{endpoint}"
        req = urllib.request.Request(url, headers=self.headers)
        try:
            with urllib.request.urlopen(req, timeout=10) as res:
                return json.loads(res.read().decode("utf-8"))
        except Exception as e:
            logger.debug("Supabase GET %s error: %s", endpoint, e)
            return []

    def post(self, endpoint: str, data: List[Dict], upsert: bool = False) -> bool:
        url = f"{self.base_url}/rest/v1/{endpoint}"
        headers = dict(self.headers)
        if upsert:
            headers["Prefer"] = "resolution=merge-duplicates"
        req = urllib.request.Request(url, data=json.dumps(data).encode("utf-8"), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as res:
                return res.status in (200, 201)
        except Exception as e:
            logger.debug("Supabase POST %s error: %s", endpoint, e)
            return False

    def patch(self, endpoint: str, data: Dict) -> bool:
        url = f"{self.base_url}/rest/v1/{endpoint}"
        req = urllib.request.Request(url, data=json.dumps(data).encode("utf-8"), headers=self.headers, method="PATCH")
        try:
            with urllib.request.urlopen(req, timeout=10) as res:
                return res.status in (200, 204)
        except Exception as e:
            logger.debug("Supabase PATCH %s error: %s", endpoint, e)
            return False


supabase = SupabaseRestClient(SUPABASE_URL, SUPABASE_KEY)


def send_free_push_notification(title: str, message: str, tags: Optional[List[str]] = None) -> None:
    """Mengirim push notification gratis ke HP pengguna via ntfy.sh dan/atau Telegram."""
    if NTFY_TOPIC:
        try:
            url = f"https://ntfy.sh/{NTFY_TOPIC}"
            req = urllib.request.Request(
                url,
                data=message.encode("utf-8"),
                headers={
                    "Title": title.encode("utf-8"),
                    "Priority": "high",
                    "Tags": ",".join(tags or ["rotating_light", "tiktok"]),
                },
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=5) as res:
                if res.status == 200:
                    logger.info("Notifikasi ntfy.sh berhasil terkirim.")
        except Exception as e:
            logger.warning("Gagal mengirim ntfy: %s", str(e))

    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        try:
            tg_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            payload = {
                "chat_id": TELEGRAM_CHAT_ID,
                "text": f"*{title}*\n{message}",
                "parse_mode": "Markdown"
            }
            req = urllib.request.Request(
                tg_url,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=5) as res:
                if res.status == 200:
                    logger.info("Notifikasi Telegram berhasil terkirim.")
        except Exception as e:
            logger.warning("Gagal mengirim Telegram: %s", str(e))


def get_live_room_data(username: str) -> Optional[Dict]:
    """
    Memeriksa status siaran live pengguna di TikTok secara akurat.
    Menggunakan multi-metode:
    1. Endpoint Webcast API resmi dengan header Referer & User-Agent valid
    2. Fallback parsing SIGI_STATE HTML untuk melewati bot detection
    Mengembalikan dict data room dan profil jika ditemukan, atau None.
    """
    clean_username = username.strip().lstrip("@").lower()
    live_page_url = f"https://www.tiktok.com/@{clean_username}/live"

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Referer": live_page_url,
        "Accept-Language": "id-ID,id;q=0.9,en-US;q=0.8",
    }

    # Metode 1: api-live dengan header Referer yang tepat
    endpoint = f"https://www.tiktok.com/api-live/user/room/?aid=1988&sourceType=54&uniqueId={clean_username}"
    try:
        req = urllib.request.Request(endpoint, headers=headers)
        with urllib.request.urlopen(req, timeout=6) as response:
            if response.status == 200:
                res_json = json.loads(response.read().decode("utf-8"))
                data = res_json.get("data") or {}
                live_room = data.get("liveRoom") or {}
                user = data.get("user") or {}
                status = live_room.get("status")

                if status is not None:
                    is_live = (status == 2)
                    raw_text = json.dumps(res_json)
                    room_id = user.get("roomId") or user.get("room_id") or live_room.get("roomId") or live_room.get("room_id") or ""
                    if not room_id and is_live:
                        m_room = re.search(r'"room_?id"[:\s]*"?(\d{15,25})"?', raw_text, re.IGNORECASE)
                        if m_room:
                            room_id = m_room.group(1)
                    return {
                        "is_live": is_live,
                        "status": status,
                        "room_id": str(room_id) if room_id else "",
                        "nickname": user.get("nickname", ""),
                        "avatar": user.get("avatarMedium") or user.get("avatarThumb"),
                        "title": live_room.get("title", "")
                    }
    except Exception as exc:
        logger.debug("api-live error untuk @%s: %s", clean_username, str(exc))

    # Metode 2: Fallback scraping HTML SIGI_STATE dengan Referer
    try:
        html_headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Referer": "https://www.tiktok.com/",
            "Accept-Language": "id-ID,id;q=0.9,en-US;q=0.8",
        }
        html_req = urllib.request.Request(live_page_url, headers=html_headers)
        with urllib.request.urlopen(html_req, timeout=7) as web_res:
            if web_res.status == 200:
                html = web_res.read().decode("utf-8", errors="ignore")
                m = re.search(r'<script id="SIGI_STATE"[^>]*>(.*?)</script>', html, re.DOTALL)
                if m:
                    sigi = json.loads(m.group(1))
                    live_user_info = sigi.get("LiveRoom", {}).get("liveRoomUserInfo", {})
                    user = live_user_info.get("user", {})
                    live_room = live_user_info.get("liveRoom", {})
                    status = live_room.get("status") or user.get("status")

                    room_id = user.get("roomId") or live_room.get("roomId") or ""
                    if not room_id:
                        m_rid = re.search(r'"room_?id"[:\s]*"?(\d{15,25})"?', html, re.IGNORECASE)
                        if m_rid:
                            room_id = m_rid.group(1)

                    if status is not None:
                        return {
                            "is_live": (status == 2),
                            "status": status,
                            "room_id": str(room_id) if room_id else "",
                            "nickname": user.get("nickname", ""),
                            "avatar": user.get("avatarMedium") or user.get("avatarThumb"),
                            "title": live_room.get("title", "")
                        }
    except Exception as exc:
        logger.debug("HTML SIGI fallback error untuk @%s: %s", clean_username, str(exc))

    return None


def fetch_room_participants(room_id: str, host_username: str) -> Tuple[Set[str], Set[str]]:
    """
    Mengambil daftar co-host/guest (TAMU) dan penonton (PENONTON) di dalam live room
    menggunakan endpoint resmi webcast/room/info TikTok.
    Mengembalikan (set_tamu, set_penonton) dalam huruf kecil.
    """
    guests: Set[str] = set()
    viewers: Set[str] = set()

    if not room_id:
        return guests, viewers

    try:
        url = f"https://webcast.tiktok.com/webcast/room/info/?room_id={room_id}&aid=1988"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
            "Referer": f"https://www.tiktok.com/@{host_username}/live",
            "Accept": "application/json, text/plain, */*",
        }
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=6) as res:
            if res.status == 200:
                data = json.loads(res.read().decode("utf-8")).get("data", {})

                # 1. Tamu / Komal / Link-Mic
                link_mic = data.get("link_mic", {})
                linked_users = link_mic.get("linked_user_list", []) or []
                for u in linked_users:
                    uname = u.get("display_id") or u.get("unique_id") or u.get("nickname")
                    if uname:
                        guests.add(str(uname).lower().strip().lstrip("@"))

                for u in link_mic.get("show_user_list", []) or []:
                    uname = u.get("display_id") or u.get("unique_id")
                    if uname:
                        guests.add(str(uname).lower().strip().lstrip("@"))

                # 2. Penonton / Top Fans
                top_fans = data.get("top_fans", []) or []
                for f in top_fans:
                    u = f.get("user", {}) if isinstance(f.get("user"), dict) else f
                    uname = u.get("display_id") or u.get("unique_id")
                    if uname:
                        viewers.add(str(uname).lower().strip().lstrip("@"))
    except Exception as e:
        logger.debug("Error fetch_room_participants room %s: %s", room_id, str(e))

    return guests, viewers


def fetch_targets_and_hosts() -> Tuple[List[Dict], List[Dict]]:
    """Mengambil daftar targets dan hosts dari database Supabase."""
    targets = supabase.get("targets?select=id,username,display_name")
    hosts = supabase.get("hosts?select=id,username,display_name")
    return targets, hosts


def get_latest_logged_status(target_username: str) -> Optional[Dict]:
    """Mengambil log terakhir dari target untuk membandingkan perubahan status."""
    clean = target_username.strip().lstrip("@").lower()
    logs = supabase.get(f"activity_logs?select=status,host_username,timestamp&target_username=eq.{clean}&order=timestamp.desc&limit=1")
    if logs and len(logs) > 0:
        return logs[0]
    return None


def run_checker():
    """Fungsi utama pengecekan background."""
    logger.info("Memulai siklus pengecekan TikTok Activity Tracker...")
    
    targets, hosts = fetch_targets_and_hosts()
    logger.info("Ditemukan %d target dan %d host terdaftar di Supabase.", len(targets), len(hosts))

    if not targets:
        logger.info("Tidak ada target yang dipantau. Selesai.")
        return

    target_map: Dict[str, Dict] = {
        t["username"].strip().lower().lstrip("@"): t for t in targets
    }
    
    detected_state: Dict[str, Tuple[str, Optional[str]]] = {}

    # 1. Cek apakah ada Target yang sedang LIVE_HOST sendiri
    for uname, target_data in target_map.items():
        room_data = get_live_room_data(uname)
        if room_data and room_data.get("is_live"):
            detected_state[uname] = ("LIVE_HOST", uname)
            logger.info("Target @%s terdeteksi sedang LIVE_HOST!", uname)
            
        # Update profil display_name / avatar jika ada
        if room_data and room_data.get("nickname"):
            try:
                update_fields = {"display_name": room_data["nickname"]}
                if room_data.get("avatar"):
                    update_fields["avatar_url"] = room_data["avatar"]
                supabase.patch(f"targets?username=eq.{target_data['username']}", update_fields)
            except Exception:
                pass
        time.sleep(0.3)

    # 2. Cek semua Host yang sedang Live dan periksa keberadaan Target di room mereka
    for h in hosts:
        host_uname = h["username"].strip().lower().lstrip("@")
        room_data = get_live_room_data(host_uname)

        if not room_data or not room_data.get("is_live"):
            continue

        logger.info("Host @%s sedang LIVE aktif.", host_uname)
        
        if host_uname in target_map and host_uname not in detected_state:
            detected_state[host_uname] = ("LIVE_HOST", host_uname)

        if room_data.get("nickname"):
            try:
                update_fields = {"display_name": room_data["nickname"]}
                if room_data.get("avatar"):
                    update_fields["avatar_url"] = room_data["avatar"]
                supabase.patch(f"hosts?username=eq.{h['username']}", update_fields)
            except Exception:
                pass

        room_id = str(room_data.get("room_id") or "")
        guests, viewers = fetch_room_participants(room_id, host_uname)
        time.sleep(0.3)

        for uname in target_map.keys():
            if uname in detected_state and detected_state[uname][0] == "LIVE_HOST":
                continue

            if uname in guests:
                detected_state[uname] = ("TAMU", host_uname)
                logger.info("Target @%s terdeteksi sebagai TAMU di live @%s", uname, host_uname)
            elif uname in viewers:
                detected_state[uname] = ("PENONTON", host_uname)
                logger.info("Target @%s terdeteksi sebagai PENONTON di live @%s", uname, host_uname)

    # 3. Target yang tidak terdeteksi di room manapun berstatus OFFLINE
    for uname in target_map.keys():
        if uname not in detected_state:
            detected_state[uname] = ("OFFLINE", None)

    # 4. Sinkronisasi ke Supabase & Kirim Push Notification jika terjadi perubahan status
    new_logs_to_insert = []
    
    for uname, (status, host_uname) in detected_state.items():
        target_info = target_map[uname]
        raw_target_username = target_info["username"]
        display_name = target_info.get("display_name") or raw_target_username

        latest_log = get_latest_logged_status(raw_target_username)
        last_status = latest_log.get("status") if latest_log else None
        last_host = latest_log.get("host_username") if latest_log else None

        status_changed = (last_status != status) or (last_host != host_uname)

        if status_changed:
            log_item = {
                "target_username": raw_target_username,
                "status": status,
                "host_username": host_uname,
                "timestamp": datetime.now(timezone.utc).isoformat()
            }
            new_logs_to_insert.append(log_item)

            time_str = datetime.now().strftime("%H:%M")
            if status == "LIVE_HOST":
                msg = f"{display_name} (@{raw_target_username}) memulai Live Streaming sendiri - {time_str}"
                title = "🔴 Target Sedang Live"
            elif status == "TAMU":
                msg = f"{display_name} sebagai Tamu di live {host_uname} - {time_str}"
                title = "🎤 Target Komal/Tamu"
            elif status == "PENONTON":
                msg = f"{display_name} menonton di live {host_uname} - {time_str}"
                title = "👀 Target Menonton Live"
            else:
                msg = f"{display_name} (@{raw_target_username}) sekarang Offline - {time_str}"
                title = "⚪ Target Offline"

            logger.info("Perubahan status: %s", msg)
            send_free_push_notification(title, msg)

    if new_logs_to_insert:
        supabase.post("activity_logs", new_logs_to_insert)
        logger.info("Berhasil menyimpan %d log aktivitas baru ke Supabase.", len(new_logs_to_insert))
    else:
        logger.info("Tidak ada perubahan status pada target. Database sinkron.")

    # 5. Update bot heartbeat
    try:
        heartbeat_payload = [{
            "id": "00000000-0000-0000-0000-000000000001",
            "bot_name": "tiktok_checker",
            "last_ping": datetime.now(timezone.utc).isoformat(),
            "status": "ACTIVE"
        }]
        supabase.post("bot_heartbeat", heartbeat_payload, upsert=True)
        logger.info("Heartbeat bot berhasil diperbarui ke Supabase.")
    except Exception as hb_err:
        logger.debug("Heartbeat info: %s", str(hb_err))

    logger.info("Pengecekan selesai.")


if __name__ == "__main__":
    run_checker()
